#!/usr/bin/env bash
#
# Configure Ptyxis.
#
# Sets the colours, the window size, the profiles and the keyboard shortcuts. The shortcuts are the substantial
# part: application actions sit on plain Control, so Control+C copies and Control+T opens a tab.
#
# That layout depends on vte being patched to read Super as Control, which is in patches/vte2.91 and is built by
# rebuild-patched-packages.sh. With that patch an interrupt is sent with Super+C, which leaves Control+C free to
# be an application shortcut. On an unpatched vte this configuration leaves the terminal with no way to
# interrupt a running process.
#
#
# Usage
# -----
#   ./configure-ptyxis.sh           show what would change, change nothing (default)
#   ./configure-ptyxis.sh --apply   apply the configuration
#
# Runs as your own user. Ptyxis settings live in dconf, so nothing here needs root.
#
#
# Profiles
# --------
# Two, identical apart from transparency: Default at 85 percent opacity, and Without transparency at full. Ptyxis
# registers profiles in a uuid list rather than deriving them from settings, so the second is created and
# registered here, and found by its label on later runs so that repeating the script does not add more.
#
#
# What Ptyxis cannot do
# ---------------------
# Three things are settings only once patches/ptyxis is built in: the selection highlight colours, a shortcut for
# Copy as HTML, and the search keys running the right way round. Until then the highlight keys in the palette are
# ignored, copy-html is reported as skipped, and the unshifted search key moves backwards.

set -euo pipefail

REPO_DIR=$(dirname "$(readlink -f "$0")")
PALETTE_SRC=$REPO_DIR/ptyxis/tango-black.palette
PALETTE_DIR=${XDG_DATA_HOME:-$HOME/.local/share}/org.gnome.Ptyxis/palettes
PALETTE_NAME=tango-black
SECOND_LABEL="Without transparency"

MODE=dry-run
case "${1:-}" in
  ""|--dry-run) MODE=dry-run ;;
  --apply)      MODE=apply ;;
  -h|--help)    awk 'NR>1 && !/^#/{exit} NR>1{sub(/^# ?/, ""); print}' "$0"; exit 0 ;;
  *)            echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

CHANGED=0
# The binding each shortcut ends up with, whether this script set it or it kept its default. Collected so that two
# actions claiming the same key can be reported, which gsettings itself will happily allow.
declare -A EFFECTIVE

# Compare against the value gsettings currently reports and only act on a difference. Desired values are written
# in the form gsettings get prints, so that the comparison is a plain string match and needs no type awareness.
set_key() {
  local schema=$1 key=$2 want=$3 now
  if ! now=$(gsettings get "$schema" "$key" 2>/dev/null); then
    echo "  skip $key, no such key in this Ptyxis version"
    return 0
  fi
  [ "$schema" = "org.gnome.Ptyxis.Shortcuts" ] && EFFECTIVE[$key]=$want
  [ "$now" = "$want" ] && return 0
  # gsettings prints a double as the shortest string for the value actually stored, so 0.85 reads back as
  # 0.84999999999999998 and never matches as text. Both sides have to look like plain numbers before comparing
  # that way, since otherwise awk would read true and false as zero and call them equal.
  if awk -v a="$now" -v b="$want" \
     'BEGIN { exit !(a ~ /^-?[0-9]+(\.[0-9]+)?$/ && b ~ /^-?[0-9]+(\.[0-9]+)?$/ && a+0 == b+0) }'; then
    return 0
  fi
  printf '  %-24s %-24s -> %s\n' "$key" "$now" "$want"
  [ "$MODE" = apply ] && gsettings set "$schema" "$key" "$want"
  CHANGED=$((CHANGED+1))
  return 0
}

profile_path() { echo "org.gnome.Ptyxis.Profile:/org/gnome/Ptyxis/Profiles/$1/"; }

# Everything the two profiles share. They differ only in transparency and name, so those are the parameters.
configure_profile() {
  local uuid=$1 opacity=$2 label=$3 path
  path=$(profile_path "$uuid")
  set_key "$path" palette             "'$PALETTE_NAME'"
  set_key "$path" login-shell         "true"
  set_key "$path" bold-is-bright      "true"
  # Unlimited scrollback is limit-scrollback turned off, rather than a large line count.
  set_key "$path" limit-scrollback    "false"
  set_key "$path" scroll-on-keystroke "false"
  # Whether a new tab or window inherits the working directory of the one it came from.
  set_key "$path" preserve-directory  "'safe'"
  set_key "$path" opacity             "$opacity"
  set_key "$path" label               "'$label'"
}

# Ptyxis registers its profiles in a uuid list, so a second one has to be created and registered rather than just
# configured. It is located by its label, so re-running the script does not keep adding profiles.
find_profile_by_label() {
  local uuid
  for uuid in $(gsettings get org.gnome.Ptyxis profile-uuids | tr -d "[]',"); do
    [ -n "$uuid" ] || continue
    [ "$(gsettings get "$(profile_path "$uuid")" label 2>/dev/null)" = "'$1'" ] && { echo "$uuid"; return 0; }
  done
  return 0
}

command -v ptyxis >/dev/null || { echo "ptyxis is not installed" >&2; exit 1; }

# Ptyxis keeps per profile settings under a relocatable schema keyed by uuid, so everything profile shaped has to
# be addressed through the uuid the application considers default.
PROFILE_UUID=$(gsettings get org.gnome.Ptyxis default-profile-uuid | tr -d "'")
[ -n "$PROFILE_UUID" ] || { echo "no default Ptyxis profile, start Ptyxis once first" >&2; exit 1; }


echo "=== palette ==="
if [ ! -r "$PALETTE_SRC" ]; then
  echo "  missing $PALETTE_SRC" >&2; exit 1
fi
if [ -r "$PALETTE_DIR/$PALETTE_NAME.palette" ] && cmp -s "$PALETTE_SRC" "$PALETTE_DIR/$PALETTE_NAME.palette"; then
  echo "  $PALETTE_NAME already installed"
else
  echo "  install $PALETTE_NAME into $PALETTE_DIR"
  if [ "$MODE" = apply ]; then
    mkdir -p "$PALETTE_DIR"
    cp "$PALETTE_SRC" "$PALETTE_DIR/$PALETTE_NAME.palette"
  fi
  CHANGED=$((CHANGED+1))
fi

echo "=== application ==="
set_key org.gnome.Ptyxis default-columns "uint32 110"
set_key org.gnome.Ptyxis default-rows    "uint32 27"
# With this off, default-columns and default-rows above decide the size of every new window. With it on, Ptyxis
# reopens at whatever size the last window was left at and ignores them.
set_key org.gnome.Ptyxis restore-window-size "false"
# Audible bells are turned into visual feedback system wide, in org.gnome.desktop.wm.preferences, so the
# terminal emits the bell and does not decide how it is signalled.
set_key org.gnome.Ptyxis audible-bell    "true"
# Kept on alongside the system wide visual bell. This one flashes the terminal's own header, which shows which
# terminal the fullscreen flash came from.
set_key org.gnome.Ptyxis visual-bell     "true"
set_key org.gnome.Ptyxis enable-zoom-scroll-ctrl "false"
set_key org.gnome.Ptyxis toast-on-copy-clipboard "false"
set_key org.gnome.Ptyxis new-tab-position "'last'"
# The window chrome follows the system light or dark setting. The terminal colours do not, because the palette
# pins them.
set_key org.gnome.Ptyxis interface-style "'system'"

echo "=== profile, default ==="
configure_profile "$PROFILE_UUID" "0.85" "Default"

echo "=== profile, $SECOND_LABEL ==="
SECOND_UUID=$(find_profile_by_label "$SECOND_LABEL")
if [ -z "$SECOND_UUID" ]; then
  if [ "$MODE" = apply ]; then
    SECOND_UUID=$(uuidgen | tr -d -)
    gsettings set org.gnome.Ptyxis profile-uuids \
      "$(gsettings get org.gnome.Ptyxis profile-uuids | sed "s/]$/, '$SECOND_UUID']/")"
    echo "  created and registered profile $SECOND_UUID"
  else
    echo "  would create and register a second profile, then configure it as above with opacity 1.0"
  fi
  CHANGED=$((CHANGED+1))
fi
[ -n "$SECOND_UUID" ] && configure_profile "$SECOND_UUID" "1.0" "$SECOND_LABEL"

# Application actions sit on plain Control. The shell is driven with Super instead, through the patched vte, so
# the Control keys listed here are the ones the shell does not see.
#
# An empty string disables a binding. Anything Ptyxis offers that is not named here keeps its default.
echo "=== shortcuts ==="
S=org.gnome.Ptyxis.Shortcuts
set_key $S copy-clipboard     "'<ctrl>c'"
set_key $S paste-clipboard    "'<ctrl>v'"
set_key $S select-all         "'<ctrl>a'"
set_key $S new-tab            "'<ctrl>t'"
set_key $S new-window         "'<ctrl>n'"
set_key $S close-tab          "'<ctrl>w'"
set_key $S search             "'<ctrl>f'"
set_key $S reset              "'<ctrl>r'"
set_key $S reset-and-clear    "'<ctrl>k'"
# Not F11, so that F11 reaches the terminal, where full screen console applications expect it.
set_key $S toggle-fullscreen  "'<ctrl><shift>f'"
set_key $S zoom-in            "'<ctrl>plus'"
set_key $S zoom-out           "'<ctrl>minus'"
set_key $S zoom-one           "'<ctrl>0'"
# Shifted slots, left free by the common actions sitting on plain Control.
# The key only exists once patches/ptyxis is built in, and set_key reports it as skipped until then.
set_key $S copy-html          "'<ctrl><shift>c'"
set_key $S undo-close-tab     "'<ctrl><shift>t'"
set_key $S select-none        "'<ctrl><shift>a'"
set_key $S tab-overview       "'<ctrl>o'"

for i in 1 2 3 4 5 6 7 8 9; do
  set_key $S "focus-tab-$i" "'<alt>$i'"
done
set_key $S focus-tab-10       "'<alt>0'"
# Left unbound.
set_key $S close-window       "''"
set_key $S move-next-tab      "''"
set_key $S move-previous-tab  "''"
set_key $S move-tab-left      "''"
set_key $S move-tab-right     "''"
set_key $S primary-menu       "''"
set_key $S preferences        "''"

# Anything not named above keeps whatever Ptyxis ships, which still has to be considered when looking for clashes.
for key in $(gsettings list-keys org.gnome.Ptyxis.Shortcuts); do
  [ -n "${EFFECTIVE[$key]:-}" ] || EFFECTIVE[$key]=$(gsettings get org.gnome.Ptyxis.Shortcuts "$key")
done

conflicts=$(for key in "${!EFFECTIVE[@]}"; do
              val=${EFFECTIVE[$key]}
              [ "$val" = "''" ] && continue
              echo "$val $key"
            done | sort | awk '{ if ($1 == prev) { if (!shown) print prev, prevkey; print $1, $2; shown=1 } else shown=0; prev=$1; prevkey=$2 }')
if [ -n "$conflicts" ]; then
  echo
  echo "=== conflicting shortcuts, two actions on one key ==="
  echo "$conflicts" | sed 's/^/  /'
fi

echo
if [ "$CHANGED" -eq 0 ]; then
  echo "already configured, nothing to change"
elif [ "$MODE" = apply ]; then
  echo "applied $CHANGED change(s), restart Ptyxis for the palette to appear"
else
  echo "$CHANGED change(s) would be made. Nothing was written, re-run with --apply."
fi
