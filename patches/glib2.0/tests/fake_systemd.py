#!/usr/bin/env python3
#
# Owns org.freedesktop.systemd1 on the session bus, answers StartTransientUnit as MODE says, and runs COMMAND meanwhile.
# See README.
#
# Usage: fake_systemd.py MODE COMMAND [ARGUMENT...]
#
# MODE is one of:
#
#   refuse   refuses every request
#   fail     accepts every request, and then sends JobRemoved for its job with result failed
#   slow     accepts every request, and only SLOW_S seconds later moves the process into a cgroup named after the scope,
#            next to the cgroup the process is in, which the user instance has to have delegated to this user, and
#            then sends JobRemoved for its job with result done
#
# Exits with the exit status of COMMAND, which starts once the name is owned.

import pathlib
import sys

from gi.repository import Gio, GLib

# How long a slow request takes, longer than the 5 seconds gio-launch-desktop once waited at most.
SLOW_S = 7

INTERFACE = Gio.DBusNodeInfo.new_for_xml(
    """
    <node>
      <interface name="org.freedesktop.systemd1.Manager">
        <method name="StartTransientUnit">
          <arg type="s" direction="in"/>
          <arg type="s" direction="in"/>
          <arg type="a(sv)" direction="in"/>
          <arg type="a(sa(sv))" direction="in"/>
          <arg type="o" direction="out"/>
        </method>
        <signal name="JobRemoved">
          <arg type="u"/>
          <arg type="o"/>
          <arg type="s"/>
          <arg type="s"/>
        </signal>
      </interface>
    </node>
    """
).interfaces[0]

mode = sys.argv[1]
loop = GLib.MainLoop()
status = 1
jobs = 0


def pid_of(properties, message):
    for name, value in properties:
        if name == "PIDs":
            return value[0]
        if name == "PIDFDs":
            fd = message.get_unix_fd_list().get(value[0])
            for line in pathlib.Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
                if line.startswith("Pid:"):
                    return int(line.split()[1])
    raise ValueError("no process in the request")


def job_removed(connection, job_id, job, unit, result):
    connection.emit_signal(None, "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "JobRemoved",
                           GLib.Variant("(uoss)", (job_id, job, unit, result)))
    return False


def move(connection, job_id, job, unit, pid):
    cgroup = pathlib.Path("/sys/fs/cgroup" + pathlib.Path(f"/proc/{pid}/cgroup").read_text().split("::", 1)[1].strip())
    scope = cgroup.parent / unit
    scope.mkdir(exist_ok=True)
    (scope / "cgroup.procs").write_text(f"{pid}\n")
    return job_removed(connection, job_id, job, unit, "done")


def method_call(connection, _sender, _path, _interface, _method, parameters, invocation):
    global jobs
    unit, _mode, properties, _aux = parameters.unpack()
    if mode == "refuse":
        invocation.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied", f"Refused {unit} for the test")
        return
    pid = pid_of(properties, invocation.get_message())
    jobs += 1
    job = f"/org/freedesktop/systemd1/job/{jobs}"
    invocation.return_value(GLib.Variant("(o)", (job,)))
    if mode == "fail":
        GLib.idle_add(job_removed, connection, jobs, job, unit, "failed")
    else:
        GLib.timeout_add_seconds(SLOW_S, move, connection, jobs, job, unit, pid)


def command_exited(_pid, wait_status):
    global status
    try:
        GLib.spawn_check_wait_status(wait_status)
        status = 0
    except GLib.Error:
        status = 1
    loop.quit()


def name_acquired(connection, _name):
    pid, *_ = GLib.spawn_async(sys.argv[2:], flags=GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.DO_NOT_REAP_CHILD)
    GLib.child_watch_add(GLib.PRIORITY_DEFAULT, pid, command_exited)


def bus_acquired(connection, _name):
    connection.register_object("/org/freedesktop/systemd1", INTERFACE, method_call, None, None)


Gio.bus_own_name(Gio.BusType.SESSION, "org.freedesktop.systemd1", Gio.BusNameOwnerFlags.NONE, bus_acquired, name_acquired,
                 lambda *_: loop.quit())
loop.run()
sys.exit(status)
