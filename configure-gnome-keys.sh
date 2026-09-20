#!/usr/bin/env bash
#
# Configure the system wide Super and Control swap, and the GNOME shortcuts that go with it.
#
# The keyboard is rotated three ways with an xkb option: the physical Ctrl key emits Super, the physical Win key
# emits Alt, and the physical Alt key emits Ctrl. The thumb key therefore acts as Control, which is where Command
# sits on a Mac, and the pinky key acts as Super.
#
# Two consequences run through everything below.
#
# A shortcut written with <Primary> is pressed with the thumb. That is where the common actions go, which is why
# most of the common actions sit on <Primary> rather than on Alt or Super.
#
# A shortcut written with bare <Super> is physically Ctrl plus that key, so binding one takes that Control
# shortcut away from every application. Those are kept to a minimum, and the ones that remain are listed under
# "Bindings on the physical Control key" below.
#
# In the terminal the physical Ctrl key has to send control characters, which is what patches/vte2.91 does by
# reading Super as Control. Ptyxis then keeps its own shortcuts on <Primary>, set by configure-ptyxis.sh.
#
#
# Physical layout
# ---------------
# The keycaps are arranged to match a Mac, so the Alt and Super caps sit swapped relative to the hardware beneath
# them. A Super cap therefore marks the key in the Command position, which is the one that copies and pastes, and
# on the right there is no Copilot cap to be had, so the Ctrl cap stays where it is.
#
#   cap     hardware  emits    note
#   Ctrl    LCTL      Super    acts as Control inside the terminal, through the patched vte
#   Fn      -         -        handled in firmware, never reaches xkb. The BIOS can move it, this does not.
#   Alt     LWIN      Alt
#   Super   LALT      Control  the Command position, and what copies and pastes
#   Super   RALT      Control
#   Ctrl    RCTL      Copilot  the firmware sends Shift+Super+F23, whatever the cap says
#
#
# Usage
# -----
#   ./configure-gnome-keys.sh           show what would change, change nothing (default)
#   ./configure-gnome-keys.sh --apply   apply the configuration
#
# Runs as your own user, because the shortcuts live in your dconf and would land in root's if run under sudo. The
# console keyboard is root owned, so that one part calls sudo itself and will ask for a password.
#
#
# Bindings on bare <Super>
# ------------------------
# A <Super> binding is pressed with the pinky key. Applications are unaffected, since they see Control from the
# thumb key, but the terminal is not: the patched vte reads Super as Control there, and GNOME takes the
# combination first, so a <Super>X binding costs ^X at the shell prompt.
#
# What remains on bare <Super>, and what it costs:
#
#   <Super>1 to <Super>9        switch-to-application-1 to 9, costs nothing, ^1 to ^9 are not control characters
#   <Super>Page_Up/Page_Down    switch-to-workspace-up and down, likewise not control characters
#
# No bare <Super> letter is bound, because every one of them is a readline binding: ^A beginning-of-line, ^N
# next-history, ^V quoted-insert, ^S forward-search-history, ^P previous-history, ^O operate-and-get-next.

set -euo pipefail

MODE=dry-run
case "${1:-}" in
  ""|--dry-run) MODE=dry-run ;;
  --apply)      MODE=apply ;;
  -h|--help)    awk 'NR>1 && !/^#/{exit} NR>1{sub(/^# ?/, ""); print}' "$0"; exit 0 ;;
  *)            echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

CHANGED=0

fail() { echo "configure-gnome-keys: $*" >&2; exit 1; }
# What each binding ends up as, whether this script set it or it kept its default. Collected so that two actions
# landing on the same key can be reported, which gsettings itself allows without complaint.
EFFECTIVE=$(mktemp); trap 'rm -f "$EFFECTIVE"' EXIT

KEYBINDING_SCHEMAS="org.gnome.desktop.wm.keybindings org.gnome.shell.keybindings
                    org.gnome.settings-daemon.plugins.media-keys org.gnome.mutter.keybindings
                    org.gnome.mutter.wayland.keybindings"

# Compares against what gsettings currently reports and acts only on a difference. Desired values are written in
# the form gsettings get prints, so the comparison is a plain string match.
set_key() {
  local schema=$1 key=$2 want=$3 now
  if ! now=$(gsettings get "$schema" "$key" 2>/dev/null); then
    echo "  skip $key, no such key in this GNOME version"
    return 0
  fi
  # The schema list spans lines, so it is collapsed to single spaces before matching. Without that, the schemas
  # sitting at the end of a line are followed by a newline rather than a space and never match.
  case " $(echo $KEYBINDING_SCHEMAS) " in
    *" $schema "*) printf '%s\t%s\t%s\n' "$schema" "$key" "$want" >> "$EFFECTIVE" ;;
  esac
  [ "$now" = "$want" ] && return 0
  printf '  %-34s %-38s -> %s\n' "$key" "$now" "$want"
  [ "$MODE" = apply ] && gsettings set "$schema" "$key" "$want"
  CHANGED=$((CHANGED+1))
  return 0
}

echo "=== keyboard ==="
# swap_lalt_lctl_lwin rotates the three keys left of the space bar, and ralt_rctrl puts Control on the key right
# of it.
#
# <RCTL> is left alone, because that position is the Copilot key and the firmware sends it as Shift+Super+F23.
# The options that would touch it rewrite <RWIN> to Control as well, which would turn that chord into
# Shift+Control+F23 if the firmware sends the right hand Super rather than the left.
#
# terminate:ctrl_alt_bksp makes Ctrl+Alt+Backspace end the session, which after the rotation is pressed with the
# thumb and the key left of it.
set_key org.gnome.desktop.input-sources xkb-options \
  "['ctrl:swap_lalt_lctl_lwin', 'ctrl:ralt_rctrl', 'terminate:ctrl_alt_bksp']"

# The second layout is Slovenian on a US keyboard, reached with the switcher bound under wm.keybindings below.
set_key org.gnome.desktop.input-sources sources "[('xkb', 'us'), ('xkb', 'si+us')]"

# Which key opens the overview when tapped on its own. Either Super, which is the pinky key on both hands.
set_key org.gnome.mutter overlay-key "'Super'"

echo "=== workspaces ==="
# The twelve switch-to-workspace and twelve move-to-workspace bindings below address fixed workspaces by number,
# which needs both of these: dynamic workspaces come and go as windows open, and four of them leave the bindings
# for five upwards with nothing to select.
#
# These are window management settings rather than keyboard ones, so anything else configuring the desktop is
# likely to set them too. Whichever runs last wins, so the values have to agree wherever they appear.
set_key org.gnome.mutter dynamic-workspaces "false"
set_key org.gnome.desktop.wm.preferences num-workspaces "12"

echo "=== org.gnome.desktop.wm.keybindings ==="
set_key org.gnome.desktop.wm.keybindings activate-window-menu           "['<Primary><Shift>Menu']"
set_key org.gnome.desktop.wm.keybindings begin-move                     "['<Primary><Super>m']"
set_key org.gnome.desktop.wm.keybindings begin-resize                   "['<Primary><Super>r']"
set_key org.gnome.desktop.wm.keybindings close                          "['<Primary><Shift>w']"
set_key org.gnome.desktop.wm.keybindings cycle-group                    "['<Primary>Above_Tab']"
set_key org.gnome.desktop.wm.keybindings cycle-group-backward           "['<Primary><Shift>Above_Tab']"
set_key org.gnome.desktop.wm.keybindings cycle-panels                   "@as []"
set_key org.gnome.desktop.wm.keybindings cycle-panels-backward          "@as []"
set_key org.gnome.desktop.wm.keybindings cycle-windows                  "['<Primary>Tab']"
set_key org.gnome.desktop.wm.keybindings cycle-windows-backward         "['<Primary><Shift>Tab']"
set_key org.gnome.desktop.wm.keybindings maximize                       "['<Primary><Alt>Up']"
set_key org.gnome.desktop.wm.keybindings minimize                       "@as []"
set_key org.gnome.desktop.wm.keybindings move-to-monitor-down           "['<Primary><Super>Down']"
set_key org.gnome.desktop.wm.keybindings move-to-monitor-left           "['<Primary><Super>Left']"
set_key org.gnome.desktop.wm.keybindings move-to-monitor-right          "['<Primary><Super>Right']"
set_key org.gnome.desktop.wm.keybindings move-to-monitor-up             "['<Primary><Super>Up']"
set_key org.gnome.desktop.wm.keybindings move-to-workspace-1            "['<Primary><Shift>F1']"
set_key org.gnome.desktop.wm.keybindings move-to-workspace-10           "['<Primary><Shift>F10']"
set_key org.gnome.desktop.wm.keybindings move-to-workspace-11           "['<Primary><Shift>F11']"
set_key org.gnome.desktop.wm.keybindings move-to-workspace-12           "['<Primary><Shift>F12']"
set_key org.gnome.desktop.wm.keybindings move-to-workspace-2            "['<Primary><Shift>F2']"
set_key org.gnome.desktop.wm.keybindings move-to-workspace-3            "['<Primary><Shift>F3']"
set_key org.gnome.desktop.wm.keybindings move-to-workspace-4            "['<Primary><Shift>F4']"
set_key org.gnome.desktop.wm.keybindings move-to-workspace-5            "['<Primary><Shift>F5']"
set_key org.gnome.desktop.wm.keybindings move-to-workspace-6            "['<Primary><Shift>F6']"
set_key org.gnome.desktop.wm.keybindings move-to-workspace-7            "['<Primary><Shift>F7']"
set_key org.gnome.desktop.wm.keybindings move-to-workspace-8            "['<Primary><Shift>F8']"
set_key org.gnome.desktop.wm.keybindings move-to-workspace-9            "['<Primary><Shift>F9']"
set_key org.gnome.desktop.wm.keybindings move-to-workspace-down         "['<Super><Shift>Page_Down']"
set_key org.gnome.desktop.wm.keybindings move-to-workspace-last         "@as []"
set_key org.gnome.desktop.wm.keybindings move-to-workspace-left         "@as []"
set_key org.gnome.desktop.wm.keybindings move-to-workspace-right        "@as []"
set_key org.gnome.desktop.wm.keybindings move-to-workspace-up           "['<Super><Shift>Page_Up']"
set_key org.gnome.desktop.wm.keybindings panel-main-menu                "['<Primary>space']"
set_key org.gnome.desktop.wm.keybindings show-desktop                   "@as []"
set_key org.gnome.desktop.wm.keybindings switch-applications            "@as []"
set_key org.gnome.desktop.wm.keybindings switch-applications-backward   "@as []"
set_key org.gnome.desktop.wm.keybindings switch-group                   "@as []"
set_key org.gnome.desktop.wm.keybindings switch-group-backward          "@as []"
set_key org.gnome.desktop.wm.keybindings switch-input-source            "['<Primary><Alt>space']"
set_key org.gnome.desktop.wm.keybindings switch-input-source-backward   "['<Primary><Shift><Alt>space']"
set_key org.gnome.desktop.wm.keybindings switch-panels                  "['<Primary>Escape']"
set_key org.gnome.desktop.wm.keybindings switch-panels-backward         "['<Primary><Shift>Escape']"
set_key org.gnome.desktop.wm.keybindings switch-to-workspace-1          "['<Primary>F1']"
set_key org.gnome.desktop.wm.keybindings switch-to-workspace-10         "['<Primary>F10']"
set_key org.gnome.desktop.wm.keybindings switch-to-workspace-11         "['<Primary>F11']"
set_key org.gnome.desktop.wm.keybindings switch-to-workspace-12         "['<Primary>F12']"
set_key org.gnome.desktop.wm.keybindings switch-to-workspace-2          "['<Primary>F2']"
set_key org.gnome.desktop.wm.keybindings switch-to-workspace-3          "['<Primary>F3']"
set_key org.gnome.desktop.wm.keybindings switch-to-workspace-4          "['<Primary>F4']"
set_key org.gnome.desktop.wm.keybindings switch-to-workspace-5          "['<Primary>F5']"
set_key org.gnome.desktop.wm.keybindings switch-to-workspace-6          "['<Primary>F6']"
set_key org.gnome.desktop.wm.keybindings switch-to-workspace-7          "['<Primary>F7']"
set_key org.gnome.desktop.wm.keybindings switch-to-workspace-8          "['<Primary>F8']"
set_key org.gnome.desktop.wm.keybindings switch-to-workspace-9          "['<Primary>F9']"
set_key org.gnome.desktop.wm.keybindings switch-to-workspace-down       "['<Super>Page_Down']"
set_key org.gnome.desktop.wm.keybindings switch-to-workspace-last       "@as []"
set_key org.gnome.desktop.wm.keybindings switch-to-workspace-left       "@as []"
set_key org.gnome.desktop.wm.keybindings switch-to-workspace-right      "@as []"
set_key org.gnome.desktop.wm.keybindings switch-to-workspace-up         "['<Super>Page_Up']"
set_key org.gnome.desktop.wm.keybindings switch-windows                 "@as []"
set_key org.gnome.desktop.wm.keybindings switch-windows-backward        "@as []"
set_key org.gnome.desktop.wm.keybindings toggle-maximized               "@as []"
set_key org.gnome.desktop.wm.keybindings unmaximize                     "['<Primary><Alt>Down']"

echo "=== org.gnome.shell.keybindings ==="
set_key org.gnome.shell.keybindings focus-active-notification      "@as []"
set_key org.gnome.shell.keybindings toggle-quick-settings       "['<Primary><Super>s']"
set_key org.gnome.shell.keybindings toggle-application-view        "['<Primary><Super>a']"
set_key org.gnome.shell.keybindings toggle-message-tray            "['<Primary><Super>n']"

# The key with a cog on the function row sends XF86AudioMedia, so it is routed to Settings, and the -static
# entry carrying its default action is cleared or it would keep doing two things at once.
#
# The key with a monitor sends the Windows projector chord, Super and P, which the rotation turns into Alt and P.
# It is left unbound: both events come from the ordinary keyboard on the ordinary scancodes, so binding it would
# reserve Alt and P system wide and take that combination away from applications.
set_key org.gnome.settings-daemon.plugins.media-keys control-center "['XF86AudioMedia']"
set_key org.gnome.settings-daemon.plugins.media-keys media          "['']"
set_key org.gnome.settings-daemon.plugins.media-keys media-static   "['']"

# volume-down, volume-up, volume-mute and play are not set here. The hardware keys are bound through the
# matching -static entries, so setting these would bind the same physical key to the same action twice.
echo "=== org.gnome.settings-daemon.plugins.media-keys ==="
set_key org.gnome.settings-daemon.plugins.media-keys help                           "@as []"
set_key org.gnome.settings-daemon.plugins.media-keys logout                         "['<Primary><Super>BackSpace']"
set_key org.gnome.settings-daemon.plugins.media-keys magnifier                      "@as []"
set_key org.gnome.settings-daemon.plugins.media-keys magnifier-zoom-in              "@as []"
set_key org.gnome.settings-daemon.plugins.media-keys magnifier-zoom-out             "@as []"
set_key org.gnome.settings-daemon.plugins.media-keys screenreader                   "@as []"
set_key org.gnome.settings-daemon.plugins.media-keys rotate-video-lock-static "['XF86RotationLockToggle']"
set_key org.gnome.settings-daemon.plugins.media-keys screensaver                    "['<Primary><Super>l']"
set_key org.gnome.settings-daemon.plugins.media-keys terminal                       "@as []"

echo "=== org.gnome.mutter.keybindings ==="
# Unbound. Its cycle builds display layouts rather than restoring the stored ones, choosing its own primary
# output and writing the result over monitors.xml, so there is no way back to a saved arrangement through it.
# Its <Super>p accelerator is also the chord the monitor key on the function row sends.
set_key org.gnome.mutter.keybindings switch-monitor            "@as []"
set_key org.gnome.mutter.keybindings toggle-tiled-left              "['<Primary><Alt>Left']"
set_key org.gnome.mutter.keybindings toggle-tiled-right             "['<Primary><Alt>Right']"

echo "=== org.gnome.mutter.wayland.keybindings ==="
set_key org.gnome.mutter.wayland.keybindings restore-shortcuts              "@as []"

# Anything not set above keeps whatever GNOME ships, which still counts when looking for clashes.
for schema in $KEYBINDING_SCHEMAS; do
  for key in $(gsettings list-keys "$schema"); do
    grep -qP "^\Q$schema\E\t\Q$key\E\t" "$EFFECTIVE" && continue
    printf '%s\t%s\t%s\n' "$schema" "$key" "$(gsettings get "$schema" "$key")" >> "$EFFECTIVE"
  done
done

conflicts=$(python3 - "$EFFECTIVE" <<'PY'
import ast, collections, sys

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

# Accelerators are compared as GTK parses them, not as text. <Primary>, <Control> and <Ctrl> are three spellings
# of one modifier, and GNOME's defaults and these settings do not agree on which to use, so comparing strings
# would call a chord free while it is already taken under another name.
def canonical(accel):
    ok, keyval, mods = Gtk.accelerator_parse(accel)
    return (keyval, int(mods)) if ok and keyval else None

seen = collections.defaultdict(list)
for line in open(sys.argv[1]):
    schema, key, value = line.rstrip("\n").split("\t", 2)
    if value.startswith("@as"):
        continue
    try:
        accels = ast.literal_eval(value)
    except Exception:
        continue
    if not isinstance(accels, (list, tuple)):
        continue
    for accel in accels:
        form = canonical(accel) if accel else None
        if form:
            seen[form].append((accel, schema.replace("org.gnome.", "") + " " + key))

for form, owners in sorted(seen.items()):
    if len(owners) > 1:
        shown = ", ".join("%s (%s)" % (o, a) for a, o in sorted(owners, key=lambda t: t[1]))
        print("  %-30s %s" % (owners[0][0], shown))
PY
)
if [ -n "$conflicts" ]; then
  echo "=== the same key bound to more than one action ==="
  echo "$conflicts"
  echo
fi

echo "=== console ==="
# The TTY reads its layout from /etc/default/keyboard, which is root owned, so this part goes through sudo and
# will ask for a password.
#
# Its options are not the ones above. Only alt and win are swapped, which leaves Control physically where it is
# printed, so the console needs no patched terminal to send control characters. It also leaves the right Alt as
# AltGr, which matters for the second layout: ckbcomp folds the second group onto AltGr rather than giving it a
# toggle, so Slovenian is reached there by holding AltGr instead of switching layout as in the session.
KEYBOARD=/etc/default/keyboard

console_set() {
  local key=$1 want=$2 have
  have=$(grep -m1 "^$key=" "$KEYBOARD" 2>/dev/null | cut -d= -f2- | tr -d '"')
  [ "$have" = "$want" ] && return 0
  printf '  %-12s %-24s -> %s\n' "$key" "${have:-empty}" "$want"
  CHANGED=$((CHANGED+1))
  [ "$MODE" = apply ] || return 0
  if grep -q "^$key=" "$KEYBOARD"; then
    sudo sed -i "s|^$key=.*|$key=\"$want\"|" "$KEYBOARD" || fail "could not edit $KEYBOARD"
  else
    printf '%s="%s"\n' "$key" "$want" | sudo tee -a "$KEYBOARD" >/dev/null || fail "could not append to $KEYBOARD"
  fi
  CONSOLE_TOUCHED=yes
  return 0
}

CONSOLE_TOUCHED=no
if [ "$MODE" = apply ] && [ ! -f "$KEYBOARD.bak" ]; then
  sudo cp -a "$KEYBOARD" "$KEYBOARD.bak" 2>/dev/null || true
fi
console_set XKBLAYOUT  "us,si"
console_set XKBVARIANT ",us"
console_set XKBOPTIONS "altwin:swap_alt_win"
if [ "$CONSOLE_TOUCHED" = yes ]; then
  echo "  reloading the console keymap"
  sudo dpkg-reconfigure -f noninteractive keyboard-configuration || fail "dpkg-reconfigure failed"
fi

echo
if [ "$CHANGED" -eq 0 ]; then
  echo "already configured, nothing to change"
elif [ "$MODE" = apply ]; then
  echo "applied $CHANGED change(s). The keyboard rotation takes effect on the next login."
else
  echo "$CHANGED change(s) would be made. Nothing was written, re-run with --apply."
fi
