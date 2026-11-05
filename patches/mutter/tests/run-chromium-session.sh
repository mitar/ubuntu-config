#!/usr/bin/env bash
#
# Run chromium_session.py against a headless mutter of its own, in a D-Bus session of its own. See README.
#
# Usage: run-chromium-session.sh MUTTER_ROOT OUT_DIR [CHROMIUM [CHROMEDRIVER]]
#
# MUTTER_ROOT is a directory the mutter and libmutter-18-0 packages were extracted into. OUT_DIR is removed first,
# then receives the mutter log, the logs of chromedriver and Chromium and the profile, and is used as the XDG data,
# config, state and cache home, so that nothing is written to the real ones, mutter's stored sessions included. A
# temporary directory under /tmp, removed at the end, is the XDG runtime directory. CHROMIUM defaults to
# /usr/lib/chromium/chromium and CHROMEDRIVER to /usr/bin/chromedriver.

set -euo pipefail

# The private D-Bus session, and every service it starts on demand, such as the desktop portals, sees only the
# headless display. With the real one in their environment, those services could open windows on your desktop.
if [ -z "${INSIDE_DBUS:-}" ]; then
  exec env -u DISPLAY WAYLAND_DISPLAY=chromium-session-test-$$ INSIDE_DBUS=1 dbus-run-session -- "$0" "$@"
fi

MUTTER_ROOT=$(readlink -f "$1")
OUT=$(readlink -f "$2")
CHROMIUM=${3:-/usr/lib/chromium/chromium}
CHROMEDRIVER=${4:-/usr/bin/chromedriver}
HERE=$(dirname "$(readlink -f "$0")")
DISPLAY_NAME=$WAYLAND_DISPLAY

rm -rf "$OUT"
mkdir -p "$OUT/xdg/data" "$OUT/xdg/config" "$OUT/xdg/state" "$OUT/xdg/cache"
export XDG_DATA_HOME=$OUT/xdg/data XDG_CONFIG_HOME=$OUT/xdg/config XDG_STATE_HOME=$OUT/xdg/state XDG_CACHE_HOME=$OUT/xdg/cache
# A runtime directory of its own keeps a document portal which the private bus starts from taking the session's
# /run/user/<uid>/doc for a stale mount and unmounting it. It is kept short, because the Wayland socket is created in
# it and socket paths are limited to 108 bytes.
RUNTIME_DIR=$(mktemp -d /tmp/chromium-session-test.XXXXXX)
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

status=0
env -u DISPLAY WAYLAND_DISPLAY="$DISPLAY_NAME" XDG_SESSION_TYPE=wayland WAYLAND_DEBUG=1 \
  python3 -I "$HERE/chromium_session.py" "$CHROMEDRIVER" "$CHROMIUM" "$OUT/profile" "$OUT" || status=$?
kill -0 $MUTTER_PID 2>/dev/null || { echo "mutter exited during the test, see $OUT/mutter.log" >&2; status=1; }
exit $status
