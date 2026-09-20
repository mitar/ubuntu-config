#!/usr/bin/env bash
#
# Lock down third-party APT repositories so they cannot take over Ubuntu package names.
#
# Each third-party origin gets an allowlist of the packages actually in use, followed by a default-deny for
# everything else from that origin. A denied version is never installable whatever its version number, which is
# what stops a vendor repository from replacing a core Ubuntu package by publishing a higher version under the
# same name.
#
#
# Usage
# -----
#   ./lockdown-apt-sources.sh               show what would change and verify it, write nothing (default)
#   sudo ./lockdown-apt-sources.sh --apply  verify, then write the config, refresh apt and audit
#   ./lockdown-apt-sources.sh --audit       report what the allowlist does not cover
#
#
# How the pins work
# -----------------
# Pins match on "origin <hostname>", the host from the URI in sources.list.d. The release fields (o=, l=, a=) are
# read out of each repository's own Release file, so a repository chooses them freely and could pick a value that
# matches a rule meant for a different repository. A hostname comes from our own configuration instead, so a
# repository cannot influence it. It is also the only thing available for repositories such as NAPS2, which declare
# no Origin or Label at all. This is not about a network attacker, since APT verifies each Release file against the
# key named in Signed-By. It is about a repository we added legitimately choosing metadata that collides with ours.
#
# One hostname can serve many repositories: packagecloud.io serves every packagecloud vendor and
# ppa.launchpadcontent.net serves every PPA on Launchpad. For a default-deny that is the safe direction.
#
# APT sorts records into specific form (naming packages) and general form (Package: *), and consults every
# specific-form record before any general-form one, whatever order the files are in. A "Package: *" deny here
# therefore cannot override a vendor file that names a package explicitly, which is what SUPERSEDED exists to
# handle. Among records of the same form the first match wins, and preferences.d is read in lexicographic order,
# which is why this config is written as 00-third-party-lockdown.
#
#
# Adding a package from a repository already configured here
# ----------------------------------------------------------
# Edit this script, never the generated /etc/apt/preferences.d/00-third-party-lockdown, which is rewritten whole on
# every --apply, so edits made there are lost.
#
#   1. Add the package name to that origin's allow stanza in generate(). For example "Package: nodejs" becomes
#      "Package: nodejs nsolid".
#   2. Update that package's entry in CHECKS, otherwise the dry-run fails by design. An entry expecting "none"
#      becomes one expecting the origin the package now comes from.
#   3. Run the dry-run, confirm VERIFY OK, then --apply, then install the package.
#
#
# Adding a new repository
# -----------------------
#   1. Add the source as the vendor instructs and run apt update. Some vendors ship the source inside a bootstrap
#      .deb (Proton, Slack), so installing that .deb is what configures the repository, and the .deb itself is
#      redundant afterwards.
#   2. Run --audit. A new origin shows every package allowed (N / N), because nothing pins it yet and everything
#      sits at the default priority of 500. That is the signal that it is unprotected.
#   3. Copy an existing block in generate(), an allow stanza followed by "Package: *" at -1, and place it before
#      the ppa.launchpadcontent.net block. A PPA needs an allow stanza too, since the deny there covers every PPA.
#   4. Add CHECKS entries: one package that must be allowed, one that must stay denied.
#   5. Dry-run, then --apply.
#   6. If the vendor dropped its own pin file into preferences.d, add its name to SUPERSEDED. The dry-run lists any
#      such file under "other pin files naming packages explicitly".
#
#
# Recognising a denial
# --------------------
# Apt blames the wrong cause, reporting the package as missing or obsoleted and never mentioning pinning:
#
#   E: Package "nsolid" has no installation candidate
#
# Confirm with "apt-cache policy <package>", where a denied version carries a priority of -1 and the candidate is
# (none). A denied package pulled in as a dependency rather than named directly reports unmet dependencies
# instead, so run the same check on the dependency named in that error.

set -euo pipefail

PREF_NAME=00-third-party-lockdown
PREF_DEST=/etc/apt/preferences.d/$PREF_NAME
# Vendor-supplied pin files this config replaces. Disabling them is not cosmetic for the ones naming a package
# explicitly (nsolid): APT consults every such specific-form record before any "Package: *" rule here, so they win
# on precedence no matter how this file is named. They are renamed rather than deleted, because APT reads a file in
# preferences.d only when it has no extension or a .pref extension, so a .disabled suffix takes it out of play
# while leaving it next to its siblings and one mv away from coming back.
SUPERSEDED="mozilla nodejs nsolid"
DISABLED_SUFFIX=.disabled
BACKUP=/var/backups/apt-lockdown-$(date +%Y%m%d-%H%M%S)

MODE=dry-run
case "${1:-}" in
  ""|--dry-run) MODE=dry-run ;;
  --apply)      MODE=apply ;;
  --audit)      MODE=audit ;;
  -h|--help)    awk 'NR>1 && !/^#/{exit} NR>1{sub(/^# ?/, ""); print}' "$0"; exit 0 ;;
  *)            echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

# Emit the preferences file on stdout.
#
# Every rule pins on "origin <hostname>", which is the host taken from the URI in sources.list.d and not anything
# the repository declares about itself. The release fields (o=, l=, a=) are read out of the repository's own
# Release file, so a repository chooses them freely and can pick a value that matches a rule meant for a different
# repository. The hostname comes from our own configuration instead, so a repository cannot influence it. It is
# also the only thing available for repositories such as NAPS2, which declare no Origin or Label at all.
generate() {
cat <<'PREF_EOF'
# Generated by lockdown-apt-sources.sh. Rewritten whole on every run, so edits made here are lost.
#
# Pins match on "origin <hostname>", the host from the URI in sources.list.d, never on the release fields (o=, l=,
# a=) which come from the repository's own Release file and are therefore chosen by the repository itself.
#
# APT sorts records into specific form (naming packages) and general form (Package: *), and consults every
# specific-form record before any general-form one, whatever order the files are in. A "Package: *" deny here
# therefore cannot override a vendor file that names a package explicitly, which is why the script disables the
# superseded vendor files rather than relying on this file sorting first. Among records of the same form the
# first match wins, and this file sorts first so its own ordering is what decides.
#
# Shape per repository: allow what is in use, then deny the rest of that origin.

# The local repository is ours and outranks everything else. It is deliberately not locked down: anything placed
# in /usr/local/lib/debs may install. Note that outranking is per package name, so a package present both here and
# in a vendor repository stays on the local copy and stops taking vendor upgrades.
Package: *
Pin: origin ""
Pin-Priority: 1001


# ---- packages.mozilla.org ----------------------------------------------------------------------------------

# Stay on the Thunderbird 140 ESR series. Security updates within 140.x still apply. Above 1000 so a newer series
# that slipped in is downgraded back to 140.x.
Package: thunderbird-esr thunderbird-esr-l10n-*
Pin: version 1:140.*
Pin-Priority: 1001

# Refuse every other Thunderbird ESR version outright. Without this the series lock is only a preference: if
# Mozilla stops carrying 140.x the rule above matches nothing and the next series quietly becomes the candidate.
# Denying it here makes the lock fail closed, leaving the installed version in place and offering no upgrade.
Package: thunderbird-esr thunderbird-esr-l10n-*
Pin: origin packages.mozilla.org
Pin-Priority: -1

# Firefox and Thunderbird come from Mozilla on purpose, because the Ubuntu packages of the same name are snap
# wrappers. Ubuntu carries a higher epoch on both (1: on firefox, 2: on thunderbird), so the snap wrapper sorts
# above the Mozilla build and replacing it counts as a downgrade. Only a priority above 1000 permits that, so
# anything lower cannot recover once a snap wrapper has been pulled back in. Safe this high because it names
# specific packages from one origin rather than the whole repository.
Package: firefox firefox-l10n-* thunderbird thunderbird-l10n-*
Pin: origin packages.mozilla.org
Pin-Priority: 1001

# Mozilla publishes around 800 names across the stable, beta, nightly and devedition channels, including
# mozillavpn. Everything not allowed above is refused.
Package: *
Pin: origin packages.mozilla.org
Pin-Priority: -1


# ---- repo.protonvpn.com ------------------------------------------------------------------------------------

# Proton's stack is modular and has been re-split before: the repository still carries the older layout
# (python3-proton-vpn-connection, -session, -logger, -killswitch, -network-manager) whose contents currently live
# inside python3-proton-vpn-api-core. Matching Proton's own namespace rather than today's exact package list means
# a future re-split upgrades cleanly instead of failing on a denied dependency.
#
# python3-dbus-fast is deliberately absent. It is a generic library Ubuntu also ships, and it is the one genuinely
# dangerous name in this repository. Ubuntu has 4.0.0-1build1 against Proton's 2.44.1, so Ubuntu already wins on
# version. Denying it makes that deterministic rather than a race on version numbers.
#
# protonvpn-beta-release, protonvpn-gui and python3-protonvpn-nm-lib (the legacy stack) are not matched either.
Package: proton-vpn-* protonvpn-stable-release python3-proton-*
Pin: origin repo.protonvpn.com
Pin-Priority: 600

Package: *
Pin: origin repo.protonvpn.com
Pin-Priority: -1


# ---- deb.nodesource.com ------------------------------------------------------------------------------------

# Ubuntu also ships a nodejs, so this needs to be above 500 to win. Denies nsolid, which is unused.
Package: nodejs
Pin: origin deb.nodesource.com
Pin-Priority: 600

Package: *
Pin: origin deb.nodesource.com
Pin-Priority: -1


# ---- packages.microsoft.com --------------------------------------------------------------------------------

# Denies code-insiders and code-exploration.
Package: code
Pin: origin packages.microsoft.com
Pin-Priority: 500

Package: *
Pin: origin packages.microsoft.com
Pin-Priority: -1


# ---- packagecloud.io ---------------------------------------------------------------------------------------

# packagecloud.io hosts many unrelated vendors, so this deny covers all of them and not just Slack. That is the
# safe direction: another packagecloud repository added later is refused until it is allowed here explicitly.
Package: slack-desktop
Pin: origin packagecloud.io
Pin-Priority: 500

Package: *
Pin: origin packagecloud.io
Pin-Priority: -1


# ---- updates.signal.org ------------------------------------------------------------------------------------

# Denies signal-desktop-beta.
Package: signal-desktop
Pin: origin updates.signal.org
Pin-Priority: 500

Package: *
Pin: origin updates.signal.org
Pin-Priority: -1


# ---- downloads.claude.ai -----------------------------------------------------------------------------------

Package: claude-desktop
Pin: origin downloads.claude.ai
Pin-Priority: 500

Package: *
Pin: origin downloads.claude.ai
Pin-Priority: -1


# ---- downloads.naps2.com -----------------------------------------------------------------------------------

Package: naps2
Pin: origin downloads.naps2.com
Pin-Priority: 500

Package: *
Pin: origin downloads.naps2.com
Pin-Priority: -1


# ---- ppa.launchpadcontent.net ------------------------------------------------------------------------------

# No PPA is configured, and one hostname serves every PPA on Launchpad, so this denies all of them: a PPA added
# later installs nothing until an allow stanza is added here first. PPAs routinely rebuild core Ubuntu libraries
# under the names Ubuntu uses, which is the case this file exists to prevent, so they are opt-in per package.
Package: *
Pin: origin ppa.launchpadcontent.net
Pin-Priority: -1
PREF_EOF
}

# Check the resulting policy against what each rule is supposed to achieve. Takes a preferences.d directory so the
# same checks run against a candidate configuration in dry-run and against the installed one after apply.
verify() {
  python3 - "$1" <<'PY_EOF'
import re, subprocess, sys

prefs = sys.argv[1]

# pkg, kind, expected. origin: candidate must come from this host. version: candidate must be exactly this.
# none: package must have no installable candidate. installed: candidate must equal the installed version.
CHECKS = [
    ("libfido2-1",               "origin",    "archive.ubuntu.com"),
    ("python3-dbus-fast",        "origin",    "archive.ubuntu.com"),
    ("thunderbird-esr",          "version",   "1:140.15.0esr~build1"),
    ("firefox",                  "origin",    "packages.mozilla.org"),
    ("thunderbird",              "origin",    "packages.mozilla.org"),
    ("nodejs",                   "origin",    "deb.nodesource.com"),
    ("code",                     "origin",    "packages.microsoft.com"),
    ("signal-desktop",           "origin",    "updates.signal.org"),
    ("claude-desktop",           "origin",    "downloads.claude.ai"),
    ("naps2",                    "origin",    "downloads.naps2.com"),
    ("proton-vpn-gtk-app",       "origin",    "repo.protonvpn.com"),
    ("zoom",                     "installed", None),
    ("firefox-beta",             "none",      None),
    ("mozillavpn",               "none",      None),
    ("signal-desktop-beta",      "none",      None),
    ("nsolid",                   "none",      None),
    ("code-insiders",            "none",      None),
    ("protonvpn-beta-release",   "none",      None),
    ("python3-protonvpn-nm-lib", "none",      None),
]

def policy(pkg):
    """Return (installed, candidate, {version: [source strings]}) as apt-cache policy reports it."""
    out = subprocess.run(["apt-cache", "-o", "Dir::Etc::PreferencesParts=" + prefs, "policy", pkg],
                         capture_output=True, text=True).stdout
    inst = cand = None
    sources, cur = {}, None
    for line in out.splitlines():
        m = re.match(r"^  Installed: (.+)$", line)
        if m: inst = m.group(1); continue
        m = re.match(r"^  Candidate: (.+)$", line)
        if m: cand = m.group(1); continue
        m = re.match(r"^ (?:\*\*\*)?\s*(\S+) (-?\d+)$", line)
        if m: cur = m.group(1); sources.setdefault(cur, []); continue
        m = re.match(r"^\s+(-?\d+) (\S+)", line)
        if m and cur: sources[cur].append(m.group(2))
    return inst, cand, sources

fails = 0
print("%-26s %-34s %s" % ("package", "candidate", "result"))
print("-" * 92)
for pkg, kind, want in CHECKS:
    inst, cand, sources = policy(pkg)
    if cand is None:
        print("%-26s %-34s SKIP (unknown package)" % (pkg, "-")); continue
    if kind == "none":
        ok = cand == "(none)"
        detail = "denied" if ok else "INSTALLABLE, expected denied"
    elif kind == "version":
        ok = cand == want
        detail = "pinned" if ok else "expected " + want
    elif kind == "installed":
        ok = cand == inst
        detail = "unchanged" if ok else "expected installed " + str(inst)
    else:
        src = " ".join(sources.get(cand, []))
        ok = want in src
        detail = want if ok else "from " + (src or "?") + ", expected " + want
    fails += 0 if ok else 1
    print("%-26s %-34s %s %s" % (pkg, cand, "ok  " if ok else "FAIL", detail))

print()
up = subprocess.run(["apt-get", "-s", "-o", "Dir::Etc::PreferencesParts=" + prefs, "full-upgrade"],
                    capture_output=True, text=True).stdout
inst_lines = [l for l in up.splitlines() if l.startswith("Inst ")]
print("simulated full-upgrade: %d package(s)" % len(inst_lines))
for l in inst_lines:
    print("   " + l)

print()
print("VERIFY FAILED: %d check(s)" % fails if fails else "VERIFY OK: all checks passed")
sys.exit(1 if fails else 0)
PY_EOF
}

# List pin files other than ours that name packages explicitly. Such a record is consulted before any
# "Package: *" rule in this config, so it silently outranks the lockdown and has to be reviewed by hand.
scan_conflicts() {
  local found=0 f base
  for f in /etc/apt/preferences.d/*; do
    [ -e "$f" ] || continue
    base=${f##*/}
    [ "$base" = "$PREF_NAME" ] && continue
    case "$base" in *.pref|*.*) [ "${base##*.}" = pref ] || continue ;; esac
    if grep -qE '^Package:[[:space:]]*[^*[:space:]]' "$f"; then echo "   $f"; found=1; fi
  done
  [ "$found" -eq 0 ] && echo "   (none)"
  for f in /etc/apt/preferences.d/*"$DISABLED_SUFFIX"; do
    [ -e "$f" ] && echo "   (disabled, ignored by apt) $f"
  done
  return 0
}

# Populate a directory with the preferences.d an apply would leave behind: everything currently there, minus the
# files an apply disables, plus the generated config. Both modes verify against this rather than against the live
# directory, so a dry-run and an apply reach the same verdict.
build_candidate() {
  cp /etc/apt/preferences.d/* "$1/" 2>/dev/null || true
  local f
  for f in $SUPERSEDED; do rm -f "$1/$f"; done
  generate > "$1/$PREF_NAME"
}

# Report anything the allowlist does not cover, so the config can be kept current after installing something new.
audit() {
  python3 <<'PY_EOF'
import apt, apt_pkg, collections

UBUNTU = ("archive.ubuntu.com", "security.ubuntu.com", "esm.ubuntu.com")
def is_ubuntu(o): return o.site.endswith(UBUNTU) or o.origin in ("Ubuntu", "UbuntuESM", "UbuntuESMApps")

cache = apt.Cache()
policy = cache._depcache.policy

orphaned, shadowed, denied_origins = [], [], collections.defaultdict(lambda: [0, 0])

for pkg in cache:
    sites = {o.site for v in pkg.versions for o in v.origins if o.site and not is_ubuntu(o)}
    for site in sites:
        allowed = any(policy.get_priority(v._cand) > 0 for v in pkg.versions
                      if any(o.site == site for o in v.origins))
        denied_origins[site][0] += 1
        denied_origins[site][1] += 1 if allowed else 0

    if not pkg.is_installed:
        continue

    # Installed from a third-party repository but every version of it is now denied, so it can never be updated.
    inst_sites = {o.site for o in pkg.installed.origins if o.site and not is_ubuntu(o)}
    if inst_sites and all(policy.get_priority(v._cand) < 0 for v in pkg.versions if v != pkg.installed):
        if not any(policy.get_priority(v._cand) > 0 for v in pkg.versions):
            orphaned.append((pkg.name, pkg.installed.version, ",".join(sorted(inst_sites))))

    # Present in the local repository and in a vendor repository, so the local copy outranks and freezes it.
    local = [v for v in pkg.versions if any(not o.site and o.archive != "now" for o in v.origins)]
    vendor = {o.site for v in pkg.versions for o in v.origins if o.site and not is_ubuntu(o)}
    if local and vendor:
        shadowed.append((pkg.name, local[0].version, ",".join(sorted(vendor))))

print("=== third-party origins: allowed / offered ===")
for site, (total, allowed) in sorted(denied_origins.items()):
    print("   %-34s %4d / %-5d %s" % (site, allowed, total, "FULLY DENIED" if allowed == 0 else ""))

print("\n=== installed from a third-party repo but not covered by any allow rule ===")
print("\n".join("   %-34s %-24s %s" % r for r in sorted(orphaned)) or "   (none)")

print("\n=== shadowed by the local repo, so frozen at the local .deb version ===")
print("\n".join("   %-34s %-24s also in %s" % r for r in sorted(shadowed)) or "   (none)")
PY_EOF
}

case "$MODE" in
  audit)
    audit
    ;;

  dry-run)
    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    build_candidate "$tmp"

    echo "=== would write $PREF_DEST ($(grep -c '^Package:' "$tmp/$PREF_NAME") rules) ==="
    if [ -e "$PREF_DEST" ]; then
      diff -u "$PREF_DEST" "$tmp/$PREF_NAME" && echo "   (no change)"
    else
      echo "   new file"
    fi
    echo
    echo "=== would disable, preferences.d ==="
    for f in $SUPERSEDED; do
      [ -e "/etc/apt/preferences.d/$f" ] && echo "   /etc/apt/preferences.d/$f -> $f$DISABLED_SUFFIX"
    done
    echo
    echo "=== other pin files naming packages explicitly, which would override this config ==="
    scan_conflicts
    echo
    echo "=== verification against the candidate configuration ==="
    verify "$tmp"
    echo
    echo "Nothing was written. Re-run with --apply to install this."
    ;;

  apply)
    [ "$(id -u)" -eq 0 ] || { echo "--apply needs root, re-run with sudo" >&2; exit 1; }

    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    build_candidate "$tmp"
    if ! gate=$(verify "$tmp" 2>&1); then
      echo "$gate"
      echo
      echo "refusing to write: the generated config does not satisfy CHECKS." >&2
      echo "Either the allowlist needs the package adding, or CHECKS needs updating to match it." >&2
      exit 1
    fi
    echo "candidate configuration verified, applying"
    echo

    mkdir -p "$BACKUP"
    cp -a /etc/apt/preferences.d "$BACKUP/" 2>/dev/null || true
    echo "backed up /etc/apt/preferences.d to $BACKUP/"

    for f in $SUPERSEDED; do
      if [ -e "/etc/apt/preferences.d/$f" ]; then
        mv "/etc/apt/preferences.d/$f" "/etc/apt/preferences.d/$f$DISABLED_SUFFIX"
        echo "disabled /etc/apt/preferences.d/$f (now $f$DISABLED_SUFFIX, a copy is in $BACKUP/)"
      fi
    done

    generate > "$PREF_DEST"
    chmod 0644 "$PREF_DEST"
    echo "wrote $PREF_DEST"

    echo
    apt-get update -qq
    echo
    echo "=== verification ==="
    verify /etc/apt/preferences.d
    echo
    echo "=== audit ==="
    audit
    ;;
esac
