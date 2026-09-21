#!/usr/bin/env bash
#
# Configure the GNOME desktop: input devices, the power button light, battery charging, bells, the login screen,
# workspaces, the look of the interface, privacy, power, the dock, file management, and the settings of individual
# GNOME applications.
#
# Keyboard layout and shortcuts are configured by configure-gnome-keys.sh, and the terminal by
# configure-ptyxis.sh. Workspaces are set in both this script and configure-gnome-keys.sh, because the workspace
# shortcuts there depend on them; the values have to agree.
#
#
# Extensions
# ----------
# Extensions packaged by Ubuntu are installed with apt when missing, as is gnome-shell-extension-prefs, whose presence
# is also what lets the shell check extensions.gnome.org for updates daily and apply them at the next login. The shell
# looks for extensions only when it starts, so a packaged extension installed during the session is enabled from the
# next login on. Extensions from extensions.gnome.org are installed through the shell, which asks for confirmation in
# a dialog and loads them immediately. Settings are then written with dconf from gnome-extensions/, one file per
# extension.
#
# tiling-assistant is disabled. While enabled it overrides mutter's tiling, and when disabled it resets the
# settings it overrode to their defaults. That includes the toggle-tiled keybindings, so on a system where it is
# still enabled, run this script before configure-gnome-keys.sh.
#
#
# Usage
# -----
#   ./configure-gnome-desktop.sh           show what would change, change nothing (default)
#   ./configure-gnome-desktop.sh --apply   apply the configuration
#
# Runs as your own user, because these settings live in your dconf and would land in root's under sudo. Installing
# packages, the console's boot-time unit, the udev rules, the module option, and the login screen settings need
# root, so those parts call sudo themselves and will ask for a password.

set -euo pipefail

MODE=dry-run
case "${1:-}" in
  ""|--dry-run) MODE=dry-run ;;
  --apply)      MODE=apply ;;
  -h|--help)    awk 'NR>1 && !/^#/{exit} NR>1{sub(/^# ?/, ""); print}' "$0"; exit 0 ;;
  *)            echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

CHANGED=0

# Compares against what gsettings currently reports and acts only on a difference. Desired values are written in
# the form gsettings get prints, so the comparison is a plain string match.
set_key() {
  local schema=$1 key=$2 want=$3 now
  if ! now=$(gsettings get "$schema" "$key" 2>/dev/null); then
    echo "  skip $schema $key, not present on this system"
    return 0
  fi
  [ "$now" = "$want" ] && return 0
  # gsettings prints a double as the shortest string for the value actually stored, so a value such as 0.85 reads
  # back as 0.84999999999999998 and never matches as text. Both sides have to look like plain numbers before
  # comparing that way, since otherwise awk would read true and false as zero and call them equal.
  if awk -v a="$now" -v b="$want" \
     'BEGIN { exit !(a ~ /^-?[0-9]+(\.[0-9]+)?$/ && b ~ /^-?[0-9]+(\.[0-9]+)?$/ && a+0 == b+0) }'; then
    return 0
  fi
  printf '  %-30s %-40s -> %s\n' "$key" "${now:0:40}" "${want:0:60}"
  [ "$MODE" = apply ] && gsettings set "$schema" "$key" "$want"
  CHANGED=$((CHANGED+1))
  return 0
}

REPO_DIR=$(dirname "$(readlink -f "$0")")
SHELL_EXT=(--session --dest org.gnome.Shell --object-path /org/gnome/Shell)

# Collects missing packages, so that apt runs once for all of them.
want_package() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'ok installed' && return 0
  echo "  install package $1"
  MISSING_PACKAGES+=("$1")
  CHANGED=$((CHANGED+1))
  return 0
}

extension_enabled() {
  gnome-extensions info "$1" 2>/dev/null | grep -q '^ *Enabled: Yes'
}

extension_installed() {
  gnome-extensions info "$1" >/dev/null 2>&1
}

# Installs from extensions.gnome.org through the shell itself, which asks for confirmation in a dialog, then loads
# and enables the extension straight away without a new login. The call is held open until the dialog is answered,
# hence the long timeout. It goes to the shell directly rather than to the org.gnome.Shell.Extensions service, which
# only forwards to the shell and exits two seconds after starting when none of its calls has returned yet, dropping a
# call whose dialog is still open.
install_remote() {
  local uuid=$1 result
  extension_installed "$uuid" && return 0
  echo "  install $uuid from extensions.gnome.org"
  CHANGED=$((CHANGED+1))
  [ "$MODE" = apply ] || return 0
  echo "      confirm the dialog the shell is showing"
  result=$(gdbus call "${SHELL_EXT[@]}" --timeout 600 \
             --method org.gnome.Shell.Extensions.InstallRemoteExtension "$uuid" 2>&1) || result="failed: $result"
  case "$result" in
    *successful*) ;;
    *cancelled*)  echo "      cancelled, $uuid not installed" ;;
    *)            echo "      $uuid not installed: $result" ;;
  esac
  return 0
}

# Enables an extension the running shell does not know by adding it to the list of enabled extensions, which the
# shell reads at the next login. It is also taken off the list of disabled extensions, which would take precedence.
enable_at_login() {
  local msg
  msg=$(python3 - "$1" "$MODE" <<'PY'
import sys
from gi.repository import Gio
uuid, mode = sys.argv[1], sys.argv[2]
s = Gio.Settings.new("org.gnome.shell")
enabled, disabled = s.get_strv("enabled-extensions"), s.get_strv("disabled-extensions")
if uuid in enabled and uuid not in disabled:
    sys.exit(0)
print("  enable %s at the next login" % uuid)
if mode == "apply":
    if uuid not in enabled:
        s.set_strv("enabled-extensions", enabled + [uuid])
    s.set_strv("disabled-extensions", [u for u in disabled if u != uuid])
    Gio.Settings.sync()
PY
)
  [ -z "$msg" ] && return 0
  echo "$msg"
  CHANGED=$((CHANGED+1))
  return 0
}

# Enabling and disabling go through gnome-extensions, which waits for the shell to run the extension's own
# enable or disable before returning. That ordering matters for extensions that override other settings while
# enabled and put them back when disabled.
want_extension() {
  local uuid=$1 state=$2
  # The shell looks for extensions only when it starts, so one installed during the session is unknown to it until
  # the next login.
  if ! extension_installed "$uuid"; then
    [ "$state" = enabled ] && enable_at_login "$uuid"
    return 0
  fi
  if [ "$state" = enabled ]; then
    extension_enabled "$uuid" && return 0
    echo "  enable $uuid"
    [ "$MODE" = apply ] && gnome-extensions enable "$uuid"
  else
    extension_enabled "$uuid" || return 0
    echo "  disable $uuid"
    [ "$MODE" = apply ] && gnome-extensions disable "$uuid"
  fi
  CHANGED=$((CHANGED+1))
  return 0
}

# Writes an extension's saved settings with dconf, which accepts them whether or not the extension's schema is
# installed. Only keys whose value differs are reported, and the file is loaded as a whole when any does.
extension_settings() {
  local name=$1 file=$REPO_DIR/gnome-extensions/$1.dconf base=/org/gnome/shell/extensions/$1/ diffs count
  diffs=$(python3 - "$file" "$base" <<'PY'
import configparser, subprocess, sys
path, base = sys.argv[1], sys.argv[2]
c = configparser.ConfigParser(interpolation=None, strict=False)
c.optionxform = str
c.read(path)
for section in c.sections():
    prefix = base if section == "/" else base + section + "/"
    for key, want in c.items(section):
        now = subprocess.run(["dconf", "read", prefix + key], capture_output=True, text=True).stdout.strip()
        if now != want:
            print("  %s%s: %s -> %s" % (prefix[len(base):], key, (now or "unset")[:40], want[:50]))
PY
)
  [ -z "$diffs" ] && return 0
  count=$(echo "$diffs" | wc -l)
  # A first run writes every key, which would bury everything else, so only a short list is shown in full.
  if [ "$count" -le 10 ]; then
    echo "  settings for $name:"
    echo "$diffs" | sed 's/^/  /'
  else
    echo "  settings for $name: $count keys to write"
  fi
  CHANGED=$((CHANGED+count))
  [ "$MODE" = apply ] && dconf load "$base" < "$file"
  return 0
}

echo "=== packages ==="
MISSING_PACKAGES=()
want_package gnome-shell-extension-prefs
want_package gnome-shell-extension-auto-move-windows
want_package gnome-shell-extension-drive-menu
# Provides kbdrate, which the console keyboard unit runs.
want_package kbd
if [ "$MODE" = apply ] && [ "${#MISSING_PACKAGES[@]}" -gt 0 ]; then
  sudo apt-get install "${MISSING_PACKAGES[@]}"
fi

echo "=== extensions ==="
# Extensions come before the settings, because disabling tiling-assistant restores the mutter settings it had
# overridden, and anything set before that would be overwritten by the restore.
install_remote caffeine@patapon.info
install_remote custom-hot-corners-extended@G-dH.github.com
install_remote just-perfection-desktop@just-perfection
install_remote vertical-workspaces@G-dH.github.com
install_remote preserve-battery-health@marcosdalvarez.org

want_extension auto-move-windows@gnome-shell-extensions.gcampax.github.com enabled
want_extension drive-menu@gnome-shell-extensions.gcampax.github.com enabled
want_extension ubuntu-appindicators@ubuntu.com enabled
# It overrides mutter's tiling while enabled, clearing the toggle-tiled keybindings that configure-gnome-keys.sh
# sets and resetting them to the defaults again at every logout.
want_extension tiling-assistant@ubuntu.com disabled

extension_settings auto-move-windows
extension_settings caffeine
extension_settings custom-hot-corners-extended
extension_settings just-perfection
extension_settings vertical-workspaces
extension_settings preserve-battery-health

WHOOPSIE=(--system com.ubuntu.WhoopsiePreferences /com/ubuntu/WhoopsiePreferences com.ubuntu.WhoopsiePreferences)

# Reads a whoopsie preference and sets it through the same service the Settings panel uses. Setting may ask for
# authentication, which only happens on --apply and only when the value actually differs.
whoopsie_want() {
  local prop=$1 method=$2 want=$3 now
  now=$(busctl get-property "${WHOOPSIE[@]}" "$prop" 2>/dev/null | awk '{print $2}') || {
    echo "  skip whoopsie $prop, the preferences service is not available"
    return 0
  }
  [ "$now" = "$want" ] && return 0
  printf '  %-30s %-40s -> %s\n' "whoopsie $prop" "$now" "$want"
  [ "$MODE" = apply ] && busctl call "${WHOOPSIE[@]}" "$method" b "$want"
  CHANGED=$((CHANGED+1))
  return 0
}

# Makes a bookmark the first one in the GTK bookmarks file, moving it there when it is further down and adding it
# when it is missing. The order of the other bookmarks is kept.
bookmark_first() {
  local uri=$1 file=${XDG_CONFIG_HOME:-$HOME/.config}/gtk-3.0/bookmarks line
  if [ -f "$file" ] && [ "$(head -n 1 "$file" | cut -d' ' -f1)" = "$uri" ]; then
    return 0
  fi
  echo "  put $uri first in $file"
  CHANGED=$((CHANGED+1))
  [ "$MODE" = apply ] || return 0
  mkdir -p "$(dirname "$file")"
  touch "$file"
  # A line is a URI optionally followed by a space and a label, and an existing label is kept.
  line=$(awk -v u="$uri" '$1 == u {print; exit}' "$file")
  { echo "${line:-$uri}"; awk -v u="$uri" '$1 != u' "$file"; } > "$file.new" && mv "$file.new" "$file"
  return 0
}

# Installs a systemd unit from systemd/ in this repository into the root-owned /etc/systemd/system through sudo,
# enables it, and runs it, so that it takes effect without a reboot.
install_unit() {
  local name=$1 src=$REPO_DIR/systemd/$1 dst=/etc/systemd/system/$1 touched=no
  if ! cmp -s "$src" "$dst"; then
    echo "  install $dst"
    CHANGED=$((CHANGED+1))
    touched=yes
    if [ "$MODE" = apply ]; then
      sudo install -m 644 "$src" "$dst"
      sudo systemctl daemon-reload
    fi
  fi
  if ! systemctl is-enabled --quiet "$name" 2>/dev/null; then
    echo "  enable $name"
    CHANGED=$((CHANGED+1))
    touched=yes
    [ "$MODE" = apply ] && sudo systemctl enable --quiet "$name"
  fi
  [ "$touched" = yes ] && [ "$MODE" = apply ] && sudo systemctl start "$name"
  return 0
}

# Installs a udev rule from udev/ in this repository into the root-owned /etc/udev/rules.d through sudo, then has
# udev reload its rules and apply them again to the devices of the given subsystem. A program that already has a
# device open sees the change only when it opens the device again.
install_udev_rule() {
  local name=$1 subsystem=$2 src=$REPO_DIR/udev/$1 dst=/etc/udev/rules.d/$1
  cmp -s "$src" "$dst" && return 0
  echo "  install $dst"
  CHANGED=$((CHANGED+1))
  [ "$MODE" = apply ] || return 0
  sudo install -m 644 "$src" "$dst"
  sudo udevadm control --reload
  sudo udevadm trigger --subsystem-match="$subsystem"
  return 0
}

# Installs a module option file from modprobe.d/ in this repository into the root-owned /etc/modprobe.d through
# sudo. A module that is already loaded is loaded again, so that the options take effect without a reboot.
install_modprobe_conf() {
  local name=$1 module=$2 src=$REPO_DIR/modprobe.d/$1 dst=/etc/modprobe.d/$1
  cmp -s "$src" "$dst" && return 0
  echo "  install $dst"
  CHANGED=$((CHANGED+1))
  [ "$MODE" = apply ] || return 0
  sudo install -m 644 "$src" "$dst"
  if [ -d "/sys/module/$module" ]; then
    sudo modprobe -r "$module"
    sudo modprobe "$module"
  fi
  return 0
}

GREETER=/etc/gdm3/greeter.dconf-defaults

# Sets a key in GDM's greeter settings through sudo, since the file is root owned. The key goes directly under its
# section header, replacing any earlier value, and the section is appended when the file does not have it yet.
greeter_set() {
  local section=$1 key=$2 want=$3 have new
  have=$(awk -v s="[$section]" -v k="$key" \
           '/^\[/ {in_s = ($0 == s)} in_s && index($0, k "=") == 1 {print substr($0, length(k) + 2); exit}' "$GREETER")
  [ "$have" = "$want" ] && return 0
  printf '  %-30s %-40s -> %s\n' "$key" "${have:-unset}" "$want"
  CHANGED=$((CHANGED+1))
  [ "$MODE" = apply ] || return 0
  new=$(awk -v s="[$section]" -v k="$key" -v v="$want" '
    /^\[/ { in_s = ($0 == s); print; if (in_s && !done) { print k "=" v; done = 1 }; next }
    in_s && index($0, k "=") == 1 { next }
    { print }
    END { if (!done) { print ""; print s; print k "=" v } }' "$GREETER")
  sudo tee "$GREETER" >/dev/null <<< "$new"
  GREETER_TOUCHED=yes
  return 0
}

echo "=== keyboard ==="
set_key org.gnome.desktop.peripherals.keyboard repeat                            "true"
# Longer than the default 500 ms, so that a key held slightly too long is not typed more than once. GNOME repeats
# keys itself and ignores the kernel's repeat, which the console uses, so the console gets the same delay from a
# unit run at boot.
set_key org.gnome.desktop.peripherals.keyboard delay                             "uint32 1000"
install_unit console-keyboard-repeat.service

echo "=== touchpad ==="
set_key org.gnome.desktop.peripherals.touchpad speed                            "0.36964980544747084"
set_key org.gnome.desktop.peripherals.touchpad tap-and-drag                     "false"
set_key org.gnome.desktop.peripherals.touchpad click-method                     "'fingers'"

echo "=== touchscreen ==="
# The rule makes libinput ignore every touchscreen. GNOME Shell checks for that only when a device appears, so it
# takes effect at the next login.
install_udev_rule 90-ignore-touchscreens.rules input

echo "=== power button light ==="
install_udev_rule 90-power-button-light-off.rules leds

echo "=== battery charging ==="
# Settings, Power offers Maximize Charge and Preserve Battery Health once UPower finds charge thresholds on the
# battery. The kernel exposes them only with the module option, and the udev rule gives UPower the thresholds for
# Preserve Battery Health. Which of the two applies is chosen in Settings and remembered by UPower.
BATTERY_CHANGED=$CHANGED
install_modprobe_conf cros-charge-control.conf cros_charge_control
install_udev_rule 90-battery-charge-limit.rules power_supply
# UPower reads both only when it adds the battery.
if [ "$MODE" = apply ] && [ "$CHANGED" -gt "$BATTERY_CHANGED" ]; then
  sudo systemctl restart upower
fi

echo "=== bell and sound ==="
set_key org.gnome.desktop.wm.preferences audible-bell                     "false"
set_key org.gnome.desktop.wm.preferences visual-bell                      "true"
set_key org.gnome.desktop.sound event-sounds                     "false"

echo "=== login screen ==="
# The login screen runs as the gdm user with settings of its own, which GDM compiles from greeter.dconf-defaults
# when it starts or reloads.
GREETER_TOUCHED=no
greeter_set org/gnome/desktop/sound event-sounds          "false"
greeter_set org/gnome/desktop/sound input-feedback-sounds "false"
greeter_set org/gnome/login-screen logo                   "''"
if [ "$GREETER_TOUCHED" = yes ]; then
  sudo systemctl reload gdm
fi

echo "=== workspaces and windows ==="
set_key org.gnome.mutter dynamic-workspaces               "false"
set_key org.gnome.mutter center-new-windows               "false"
set_key org.gnome.mutter edge-tiling                      "false"
set_key org.gnome.mutter workspaces-only-on-primary       "false"
set_key org.gnome.desktop.wm.preferences num-workspaces                   "12"
set_key org.gnome.desktop.wm.preferences action-middle-click-titlebar     "'none'"
set_key org.gnome.desktop.wm.preferences mouse-button-modifier            "'disabled'"
set_key org.gnome.shell.app-switcher current-workspace-only           "true"

echo "=== interface ==="
set_key org.gnome.desktop.interface clock-show-date                  "false"
set_key org.gnome.desktop.interface clock-show-weekday               "true"
set_key org.gnome.desktop.interface enable-animations                "false"
set_key org.gnome.desktop.interface enable-hot-corners               "true"
set_key org.gnome.desktop.interface monospace-font-name              "'Ubuntu Sans Mono 13'"
set_key org.gnome.desktop.calendar show-weekdate                    "true"
set_key org.gnome.desktop.background picture-uri                      "'file:///usr/share/backgrounds/Little_numbat_boy_by_azskalt.png'"
set_key org.gnome.desktop.background picture-uri-dark                 "'file:///usr/share/backgrounds/Little_numbat_boy_by_azskalt.png'"
set_key org.gnome.desktop.screensaver picture-uri                      "'file:///usr/share/backgrounds/Little_numbat_boy_by_azskalt.png'"

echo "=== colour scheme ==="
# The shell is dark and applications are light. Ubuntu's shell takes its scheme from org.gnome.shell.ubuntu when that
# is set, independently of the one applications follow, which they read from org.gnome.desktop.interface directly or
# through the settings portal. GTK 3 applications follow the theme instead, and the Yaru-dark variants would make
# them dark. GIMP and Inkscape keep their own dark themes, so that the drawing stands out.
set_key org.gnome.shell.ubuntu color-scheme                     "'prefer-dark'"
set_key org.gnome.desktop.interface color-scheme                "'prefer-light'"
set_key org.gnome.desktop.interface gtk-theme                   "'Yaru'"
set_key org.gnome.desktop.interface icon-theme                  "'Yaru'"

echo "=== privacy and power ==="
# Neither error reports nor metrics are sent. The GNOME key covers GNOME, but on Ubuntu the uploading is done by
# whoopsie, whose preferences are held by a system service rather than in dconf, so both are set.
set_key org.gnome.desktop.privacy report-technical-problems  "false"
whoopsie_want ReportCrashes              SetReportCrashes              false
whoopsie_want AutomaticallyReportCrashes SetAutomaticallyReportCrashes false
whoopsie_want ReportMetrics              SetReportMetrics              false
# Apport still records crashes in /var/crash, but without a dialog offering to report each of them.
set_key com.ubuntu.update-notifier show-apport-crashes "false"
set_key org.gnome.desktop.privacy recent-files-max-age             "30"
set_key org.gnome.desktop.privacy remove-old-temp-files            "true"
set_key org.gnome.desktop.screensaver lock-delay                       "uint32 30"
set_key org.gnome.settings-daemon.plugins.power ambient-enabled                  "false"
set_key org.gnome.settings-daemon.plugins.power idle-dim                         "false"
set_key org.gnome.settings-daemon.plugins.power sleep-inactive-ac-timeout        "3600"
set_key org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type           "'nothing'"
set_key org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type      "'nothing'"
set_key org.gnome.desktop.media-handling autorun-x-content-start-app      "@as []"


echo "=== dock and desktop icons ==="
set_key org.gnome.shell.extensions.dash-to-dock click-action                     "'skip'"
set_key org.gnome.shell.extensions.dash-to-dock hot-keys                         "false"
set_key org.gnome.shell.extensions.dash-to-dock isolate-workspaces               "true"
set_key org.gnome.shell.extensions.dash-to-dock preferred-monitor-by-connector   "'eDP-1'"
set_key org.gnome.shell.extensions.dash-to-dock scroll-action                    "'cycle-windows'"
set_key org.gnome.shell.extensions.dash-to-dock shortcut                         "@as []"
set_key org.gnome.shell.extensions.dash-to-dock show-trash                       "false"
set_key org.gnome.shell.extensions.ding icon-size                        "'small'"
set_key org.gnome.shell.extensions.ding show-home                        "false"
set_key org.gnome.shell.extensions.ding start-corner                     "'top-left'"

echo "=== files ==="
set_key org.gnome.nautilus.preferences default-folder-viewer            "'list-view'"
set_key org.gnome.nautilus.preferences fts-enabled                      "true"
set_key org.gnome.nautilus.icon-view default-zoom-level               "'extra-large'"
set_key org.gtk.Settings.FileChooser sort-column                      "'modified'"
set_key org.gtk.Settings.FileChooser sort-order                       "'descending'"
set_key org.gtk.gtk4.Settings.FileChooser sort-column                      "'modified'"
set_key org.gtk.gtk4.Settings.FileChooser sort-order                       "'descending'"

echo "=== applications ==="
set_key org.gnome.calculator button-mode                      "'programming'"
set_key org.gnome.calculator number-format                    "'fixed'"
set_key org.gnome.TextEditor spellcheck                       "false"
set_key org.gnome.TextEditor style-variant                    "'light'"
set_key org.gnome.meld folder-status-filters            "['new', 'modified']"
set_key org.gnome.meld ignore-blank-lines               "true"
set_key org.gnome.yelp show-cursor                      "true"

echo "=== file manager sidebar ==="
# The folders in the Files sidebar are the entries of the GTK bookmarks file. xdg-user-dirs-gtk-update adds the
# standard folders to it at login but leaves Desktop out by design, and never removes an entry it did not add, so
# Desktop placed first stays there.
bookmark_first "file://$HOME/Desktop"

echo
if [ "$CHANGED" -eq 0 ]; then
  echo "already configured, nothing to change"
elif [ "$MODE" = apply ]; then
  echo "applied $CHANGED change(s)"
else
  echo "$CHANGED change(s) would be made. Nothing was written, re-run with --apply."
fi
