#!/usr/bin/env bash
#
# Install health-benchmark.py, the pinned 7-Zip build it benchmarks with, and the systemd timer which runs it once a
# day, as root-owned copies that root runs. Only what differs from the installed copy is replaced.
#
#   health-benchmark.py                 ->  /usr/local/bin/health-benchmark
#   7-Zip 26.05, static build (7zzs)    ->  /usr/local/lib/health-benchmark/7zzs
#   systemd/health-benchmark.service    ->  /etc/systemd/system/health-benchmark.service
#   systemd/health-benchmark.timer      ->  /etc/systemd/system/health-benchmark.timer
#
# 7-Zip is downloaded from its release on GitHub and installed only when both the archive and the binary in it have
# the checksums recorded below. The report compares only measurements made with the same binary, so changing the
# pinned version starts the comparison over.
#
#
# Usage
# -----
#   ./install-health-benchmark.sh           show what would change, change nothing (default)
#   ./install-health-benchmark.sh --apply   install
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

SEVENZIP_VERSION=26.05
SEVENZIP_ARCHIVE=7z2605-linux-x64.tar.xz
SEVENZIP_URL=https://github.com/ip7z/7zip/releases/download/$SEVENZIP_VERSION/$SEVENZIP_ARCHIVE
# The archive's checksum matches the digest GitHub publishes for the release asset.
SEVENZIP_ARCHIVE_SHA256=22cf82fcc6eb7474f1d5bb4f02c0f8ebd573a718089d6833dc30f8350a3b232e
SEVENZIP_BINARY_SHA256=8952c73646dc8d4abfb31e48a949ba0ac15cb2437da9e4430886ddb6e12b383b
SEVENZIP_DST=/usr/local/lib/health-benchmark/7zzs

install_file() {
  local src=$1 dst=$2 mode=$3
  cmp -s "$src" "$dst" && return 0
  echo "  install $dst"
  CHANGED=$((CHANGED+1))
  [ "$MODE" = apply ] || return 0
  sudo install -o root -g root -m "$mode" "$src" "$dst"
  return 0
}

# Collects missing packages, so that apt runs once for all of them.
want_package() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'ok installed' && return 0
  echo "  install package $1"
  MISSING_PACKAGES+=("$1")
  CHANGED=$((CHANGED+1))
  return 0
}

# Installs a systemd unit from systemd/ in this repository into the root-owned /etc/systemd/system through sudo.
install_unit() {
  local name=$1 dst=/etc/systemd/system/$1
  cmp -s "$REPO_DIR/systemd/$name" "$dst" && return 0
  echo "  install $dst"
  CHANGED=$((CHANGED+1))
  [ "$MODE" = apply ] || return 0
  sudo install -o root -g root -m 644 "$REPO_DIR/systemd/$name" "$dst"
  sudo systemctl daemon-reload
  return 0
}

echo "=== packages ==="
MISSING_PACKAGES=()
# framework_tool sets the fan, perf runs the context switch and system call benchmarks, powerprofilesctl holds the
# power profile, and taskset places the perf benchmarks on one CPU.
want_package framework-tool
want_package linux-perf
want_package power-profiles-daemon
want_package util-linux
if [ "$MODE" = apply ] && [ "${#MISSING_PACKAGES[@]}" -gt 0 ]; then
  sudo apt-get install "${MISSING_PACKAGES[@]}"
fi

echo "=== 7-Zip $SEVENZIP_VERSION ==="
if ! echo "$SEVENZIP_BINARY_SHA256  $SEVENZIP_DST" | sha256sum --check --status >/dev/null 2>&1; then
  echo "  install $SEVENZIP_DST"
  CHANGED=$((CHANGED+1))
  if [ "$MODE" = apply ]; then
    work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT
    curl --fail --silent --show-error --location --output "$work/$SEVENZIP_ARCHIVE" "$SEVENZIP_URL"
    echo "$SEVENZIP_ARCHIVE_SHA256  $work/$SEVENZIP_ARCHIVE" | sha256sum --check --quiet
    tar --extract --xz --file "$work/$SEVENZIP_ARCHIVE" --directory "$work" 7zzs
    echo "$SEVENZIP_BINARY_SHA256  $work/7zzs" | sha256sum --check --quiet
    sudo install -D -o root -g root -m 755 "$work/7zzs" "$SEVENZIP_DST"
  fi
fi

echo "=== health-benchmark ==="
install_file "$REPO_DIR/health-benchmark.py" /usr/local/bin/health-benchmark 755
install_unit health-benchmark.service
install_unit health-benchmark.timer
if ! systemctl is-enabled --quiet health-benchmark.timer 2>/dev/null; then
  echo "  enable health-benchmark.timer"
  CHANGED=$((CHANGED+1))
  [ "$MODE" = apply ] && sudo systemctl enable --now --quiet health-benchmark.timer
fi

echo
if [ "$CHANGED" -eq 0 ]; then
  echo "already installed, nothing to change"
elif [ "$MODE" = apply ]; then
  echo "installed $CHANGED item(s)"
else
  echo "$CHANGED item(s) would be installed. Nothing was written, re-run with --apply."
fi
