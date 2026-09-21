import GLib from 'gi://GLib';
import UPower from 'gi://UPowerGlib';

import {Extension, InjectionManager} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const FIND_INTERVAL_MS = 100;

/**
 * Formats the time UPower estimates until the battery is empty or full.
 *
 * @param {Gio.DBusProxy} proxy - the proxy for UPower's display device
 * @returns {string|null} the time as hours and minutes, or null when there is no estimate, which hides the subtitle
 */
function remainingTime(proxy) {
    let seconds, suffix;
    switch (proxy.State) {
    case UPower.DeviceState.DISCHARGING:
        seconds = proxy.TimeToEmpty;
        suffix = 'left';
        break;
    case UPower.DeviceState.CHARGING:
        seconds = proxy.TimeToFull;
        suffix = 'until full';
        break;
    default:
        // Also while the battery is held at a charge threshold, since it is then neither charging nor discharging.
        return null;
    }
    // UPower reports zero while it is still estimating.
    if (!seconds)
        return null;
    const minutes = Math.round(seconds / 60);
    return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')} ${suffix}`;
}

export default class BatteryTimeExtension extends Extension {
    enable() {
        this._injectionManager = new InjectionManager();
        if (this._setup())
            return;
        // The shell creates the quick settings indicators asynchronously, so at startup they can be missing still
        // when extensions are enabled.
        this._findId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, FIND_INTERVAL_MS, () => {
            if (!this._setup())
                return GLib.SOURCE_CONTINUE;
            this._findId = 0;
            return GLib.SOURCE_REMOVE;
        });
    }

    disable() {
        if (this._findId) {
            GLib.source_remove(this._findId);
            this._findId = 0;
        }
        this._injectionManager.clear();
        this._injectionManager = null;
        if (this._toggle) {
            this._toggle.subtitle = null;
            this._toggle = null;
        }
    }

    _setup() {
        const toggle = Main.panel.statusArea.quickSettings._system?._systemItem.powerToggle;
        if (!toggle)
            return false;
        this._toggle = toggle;
        // The toggle updates itself in _sync whenever UPower's display device changes, including once the proxy has
        // loaded its properties, so the subtitle is set right after it every time.
        this._injectionManager.overrideMethod(Object.getPrototypeOf(toggle), '_sync', originalMethod => function (...args) {
            originalMethod.apply(this, args);
            this.subtitle = remainingTime(this._proxy);
        });
        toggle._sync();
        return true;
    }
}
