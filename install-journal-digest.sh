#!/usr/bin/env bash
#
# Install journal-digest.py, its ignore patterns and its daily cron job from this repository as the root-owned copies
# that root runs. Only a file which differs from its installed copy is replaced.
#
#   journal-digest.py           ->  /usr/local/bin/journal-digest
#   journal-digest.ignore       ->  /etc/journal-digest/ignore
#   cron.daily/journal-digest   ->  /etc/cron.daily/journal-digest
#
#
# Usage
# -----
#   ./install-journal-digest.sh           show what would change, change nothing (default)
#   ./install-journal-digest.sh --apply   install
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

echo "=== journal-digest ==="
install_file "$REPO_DIR/journal-digest.py"         /usr/local/bin/journal-digest  755
install_file "$REPO_DIR/journal-digest.ignore"     /etc/journal-digest/ignore     644
install_file "$REPO_DIR/cron.daily/journal-digest" /etc/cron.daily/journal-digest 755

echo
if [ "$CHANGED" -eq 0 ]; then
  echo "already installed, nothing to change"
elif [ "$MODE" = apply ]; then
  echo "installed $CHANGED item(s)"
else
  echo "$CHANGED item(s) would be installed. Nothing was written, re-run with --apply."
fi
