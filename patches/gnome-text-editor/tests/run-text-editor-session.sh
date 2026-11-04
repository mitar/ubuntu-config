#!/usr/bin/env bash
#
# Run text_editor_session.py against a headless mutter of its own, in a D-Bus session of its own. See README.
#
# Usage: run-text-editor-session.sh MUTTER_ROOT ROOT OUT_DIR [SCENARIO]
#
# MUTTER_ROOT is a directory the mutter and libmutter-18-0 packages were extracted into, built with
# patches/mutter/xx-session-management.patch. ROOT is a directory the gnome-text-editor and libgtk-4-1 packages to
# test were extracted into. OUT_DIR is removed first, then receives the logs and the files Text Editor opens, and is
# used as the XDG data, config, state and cache home, so that nothing is written to the real ones, mutter's stored
# sessions included. SCENARIO is none, unsupported or supported, see text_editor_session.py, and defaults to
# unsupported, which is how gnome-session 50 behaves.

set -euo pipefail

# The private D-Bus session, and every service it starts on demand, such as the desktop portals, sees only the
# headless display. With the real one in their environment, those services could open windows on your desktop.
if [ -z "${INSIDE_DBUS:-}" ]; then
  exec env -u DISPLAY WAYLAND_DISPLAY=text-editor-session-test-$$ INSIDE_DBUS=1 dbus-run-session -- "$0" "$@"
fi

MUTTER_ROOT=$(readlink -f "$1")
ROOT=$(readlink -f "$2")
OUT=$(readlink -f "$3")
SCENARIO=${4:-unsupported}
HERE=$(dirname "$(readlink -f "$0")")
DISPLAY_NAME=$WAYLAND_DISPLAY

case "$SCENARIO" in
  none|unsupported|supported) ;;
  *) echo "unknown scenario $SCENARIO" >&2; exit 2 ;;
esac

rm -rf "$OUT"
mkdir -p "$OUT/xdg/data" "$OUT/xdg/config" "$OUT/xdg/state" "$OUT/xdg/cache"
export XDG_DATA_HOME=$OUT/xdg/data XDG_CONFIG_HOME=$OUT/xdg/config XDG_STATE_HOME=$OUT/xdg/state XDG_CACHE_HOME=$OUT/xdg/cache
# Text Editor's own settings, such as whether it restores the session, are read from defaults and not from dconf.
export GSETTINGS_BACKEND=memory GTK_A11Y=none

LIBDIR=$MUTTER_ROOT/usr/lib/x86_64-linux-gnu
env -u WAYLAND_DISPLAY LD_LIBRARY_PATH=$LIBDIR:$LIBDIR/mutter-18 "$MUTTER_ROOT/usr/bin/mutter" --headless --no-x11 \
  --virtual-monitor 1920x1080 --wayland-display "$DISPLAY_NAME" --mutter-plugin="$LIBDIR/mutter-18/plugins/libdefault.so" > "$OUT/mutter.log" 2>&1 &
MUTTER_PID=$!
PIDS=$MUTTER_PID
trap 'kill $PIDS 2>/dev/null || true' EXIT

if [ "$SCENARIO" != none ]; then
  python3 -I "$HERE/fake_session_manager.py" "$([ "$SCENARIO" = supported ] && echo true || echo false)" \
    "$OUT/session-manager.log" > "$OUT/session-manager.out" 2>&1 &
  PIDS="$PIDS $!"
  for _ in $(seq 100); do
    gdbus introspect --session --dest org.gnome.SessionManager --object-path /org/gnome/SessionManager >/dev/null 2>&1 && break
    sleep 0.1
  done
fi

for _ in $(seq 100); do
  [ -S "$XDG_RUNTIME_DIR/$DISPLAY_NAME" ] && break
  sleep 0.1
done
[ -S "$XDG_RUNTIME_DIR/$DISPLAY_NAME" ] || { echo "mutter did not start, see $OUT/mutter.log" >&2; exit 1; }

status=0
env -u DISPLAY WAYLAND_DISPLAY="$DISPLAY_NAME" XDG_SESSION_TYPE=wayland LD_LIBRARY_PATH="$ROOT/usr/lib/x86_64-linux-gnu" \
  python3 -I "$HERE/text_editor_session.py" "$ROOT/usr/bin/gnome-text-editor" "$OUT" "$SCENARIO" || status=$?
kill -0 $MUTTER_PID 2>/dev/null || { echo "mutter exited during the test, see $OUT/mutter.log" >&2; status=1; }
exit $status
