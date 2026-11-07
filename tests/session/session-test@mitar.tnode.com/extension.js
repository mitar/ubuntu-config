import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {ExtensionState} from 'resource:///org/gnome/shell/misc/extensionUtils.js';
import * as Util from 'resource:///org/gnome/shell/misc/util.js';

/**
 * Returns the window with the given ID.
 *
 * @param {number} id - the window's ID, as windows() reports it
 * @returns {Meta.Window} the window
 * @throws {Error} when there is no such window
 */
function windowById(id) {
    const window = global.get_window_actors().map(actor => actor.meta_window).find(w => w.get_id() === id);
    if (!window)
        throw new Error(`No window ${id}`);
    return window;
}

/**
 * Returns the application with the given ID.
 *
 * @param {string} appId - the ID of the application's desktop file, such as org.gnome.TextEditor.desktop
 * @returns {Shell.App} the application
 * @throws {Error} when there is no such application
 */
function appById(appId) {
    const app = Shell.AppSystem.get_default().lookup_app(appId);
    if (!app)
        throw new Error(`No application ${appId}`);
    return app;
}

/**
 * The methods the session tests call through Eval, as globalThis.sessionTest.<method>(...). Every method returns a
 * value which JSON can represent.
 */
class SessionTest {
    /**
     * @returns {boolean} whether GNOME Shell has finished starting up
     */
    ready() {
        return !Main.layoutManager._startingUp;
    }

    /**
     * @returns {object[]} the normal windows and dialogs, each with the properties the tests check
     */
    windows() {
        const tracker = Shell.WindowTracker.get_default();
        return global.get_window_actors().map(actor => actor.meta_window).filter(w => !w.is_override_redirect()).map(w => {
            const rect = w.get_frame_rect();
            const workspace = w.get_workspace();
            return {
                id: w.get_id(),
                title: w.get_title(),
                wmClass: w.get_wm_class(),
                gtkApplicationId: w.get_gtk_application_id(),
                app: tracker.get_window_app(w)?.get_id() ?? null,
                pid: w.get_pid(),
                type: w.get_window_type(),
                workspace: workspace ? workspace.index() : null,
                onAllWorkspaces: w.is_on_all_workspaces(),
                monitor: w.get_monitor(),
                x: rect.x,
                y: rect.y,
                width: rect.width,
                height: rect.height,
                focused: w.has_focus(),
                demandsAttention: w.demands_attention,
                minimized: w.minimized,
                maximized: w.get_maximize_flags() === Meta.MaximizeFlags.BOTH,
                fullscreen: w.is_fullscreen(),
            };
        });
    }

    /**
     * @returns {{count: number, active: number}} the number of workspaces and the index of the active one
     */
    workspaces() {
        const manager = global.workspace_manager;
        return {count: manager.get_n_workspaces(), active: manager.get_active_workspace_index()};
    }

    /**
     * Switches to a workspace, as the workspace switcher does.
     *
     * @param {number} index - the index of the workspace
     */
    activateWorkspace(index) {
        global.workspace_manager.get_workspace_by_index(index).activate(global.get_current_time());
    }

    /**
     * Moves a window to a workspace, as its window menu does, without switching to it.
     *
     * @param {number} id - the window's ID
     * @param {number} index - the index of the workspace
     */
    moveWindowToWorkspace(id, index) {
        windowById(id).change_workspace_by_index(index, false);
    }

    /**
     * Moves and resizes a window, as dragging it and its edges does.
     *
     * @param {number} id - the window's ID
     * @param {number} x - where its frame's left edge goes, in stage coordinates
     * @param {number} y - where its frame's top edge goes
     * @param {number} width - the width of its frame
     * @param {number} height - the height of its frame
     */
    moveResizeWindow(id, x, y, width, height) {
        windowById(id).move_resize_frame(true, x, y, width, height);
    }

    /**
     * Activates a window, switching to its workspace, as clicking it in the overview does.
     *
     * @param {number} id - the window's ID
     */
    activateWindow(id) {
        Main.activateWindow(windowById(id));
    }

    /**
     * Marks a window as demanding attention, the mark mutter sets on a window whose activation it refuses.
     *
     * @param {number} id - the window's ID
     */
    setDemandsAttention(id) {
        windowById(id).set_demands_attention();
    }

    /**
     * Starts an application or activates it, as clicking it in the overview or in the dash does.
     *
     * @param {string} appId - the ID of the application's desktop file
     */
    activateApp(appId) {
        appById(appId).activate();
    }

    /**
     * Opens a new window of an application, as the New Window item of its menu in the dash does.
     *
     * @param {string} appId - the ID of the application's desktop file
     */
    openNewWindow(appId) {
        appById(appId).open_new_window(-1);
    }

    /**
     * Clicks an application's icon in the Ubuntu Dock, which then does what its settings say, as a click of the mouse
     * does. The dock shows an icon for every favorite and running application.
     *
     * @param {string} appId - the ID of the application's desktop file
     * @param {number} button - the mouse button, 1 for the left one and 2 for the middle one
     */
    async clickDockIcon(appId, button = 1) {
        const dock = Main.extensionManager.lookup('ubuntu-dock@ubuntu.com');
        if (!dock)
            throw new Error('No Ubuntu Dock');
        // The same module the dock imported, which holds the running dock.
        const {DockManager} = await import(`file://${dock.path}/docking.js`);
        const icon = DockManager.getDefault().mainDock.dash.getAppIcons().find(i => i.app?.get_id() === appId);
        if (!icon)
            throw new Error(`No dock icon for ${appId}`);
        icon.activate(button);
    }

    /**
     * @returns {Promise<string>} the text in the clipboard, as the next paste would insert it
     */
    clipboardText() {
        return new Promise(resolve => {
            St.Clipboard.get_default().get_text(St.ClipboardType.CLIPBOARD, (_clipboard, text) => resolve(text));
        });
    }

    /**
     * Runs a command, as the Alt+F2 dialog does.
     *
     * @param {string} commandLine - the command, which is parsed as a shell would
     */
    spawnCommandLine(commandLine) {
        Util.trySpawnCommandLine(commandLine);
    }

    /**
     * Presses and releases a key with modifiers on a virtual keyboard, which goes to the focused window as a key of a
     * real keyboard does.
     *
     * @param {string} key - the name of the key's keysym, such as Return or F2
     * @param {string[]} modifiers - the names of the modifiers' keysyms held meanwhile, such as Control_L
     */
    async pressKey(key, modifiers = []) {
        if (!this._keyboard) {
            const seat = Clutter.get_default_backend().get_default_seat();
            this._keyboard = seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
            // Keys sent before the new device is set up are lost.
            await new Promise(resolve => GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
                resolve();
                return GLib.SOURCE_REMOVE;
            }));
        }
        const keyvals = [...modifiers, key].map(name => {
            const keyval = Clutter[`KEY_${name}`];
            if (keyval === undefined)
                throw new Error(`No key ${name}`);
            return keyval;
        });
        const time = () => GLib.get_monotonic_time();
        for (const keyval of keyvals)
            this._keyboard.notify_keyval(time(), keyval, Clutter.KeyState.PRESSED);
        for (const keyval of keyvals.reverse())
            this._keyboard.notify_keyval(time(), keyval, Clutter.KeyState.RELEASED);
    }

    /**
     * @returns {object[]} the open modal dialogs, such as the one asking to confirm logging out, with their title and,
     *     for that one, the applications it lists as busy or with unsaved work
     */
    dialogs() {
        return Main.layoutManager.modalDialogGroup.get_children().filter(d => d.visible && d.state !== undefined).map(d => ({
            type: d.constructor.name,
            state: d.state,
            title: d._messageDialogContent?.title ?? null,
            applications: d._applicationSection?.list.get_children().map(item => item.title) ?? [],
        }));
    }

    /**
     * Confirms the open dialog which asks to confirm logging out, restarting or powering off, as its button does.
     *
     * @param {string} signal - the D-Bus signal the button emits, ConfirmedLogout, ConfirmedReboot or ConfirmedShutdown
     * @returns {boolean} whether such a dialog was open
     */
    confirmEndSession(signal = 'ConfirmedLogout') {
        const dialog = Main.layoutManager.modalDialogGroup.get_children().find(d => d.visible && d._confirm && d._type !== undefined);
        if (!dialog)
            return false;
        dialog._confirm(signal).catch(logError);
        return true;
    }

    /**
     * @param {string} uuid - the extension's UUID
     * @returns {?{state: string, error: string}} the extension's state, such as ACTIVE, and its error, or null when
     *     GNOME Shell does not know the extension
     */
    extensionState(uuid) {
        const extension = Main.extensionManager.lookup(uuid);
        if (!extension)
            return null;
        const name = Object.keys(ExtensionState).find(key => ExtensionState[key] === extension.state) ?? `${extension.state}`;
        return {state: name, error: `${extension.error ?? ''}`};
    }

    /**
     * @returns {object[]} the chrome GNOME Shell tracks, the dock and the top bar for example, each with its position,
     *     its size and whether it reserves space for itself
     */
    trackedChrome() {
        return Main.layoutManager._trackedActors.map(({actor, affectsStruts}) => {
            const [x, y] = actor.get_transformed_position();
            const [width, height] = actor.get_transformed_size();
            return {name: actor.name ?? null, type: actor.constructor.name, affectsStruts: !!affectsStruts,
                x: Math.round(x), y: Math.round(y), width: Math.round(width), height: Math.round(height),
                mapped: actor.mapped};
        });
    }

    /**
     * Maximizes a window, as double-clicking its title bar does.
     *
     * @param {number} id - the window's ID
     */
    maximizeWindow(id) {
        windowById(id).maximize(Meta.MaximizeFlags.BOTH);
    }

    /**
     * @returns {{button: ?boolean, open: boolean}} whether the screenshot button of quick settings is visible, null when
     *     there is none, and whether the screenshot UI is open
     */
    screenshotState() {
        const buttons = [];
        const walk = actor => {
            if (actor.constructor.name === 'ScreenshotItem')
                buttons.push(actor);
            actor.get_children().forEach(walk);
        };
        walk(Main.panel.statusArea.quickSettings.menu.actor);
        return {button: buttons.length ? buttons[0].visible : null, open: Main.screenshotUI.visible};
    }

    /**
     * Returns GNOME Shell's lock screen. GNOME Shell creates it only when GDM and logind run, which the test session
     * has neither of, so then one of its own is created here, which locks the screen the same way.
     *
     * @returns {Promise<object>} the lock screen
     */
    async _screenShield() {
        if (Main.screenShield)
            return Main.screenShield;
        if (!this._ownScreenShield) {
            const {ScreenShield} = await import('resource:///org/gnome/shell/ui/screenShield.js');
            this._ownScreenShield = new ScreenShield();
        }
        return this._ownScreenShield;
    }

    /**
     * Unlocks the screen, as authenticating on the lock screen does.
     */
    async unlock() {
        (await this._screenShield()).deactivate(false);
    }

    /**
     * Locks the screen, as Super+L does.
     */
    async lock() {
        (await this._screenShield()).lock(false);
    }

    /**
     * @returns {boolean} whether the screen is locked
     */
    locked() {
        return Main.sessionMode.isLocked;
    }
}

export default class SessionTestExtension extends Extension {
    enable() {
        // Eval works only in unsafe mode, which is otherwise turned on only from Looking Glass.
        global.context.unsafe_mode = true;
        globalThis.sessionTest = new SessionTest();
    }

    disable() {
        delete globalThis.sessionTest;
        global.context.unsafe_mode = false;
    }
}
