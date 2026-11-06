#!/usr/bin/env python3
#
# Launches a test application with GDesktopAppInfo and checks which cgroup each of its processes is in. See README.
#
# Usage: scope_test.py SCENARIO OUT_DIR
#
# SCENARIO is one of:
#
#   loop              the launching process runs its main loop after launching, as GNOME Shell does
#   pid-namespace     the same, from a PID namespace of its own
#   command-line      the same, but the application is a command line, launched as GNOME Shell runs a command with
#                     Alt+F2, see patches/gnome-shell/spawn-commands-in-scope.patch
#   exit              the launching process exits right after launching, as gio open does
#   late-request      the launching process asks for the scope after GLib, as gnome_start_systemd_scope() does
#   no-systemd        run on a session bus without systemd, where no scope is asked for
#   no-systemd-exit   the same, and the launching process exits right after launching
#   refused           run on a session bus where fake_systemd.py refuses to create the scope
#   refused-exit      the same, and the launching process exits right after launching, so that nothing tells
#                     gio-launch-desktop about the refusal and it waits until its time limit
#   job-failed        run on a session bus where fake_systemd.py accepts the request and then fails its job
#   slow              run on a session bus where fake_systemd.py moves the process into the scope only after SLOW_S
#   outside           run outside of the systemd user instance, where no scope is asked for
#
# The scenarios in which the launching process exits are checked by a second run, with SCENARIO prefixed by check-.
#
# OUT_DIR/SCENARIO receives the application's desktop file, what the application and the launching process report, and
# result.json. The exit status is 0 when every check of the scenario passed. The launching process's standard error is
# connected to the journal of fake_journald.py, whose records in OUT_DIR/journal show what gio-launch-desktop logged.

import json
import os
import pathlib
import re
import socket
import sys
import time

SCENARIOS = ("loop", "pid-namespace", "command-line", "exit", "late-request", "no-systemd", "no-systemd-exit", "refused", "refused-exit",
             "job-failed", "slow", "outside")
EXITING = ("exit", "no-systemd-exit", "refused-exit")
APP_ID = "glib-scope-test"
# The application ID escaped as systemd and gnome-desktop escape it for a unit name.
ESCAPED_APP_ID = "glib\\x2dscope\\x2dtest"
# How long gio-launch-desktop waits for a scope at most. The application has to start well before that whenever
# gio-launch-desktop is not supposed to wait for it.
SCOPE_TIMEOUT_S = 60
# How long fake_systemd.py takes for a slow request.
SLOW_S = 7
WAIT_S = 20
JOURNAL_STREAM = "/run/systemd/journal/stdout"
# syslog's priority of a message at error level, LOG_ERR, from a user process, LOG_USER.
SYSLOG_ERR = "<11>"

scenario = sys.argv[1]
checking = scenario.startswith("check-")
out_dir = pathlib.Path(sys.argv[2]) / scenario.removeprefix("check-")


def app_script():
    # The application starts two processes the moment it runs, as Chromium starts its zygotes, and only then reports
    # them and its environment.
    return (
        "sleep 600 & a=$!; sleep 600 & b=$!; "
        f'env > {out_dir}/app-env; '
        f'echo "$$ $a $b $(date +%s.%N)" > {out_dir}/app-pids.tmp; mv {out_dir}/app-pids.tmp {out_dir}/app-pids; '
        "exec sleep 600"
    )


def write_desktop_file():
    applications = out_dir / "data" / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    # % starts a field code in the Exec key.
    command = app_script().replace("%", "%%")
    (applications / f"{APP_ID}.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=GLib scope test\n"
        f"Exec=sh -c '{command}'\n"
    )


def command_line(argv, shell_quote):
    """Returns argv as the command line of a GAppInfo, as shell_util_spawn_command_async() of
    patches/gnome-shell/spawn-commands-in-scope.patch builds it."""
    words = [arg if re.fullmatch(r"[A-Za-z0-9/._\-+=:,@]+", arg) else shell_quote(arg) for arg in argv]
    return " ".join(words).replace("%", "%%")


def cgroup_of(pid):
    try:
        lines = pathlib.Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    except FileNotFoundError:
        return None
    return next((line[3:] for line in lines if line.startswith("0::")), None)


def wait_for_app(wait_s):
    path = out_dir / "app-pids"
    deadline = time.monotonic() + wait_s
    while not path.exists():
        if time.monotonic() > deadline:
            return None
        time.sleep(0.01)
    *pids, started = path.read_text().split()
    return [int(pid) for pid in pids], float(started)


class Checks:
    def __init__(self):
        self.results = []

    def check(self, name, passed, detail):
        self.results.append({"check": name, "passed": bool(passed), "detail": detail})
        print(f"{'PASS' if passed else 'FAIL'} {scenario}: {name}: {detail}", flush=True)

    def finish(self, extra):
        result = {"scenario": scenario, "checks": self.results, **extra}
        (out_dir / ("result-check.json" if checking else "result.json")).write_text(
            json.dumps(result, indent=2) + "\n"
        )
        return 0 if all(r["passed"] for r in self.results) else 1


def connect_stderr_to_journal():
    # gio-launch-desktop inherits standard error from here, and logs at error priority through syslog only when it is
    # the journal.
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(JOURNAL_STREAM)
    os.dup2(sock.fileno(), 2)
    sock.close()


def check_logged(checks, pid, expected):
    """Checks that gio-launch-desktop, with pid, logged one message at error priority which contains expected, or
    nothing when expected is None."""
    path = pathlib.Path(sys.argv[2]) / "journal" / "syslog.log"
    tag = f"gio-launch-desktop[{pid}]: "
    # The message is sent before the application runs, so it has arrived by now, but fake_journald.py might not have
    # written it yet.
    deadline = time.monotonic() + (5 if expected else 0.5)
    while True:
        lines = [line for line in (path.read_text(errors="replace").splitlines() if path.exists() else []) if tag in line]
        if (expected and lines) or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    if expected is None:
        checks.check("nothing logged", not lines, json.dumps(lines))
    else:
        logged = len(lines) == 1 and lines[0].startswith(SYSLOG_ERR) and expected in lines[0]
        checks.check("logged at error priority", logged, f"{json.dumps(lines)}, expected {expected!r} at {SYSLOG_ERR}")


def check_app(checks, launched, own_cgroup, expect_scope, waited=False, logged=None, slow=False, name=ESCAPED_APP_ID):
    wait_s = SCOPE_TIMEOUT_S + WAIT_S if waited else WAIT_S
    app = wait_for_app(wait_s)
    checks.check("application started", app is not None, f"within {wait_s} s")
    if app is None:
        return {}
    pids, started = app
    delay = started - launched["time"]
    cgroups = {pid: cgroup_of(pid) for pid in pids}
    unit = launched["platform_data"].get("systemd-unit")

    if expect_scope:
        expected = f"app-gnome-{name}-{launched['platform_data']['pid']}.scope"
        checks.check("scope named in the launched signal", unit == expected, f"{unit!r}, expected {expected!r}")
        in_scope = all(cg is not None and cg.endswith("/" + expected) for cg in cgroups.values())
        checks.check("every process in the scope", in_scope, json.dumps(cgroups))
        if slow:
            checks.check("started once the slow request was done", delay > SLOW_S - 0.5, f"{delay * 1000:.0f} ms after launching")
        checks.check("started without the time limit", delay < SCOPE_TIMEOUT_S - 1, f"{delay * 1000:.0f} ms after launching")
    else:
        stayed = all(cg == own_cgroup for cg in cgroups.values())
        checks.check("every process in the launching process's cgroup", stayed, f"{json.dumps(cgroups)}, launcher in {own_cgroup}")
        if waited:
            in_time = SCOPE_TIMEOUT_S - 0.5 < delay < SCOPE_TIMEOUT_S + 10
            checks.check("started at the time limit", in_time, f"{delay * 1000:.0f} ms after launching")
        else:
            checks.check("started without the time limit", delay < SCOPE_TIMEOUT_S - 1, f"{delay * 1000:.0f} ms after launching")

    env = (out_dir / "app-env").read_text()
    hidden = "GIO_LAUNCH_DESKTOP_SCOPE" not in env
    checks.check("variables of gio-launch-desktop hidden from the application", hidden, "GIO_LAUNCH_DESKTOP_SCOPE* not in its environment")
    check_logged(checks, launched["platform_data"]["pid"], logged)

    for pid in pids:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    return {"cgroups": cgroups, "delay_ms": round(delay * 1000)}


def main():
    checks = Checks()

    if checking:
        launched = json.loads((out_dir / "launched.json").read_text())
        if scenario == "check-exit":
            extra = check_app(checks, launched, None, True)
        elif scenario == "check-no-systemd-exit":
            checks.check("no scope asked for", "systemd-unit" not in launched["platform_data"], json.dumps(launched["platform_data"]))
            extra = check_app(checks, launched, launched["cgroup"], False)
        else:
            unit = launched["platform_data"].get("systemd-unit")
            checks.check("scope asked for", unit is not None, json.dumps(launched["platform_data"]))
            extra = check_app(checks, launched, launched["cgroup"], False, waited=True,
                              logged=f"Scope {unit} did not exist after {SCOPE_TIMEOUT_S} seconds")
        return checks.finish(extra)

    if scenario not in SCENARIOS:
        print(f"unknown scenario {scenario}", file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    write_desktop_file()
    connect_stderr_to_journal()
    # GLib reads the variable once it first looks for applications, which is below.
    os.environ["XDG_DATA_HOME"] = str(out_dir / "data")

    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    own_cgroup = cgroup_of(os.getpid())
    late_reply = {}

    def launched_cb(_context, _info, platform_data):
        data = platform_data.unpack()
        if scenario == "late-request":
            # What gnome-desktop's gnome_start_systemd_scope() asks for, after GLib, on the same connection.
            unit = f"app-gnome-{ESCAPED_APP_ID}-{data['pid']}.scope"
            bus = Gio.bus_get_sync(Gio.BusType.SESSION)

            def done(bus, result):
                try:
                    bus.call_finish(result)
                    late_reply["error"] = None
                except GLib.Error as error:
                    late_reply["error"] = Gio.DBusError.get_remote_error(error)

            bus.call(
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "StartTransientUnit",
                GLib.Variant("(ssa(sv)a(sa(sv)))", (unit, "fail", [("PIDs", GLib.Variant("au", [data["pid"]]))], [])),
                GLib.VariantType("(o)"),
                Gio.DBusCallFlags.NO_AUTO_START,
                -1,
                None,
                done,
            )
        launched["platform_data"] = data

    launched = {"time": time.time()}
    context = Gio.AppLaunchContext()
    context.connect("launched", launched_cb)
    if scenario == "command-line":
        # The child is not reaped by GLib, which the Shell does itself.
        info = Gio.AppInfo.create_from_commandline(command_line(["/bin/sh", "-c", app_script()], GLib.shell_quote), None,
                                                   Gio.AppInfoCreateFlags.NONE)
        info.launch_uris_as_manager([], context, GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.DO_NOT_REAP_CHILD,
                                    None, None, None, None)
    else:
        info = Gio.DesktopAppInfo.new(f"{APP_ID}.desktop")
        info.launch([], context)
    launched["cgroup"] = own_cgroup
    (out_dir / "launched.json").write_text(json.dumps(launched) + "\n")

    if scenario in EXITING:
        # The check runs in a process of its own, after this one exited.
        return 0

    # The reply to GLib's request, and to the late one, arrive through the main loop.
    loop = GLib.MainLoop()
    deadline = time.monotonic() + WAIT_S

    def poll():
        done = (out_dir / "app-pids").exists() and (scenario != "late-request" or late_reply)
        if done or time.monotonic() > deadline:
            loop.quit()
            return False
        return True

    GLib.timeout_add(10, poll)
    loop.run()

    if scenario in ("outside", "no-systemd"):
        checks.check("no scope asked for", "systemd-unit" not in launched["platform_data"], json.dumps(launched["platform_data"]))
        extra = check_app(checks, launched, own_cgroup, False)
    elif scenario == "refused":
        unit = launched["platform_data"].get("systemd-unit")
        checks.check("scope asked for", unit is not None, json.dumps(launched["platform_data"]))
        extra = check_app(checks, launched, own_cgroup, False, logged=f"systemd did not create scope {unit}")
    elif scenario == "job-failed":
        unit = launched["platform_data"].get("systemd-unit")
        checks.check("scope asked for", unit is not None, json.dumps(launched["platform_data"]))
        extra = check_app(checks, launched, own_cgroup, False, logged="the job starting the scope ended with result failed")
    else:
        extra = check_app(checks, launched, own_cgroup, True, slow=scenario == "slow",
                          name="sh" if scenario == "command-line" else ESCAPED_APP_ID)
        if scenario == "late-request":
            error = late_reply.get("error", "no reply")
            checks.check("late request refused", error == "org.freedesktop.systemd1.UnitExists", repr(error))

    return checks.finish({"launcher_cgroup": own_cgroup, "platform_data": launched["platform_data"], **extra})


sys.exit(main())
