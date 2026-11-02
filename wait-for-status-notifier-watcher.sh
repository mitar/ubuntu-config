#!/bin/sh
#
# Installed as /usr/local/libexec/wait-for-status-notifier-watcher by configure-gnome-desktop.sh, and run by the
# entries in ~/.config/autostart which the script installs for applications built on Electron.
#
# Replaces itself with the command given as its arguments once org.kde.StatusNotifierWatcher has an owner on the
# session bus, or after WAIT_S seconds without one.
#
# Chromium, and every Electron application built on it, checks whether that name has an owner only when the
# application creates its tray icon. Without one, it uses a GTK status icon instead, which is not shown under Wayland,
# and does not check again. On Ubuntu the name is owned by the Ubuntu AppIndicators extension, which GNOME Shell
# enables only after every extension in the user's own directory, while gnome-session starts the applications at
# login as soon as the Shell reports it is ready, which it does before it has enabled its extensions. So an application
# started at login had no tray icon whenever it created the icon first. The extension exports the watcher before it
# requests the name, so once the name has an owner, Chromium's checks pass.

set -u

# Long enough for the Shell to enable its extensions, and short enough that without a tray at all the application is
# only delayed.
WAIT_S=30

gdbus wait --session --timeout "$WAIT_S" org.kde.StatusNotifierWatcher
exec "$@"
