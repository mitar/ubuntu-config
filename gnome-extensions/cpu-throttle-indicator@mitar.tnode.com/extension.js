import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {REASONS, decodeMetrics, dominantReason, findMetricsFile, throttleShares} from './metrics.js';

const SAMPLE_INTERVAL_S = 2;

// Below this share of an interval a reason counts as holding nothing back. The processor brushes against its limits
// briefly whenever it boosts, and reporting that would leave the indicator showing almost all of the time.
const THRESHOLD_PERCENT = 10;

// How many samples in a row have to stay under the threshold before the indicator goes away again. Starting a
// program drains the fast power budget for about one sample, so leaving at the first quiet sample would blink.
const HIDE_AFTER_SAMPLES = 2;

const Indicator = GObject.registerClass(
class ThrottleIndicator extends PanelMenu.Button {
    _init(iconsPath, file) {
        super._init(0, 'CPU Throttle Indicator');

        this._file = file;
        this._cancellable = new Gio.Cancellable();
        this._previous = null;
        this._reading = false;
        this._samplesUnderThreshold = 0;
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

        this._shareLabels = new Map();
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Limits'));
        for (const reason of REASONS)
            this._shareLabels.set(reason.key, this._addRow(reason.label));
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Sensors'));
        this._powerLabel = this._addRow('Package power');
        this._coreLabel = this._addRow('Hottest core');
        this._gfxLabel = this._addRow('Graphics');
        this._socLabel = this._addRow('SoC');
        this._skinLabel = this._addRow('Skin');

        // Nothing is known until two readings have been compared, and until then there is nothing to show.
        this.hide();

        this._timeoutId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, SAMPLE_INTERVAL_S, () => {
            this._sample();
            return GLib.SOURCE_CONTINUE;
        });
        this._sample();
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
        this._powerLabel.text = `${metrics.socketPower.toFixed(1)} W`;
        this._coreLabel.text = `${metrics.temperatureCore.toFixed(1)} C`;
        this._gfxLabel.text = `${metrics.temperatureGfx.toFixed(1)} C`;
        this._socLabel.text = `${metrics.temperatureSoc.toFixed(1)} C`;
        this._skinLabel.text = `${metrics.temperatureSkin.toFixed(1)} C`;

        const dominant = dominantReason(shares, THRESHOLD_PERCENT);
        if (dominant) {
            this._samplesUnderThreshold = 0;
            this._icon.gicon = this._icons[dominant.kind];
            this._reasonLabel.text = dominant.short;
            this.show();
            return;
        }
        this._samplesUnderThreshold++;
        if (this._samplesUnderThreshold >= HIDE_AFTER_SAMPLES && this.visible) {
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
        this._indicator = new Indicator(`${this.path}/icons`, file);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
