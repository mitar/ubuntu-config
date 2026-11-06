#!/usr/bin/env bash
#
# Run scope_test.py in every scenario against a systemd user instance of its own. See README.
#
# Usage: run-scope-test.sh GLIB_ROOT OUT_DIR USER
#
# Runs as root in a privileged container, and refuses to run anywhere else, as it creates cgroups at the root of the
# cgroup hierarchy it sees. GLIB_ROOT is a directory the libglib2.0-0t64 package to test was extracted into, whose
# libraries and gio-launch-desktop are used in place of the installed ones. OUT_DIR is removed first, then receives the
# logs and what each scenario reports. USER is the unprivileged user the user instance and the scenarios run as.

set -euo pipefail

GLIB_ROOT=$(readlink -f "$1")
OUT=$(readlink -m "$2")
TEST_USER=$3
HERE=$(dirname "$(readlink -f "$0")")

[ "$(id -u)" = 0 ] || { echo "run-scope-test.sh has to run as root, in a container" >&2; exit 2; }
# In a container with a cgroup namespace of its own, the process is at the root of the hierarchy it sees. On a host,
# a shell is always in a session or a unit, and the cgroups created below would land next to the host's own.
[ "$(cat /proc/self/cgroup)" = "0::/" ] || { echo "run-scope-test.sh runs only in a container with a cgroup namespace of its own" >&2; exit 2; }

LIBDIR=$(dirname "$(find "$GLIB_ROOT" -name 'libgio-2.0.so.0' | head -n 1)")
LAUNCH_DESKTOP=$(find "$GLIB_ROOT" -name gio-launch-desktop -type f | head -n 1)
[ -n "$LAUNCH_DESKTOP" ] && [ "$LIBDIR" != . ] || { echo "no libgio-2.0.so.0 or gio-launch-desktop in $GLIB_ROOT" >&2; exit 2; }

rm -rf "$OUT"
mkdir -p "$OUT"
chown "$TEST_USER:" "$OUT"

TEST_UID=$(id -u "$TEST_USER")
RUNTIME_DIR=/run/user/$TEST_UID
CGROUP=/sys/fs/cgroup/user.slice/user-$TEST_UID.slice/user@$TEST_UID.service

# The user instance is started the way user@.service starts it, in a cgroup delegated to the user, which it then
# divides into its slices and units, and with a runtime directory of the user's. systemd refuses to start a user
# instance when the system was not booted with it, which it tells by this directory.
mkdir -p /run/systemd/system "$CGROUP" "$RUNTIME_DIR"
chown -R "$TEST_USER:" "$CGROUP" "$RUNTIME_DIR"
chmod 700 "$RUNTIME_DIR"

AS_USER=(setpriv --reuid="$TEST_USER" --regid="$TEST_USER" --init-groups env -i HOME="$(getent passwd "$TEST_USER" | cut -d: -f6)"
  USER="$TEST_USER" PATH=/usr/bin:/bin XDG_RUNTIME_DIR="$RUNTIME_DIR" DBUS_SESSION_BUS_ADDRESS="unix:path=$RUNTIME_DIR/bus")
UNDER_TEST=(LD_LIBRARY_PATH="$LIBDIR" GIO_LAUNCH_DESKTOP="$LAUNCH_DESKTOP")
# A service of the user instance, as GNOME Shell's is. A scope would not do, as the user instance cannot move this
# script's processes, which are outside of the cgroup delegated to it. KillMode=process leaves an application which
# stayed in the service's cgroup running after the launching process exited, for the check.
IN_SERVICE=(systemd-run --user --wait --pipe --quiet --collect --property=KillMode=process --setenv=LD_LIBRARY_PATH="$LIBDIR"
  --setenv=GIO_LAUNCH_DESKTOP="$LAUNCH_DESKTOP" --setenv=DBUS_SESSION_BUS_ADDRESS="unix:path=$RUNTIME_DIR/bus")

(echo "$BASHPID" > "$CGROUP/cgroup.procs" && exec "${AS_USER[@]}" /usr/lib/systemd/systemd --user) > "$OUT/user-manager.log" 2>&1 &
MANAGER_PID=$!
trap 'kill "$MANAGER_PID" 2>/dev/null || true' EXIT

for _ in $(seq 300); do
  state=$("${AS_USER[@]}" systemctl --user is-system-running 2>/dev/null || true)
  case "$state" in running|degraded) break ;; esac
  sleep 0.1
done
case "$state" in
  running|degraded) ;;
  *) echo "the user instance did not start, it is $state, see $OUT/user-manager.log" >&2; exit 1 ;;
esac

# The container has no journald, which gio-launch-desktop logs to when standard error is the journal.
python3 -I "$HERE/fake_journald.py" "$OUT/journal" > "$OUT/fake-journald.log" 2>&1 &
JOURNALD_PID=$!
trap 'kill "$MANAGER_PID" "$JOURNALD_PID" 2>/dev/null || true' EXIT
for _ in $(seq 100); do
  [ -S /dev/log ] && [ -S /run/systemd/journal/stdout ] && break
  sleep 0.1
done
[ -S /dev/log ] && [ -S /run/systemd/journal/stdout ] || { echo "fake_journald.py did not start, see $OUT/fake-journald.log" >&2; exit 1; }

status=0
run() {
  echo "== $1"
  shift
  "$@" || status=1
}

TEST=(python3 -I "$HERE/scope_test.py")
run loop "${AS_USER[@]}" "${IN_SERVICE[@]}" "${TEST[@]}" loop "$OUT"
run exit "${AS_USER[@]}" "${IN_SERVICE[@]}" "${TEST[@]}" exit "$OUT"
run check-exit "${AS_USER[@]}" "${TEST[@]}" check-exit "$OUT"
run late-request "${AS_USER[@]}" "${IN_SERVICE[@]}" "${TEST[@]}" late-request "$OUT"
run no-systemd "${AS_USER[@]}" "${IN_SERVICE[@]}" dbus-run-session -- "${TEST[@]}" no-systemd "$OUT"
run no-systemd-exit "${AS_USER[@]}" "${IN_SERVICE[@]}" dbus-run-session -- "${TEST[@]}" no-systemd-exit "$OUT"
run check-no-systemd-exit "${AS_USER[@]}" "${TEST[@]}" check-no-systemd-exit "$OUT"
FAKE=(python3 -I "$HERE/fake_systemd.py")
run refused "${AS_USER[@]}" "${IN_SERVICE[@]}" dbus-run-session -- "${FAKE[@]}" refuse "${TEST[@]}" refused "$OUT"
run refused-exit "${AS_USER[@]}" "${IN_SERVICE[@]}" dbus-run-session -- "${FAKE[@]}" refuse "${TEST[@]}" refused-exit "$OUT"
run check-refused-exit "${AS_USER[@]}" "${TEST[@]}" check-refused-exit "$OUT"
run job-failed "${AS_USER[@]}" "${IN_SERVICE[@]}" dbus-run-session -- "${FAKE[@]}" fail "${TEST[@]}" job-failed "$OUT"
run slow "${AS_USER[@]}" "${IN_SERVICE[@]}" dbus-run-session -- "${FAKE[@]}" slow "${TEST[@]}" slow "$OUT"
# A launching process with a PID namespace of its own, which still uses the session bus and the cgroups of the user
# instance, as in a sandbox like Firejail's. It is put into a cgroup of the user instance first, as a process the user
# instance started would be.
PIDNS_CGROUP=$CGROUP/app.slice/pid-namespace-test
mkdir -p "$PIDNS_CGROUP"
chown -R "$TEST_USER:" "$PIDNS_CGROUP"
# shellcheck disable=SC2016
run pid-namespace bash -c 'echo "$$" > "$1/cgroup.procs" && shift && exec unshare --pid --fork --mount-proc "$@"' _ \
  "$PIDNS_CGROUP" "${AS_USER[@]}" "${UNDER_TEST[@]}" "${TEST[@]}" pid-namespace "$OUT"
# Outside of the user instance, in the cgroup this script runs in.
run outside "${AS_USER[@]}" "${UNDER_TEST[@]}" "${TEST[@]}" outside "$OUT"

exit $status
