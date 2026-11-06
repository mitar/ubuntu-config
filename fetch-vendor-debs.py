#!/usr/bin/python3 -I
"""Download the newest .deb of packages whose vendors publish one but no APT repository, into the local repository.

For each package in PACKAGES below, find the newest version its vendor offers, and when it is not yet in the local
repository /usr/local/lib/debs, download it, verify it, publish it there under its Debian file name, remove the older
versions of the same package from there, and regenerate the repository's index. Nothing is installed: the downloaded
package simply becomes the candidate, so unattended-upgrades or the next ordinary apt upgrade upgrades it where it is
installed. A package which is not installed stays so until it is installed by hand, with apt install.

The local repository is pinned at 1001 by lockdown-apt-sources.sh, which makes apt downgrade an installed package to
the version there. A version lower than the one already in the repository is therefore never published, it is only
reported.

The kind of a download's source decides how it is verified:

  SignedRedirect   a fixed URL which redirects to the newest .deb, signed by the vendor in the format of dpkg-sig: a
                   member named _gpgbuilder holds the checksums of the other members, signed with the vendor's key.
                   The key is kept in this repository in vendor-debs/, so that a download signed with another key is
                   refused. When the vendor changes its key, the fetch fails naming the new key, until the new key
                   replaces the old one there and is installed.
  GitHubRelease    a release asset on GitHub, checked against the SHA256 GitHub publishes for it. That catches a
                   download corrupted or altered on its way, but not a release published by whoever controls the
                   project's account.

A downloaded package has to carry the package name it is fetched as, so that a vendor cannot replace another package
through the local repository, which is otherwise not locked down.

Which versions were last fetched is kept in /var/lib/vendor-debs/state.json, by the URL they were downloaded from, so
that a package is downloaded again only when its vendor offers a new one.

Failures are reported on stderr, for cron to mail, and to syslog, so that a missing mail transport cannot swallow
them. One package failing does not stop the others.


Usage
-----
  ./fetch-vendor-debs.py [package...]                report what would be downloaded, change nothing (default)
  sudo fetch-vendor-debs --fetch [package...]        download and publish whatever is newer

Naming packages limits the run to them, and without names every package is included.

--fetch prints nothing when everything is current, so a daily cron job stays quiet until there is something to say.
It prints a summary when it publishes, so that a new version is still mailed as a notification.


Installing
----------
Root runs this from cron, and apt installs the packages it publishes as root, so it and the keys it verifies with
have to be as trusted as root itself. Root therefore runs only root-owned copies installed from the repository with
install-fetch-vendor-debs.sh, never the repository itself, which your user can change. Run from the repository, it
reports each installed copy that differs from the repository.

The local repository itself is set up by install-rebuild-patched-packages.sh.


Adding a package
----------------
Add it to PACKAGES below, keyed by its Debian package name: a GitHubRelease for a project which publishes its .deb as
a release asset, or a SignedRedirect for a vendor which redirects a fixed URL to its newest .deb and signs it, with
its key in vendor-debs/. Then install the result with install-fetch-vendor-debs.sh.
"""

import argparse
import base64
import collections
import filecmp
import functools
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import syslog
import tempfile
import urllib.error
import urllib.parse
import urllib.request

LOCAL_REPO = '/usr/local/lib/debs'
STATE_DIR = '/var/lib/vendor-debs'
# Maps each package name to the URL its newest version was last downloaded from, and the file in the local repository
# that version is.
STATE_FILE = f'{STATE_DIR}/state.json'
KEY_DIR = '/usr/local/share/vendor-debs'
INSTALLED_SCRIPT = '/usr/local/bin/fetch-vendor-debs'
CRON_JOB = '/etc/cron.daily/fetch-vendor-debs'

SCRIPT = os.path.realpath(__file__)
REPO_DIR = os.path.dirname(SCRIPT)

# A generic client name, since GitHub's API refuses requests without one.
USER_AGENT = 'ubuntu-config-fetch-vendor-debs'
TIMEOUT_S = 60
CHUNK_BYTES = 1024 * 1024
SIGNATURE_MEMBER = '_gpgbuilder'
# The largest _gpgbuilder accepted. It holds three lines of checksums and a signature, so anything near this is not one.
SIGNATURE_MAX_BYTES = 64 * 1024
REQUIRED_COMMANDS = ['dpkg', 'dpkg-deb', 'dpkg-scanpackages', 'gpgv', 'apt-get']


def os_release(field):
    """Returns the value of a field of /etc/os-release, or an empty string when it has none."""
    with open('/etc/os-release') as file:
        for line in file:
            key, _, value = line.strip().partition('=')
            if key == field:
                return value.strip('"\'')
    return ''


UBUNTU_VERSION = os_release('VERSION_ID')

Offer = collections.namedtuple('Offer', ['url', 'sha256'])


class FetchError(Exception):
    """A package could not be located, downloaded, verified or published."""


def request(url, method='GET', accept=None):
    """Returns a urllib request for url which identifies this client."""
    headers = {'User-Agent': USER_AGENT}
    if accept:
        headers['Accept'] = accept
    return urllib.request.Request(url, method=method, headers=headers)


def read_json(url):
    """Returns the JSON document at url, decoded.

    Raises FetchError when it cannot be downloaded or is not JSON.
    """
    try:
        with urllib.request.urlopen(request(url, accept='application/vnd.github+json'), timeout=TIMEOUT_S) as response:
            return json.load(response)
    except (OSError, ValueError) as error:
        raise FetchError(f'cannot read {url}: {error}') from error


def download(url, path):
    """Downloads url into the new file path and returns the SHA256 of what was written, as hex.

    Raises FetchError when the download fails.
    """
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(request(url), timeout=TIMEOUT_S) as response, open(path, 'xb') as file:
            while chunk := response.read(CHUNK_BYTES):
                digest.update(chunk)
                file.write(chunk)
    except OSError as error:
        raise FetchError(f'cannot download {url}: {error}') from error
    return digest.hexdigest()


class GitHubRelease:
    """A package published as an asset of the releases of a GitHub repository.

    The newest release is the newest one which is not a draft, and not a pre-release unless prereleases is true. Its
    asset whose name matches the regular expression asset in full is the package, and there has to be exactly one.
    """

    def __init__(self, repository, asset, prereleases=False):
        self.repository = repository
        self.asset = asset
        self.prereleases = prereleases

    def locate(self):
        """Returns the Offer of the newest release, with the SHA256 GitHub publishes for the asset.

        Raises FetchError when there is no such release, or its assets do not include exactly one matching asset with
        a SHA256.
        """
        releases = read_json(f'https://api.github.com/repos/{self.repository}/releases?per_page=10')
        # GitHub orders releases by the date of the commit they tag, so a fix released from an older commit can be
        # listed below a release made before it. The publication date says which is newest.
        release = max((r for r in releases if not r['draft'] and (self.prereleases or not r['prerelease'])),
                      key=lambda r: r['published_at'] or '', default=None)
        if release is None:
            raise FetchError(f'{self.repository} has no release to download')
        # A newest release without the asset is reported rather than skipped for an older one, so that a project which
        # stops publishing it, or renames it, is noticed instead of being silently frozen at its last version.
        assets = [a for a in release['assets'] if re.fullmatch(self.asset, a['name'])]
        if len(assets) != 1:
            raise FetchError(f'release {release["tag_name"]} of {self.repository} has {len(assets)} assets matching '
                             f'{self.asset} instead of one')
        digest = assets[0].get('digest') or ''
        if not digest.startswith('sha256:'):
            raise FetchError(f'GitHub publishes no SHA256 for {assets[0]["browser_download_url"]}')
        return Offer(assets[0]['browser_download_url'], digest.removeprefix('sha256:').lower())

    def verify(self, path, offer, sha256):
        """Raises FetchError unless the downloaded file, whose SHA256 is sha256, is the asset GitHub describes."""
        if sha256 != offer.sha256:
            raise FetchError(f'{offer.url} has SHA256 {sha256}, but GitHub publishes {offer.sha256} for it')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Makes urllib raise HTTPError for a redirect instead of following it."""

    def redirect_request(self, *args, **kwargs):
        return None


class SignedRedirect:
    """A package at a fixed URL which redirects to the newest version, signed in the format of dpkg-sig.

    key is the file name, in KEY_DIR, of the ASCII-armored public key the package has to be signed with.
    """

    def __init__(self, url, key):
        self.url = url
        self.key = key

    def locate(self):
        """Returns the Offer of the URL the fixed one redirects to, without a SHA256.

        Raises FetchError when it does not redirect to an HTTPS URL.
        """
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request(self.url, method='HEAD'), timeout=TIMEOUT_S) as response:
                raise FetchError(f'{self.url} answered with status {response.status} instead of a redirect')
        except urllib.error.HTTPError as error:
            location = error.headers.get('Location') if error.code in (301, 302, 303, 307, 308) else None
            if not location:
                raise FetchError(f'{self.url} answered with status {error.code} instead of a redirect') from error
        except OSError as error:
            raise FetchError(f'cannot reach {self.url}: {error}') from error
        target = urllib.parse.urljoin(self.url, location)
        if urllib.parse.urlsplit(target).scheme != 'https':
            raise FetchError(f'{self.url} redirects to {target}, which is not HTTPS')
        return Offer(target, None)

    def verify(self, path, offer, sha256):
        """Raises FetchError unless the .deb at path is signed with the key and every member matches its signature."""
        members, signature = read_ar(path)
        if signature is None:
            raise FetchError(f'{offer.url} is not signed, it has no {SIGNATURE_MEMBER} member')
        signed = verify_signature(signature, os.path.join(KEY_DIR, self.key), offer.url)

        # The signed text lists, under Files:, the MD5, SHA1, size and name of every other member, one per line.
        listed = {}
        in_files = False
        for line in signed.splitlines():
            if line.startswith('Files:'):
                in_files = True
            elif in_files and line[:1] in (' ', '\t'):
                fields = line.split()
                if len(fields) != 4 or fields[3] in listed:
                    raise FetchError(f'the signature of {offer.url} lists files in an unknown form: {line.strip()}')
                listed[fields[3]] = (int(fields[2]), fields[1].lower())
            else:
                in_files = False
        # Every member dpkg could read has to be covered, so the signature has to list exactly the members there are.
        differing = sorted(name for name in listed.keys() | members.keys() if listed.get(name) != members.get(name))
        if differing:
            raise FetchError(f'{offer.url} does not match its signature in {", ".join(differing)}')


def read_ar(path):
    """Reads the ar archive at path, which a .deb is.

    Returns a dict of each member's name to its size and SHA1 as hex, leaving out the signature member, and the content
    of the signature member, or None when there is none. Raises FetchError when path is not an ar archive or holds a
    name twice.
    """
    members = {}
    signature = None
    with open(path, 'rb') as file:
        if file.read(8) != b'!<arch>\n':
            raise FetchError(f'{path} is not a .deb')
        while header := file.read(60):
            if len(header) != 60 or header[58:60] != b'`\n':
                raise FetchError(f'{path} is not a valid .deb, a member header is damaged')
            try:
                # GNU ar ends names with a slash, and the header pads them with spaces.
                name = header[0:16].decode('ascii').rstrip(' ').rstrip('/')
                size = int(header[48:58].decode('ascii'))
            except ValueError as error:
                raise FetchError(f'{path} is not a valid .deb, a member header is damaged') from error
            if name in members or (name == SIGNATURE_MEMBER and signature is not None):
                raise FetchError(f'{path} holds the member {name} twice')
            if name == SIGNATURE_MEMBER:
                if size > SIGNATURE_MAX_BYTES:
                    raise FetchError(f'{path} has a {SIGNATURE_MEMBER} member of {size} bytes, too large to be one')
                signature = file.read(size)
            else:
                digest = hashlib.sha1()
                remaining = size
                while remaining:
                    chunk = file.read(min(remaining, CHUNK_BYTES))
                    if not chunk:
                        raise FetchError(f'{path} is truncated in member {name}')
                    digest.update(chunk)
                    remaining -= len(chunk)
                members[name] = (size, digest.hexdigest())
            # Members start at even offsets.
            if size % 2:
                file.read(1)
    return members, signature


def dearmor(path):
    """Returns the ASCII-armored OpenPGP public key in path in the binary form gpgv reads.

    Raises FetchError when path cannot be read or holds no armored key.
    """
    try:
        with open(path) as file:
            lines = [line.strip() for line in file]
        body = lines[lines.index('-----BEGIN PGP PUBLIC KEY BLOCK-----') + 1:
                     lines.index('-----END PGP PUBLIC KEY BLOCK-----')]
        # Armor headers, such as Comment:, end at the first empty line. A line starting with = is the checksum.
        if '' in body:
            body = body[body.index('') + 1:]
        return base64.b64decode(''.join(line for line in body if not line.startswith('=')), validate=True)
    except (OSError, ValueError) as error:
        raise FetchError(f'cannot read the key {path}: {error}') from error


def verify_signature(signature, key, url):
    """Verifies the clear-signed signature, as bytes, against the key file key and returns the text it signs.

    Only the text gpgv extracts is returned, never the text around it, which a clear-signed message does not cover.
    Raises FetchError naming the key it was made with when it is not a valid signature by that key.
    """
    with tempfile.TemporaryDirectory(prefix='fetch-vendor-debs-') as directory:
        keyring = os.path.join(directory, 'keyring.gpg')
        signature_file = os.path.join(directory, 'signature.asc')
        signed_file = os.path.join(directory, 'signed.txt')
        with open(keyring, 'wb') as file:
            file.write(dearmor(key))
        with open(signature_file, 'wb') as file:
            file.write(signature)
        result = subprocess.run(['gpgv', '--homedir', directory, '--keyring', keyring, '--status-fd', '1',
                                 '--output', signed_file, signature_file], capture_output=True, text=True)
        status = result.stdout.splitlines()
        if result.returncode != 0 or not any(line.startswith('[GNUPG:] VALIDSIG ') for line in status):
            # ERRSIG ends with the fingerprint of the key the signature was made with, when the signature names it.
            made_with = [line.split()[-1] for line in status if line.startswith('[GNUPG:] ERRSIG ')]
            reason = (result.stderr.strip().splitlines() or ['gpgv found no valid signature'])[-1]
            raise FetchError(f'{url} is not signed with the key {key}'
                             + (f', but with {made_with[0]}' if made_with else '') + f' ({reason})')
        with open(signed_file) as file:
            return file.read()


# The packages to fetch, keyed by their Debian package name.
PACKAGES = {
    # The builds prefixed HRX_ use the HRX runtime of AMD's FastFlow SDK, which FastFlowLM still calls a preview,
    # instead of XRT from the Ubuntu archive.
    'fastflowlm': GitHubRelease('ROCm/FastFlowLM', rf'fastflowlm_[^_]+_ubuntu{re.escape(UBUNTU_VERSION)}_amd64\.deb'),
    # FreeTube publishes every release as a pre-release.
    'freetube': GitHubRelease('FreeTubeApp/FreeTube', r'freetube_.+_amd64\.deb', prereleases=True),
    # The key in zoom.asc is 84C3 65D6 CC9A 4886 CA92 6BCC 4F21 9739 9706 AC24, created 2026-01-15. Zoom's download
    # center links its current key as https://zoom.us/linux/download/pubkey?version=<version>, 6-7-5 for this one.
    'zoom': SignedRedirect('https://zoom.us/client/latest/zoom_amd64.deb', 'zoom.asc'),
}


def log(priority, message):
    """Reports message on stderr and to syslog."""
    print(f'fetch-vendor-debs: {message}', file=sys.stderr)
    syslog.openlog('fetch-vendor-debs', 0, syslog.LOG_DAEMON)
    syslog.syslog(priority, message)


def run(arguments, **kwargs):
    """Runs a command and returns its standard output as text, raising FetchError when it fails."""
    result = subprocess.run(arguments, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise FetchError(f'{arguments[0]} failed: {result.stderr.strip()}')
    return result.stdout


def deb_fields(path):
    """Returns the Package, Version and Architecture fields of the .deb at path, as a dict."""
    output = run(['dpkg-deb', '--field', path, 'Package', 'Version', 'Architecture'])
    fields = dict(line.split(': ', 1) for line in output.splitlines() if ': ' in line)
    if set(fields) != {'Package', 'Version', 'Architecture'}:
        raise FetchError(f'{path} lacks one of the Package, Version and Architecture fields')
    return fields


def compare_versions(a, b):
    """Compares two Debian versions as dpkg does, returning a negative number, zero or a positive number."""
    for relation, result in (('lt', -1), ('gt', 1)):
        if subprocess.run(['dpkg', '--compare-versions', a, relation, b]).returncode == 0:
            return result
    return 0


def repository_versions(package):
    """Returns the versions of package in the local repository, as a list of (version, file name), highest first."""
    found = []
    for entry in os.scandir(LOCAL_REPO):
        # Files starting with a dot are ones being written, which apt does not see either.
        if entry.name.startswith('.') or not entry.name.endswith('.deb') or not entry.is_file():
            continue
        fields = deb_fields(entry.path)
        if fields['Package'] == package:
            found.append((fields['Version'], entry.name))
    return sorted(found, key=functools.cmp_to_key(lambda a, b: compare_versions(b[0], a[0])))


def read_state():
    """Returns what was last fetched, as described at STATE_FILE, or an empty dict when nothing was.

    A state which cannot be read is reported and treated as none, which only means downloading everything again.
    """
    try:
        with open(STATE_FILE) as file:
            state = json.load(file)
        if isinstance(state, dict):
            return state
        raise ValueError('not an object')
    except FileNotFoundError:
        return {}
    except ValueError as error:
        print(f'fetch-vendor-debs: ignoring {STATE_FILE}, which cannot be read: {error}', file=sys.stderr)
        return {}


def write_state(state):
    """Replaces the state file with the given state."""
    os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
    temporary = f'{STATE_FILE}.new'
    with open(temporary, 'w') as file:
        json.dump(state, file, indent=2, sort_keys=True)
        file.write('\n')
    os.replace(temporary, STATE_FILE)


def is_current(package, offer, state):
    """Returns whether offer is the one last fetched for package and its file is still in the local repository."""
    recorded = state.get(package)
    return (isinstance(recorded, dict) and recorded.get('url') == offer.url
            and os.path.isfile(os.path.join(LOCAL_REPO, recorded.get('file') or '.missing')))


def publish(package, source, offer, state):
    """Downloads and verifies offer and publishes it in the local repository when it is newer than the version there.

    Records the offer in state either way, so that it is not downloaded again. Returns a line describing what was
    published, or None when nothing was. Raises FetchError when the download fails, does not verify or is not package.
    """
    with tempfile.TemporaryDirectory(prefix='fetch-vendor-debs-') as directory:
        path = os.path.join(directory, 'download.deb')
        sha256 = download(offer.url, path)
        source.verify(path, offer, sha256)
        fields = deb_fields(path)
        if fields['Package'] != package:
            raise FetchError(f'{offer.url} is the package {fields["Package"]}, not {package}')
        if fields['Architecture'] not in ('all', 'amd64'):
            raise FetchError(f'{offer.url} is for the architecture {fields["Architecture"]}, not amd64')
        version = fields['Version']

        existing = repository_versions(package)
        if existing and compare_versions(version, existing[0][0]) <= 0:
            if compare_versions(version, existing[0][0]) < 0:
                log(syslog.LOG_WARNING, f'{package}: {offer.url} is version {version}, lower than {existing[0][0]} in '
                                        f'the local repository, which is kept')
            state[package] = {'url': offer.url, 'file': existing[0][1]}
            return None

        # The Debian file name, in which the version is without its epoch.
        name = f'{package}_{version.partition(":")[2] or version}_{fields["Architecture"]}.deb'
        temporary = os.path.join(LOCAL_REPO, f'.{name}.new')
        shutil.copyfile(path, temporary)
        os.chmod(temporary, 0o644)
        os.replace(temporary, os.path.join(LOCAL_REPO, name))

    removed = [file for _, file in existing if file != name]
    for file in removed:
        os.remove(os.path.join(LOCAL_REPO, file))
    state[package] = {'url': offer.url, 'file': name}
    return f'{package}: published {version} as {name}' + (f', removed {", ".join(removed)}' if removed else '')


def refresh_index():
    """Regenerates the index of the local repository. Raises FetchError when it cannot."""
    output = subprocess.run(['dpkg-scanpackages', '.', '/dev/null'], cwd=LOCAL_REPO, capture_output=True)
    if output.returncode != 0:
        raise FetchError(f'dpkg-scanpackages failed: {output.stderr.decode(errors="replace").strip()}')
    # Written under a name starting with a dot and renamed into place, so that apt never reads a partial index.
    temporary = os.path.join(LOCAL_REPO, '.Packages.gz.new')
    with open(temporary, 'wb') as file:
        file.write(gzip.compress(output.stdout, compresslevel=9, mtime=0))
    os.chmod(temporary, 0o644)
    os.replace(temporary, os.path.join(LOCAL_REPO, 'Packages.gz'))


def install_drift():
    """Returns a line for each installed copy which differs from the repository this runs from.

    Returns nothing when run as an installed copy, which has no repository next to it.
    """
    keys = os.path.join(REPO_DIR, 'vendor-debs')
    if not os.path.isdir(keys):
        return []
    pairs = [(SCRIPT, INSTALLED_SCRIPT), (os.path.join(REPO_DIR, 'cron.daily', 'fetch-vendor-debs'), CRON_JOB)]
    pairs += [(os.path.join(keys, name), os.path.join(KEY_DIR, name)) for name in sorted(os.listdir(keys))]
    drift = []
    for source, installed in pairs:
        if not os.path.isfile(installed):
            drift.append(f'  {installed} is not installed')
        elif not filecmp.cmp(source, installed, shallow=False):
            drift.append(f'  {installed} differs from {source}')
    return drift


def check(packages):
    """Reports what --fetch would download, without changing anything. Returns the exit status."""
    drift = install_drift()
    if drift:
        print('=== installed copies differ from the repository, update them with install-fetch-vendor-debs.sh ===')
        print('\n'.join(drift))
        print()
    state = read_state()
    pending = 0
    failed = False
    for package in packages:
        try:
            offer = PACKAGES[package].locate()
        except FetchError as error:
            print(f'fetch-vendor-debs: {package}: {error}', file=sys.stderr)
            failed = True
            continue
        if not is_current(package, offer, state):
            print(f'{package}: would download {offer.url}')
            pending += 1
    if not pending and not failed:
        print('everything current, nothing to download')
    print()
    print('Nothing was downloaded. Re-run with --fetch as root to download.')
    return 1 if failed else 0


def fetch(packages):
    """Downloads and publishes whatever is newer, printing only what was published. Returns the exit status."""
    if os.geteuid() != 0:
        sys.exit('fetch-vendor-debs: --fetch requires root, re-run with sudo')
    missing = [command for command in REQUIRED_COMMANDS if shutil.which(command) is None]
    if missing:
        log(syslog.LOG_ERR, f'cannot fetch, these commands are missing: {" ".join(missing)} (dpkg-scanpackages comes '
                            f'with the dpkg-dev package)')
        return 1
    if not os.path.isdir(LOCAL_REPO):
        log(syslog.LOG_ERR, f'the local repository {LOCAL_REPO} does not exist, set it up with '
                            f'install-rebuild-patched-packages.sh')
        return 1
    drift = install_drift()
    if drift:
        print(f'installed copies differ from the repository, the keys are read from {KEY_DIR}:')
        print('\n'.join(drift))

    state = read_state()
    published = []
    failed = False
    for package in packages:
        try:
            offer = PACKAGES[package].locate()
            if is_current(package, offer, state):
                continue
            line = publish(package, PACKAGES[package], offer, state)
            # Recorded right away, so that a later failure does not cause this download to be repeated.
            write_state(state)
            if line:
                published.append(line)
        # Writing to the local repository or the state file fails with OSError.
        except (FetchError, OSError) as error:
            log(syslog.LOG_ERR, f'{package}: {error}')
            failed = True

    if published:
        print('\n'.join(published))
        try:
            refresh_index()
        except (FetchError, OSError) as error:
            log(syslog.LOG_ERR, f'published packages are not in the index of the local repository yet: {error}')
            return 1
        # A failed update does not undo the index, which apt reads at its next update, so the run continues.
        try:
            run(['apt-get', 'update', '-qq'])
        except FetchError as error:
            log(syslog.LOG_ERR, f'the index of the local repository is regenerated, but apt sees it only after its '
                                f'next successful update: {error}')
            failed = True
        print()
        print('Downloaded packages are now the apt candidate. unattended-upgrades or the next apt upgrade upgrades the '
              'installed ones.')
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(
        description='Download the newest .deb of packages which have no APT repository into the local repository.')
    parser.add_argument('--fetch', action='store_true', help='download and publish whatever is newer (root)')
    parser.add_argument('packages', nargs='*', metavar='package',
                        help=f'limit the run to these packages, out of {", ".join(sorted(PACKAGES))}')
    args = parser.parse_args()
    unknown = [package for package in args.packages if package not in PACKAGES]
    if unknown:
        parser.error(f'unknown package {", ".join(unknown)}, known are {", ".join(sorted(PACKAGES))}')
    packages = args.packages or sorted(PACKAGES)
    sys.exit(fetch(packages) if args.fetch else check(packages))


if __name__ == '__main__':
    main()
