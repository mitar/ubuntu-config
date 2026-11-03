#!/usr/bin/env bash
#
# Run rename_client.py against a headless mutter of its own, in a D-Bus session of its own, and report whether mutter
# survived it. See README.
#
# Usage: run-rename-check.sh MUTTER_ROOT OUT_DIR [SCENARIO]
#
# MUTTER_ROOT is a directory the mutter and libmutter-18-0 packages were extracted into. OUT_DIR is removed first,
# then receives the mutter log, and is used as the XDG data, config, state and cache home. SCENARIO is passed on to
# rename_client.py and defaults to rename-check.

set -uo pipefail

if [ -z "${INSIDE_DBUS:-}" ]; then
  exec env INSIDE_DBUS=1 dbus-run-session -- "$0" "$@"
fi

MUTTER_ROOT=$(readlink -f "$1")
OUT=$(readlink -f "$2")
SCENARIO=${3:-rename-check}
HERE=$(dirname "$(readlink -f "$0")")
DISPLAY_NAME=rename-check-$$

rm -rf "$OUT"
mkdir -p "$OUT/xdg/data" "$OUT/xdg/config" "$OUT/xdg/state" "$OUT/xdg/cache"
export XDG_DATA_HOME=$OUT/xdg/data XDG_CONFIG_HOME=$OUT/xdg/config XDG_STATE_HOME=$OUT/xdg/state XDG_CACHE_HOME=$OUT/xdg/cache

LIBDIR=$MUTTER_ROOT/usr/lib/x86_64-linux-gnu
LD_LIBRARY_PATH=$LIBDIR:$LIBDIR/mutter-18 "$MUTTER_ROOT/usr/bin/mutter" --headless --no-x11 \
  --virtual-monitor 1920x1080 --wayland-display "$DISPLAY_NAME" --mutter-plugin="$LIBDIR/mutter-18/plugins/libdefault.so" > "$OUT/mutter.log" 2>&1 &
MUTTER_PID=$!

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
