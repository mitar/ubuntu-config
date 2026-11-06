#!/usr/bin/env bash
#
# Install fetch-vendor-debs.py, the keys it verifies downloads with and its daily cron job from this repository as the
# root-owned copies that root runs. Only a file which differs from its installed copy is replaced.
#
#   fetch-vendor-debs.py           ->  /usr/local/bin/fetch-vendor-debs
#   vendor-debs/                   ->  /usr/local/share/vendor-debs
#   cron.daily/fetch-vendor-debs   ->  /etc/cron.daily/fetch-vendor-debs
#
# The downloads are published to the local repository, which install-rebuild-patched-packages.sh sets up, so that
# has to be installed first. See Installing in fetch-vendor-debs.py for why root does not run the repository directly.
#
#
# Usage
# -----
#   ./install-fetch-vendor-debs.sh           show what would change, change nothing (default)
#   ./install-fetch-vendor-debs.sh --apply   install
#
# Runs as your own user and calls sudo for the writes, which will ask for a password.

set -euo pipefail

MODE=dry-run
case "${1:-}" in
  ""|--dry-run) MODE=dry-run ;;
  --apply)      MODE=apply ;;
  -h|--help)    awk 'NR>1 && !/^#/{exit} NR>1{sub(/^# ?/, ""); print}' "$0"; exit 0 ;;
  *)            echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

REPO_DIR=$(dirname "$(readlink -f "$0")")
CHANGED=0

install_file() {
  local src=$1 dst=$2 mode=$3
  cmp -s "$src" "$dst" && return 0
  echo "  install $dst"
  CHANGED=$((CHANGED+1))
  [ "$MODE" = apply ] || return 0
  sudo install -D -o root -g root -m "$mode" "$src" "$dst"
  return 0
}

echo "=== fetch-vendor-debs ==="
# A dry run only reports it, since on a fresh system the local repository is created by applying the patched packages
# step, which all.sh does first.
if [ ! -d /usr/local/lib/debs ]; then
  if [ "$MODE" = apply ]; then
    echo "install-fetch-vendor-debs: the local repository /usr/local/lib/debs does not exist, set it up with" \
         "install-rebuild-patched-packages.sh first" >&2
    exit 1
  fi
  echo "  the local repository /usr/local/lib/debs does not exist yet, install-rebuild-patched-packages.sh sets it up"
fi
install_file "$REPO_DIR/fetch-vendor-debs.py"         /usr/local/bin/fetch-vendor-debs  755
for key in "$REPO_DIR"/vendor-debs/*; do
  install_file "$key" "/usr/local/share/vendor-debs/$(basename "$key")" 644
done
install_file "$REPO_DIR/cron.daily/fetch-vendor-debs" /etc/cron.daily/fetch-vendor-debs 755

echo
if [ "$CHANGED" -eq 0 ]; then
  echo "already installed, nothing to change"
elif [ "$MODE" = apply ]; then
  echo "installed $CHANGED item(s)"
else
  echo "$CHANGED item(s) would be installed. Nothing was written, re-run with --apply."
fi
