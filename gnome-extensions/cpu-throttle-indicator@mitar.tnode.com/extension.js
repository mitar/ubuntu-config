import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {FAN_MAX_RPM, findApuSensorFile, findFanFiles} from './ec.js';
import {CORE_GROUPS, REASONS, decodeMetrics, dominantReason, findMetricsFile, throttleShares} from './metrics.js';

const SAMPLE_INTERVAL_S = 2;

// Below this share of an interval a reason counts as holding nothing back. The processor brushes against its limits
// briefly whenever it boosts, and reporting that would leave the indicator showing almost all of the time.
const THRESHOLD_PERCENT = 10;

// How many samples in a row have to stay under the threshold before the indicator goes away again. Starting a
// program drains the fast power budget for about one sample, so leaving at the first quiet sample would blink.
const HIDE_AFTER_SAMPLES = 2;

// The hottest core's temperature from which a fan is expected to spin. The embedded controller runs the fan from the
// sensors on the board only, and starts it once one of them passes about 50 C, which the board reaches some time
// after the cores reach this.
const FAN_NEEDED_ABOVE_C = 70;

// How many samples in a row no fan has to spin while the hottest core is at FAN_NEEDED_ABOVE_C or more before that is
// shown as a fault. It has to outlast the board warming up to where the EC starts the fan. Starting from a cool board
// with the fan off, the EC was measured to start the fan at most about 10 seconds after the hottest core reached
// FAN_NEEDED_ABOVE_C, for loads from a few threads to all of them, while lighter loads warm the board enough before
// the cores get there, or never get the cores there.
const FAN_OFF_FAULT_AFTER_SAMPLES = 15;

// The theme's red pill for a top bar indicator which needs attention, the one the shell shows while recording the
// screen. Borrowing it keeps the warning in the theme's colors, light or dark.
const WARNING_PANEL_STYLE_CLASS = 'screen-recording-indicator';

const Indicator = GObject.registerClass(
class ThrottleIndicator extends PanelMenu.Button {
    _init(iconsPath, file, apuSensorFile, fanFiles) {
        super._init(0, 'CPU Throttle Indicator');

        this._file = file;
        this._apuSensorFile = apuSensorFile;
        this._cancellable = new Gio.Cancellable();
        this._previous = null;
        this._reading = false;
        this._readingApuSensor = false;
        this._samplesUnderThreshold = 0;
        this._samplesFanOffWhileHot = 0;
        // The reason the top bar names, which stays for HIDE_AFTER_SAMPLES after it stops holding anything back.
        this._throttle = null;
        this._icons = {
            power: Gio.icon_new_for_string(`${iconsPath}/cpu-throttle-power-symbolic.svg`),
            thermal: Gio.icon_new_for_string(`${iconsPath}/cpu-throttle-thermal-symbolic.svg`),
            // The platform does not report why it asserted PROCHOT, so it is shown with the shell's own warning
            // icon rather than being presented as either a power or a temperature limit.
            platform: new Gio.ThemedIcon({name: 'dialog-warning-symbolic'}),
        };

        const box = new St.BoxLayout({style_class: 'panel-status-indicators-box'});
        this._icon = new St.Icon({style_class: 'system-status-icon'});
        this._reasonLabel = new St.Label({y_align: Clutter.ActorAlign.CENTER});
        box.add_child(this._icon);
        box.add_child(this._reasonLabel);
        this.add_child(box);

        this._warningItem = this._addWarning('Fan is off while the processor is hot',
            ['Fan control may have stopped reading its sensors.', 'Suspend and resume to make it read them again.']);

        this._shareLabels = new Map();
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Limits'));
        for (const reason of REASONS)
            this._shareLabels.set(reason.key, this._addRow(reason.label));
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Frequency'));
        this._frequencyLabels = CORE_GROUPS.map(group => this._addRow(`${group.name} cores`));
        if (fanFiles.length)
            this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Fans'));
        // Each row of the section shows one file, either the speed of a fan or its target.
        this._fanRows = [];
        for (const fan of fanFiles) {
            // Fans are numbered only when there are several of them to tell apart.
            const name = fanFiles.length === 1 ? 'Fan' : `Fan ${fan.number}`;
            this._fanRows.push({
                kind: 'speed',
                file: fan.file,
                label: this._addRow(name),
                reading: false,
                // Undefined until the fan has been read, and null while the EC reports the fan as stalled.
                speed: undefined,
            });
            if (fan.targetFile) {
                this._fanRows.push({
                    kind: 'target',
                    file: fan.targetFile,
                    label: this._addRow(`${name} target`),
                    reading: false,
                });
            }
        }
        this._fanSpeedRows = this._fanRows.filter(row => row.kind === 'speed');
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Sensors'));
        this._powerLabel = this._addRow('APU power (SPL budget)');
        this._corePowerLabel = this._addRow('CPU cores');
        this._graphicsPowerLabel = this._addRow('Graphics');
        this._coreLabel = this._addRow('Hottest core');
        this._gfxLabel = this._addRow('Graphics temperature');
        this._socLabel = this._addRow('SoC temperature');
        this._skinLabel = this._addRow('Skin temperature');
        if (apuSensorFile)
            this._apuSensorLabel = this._addRow('APU temperature (EC)');

        // Nothing is known until two readings have been compared, and until then there is nothing to show.
        this.hide();

        this._timeoutId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, SAMPLE_INTERVAL_S, () => {
            this._sample();
            this._sampleApuSensor();
            this._sampleFans();
            return GLib.SOURCE_CONTINUE;
        });
        this._sample();
        this._sampleApuSensor();
        this._sampleFans();
    }

    // PanelMenu.Button connects this to its own destroy signal, and destroys the menu and the container in it, so
    // an override has to chain up for that to still happen.
    _onDestroy() {
        this._cancellable.cancel();
        if (this._timeoutId) {
            GLib.source_remove(this._timeoutId);
            this._timeoutId = 0;
        }
        super._onDestroy();
    }

    /**
     * Adds a row to the menu which names something and leaves room for its value.
     *
     * @param {string} text - the name to show on the left of the row
     * @returns {St.Label} the label on the right of the row, to set the value on
     */
    _addRow(text) {
        const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const value = new St.Label({y_align: Clutter.ActorAlign.CENTER});
        item.add_child(new St.Label({text, x_expand: true, y_align: Clutter.ActorAlign.CENTER}));
        item.add_child(value);
        this.menu.addMenuItem(item);
        return value;
    }

    /**
     * Adds a warning to the menu, hidden until it is shown.
     *
     * @param {string} title - the first line of the warning, saying what is wrong
     * @param {string[]} lines - the lines under the title
     * @returns {PopupMenu.PopupBaseMenuItem} the menu item holding the warning, to show and hide
     */
    _addWarning(title, lines) {
        const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const box = new St.BoxLayout({style_class: 'cpu-throttle-indicator-warning', x_expand: true});
        const text = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, y_align: Clutter.ActorAlign.CENTER});
        text.add_child(new St.Label({text: title, style_class: 'cpu-throttle-indicator-warning-title'}));
        for (const line of lines)
            text.add_child(new St.Label({text: line}));
        box.add_child(new St.Icon({
            icon_name: 'dialog-warning-symbolic',
            style_class: 'popup-menu-icon',
            y_align: Clutter.ActorAlign.CENTER,
        }));
        box.add_child(text);
        item.add_child(box);
        item.hide();
        this.menu.addMenuItem(item);
        return item;
    }

    _sample() {
        // A read is answered by the SMU rather than out of memory, so it is kept off the shell's main loop. A read
        // still running when the next one is due is left to finish on its own instead of queueing another behind
        // it, so that a slow one cannot build up a backlog.
        if (this._reading)
            return;
        this._reading = true;
        this._file.load_bytes_async(this._cancellable, (file, result) => {
            let bytes;
            try {
                [bytes] = file.load_bytes_finish(result);
            } catch (error) {
                if (!error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    console.warn(`CPU Throttle Indicator: cannot read ${file.get_path()}: ${error.message}`);
                this._reading = false;
                return;
            }
            this._reading = false;
            // Cancelling happens as the indicator is being destroyed, and a read that had already completed by then
            // still arrives here, with widgets that must not be touched any more.
            if (!this._cancellable.is_cancelled())
                this._update(decodeMetrics(bytes.get_data()), GLib.get_monotonic_time());
        });
    }

    _sampleApuSensor() {
        // The file is read off the main loop and one read at a time, the same as the metrics table.
        if (!this._apuSensorFile || this._readingApuSensor)
            return;
        this._readingApuSensor = true;
        this._apuSensorFile.load_bytes_async(this._cancellable, (file, result) => {
            this._readingApuSensor = false;
            let temperature = null;
            try {
                const [bytes] = file.load_bytes_finish(result);
                temperature = Number(new TextDecoder().decode(bytes.get_data())) / 1000;
            } catch (error) {
                if (error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    return;
                // INVALID_DATA is the EC failing to read the sensor. Any other error is about reaching the file and
                // says nothing about the sensor, so the menu keeps showing what it showed.
                if (!error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.INVALID_DATA)) {
                    console.warn(`CPU Throttle Indicator: cannot read ${file.get_path()}: ${error.message}`);
                    return;
                }
            }
            if (!this._cancellable.is_cancelled())
                this._updateApuSensor(temperature);
        });
    }

    /**
     * Takes a reading of the APU temperature the embedded controller reports, and shows it in the menu.
     *
     * @param {number|null} temperature - the temperature in degrees Celsius, or null when the EC cannot read the sensor
     */
    _updateApuSensor(temperature) {
        this._apuSensorLabel.text = temperature === null ? 'error' : `${temperature.toFixed(1)} C`;
    }

    _sampleFans() {
        // Each file is read off the main loop and one read at a time, the same as the metrics table.
        for (const row of this._fanRows) {
            if (row.reading)
                continue;
            row.reading = true;
            row.file.load_bytes_async(this._cancellable, (file, result) => {
                row.reading = false;
                let speed = null;
                try {
                    const [bytes] = file.load_bytes_finish(result);
                    speed = Number(new TextDecoder().decode(bytes.get_data()));
                } catch (error) {
                    if (error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                        return;
                    // INVALID_DATA for the speed is the EC reporting the fan as stalled. Any other error, and any error
                    // for the target, is about reaching the EC and says nothing about the fan, so the menu keeps
                    // showing what it showed.
                    if (row.kind !== 'speed' || !error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.INVALID_DATA)) {
                        console.warn(`CPU Throttle Indicator: cannot read ${file.get_path()}: ${error.message}`);
                        return;
                    }
                }
                if (!this._cancellable.is_cancelled())
                    this._updateFanRow(row, speed);
            });
        }
    }

    /**
     * Takes a reading of the speed of a fan or of its target, and shows it in the menu.
     *
     * @param {object} row - the row the reading is for, as an entry of this._fanRows
     * @param {number|null} speed - the speed in RPM, or null when the EC reports the fan as stalled
     */
    _updateFanRow(row, speed) {
        row.speed = speed;
        row.label.text = speed === null ? 'stalled' : `${speed} RPM, ${Math.round(100 * speed / FAN_MAX_RPM)}%`;
    }

    /**
     * Takes a reading and updates the indicator and its menu from it and the reading before it.
     *
     * @param {object|null} metrics - the reading, or null when the table could not be decoded
     * @param {number} timestamp - the monotonic time in microseconds at which the reading was taken
     */
    _update(metrics, timestamp) {
        // A table that cannot be decoded says nothing about whether anything is being held back, which is not the
        // same as saying that nothing is, so the indicator keeps showing what it showed.
        if (!metrics)
            return;

        // A fan which the EC reports as stalled does not spin either, while one which has not been read yet counts as
        // spinning, so that nothing is reported before it is known.
        const fansOff = this._fanSpeedRows.length > 0 &&
            this._fanSpeedRows.every(row => row.speed === 0 || row.speed === null);
        if (!fansOff) {
            this._samplesFanOffWhileHot = 0;
        } else if (metrics.temperatureCore >= FAN_NEEDED_ABOVE_C) {
            this._samplesFanOffWhileHot++;
        } else if (this._samplesFanOffWhileHot < FAN_OFF_FAULT_AFTER_SAMPLES) {
            // A burst of load which ends before the board warms up rightly never starts the fan, so a cool sample
            // starts the count over. Once the fault is shown, it stays until a fan spins again, because fan control
            // which stopped reading its sensors does not recover as the processor cools down.
            this._samplesFanOffWhileHot = 0;
        }

        const previous = this._previous;
        this._previous = {metrics, timestamp};
        // The counters are totals since boot, so a share of an interval needs the reading that bounds it.
        if (!previous)
            return;
        const elapsed = (timestamp - previous.timestamp) / 1000;
        if (elapsed <= 0)
            return;

        const shares = throttleShares(previous.metrics, metrics, elapsed);
        for (const reason of REASONS)
            this._shareLabels.get(reason.key).text = `${Math.round(shares[reason.key])}%`;
        // A group which is not working is slow because it has nothing to do, so reporting a share of its maximum
        // would say nothing about whether anything is holding it back.
        metrics.coreGroups.forEach((group, index) => {
            this._frequencyLabels[index].text = group.active
                ? `${group.frequency} MHz, ${Math.round(100 * group.frequency / group.maxFrequency)}%`
                : 'idle';
        });
        this._powerLabel.text = `${metrics.socketPower.toFixed(1)} W`;
        this._corePowerLabel.text = `${metrics.corePower.toFixed(1)} W`;
        this._graphicsPowerLabel.text = `${metrics.graphicsPower.toFixed(1)} W`;
        this._coreLabel.text = `${metrics.temperatureCore.toFixed(1)} C`;
        this._gfxLabel.text = `${metrics.temperatureGfx.toFixed(1)} C`;
        this._socLabel.text = `${metrics.temperatureSoc.toFixed(1)} C`;
        this._skinLabel.text = `${metrics.temperatureSkin.toFixed(1)} C`;

        const dominant = dominantReason(shares, THRESHOLD_PERCENT);
        if (dominant) {
            this._samplesUnderThreshold = 0;
            this._throttle = dominant;
        } else {
            this._samplesUnderThreshold++;
            if (this._samplesUnderThreshold >= HIDE_AFTER_SAMPLES)
                this._throttle = null;
        }
        this._render();
    }

    /**
     * Shows in the top bar what currently deserves attention, and hides the indicator when nothing does.
     */
    _render() {
        const fanFault = this._samplesFanOffWhileHot >= FAN_OFF_FAULT_AFTER_SAMPLES;
        this._warningItem.visible = fanFault;
        // Without the fan the APU heats up until its temperature limit holds it back. That takes the place of
        // whatever is holding it back right now.
        if (fanFault) {
            this._icon.gicon = this._icons.thermal;
            this._reasonLabel.text = 'THM';
            this.add_style_class_name(WARNING_PANEL_STYLE_CLASS);
            this.show();
            return;
        }
        this.remove_style_class_name(WARNING_PANEL_STYLE_CLASS);
        if (this._throttle) {
            this._icon.gicon = this._icons[this._throttle.kind];
            this._reasonLabel.text = this._throttle.short;
            this.show();
        } else if (this.visible) {
            this.menu.close();
            this.hide();
        }
    }
});

export default class CpuThrottleIndicatorExtension extends Extension {
    enable() {
        const file = findMetricsFile();
        // Without a table there is nothing this could ever report, so rather than sitting in the top bar saying
        // nothing it stays out of it and leaves a line in the journal saying why.
        if (!file) {
            console.warn(`${this.uuid}: no device publishes a metrics table this can read, not showing an indicator`);
            return;
        }
        // An embedded controller which does not report the APU temperature leaves the menu without it, and one which
        // reports no fan leaves the menu without its fans and the indicator without the warning about them.
        this._indicator = new Indicator(`${this.path}/icons`, file, findApuSensorFile(), findFanFiles());
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
