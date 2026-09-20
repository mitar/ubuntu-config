#!/usr/bin/env bash
#
# Rebuild locally patched packages when Ubuntu ships a newer version of them.
#
# For each package under patches/, compare the version Ubuntu currently offers against the version last built
# here. When Ubuntu has moved ahead, fetch that source, apply the local patches on top of the ones the package
# already carries, build it, and publish the result to the local apt repository. Nothing is installed: the
# rebuilt package simply becomes the candidate, so the next ordinary apt upgrade picks it up.
#
# This only works because the local repository outranks the archive (Pin-Priority 1001, see
# lockdown-apt-sources.sh). That is what stops an unpatched Ubuntu build from replacing a patched one. The
# consequence is that a package stays on the old version until a rebuild succeeds, so a silent failure would
# mean silently missing security updates. Every failure is therefore loud: it goes to stderr for cron to mail,
# and to syslog as well so that a missing mail transport cannot swallow it.
#
#
# Usage
# -----
#   ./rebuild-patched-packages.sh            report what is out of date, change nothing (default)
#   sudo ./rebuild-patched-packages.sh --build    rebuild whatever is out of date
#   sudo ./rebuild-patched-packages.sh --build --force    rebuild everything regardless
#   ./rebuild-patched-packages.sh --status   show recorded state for each package
#   ./rebuild-patched-packages.sh --prereqs  list everything still to be installed, including build dependencies
#
# Every mode reports missing prerequisites. Only --build treats them as fatal, and it names the whole install in
# one message rather than stopping at the first gap, so that a cron mail is actionable on its own.
#
# --build prints nothing when everything is current, so a daily cron job stays quiet until there is something to
# say. It prints a summary when it rebuilds, so that a successful rebuild is still mailed as a notification.
#
#
# Adding a package
# ----------------
# Create patches/<source-package>/ containing the patch files and a "series" file naming them in the order they
# apply. The directory name has to be the source package name, which is what apt-get source takes. Optionally
# add a "build-options" file whose contents are used as DEB_BUILD_OPTIONS. Do not put nocheck there: the tests
# are how a stale patch is caught. Parallelism does not belong there either, it is passed as -j from BUILD_JOBS.
# A README in that directory is a good place to record what the patch does and what to re-check after a version
# bump.
#
# A patch that applies is not a patch that works, and the build itself is what says so. Two things catch a patch
# that has gone stale, and neither needs any machinery here:
#
#   the build fails   dpkg-source refuses a patch needing fuzz, and a hunk that landed somewhere plausible but
#                     wrong will not compile.
#   the tests fail    a patch should add tests for its own behaviour to the package's test suite, which then run
#                     during the build like any other. This is what catches a patch that still applies, still
#                     compiles, and has been made inert by a change elsewhere.
#
#
# Prerequisites, installed by hand rather than by this script
# ------------------------------------------------------------
#   (deb-src is not listed here any more. A build enables it on the Ubuntu sources itself and runs apt update,
#    because ubuntu-pro-client rewrites its sources without it and a manual fix does not survive that.)
#   a mail transport  cron mails output through it. Without one, failures still reach syslog, but no mail is
#                     sent. Set MAILTO in the crontab to choose the recipient.
#   devscripts        provides dch, used to append the local version suffix.
#
# Build dependencies are installed automatically per package by apt-get build-dep, since they change over time.
#
#
# Versioning
# ----------
# The rebuild carries a local suffix, so 0.84.0-2 becomes 0.84.0-2+patched1. That sorts above the Ubuntu version
# it was built from and below Ubuntu's next revision, which is what makes a new Ubuntu release register as
# "newer upstream" here and trigger a rebuild rather than being masked forever by the pin.

set -euo pipefail

REPO_DIR=$(dirname "$(readlink -f "$0")")
PATCH_DIR=$REPO_DIR/patches
LOCAL_REPO=/usr/local/lib/debs
STATE_DIR=/var/lib/patched-packages
SOURCES_DIR=/etc/apt/sources.list.d
BUILD_ROOT=/var/tmp
VERSION_SUFFIX=+patched
# Parallelism has to be passed as -j. Setting parallel= in DEB_BUILD_OPTIONS does not survive, because
# dpkg-buildpackage rewrites that variable from its own -j handling, and debhelper then falls back to ninja -j1.
BUILD_JOBS=$(nproc 2>/dev/null || echo 1)

# Commands the rebuild needs, each with the package providing it. Everything missing is reported in one go, so a
# failure mail names the whole install rather than whichever thing was noticed first.
REQUIRED_COMMANDS="apt-get:apt apt-cache:apt patch:patch gzip:gzip dpkg-deb:dpkg dpkg-scanpackages:dpkg-dev
                   dpkg-buildpackage:dpkg-dev dch:devscripts fakeroot:fakeroot g++:build-essential"

MODE=check
FORCE=no
for arg in "$@"; do
  case "$arg" in
    --build)   MODE=build ;;
    --status)  MODE=status ;;
    --prereqs) MODE=prereqs ;;
    --check)   MODE=check ;;
    --force)   FORCE=yes ;;
    -h|--help) awk 'NR>1 && !/^#/{exit} NR>1{sub(/^# ?/, ""); print}' "$0"; exit 0 ;;
    *)         echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# Failures have to be impossible to miss, because the pin means a stale package is silently kept rather than
# replaced. Syslog is used alongside stderr so that the report survives a missing mail transport.
CLEANUP=""
cleanup() { [ -n "$CLEANUP" ] && rm -rf $CLEANUP; return 0; }
trap cleanup EXIT

fail() {
  echo "rebuild-patched-packages: $*" >&2
  command -v logger >/dev/null && logger -p daemon.err -t rebuild-patched-packages -- "$*" || true
  exit 1
}

# Collect everything standing between the current system and a successful rebuild. Prints a human readable
# reason per item and leaves the packages to install in MISSING_PACKAGES, so callers can decide whether a given
# gap is fatal. Only cheap checks belong here, since this runs in every mode.
MISSING_PACKAGES=""
check_prereqs() {
  MISSING_PACKAGES=""
  local entry cmd pkg

  for entry in $REQUIRED_COMMANDS; do
    cmd=${entry%%:*}; pkg=${entry##*:}
    command -v "$cmd" >/dev/null 2>&1 && continue
    echo "  missing command $cmd, provided by the $pkg package"
    case " $MISSING_PACKAGES " in *" $pkg "*) ;; *) MISSING_PACKAGES="$MISSING_PACKAGES $pkg" ;; esac
  done

  # Source indexes are what apt-get source reads. Deliberately only reported, never fixed here: editing apt
  # sources is the business of whoever installs this, not of a job that runs unattended every night.
  local targets
  targets=$(apt-get indextargets --format '$(CREATED_BY)' 2>/dev/null || true)
  if ! grep -qx Sources <<<"$targets"; then
    echo "  deb-src is not enabled, so apt-get source cannot fetch anything"
    echo "      in deb822 format that is a Types: edit, so Types: deb becomes Types: deb deb-src"
    echo "      in /etc/apt/sources.list.d/ubuntu.sources, followed by apt update"
  fi

  # Not fatal. Failures still reach syslog, they just will not reach an inbox.
  if ! command -v sendmail >/dev/null 2>&1 && [ ! -x /usr/sbin/sendmail ]; then
    echo "  no mail transport, so cron cannot mail anything and failures reach syslog only"
    echo "      install one, for example msmtp-mta for relaying or postfix for a local setup,"
    echo "      and assign MAILTO in /etc/anacrontab to choose the recipient, since anacron runs cron.daily"
  fi

  [ -d "$LOCAL_REPO" ] || echo "  local repository $LOCAL_REPO does not exist"
}

# Build dependencies are installed per package by apt-get build-dep during a rebuild, so they never block. This
# reports them anyway, because the first rebuild of a package can pull in a great deal and it is better to know
# that in advance than to discover it from a cron job at three in the morning.
report_build_deps() {
  local src=$1 version=$2 sim count
  sim=$(apt-get build-dep --simulate --only-source "$src=$version" 2>/dev/null | awk '/^Inst /{print $2}' | sort -u)
  count=$(printf '%s' "$sim" | grep -c . || true)
  if [ "$count" -eq 0 ]; then
    echo "  $src: build dependencies already satisfied"
  else
    echo "  $src: $count package(s) would be installed by apt-get build-dep"
    printf '%s\n' "$sim" | tr '\n' ' ' | fold -s -w 110 | sed 's/^/      /'
  fi
}

# Every Ubuntu source has to offer deb-src, or a rebuild cannot fetch what it is rebuilding. Enabling it once by
# hand is not enough: ubuntu-pro-client rewrites its own sources whenever it runs, writing them without deb-src,
# while the Ubuntu Pro pins place those sources above the archive. A security update could then be installed and
# never rebuilt. The sources are therefore checked on every run, and corrected during a build.
#
# Only Ubuntu origins are touched. A third party repository may publish no source at all, and asking apt for an
# index that does not exist turns every later apt update into an error.
ensure_deb_src() {
  local mode=$1 report
  report=$(python3 - "$SOURCES_DIR" "$mode" <<'PY'
import glob, os, sys

sources_dir, mode = sys.argv[1], sys.argv[2]

for path in sorted(glob.glob(os.path.join(sources_dir, "*.sources"))):
    with open(path) as handle:
        text = handle.read()

    # A deb822 file holds several stanzas and only the Ubuntu ones should gain deb-src, so each is judged alone.
    stanzas = text.split("\n\n")
    changed = False
    for i, stanza in enumerate(stanzas):
        lines = stanza.splitlines()
        uris = [l for l in lines if l.lower().startswith("uris:")]
        types = [l for l in lines if l.lower().startswith("types:")]
        if not uris or not types or ".ubuntu.com" not in uris[0] or "deb-src" in types[0]:
            continue
        lines[lines.index(types[0])] = types[0].rstrip() + " deb-src"
        stanzas[i] = "\n".join(lines)
        changed = True

    if not changed:
        continue
    if mode == "apply":
        # .bak is one of the suffixes apt is configured to ignore silently, so the backup can sit next to
        # the original without apt complaining about it on every run.
        os.replace(path, path + ".bak")
        with open(path, "w") as handle:
            handle.write("\n\n".join(stanzas))
        print("  enabled deb-src in %s, previous kept alongside as .bak" % path)
    else:
        print("  would enable deb-src in %s" % path)
PY
) || fail "could not inspect the apt sources for deb-src"

  [ -z "$report" ] && return 0
  echo "$report"
  [ "$mode" = apply ] && { apt-get update -qq || fail "apt-get update failed after enabling deb-src"; }
  return 0
}

# Loud but not fatal. Used where continuing is still better than stopping, but silence would not be.
warn() {
  echo "rebuild-patched-packages: $*" >&2
  command -v logger >/dev/null && logger -p daemon.warning -t rebuild-patched-packages -- "$*" || true
  return 0
}

# official_version reads the source indexes, so an update published only as a binary is invisible to it. ESM is
# the case that matters: ubuntu-pro-client writes its sources with Types: deb and no deb-src, while pinning them
# at 510, above the archive. Once ESM starts carrying a package patched here, a security update there would leave
# this script reporting the package as current while the local build stayed behind, which is the one failure the
# whole arrangement exists to prevent. Comparing against the binary versions catches it.
#
# Prints the newer binary version when there is one, nothing otherwise. Local builds are excluded by matching
# ubuntu.com origins rather than by version, since a local build carries the suffix anyway.
unfetchable_update() {
  local src=$1 srcver=$2 bins bin ver origin best=""
  bins=$(apt-cache showsrc "$src" 2>/dev/null | awk '/^Binary:/{ $1=""; gsub(/,/," "); print; exit }')
  for bin in $bins; do
    while IFS='|' read -r _ ver origin; do
      ver=${ver// /}
      case "$origin" in *ubuntu.com*) ;; *) continue ;; esac
      if [ -z "$best" ] || dpkg --compare-versions "$ver" gt "$best"; then best=$ver; fi
    done < <(apt-cache madison "$bin" 2>/dev/null)
  done
  [ -n "$best" ] && dpkg --compare-versions "$best" gt "$srcver" && echo "$best"
  return 0
}

# The source package names of everything with a patch directory.
patched_packages() {
  local d
  for d in "$PATCH_DIR"/*/; do
    [ -d "$d" ] || continue
    basename "$d"
  done
}

# The version Ubuntu currently offers for a source package. Only archive origins are considered, so that our own
# rebuild sitting in the local repository is not mistaken for the upstream version and cannot suppress a rebuild.
official_version() {
  apt-cache showsrc "$1" 2>/dev/null \
    | awk '/^Package:/{p=$2} /^Version:/{v=$2} /^$/{if (p && v) print v; p=v=""} END{if (p && v) print v}' \
    | grep -v "$VERSION_SUFFIX" \
    | sort -V | tail -1 || true
}

# The official version the last successful build was made from, or empty if this package has never been built.
built_version() {
  cat "$STATE_DIR/$1.built" 2>/dev/null || true
}

# Add our patches to the package's own series and let dpkg-source apply them, which is exactly what the build
# does. Running it here, before the build dependencies are installed, means a broken patch is reported in seconds
# instead of after a long dependency install, and the message is dpkg's own rather than a reinterpretation.
#
# dpkg-source applies with patch -t -F 0, so a patch needing any fuzz is refused outright. There is nothing to
# configure about that and no point detecting it separately: a hunk that no longer matches its context stops the
# build either way.
stage_patches() {
  local src=$1 tree=$2 p out
  mkdir -p "$tree/debian/patches"
  touch "$tree/debian/patches/series"

  while read -r p; do
    [ -n "$p" ] || continue
    case "$p" in \#*) continue ;; esac
    [ -r "$PATCH_DIR/$src/$p" ] || fail "$src: series lists $p but the file is missing"
    cp "$PATCH_DIR/$src/$p" "$tree/debian/patches/$p"
    echo "$p" >> "$tree/debian/patches/series"
  done < "$PATCH_DIR/$src/series"

  out=$( cd "$tree" && dpkg-source --before-build . 2>&1 ) || {
    printf '%s\n' "$out" >&2
    fail "$src: patches do not apply, rebuild skipped and the installed version left alone"
  }
}

# Drop any older build of the binary packages we just produced, so the local repository holds one version of each
# rather than growing without bound. Only names we actually rebuilt are touched.
prune_old_debs() {
  local new=$1 f name
  for f in "$LOCAL_REPO"/*.deb; do
    [ -e "$f" ] || continue
    name=$(dpkg-deb -f "$f" Package 2>/dev/null) || continue
    if grep -qxF "$name" "$new" && ! grep -qxF "$f" "$new.files"; then
      rm -f "$f"
    fi
  done
}

rebuild() {
  local src=$1 version=$2
  local work
  work=$(mktemp -d "$BUILD_ROOT/rebuild-$src.XXXXXX")
  CLEANUP="$CLEANUP $work"

  ( cd "$work" && apt-get source --only-source "$src=$version" ) >/dev/null 2>&1 \
    || fail "$src: apt-get source failed for $version (is deb-src enabled?)"

  local tree
  tree=$(find "$work" -maxdepth 1 -mindepth 1 -type d | head -1)
  [ -n "$tree" ] || fail "$src: apt-get source produced no source tree"

  stage_patches "$src" "$tree"

  apt-get build-dep -y --only-source "$src=$version" >/dev/null 2>&1 \
    || fail "$src: apt-get build-dep failed for $version"

  local opts=""
  [ -r "$PATCH_DIR/$src/build-options" ] && opts=$(cat "$PATCH_DIR/$src/build-options")

  ( cd "$tree" \
      && DEBEMAIL="${DEBEMAIL:-root@$(hostname -f 2>/dev/null || hostname)}" \
         DEBFULLNAME="${DEBFULLNAME:-Local patched build}" \
         dch --local "$VERSION_SUFFIX" "Rebuilt with local patches from $PATCH_DIR/$src." \
      && DEB_BUILD_OPTIONS="$opts" dpkg-buildpackage -b --no-sign -j"$BUILD_JOBS" ) >"$work/build.log" 2>&1 \
    || { echo "--- last 40 lines of the build log ---" >&2; tail -40 "$work/build.log" >&2; \
         fail "$src: build failed for $version"; }

  local produced="$work/produced" 
  : > "$produced"; : > "$produced.files"
  local f
  for f in "$work"/*.deb; do
    [ -e "$f" ] || continue
    dpkg-deb -f "$f" Package >> "$produced"
    echo "$LOCAL_REPO/$(basename "$f")" >> "$produced.files"
  done
  [ -s "$produced" ] || fail "$src: build produced no .deb files"

  prune_old_debs "$produced"
  cp "$work"/*.deb "$LOCAL_REPO/"

  mkdir -p "$STATE_DIR"
  echo "$version" > "$STATE_DIR/$src.built"

  echo "$src: rebuilt $version with $(wc -l < "$produced") binary package(s)"
  sed 's/^/    /' "$produced"
}

refresh_repo() {
  ( cd "$LOCAL_REPO" && dpkg-scanpackages . /dev/null 2>/dev/null | gzip -9c > Packages.gz ) \
    || fail "failed to regenerate $LOCAL_REPO/Packages.gz"
  apt-get update -qq || fail "apt-get update failed after publishing rebuilt packages"
}

prereq_report=$(check_prereqs)

case "$MODE" in
  prereqs)
    echo "=== prerequisites ==="
    ensure_deb_src report
    if [ -n "$prereq_report" ]; then
      echo "$prereq_report"
      [ -n "$MISSING_PACKAGES" ] && { echo; echo "  install with:"; echo "      sudo apt install$MISSING_PACKAGES"; }
    else
      echo "  everything needed is present"
    fi
    echo
    echo "=== build dependencies ==="
    for src in $(patched_packages); do
      off=$(official_version "$src")
      if [ -z "$off" ]; then echo "  $src: unknown, the Ubuntu version cannot be determined"
      else report_build_deps "$src" "$off"; fi
    done
    ;;

  status)
    printf '%-20s %-22s %-22s %s\n' "package" "ubuntu offers" "built from" "state"
    printf -- '-%.0s' {1..80}; echo
    for src in $(patched_packages); do
      off=$(official_version "$src"); blt=$(built_version "$src")
      if [ -z "$off" ]; then st="NO SOURCE (deb-src missing?)"
      elif [ -z "$blt" ]; then st="never built"
      elif [ "$off" = "$blt" ]; then st="current"
      else st="OUT OF DATE"; fi
      printf '%-20s %-22s %-22s %s\n' "$src" "${off:-?}" "${blt:--}" "$st"
    done
    ;;

  check)
    ensure_deb_src report
    [ -n "$prereq_report" ] && { echo "=== prerequisites missing ==="; echo "$prereq_report"; \
      [ -n "$MISSING_PACKAGES" ] && echo "      sudo apt install$MISSING_PACKAGES"; echo; }
    n=0
    for src in $(patched_packages); do
      off=$(official_version "$src"); blt=$(built_version "$src")
      [ -n "$off" ] || { echo "$src: cannot determine the Ubuntu version, is deb-src enabled?"; n=$((n+1)); continue; }
      if [ "$off" != "$blt" ] || [ "$FORCE" = yes ]; then
        echo "$src: would rebuild ${blt:-(never built)} -> $off"
        n=$((n+1))
      fi
      newer=$(unfetchable_update "$src" "$off")
      [ -n "$newer" ] && echo "$src: binary $newer exists but no source for it, see --prereqs about deb-src"
      true
    done
    [ "$n" -eq 0 ] && echo "everything current, nothing to rebuild"
    echo
    echo "Nothing was built. Re-run with --build as root to rebuild."
    ;;

  build)
    [ "$(id -u)" -eq 0 ] || fail "--build needs root, re-run with sudo"

    # Before any version is read, because a missing source index makes every package look current.
    ensure_deb_src apply
    prereq_report=$(check_prereqs)

    if [ -n "$MISSING_PACKAGES" ]; then
      echo "$prereq_report" >&2
      fail "cannot rebuild until these are installed: sudo apt install$MISSING_PACKAGES"
    fi
    [ -d "$LOCAL_REPO" ] || fail "local repository $LOCAL_REPO does not exist"

    built=0
    for src in $(patched_packages); do
      off=$(official_version "$src")
      [ -n "$off" ] || fail "$src: cannot determine the Ubuntu version, is deb-src enabled?"
      blt=$(built_version "$src")
      newer=$(unfetchable_update "$src" "$off")
      [ -n "$newer" ] && warn "$src: binary $newer is available but its source is not, so the rebuild is made" \
                              "from $off and will not contain whatever that newer version fixes"
      if [ "$off" != "$blt" ] || [ "$FORCE" = yes ]; then
        rebuild "$src" "$off"
        built=$((built+1))
      fi
    done

    if [ "$built" -gt 0 ]; then
      refresh_repo
      echo
      echo "Rebuilt packages are now the apt candidate. Install them with the next apt upgrade."
    fi
    ;;
esac
