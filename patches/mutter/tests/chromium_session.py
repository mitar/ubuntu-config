"""Drive Chromium through chromedriver to test Wayland session restore over mutter's draft protocol. See README.

Usage: chromium_session.py CHROMEDRIVER CHROMIUM PROFILE_DIR LOG_DIR

Chromium runs three times with the same profile: the first run opens three windows and quits, the second and the
third restore the last session. Each run writes the output of chromedriver and Chromium, with Chromium's Wayland
requests and events, to LOG_DIR/runN.log, and a summary of the session requests is printed for each. The exit
status is 0 when every run went through without a protocol error and the second and third runs each restored
three windows through the protocol.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Each of these is matched in the WAYLAND_DEBUG output of Chromium, where "->" marks a request it sends.
PATTERNS = {
    "get_session": re.compile(r'-> xx_session_manager_v1#\d+\.get_session\(new id xx_session_v1#\d+, (\d+), ("[^"]*"|nil)\)'),
    "created": re.compile(r'xx_session_v1#\d+\.created\("([^"]*)"\)'),
    "restored_session": re.compile(r'xx_session_v1#\d+\.restored\(\)'),
    "add_toplevel": re.compile(r'-> xx_session_v1#\d+\.add_toplevel\(new id xx_toplevel_session_v1#\d+, xdg_toplevel#\d+, "([^"]*)"\)'),
    "restore_toplevel": re.compile(r'-> xx_session_v1#\d+\.restore_toplevel\(new id xx_toplevel_session_v1#\d+, xdg_toplevel#\d+, "([^"]*)"\)'),
    "restored_toplevel": re.compile(r'xx_toplevel_session_v1#\d+\.restored\(xdg_toplevel#\d+\)'),
    "remove": re.compile(r'-> xx_toplevel_session_v1#\d+\.remove\(\)'),
    "error": re.compile(r'wl_display#1\.error\(|[Pp]rotocol error'),
}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ChromeDriver:
    """A chromedriver of its own, spoken to over the W3C WebDriver HTTP protocol."""

    def __init__(self, chromedriver, log_path):
        self.port = free_port()
        self.log = open(log_path, "w")
        # --enable-chrome-logs passes Chromium's own output, and with it WAYLAND_DEBUG, on to this log.
        self.proc = subprocess.Popen([chromedriver, "--port=%d" % self.port, "--enable-chrome-logs"],
                                     stdout=self.log, stderr=subprocess.STDOUT)
        self.session = None
        deadline = time.time() + 30
        while True:
            try:
                if self.request("GET", "/status")["ready"]:
                    break
            except (urllib.error.URLError, ConnectionError):
                pass
            if time.time() > deadline:
                raise RuntimeError("chromedriver did not start")
            time.sleep(0.2)

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())["value"]

    def start(self, chromium, profile, extra_args):
        args = ["--ozone-platform=wayland", "--enable-features=WaylandSessionManagement", "--disable-gpu",
                "--user-data-dir=" + profile] + extra_args
        caps = {"capabilities": {"alwaysMatch": {"browserName": "chrome", "goog:chromeOptions": {
            "binary": chromium, "args": args,
            # Continue where you left off, so that the last session is kept to be restored.
            "prefs": {"session.restore_on_startup": 1},
        }}}}
        self.session = self.request("POST", "/session", caps)["sessionId"]

    def new_window(self):
        self.request("POST", "/session/%s/window/new" % self.session, {"type": "window"})

    def handle_count(self):
        return len(self.request("GET", "/session/%s/window/handles" % self.session))

    def quit(self):
        try:
            if self.session:
                self.request("DELETE", "/session/%s" % self.session)
        finally:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            self.log.close()


def summarize(log_path):
    with open(log_path, errors="replace") as f:
        text = f.read()
    found = {key: pattern.findall(text) for key, pattern in PATTERNS.items()}
    return found


def run(n, chromedriver, chromium, profile, log_dir, extra_args, new_windows):
    log_path = os.path.join(log_dir, "run%d.log" % n)
    driver = ChromeDriver(chromedriver, log_path)
    try:
        driver.start(chromium, profile, extra_args)
        for _ in range(new_windows):
            driver.new_window()
        # Chromium saves its session and adds its windows to the compositor's session in the background.
        time.sleep(5)
        tabs = driver.handle_count()
    finally:
        driver.quit()
    found = summarize(log_path)
    # Mutter writes its stored sessions a few seconds after the last change, here the windows closing at quit.
    time.sleep(5)
    gvdb = os.path.join(os.environ.get("XDG_DATA_HOME", ""), "mutter", "session.gvdb")
    found["gvdb_size"] = os.path.getsize(gvdb) if os.path.exists(gvdb) else 0
    reasons = ", ".join("%s %s" % (reason, sid) for reason, sid in found["get_session"]) or "none"
    print("run %d: %d tabs, get_session (reason, id): %s" % (n, tabs, reasons), flush=True)
    print("run %d: stored sessions %d bytes" % (n, found["gvdb_size"]), flush=True)
    print("run %d: created %s, session restored %d" % (n, found["created"] or "none", len(found["restored_session"])),
          flush=True)
    print("run %d: add_toplevel %s" % (n, found["add_toplevel"] or "none"), flush=True)
    print("run %d: restore_toplevel %s, restored %d, removed %d" % (
        n, found["restore_toplevel"] or "none", len(found["restored_toplevel"]), len(found["remove"])), flush=True)
    if found["error"]:
        print("run %d: PROTOCOL ERROR, see %s" % (n, log_path), flush=True)
    return found


def main():
    chromedriver, chromium, profile, log_dir = sys.argv[1:5]
    shutil.rmtree(profile, ignore_errors=True)
    os.makedirs(profile)
    os.makedirs(log_dir, exist_ok=True)

    results = [run(1, chromedriver, chromium, profile, log_dir, [], 2)]
    for n in (2, 3):
        results.append(run(n, chromedriver, chromium, profile, log_dir, ["--restore-last-session"], 0))

    ok = all(not r["error"] for r in results)
    for r in results[1:]:
        ok = ok and len(r["restore_toplevel"]) >= 3 and len(r["restored_toplevel"]) >= 3
    # Each run removes the names it restored and adds as many, so the stored sessions do not grow from the second
    # run to the third.
    if results[2]["gvdb_size"] > results[1]["gvdb_size"]:
        print("stored sessions grew from run 2 to run 3", flush=True)
        ok = False
    print("RESULT: %s" % ("OK" if ok else "FAILED"), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
