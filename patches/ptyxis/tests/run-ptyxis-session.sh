#!/usr/bin/env bash
#
# Run ptyxis_session.py, which starts a headless mutter of its own, in a D-Bus session of its own. See README.
#
# Usage: run-ptyxis-session.sh MUTTER_ROOT PTYXIS_ROOT OUT_DIR
#
# MUTTER_ROOT is a directory the mutter and libmutter-18-0 packages were extracted into, built with the session
# management patches of patches/mutter/. PTYXIS_ROOT is a directory the ptyxis package to test was extracted into. It
# runs with the installed GTK, which has to be the one from patches/gtk4/, and the installed ptyxis-agent. OUT_DIR is
# removed first, then receives the logs, and is used as the home directory and the XDG data, config, state and cache
# home, so that nothing is written to the real ones, neither Ptyxis's session nor mutter's stored sessions. A temporary
# directory under /tmp, removed at the end, is the XDG runtime directory.

set -euo pipefail

# The private D-Bus session, and every service it starts on demand, such as the desktop portals, sees only the
# headless display. With the real one in their environment, those services could open windows on your desktop.
if [ -z "${INSIDE_DBUS:-}" ]; then
  exec env -u DISPLAY WAYLAND_DISPLAY=ptyxis-session-test-$$ INSIDE_DBUS=1 dbus-run-session -- "$0" "$@"
fi

MUTTER_ROOT=$(readlink -f "$1")
PTYXIS_ROOT=$(readlink -f "$2")
OUT=$(readlink -f "$3")
HERE=$(dirname "$(readlink -f "$0")")

rm -rf "$OUT"
mkdir -p "$OUT/home" "$OUT/xdg/data" "$OUT/xdg/config" "$OUT/xdg/state" "$OUT/xdg/cache"
export HOME=$OUT/home
export XDG_DATA_HOME=$OUT/xdg/data XDG_CONFIG_HOME=$OUT/xdg/config XDG_STATE_HOME=$OUT/xdg/state XDG_CACHE_HOME=$OUT/xdg/cache
# A runtime directory of its own keeps a document portal which the private bus starts from treating the session's
# /run/user/<uid>/doc as a stale mount and unmounting it. It is kept short, because the Wayland socket is created in
# it and socket paths are limited to 108 bytes.
RUNTIME_DIR=$(mktemp -d /tmp/ptyxis-session-test.XXXXXX)
export XDG_RUNTIME_DIR=$RUNTIME_DIR
# Services the private bus starts on demand receive the environment the bus had when it started, so they are told
# about the private directories too.
dbus-update-activation-environment HOME XDG_RUNTIME_DIR XDG_DATA_HOME XDG_CONFIG_HOME XDG_STATE_HOME XDG_CACHE_HOME
# Ptyxis's settings and profiles, and mutter's key bindings, are read from their defaults and not from dconf.
export GSETTINGS_BACKEND=memory GTK_A11Y=none

# The document portal and gvfs, which the private bus starts on demand, mount FUSE filesystems in the runtime
# directory and keep running until the bus ends, after this script. Unmounting them lazily lets the directory be
# removed.
cleanup() {
  findmnt -rn -o TARGET | grep -F "$RUNTIME_DIR/" | while read -r mountpoint; do
    fusermount3 -u -z "$mountpoint" 2>/dev/null || true
  done || true
  rm -rf "${RUNTIME_DIR:?}"
}
trap cleanup EXIT

env -u DISPLAY XDG_SESSION_TYPE=wayland python3 -I "$HERE/ptyxis_session.py" "$PTYXIS_ROOT/usr/bin/ptyxis" "$MUTTER_ROOT" "$OUT"
