"""Test that Text Editor autosaves its session a few seconds after every change. See README.

Usage: text_editor_autosave.py TEXT_EDITOR OUT_DIR

TEXT_EDITOR is the gnome-text-editor binary to run, with the GTK it is to use already on LD_LIBRARY_PATH, and with
GSettings set up by run-text-editor-autosave.sh to read a keyfile from the XDG config home. Each scenario starts Text
Editor with XDG homes of its own under OUT_DIR/SCENARIO, waits for it to save its session, opens two files one after
the other in the running instance, waits each time for the session saved with that file, and quits it. The scenario
slow-draft-save reads a large text from standard input instead, see run_slow_draft_scenario. Text Editor's output goes
to OUT_DIR/SCENARIO/text-editor.log. The exit status is 0 when every check passed.
"""

import os
import subprocess
import sys
import time

from gi.repository import Gio, GLib

APP_ID = "org.gnome.TextEditor"

# The time Text Editor has to save after the auto-save delay has passed.
SLACK = 5

# GLib fires timeouts of whole seconds up to a quarter of a second early, to group them with other timeouts.
EARLY = 0.5

# Each scenario has the auto-save-delay written to the keyfile before Text Editor starts, None for none, and the
# delay set while the save after opening the first file is pending, None to leave it.
SCENARIOS = [
    {"name": "default-delay", "start_delay": None, "pending_delay": None},
    # Text Editor binds auto-save-delay while it starts, before it enables auto-save, so a delay other than the
    # default changes the session before auto-save is enabled.
    {"name": "delay-set-before-start", "start_delay": 5, "pending_delay": None},
    # Changing the delay removes the pending auto-save, and has to schedule a new one with the new delay. The keyfile
    # exists from the start, so that Text Editor's settings backend notices the change.
    {"name": "delay-changed-while-pending", "start_delay": 3, "pending_delay": 4},
]

DEFAULT_DELAY = 3

# The size of the text slow-draft-save reads from standard input. Text Editor writes a draft at a few megabytes a
# second, so writing this one lasts several seconds.
SLOW_DRAFT_MEGABYTES = 24

failures = []


def check(condition, message):
    print(("ok:   " if condition else "FAIL: ") + message)
    if not condition:
        failures.append(message)


def scenario_env(base):
    env = dict(os.environ)
    for name in ("data", "config", "state", "cache"):
        path = os.path.join(base, "xdg", name)
        os.makedirs(path, exist_ok=True)
        env["XDG_%s_HOME" % name.upper()] = path
    return env


def write_settings(env, delay):
    settings_dir = os.path.join(env["XDG_CONFIG_HOME"], "glib-2.0", "settings")
    os.makedirs(settings_dir, exist_ok=True)
    with open(os.path.join(settings_dir, "keyfile"), "w") as f:
        f.write("[org/gnome/TextEditor]\nauto-save-delay=uint32 %d\n" % delay)


def state_path(env):
    return os.path.join(env["XDG_DATA_HOME"], APP_ID, "session.gvariant")


def saved_uris(path):
    """The URIs of the pages in Text Editor's session state at path."""
    with open(path, "rb") as f:
        data = f.read()
    state = GLib.Variant.new_from_bytes(GLib.VariantType("a{sv}"), GLib.Bytes.new(data), False).unpack()
    return [p.get("uri") for w in state.get("windows", []) for p in w.get("pages", [])]


def wait_for_save(env, after_ns, timeout, uri=None):
    """Waits until the session state is written later than after_ns, with uri among its pages when given.

    Returns the seconds from after_ns to that write, or None when there was none within timeout seconds.
    """
    path = state_path(env)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            mtime_ns = os.stat(path).st_mtime_ns
            if mtime_ns > after_ns and (uri is None or uri in saved_uris(path)):
                return (mtime_ns - after_ns) / 1e9
        except (FileNotFoundError, GLib.Error):
            pass
        time.sleep(0.2)
    return None


def describe(after, timeout):
    return "after %.1f s" % after if after is not None else "not within %d s" % timeout


def wait_for_name(timeout=30):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    deadline = time.time() + timeout
    while time.time() < deadline:
        reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                              GLib.Variant("(s)", (APP_ID,)), GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, -1, None)
        if reply.unpack()[0]:
            return True
        time.sleep(0.2)
    return False


def quit_text_editor(proc):
    # app.quit saves the session of Text Editor and quits.
    subprocess.run(["gdbus", "call", "--session", "--dest", APP_ID, "--object-path", "/org/gnome/TextEditor",
                    "--method", "org.gtk.Actions.Activate", "quit", "[]", "{}"],
                   check=True, stdout=subprocess.DEVNULL)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        check(False, "Text Editor quit within 30 seconds")


def make_file(base, name):
    path = os.path.join(base, name)
    with open(path, "w") as f:
        f.write("The file %s.\n" % name)
    return path, GLib.filename_to_uri(path, None)


def run_scenario(text_editor, out, scenario):
    name = scenario["name"]
    base = os.path.join(out, name)
    env = scenario_env(base)
    delay = DEFAULT_DELAY
    if scenario["start_delay"] is not None:
        write_settings(env, scenario["start_delay"])
        delay = scenario["start_delay"]
    first, first_uri = make_file(base, "first.txt")
    second, second_uri = make_file(base, "second.txt")

    with open(os.path.join(base, "text-editor.log"), "w") as log:
        started_ns = time.time_ns()
        proc = subprocess.Popen([text_editor], stdout=log, stderr=subprocess.STDOUT, env=env)
        if not wait_for_name():
            check(False, "%s: Text Editor registered on the session bus" % name)
            proc.kill()
            proc.wait()
            return

        after = wait_for_save(env, started_ns, delay + SLACK)
        check(after is not None, "%s: the session was autosaved after the start (%s)" % (name, describe(after, delay + SLACK)))

        # A file opened in the running instance is a change of the session like any edit.
        opened_ns = time.time_ns()
        subprocess.run([text_editor, first], stdout=log, stderr=subprocess.STDOUT, env=env, check=True)
        if scenario["pending_delay"] is None:
            after = wait_for_save(env, opened_ns, delay + SLACK, first_uri)
            check(after is not None, "%s: the session was autosaved with the first file (%s)" % (
                name, describe(after, delay + SLACK)))
        else:
            # One second after the change, the save it scheduled is still pending.
            time.sleep(1)
            delay = scenario["pending_delay"]
            changed_ns = time.time_ns()
            subprocess.run(["gsettings", "set", "org.gnome.TextEditor", "auto-save-delay", str(delay)], env=env, check=True)
            after = wait_for_save(env, changed_ns, delay + SLACK, first_uri)
            check(after is not None, "%s: the session was autosaved with the first file after the delay changed (%s)" % (
                name, describe(after, delay + SLACK)))
            # A save sooner than the new delay is the one scheduled before, when Text Editor missed the change.
            if after is not None:
                check(after >= delay - EARLY, "%s: that save followed the new delay of %d s" % (name, delay))

        opened_ns = time.time_ns()
        subprocess.run([text_editor, second], stdout=log, stderr=subprocess.STDOUT, env=env, check=True)
        after = wait_for_save(env, opened_ns, delay + SLACK, second_uri)
        check(after is not None, "%s: the session was autosaved with the second file (%s)" % (
            name, describe(after, delay + SLACK)))

        quit_text_editor(proc)
    check(proc.returncode == 0, "%s: Text Editor exited with status %s" % (name, proc.returncode))
    saved = saved_uris(state_path(env)) if os.path.exists(state_path(env)) else []
    check(first_uri in saved and second_uri in saved, "%s: quitting saved the session with both files" % name)


def draft_sizes(env):
    """The sizes of the draft files, by their draft IDs."""
    drafts = os.path.join(env["XDG_DATA_HOME"], APP_ID, "drafts")
    sizes = {}
    for name in os.listdir(drafts) if os.path.isdir(drafts) else []:
        try:
            sizes[name] = os.stat(os.path.join(drafts, name)).st_size
        except FileNotFoundError:
            pass
    return sizes


def saved_draft_ids(path):
    """The draft IDs in the list of drafts of Text Editor's session state at path."""
    with open(path, "rb") as f:
        data = f.read()
    state = GLib.Variant.new_from_bytes(GLib.VariantType("a{sv}"), GLib.Bytes.new(data), False).unpack()
    return [d.get("draft-id") for d in state.get("drafts", [])]


def wait_for(condition, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def run_slow_draft_scenario(text_editor, out):
    """Checks that the state saved last is the newest one also when an earlier save writes a large draft.

    A save captures the session state when it is requested, and writes it only after it wrote its drafts. Writing a
    draft for the first time adds it to the session's list of drafts, after that save captured the state, and
    requests another save, whose state has the draft in the list. While the first save still writes the large draft,
    the second one has no draft to write, so it would write its state first, and the first save would then write its
    older state, without the draft in the list, over it. The list of drafts is checked rather than the pages, since a
    page which is still loading is left out of the state, and a file loads for seconds while a large document is shown.
    """
    name = "slow-draft-save"
    base = os.path.join(out, name)
    env = scenario_env(base)
    delay = 1
    write_settings(env, delay)
    size = SLOW_DRAFT_MEGABYTES * 1000 * 1000
    text = os.path.join(base, "standard-input.txt")
    with open(text, "w") as f:
        f.write(("x" * 99 + "\n") * (size // 100))

    def writing_draft():
        return [draft_id for draft_id, s in draft_sizes(env).items() if 0 < s < size]

    with open(os.path.join(base, "text-editor.log"), "w") as log:
        started_ns = time.time_ns()
        proc = subprocess.Popen([text_editor], stdout=log, stderr=subprocess.STDOUT, env=env)
        if not wait_for_name():
            check(False, "%s: Text Editor registered on the session bus" % name)
            proc.kill()
            proc.wait()
            return
        check(wait_for_save(env, started_ns, delay + SLACK) is not None, "%s: the session was autosaved after the start" % name)

        with open(text) as stdin:
            subprocess.run([text_editor, "-"], stdin=stdin, stdout=log, stderr=subprocess.STDOUT, env=env, check=True)
        writing = wait_for(writing_draft, 120)
        check(writing, "%s: Text Editor started writing the draft of standard input" % name)
        if writing:
            draft_id = writing_draft()[0]
            # When writing the draft lasts no longer than the delay, the scenario does not test anything.
            time.sleep(delay + 1)
            check(draft_id in writing_draft(), "%s: writing the draft lasted longer than the auto-save delay" % name)
            check(wait_for(lambda: draft_sizes(env).get(draft_id) == size, 120), "%s: the whole draft was written" % name)
            time.sleep(delay + SLACK)
            listed = saved_draft_ids(state_path(env)) if os.path.exists(state_path(env)) else []
            check(draft_id in listed, "%s: the session saved last lists the draft" % name)

        # Quitting would ask what to do with the unsaved draft, which nobody answers here.
        proc.terminate()
        proc.wait()


def main():
    text_editor, out = sys.argv[1:3]
    for scenario in SCENARIOS:
        run_scenario(text_editor, out, scenario)
    run_slow_draft_scenario(text_editor, out)
    print("RESULT: %s" % ("OK" if not failures else "%d check(s) failed" % len(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
