"""Drive Firefox through Marionette to test Wayland session restore.

Usage: driver.py FIREFOX PROFILE_DIR LOG_DIR SCENARIO

SCENARIO is one of:

  menu-restore     quit with several windows, start again without restoring, then restore the previous
                   session as the History menu does
  startup-restore  quit with several windows, start again with automatic session restore
  startup-no-ids   like startup-restore, but the first run has session management disabled, so it saves its
                   windows without IDs, as every profile used on GNOME before the patches did

A third run then restores the session the second one saved. Each Firefox run writes its output to
LOG_DIR/runN.log, and a summary of each run is printed. The exit status is 1 when Firefox exited on its own.
"""

import json
import os
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


def write_prefs(profile, startup_page, session_management=True):
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
        ]:
            f.write("user_pref(%s, %s);\n" % (json.dumps(key), json.dumps(value)))


def start(firefox, profile, log_path):
    log = open(log_path, "w")
    # Marionette refuses the chrome context the tests run their scripts in without system access.
    env = dict(os.environ, MOZ_REMOTE_ALLOW_SYSTEM_ACCESS="1", MOZ_CRASHREPORTER_DISABLE="1")
    proc = subprocess.Popen([firefox, "-marionette", "-no-remote", "-profile", profile],
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


def alive_after(proc, seconds):
    try:
        proc.wait(timeout=seconds)
        return False
    except subprocess.TimeoutExpired:
        return True


def main():
    firefox, profile, log_dir, scenario = sys.argv[1:5]
    shutil.rmtree(profile, ignore_errors=True)
    os.makedirs(profile)
    os.makedirs(log_dir, exist_ok=True)

    write_prefs(profile, 0 if scenario == "menu-restore" else 3, scenario != "startup-no-ids")
    proc, m = start(firefox, profile, os.path.join(log_dir, "run1.log"))
    open_windows(m, 2)
    print("run 1: %d windows" % window_count(m), flush=True)
    # Session store collects window data, and so calls GetWorkspaceID(), every second.
    time.sleep(5)
    print("run 1: %d windows shown" % mapped_toplevels(os.path.join(log_dir, "run1.log")), flush=True)
    print("run 1: exit status %s" % quit(m, proc), flush=True)

    write_prefs(profile, 0 if scenario == "menu-restore" else 3)
    proc, m = start(firefox, profile, os.path.join(log_dir, "run2.log"))
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
            print("run 2: %d windows, still running after the restore" % window_count(m), flush=True)
    except (ConnectionError, OSError):
        pass
    if proc.poll() is not None or not alive_after(proc, 0.1):
        print("run 2: FIREFOX EXITED with status %s" % proc.wait(), flush=True)
        return 1
    print("run 2: %d windows shown" % mapped_toplevels(os.path.join(log_dir, "run2.log")), flush=True)
    print("run 2: exit status %s" % quit(m, proc), flush=True)

    # A third run restores the session saved by the second one, which shows that the IDs it saved are
    # the ones the compositor knows.
    write_prefs(profile, 3)
    proc, m = start(firefox, profile, os.path.join(log_dir, "run3.log"))
    time.sleep(3)
    if not alive_after(proc, 5):
        print("run 3: FIREFOX EXITED with status %s" % proc.returncode, flush=True)
        return 1
    print("run 3: %d windows" % window_count(m), flush=True)
    print("run 3: %d windows shown" % mapped_toplevels(os.path.join(log_dir, "run3.log")), flush=True)
    print("run 3: exit status %s" % quit(m, proc), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
