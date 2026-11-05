#!/usr/bin/env bash
#
# Run rename_client.py against a headless mutter of its own, in a D-Bus session of its own, and report whether mutter
# survived it. See README.
#
# Usage: run-rename-check.sh MUTTER_ROOT OUT_DIR [SCENARIO]
#
# MUTTER_ROOT is a directory the mutter and libmutter-18-0 packages were extracted into. OUT_DIR is removed first,
# then receives the mutter log, and is used as the XDG data, config, state and cache home. A temporary directory under
# /tmp, removed at the end, is the XDG runtime directory. SCENARIO is passed on to rename_client.py and defaults to
# rename-check.

set -uo pipefail

# The private D-Bus session, and every service it starts on demand, such as the desktop portals, sees only the
# headless display. With the real one in their environment, those services could open windows on your desktop.
if [ -z "${INSIDE_DBUS:-}" ]; then
  exec env -u DISPLAY WAYLAND_DISPLAY=rename-check-$$ INSIDE_DBUS=1 dbus-run-session -- "$0" "$@"
fi

MUTTER_ROOT=$(readlink -f "$1")
OUT=$(readlink -f "$2")
SCENARIO=${3:-rename-check}
HERE=$(dirname "$(readlink -f "$0")")
DISPLAY_NAME=$WAYLAND_DISPLAY

rm -rf "$OUT"
mkdir -p "$OUT/xdg/data" "$OUT/xdg/config" "$OUT/xdg/state" "$OUT/xdg/cache"
export XDG_DATA_HOME=$OUT/xdg/data XDG_CONFIG_HOME=$OUT/xdg/config XDG_STATE_HOME=$OUT/xdg/state XDG_CACHE_HOME=$OUT/xdg/cache
# A runtime directory of its own keeps a document portal which the private bus starts from taking the session's
# /run/user/<uid>/doc for a stale mount and unmounting it. It is kept short, because the Wayland socket is created in
# it and socket paths are limited to 108 bytes.
RUNTIME_DIR=$(mktemp -d /tmp/rename-check.XXXXXX)
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

env -u DISPLAY WAYLAND_DISPLAY="$DISPLAY_NAME" timeout 20 python3 -I "$HERE/rename_client.py" "$SCENARIO"
sleep 1
if kill -0 $MUTTER_PID 2>/dev/null; then
  echo "mutter: still running"
  kill $MUTTER_PID
  exit 0
fi
wait $MUTTER_PID
echo "mutter: exited with status $?, see $OUT/mutter.log"
exit 1
