#!/usr/bin/env bash
#
# Install rebuild-patched-packages.sh, its patches and its daily cron job from this repository as the root-owned
# copies that root runs, and give the sbuild user its builds run as the subordinate ids building in unshare mode
# needs. Also set up the local repository the rebuilds are published to, and let unattended-upgrades install from
# it. Only what differs from the installed copy is replaced.
#
#   rebuild-patched-packages.sh                         ->  /usr/local/bin/rebuild-patched-packages
#   patches/                                            ->  /usr/local/share/patched-packages
#   cron.daily/rebuild-patched-packages                 ->  /etc/cron.daily/rebuild-patched-packages
#   sources.list.d/local.list                           ->  /etc/apt/sources.list.d/local.list
#   apt.conf.d/52unattended-upgrades-local-repository   ->  /etc/apt/apt.conf.d/52unattended-upgrades-local-repository
#
# See Installing in rebuild-patched-packages.sh for why root does not run the repository directly.
#
#
# Usage
# -----
#   ./install-rebuild-patched-packages.sh           show what would change, change nothing (default)
#   ./install-rebuild-patched-packages.sh --apply   install
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
  sudo install -o root -g root -m "$mode" "$src" "$dst"
  return 0
}

# Replaces a directory with a copy of another when their contents differ. The copy is made next to the target and
# renamed into place, so that the target is missing only between two renames. Symlinks are copied as the files
# they point to, so that nothing in the installed copy leads back to files your user can change, and every file
# is made readable, so that the rebuild script can report on it without root.
install_dir() {
  local src=$1 dst=$2
  diff -rq "$src" "$dst" >/dev/null 2>&1 && return 0
  echo "  install $dst"
  CHANGED=$((CHANGED+1))
  [ "$MODE" = apply ] || return 0
  sudo rm -rf "$dst.new" "$dst.old"
  sudo cp -rL "$src" "$dst.new"
  sudo chmod -R u=rwX,go=rX "$dst.new"
  if [ -e "$dst" ]; then
    sudo mv "$dst" "$dst.old"
  fi
  sudo mv "$dst.new" "$dst"
  sudo rm -rf "$dst.old"
  return 0
}

echo "=== rebuild-patched-packages ==="
# The unshare mode of sbuild and mmdebstrap maps the users inside the build chroot onto subordinate ids of the
# build user. A range is taken above every range already given out.
if ! getent passwd sbuild >/dev/null; then
  echo "install-rebuild-patched-packages: the sbuild user does not exist, install the sbuild package first" >&2
  exit 1
fi
if ! grep -q '^sbuild:' /etc/subuid || ! grep -q '^sbuild:' /etc/subgid; then
  first=$(awk -F: '{ end = $2 + $3; if (end > last) last = end } END { print (last < 100000 ? 100000 : last) }' \
    /etc/subuid /etc/subgid)
  range="$first-$((first + 65535))"
  echo "  add subordinate ids $range for sbuild"
  CHANGED=$((CHANGED+1))
  if [ "$MODE" = apply ]; then
    sudo usermod --add-subuids "$range" --add-subgids "$range" sbuild
  fi
fi
install_dir  "$REPO_DIR/patches"                             /usr/local/share/patched-packages
install_file "$REPO_DIR/rebuild-patched-packages.sh"         /usr/local/bin/rebuild-patched-packages  755
install_file "$REPO_DIR/cron.daily/rebuild-patched-packages" /etc/cron.daily/rebuild-patched-packages 755

echo "=== local repository ==="
# apt update fails on a source whose index is missing, so a repository without one gets an empty index.
if [ ! -e /usr/local/lib/debs/Packages.gz ]; then
  echo "  create an empty index in /usr/local/lib/debs"
  CHANGED=$((CHANGED+1))
  if [ "$MODE" = apply ]; then
    sudo install -d -o root -g root -m 755 /usr/local/lib/debs
    gzip -9n < /dev/null | sudo tee /usr/local/lib/debs/Packages.gz >/dev/null
  fi
fi
install_file "$REPO_DIR/sources.list.d/local.list" /etc/apt/sources.list.d/local.list 644
install_file "$REPO_DIR/apt.conf.d/52unattended-upgrades-local-repository" \
             /etc/apt/apt.conf.d/52unattended-upgrades-local-repository 644

echo
if [ "$CHANGED" -eq 0 ]; then
  echo "already installed, nothing to change"
elif [ "$MODE" = apply ]; then
  echo "installed $CHANGED item(s)"
else
  echo "$CHANGED item(s) would be installed. Nothing was written, re-run with --apply."
fi
