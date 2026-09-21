#!/usr/bin/env bash
#
# Run every script of this repository in the order they have to be applied, as described under "Applying
# everything" in README.md. The BIOS settings described there cannot be applied from here and come first.
#
#
# Usage
# -----
#   ./all.sh           show what each script would change, change nothing (default)
#   ./all.sh --apply   apply everything, stopping at the first step which fails
#
# Runs as your own user. The steps which need root run through sudo, which will ask for a password.

set -euo pipefail

MODE=dry-run
case "${1:-}" in
  ""|--dry-run) MODE=dry-run ;;
  --apply)      MODE=apply ;;
  -h|--help)    awk 'NR>1 && !/^#/{exit} NR>1{sub(/^# ?/, ""); print}' "$0"; exit 0 ;;
  *)            echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

cd "$(dirname "$(readlink -f "$0")")"

step() {
  echo
  echo "########## $* ##########"
  echo
}

echo "The BIOS settings from README.md have to be made by hand: battery charge limit 99% and Battery Extender disabled."

if [ "$MODE" = apply ]; then
  step "APT lockdown"
  sudo ./lockdown-apt-sources.sh --apply

  step "Patched packages"
  ./install-rebuild-patched-packages.sh --apply
  sudo rebuild-patched-packages --build
  sudo apt upgrade

  step "Desktop"
  ./configure-gnome-desktop.sh --apply

  step "Keyboard"
  ./configure-gnome-keys.sh --apply

  step "Terminal"
  ./configure-ptyxis.sh --apply

  echo
  echo "Done. Log out and back in, or reboot, so that newly installed packages and extensions are loaded."
else
  step "APT lockdown"
  ./lockdown-apt-sources.sh

  # The installed copy of the rebuild script may not exist yet, so the report comes from the repository's copy,
  # which also lists the installed copies that differ from it.
  step "Patched packages"
  ./install-rebuild-patched-packages.sh
  ./rebuild-patched-packages.sh

  step "Desktop"
  ./configure-gnome-desktop.sh

  step "Keyboard"
  ./configure-gnome-keys.sh

  step "Terminal"
  ./configure-ptyxis.sh
fi
