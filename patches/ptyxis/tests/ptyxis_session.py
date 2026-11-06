"""Run Ptyxis three times to test that its session survives a crash, and that closing it keeps no screens. See README.

Usage: ptyxis_session.py PTYXIS MUTTER_ROOT OUT_DIR

PTYXIS is the ptyxis binary to run, and MUTTER_ROOT the directory mutter was extracted into, which is started headless
on the Wayland display named by WAYLAND_DISPLAY. OUT_DIR is the home directory and XDG base directories
run-ptyxis-session.sh set up, and receives Ptyxis's output, with its Wayland requests and events, as runN.log, and
mutter's as mutterN.log. The first run ends with SIGKILL of mutter and Ptyxis, as when the machine crashes, and mutter
is started again. The second run ends by closing its three windows with Alt+F4. The shells Ptyxis starts read
OUT_DIR/home/.bashrc, written here, which in the first run moves each shell into a directory of its own and prints a
line naming it, and in later runs prints the directory it started in. The exit status is 0 when every check passed.
"""

import os
import re
import signal
import stat
import subprocess
import sys
import time

from gi.repository import Gio, GLib

APP_ID = "org.gnome.Ptyxis"

# Each of these is matched in the WAYLAND_DEBUG output of Ptyxis, where "->" marks a request it sends.
PATTERNS = {
    "get_session": re.compile(r'-> xx_session_manager_v1#\d+\.get_session\(new id xx_session_v1#\d+, (\d+), ("[^"]*"|nil)\)'),
    "created": re.compile(r'xx_session_v1#\d+\.created\("([^"]*)"\)'),
    "restored_session": re.compile(r'xx_session_v1#\d+\.restored\(\)'),
    "restore_toplevel": re.compile(r'-> xx_session_v1#\d+\.restore_toplevel\(new id xx_toplevel_session_v1#\d+, xdg_toplevel#\d+, "([^"]*)"\)'),
    "restored_toplevel": re.compile(r'xx_toplevel_session_v1#\d+\.restored\(xdg_toplevel#\d+\)'),
    "error": re.compile(r'wl_display#\d+\.error\(|[Pp]rotocol error'),
}

# The activation token the later runs are launched with, as GNOME Shell passes one in the platform data. The
# compositor does not know it and ignores it, but the WAYLAND_DEBUG output shows the window Ptyxis presents it with.
TOKEN = "ptyxis-session-test-token"

# The first run prints "first run, tab N" in each new shell, later runs "second run in" and the directory. The first
# tab shows nothing for its first 5 seconds, so that Ptyxis saves its session once while no tab has a screen.
BASHRC = r"""# Written by ptyxis_session.py for the shells of the Ptyxis under test.
PS1='prompt$ '
PROMPT_COMMAND=
if [ "$(cat "$HOME/run")" = 1 ]; then
  n=$(( $(cat "$HOME/tabs" 2>/dev/null || echo 0) + 1 ))
  echo "$n" > "$HOME/tabs"
  mkdir -p "$HOME/tab$n" && cd "$HOME/tab$n"
  [ "$n" = 1 ] && sleep 5
  echo "first run, tab $n"
else
  echo "second run in $PWD"
fi
# The current directory, which Ptyxis saves with the tab, as vte.sh reports it in a login shell.
printf '\033]7;file://%s%s\033\\' "$HOSTNAME" "$PWD"
"""

failures = []


def check(condition, message):
    print(("ok:   " if condition else "FAIL: ") + message)
    if not condition:
        failures.append(message)


def scan(log_path):
    with open(log_path, errors="replace") as log:
        text = log.read()
    return {name: pattern.findall(text) for name, pattern in PATTERNS.items()}


def wait_until(predicate, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except (OSError, GLib.Error):
            pass
        time.sleep(0.2)
    return False


def load_variant(path, type_string):
    with open(path, "rb") as f:
        data = f.read()
    return GLib.Variant.new_from_bytes(GLib.VariantType(type_string), GLib.Bytes.new(data), False).unpack()


def session_path():
    return os.path.join(GLib.get_user_config_dir(), APP_ID, "session.gvariant")


def screens_path():
    return os.path.join(GLib.get_user_state_dir(), APP_ID, "screens.gvariant")


def gtk_state_path():
    return os.path.join(GLib.get_user_state_dir(), "%s@fallback.state" % APP_ID)


def session_windows():
    """The windows of Ptyxis's session, each as (window ID, [(directory, screen text) for each tab])."""
    windows = load_variant(session_path(), "a{sv}").get("windows", [])
    screens = load_variant(screens_path(), "a{ss}") if os.path.exists(screens_path()) else {}
    result = []
    for window in windows:
        tabs = [(GLib.filename_from_uri(tab["cwd"])[0] if "cwd" in tab else None, screens.get(tab.get("screen-id"), ""))
                for tab in window.get("tabs", [])]
        if tabs:
            result.append((window.get("window-id"), tabs))
    return result


def gtk_windows():
    """The windows of GTK's state file, as (window ID of Ptyxis, Wayland toplevel name)."""
    _, _, windows = load_variant(gtk_state_path(), "(a{sv}a{sv}a(a{sv}a{sv}))")
    return [(app.get("window-id"), gtk.get("session-id")) for gtk, app in windows]


def all_screens():
    return "\n".join(screen for _, tabs in session_windows() for _, screen in tabs)


def window_dirs(windows):
    """The directories of the tabs of each window, in the order of the tabs, for windows in any order."""
    return sorted([d for d, _ in tabs] for _, tabs in windows)


def screens_by_dir(windows):
    return {d: screen for _, tabs in windows for d, screen in tabs}


def toplevel_kinds(log_path, surface_events):
    """Returns for each line matched by the pattern surface_events, whose last group is a wl_surface ID, whether the
    window of that surface is one the compositor restored ("restored"), another one ("new"), or none (None)."""
    xdg_surfaces, toplevels, restored, found = {}, {}, set(), []
    with open(log_path, errors="replace") as log:
        for line in log:
            if match := re.search(r"-> xdg_wm_base#\d+\.get_xdg_surface\(new id xdg_surface#(\d+), wl_surface#(\d+)\)", line):
                xdg_surfaces[match[1]] = match[2]
            elif match := re.search(r"-> xdg_surface#(\d+)\.get_toplevel\(new id xdg_toplevel#(\d+)\)", line):
                toplevels[xdg_surfaces.get(match[1])] = match[2]
            elif match := re.search(r"xx_toplevel_session_v1#\d+\.restored\(xdg_toplevel#(\d+)\)", line):
                restored.add(match[1])
            elif match := surface_events.search(line):
                found.append(toplevels.get(match.groups()[-1]))
    return [None if t is None else "restored" if t in restored else "new" for t in found]


def activated(log_path, token=None):
    """The kind of window, see toplevel_kinds, of each activation request with token, or with any token."""
    token_pattern = re.escape(token) if token else r'[^"]*'
    return toplevel_kinds(log_path, re.compile(r'-> xdg_activation_v1#\d+\.activate\("%s", wl_surface#(\d+)\)' % token_pattern))


def focused(log_path):
    """The kind of window, see toplevel_kinds, which received the keyboard focus last, or None."""
    kinds = toplevel_kinds(log_path, re.compile(r"wl_keyboard#\d+\.enter\(\d+, wl_surface#(\d+),"))
    return kinds[-1] if kinds else None


def split_windows(windows, ids):
    """Splits session windows into those with one of ids and the others."""
    return [w for w in windows if w[0] in ids], [w for w in windows if w[0] not in ids]


def activate(action):
    subprocess.run(["gdbus", "call", "--session", "--dest", APP_ID, "--object-path", "/org/gnome/Ptyxis",
                    "--method", "org.gtk.Actions.Activate", action, "[]", "{}"],
                   check=True, stdout=subprocess.DEVNULL)


def start(ptyxis, log_path, launch="command-line"):
    """Starts Ptyxis from its command line, or with launch "new-window" or "activate" as a D-Bus service which is then
    asked to activate its new-window action, as GNOME Shell does when Ptyxis is started from the dock or the
    overview, or to activate itself, as D-Bus activation without an action does."""
    log = open(log_path, "w")
    # ptyxis-agent starts each shell through systemd-run --user when systemd-run is found, which needs the user's
    # systemd manager in XDG_RUNTIME_DIR, and the test has a runtime directory of its own. A systemd-run first on
    # PATH which fails makes the agent start the shells directly.
    stub_dir = os.path.join(os.environ["HOME"], "..", "bin")
    os.makedirs(stub_dir, exist_ok=True)
    stub = os.path.join(stub_dir, "systemd-run")
    with open(stub, "w") as f:
        f.write("#!/bin/sh\nexit 1\n")
    os.chmod(stub, 0o755)
    env = dict(os.environ, WAYLAND_DEBUG="1", PATH=os.path.realpath(stub_dir) + ":" + os.environ["PATH"])
    # A process group of its own, so that the crash ends the agent and the shells with it.
    if launch == "command-line":
        return subprocess.Popen([ptyxis], stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    proc = subprocess.Popen([ptyxis, "--gapplication-service"], stdout=log, stderr=subprocess.STDOUT, env=env,
                            start_new_session=True)
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    wait_until(lambda: bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                     "NameHasOwner", GLib.Variant("(s)", (APP_ID,)), GLib.VariantType("(b)"),
                                     0, -1, None).unpack()[0])
    platform_data = {"activation-token": GLib.Variant("s", TOKEN)}
    if launch == "new-window":
        method, args = "ActivateAction", GLib.Variant("(sava{sv})", ("new-window", [], platform_data))
    else:
        method, args = "Activate", GLib.Variant("(a{sv})", (platform_data,))
    bus.call_sync(APP_ID, "/org/gnome/Ptyxis", "org.freedesktop.Application", method, args, None, 0, -1, None)
    return proc


def end_group(proc):
    """Ends what is left of the process group of Ptyxis, such as its agent and the shells."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def crash(proc):
    end_group(proc)
    proc.wait()


class Mutter:
    """A headless mutter with one virtual monitor, and a keyboard to press keys with."""

    def __init__(self, root, log_path):
        libdir = os.path.join(root, "usr/lib/x86_64-linux-gnu")
        self.display = os.environ["WAYLAND_DISPLAY"]
        env = dict(os.environ, LD_LIBRARY_PATH="%s:%s/mutter-18" % (libdir, libdir))
        del env["WAYLAND_DISPLAY"]
        self.log = open(log_path, "w")
        self.proc = subprocess.Popen([os.path.join(root, "usr/bin/mutter"), "--headless", "--no-x11",
                                      "--virtual-monitor", "1920x1080", "--wayland-display", self.display,
                                      "--mutter-plugin=%s/mutter-18/plugins/libdefault.so" % libdir],
                                     stdout=self.log, stderr=subprocess.STDOUT, env=env)
        socket = os.path.join(os.environ["XDG_RUNTIME_DIR"], self.display)
        if not wait_until(lambda: os.path.exists(socket) and self.has_remote_desktop(), timeout=20):
            sys.exit("mutter did not start, see %s" % log_path)
        self.keyboard = Keyboard()

    def has_remote_desktop(self):
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        owner = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                              GLib.Variant("(s)", (Keyboard.BUS_NAME,)), GLib.VariantType("(b)"), 0, -1, None)
        return owner.unpack()[0]

    def running(self):
        return self.proc.poll() is None

    def crash(self):
        self.proc.kill()
        self.proc.wait()


class Keyboard:
    """A virtual keyboard of mutter's remote desktop interface, which lasts as long as this process or mutter.

    Without a keyboard, mutter gives no window keyboard focus, and GTK saves its window state only while one of the
    application's windows has it. It also presses Alt+F4, mutter's default key binding for closing a window.
    """

    BUS_NAME = "org.gnome.Mutter.RemoteDesktop"

    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.path = self.bus.call_sync(self.BUS_NAME, "/org/gnome/Mutter/RemoteDesktop", self.BUS_NAME, "CreateSession",
                                       None, GLib.VariantType("(o)"), 0, -1, None).unpack()[0]
        self.session_call("Start", None)
        # Mutter adds the keyboard to its seat with the first key.
        shift = 0xffe1
        self.key(shift, True)
        self.key(shift, False)

    def session_call(self, method, args):
        self.bus.call_sync(self.BUS_NAME, self.path, self.BUS_NAME + ".Session", method, args, None, 0, -1, None)

    def key(self, keysym, pressed):
        self.session_call("NotifyKeyboardKeysym", GLib.Variant("(ub)", (keysym, pressed)))

    def close_window(self):
        alt, f4 = 0xffe9, 0xffc1
        self.key(alt, True)
        self.key(f4, True)
        self.key(f4, False)
        self.key(alt, False)


def summary(run, found):
    print("run %d: get_session %s, created %s, restored session %d" % (
        run, found["get_session"], found["created"], len(found["restored_session"])))
    print("run %d: restore_toplevel %s, restored %d" % (run, found["restore_toplevel"], len(found["restored_toplevel"])))


def main():
    ptyxis, mutter_root, out = sys.argv[1:4]
    home = os.environ["HOME"]
    with open(os.path.join(home, ".bashrc"), "w") as f:
        f.write(BASHRC)
    with open(os.path.join(home, ".bash_profile"), "w") as f:
        f.write(". \"$HOME/.bashrc\"\n")
    with open(os.path.join(home, "run"), "w") as f:
        f.write("1\n")
    tab_dirs = [os.path.join(home, "tab%d" % n) for n in (1, 2, 3)]
    mutter = Mutter(mutter_root, os.path.join(out, "mutter1.log"))

    # Run 1: a window with two tabs and a second window with one, each tab in a directory of its own. Ptyxis saves
    # the session a few seconds after each change, GTK its window state every 15 seconds while a window has the
    # focus, and mutter the places of the windows a few seconds after they change, so the test waits for GTK before
    # ending mutter and Ptyxis as a crash of the machine would. The windows can then be placed again only from what
    # mutter wrote while they were open.
    log1 = os.path.join(out, "run1.log")
    proc = start(ptyxis, log1)
    check(wait_until(lambda: "first run, tab 1" in all_screens()), "run 1: the session saved the screen of the first tab")
    activate("new-tab")
    check(wait_until(lambda: "first run, tab 2" in all_screens()), "run 1: the session saved the screen of a second tab")
    activate("new-window")
    check(wait_until(lambda: "first run, tab 3" in all_screens()), "run 1: the session saved the screen of a second window")
    check(wait_until(lambda: len(gtk_windows()) == 2, timeout=40), "run 1: GTK saved the state of both windows")
    session1 = session_windows()
    gtk1 = gtk_windows()
    mutter.crash()
    crash(proc)
    mutter = Mutter(mutter_root, os.path.join(out, "mutter2.log"))

    run1 = scan(log1)
    summary(1, run1)
    print("Ptyxis session: %s" % session1)
    print("GTK state: %s" % gtk1)
    check(not run1["error"], "run 1: no protocol error")
    check(len(run1["created"]) == 1, "run 1: the compositor created the session")
    check(len(run1["restore_toplevel"]) == 2, "run 1: both windows were named to the compositor")
    check(window_dirs(session1) == [tab_dirs[:2], tab_dirs[2:]],
          "run 1: the session saved both windows with the directory of each tab")
    screens1 = screens_by_dir(session1)
    check(all("first run, tab %d" % n in screens1.get(d, "") for n, d in zip((1, 2, 3), tab_dirs)),
          "run 1: the session saved the screen of each tab with its tab")
    check(sorted(i for i, _ in gtk1) == sorted(i for i, _ in session1) and None not in [i for i, _ in gtk1],
          "run 1: GTK and Ptyxis saved the same two window IDs")
    check(sorted(n for _, n in gtk1) == sorted(run1["restore_toplevel"]), "run 1: GTK saved the names of both windows")
    mode = stat.S_IMODE(os.stat(screens_path()).st_mode)
    check(mode == 0o600, "the screens file is readable only by its owner (mode %o)" % mode)

    # Run 2: with a new mutter, and Ptyxis started with its new-window action and an activation token, as GNOME Shell
    # may start it, restores both windows under their names of run 1, each tab with the screen it had, dimmed, above
    # the output of a new shell started in the tab's directory, and opens a new window beside them. A tab which is not
    # the selected one of its window starts its shell only once it is shown, so it shows just the screen of run 1. The
    # new window receives the token and the focus, and no restored window is activated.
    with open(os.path.join(home, "run"), "w") as f:
        f.write("2\n")
    log2 = os.path.join(out, "run2.log")
    proc = start(ptyxis, log2, launch="new-window")
    ids1 = [i for i, _ in session1]
    check(wait_until(lambda: len(scan(log2)["restored_toplevel"]) >= 2), "run 2: the compositor restored both windows")
    check(wait_until(lambda: len(session_windows()) == 3), "run 2: a new window opened beside the restored ones")
    check(wait_until(lambda: all_screens().count("second run in") == 3),
          "run 2: the session saved the screens of the new shells")
    check(wait_until(lambda: focused(log2) == "new"), "run 2: the new window has the keyboard focus")
    session2 = session_windows()
    restored2, new2 = split_windows(session2, ids1)
    check(activated(log2, TOKEN) == ["new"], "run 2: the activation token was presented with the new window only (%s)"
          % activated(log2, TOKEN))
    check("restored" not in activated(log2), "run 2: no restored window was activated (%s)" % activated(log2))

    # Then the three windows are closed, the new one, which has the focus, first. The session saves the window closed
    # last with its tabs, as Ptyxis does without the patches, but removes the screens, so that only a session which
    # ended with its windows open shows them.
    mutter.keyboard.close_window()
    check(wait_until(lambda: len(session_windows()) == 2), "run 2: the session saved closing the new window")
    check(sorted(i for i, _ in session_windows()) == sorted(ids1), "run 2: the restored windows are left")
    mutter.keyboard.close_window()
    check(wait_until(lambda: len(session_windows()) == 1), "run 2: the session saved closing the first window")
    check(os.path.exists(screens_path()), "run 2: the screens are kept while a window is open")
    last_window = session_windows()
    mutter.keyboard.close_window()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        crash(proc)
    check(proc.returncode == 0, "run 2: Ptyxis exited with status 0 once its last window was closed (status %s)" % proc.returncode)
    end_group(proc)

    run2 = scan(log2)
    summary(2, run2)
    print("Ptyxis session: %s" % session2)
    check(not run2["error"], "run 2: no protocol error")
    check(len(run2["get_session"]) == 1 and run2["get_session"][0][1] == '"%s"' % run1["created"][0],
          "run 2: the session of run 1 was requested")
    check(len(run2["restored_session"]) == 1, "run 2: the compositor restored the session")
    check(set(run1["restore_toplevel"]) <= set(run2["restore_toplevel"]) and len(run2["restore_toplevel"]) == 3,
          "run 2: the restored windows used their names of run 1, and the new window a name of its own")
    check(sorted(i for i, _ in restored2) == sorted(ids1), "run 2: the windows kept their IDs")
    check(window_dirs(restored2) == [tab_dirs[:2], tab_dirs[2:]], "run 2: each tab kept its window and directory")
    check(len(new2) == 1 and len(new2[0][1]) == 1, "run 2: the new window has one tab")
    screens2 = screens_by_dir(restored2)
    for n, d in zip((1, 2, 3), tab_dirs):
        check("first run, tab %d" % n in screens2.get(d, ""), "run 2: tab %d shows its screen of run 1" % n)
    for n in (2, 3):
        screen = screens2.get(tab_dirs[n - 1], "")
        new = "second run in %s" % tab_dirs[n - 1]
        check(new in screen and screen.index("first run, tab %d" % n) < screen.index(new),
              "run 2: the shell of tab %d started in its directory, below the screen of run 1" % n)
    check("second run" not in screens2.get(tab_dirs[0], ""), "run 2: the tab not selected has not started its shell")
    check(not os.path.exists(screens_path()), "run 2: closing the last window removed the screens")
    session_closed = session_windows()
    print("Ptyxis session after closing: %s" % session_closed)
    check(window_dirs(session_closed) == window_dirs(last_window), "run 2: the session kept the window closed last")

    # Run 3: with Ptyxis activated without an action and with an activation token, restores the window closed last,
    # with new shells in the directories of its tabs and without screens, and opens a new window beside it, which
    # receives the token and the focus.
    log3 = os.path.join(out, "run3.log")
    proc = start(ptyxis, log3, launch="activate")
    check(wait_until(lambda: len(scan(log3)["restored_toplevel"]) >= 1), "run 3: the compositor restored the window")
    check(wait_until(lambda: len(session_windows()) == 2), "run 3: a new window opened beside the restored one")
    check(wait_until(lambda: os.path.exists(screens_path()) and len(session_windows()) == 2 and
                     all("second run in" in "".join(screen for _, screen in tabs) for _, tabs in session_windows())),
          "run 3: the session saved the screens of the new shells of both windows")
    check(wait_until(lambda: focused(log3) == "new"), "run 3: the new window has the keyboard focus")
    session3 = session_windows()
    restored3, new3 = split_windows(session3, [i for i, _ in session_closed])
    crash(proc)
    run3 = scan(log3)
    summary(3, run3)
    print("Ptyxis session: %s" % session3)
    check(not run3["error"], "run 3: no protocol error")
    check(window_dirs(restored3) == window_dirs(last_window), "run 3: the window closed last was restored with its tabs")
    check(len(new3) == 1, "run 3: the new window was saved beside it")
    check(activated(log3, TOKEN) == ["new"], "run 3: the activation token was presented with the new window only (%s)"
          % activated(log3, TOKEN))
    check("restored" not in activated(log3), "run 3: no restored window was activated (%s)" % activated(log3))
    check("first run" not in all_screens(), "run 3: no tab shows a screen of an earlier run")

    check(mutter.running(), "mutter kept running")
    mutter.crash()

    print("RESULT: %s" % ("OK" if not failures else "%d check(s) failed" % len(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
