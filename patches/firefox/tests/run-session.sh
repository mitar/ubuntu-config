#!/usr/bin/env bash
#
# Run driver.py against a headless mutter of its own, in a D-Bus session of its own. See README.
#
# Usage: run-session.sh MUTTER_ROOT GTK_ROOT FIREFOX XDG_CURRENT_DESKTOP OUT_DIR SCENARIO
#
# MUTTER_ROOT is a directory the mutter and libmutter-18-0 packages were extracted into, and GTK_ROOT one the
# libgtk-3-0t64 package was extracted into, or "-" for the installed GTK. Only Firefox uses GTK_ROOT. OUT_DIR is
# removed first, then receives the mutter log, the Firefox logs and the profile, and is used as the XDG data,
# config, state and cache home, so that nothing is written to the real ones. A temporary directory under /tmp, removed
# at the end, is the XDG runtime directory.

set -euo pipefail

# The private D-Bus session, and every service it starts on demand, such as the desktop portals, sees only the
# headless display. With the real one in their environment, those services could open windows on your desktop.
if [ -z "${INSIDE_DBUS:-}" ]; then
  exec env -u DISPLAY WAYLAND_DISPLAY=ff-session-test-$$ INSIDE_DBUS=1 dbus-run-session -- "$0" "$@"
fi

MUTTER_ROOT=$1
GTK_ROOT=$2
FIREFOX=$3
DESKTOP=$4
OUT=$(readlink -f "$5")
SCENARIO=$6
HERE=$(dirname "$(readlink -f "$0")")
DISPLAY_NAME=$WAYLAND_DISPLAY

rm -rf "$OUT"
mkdir -p "$OUT/xdg/data" "$OUT/xdg/config" "$OUT/xdg/state" "$OUT/xdg/cache"
export XDG_DATA_HOME=$OUT/xdg/data XDG_CONFIG_HOME=$OUT/xdg/config XDG_STATE_HOME=$OUT/xdg/state XDG_CACHE_HOME=$OUT/xdg/cache
# A runtime directory of its own keeps a document portal which the private bus starts from taking the session's
# /run/user/<uid>/doc for a stale mount and unmounting it. It is kept short, because the Wayland socket is created in
# it and socket paths are limited to 108 bytes.
RUNTIME_DIR=$(mktemp -d /tmp/ff-session-test.XXXXXX)
export XDG_RUNTIME_DIR=$RUNTIME_DIR
# Services the private bus starts on demand receive the environment the bus had when it started, so they are told
# about the private directories too.
dbus-update-activation-environment XDG_RUNTIME_DIR XDG_DATA_HOME XDG_CONFIG_HOME XDG_STATE_HOME XDG_CACHE_HOME

LIBDIR=$MUTTER_ROOT/usr/lib/x86_64-linux-gnu
env -u WAYLAND_DISPLAY LD_LIBRARY_PATH=$LIBDIR:$LIBDIR/mutter-18 "$MUTTER_ROOT/usr/bin/mutter" --headless --no-x11 \
  --virtual-monitor 1920x1080 --wayland-display "$DISPLAY_NAME" --mutter-plugin="$LIBDIR/mutter-18/plugins/libdefault.so" > "$OUT/mutter.log" 2>&1 &
MUTTER_PID=$!

# The document portal and gvfs, which the private bus starts on demand, mount FUSE filesystems in the runtime
# directory and keep running until the bus ends, after this script. Unmounting them lazily lets the directory be
# removed.
cleanup() {
  kill "$MUTTER_PID" 2>/dev/null || true
  findmnt -rn -o TARGET | grep -F "$RUNTIME_DIR/" | while read -r mountpoint; do
    fusermount3 -u -z "$mountpoint" 2>/dev/null || true
  done || true
  rm -rf "${RUNTIME_DIR:?}"
}
trap cleanup EXIT

for _ in $(seq 100); do
  [ -S "$XDG_RUNTIME_DIR/$DISPLAY_NAME" ] && break
  sleep 0.1
done
[ -S "$XDG_RUNTIME_DIR/$DISPLAY_NAME" ] || { echo "mutter did not start, see $OUT/mutter.log" >&2; exit 1; }

FIREFOX_LIBS=
[ "$GTK_ROOT" != - ] && FIREFOX_LIBS=$GTK_ROOT/usr/lib/x86_64-linux-gnu

env -u DISPLAY WAYLAND_DISPLAY="$DISPLAY_NAME" MOZ_ENABLE_WAYLAND=1 XDG_CURRENT_DESKTOP="$DESKTOP" \
  LD_LIBRARY_PATH="$FIREFOX_LIBS" \
  MOZ_LOG=Widget:4,WidgetWayland:4 WAYLAND_DEBUG=1 \
  python3 -I "$HERE/driver.py" "$FIREFOX" "$OUT/profile" "$OUT" "$SCENARIO"
