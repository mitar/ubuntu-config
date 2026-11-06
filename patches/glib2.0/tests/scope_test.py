#!/usr/bin/env python3
#
# Launches a test application with GDesktopAppInfo and checks which cgroup each of its processes is in. See README.
#
# Usage: scope_test.py SCENARIO OUT_DIR
#
# SCENARIO is one of:
#
#   loop           the launching process runs its main loop after launching, as GNOME Shell does
#   exit           the launching process exits right after launching, as gio open does
#   late-request   the launching process asks for the scope after GLib, as gnome_start_systemd_scope() does
#   refused        run on a session bus without systemd, which refuses to create the scope
#   refused-exit   run on a session bus without systemd, and the launching process exits right after launching, so
#                  that nothing tells gio-launch-desktop about the refusal and it waits until its time limit
#   outside        run outside of the systemd user instance, where no scope is asked for
#
# The scenarios in which the launching process exits are checked by a second run, with SCENARIO prefixed by check-.
#
# OUT_DIR/SCENARIO receives the application's desktop file, what the application and the launching process report, and
# result.json. The exit status is 0 when every check of the scenario passed.

import json
import os
import pathlib
import sys
import time

SCENARIOS = ("loop", "exit", "late-request", "refused", "refused-exit", "outside")
EXITING = ("exit", "refused-exit")
APP_ID = "glib-scope-test"
# The application ID escaped as systemd and gnome-desktop escape it for a unit name.
ESCAPED_APP_ID = "glib\\x2dscope\\x2dtest"
# How long gio-launch-desktop waits for a scope at most. The application has to start well before that whenever
# gio-launch-desktop is not supposed to wait for it.
SCOPE_TIMEOUT_S = 5
WAIT_S = 20

scenario = sys.argv[1]
checking = scenario.startswith("check-")
out_dir = pathlib.Path(sys.argv[2]) / scenario.removeprefix("check-")


def write_desktop_file():
    applications = out_dir / "data" / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    # The application starts two processes the moment it runs, as Chromium starts its zygotes, and only then reports
    # them and its environment.
    command = (
        "sleep 600 & a=$!; sleep 600 & b=$!; "
        f'env > {out_dir}/app-env; '
        f'echo "$$ $a $b $(date +%%s.%%N)" > {out_dir}/app-pids.tmp; mv {out_dir}/app-pids.tmp {out_dir}/app-pids; '
        "exec sleep 600"
    )
    (applications / f"{APP_ID}.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=GLib scope test\n"
        f"Exec=sh -c '{command}'\n"
    )


def cgroup_of(pid):
    try:
        lines = pathlib.Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    except FileNotFoundError:
        return None
    return next((line[3:] for line in lines if line.startswith("0::")), None)


def wait_for_app():
    path = out_dir / "app-pids"
    deadline = time.monotonic() + WAIT_S
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


def check_app(checks, launched, own_cgroup, expect_scope, waited=False):
    app = wait_for_app()
    checks.check("application started", app is not None, f"within {WAIT_S} s")
    if app is None:
        return {}
    pids, started = app
    delay = started - launched["time"]
    cgroups = {pid: cgroup_of(pid) for pid in pids}
    unit = launched["platform_data"].get("systemd-unit")

    if expect_scope:
        expected = f"app-gnome-{ESCAPED_APP_ID}-{launched['platform_data']['pid']}.scope"
        checks.check("scope named in the launched signal", unit == expected, f"{unit!r}, expected {expected!r}")
        in_scope = all(cg is not None and cg.endswith("/" + expected) for cg in cgroups.values())
        checks.check("every process in the scope", in_scope, json.dumps(cgroups))
        checks.check("started without the time limit", delay < SCOPE_TIMEOUT_S - 1, f"{delay * 1000:.0f} ms after launching")
    else:
        stayed = all(cg == own_cgroup for cg in cgroups.values())
        checks.check("every process in the launching process's cgroup", stayed, f"{json.dumps(cgroups)}, launcher in {own_cgroup}")
        if waited:
            in_time = SCOPE_TIMEOUT_S - 0.5 < delay < SCOPE_TIMEOUT_S + 5
            checks.check("started at the time limit", in_time, f"{delay * 1000:.0f} ms after launching")
        else:
            checks.check("started without the time limit", delay < SCOPE_TIMEOUT_S - 1, f"{delay * 1000:.0f} ms after launching")

    env = (out_dir / "app-env").read_text()
    hidden = "GIO_LAUNCH_DESKTOP_SCOPE" not in env
    checks.check("variables of gio-launch-desktop hidden from the application", hidden, "GIO_LAUNCH_DESKTOP_SCOPE* not in its environment")

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
        else:
            checks.check("scope asked for", "systemd-unit" in launched["platform_data"], json.dumps(launched["platform_data"]))
            extra = check_app(checks, launched, launched["cgroup"], False, waited=True)
        return checks.finish(extra)

    if scenario not in SCENARIOS:
        print(f"unknown scenario {scenario}", file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    write_desktop_file()
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
    info = Gio.DesktopAppInfo.new(f"{APP_ID}.desktop")
    context = Gio.AppLaunchContext()
    context.connect("launched", launched_cb)
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

    if scenario == "outside":
        checks.check("no scope asked for", "systemd-unit" not in launched["platform_data"], json.dumps(launched["platform_data"]))
        extra = check_app(checks, launched, own_cgroup, False)
    elif scenario == "refused":
        checks.check("scope asked for", "systemd-unit" in launched["platform_data"], json.dumps(launched["platform_data"]))
        extra = check_app(checks, launched, own_cgroup, False)
    else:
        extra = check_app(checks, launched, own_cgroup, True)
        if scenario == "late-request":
            error = late_reply.get("error", "no reply")
            checks.check("late request refused", error == "org.freedesktop.systemd1.UnitExists", repr(error))

    return checks.finish({"launcher_cgroup": own_cgroup, "platform_data": launched["platform_data"], **extra})


sys.exit(main())
