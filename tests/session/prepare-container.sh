#!/usr/bin/env bash
#
# Prepare a container for run-session-tests.sh. See README.
#
# Usage: prepare-container.sh DEBS_DIR...
#
# Runs as root in a container, and refuses to run anywhere else, as it installs packages and system files. Installs a
# GNOME session as Ubuntu ships it, with the applications the tests run, then the packages from the .deb files in each
# DEBS_DIR over the installed ones of the same name, and Firefox from there, which Ubuntu has only as a snap. A package
# in a later DEBS_DIR replaces one of the same name in an earlier one, so a build to test can come after the local
# repository. Chromium is installed when a configured repository has it, as the xtradeb one does. Then installs this
# repository's session files where configure-gnome-desktop.sh installs them, and the files the tests need besides.

set -euo pipefail

REPO_DIR=$(readlink -f "$(dirname "$(readlink -f "$0")")/../..")
HERE=$REPO_DIR/tests/session

[ "$(id -u)" = 0 ] || { echo "prepare-container.sh has to run as root, in a container" >&2; exit 2; }
[ -e /.dockerenv ] || [ -e /run/.containerenv ] || { echo "prepare-container.sh runs only in a container" >&2; exit 2; }

export DEBIAN_FRONTEND=noninteractive

# The packages of a GNOME session on Ubuntu, without the services of the machine, and the applications the tests run.
# Python runs the tests, and xdg-utils and the portals open links and files as they do on the desktop. --no-upgrade
# keeps the installed packages as they are, as apt-get would otherwise replace a package built here, when this runs
# again, with Ubuntu's package of the same version.
apt-get update -q
apt-get install -y -q --no-install-recommends --no-upgrade \
  at-spi2-core dbus dbus-user-session dconf-gsettings-backend fonts-dejavu-core gnome-session gnome-shell \
  gnome-shell-extension-appindicator gnome-shell-extension-ubuntu-dock gnome-text-editor gsettings-desktop-schemas \
  procps ptyxis python3 python3-gi systemd ubuntu-session util-linux xdg-desktop-portal xdg-desktop-portal-gnome \
  xdg-utils
if [ -n "$(apt-cache policy chromium 2>/dev/null | awk '/Candidate:/ && $2 != "(none)" {print $2}')" ]; then
  apt-get install -y -q --no-install-recommends --no-upgrade chromium chromium-driver chromium-sandbox
fi

# The packages built here replace the installed ones of the same name, and Firefox is installed from them.
declare -A debs=()
for dir in "$@"; do
  for deb in "$dir"/*.deb; do
    [ -e "$deb" ] || continue
    package=$(dpkg-deb -f "$deb" Package)
    if [ "$package" = firefox ] || dpkg -s "$package" >/dev/null 2>&1; then
      debs[$package]=$deb
    fi
  done
done
if [ ${#debs[@]} -gt 0 ]; then
  echo "installing ${!debs[*]}"
  # dpkg installs each file also when the same version is installed already, as a build of Ubuntu's version is, which
  # apt-get would skip. apt-get then installs the dependencies which the installed packages do not provide.
  dpkg -i "${debs[@]}" || apt-get install -y -q --no-install-recommends -f
fi

# This repository's session files, where configure-gnome-desktop.sh installs them.
install -D -m 644 "$REPO_DIR/systemd/user/app-.scope.d/50-graphical-session.conf" \
  /etc/systemd/user/app-.scope.d/50-graphical-session.conf
install -D -m 644 "$REPO_DIR/systemd/user/org.gnome.Shell@.service.d/50-before-application-scopes.conf" \
  /etc/systemd/user/org.gnome.Shell@.service.d/50-before-application-scopes.conf
install -D -m 644 "$REPO_DIR/systemd/user/at-spi-dbus-bus.service.d/50-stop-launcher-only.conf" \
  /etc/systemd/user/at-spi-dbus-bus.service.d/50-stop-launcher-only.conf
install -D -m 755 "$REPO_DIR/stop-application-scopes.sh" /usr/local/libexec/stop-application-scopes
install -D -m 755 "$REPO_DIR/stop-application-scopes-before-shutdown.py" /usr/local/libexec/stop-application-scopes-before-shutdown
for unit in stop-application-scopes.service stop-application-scopes-before-shutdown.service; do
  install -m 644 "$REPO_DIR/systemd/user/$unit" "/etc/systemd/user/$unit"
  systemctl --global enable --quiet "$unit"
done
if [ -d /etc/chromium.d ]; then
  install -m 644 "$REPO_DIR/chromium.d/wayland-session-management" /etc/chromium.d/wayland-session-management
  # Only for the tests: a new profile starts without the questions of a first start, and without a keyring to unlock.
  # The file is sourced by /usr/bin/chromium, which expands the variable.
  # shellcheck disable=SC2016
  echo 'export CHROMIUM_FLAGS="$CHROMIUM_FLAGS --no-first-run --no-default-browser-check --password-store=basic"' \
    > /etc/chromium.d/session-test
  # Chromium reopens the windows of the last session only with "Continue where you left off", which is not its
  # default, and which the tests of the windows' placement need.
  mkdir -p /etc/chromium/policies/managed
  echo '{"RestoreOnStartup": 1}' > /etc/chromium/policies/managed/session-test.json
  install -D -m 755 "$REPO_DIR/chromium-quit.sh" /usr/local/libexec/chromium-quit
  install -D -m 644 "$REPO_DIR/chromium-native-messaging-hosts/local.chromium_quit.json" \
    /etc/chromium/native-messaging-hosts/local.chromium_quit.json
  rm -rf /usr/share/chromium/extensions/quit-shortcut
  mkdir -p /usr/share/chromium/extensions
  cp -r "$REPO_DIR/chromium-extensions/quit-shortcut" /usr/share/chromium/extensions/quit-shortcut
fi

# GNOME Shell runs headless, with a virtual monitor, as the container has no display. The unit refuses to start
# outside of a Wayland session, which the test session is, but whose type only gnome-session sets.
mkdir -p /etc/systemd/user/org.gnome.Shell@.service.d
cat > /etc/systemd/user/org.gnome.Shell@.service.d/90-session-test-headless.conf <<'EOF'
[Unit]
AssertEnvironment=

[Service]
ExecStart=
ExecStart=/usr/bin/gnome-shell --headless --virtual-monitor 1920x1080 --mode=%i
EOF

# Firefox restores the windows of the last session when it starts and opens links from other applications in a new
# window, as the user's profile has it, and saves its session every second instead of every 15, so that the tests do
# not wait as long. A new profile starts without the pages and questions of a first start.
if [ -x /usr/lib/firefox/firefox ]; then
  mkdir -p /etc/firefox/policies
  cat > /etc/firefox/policies/policies.json <<'EOF'
{
  "policies": {
    "DisableAppUpdate": true,
    "DisableTelemetry": true,
    "DontCheckDefaultBrowser": true,
    "OverrideFirstRunPage": "",
    "OverridePostUpdatePage": "",
    "UserMessaging": {"WhatsNew": false, "ExtensionRecommendations": false, "FeatureRecommendations": false, "SkipOnboarding": true, "MoreFromMozilla": false},
    "Preferences": {
      "browser.startup.page": {"Value": 3, "Status": "user"},
      "browser.link.open_newwindow.override.external": {"Value": 2, "Status": "user"},
      "browser.sessionstore.interval": {"Value": 1000, "Status": "user"},
      "browser.aboutwelcome.enabled": {"Value": false, "Status": "user"}
    }
  }
}
EOF
fi

# The extension through which the tests drive GNOME Shell, for every user of the container.
rm -rf /usr/share/gnome-shell/extensions/session-test@mitar.tnode.com
cp -r "$HERE/session-test@mitar.tnode.com" /usr/share/gnome-shell/extensions/
