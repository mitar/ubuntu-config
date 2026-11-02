#!/usr/bin/env bash
#
# Run driver.py against a headless mutter of its own, in a D-Bus session of its own. See README.
#
# Usage: run-session.sh MUTTER_ROOT GTK_ROOT FIREFOX XDG_CURRENT_DESKTOP OUT_DIR SCENARIO
#
# MUTTER_ROOT is a directory the mutter and libmutter-18-0 packages were extracted into, and GTK_ROOT one the
# libgtk-3-0t64 package was extracted into, or "-" for the installed GTK. Only Firefox uses GTK_ROOT. OUT_DIR is
# removed first, then receives the mutter log, the Firefox logs and the profile, and is used as the XDG data,
# config, state and cache home, so that nothing is written to the real ones.

set -euo pipefail

if [ -z "${INSIDE_DBUS:-}" ]; then
  exec env INSIDE_DBUS=1 dbus-run-session -- "$0" "$@"
fi

MUTTER_ROOT=$1
GTK_ROOT=$2
FIREFOX=$3
DESKTOP=$4
OUT=$(readlink -f "$5")
SCENARIO=$6
HERE=$(dirname "$(readlink -f "$0")")
DISPLAY_NAME=ff-session-test-$$

rm -rf "$OUT"
mkdir -p "$OUT/xdg/data" "$OUT/xdg/config" "$OUT/xdg/state" "$OUT/xdg/cache"
export XDG_DATA_HOME=$OUT/xdg/data XDG_CONFIG_HOME=$OUT/xdg/config XDG_STATE_HOME=$OUT/xdg/state XDG_CACHE_HOME=$OUT/xdg/cache

LIBDIR=$MUTTER_ROOT/usr/lib/x86_64-linux-gnu
LD_LIBRARY_PATH=$LIBDIR:$LIBDIR/mutter-18 "$MUTTER_ROOT/usr/bin/mutter" --headless --no-x11 \
  --virtual-monitor 1920x1080 --wayland-display "$DISPLAY_NAME" --mutter-plugin="$LIBDIR/mutter-18/plugins/libdefault.so" > "$OUT/mutter.log" 2>&1 &
MUTTER_PID=$!
trap 'kill $MUTTER_PID 2>/dev/null || true' EXIT

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
