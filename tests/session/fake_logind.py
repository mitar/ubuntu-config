#!/usr/bin/env python3
"""Owns org.freedesktop.login1 on the system bus of the container of run-session-tests.sh, with as much of logind as
the session tests need. See README.

Usage: fake_logind.py STATE_FILE

Runs as root. Hands out inhibitors as logind does, each a file descriptor which holds the inhibitor until its holder
closes it, and announces a shutdown with PrepareForShutdown when a test asks for it through the interface
org.freedesktop.login1.SessionTest, which logind does not have. Writes the inhibitors it handed out and the shutdowns
it announced, with their times, to STATE_FILE as JSON after every change.
"""

import json
import os
import sys
import time

from gi.repository import Gio, GLib

NODE = Gio.DBusNodeInfo.new_for_xml(
    """
    <node>
      <interface name="org.freedesktop.login1.Manager">
        <method name="Inhibit">
          <arg type="s" name="what" direction="in"/>
          <arg type="s" name="who" direction="in"/>
          <arg type="s" name="why" direction="in"/>
          <arg type="s" name="mode" direction="in"/>
          <arg type="h" name="fd" direction="out"/>
        </method>
        <method name="ListInhibitors">
          <arg type="a(ssssuu)" direction="out"/>
        </method>
        <signal name="PrepareForShutdown">
          <arg type="b" name="start"/>
        </signal>
      </interface>
      <interface name="org.freedesktop.login1.SessionTest">
        <method name="PrepareForShutdown">
          <arg type="b" name="start" direction="in"/>
        </method>
      </interface>
    </node>
    """
)

state_file = sys.argv[1]
state = {"inhibitors": [], "announced": []}


def save():
    with open(state_file + ".new", "w") as f:
        json.dump(state, f, indent=2)
    os.chmod(state_file + ".new", 0o644)
    os.replace(state_file + ".new", state_file)


def unix_ids(connection, sender):
    uid = connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetConnectionUnixUser",
                               GLib.Variant("(s)", (sender,)), None, Gio.DBusCallFlags.NONE, -1, None).unpack()[0]
    pid = connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetConnectionUnixProcessID",
                               GLib.Variant("(s)", (sender,)), None, Gio.DBusCallFlags.NONE, -1, None).unpack()[0]
    return uid, pid


def inhibit(connection, sender, parameters, invocation):
    what, who, why, mode = parameters.unpack()
    uid, pid = unix_ids(connection, sender)
    read_end, write_end = os.pipe()
    inhibitor = {"what": what, "who": who, "why": why, "mode": mode, "uid": uid, "pid": pid, "taken": time.time(), "released": None}
    state["inhibitors"].append(inhibitor)
    save()

    # The inhibitor holds until every copy of the descriptor handed out is closed, which ends the pipe.
    def released(fd, _condition):
        inhibitor["released"] = time.time()
        os.close(fd)
        save()
        return GLib.SOURCE_REMOVE

    GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, read_end, GLib.IOCondition.HUP | GLib.IOCondition.IN | GLib.IOCondition.ERR, released)
    fds = Gio.UnixFDList.new_from_array([write_end])
    invocation.return_value_with_unix_fd_list(GLib.Variant("(h)", (0,)), fds)


def method_call(connection, sender, _path, interface, method, parameters, invocation):
    if interface == "org.freedesktop.login1.Manager" and method == "Inhibit":
        inhibit(connection, sender, parameters, invocation)
    elif interface == "org.freedesktop.login1.Manager" and method == "ListInhibitors":
        held = [(i["what"], i["who"], i["why"], i["mode"], i["uid"], i["pid"]) for i in state["inhibitors"] if i["released"] is None]
        invocation.return_value(GLib.Variant("(a(ssssuu))", (held,)))
    elif interface == "org.freedesktop.login1.SessionTest" and method == "PrepareForShutdown":
        (start,) = parameters.unpack()
        state["announced"].append({"start": start, "time": time.time()})
        save()
        connection.emit_signal(None, "/org/freedesktop/login1", "org.freedesktop.login1.Manager", "PrepareForShutdown",
                               GLib.Variant("(b)", (start,)))
        invocation.return_value(None)
    else:
        invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", f"{interface}.{method} is not faked")


def bus_acquired(connection, _name):
    for interface in NODE.interfaces:
        connection.register_object("/org/freedesktop/login1", interface, method_call, None, None)


save()
loop = GLib.MainLoop()
Gio.bus_own_name(Gio.BusType.SYSTEM, "org.freedesktop.login1", Gio.BusNameOwnerFlags.NONE, bus_acquired, None, lambda *_: loop.quit())
loop.run()
