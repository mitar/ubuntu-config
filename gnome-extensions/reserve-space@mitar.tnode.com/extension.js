import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

// How long the reserved space has to stay unchanged before the placeholders follow it. After an unlock the shell
// enables the dock again only once the screen is unlocked, and the dock settles its size only after its first paint,
// so placeholders which followed sooner would give the dock's space to windows in between.
const SETTLE_TIMEOUT_MS = 5000;

/**
 * Returns the part of the screen an actor reserves, computed the same way the shell computes it when it makes a strut
 * out of the actor, so that a placeholder at the returned rectangle makes the same strut.
 *
 * @param {Clutter.Actor} actor - an actor which the shell tracks as chrome affecting struts
 * @returns {{x: number, y: number, width: number, height: number}} the rectangle in stage coordinates
 */
function reservedRect(actor) {
    actor.get_allocation_box();
    const [x, y] = actor.get_transformed_position();
    const [width, height] = actor.get_transformed_size();
    return {x: Math.round(x), y: Math.round(y), width: Math.round(width), height: Math.round(height)};
}

/**
 * Orders rectangles by their position and then by their size.
 *
 * @param {{x: number, y: number, width: number, height: number}} a - the first rectangle
 * @param {{x: number, y: number, width: number, height: number}} b - the second rectangle
 * @returns {number} a negative number, zero or a positive number when a comes before, equals or comes after b
 */
function compareRects(a, b) {
    return a.x - b.x || a.y - b.y || a.width - b.width || a.height - b.height;
}

// An actor which only reserves space. Without content it draws nothing, and it is never picked, so it takes no clicks
// and does not hide the dock from drag and drop, which picks actors whether they are reactive or not.
const Placeholder = GObject.registerClass(
class Placeholder extends Clutter.Actor {
    vfunc_pick(_pickContext) {
    }
});

/**
 * Keeps the screen space reserved by chrome, such as the dock and the top bar, while the screen is locked. Every part
 * of the screen which chrome reserves gets an invisible placeholder which reserves the same space. The shell disables
 * extensions such as the dock while the screen is locked, but the placeholders stay, so the work area does not change
 * and snapped and maximized windows keep their size. While the screen is unlocked the placeholders follow the
 * reserved space once it has stayed unchanged for a few seconds.
 */
export default class ReserveSpaceExtension extends Extension {
    enable() {
        this._sources = new Set();
        this._placeholders = [];
        this._rects = [];
        this._settleId = 0;
        Main.sessionMode.connectObject('updated', () => this._queueUpdate(), this);
        Main.layoutManager.connectObject('monitors-changed', () => this._queueUpdate(), this);
        global.display.connectObject('workareas-changed', () => this._queueUpdate(), this);
        this._update();
    }

    disable() {
        this._cancelUpdate();
        Main.sessionMode.disconnectObject(this);
        Main.layoutManager.disconnectObject(this);
        global.display.disconnectObject(this);
        for (const actor of this._sources)
            actor.disconnectObject(this);
        this._sources = null;
        for (const placeholder of this._placeholders)
            placeholder.destroy();
        this._placeholders = null;
        this._rects = null;
    }

    _cancelUpdate() {
        if (this._settleId) {
            GLib.source_remove(this._settleId);
            this._settleId = 0;
        }
    }

    _queueUpdate() {
        this._cancelUpdate();
        // While the screen is locked the placeholders stay as they are, which is what keeps the space of the dock
        // after the shell disables it for the lock screen.
        if (Main.sessionMode.isLocked)
            return;
        this._settleId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, SETTLE_TIMEOUT_MS, () => {
            this._settleId = 0;
            this._update();
            return GLib.SOURCE_REMOVE;
        });
    }

    _update() {
        if (Main.sessionMode.isLocked)
            return;

        // _trackedActors is private to the layout manager. It is the list of chrome the shell makes struts from.
        const sources = Main.layoutManager._trackedActors
            .filter(({actor, affectsStruts}) => affectsStruts && !this._placeholders.includes(actor))
            .map(({actor}) => actor);

        // The shell updates struts when the allocation of tracked chrome changes, so the same signal tells when the
        // reserved space changes. The work area does not always change with it, for example when the dock gets
        // narrower while its placeholder still reserves the earlier width.
        for (const actor of sources) {
            if (this._sources.has(actor))
                continue;
            this._sources.add(actor);
            actor.connectObject(
                'notify::allocation', () => this._queueUpdate(),
                'destroy', () => {
                    this._sources.delete(actor);
                    this._queueUpdate();
                }, this);
        }
        for (const actor of this._sources) {
            if (sources.includes(actor))
                continue;
            actor.disconnectObject(this);
            this._sources.delete(actor);
        }

        // Sorted, so that each placeholder keeps its rectangle when the shell tracks the same chrome in another order,
        // as it does after the dock is created again.
        const rects = sources.map(reservedRect).sort(compareRects);
        if (rects.length === this._rects.length && rects.every((rect, i) => compareRects(rect, this._rects[i]) === 0))
            return;
        this._rects = rects;

        while (this._placeholders.length > rects.length)
            this._placeholders.pop().destroy();
        while (this._placeholders.length < rects.length) {
            const placeholder = new Placeholder({name: 'reserveSpacePlaceholder', reactive: false});
            Main.layoutManager.addChrome(placeholder, {affectsStruts: true, affectsInputRegion: false});
            this._placeholders.push(placeholder);
        }
        rects.forEach(({x, y, width, height}, i) => {
            this._placeholders[i].set_position(x, y);
            this._placeholders[i].set_size(width, height);
        });
    }
}
