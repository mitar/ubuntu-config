"""Stand in for gnome-session's save/restore interface on a private D-Bus session bus. See README.

Usage: fake_session_manager.py RESTORE_SUPPORTED LOG_FILE

Owns org.gnome.SessionManager and offers the RestoreSupported property, true or false as RESTORE_SUPPORTED says,
and the RegisterRestore, UnregisterRestore and DeletedInstanceIds methods. Every method call is appended to
LOG_FILE as one line with the method name and its arguments. RegisterRestore answers with the restore reason
launch and the instance ID "test-instance", as gnome-session does for a first start. Runs until killed.
"""

import sys

from gi.repository import Gio, GLib

INTERFACE = """
<node>
  <interface name="org.gnome.SessionManager">
    <method name="RegisterRestore">
      <arg type="s" name="app_id" direction="in"/>
      <arg type="s" name="unused" direction="in"/>
      <arg type="u" name="reason" direction="out"/>
      <arg type="s" name="instance_id" direction="out"/>
      <arg type="as" name="discarded_ids" direction="out"/>
    </method>
    <method name="UnregisterRestore">
      <arg type="s" name="app_id" direction="in"/>
      <arg type="s" name="instance_id" direction="in"/>
    </method>
    <method name="DeletedInstanceIds">
      <arg type="s" name="app_id" direction="in"/>
      <arg type="as" name="ids" direction="in"/>
    </method>
    <property name="RestoreSupported" type="b" access="read"/>
  </interface>
</node>
"""

# GtkRestoreReason and gnome-session's restore reason share their values, and 1 is launch.
REASON_LAUNCH = 1


def main():
    restore_supported = sys.argv[1] == "true"
    log_path = sys.argv[2]
    loop = GLib.MainLoop()
    node = Gio.DBusNodeInfo.new_for_xml(INTERFACE)

    def method_call(connection, sender, path, interface, method, parameters, invocation):
        with open(log_path, "a") as log:
            log.write("%s %s\n" % (method, parameters.print_(False)))
        if method == "RegisterRestore":
            invocation.return_value(GLib.Variant("(usas)", (REASON_LAUNCH, "test-instance", [])))
        else:
            invocation.return_value(None)

    def get_property(connection, sender, path, interface, name):
        return GLib.Variant("b", restore_supported)

    def bus_acquired(connection, name):
        connection.register_object("/org/gnome/SessionManager", node.interfaces[0], method_call, get_property, None)

    Gio.bus_own_name(Gio.BusType.SESSION, "org.gnome.SessionManager", Gio.BusNameOwnerFlags.NONE,
                     bus_acquired, None, lambda *args: loop.quit())
    loop.run()


if __name__ == "__main__":
    main()
