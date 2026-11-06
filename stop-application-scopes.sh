#!/bin/sh
#
# Installed as /usr/local/libexec/stop-application-scopes by configure-gnome-desktop.sh, and run by
# stop-application-scopes.service when the graphical session ends, in the user's service manager.
#
# Sends SIGTERM to the browser process of each scope which a Chromium-based application created for itself, and
# returns once these processes exited, or after WAIT_S seconds. Every application scope is ordered before the service,
# so systemd stops the scopes, and with them every process left in them, only after this returns.
#
# Chromium, and every Electron application built on it, moves its browser process into a scope of its own, which it
# names after the process, app-<name>-<pid>.scope, and starts its renderers there too, while its zygotes and GPU
# process stay in the app-gnome-*.scope GNOME launched it in. Stopping the scopes sends SIGTERM to all of these at
# once, and a browser process whose helpers die under it crashes with "GPU process isn't usable. Goodbye.", or stalls
# without its renderer until the service manager is killed. Receiving SIGTERM alone, it shuts its helpers down itself.

set -uf

# Below the TimeoutStopSec= of stop-application-scopes.service, so that the scopes are still stopped in order when an
# application does not exit.
WAIT_S=8

# GNOME's scopes are named after the application rather than after a process. Scopes which failed are listed with a
# marker in front of their name, but they need no stopping, so only active scopes, and those being stopped, are
# listed.
units=$(systemctl --user list-units --plain --no-legend --full --state=active,deactivating 'app-*.scope' |
  cut -d ' ' -f 1 | grep -v '^app-gnome-')
[ -n "$units" ] || exit 0

pids=
for unit in $units; do
  pid=${unit%.scope}
  pid=${pid##*-}
  case $pid in
    ''|*[!0-9]*) continue ;;
  esac
  # Other launchers end the names of their scopes with a random number, so a process receives the signal only when
  # it is in the scope named after it.
  cgroup=$(cat "/proc/$pid/cgroup" 2>/dev/null) || continue
  case $cgroup in
    */"$unit") ;;
    *) continue ;;
  esac
  kill -TERM "$pid" 2>/dev/null && pids="$pids $pid"
done

# A process which exited stays a zombie until its parent collects it, so it counts as gone once its state is Z.
alive() {
  state=$(awk '/^State:/ {print $2}' "/proc/$1/status" 2>/dev/null) && [ -n "$state" ] && [ "$state" != Z ]
}

deadline=$(($(date +%s) + WAIT_S))
for pid in $pids; do
  while alive "$pid" && [ "$(date +%s)" -lt "$deadline" ]; do
    sleep 0.1
  done
done
