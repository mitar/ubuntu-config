"""Run Text Editor twice to test that GTK names its windows to the compositor and restores them. See README.

Usage: text_editor_session.py TEXT_EDITOR OUT_DIR SCENARIO

TEXT_EDITOR is the gnome-text-editor binary to run, with the GTK it is to use already on LD_LIBRARY_PATH. The
first run opens two files, each in a window of its own, and quits. The second run starts without files, which
restores the last session. The third run starts with --ignore-session, so that Text Editor has to discard the
windows GTK restores. Text Editor's output, with its Wayland requests and events, goes to OUT_DIR/runN.log.
SCENARIO says which session manager the private D-Bus session has, which run-text-editor-session.sh sets up:
none, unsupported (RestoreSupported is false) or supported (RestoreSupported is true), and so whether GTK has to
register with it. The exit status is 0 when every check passed.
"""

import os
import re
import subprocess
import sys
import time

from gi.repository import GLib

APP_ID = "org.gnome.TextEditor"

# Each of these is matched in the WAYLAND_DEBUG output of Text Editor, where "->" marks a request it sends.
PATTERNS = {
    "get_session": re.compile(r'-> xx_session_manager_v1#\d+\.get_session\(new id xx_session_v1#\d+, (\d+), ("[^"]*"|nil)\)'),
    "created": re.compile(r'xx_session_v1#\d+\.created\("([^"]*)"\)'),
    "restored_session": re.compile(r'xx_session_v1#\d+\.restored\(\)'),
    "add_toplevel": re.compile(r'-> xx_session_v1#\d+\.add_toplevel\(new id xx_toplevel_session_v1#\d+, xdg_toplevel#\d+, "([^"]*)"\)'),
    "restore_toplevel": re.compile(r'-> xx_session_v1#\d+\.restore_toplevel\(new id xx_toplevel_session_v1#\d+, xdg_toplevel#\d+, "([^"]*)"\)'),
    "restored_toplevel": re.compile(r'xx_toplevel_session_v1#\d+\.restored\(xdg_toplevel#\d+\)'),
    "remove": re.compile(r'-> xx_toplevel_session_v1#\d+\.remove\(\)'),
    "error": re.compile(r'wl_display#\d+\.error\(|[Pp]rotocol error'),
}

failures = []


def check(condition, message):
    print(("ok:   " if condition else "FAIL: ") + message)
    if not condition:
        failures.append(message)


def scan(log_path):
    with open(log_path, errors="replace") as log:
        text = log.read()
    return {name: pattern.findall(text) for name, pattern in PATTERNS.items()}


def wait_for(log_path, name, count, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(scan(log_path)[name]) >= count:
            return True
        time.sleep(0.2)
    return False


def quit_text_editor(proc):
    # app.quit saves the session of Text Editor and quits, and GTK saves its window state while shutting down.
    subprocess.run(["gdbus", "call", "--session", "--dest", APP_ID, "--object-path", "/org/gnome/TextEditor",
                    "--method", "org.gtk.Actions.Activate", "quit", "[]", "{}"],
                   check=True, stdout=subprocess.DEVNULL)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        check(False, "Text Editor quit within 30 seconds")


def start(text_editor, args, log_path):
    log = open(log_path, "w")
    env = dict(os.environ, WAYLAND_DEBUG="1")
    return subprocess.Popen([text_editor] + args, stdout=log, stderr=subprocess.STDOUT, env=env)


def load_variant(path, type_string):
    with open(path, "rb") as f:
        data = f.read()
    return GLib.Variant.new_from_bytes(GLib.VariantType(type_string), GLib.Bytes.new(data), False).unpack()


def editor_windows():
    """The windows of Text Editor's session state, as (window ID, URIs of the pages)."""
    state = load_variant(os.path.join(GLib.get_user_data_dir(), APP_ID, "session.gvariant"), "a{sv}")
    return [(w.get("window-id"), sorted(p.get("uri", "") for p in w.get("pages", []))) for w in state.get("windows", [])]


def gtk_windows(path):
    """The windows of GTK's state file, as (window ID of Text Editor, Wayland toplevel name)."""
    global_gtk, _, windows = load_variant(path, "(a{sv}a{sv}a(a{sv}a{sv}))")
    return global_gtk.get("wayland-session"), [(app.get("window-id"), gtk.get("session-id")) for gtk, app in windows]


def summary(run, found):
    print("run %d: get_session %s, created %s, restored session %d" % (
        run, found["get_session"], found["created"], len(found["restored_session"])))
    print("run %d: restore_toplevel %s, add_toplevel %s, restored %d, removed %d" % (
        run, found["restore_toplevel"], found["add_toplevel"], len(found["restored_toplevel"]), len(found["remove"])))


def main():
    text_editor, out, scenario = sys.argv[1:4]
    files = []
    for name in ("first.txt", "second.txt"):
        path = os.path.join(out, name)
        with open(path, "w") as f:
            f.write("The %s file.\n" % name.split(".")[0])
        files.append(path)
    uris = sorted(GLib.filename_to_uri(path, None) for path in files)

    # Run 1: the first file opens in the first window, the second one in a window of its own.
    log1 = os.path.join(out, "run1.log")
    proc1 = proc = start(text_editor, [files[0]], log1)
    check(wait_for(log1, "restore_toplevel", 1), "run 1: the first window was named to the compositor")
    with open(os.path.join(out, "run1-new-window.log"), "w") as log:
        subprocess.run([text_editor, "--new-window", files[1]], stdout=log, stderr=subprocess.STDOUT, check=True)
    check(wait_for(log1, "restore_toplevel", 2), "run 1: the second window was named to the compositor")
    time.sleep(2)
    quit_text_editor(proc)
    run1 = scan(log1)
    summary(1, run1)
    check(not run1["error"], "run 1: no protocol error")
    check(len(run1["get_session"]) == 1 and run1["get_session"][0][1] == "nil", "run 1: a new session was requested")
    check(len(run1["created"]) == 1, "run 1: the compositor created the session")
    check(not run1["restored_toplevel"], "run 1: no window was restored")

    state_name = "%s@%s.state" % (APP_ID, "test-instance" if scenario == "supported" else "fallback")
    state_path = os.path.join(GLib.get_user_state_dir(), state_name)
    check(os.path.exists(state_path), "GTK saved its state as %s" % state_name)
    if not os.path.exists(state_path):
        return 1
    session1, gtk1 = gtk_windows(state_path)
    editor1 = editor_windows()
    print("GTK state: session %s, windows %s" % (session1, gtk1))
    print("Text Editor session: windows %s" % editor1)
    check(session1 == run1["created"][0], "GTK saved the session the compositor created")
    check(sorted(name for _, name in gtk1) == sorted(run1["restore_toplevel"]), "GTK saved the names of both windows")
    check(sorted(i for i, _ in gtk1) == sorted(i for i, _ in editor1) and None not in [i for i, _ in gtk1],
          "GTK and Text Editor saved the same two window IDs")
    check(sorted(u for _, pages in editor1 for u in pages) == uris, "Text Editor saved both files")

    # Run 2: restores the session, and with it both windows under the names of run 1.
    log2 = os.path.join(out, "run2.log")
    proc2 = proc = start(text_editor, [], log2)
    check(wait_for(log2, "restored_toplevel", 2), "run 2: the compositor restored both windows")
    time.sleep(2)
    quit_text_editor(proc)
    run2 = scan(log2)
    summary(2, run2)
    check(not run2["error"], "run 2: no protocol error")
    check(len(run2["get_session"]) == 1 and run2["get_session"][0][1] == '"%s"' % session1,
          "run 2: the session of run 1 was requested")
    check(len(run2["restored_session"]) == 1, "run 2: the compositor restored the session")
    check(sorted(run2["restore_toplevel"]) == sorted(run1["restore_toplevel"]), "run 2: the windows used their names of run 1")

    session2, gtk2 = gtk_windows(state_path)
    editor2 = editor_windows()
    print("GTK state: session %s, windows %s" % (session2, gtk2))
    print("Text Editor session: windows %s" % editor2)
    check(sorted(gtk2) == sorted(gtk1), "run 2: GTK saved the same windows again")
    check(sorted(editor2) == sorted(editor1), "run 2: each window has the file it had in run 1")

    # Run 3: --ignore-session makes the session restore open one new window instead of restoring the two, so the
    # two windows created for GTK's restored window state are destroyed without ever being shown.
    log3 = os.path.join(out, "run3.log")
    proc = start(text_editor, ["--ignore-session"], log3)
    check(wait_for(log3, "restore_toplevel", 1), "run 3: a window was named to the compositor")
    time.sleep(2)
    check(proc.poll() is None, "run 3: Text Editor still runs after discarding the restored windows")
    quit_text_editor(proc)
    run3 = scan(log3)
    summary(3, run3)
    check(not run3["error"], "run 3: no protocol error")
    check(len(run3["restore_toplevel"]) == 1 and run3["restore_toplevel"][0] not in run1["restore_toplevel"],
          "run 3: only the new window was named, under a new name")
    check(not run3["restored_toplevel"], "run 3: no window was restored")
    _, gtk3 = gtk_windows(state_path)
    print("GTK state: windows %s" % gtk3)
    check(len(gtk3) == 1 and gtk3[0][0] not in [i for i, _ in gtk1] and gtk3[0][1] == run3["restore_toplevel"][0],
          "run 3: GTK saved only the new window")
    for run, p in ((1, proc1), (2, proc2), (3, proc)):
        check(p.returncode == 0, "run %d: Text Editor exited with 0, not %s" % (run, p.returncode))

    calls = []
    calls_path = os.path.join(out, "session-manager.log")
    if os.path.exists(calls_path):
        with open(calls_path) as f:
            calls = [line.split(" ", 1)[0] for line in f]
    print("session manager calls: %s" % (calls or "none"))
    if scenario == "supported":
        check(calls.count("RegisterRestore") == 3, "GTK registered with the session manager in every run")
    else:
        check("RegisterRestore" not in calls, "GTK did not register with the session manager")

    print("RESULT: %s" % ("OK" if not failures else "%d check(s) failed" % len(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
