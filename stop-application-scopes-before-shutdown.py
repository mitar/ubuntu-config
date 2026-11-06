#!/usr/bin/python3 -I
"""Let Chromium-based applications quit at a shutdown or a reboot before logind starts it.

Installed as /usr/local/libexec/stop-application-scopes-before-shutdown by configure-gnome-desktop.sh, and run by
stop-application-scopes-before-shutdown.service in the user's service manager.

Holds a delay inhibitor for shutdown with logind. When logind announces a shutdown or a reboot, it stops
stop-application-scopes.service, which sends SIGTERM to the browser process of each Chromium-based application and
waits for it to exit, and then releases the inhibitor. logind starts the shutdown once every delay inhibitor is
released, or after its InhibitDelayMaxSec= at the latest. When logind announces that the shutdown does not happen
after all, because starting it failed, it starts the service again, if it was running, and holds a new inhibitor.

Ordering every application scope before stop-application-scopes.service lets these applications quit by themselves
at a logout. At a shutdown, logind starts the shutdown as soon as gnome-session released its own inhibitor, which it
does right before it exits, and the helpers of these applications can then die before the user's service manager
starts to stop the graphical session.

It exits with status 1 when logind refuses an inhibitor.
"""

import os
import subprocess
import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

LOGIND = "org.freedesktop.login1"
LOGIND_PATH = "/org/freedesktop/login1"
LOGIND_MANAGER = "org.freedesktop.login1.Manager"
UNIT = "stop-application-scopes.service"
INHIBITOR_WHO = "Stop application scopes before shutdown"
INHIBITOR_WHY = "Chromium-based applications quit before their helpers are stopped"


def systemctl(*arguments):
    """Runs systemctl for the user's service manager and returns whether it succeeded."""
    return subprocess.run(["systemctl", "--user", *arguments], check=False).returncode == 0


class ShutdownDelay:
    """Holds a delay inhibitor for shutdown on the given connection to the system bus, and when logind announces a
    shutdown, stops UNIT before releasing it.

    When logind refuses a new inhibitor, it prints why to standard error, sets exit_status to 1 and quits the given
    main loop.
    """

    def __init__(self, bus, loop):
        self.bus = bus
        self.loop = loop
        self.fd = None
        self.unit_stopped = False
        self.exit_status = 0

    def inhibit(self):
        """Holds a new inhibitor. Returns whether logind granted it."""
        try:
            result, fds = self.bus.call_with_unix_fd_list_sync(
                LOGIND, LOGIND_PATH, LOGIND_MANAGER, "Inhibit",
                GLib.Variant("(ssss)", ("shutdown", INHIBITOR_WHO, INHIBITOR_WHY, "delay")),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None)
        except GLib.Error as error:
            print(f"cannot hold a shutdown inhibitor: {error.message}", file=sys.stderr, flush=True)
            self.exit_status = 1
            self.loop.quit()
            return False
        # The list keeps a descriptor of its own for each one it returns, which would hold the inhibitor too.
        self.fd = fds.steal_fds()[result.unpack()[0]]
        return True

    def on_prepare_for_shutdown(self, connection, sender, path, interface, signal, parameters):
        """Handles logind's PrepareForShutdown signal, which announces a shutdown, or that it does not happen."""
        (starting,) = parameters.unpack()
        if starting:
            if self.fd is None:
                return
            # Only a service which was running is started again when the shutdown does not happen. It is not running
            # anymore also when the end of the graphical session is already stopping it.
            self.unit_stopped = systemctl("--quiet", "is-active", UNIT)
            # Stopping the service, instead of running its command, has every browser process receive SIGTERM once:
            # gnome-session exits right after the announcement, so the graphical session ends, which stops the service
            # too, and systemd merges the later stop into the one already running, for which systemctl waits as well.
            # Chromium handles only the first SIGTERM, and a second one kills the browser process at once.
            systemctl("stop", UNIT)
            os.close(self.fd)
            self.fd = None
            print("released the shutdown inhibitor", flush=True)
        else:
            if self.unit_stopped:
                systemctl("start", UNIT)
                self.unit_stopped = False
            if self.fd is None and self.inhibit():
                print("the shutdown did not happen, holding a shutdown inhibitor again", flush=True)


def main():
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    loop = GLib.MainLoop()
    delay = ShutdownDelay(bus, loop)
    # Subscribing before the inhibitor is held means that no announcement is missed while it is held.
    bus.signal_subscribe(LOGIND, LOGIND_MANAGER, "PrepareForShutdown", LOGIND_PATH, None, Gio.DBusSignalFlags.NONE,
                         delay.on_prepare_for_shutdown)
    if not delay.inhibit():
        return delay.exit_status
    loop.run()
    return delay.exit_status


if __name__ == "__main__":
    sys.exit(main())
