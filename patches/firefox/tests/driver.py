"""Drive Firefox through Marionette to test Wayland session restore.

Usage: driver.py FIREFOX PROFILE_DIR LOG_DIR SCENARIO

SCENARIO is one of:

  menu-restore     quit with several windows, start again without restoring and with an activation token,
                   which the window it starts with has to receive, then restore the previous session as the
                   History menu does
  startup-restore  quit with several windows, start again with automatic session restore and an activation
                   token, as when launched from the dock; the saved windows have to be restored into windows of
                   their own, beside a new window with the start page, which has to receive the token and the
                   focus
  startup-no-ids   like startup-restore, but the first run has session management disabled, so it saves its
                   windows without IDs, as every profile used on GNOME before the patches did
  startup-url      like startup-restore, but the second run is started with a URL, as when a link is opened from
                   another application, and external links open in a new window; the URL has to open in the new
                   window
  startup-url-tab  like startup-url, with external links opening in a tab as by default; the URL has to open as
                   a tab of the first restored window, and no window may receive the token

Each window of the first run shows a page of its own. A third run then restores the session the second one saved,
in which the last window is given the ID DIGITS_ID. Each Firefox run writes its output to LOG_DIR/runN.log, and a
summary of each run is printed. The first run has to have 3 windows, the second 3 or, when it opens a new window
beside the restored ones, 4, and the third one more than the second saved, all of them shown. Every run has to exit
with status 0. In the third run, Firefox has to restore the window given DIGITS_ID under it, and the compositor has to
restore every other window the second run saved, which it does only when Firefox uses session management. The script
then prints "RESULT: OK" and exits with 0. It exits with 1 when a check fails, or when Firefox exited on its own.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# A port of its own, so that a Firefox left over from an earlier run, still listening on its port, is not
# the one the test talks to.
PORT = free_port()


class Marionette:
    def __init__(self, port):
        deadline = time.time() + 60
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=60)
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        self.buf = b""
        self.msgid = 0
        self._read()
        self.call("WebDriver:NewSession", {"capabilities": {}})
        self.call("Marionette:SetContext", {"value": "chrome"})

    def _read(self):
        while True:
            if b":" in self.buf:
                length, rest = self.buf.split(b":", 1)
                length = int(length)
                if len(rest) >= length:
                    self.buf = rest[length:]
                    return json.loads(rest[:length])
            self._recv()

    def _recv(self):
        data = self.sock.recv(65536)
        if not data:
            raise ConnectionError("marionette connection closed")
        self.buf += data

    def call(self, name, params):
        self.msgid += 1
        data = json.dumps([0, self.msgid, name, params]).encode()
        self.sock.sendall(str(len(data)).encode() + b":" + data)
        while True:
            msg = self._read()
            if msg[0] == 1 and msg[1] == self.msgid:
                if msg[2]:
                    raise RuntimeError("%s failed: %s" % (name, msg[2]))
                return msg[3]

    def script(self, source):
        return self.call("WebDriver:ExecuteAsyncScript", {"script": source, "args": [], "scriptTimeout": 60000})["value"]


# What external_links passed to write_prefs sets browser.link.open_newwindow.override.external to: 2 opens external
# links in a new window, and -1, the default, opens them as browser.link.open_newwindow says, in a new tab by
# default.
EXTERNAL_LINKS = {"window": 2, "default": -1}

# The ID the first run gives one of its windows, instead of the random one Firefox generates. Firefox replaces an ID
# saved on X11, the workspace number, by a new one, and must not take an ID whose first group consists of decimal
# digits, like this one, for such a number, which about one in 43 random IDs is.
DIGITS_ID = "{12345678-1234-4123-8123-123456789abc}"

# The activation token every second run is launched with, in both variables launchers set.
# The compositor does not know it and ignores it, but the WAYLAND_DEBUG output shows the window Firefox passes it
# with.
ACTIVATION_TOKEN = "session-test-activation-token"


def write_prefs(profile, startup_page, session_management=True, external_links="default"):
    with open(os.path.join(profile, "user.js"), "w") as f:
        for key, value in [
            ("widget.wayland.session-management.enabled", session_management),
            ("marionette.port", PORT),
            ("browser.startup.page", startup_page),
            ("browser.sessionstore.interval", 1000),
            ("browser.sessionstore.resume_from_crash", False),
            ("browser.aboutwelcome.enabled", False),
            ("browser.startup.homepage_override.mstone", "ignore"),
            ("browser.shell.checkDefaultBrowser", False),
            ("datareporting.policy.dataSubmissionEnabled", False),
            ("toolkit.telemetry.reportingpolicy.firstRun", False),
            ("browser.newtabpage.enabled", False),
            ("browser.startup.homepage", "about:blank"),
            ("app.update.disabledForTesting", True),
            ("browser.link.open_newwindow.override.external", EXTERNAL_LINKS[external_links]),
        ]:
            f.write("user_pref(%s, %s);\n" % (json.dumps(key), json.dumps(value)))


def start(firefox, profile, log_path, urls=(), token=None):
    log = open(log_path, "w")
    # Marionette refuses the chrome context the tests run their scripts in without system access.
    env = dict(os.environ, MOZ_REMOTE_ALLOW_SYSTEM_ACCESS="1", MOZ_CRASHREPORTER_DISABLE="1")
    if token:
        env.update(XDG_ACTIVATION_TOKEN=token, DESKTOP_STARTUP_ID=token)
    proc = subprocess.Popen([firefox, "-marionette", "-no-remote", "-profile", profile, *urls],
                            stdout=log, stderr=subprocess.STDOUT, env=env)
    try:
        return proc, Marionette(PORT)
    except BaseException:
        proc.kill()
        proc.wait()
        raise


def open_windows(m, count):
    m.script("""
        let [resolve] = arguments;
        let opened = 0;
        for (let i = 0; i < %d; i++) {
          let win = OpenBrowserWindow();
          win.addEventListener("load", () => { if (++opened == %d) resolve(true); }, { once: true });
        }
    """ % (count, count))


def load_pages(m, urls):
    """Loads each of urls into the selected tab of a window of its own."""
    m.script("""
        let [resolve] = arguments;
        let urls = %s;
        let windows = Array.from(Services.wm.getEnumerator("navigator:browser"));
        let principal = Services.scriptSecurityManager.getSystemPrincipal();
        windows.forEach((win, i) => {
          win.gBrowser.selectedBrowser.fixupAndLoadURIString(urls[i], { triggeringPrincipal: principal });
        });
        resolve(true);
    """ % json.dumps(urls))


# Defines tabPages(win) in a script, which returns the sorted pages of the tabs of win, from the session store,
# which also knows the pages of tabs which are not loaded yet.
TAB_PAGES = """
    let tabPages = win => win.gBrowser.tabs.map(tab => {
      let state = JSON.parse(SessionStore.getTabState(tab));
      return state.entries.length ? state.entries[state.index - 1].url : tab.linkedBrowser.currentURI.spec;
    }).sort();
"""


def window_pages(m):
    """Returns the sorted lists of the pages of the tabs of each window."""
    return sorted(m.script(TAB_PAGES + """
        let [resolve] = arguments;
        resolve(Array.from(Services.wm.getEnumerator("navigator:browser")).map(tabPages));
    """))


def focused_window_pages(m):
    """Returns the pages of the tabs of the window which has the focus, or None when no window has it."""
    return m.script(TAB_PAGES + """
        let [resolve] = arguments;
        let win = Services.focus.activeWindow;
        resolve(win ? tabPages(win) : null);
    """)


def window_count(m):
    return m.script("""
        let [resolve] = arguments;
        resolve(Array.from(Services.wm.getEnumerator("navigator:browser")).length);
    """)


def quit(m, proc):
    try:
        m.call("Marionette:Quit", {"flags": ["eAttemptQuit"]})
    except (ConnectionError, RuntimeError, OSError):
        pass
    try:
        return proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait()


def mapped_toplevels(log_path):
    # Every window which is shown creates an xdg_toplevel, one which stays hidden does not.
    with open(log_path, errors="replace") as f:
        return sum(1 for line in f if ".get_toplevel(new id xdg_toplevel#" in line)


def restored_toplevels(log_path):
    # The compositor answers each window it places again as it was in the session with restored().
    with open(log_path, errors="replace") as f:
        return sum(1 for line in f if "toplevel_session_v1#" in line and ".restored()" in line)


MOZLZ4_MAGIC = b"mozLz40\0"


def read_mozlz4(path):
    """Returns the JSON value of a file Firefox compressed with LZ4, like its saved session."""
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(MOZLZ4_MAGIC):
        raise ValueError("%s is not a mozLz4 file" % path)
    size = int.from_bytes(data[8:12], "little")
    src = data[12:]
    out = bytearray()
    i = 0

    def length(value):
        nonlocal i
        if value == 15:
            while True:
                byte = src[i]
                i += 1
                value += byte
                if byte != 255:
                    break
        return value

    # An LZ4 block is a sequence of literals, each followed by a match copied from the output so far, except the last.
    while i < len(src):
        token = src[i]
        i += 1
        count = length(token >> 4)
        out += src[i:i + count]
        i += count
        if i >= len(src):
            break
        offset = src[i] | src[i + 1] << 8
        i += 2
        # A match can overlap the bytes it produces, so it is copied byte by byte.
        for _ in range(length(token & 15) + 4):
            out.append(out[-offset])
    return json.loads(bytes(out[:size]))


def write_mozlz4(path, value):
    """Writes value as JSON to a file in the format of read_mozlz4, as a single LZ4 literal run, which needs no
    compression to be valid."""
    data = json.dumps(value).encode()
    block = bytearray([min(len(data), 15) << 4])
    if len(data) >= 15:
        rest = len(data) - 15
        while rest >= 255:
            block.append(255)
            rest -= 255
        block.append(rest)
    with open(path, "wb") as f:
        f.write(MOZLZ4_MAGIC + len(data).to_bytes(4, "little") + block + data)


def give_saved_window_id(profile, workspace_id):
    """Gives the last window of the session saved in profile the ID workspace_id."""
    path = os.path.join(profile, "sessionstore.jsonlz4")
    state = read_mozlz4(path)
    state["windows"][-1]["workspaceID"] = workspace_id
    write_mozlz4(path, state)


def named_toplevels(log_path, name):
    """Returns for each window added to the session under name or restored under it "add" or "restore", in order."""
    with open(log_path, errors="replace") as f:
        return re.findall(r'-> xdg_session_v1#\d+\.(add|restore)_toplevel\(new id xdg_toplevel_session_v1#\d+, '
                          r'xdg_toplevel#\d+, "%s"\)' % re.escape(name), f.read())


def activated_toplevels(log_path, token):
    """Returns for each activation request with token whether the window it names was added to the session under a
    new ID ("add") or restored under its saved one ("restore"), or None when the window was not added to it."""
    xdg_surfaces = {}
    toplevels = {}
    kinds = {}
    activated = []
    with open(log_path, errors="replace") as f:
        for line in f:
            if " -> " not in line:
                continue
            if match := re.search(r"\.get_xdg_surface\(new id xdg_surface#(\d+), wl_surface#(\d+)\)", line):
                xdg_surfaces[match[1]] = match[2]
            elif match := re.search(r"xdg_surface#(\d+)\.get_toplevel\(new id xdg_toplevel#(\d+)\)", line):
                toplevels[xdg_surfaces.get(match[1])] = match[2]
            elif match := re.search(r"\.(add|restore)_toplevel\(new id xdg_toplevel_session_v1#\d+, xdg_toplevel#(\d+),", line):
                kinds[match[2]] = match[1]
            elif match := re.search(r'xdg_activation_v1#\d+\.activate\("([^"]*)", wl_surface#(\d+)\)', line):
                if match[1] == token:
                    activated.append(toplevels.get(match[2]))
    return [kinds.get(toplevel) for toplevel in activated]


def alive_after(proc, seconds):
    try:
        proc.wait(timeout=seconds)
        return False
    except subprocess.TimeoutExpired:
        return True


failures = []


def report(line, value, expected):
    """Prints line formatted with value. When value is not the expected one, marks the line as a failure and records it."""
    if value == expected:
        print(line % value, flush=True)
    else:
        print(line % value + " (FAIL: expected %s)" % expected, flush=True)
        failures.append(line % value)


def write_page(log_dir, name):
    path = os.path.join(log_dir, name + ".html")
    with open(path, "w") as f:
        f.write("<!DOCTYPE html><title>%s</title><p>%s</p>\n" % (name, name))
    return "file://" + path


def main():
    firefox, profile, log_dir, scenario = sys.argv[1:5]
    shutil.rmtree(profile, ignore_errors=True)
    os.makedirs(profile)
    os.makedirs(log_dir, exist_ok=True)

    with_url = scenario in ("startup-url", "startup-url-tab")
    at_startup = scenario != "menu-restore"
    external_links = "window" if scenario == "startup-url" else "default"
    # A restore at startup opens a new window beside the restored ones, unless the URL opens in a tab of one of them.
    run2_windows = 3 if scenario in ("menu-restore", "startup-url-tab") else 4
    pages = [write_page(log_dir, "page%d" % i) for i in range(1, 4)]
    link = write_page(log_dir, "link")

    write_prefs(profile, 0 if scenario == "menu-restore" else 3, scenario != "startup-no-ids", external_links)
    proc, m = start(firefox, profile, os.path.join(log_dir, "run1.log"))
    open_windows(m, 2)
    report("run 1: %s windows", window_count(m), 3)
    load_pages(m, pages)
    # Session store collects window data, and so calls GetWorkspaceID(), every second.
    time.sleep(5)
    report("run 1: %s windows shown", mapped_toplevels(os.path.join(log_dir, "run1.log")), 3)
    report("run 1: exit status %s", quit(m, proc), 0)

    write_prefs(profile, 0 if scenario == "menu-restore" else 3, external_links=external_links)
    proc, m = start(firefox, profile, os.path.join(log_dir, "run2.log"), [link] if with_url else [], ACTIVATION_TOKEN)
    windows = None
    try:
        if scenario == "menu-restore":
            print("run 2: %d windows before restoring" % window_count(m), flush=True)
            m.script("""
                let [resolve] = arguments;
                SessionStore.restoreLastSession();
                resolve(true);
            """)
        time.sleep(3)
        if alive_after(proc, 5):
            windows = window_count(m)
    except (ConnectionError, OSError):
        pass
    if proc.poll() is not None or not alive_after(proc, 0.1):
        print("run 2: FIREFOX EXITED with status %s" % proc.wait(), flush=True)
        return 1
    run2_log = os.path.join(log_dir, "run2.log")
    report("run 2: %s windows, still running after the restore", windows, run2_windows)
    report("run 2: %s windows shown", mapped_toplevels(run2_log), run2_windows)
    if scenario == "menu-restore":
        report("run 2: activation token passed with windows %s", activated_toplevels(run2_log, ACTIVATION_TOKEN), ["add"])
    elif scenario == "startup-url-tab":
        # The URL opens in a tab of the first window of the saved session, and the token is dropped.
        report("run 2: pages of the windows %s", window_pages(m), sorted([sorted([pages[0], link]), [pages[1]], [pages[2]]]))
        report("run 2: activation token passed with windows %s", activated_toplevels(run2_log, ACTIVATION_TOKEN), [])
    elif at_startup:
        # The start page is about:blank, see write_prefs.
        new_page = link if with_url else "about:blank"
        report("run 2: pages of the windows %s", window_pages(m), sorted([[new_page]] + [[page] for page in pages]))
        report("run 2: activation token passed with windows %s", activated_toplevels(run2_log, ACTIVATION_TOKEN), ["add"])
        # Windows saved without IDs are shown at the same time as the new one, so which of them has the focus is
        # left open.
        if scenario != "startup-no-ids":
            report("run 2: %s windows restored by the compositor", restored_toplevels(run2_log), 3)
            report("run 2: pages of the focused window %s", focused_window_pages(m), [new_page])
    report("run 2: exit status %s", quit(m, proc), 0)

    # A third run restores the session saved by the second one, which shows that the IDs it saved are the ones the
    # compositor knows. One saved window is given DIGITS_ID instead, under which Firefox has to restore it, though the
    # compositor does not know it and so does not restore it.
    give_saved_window_id(profile, DIGITS_ID)
    write_prefs(profile, 3, external_links=external_links)
    proc, m = start(firefox, profile, os.path.join(log_dir, "run3.log"))
    time.sleep(3)
    if not alive_after(proc, 5):
        print("run 3: FIREFOX EXITED with status %s" % proc.returncode, flush=True)
        return 1
    # The session store does not save a window which shows only about:blank, the start page.
    run2_saved = 3 if scenario in ("startup-restore", "startup-no-ids") else run2_windows
    report("run 3: %s windows", window_count(m), run2_saved + 1)
    report("run 3: %s windows shown", mapped_toplevels(os.path.join(log_dir, "run3.log")), run2_saved + 1)
    report("run 3: %s windows restored by the compositor", restored_toplevels(os.path.join(log_dir, "run3.log")),
           run2_saved - 1)
    report("run 3: windows named " + DIGITS_ID + " %s", named_toplevels(os.path.join(log_dir, "run3.log"), DIGITS_ID),
           ["restore"])
    report("run 3: exit status %s", quit(m, proc), 0)

    print("RESULT: %s" % ("OK" if not failures else "%d check(s) failed" % len(failures)), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
