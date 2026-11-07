#!/usr/bin/env python3
"""Runs one test in a GNOME session of the user running it, in the container of run-session-tests.sh. See README.

Usage: session_tests.py TEST OUT_DIR

OUT_DIR receives the files the test writes, and result.json with its checks. The exit status is 0 when every check
passed.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

# Python runs with -I, which leaves this directory out of the search path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from session import REPO_DIR, TEST_EXTENSION, Checks, Session, cgroup_of, processes, unit_of, wait_for  # noqa: E402

TESTS = {}


def test(function):
    TESTS[function.__name__.replace("_", "-")] = function
    return function


def write_desktop_file(directory, app_id, name, command):
    """Writes a desktop file, whose command line is a shell's, with % escaped as the Exec key needs it."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{app_id}.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Exec=sh -c '{command.replace('%', '%%')}'\n"
    )


def forking_command(tag):
    """Returns a command which starts two processes the moment it runs, as Chromium starts its zygotes, and then
    becomes a third, each a sleep whose argument is tag followed by its number, so that the test finds them."""
    return f"sleep {tag}1 & sleep {tag}2 & exec sleep {tag}3"


@test
def spawn_scopes(session, checks, out_dir):
    """Every way the session starts an application puts it into a scope of its own before it runs, so that the
    processes it starts at once are in that scope too, and logging out ends them. The application started at login
    by gnome-session, one started from the overview or the dash, and a command run with Alt+F2."""
    home = pathlib.Path.home()
    ways = {
        # tag: (how it is started, the application ID which names the scope)
        "6001": ("at login", "scope\\x2dautostart"),
        "6002": ("from the dash", "scope\\x2dtest"),
        "6003": ("with Alt+F2", "sh"),
    }
    write_desktop_file(home / ".config/autostart", "scope-autostart", "Scope autostart", forking_command("6001"))
    write_desktop_file(home / ".local/share/applications", "scope-test", "Scope test", forking_command("6002"))

    session.login()
    session.call("activateApp", "scope-test.desktop")
    session.call("spawnCommandLine", f"sh -c '{forking_command('6003')}'")

    shell_unit = "org.gnome.Shell@ubuntu.service"
    found = {}
    for tag, (how, name) in ways.items():
        pids = wait_for(lambda: (lambda p: p if len(p) == 3 else None)(processes(rf"^sleep {tag}[123]$")), 20) or []
        units = {pid: unit_of(cgroup_of(pid)) for pid in pids}
        found[tag] = units
        scope = re.compile(rf"^app-gnome-{re.escape(name)}-[0-9]+\.scope$")
        checks.check(f"started {how}", len(pids) == 3, f"{len(pids)} of 3 processes")
        if pids:
            same = len(set(units.values())) == 1
            checks.check(f"every process started {how} in its scope", same and all(scope.match(u or "") for u in units.values()),
                         str(units))
            checks.check(f"no process started {how} in GNOME Shell's unit", shell_unit not in units.values(), str(units))

    session.logout()
    left = processes(r"^sleep 600[123][123]$")
    checks.check("logging out ended every application", not left, f"left: {left}")
    return {"units": found}


TEXT_EDITOR = "org.gnome.TextEditor.desktop"
DRAFT_TEXT = "unsaved draft text"


def find(windows, predicate):
    return next((w for w in windows if predicate(w)), None)


def shown_window(session, app, predicate, timeout=20):
    """Waits until the application has a window matching predicate which is shown, has a size and, as a new window
    should, the focus, and returns it, or the matching window as it is after timeout, or None."""
    shown = wait_for(lambda: find(session.windows(app), lambda w: predicate(w) and w["width"] > 0 and w["focused"]), timeout)
    return shown or find(session.windows(app), predicate)


def window_summary(window):
    return {k: window[k] for k in ("title", "workspace", "focused", "x", "y", "width", "height")} if window else None


@test
def text_editor(session, checks, out_dir):
    """Text Editor returns its windows to their workspaces after logging out and in again, with the text typed into a
    document never saved, and without activating them, while every launch, from the dock or of a file opened from a
    terminal, opens a new window on the current workspace and focuses it."""
    home = pathlib.Path.home()
    document = home / "one.txt"
    document.write_text("first line\n")
    drafts = home / ".local/share/org.gnome.TextEditor/drafts"
    session.gsettings("org.gnome.shell", "favorite-apps", f"['{TEXT_EDITOR}']")

    session.login()
    workspaces = session.call("workspaces")
    checks.check("the session has the 12 workspaces configured", workspaces["count"] == 12, str(workspaces))

    # Started from the dock on workspace 2, it opens a new document, into which text is typed and never saved.
    session.call("activateWorkspace", 1)
    session.call("clickDockIcon", TEXT_EDITOR)
    draft = shown_window(session, TEXT_EDITOR, lambda w: True)
    checks.check("started from the dock on the current workspace, focused",
                 draft and draft["workspace"] == 1 and draft["focused"], str(window_summary(draft)))
    if not draft:
        return {}
    # The document accepts text once it shows, a moment after the window gained the focus.
    time.sleep(1)
    session.type_text(DRAFT_TEXT)
    saved = wait_for(lambda: drafts.is_dir() and any(DRAFT_TEXT in p.read_text(errors="replace") for p in drafts.iterdir()), 20)
    checks.check("the typed text autosaved as a draft", saved,
                 str([p.read_text(errors="replace")[:80] for p in drafts.iterdir()]) if drafts.is_dir() else "no drafts")

    # A file opened from a terminal on workspace 5 opens in a new window there.
    session.call("activateWorkspace", 4)
    session.run_in_session("gio", "open", str(document))
    opened = shown_window(session, TEXT_EDITOR, lambda w: "one.txt" in (w["title"] or ""))
    checks.check("a file opened from a terminal opens in a new window on the current workspace, focused",
                 opened and opened["workspace"] == 4 and opened["focused"], str(window_summary(opened)))

    # Clicked in the dock on workspace 8, where it has no window, it opens a new window there.
    session.call("activateWorkspace", 7)
    known = {w["id"] for w in session.windows(TEXT_EDITOR)}
    session.call("clickDockIcon", TEXT_EDITOR)
    third = shown_window(session, TEXT_EDITOR, lambda w: w["id"] not in known)
    checks.check("clicked in the dock on a workspace without its window, it opens a new one there, focused",
                 third and third["workspace"] == 7 and third["focused"], str(window_summary(third)))
    after_launches = session.windows(TEXT_EDITOR)
    checks.check("launches left the earlier windows on their workspaces",
                 find(after_launches, lambda w: w["id"] == draft["id"] and w["workspace"] == 1)
                 and find(after_launches, lambda w: opened and w["id"] == opened["id"] and w["workspace"] == 4),
                 str([window_summary(w) for w in after_launches]))

    # The session is saved a few seconds after the last change, as auto-save-delay says.
    time.sleep(6)
    first = session.windows(TEXT_EDITOR)
    dialogs = session.logout()
    # Text Editor holds the logout while it has unsaved documents, also when their drafts are saved, and GNOME Shell
    # lists it by name.
    listed = [d["applications"] for shown in dialogs for d in shown["dialogs"] if d["type"] == "EndSessionDialog"]
    checks.check("logging out lists Text Editor as having unsaved work", listed == [["Text Editor"]], str(dialogs))

    session.login()
    session.call("activateWorkspace", 10)
    session.call("clickDockIcon", TEXT_EDITOR)
    shown_window(session, TEXT_EDITOR, lambda w: w["workspace"] == 10)
    time.sleep(2)
    second = session.windows(TEXT_EDITOR)
    restored_draft = find(second, lambda w: w["workspace"] == 1)
    restored_file = find(second, lambda w: "one.txt" in (w["title"] or ""))
    new = find(second, lambda w: w["workspace"] == 10)
    checks.check("the document with the draft returns to its workspace, not focused",
                 restored_draft and not restored_draft["focused"], str(window_summary(restored_draft)))
    checks.check("the file returns to its workspace, not focused",
                 restored_file and restored_file["workspace"] == 4 and not restored_file["focused"], str(window_summary(restored_file)))
    checks.check("the launch after logging in opens a new window on the current workspace, focused",
                 new and new["focused"], str(window_summary(new)))
    checks.check("restoring switched no workspace", session.call("workspaces")["active"] == 10, str(session.call("workspaces")))

    # The draft has the text typed before logging out.
    if restored_draft:
        session.call("activateWindow", restored_draft["id"])
        wait_for(lambda: find(session.windows(TEXT_EDITOR), lambda w: w["id"] == restored_draft["id"] and w["focused"]), 10)
        session.press("a", "Control_L")
        session.press("c", "Control_L")
        text = wait_for(lambda: session.call("clipboardText"), 10)
        checks.check("the draft has the text typed before logging out", text and DRAFT_TEXT in text, repr(text))
    session.logout()
    return {"before": first, "after": second, "dialogs": dialogs}


PTYXIS = "org.gnome.Ptyxis.desktop"


def copy_terminal_text(session, window):
    """Activates a Ptyxis window and returns the text of its terminal, as selecting all and copying gives it."""
    session.call("activateWindow", window["id"])
    wait_for(lambda: find(session.windows(PTYXIS), lambda w: w["id"] == window["id"] and w["focused"]), 10)
    time.sleep(0.5)
    session.press("a", "Control_L", "Shift_L")
    session.press("c", "Control_L", "Shift_L")
    time.sleep(0.5)
    return session.call("clipboardText") or ""


@test
def ptyxis(session, checks, out_dir):
    """Ptyxis returns its windows to their workspaces, positions and sizes after logging out and in again, each with
    the screen it showed and a new shell in the directory the old one was in, without activating them, while every
    launch from the dock opens a new window on the current workspace and focuses it. Logging out while a program runs
    in it lists Ptyxis in GNOME's dialog."""
    # The shells run with Ubuntu's .bashrc, which the user's home received from /etc/skel.
    home = pathlib.Path.home()
    (home / "one").mkdir()
    (home / "two").mkdir()
    session.gsettings("org.gnome.shell", "favorite-apps", f"['{PTYXIS}']")

    session.login()
    session.call("activateWorkspace", 1)
    session.call("clickDockIcon", PTYXIS)
    first = shown_window(session, PTYXIS, lambda w: True)
    checks.check("started from the dock on the current workspace, focused",
                 first and first["workspace"] == 1 and first["focused"], str(window_summary(first)))
    if not first:
        return {}
    time.sleep(1)
    session.type_text("cd one\necho first screen\n")

    session.call("activateWorkspace", 4)
    session.call("clickDockIcon", PTYXIS)
    second = shown_window(session, PTYXIS, lambda w: w["id"] != first["id"])
    checks.check("clicked in the dock on a workspace without its window, it opens a new one there, focused",
                 second and second["workspace"] == 4 and second["focused"], str(window_summary(second)))
    if not second:
        return {}
    time.sleep(1)
    session.type_text("cd two\necho second screen\n")
    geometry = (100, 120, 1000, 600)
    session.call("moveResizeWindow", second["id"], *geometry)

    # Ptyxis saves its session a few seconds after a change, and a program running in a terminal holds the logout.
    time.sleep(5)
    session.type_text("sleep 600\n")
    time.sleep(1)
    before = session.windows(PTYXIS)
    dialogs = session.logout()
    listed = [d["applications"] for shown in dialogs for d in shown["dialogs"] if d["type"] == "EndSessionDialog"]
    checks.check("logging out while a program runs lists Ptyxis by name", listed == [["Terminal"]], str(dialogs))

    session.login()
    session.call("activateWorkspace", 10)
    session.call("clickDockIcon", PTYXIS)
    new = shown_window(session, PTYXIS, lambda w: w["workspace"] == 10, 30)
    time.sleep(2)
    after = session.windows(PTYXIS)
    restored_first = find(after, lambda w: w["workspace"] == 1)
    restored_second = find(after, lambda w: w["workspace"] == 4)
    checks.check("the first window returns to its workspace, not focused",
                 restored_first and not restored_first["focused"], str(window_summary(restored_first)))
    checks.check("the second window returns to its workspace, not focused",
                 restored_second and not restored_second["focused"], str(window_summary(restored_second)))
    if restored_second:
        placed = (restored_second["x"], restored_second["y"], restored_second["width"], restored_second["height"])
        checks.check("the second window returns to its position and size", placed == geometry, f"{placed}, was {geometry}")
    checks.check("the launch after logging in opens a new window on the current workspace, focused",
                 new and new["workspace"] == 10 and new["focused"], str(window_summary(new)))
    checks.check("restoring switched no workspace", session.call("workspaces")["active"] == 10, str(session.call("workspaces")))

    texts = {}
    for name, window, directory in (("first", restored_first, "one"), ("second", restored_second, "two")):
        if not window:
            continue
        session.call("activateWindow", window["id"])
        time.sleep(1)
        session.type_text("pwd\n")
        text = copy_terminal_text(session, window)
        texts[name] = text
        checks.check(f"the {name} window shows its screen from before logging out", f"{name} screen" in text, repr(text[-300:]))
        checks.check(f"the {name} window's new shell is in the directory of the old one", f"{home}/{directory}\n" in text,
                     repr(text[-300:]))
    session.logout()
    return {"before": before, "after": after, "dialogs": dialogs, "texts": texts}


FIREFOX = "firefox.desktop"
WEB_PORT = 8000


def write_pages(directory, names):
    """Writes a page for each name, whose title is the name, which Firefox shows in its window's title."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / f"{name}.html").write_text(f"<!DOCTYPE html><title>{name}</title><p>{name}</p>\n")


def serve(directory):
    """Serves the files in directory over HTTP on localhost, as the links which a terminal opens point to a web server."""
    return subprocess.Popen([sys.executable, "-I", "-m", "http.server", "--bind", "127.0.0.1", "--directory", str(directory),
                             str(WEB_PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def firefox_session_saved():
    """Whether a Firefox profile of the user has the session file Firefox writes only when it quits cleanly, and
    moves away when it starts."""
    home = pathlib.Path.home()
    return any(home.glob(".config/mozilla/firefox/*/sessionstore.jsonlz4")) or \
        any(home.glob(".mozilla/firefox/*/sessionstore.jsonlz4"))


def ended_before_shell(session, since, pid):
    """Returns whether the scope of the application launched as process pid ended before GNOME Shell began to shut
    down, according to the journal since a time in seconds since the epoch, and the two lines which tell."""
    lines = session.journal(since)
    ended = next((line for line in lines if f"-{pid}.scope" in line and
                  any(word in line for word in ("Deactivated", "Consumed", "Stopped "))), None)
    shell = next((line for line in lines if "Shutting down GNOME Shell" in line), None)
    return ended is not None and shell is not None and lines.index(ended) < lines.index(shell), [ended, shell]


def titled(name):
    return lambda w: (w["title"] or "").startswith(name)


@test
def firefox(session, checks, out_dir):
    """Firefox returns its windows to their workspaces, positions and sizes after logging out and in again, each with
    its page, without activating them, while every launch from the dock and every link opened from a terminal opens a
    new window on the current workspace and focuses it."""
    home = pathlib.Path.home()
    pages = home / "pages"
    write_pages(pages, ["page-one", "page-two", "page-three", "page-four"])
    server = serve(pages)
    # Links open in Firefox, as in the user's session, where Firefox is the default browser.
    (home / ".config").mkdir(exist_ok=True)
    (home / ".config/mimeapps.list").write_text(
        "[Default Applications]\nx-scheme-handler/http=firefox.desktop\nx-scheme-handler/https=firefox.desktop\ntext/html=firefox.desktop\n")
    session.gsettings("org.gnome.shell", "favorite-apps", f"['{FIREFOX}']")
    url = f"localhost:{WEB_PORT}"

    def load(page):
        session.press("l", "Control_L")
        time.sleep(0.5)
        session.type_text(f"{url}/{page}.html\n")

    try:
        session.login()
        session.call("activateWorkspace", 1)
        session.call("clickDockIcon", FIREFOX)
        first = shown_window(session, FIREFOX, lambda w: True, 60)
        checks.check("started from the dock on the current workspace, focused",
                     first and first["workspace"] == 1 and first["focused"], str(window_summary(first)))
        if not first:
            return {}
        time.sleep(2)
        load("page-one")
        first = wait_for(lambda: find(session.windows(FIREFOX), titled("page-one")), 30)
        checks.check("the first window shows its page", first, str([window_summary(w) for w in session.windows(FIREFOX)]))

        # A link opened from a terminal on workspace 5 opens in a new window there.
        session.call("activateWorkspace", 4)
        session.run_in_session("xdg-open", f"http://{url}/page-two.html")
        second = shown_window(session, FIREFOX, titled("page-two"), 30)
        checks.check("a link opened from a terminal opens in a new window on the current workspace, focused",
                     second and second["workspace"] == 4 and second["focused"], str(window_summary(second)))
        geometry = (100, 120, 1000, 600)
        if second:
            session.call("moveResizeWindow", second["id"], *geometry)

        # Clicked in the dock on workspace 8, where it has no window, it opens a new window there.
        session.call("activateWorkspace", 7)
        known = {w["id"] for w in session.windows(FIREFOX)}
        session.call("clickDockIcon", FIREFOX)
        third = shown_window(session, FIREFOX, lambda w: w["id"] not in known, 30)
        checks.check("clicked in the dock on a workspace without its window, it opens a new one there, focused",
                     third and third["workspace"] == 7 and third["focused"], str(window_summary(third)))
        if third:
            time.sleep(2)
            load("page-three")
            wait_for(lambda: find(session.windows(FIREFOX), titled("page-three")), 30)

        # Firefox saves its session every second.
        time.sleep(4)
        before = session.windows(FIREFOX)
        logout_started = time.time()
        dialogs = session.logout()
        checks.check("logging out asks nothing", not dialogs, str(dialogs))
        # Without a clean quit, the next start is a recovery from a crash, which restores the windows as they were.
        checks.check("logging out ends Firefox cleanly, with its session saved", firefox_session_saved())
        # Firefox quits when the session ends, and GNOME Shell and the processes of Firefox have to be there until it
        # has quit.
        ended, lines = ended_before_shell(session, logout_started, before[0]["pid"])
        checks.check("logging out lets Firefox quit before GNOME Shell shuts down", ended, str(lines))

        session.login()
        session.call("activateWorkspace", 10)
        session.call("clickDockIcon", FIREFOX)
        new = shown_window(session, FIREFOX, lambda w: w["workspace"] == 10, 60)
        wait_for(lambda: len(session.windows(FIREFOX)) >= 4, 30)
        time.sleep(3)
        after = session.windows(FIREFOX)
        for name, workspace in (("page-one", 1), ("page-two", 4), ("page-three", 7)):
            window = find(after, titled(name))
            checks.check(f"the window with {name} returns to its workspace, not focused",
                         window and window["workspace"] == workspace and not window["focused"], str(window_summary(window)))
        restored_second = find(after, titled("page-two"))
        if restored_second:
            placed = (restored_second["x"], restored_second["y"], restored_second["width"], restored_second["height"])
            checks.check("the window with page-two returns to its position and size", placed == geometry, f"{placed}, was {geometry}")
        checks.check("the launch after logging in opens a new window on the current workspace, focused",
                     new and new["focused"], str(window_summary(new)))
        checks.check("restoring switched no workspace", session.call("workspaces")["active"] == 10, str(session.call("workspaces")))

        # A link opened from a terminal while it runs opens in a new window on the current workspace too.
        session.call("activateWorkspace", 11)
        session.run_in_session("xdg-open", f"http://{url}/page-four.html")
        fourth = shown_window(session, FIREFOX, titled("page-four"), 30)
        checks.check("a link opened from a terminal after restoring opens in a new window on the current workspace, focused",
                     fourth and fourth["workspace"] == 11 and fourth["focused"], str(window_summary(fourth)))
        pid = session.windows(FIREFOX)[0]["pid"]
        logout_started = time.time()
        session.logout()
        checks.check("logging out again ends Firefox cleanly, with its session saved", firefox_session_saved())
        ended, lines = ended_before_shell(session, logout_started, pid)
        checks.check("logging out again lets Firefox quit before GNOME Shell shuts down", ended, str(lines))
        return {"before": before, "after": after, "dialogs": dialogs}
    finally:
        server.terminate()


CHROMIUM = "chromium.desktop"


def chromium_units():
    """Returns the units Chromium's processes are in, with how many processes each."""
    units = {}
    for pid in processes(r"^/usr/lib/chromium/chromium( |$)"):
        unit = unit_of(cgroup_of(pid))
        units[unit] = units.get(unit, 0) + 1
    return units


@test
def chromium(session, checks, out_dir):
    """Chromium ends cleanly when the user logs out, with its helpers stopped only after the browser process
    exited, and returns its windows to their workspaces, positions and sizes after logging in again, without activating
    them. Its helpers start in the scope its launch put it into, while the browser process moves into a scope of its
    own."""
    home = pathlib.Path.home()
    pages = home / "pages"
    write_pages(pages, ["page-one", "page-two"])
    server = serve(pages)
    session.gsettings("org.gnome.shell", "favorite-apps", f"['{CHROMIUM}']")
    url = f"localhost:{WEB_PORT}"

    def load(page):
        session.press("l", "Control_L")
        time.sleep(0.5)
        session.type_text(f"{url}/{page}.html\n")

    try:
        session.login()
        session.call("activateWorkspace", 1)
        session.call("clickDockIcon", CHROMIUM)
        first = shown_window(session, CHROMIUM, lambda w: True, 60)
        checks.check("started from the dock on the current workspace, focused",
                     first and first["workspace"] == 1 and first["focused"], str(window_summary(first)))
        if not first:
            return {}
        time.sleep(2)
        load("page-one")
        wait_for(lambda: find(session.windows(CHROMIUM), titled("page-one")), 30)

        session.call("activateWorkspace", 4)
        known = {w["id"] for w in session.windows(CHROMIUM)}
        session.call("clickDockIcon", CHROMIUM)
        second = shown_window(session, CHROMIUM, lambda w: w["id"] not in known, 30)
        checks.check("clicked in the dock on a workspace without its window, it opens a new one there, focused",
                     second and second["workspace"] == 4 and second["focused"], str(window_summary(second)))
        geometry = (100, 120, 1000, 600)
        if second:
            time.sleep(2)
            load("page-two")
            wait_for(lambda: find(session.windows(CHROMIUM), titled("page-two")), 30)
            session.call("moveResizeWindow", second["id"], *geometry)

        units = chromium_units()
        checks.check("Chromium's helpers are in the scope its launch put it into, the browser process in one of its own",
                     len(units) == 2 and any(re.match(r"^app-gnome-chromium-[0-9]+\.scope$", u or "") for u in units),
                     str(units))
        time.sleep(3)
        before = session.windows(CHROMIUM)
        logout_started = time.time()
        dialogs = session.logout()
        checks.check("logging out asks nothing", not dialogs, str(dialogs))
        crashes = [line for line in session.journal(logout_started)
                   if "GPU process isn't usable" in line or re.search(r"chromium.*(SIGTRAP|core dumped|Crashpad)", line)]
        checks.check("Chromium did not crash at logout", not crashes, str(crashes[:5]))
        preferences = json.loads((home / ".config/chromium/Default/Preferences").read_text())
        exit_type = preferences.get("profile", {}).get("exit_type")
        checks.check("Chromium recorded that the session ended", exit_type == "SessionEnded", str(exit_type))

        session.login()
        session.call("activateWorkspace", 10)
        session.call("clickDockIcon", CHROMIUM)
        wait_for(lambda: find(session.windows(CHROMIUM), titled("page-two")), 60)
        time.sleep(3)
        after = session.windows(CHROMIUM)
        for name, workspace in (("page-one", 1), ("page-two", 4)):
            window = find(after, titled(name))
            checks.check(f"the window with {name} returns to its workspace, not focused",
                         window and window["workspace"] == workspace and not window["focused"], str(window_summary(window)))
        restored_second = find(after, titled("page-two"))
        if restored_second:
            placed = (restored_second["x"], restored_second["y"], restored_second["width"], restored_second["height"])
            checks.check("the window with page-two returns to its position and size", placed == geometry, f"{placed}, was {geometry}")
        checks.check("restoring switched no workspace", session.call("workspaces")["active"] == 10, str(session.call("workspaces")))
        session.logout()
        return {"before": before, "after": after, "units": units, "dialogs": dialogs}
    finally:
        server.terminate()


SHUTDOWN_INHIBITOR = "Stop application scopes before shutdown"


def browser_process():
    """Returns the ID of Chromium's browser process, the one in the scope Chromium created for itself, or None."""
    for pid in processes(r"^/usr/lib/chromium/chromium( |$)"):
        if re.match(r"^app-org\.chromium\.Chromium-[0-9]+\.scope$", unit_of(cgroup_of(pid)) or ""):
            return pid
    return None


@test
def shutdown(session, checks, out_dir):
    """When logind announces a shutdown, Chromium exits before the shutdown starts, as a service of the user holds
    logind's delay inhibitor until Chromium's browser process exited, and Chromium neither crashes nor loses its
    session. Without it, logind starts the shutdown while the browser process still runs, and its helpers can die first."""
    home = pathlib.Path.home()
    pages = home / "pages"
    write_pages(pages, ["page-one"])
    server = serve(pages)
    session.gsettings("org.gnome.shell", "favorite-apps", f"['{CHROMIUM}']")
    try:
        session.login()
        own = lambda: [i for i in session.logind()["inhibitors"]
                       if i["who"] == SHUTDOWN_INHIBITOR and i["uid"] == os.getuid() and i["released"] is None]
        held = wait_for(own, 20)
        checks.check("the service holds a delay inhibitor for shutdowns",
                     held and held[0]["mode"] == "delay" and held[0]["what"] == "shutdown", str(held))

        session.call("clickDockIcon", CHROMIUM)
        shown_window(session, CHROMIUM, lambda w: True, 60)
        time.sleep(2)
        session.press("l", "Control_L")
        time.sleep(0.5)
        session.type_text(f"localhost:{WEB_PORT}/page-one.html\n")
        wait_for(lambda: find(session.windows(CHROMIUM), titled("page-one")), 30)
        browser = browser_process()
        checks.check("Chromium's browser process runs in a scope of its own", browser, str(chromium_units()))
        time.sleep(3)

        announced = time.time()
        exited = None

        # Polled often, so that when the browser process exited is known closely enough to compare it with when the
        # inhibitor was released.
        def released():
            nonlocal exited
            if exited is None and browser and not pathlib.Path(f"/proc/{browser}").exists():
                exited = time.time()
            inhibitor = next((i for i in session.logind()["inhibitors"] if held and i["taken"] == held[0]["taken"]), None)
            return inhibitor if inhibitor and inhibitor["released"] else None

        session.prepare_for_shutdown(True)
        inhibitor = wait_for(released, 30, 0.01)
        checks.check("the inhibitor is released once logind announced the shutdown", inhibitor,
                     f"{inhibitor['released'] - announced:.3f} s after the announcement" if inhibitor else "not released")
        if inhibitor:
            # The poll which saw the release may have been the first to see the process gone too.
            exited = exited or time.time()
            checks.check("Chromium's browser process exited before the inhibitor was released",
                         exited - announced <= inhibitor["released"] - announced + 0.02,
                         f"exited {exited - announced:.3f} s, released {inhibitor['released'] - announced:.3f} s after the announcement")
        crashes = [line for line in session.journal(announced)
                   if "GPU process isn't usable" in line or re.search(r"chromium.*(SIGTRAP|core dumped|Crashpad)", line)]
        checks.check("Chromium did not crash", not crashes, str(crashes[:5]))
        preferences = json.loads((home / ".config/chromium/Default/Preferences").read_text())
        exit_type = preferences.get("profile", {}).get("exit_type")
        checks.check("Chromium recorded that the session ended", exit_type == "SessionEnded", str(exit_type))
        session.logout()
        return {"logind": session.logind()}
    finally:
        server.terminate()


@test
def chromium_quit(session, checks, out_dir):
    """Ctrl+Q quits Chromium, through the extension and native messaging host of this repository, as a logout does: the
    browser process exits on SIGTERM and records that the session ended, so that the next start restores every window,
    each on its workspace, and nothing crashes."""
    home = pathlib.Path.home()
    pages = home / "pages"
    write_pages(pages, ["page-one", "page-two"])
    server = serve(pages)
    session.gsettings("org.gnome.shell", "favorite-apps", f"['{CHROMIUM}']")
    url = f"localhost:{WEB_PORT}"

    def load(page):
        session.press("l", "Control_L")
        time.sleep(0.5)
        session.type_text(f"{url}/{page}.html\n")

    try:
        session.login()
        for workspace, page in ((1, "page-one"), (4, "page-two")):
            session.call("activateWorkspace", workspace)
            known = {w["id"] for w in session.windows(CHROMIUM)}
            session.call("clickDockIcon", CHROMIUM)
            if not shown_window(session, CHROMIUM, lambda w: w["id"] not in known, 60):
                checks.check(f"Chromium opened a window on workspace {workspace + 1}", False)
                return {}
            time.sleep(2)
            load(page)
            wait_for(lambda: find(session.windows(CHROMIUM), titled(page)), 30)
        browser = browser_process()
        time.sleep(3)

        pressed = time.time()
        session.press("q", "Control_L")
        gone = wait_for(lambda: not session.windows(CHROMIUM) and not processes(r"^/usr/lib/chromium/chromium( |$)"), 20)
        checks.check("Ctrl+Q quits Chromium with all its windows and processes", gone,
                     str([window_summary(w) for w in session.windows(CHROMIUM)]))
        crashes = [line for line in session.journal(pressed)
                   if "GPU process isn't usable" in line or re.search(r"chromium.*(SIGTRAP|core dumped|Crashpad)", line)]
        checks.check("Chromium did not crash", not crashes, str(crashes[:5]))
        preferences = json.loads((home / ".config/chromium/Default/Preferences").read_text())
        exit_type = preferences.get("profile", {}).get("exit_type")
        checks.check("Chromium recorded that the session ended, as at a logout", exit_type == "SessionEnded", str(exit_type))

        session.call("activateWorkspace", 10)
        session.call("clickDockIcon", CHROMIUM)
        wait_for(lambda: find(session.windows(CHROMIUM), titled("page-two")), 60)
        time.sleep(3)
        after = session.windows(CHROMIUM)
        for name, workspace in (("page-one", 1), ("page-two", 4)):
            window = find(after, titled(name))
            checks.check(f"the next start restores the window with {name} to its workspace, not focused",
                         window and window["workspace"] == workspace and not window["focused"], str(window_summary(window)))
        session.logout()
        return {"browser": browser, "after": after}
    finally:
        server.terminate()


RESERVE_SPACE = "reserve-space@mitar.tnode.com"


def sizes_while(session, window_id, seconds):
    """Returns the distinct sizes a window had while sampled for seconds."""
    sizes = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        window = find(session.windows(), lambda w: w["id"] == window_id)
        if window:
            size = (window["width"], window["height"])
            if size not in sizes:
                sizes.append(size)
        time.sleep(0.1)
    return sizes


@test
def lock_screen(session, checks, out_dir):
    """Locking the screen leaves a maximized window its size with reserve-space@mitar.tnode.com, although GNOME Shell
    disables the dock meanwhile, whose space a window would otherwise fill, as it does without the extension. The locked
    screen offers no screenshots: quick settings have no screenshot button, and Print and Shift+Print do nothing."""
    home = pathlib.Path.home()
    extensions = home / ".local/share/gnome-shell/extensions"
    extensions.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_DIR / "gnome-extensions" / RESERVE_SPACE, extensions / RESERVE_SPACE)
    screenshots = home / "Pictures/Screenshots"
    session.gsettings("org.gnome.shell", "favorite-apps", f"['{TEXT_EDITOR}']")

    def maximized_window():
        known = {w["id"] for w in session.windows(TEXT_EDITOR)}
        session.call("clickDockIcon", TEXT_EDITOR)
        window = shown_window(session, TEXT_EDITOR, lambda w: w["id"] not in known, 30)
        if not window:
            return None, None
        session.call("maximizeWindow", window["id"])
        time.sleep(2)
        window = find(session.windows(), lambda w: w["id"] == window["id"])
        return window, (window["width"], window["height"])

    # Without the extension, the window fills the dock's space while the screen is locked.
    session.login()
    window, size = maximized_window()
    if not window:
        checks.check("Text Editor started", False)
        return {}
    checks.check("the maximized window leaves the dock its space", window["maximized"] and window["width"] < 1920, str(window_summary(window)))
    session.call("lock")
    without = sizes_while(session, window["id"], 3)
    checks.check("without reserve-space, locking resizes the maximized window", without != [size], f"{without}, was {size}")
    session.call("unlock")
    session.logout()

    # The extension is enabled at login, as configure-gnome-desktop.sh enables it. GNOME Shell then enables it before
    # the dock, which it would otherwise disable and enable again when it disables the dock for the lock screen.
    session.enable_extensions(RESERVE_SPACE)
    session.login()
    checks.check("reserve-space is enabled", session.call("extensionState", RESERVE_SPACE), str(session.call("extensionState", RESERVE_SPACE)))
    window, size = maximized_window()
    if not window:
        checks.check("Text Editor started again", False)
        return {}
    # The extension follows the space the dock reserves a few seconds after it settled.
    time.sleep(7)
    chrome_unlocked = session.call("trackedChrome")
    session.call("lock")
    checks.check("the screen is locked", wait_for(lambda: session.call("locked"), 5))
    while_locked = sizes_while(session, window["id"], 3)
    chrome_locked = session.call("trackedChrome")
    checks.check("with reserve-space, locking leaves the maximized window its size", while_locked == [size], f"{while_locked}, was {size}")

    state = session.call("screenshotState")
    checks.check("the locked screen's quick settings have no screenshot button", state["button"] is False, str(state))
    before = sorted(screenshots.iterdir()) if screenshots.is_dir() else []
    session.press("Print")
    time.sleep(1)
    checks.check("Print opens no screenshot UI on the locked screen", not session.call("screenshotState")["open"],
                 str(session.call("screenshotState")))
    session.press("Print", "Shift_L")
    time.sleep(2)
    after = sorted(screenshots.iterdir()) if screenshots.is_dir() else []
    checks.check("Shift+Print saves no screenshot on the locked screen", after == before, str(after))

    session.call("unlock")
    after_unlock = sizes_while(session, window["id"], 7)
    checks.check("with reserve-space, unlocking leaves the maximized window its size", after_unlock == [size], f"{after_unlock}, was {size}")
    checks.check("unlocked, quick settings have the screenshot button again", session.call("screenshotState")["button"] is True,
                 str(session.call("screenshotState")))
    session.logout()
    return {"size": size, "without": without, "while_locked": while_locked, "after_unlock": after_unlock,
            "chrome_unlocked": chrome_unlocked, "chrome_locked": chrome_locked}


def main():
    name, out_dir = sys.argv[1], pathlib.Path(sys.argv[2])
    if name not in TESTS:
        print(f"unknown test {name}, known are {', '.join(TESTS)}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    checks = Checks(name, out_dir)
    session = Session(out_dir)
    skipped = session.apply_desktop_settings()
    (out_dir / "settings-skipped.txt").write_text("".join(f"{key}\n" for key in skipped))
    session.enable_extensions(TEST_EXTENSION)
    extra = {}
    try:
        extra = TESTS[name](session, checks, out_dir) or {}
    except Exception as error:  # noqa: BLE001 - any failure of the test is reported as one
        checks.check("test ran to its end", False, f"{type(error).__name__}: {error}")
    # Neither the session nor logging out of it may leave a unit failed, as at-spi-dbus-bus.service did at a logout.
    failed = session.failed_units()
    checks.check("no unit of the user's service manager failed", not failed, str(failed))
    return checks.finish(**extra)


sys.exit(main())
