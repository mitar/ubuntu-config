#!/usr/bin/env bash
#
# Run text_editor_autosave.py against a headless mutter of its own, in a D-Bus session of its own. See README.
#
# Usage: run-text-editor-autosave.sh MUTTER_ROOT ROOT OUT_DIR
#
# MUTTER_ROOT is a directory the mutter and libmutter-18-0 packages were extracted into. ROOT is a directory Text
# Editor was installed or extracted into, with its GSettings schema, and with the GTK to use when libgtk-4-1 was
# extracted there too. OUT_DIR is removed first, then receives the logs, the files Text Editor opens and the XDG data,
# config, state and cache homes of each scenario, so that nothing is written to the real ones. A temporary directory
# under /tmp, removed at the end, is the XDG runtime directory.

set -euo pipefail

# The private D-Bus session, and every service it starts on demand, such as the desktop portals, sees only the
# headless display. With the real one in their environment, those services could open windows on your desktop.
if [ -z "${INSIDE_DBUS:-}" ]; then
  exec env -u DISPLAY WAYLAND_DISPLAY=text-editor-autosave-test-$$ INSIDE_DBUS=1 dbus-run-session -- "$0" "$@"
fi

MUTTER_ROOT=$(readlink -f "$1")
ROOT=$(readlink -f "$2")
OUT=$(readlink -f "$3")
HERE=$(dirname "$(readlink -f "$0")")
DISPLAY_NAME=$WAYLAND_DISPLAY

rm -rf "$OUT"
mkdir -p "$OUT/schemas"

# Text Editor reads its settings with the keyfile backend, from the XDG config home of each scenario, so that a
# scenario can set them before Text Editor starts. Its schema comes from ROOT, so that a build with new keys runs.
cp "$ROOT"/usr/share/glib-2.0/schemas/org.gnome.TextEditor*.xml "$OUT/schemas/"
glib-compile-schemas "$OUT/schemas"
export GSETTINGS_BACKEND=keyfile GSETTINGS_SCHEMA_DIR=$OUT/schemas GTK_A11Y=none

# A runtime directory of its own keeps a document portal which the private bus starts from treating the session's
# /run/user/<uid>/doc as a stale mount and unmounting it. It is kept short, because the Wayland socket is created in
# it and socket paths are limited to 108 bytes. Services the private bus starts on demand receive the environment the
# bus had when it started, so they are told about it too.
RUNTIME_DIR=$(mktemp -d /tmp/text-editor-autosave-test.XXXXXX)
export XDG_RUNTIME_DIR=$RUNTIME_DIR
dbus-update-activation-environment XDG_RUNTIME_DIR

LIBDIR=$MUTTER_ROOT/usr/lib/x86_64-linux-gnu
mkdir -p "$OUT/mutter-xdg"
env -u WAYLAND_DISPLAY XDG_DATA_HOME="$OUT/mutter-xdg" XDG_STATE_HOME="$OUT/mutter-xdg" XDG_CONFIG_HOME="$OUT/mutter-xdg" \
  XDG_CACHE_HOME="$OUT/mutter-xdg" LD_LIBRARY_PATH=$LIBDIR:$LIBDIR/mutter-18 "$MUTTER_ROOT/usr/bin/mutter" --headless \
  --no-x11 --virtual-monitor 1920x1080 --wayland-display "$DISPLAY_NAME" --mutter-plugin="$LIBDIR/mutter-18/plugins/libdefault.so" \
  > "$OUT/mutter.log" 2>&1 &
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
env -u DISPLAY WAYLAND_DISPLAY="$DISPLAY_NAME" XDG_SESSION_TYPE=wayland LD_LIBRARY_PATH="$ROOT/usr/lib/x86_64-linux-gnu" \
  python3 -I "$HERE/text_editor_autosave.py" "$ROOT/usr/bin/gnome-text-editor" "$OUT" || status=$?
kill -0 $MUTTER_PID 2>/dev/null || { echo "mutter exited during the test, see $OUT/mutter.log" >&2; status=1; }
exit $status
