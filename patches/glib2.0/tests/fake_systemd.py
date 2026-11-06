#!/usr/bin/env python3
#
# Owns org.freedesktop.systemd1 on the session bus, refuses every StartTransientUnit, and runs COMMAND meanwhile. See
# README.
#
# Usage: fake_systemd.py COMMAND [ARGUMENT...]
#
# Exits with the exit status of COMMAND, which starts once the name is owned.

import sys

from gi.repository import Gio, GLib

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
      </interface>
    </node>
    """
).interfaces[0]

loop = GLib.MainLoop()
status = 1


def method_call(_connection, _sender, _path, _interface, _method, parameters, invocation):
    invocation.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied", f"Refused {parameters[0]} for the test")


def command_exited(_pid, wait_status):
    global status
    try:
        GLib.spawn_check_wait_status(wait_status)
        status = 0
    except GLib.Error:
        status = 1
    loop.quit()


def name_acquired(connection, _name):
    pid, *_ = GLib.spawn_async(sys.argv[1:], flags=GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.DO_NOT_REAP_CHILD)
    GLib.child_watch_add(GLib.PRIORITY_DEFAULT, pid, command_exited)


def bus_acquired(connection, _name):
    connection.register_object("/org/freedesktop/systemd1", INTERFACE, method_call, None, None)


Gio.bus_own_name(Gio.BusType.SESSION, "org.freedesktop.systemd1", Gio.BusNameOwnerFlags.NONE, bus_acquired, name_acquired,
                 lambda *_: loop.quit())
loop.run()
sys.exit(status)
