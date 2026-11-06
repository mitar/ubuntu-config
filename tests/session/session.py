"""Drives a GNOME session in the container of run-session-tests.sh. See README.

The session is the real one: gnome-session --session=ubuntu as GDM starts it, which has the user's service manager
start gnome-session and GNOME Shell, headless. The tests query and drive GNOME Shell through the methods of
session-test@mitar.tnode.com, called over GNOME Shell's Eval D-Bus method.
"""

import json
import os
import pathlib
import re
import subprocess
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

REPO_DIR = pathlib.Path(__file__).resolve().parents[2]
TEST_EXTENSION = "session-test@mitar.tnode.com"

# The environment GDM sets for an Ubuntu session on Wayland, which gnome-session passes on to the services it starts.
SESSION_ENV = {
    "XDG_SESSION_TYPE": "wayland",
    "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
    "XDG_SESSION_DESKTOP": "ubuntu",
    "DESKTOP_SESSION": "ubuntu",
    "GNOME_SHELL_SESSION_MODE": "ubuntu",
}


class Checks:
    """Collects the checks of a test, prints each, and writes them to result.json."""

    def __init__(self, name, out_dir):
        self.name = name
        self.out_dir = out_dir
        self.results = []

    def check(self, what, passed, detail=""):
        self.results.append({"check": what, "passed": bool(passed), "detail": detail})
        print(f"{'PASS' if passed else 'FAIL'} {self.name}: {what}: {detail}", flush=True)
        return bool(passed)

    def finish(self, **extra):
        (self.out_dir / "result.json").write_text(json.dumps({"test": self.name, "checks": self.results, **extra}, indent=2) + "\n")
        return 0 if self.results and all(r["passed"] for r in self.results) else 1


def wait_for(predicate, timeout, interval=0.1):
    """Calls predicate until it returns something true, and returns that, or None after timeout seconds."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = predicate()
        except GLib.Error:
            value = None
        if value:
            return value
        if time.monotonic() > deadline:
            return None
        time.sleep(interval)


def cgroup_of(pid):
    """Returns the cgroup of a process in the unified hierarchy, or None when it is gone."""
    try:
        lines = pathlib.Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return next((line[3:] for line in lines if line.startswith("0::")), None)


def unit_of(cgroup):
    """Returns the systemd unit a cgroup belongs to, the last service or scope in its path."""
    if cgroup is None:
        return None
    units = [part for part in cgroup.split("/") if part.endswith((".service", ".scope"))]
    return units[-1] if units else None


def processes(pattern):
    """Returns the IDs of this user's processes whose command line, with its arguments joined by spaces, matches the
    regular expression pattern."""
    pids = []
    uid = os.getuid()
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if re.search(pattern, command):
            pids.append(int(entry.name))
    return sorted(pids)


class Session:
    """A GNOME session of the user running the tests, which can be logged into and out of again."""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.bus = None
        self.leader = None
        self.logins = 0
        self.connect()

    def connect(self):
        """Connects to the session bus anew, as logging out can restart it."""
        self.bus = Gio.DBusConnection.new_for_address_sync(
            os.environ["DBUS_SESSION_BUS_ADDRESS"],
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)

    def gsettings(self, schema, key, value):
        """Sets a key, given as GVariant text, with gsettings, as configure-gnome-desktop.sh's set_key does."""
        subprocess.run(["gsettings", "set", schema, key, value], check=True)

    def apply_desktop_settings(self):
        """Applies the settings configure-gnome-desktop.sh sets with set_key, as far as their schemas are installed,
        so that the session behaves as the configured desktop does. Returns the keys which could not be set."""
        skipped = []
        script = (REPO_DIR / "configure-gnome-desktop.sh").read_text()
        for match in re.finditer(r'^\s*set_key\s+(\S+)\s+(\S+)\s+"(.*)"\s*$', script, re.MULTILINE):
            schema, key, value = match.groups()
            result = subprocess.run(["gsettings", "set", schema, key, value], capture_output=True, text=True)
            if result.returncode != 0:
                skipped.append(f"{schema} {key}")
        return skipped

    def enable_extensions(self, *uuids):
        """Adds extensions to the ones the next login enables."""
        current = subprocess.run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"], capture_output=True, text=True,
                                 check=True).stdout.strip()
        enabled = [] if current.startswith("@as") else json.loads(current.replace("'", '"'))
        for uuid in uuids:
            if uuid not in enabled:
                enabled.append(uuid)
        self.gsettings("org.gnome.shell", "enabled-extensions", json.dumps(enabled).replace('"', "'"))

    def login(self, timeout=90):
        """Logs in as GDM does, by starting gnome-session's leader, and waits until GNOME Shell started up and the
        test extension answers."""
        self.logins += 1
        if self.bus.is_closed():
            self.connect()
        log = open(self.out_dir / f"leader{self.logins}.log", "w")
        self.leader = subprocess.Popen(["gnome-session", "--session=ubuntu"], env=os.environ | SESSION_ENV, stdout=log,
                                       stderr=subprocess.STDOUT)

        # GNOME Shell enables extensions only once it has started up, and Eval fails until the test extension is
        # enabled.
        def ready():
            try:
                return self.call("ready")
            except RuntimeError:
                return False

        if not wait_for(ready, timeout):
            raise RuntimeError(f"GNOME Shell did not start within {timeout} seconds, see the journal")
        # The virtual keyboard is set up before a test types, which loses the first keys otherwise.
        self.press("Shift_L")

    def inhibitors(self):
        """Returns the application, the reason and the flags of each of gnome-session's inhibitors."""
        result = []
        reply = self.bus.call_sync("org.gnome.SessionManager", "/org/gnome/SessionManager", "org.gnome.SessionManager",
                                   "GetInhibitors", None, GLib.VariantType("(ao)"), Gio.DBusCallFlags.NONE, 10000, None)
        for path in reply.unpack()[0]:
            info = {}
            for method, key in (("GetAppId", "app"), ("GetReason", "reason"), ("GetFlags", "flags")):
                value = self.bus.call_sync("org.gnome.SessionManager", path, "org.gnome.SessionManager.Inhibitor", method, None,
                                           None, Gio.DBusCallFlags.NONE, 10000, None)
                info[key] = value.unpack()[0]
            result.append(info)
        return result

    def logout(self, timeout=60):
        """Logs out as Log Out in the session menu does, and waits until the session has ended, which the leader does
        when gnome-session has stopped every unit of the session. When applications hold the logout, GNOME Shell shows
        a dialog which lists them, as it does for the user, which is confirmed as the user would. Returns the
        contents of each such dialog, with the inhibitors gnome-session held then."""
        shown = []
        self.bus.call_sync("org.gnome.SessionManager", "/org/gnome/SessionManager", "org.gnome.SessionManager", "Logout",
                           GLib.Variant("(u)", (1,)), None, Gio.DBusCallFlags.NONE, 10000, None)
        deadline = time.monotonic() + timeout
        while self.leader.poll() is None:
            if time.monotonic() > deadline:
                raise RuntimeError(f"the session did not end within {timeout} seconds, dialogs: {shown}")
            try:
                dialogs = [d for d in self.call("dialogs") if d["type"] == "EndSessionDialog" and d["state"] == 0]
                if dialogs:
                    # The dialog fills its list of applications from the inhibitors a moment after it opens.
                    time.sleep(1)
                    shown.append({"dialogs": self.call("dialogs"), "inhibitors": self.inhibitors()})
                    self.call("confirmEndSession", "ConfirmedLogout")
            except (GLib.Error, RuntimeError):
                pass
            time.sleep(0.2)
        return shown

    def eval(self, code):
        """Runs JavaScript in GNOME Shell and returns its value, which has to be one JSON can represent."""
        reply = self.bus.call_sync("org.gnome.Shell", "/org/gnome/Shell", "org.gnome.Shell", "Eval", GLib.Variant("(s)", (code,)),
                                   GLib.VariantType("(bs)"), Gio.DBusCallFlags.NO_AUTO_START, 20000, None)
        success, result = reply.unpack()
        if not success:
            raise RuntimeError(f"GNOME Shell failed to run {code}: {result}")
        return json.loads(result) if result else None

    def call(self, method, *args):
        """Calls a method of the test extension, see session-test@mitar.tnode.com/extension.js."""
        return self.eval(f"globalThis.sessionTest.{method}({', '.join(json.dumps(arg) for arg in args)})")

    def press(self, key, *modifiers):
        """Presses a key with modifiers, given by the names of their keysyms, such as ("a", "Control_L")."""
        self.call("pressKey", key, list(modifiers))

    # The names of the keysyms of the characters which are not their own name. Mutter's virtual keyboard holds Shift
    # itself for the characters which need it.
    KEYSYMS = {" ": "space", "\n": "Return", "/": "slash", ".": "period", "-": "minus", "_": "underscore", "~": "asciitilde",
               "=": "equal", ">": "greater", "<": "less", "$": "dollar", "&": "ampersand", ";": "semicolon", ":": "colon",
               "'": "apostrophe", '"': "quotedbl", "|": "bar", "*": "asterisk", ",": "comma"}

    def type_text(self, text):
        """Types text of letters, digits, common punctuation and newlines, which press Return, into the focused
        window."""
        for char in text:
            self.press(self.KEYSYMS.get(char, char))

    def run_in_session(self, *argv):
        """Runs a command as the user would from a terminal of the session, in a unit of the user's service manager
        with the session's environment, and waits until it exits. As in a terminal, the processes the command starts
        keep running when it exits, which in a service only KillMode=process allows."""
        subprocess.run(["systemd-run", "--user", "--quiet", "--collect", "--wait", "--property=KillMode=process", "--", *argv],
                       check=True)

    def logind(self):
        """Returns the inhibitors fake_logind.py handed out, each with the times it was handed out and released, and the
        shutdowns it announced."""
        return json.loads(pathlib.Path(os.environ["SESSION_TEST_LOGIND_STATE"]).read_text())

    def prepare_for_shutdown(self, start=True):
        """Has fake_logind.py announce a shutdown, as logind does once it is confirmed, or that it does not happen."""
        Gio.bus_get_sync(Gio.BusType.SYSTEM).call_sync("org.freedesktop.login1", "/org/freedesktop/login1",
                                                      "org.freedesktop.login1.SessionTest", "PrepareForShutdown",
                                                      GLib.Variant("(b)", (start,)), None, Gio.DBusCallFlags.NONE, 10000, None)

    def failed_units(self):
        """Returns the units of the user's service manager which failed."""
        result = subprocess.run(["systemctl", "--user", "list-units", "--state=failed", "--no-legend", "--plain"], capture_output=True,
                                text=True, check=True)
        return [line.split()[0] for line in result.stdout.splitlines() if line.strip()]

    def journal(self, since):
        """Returns the messages logged since a time in seconds since the epoch, one line each, with the identifier of
        the program which logged it in front."""
        result = subprocess.run(["journalctl", "--quiet", f"--since=@{int(since)}", "--output=short"], capture_output=True, text=True,
                                check=True)
        return result.stdout.splitlines()

    def windows(self, app=None):
        """Returns GNOME Shell's normal windows, only those of the application with the given desktop file ID when app
        is given."""
        return [w for w in self.call("windows") if app is None or w["app"] == app]

    def wait_for_windows(self, app, count, timeout=30):
        """Waits until the application has at least count windows, and returns them, or None."""
        return wait_for(lambda: (lambda ws: ws if len(ws) >= count else None)(self.windows(app)), timeout)
