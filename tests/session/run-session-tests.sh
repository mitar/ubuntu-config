#!/usr/bin/env bash
#
# Run session_tests.py for each TEST, each as a new user in a GNOME session of its own. See README.
#
# Usage: run-session-tests.sh OUT_DIR TEST...
#
# Runs as root in a privileged container prepared with prepare-container.sh, and refuses to run anywhere else, as it
# creates cgroups at the root of the cgroup hierarchy it sees, and users. OUT_DIR is removed first, then receives a
# directory for each TEST with the files the test wrote and the journal written meanwhile.

set -euo pipefail

HERE=$(dirname "$(readlink -f "$0")")
OUT=$(readlink -m "$1")
shift

[ "$(id -u)" = 0 ] || { echo "run-session-tests.sh has to run as root, in a container" >&2; exit 2; }
# In a container with a cgroup namespace of its own, the process is at the root of the hierarchy it sees. On a host,
# a shell is always in a session or a unit, and the cgroups created below would land next to the host's own.
[ "$(cat /proc/self/cgroup)" = "0::/" ] || { echo "run-session-tests.sh runs only in a container with a cgroup namespace of its own" >&2; exit 2; }
[ $# -gt 0 ] || { echo "no test given" >&2; exit 2; }

rm -rf "$OUT"
mkdir -p "$OUT"

# The container is to look like a machine booted with systemd, without logind, whose seats directory makes
# gnome-session and GNOME Shell use it. Without it they track the session themselves, which works without logind.
rm -rf /run/systemd/seats
mkdir -p /run/systemd/system /run/dbus
# The directories of X's sockets, which systemd-tmpfiles creates at boot. Xwayland refuses one which a user created.
install -d -m 1777 -o root -g root /tmp/.X11-unix /tmp/.ICE-unix
# A privileged container sees the host's GPU, which the hosted CI runners do not have. GNOME Shell and the applications
# render in software either way, but with the devices visible they try them and differ from the runners. The devices
# can be mounts, so an empty directory is mounted over them.
for dir in /dev/dri /dev/accel; do
  [ -d "$dir" ] && mount -t tmpfs -o mode=755 none "$dir"
done

# journald keeps the session's logs, which the tests check and the output directory receives. It opens its sockets
# itself when it is not started by systemd.
/usr/lib/systemd/systemd-journald > "$OUT/journald.log" 2>&1 &
for _ in $(seq 100); do
  [ -S /run/systemd/journal/socket ] && [ -S /run/systemd/journal/stdout ] && break
  sleep 0.1
done
[ -S /run/systemd/journal/stdout ] || { echo "journald did not start, see $OUT/journald.log" >&2; exit 1; }
# Members of systemd-journal may read the journal, as systemd's tmpfiles.d sets it up when the machine boots.
chgrp -R systemd-journal /run/log/journal
chmod -R g+rX /run/log/journal
find /run/log/journal -type d -exec chmod g+s {} +
# gnome-session and GNOME Shell connect to the system bus, where nothing answers for the machine's services but
# fake_logind.py, which the tests drive through an interface logind does not have.
cat > /etc/dbus-1/system.d/session-test.conf <<'END'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy context="default">
    <allow send_destination="org.freedesktop.login1" send_interface="org.freedesktop.login1.SessionTest"/>
  </policy>
</busconfig>
END
dbus-daemon --system --fork --nopidfile
python3 -I "$HERE/fake_logind.py" "$OUT/logind.json" > "$OUT/fake-logind.log" 2>&1 &
for _ in $(seq 100); do
  gdbus introspect --system --dest org.freedesktop.login1 --object-path /org/freedesktop/login1 >/dev/null 2>&1 && break
  sleep 0.1
done

status=0
n=0
for test in "$@"; do
  n=$((n+1))
  user=session-test-$n
  # The tests read the session's journal, which the group systemd-journal allows.
  useradd --create-home --shell /bin/bash --groups systemd-journal "$user"
  uid=$(id -u "$user")
  runtime_dir=/run/user/$uid
  cgroup=/sys/fs/cgroup/user.slice/user-$uid.slice/user@$uid.service

  # The user's service manager is started the way user@.service starts it, in a cgroup delegated to the user, which it
  # then divides into its slices and units, and with a runtime directory of the user's.
  mkdir -p "$cgroup" "$runtime_dir"
  chown -R "$user:" "$cgroup" "$runtime_dir"
  chmod 700 "$runtime_dir"
  as_user=(setpriv --reuid="$user" --regid="$user" --init-groups env -i HOME="/home/$user" USER="$user" LOGNAME="$user"
    SHELL=/bin/bash PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 XDG_RUNTIME_DIR="$runtime_dir"
    DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime_dir/bus" SESSION_TEST_LOGIND_STATE="$OUT/logind.json")
  (echo "$BASHPID" > "$cgroup/cgroup.procs" && exec "${as_user[@]}" /usr/lib/systemd/systemd --user --log-target=journal) &
  manager=$!
  # It is killed at the end, which bash would report otherwise.
  disown "$manager"
  state=
  for _ in $(seq 300); do
    state=$("${as_user[@]}" systemctl --user is-system-running 2>/dev/null || true)
    case "$state" in running|degraded) break ;; esac
    sleep 0.1
  done

  # A test given more than once, to see whether it fails only now and then, reports each run separately.
  dir=$OUT/$test
  [ -e "$dir" ] && dir=$OUT/$test-$n
  mkdir -p "$dir"
  chown "$user:" "$dir"
  since=$(date +%s)
  echo "== $test"
  case "$state" in
    running|degraded) "${as_user[@]}" python3 -I "$HERE/session_tests.py" "$test" "$dir" || status=1 ;;
    *) echo "FAIL $test: the user's service manager did not start, it is $state" ; status=1 ;;
  esac
  journalctl --since="@$((since - 1))" -o short-precise > "$dir/journal.txt" 2>&1 || true

  # Everything of the user ends, as at a reboot, before the next test starts with a new user, and with it the X
  # displays its Xwayland held.
  kill "$manager" 2>/dev/null || true
  pkill -KILL -u "$user" 2>/dev/null || true
  rm -f /tmp/.X*-lock /tmp/.X11-unix/X*
done
exit $status
