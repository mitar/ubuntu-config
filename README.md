# Ubuntu desktop configuration

Scripts which turn an Ubuntu desktop installation into a
[malleable computer](https://world.hey.com/dhh/the-malleable-computer-7c187a9b): a system whose behavior is
changed to fit its user, not only its settings. On Linux the whole system is open source, so when a setting is
not enough, the program itself can be changed. With AI assistance, doing so has become practical even for parts
of the system one has never looked into, like the terminal or the window manager.

The configuration here is highly opinionated: it is how I like my computer. But the code is free to reuse. Use
whatever helps and adapt it to your own liking.

It targets Ubuntu 26.04 with GNOME 50 on a Framework Laptop 13 Pro.

## How it works

Most of the configuration is done by scripts which write settings (mostly through gsettings and dconf, and a few
system files through sudo). Every script by default only shows what it would change, and changes things only
when run with `--apply`. They are idempotent, so they can be rerun at any time to bring the system back to the
configuration or to see how it has drifted from it.

Where a setting is not enough, Ubuntu's own packages are patched. `patches/<source-package>/` contains patches
which are applied on top of Ubuntu's source package, or for Firefox the Mozilla Team PPA's, together with tests for
the patched behavior which run during the build where the package has a test suite to add them to. `rebuild-patched-packages.sh` rebuilds a package whenever Ubuntu publishes a new version of it or its
patches change, building it with sbuild as an unprivileged user in a throwaway chroot, and publishes the result to
a local APT repository. That repository is pinned above the Ubuntu archive, so unattended-upgrades or the next
ordinary `apt upgrade` installs the patched build. A daily cron job keeps patched packages current, running
root-owned copies of the script and patches, installed with `install-rebuild-patched-packages.sh`.

## Main features

### Keyboard like on macOS

The key next to the space bar, pressed with the thumb, acts as Control, where macOS has Command. Copy, paste, and
other common shortcuts are all pressed with the thumb, in every application. The Ctrl key in the corner, pressed
with the pinky, acts as Super. The right side of the space bar mirrors the left: Control, then Super.

This makes the terminal behave like every other application. The pinky key sends control characters (Ctrl+C
interrupts, Ctrl+R searches history), while the thumb key copies and pastes with the same shortcuts as everywhere
else, without the usual Ctrl+Shift+C and Ctrl+Shift+V.

What it needs:

- The keys rotated with XKB options, and GNOME's shortcuts moved to match (`configure-gnome-keys.sh`).
- Super treated as Control inside the terminal, so that the pinky key sends control characters there
  (`patches/vte2.91/`).
- The terminal's own shortcuts on the thumb key, like in other applications (`configure-ptyxis.sh`).
- The console keeping Control under the Ctrl keycaps, with the US layout only (`configure-gnome-keys.sh`).

![The bottom row of the keyboard: the keycaps as they come, the keycaps rearranged as on a Mac, and what each key
sends in GNOME](docs/keyboard-bottom-row.svg)

Note: Framework supports swapping left Ctrl with Fn in BIOS so if you really want complete macOS layout
with Fn key in the left bottom corner position, you can switch additionally those two keys there.

### One key for Claude Quick Entry

Tapping the right Super on its own opens Claude Desktop's Quick Entry, and tapping the left Super on its own opens
the overview. Held together with other keys, both stay an ordinary Super, so Ctrl+Super+L, both right of the
space bar, locks the screen.

What it needs:

- A third key tapped on its own, next to GNOME's overview and locate-pointer keys, which sends a configured key
  combination (`patches/mutter/`). The right Super sends XF86Launch9, which no physical key sends
  (`configure-gnome-keys.sh`).
- Claude Desktop's Quick Entry shortcut set to that key, in the application.

Framework's BIOS can turn the key right of the right Alt into a Copilot key, but do not switch it and leave it as
Right Ctrl, the default. This configuration supersedes a Copilot key: the key stays a full Super, and a tap of it
opens Claude's Quick Entry. As a Copilot key, it would send a fixed key combination instead and could not act as
Super.

### Terminal improvements

Ptyxis, Ubuntu's terminal, is patched and configured (`patches/ptyxis/`, `configure-ptyxis.sh`):

- Ctrl+G moves to the next search match, and Ctrl+Shift+G to the previous one.
- A keyboard shortcut for copying as HTML.
- Palettes can set the selection highlight colours.
- The visual bell shows even with animations disabled.

### Third-party APT repositories pinned to their packages

Third-party repositories are pinned by hostname, with an allowlist of the packages actually in use and a default
deny for everything else from them, so a vendor repository cannot replace an Ubuntu package by publishing a higher
version under the same name (`lockdown-apt-sources.sh`). Unattended upgrades are likewise tied to Ubuntu's own
hosts, so a vendor repository cannot have its packages installed unattended by claiming to be Ubuntu's security
pocket.

### Patched packages kept up to date

Patched packages are rebuilt whenever Ubuntu ships a new version or their patches change, and published to a local
APT repository which a daily cron job keeps current (`rebuild-patched-packages.sh`,
`install-rebuild-patched-packages.sh`, `cron.daily/`). They are built the way Ubuntu builds its own packages,
with sbuild in a throwaway chroot without network access, with build dependencies installed only inside it. Its
unshare mode needs no root even to set up the chroot, so everything runs as an unprivileged system user, and a
package's build scripts and test suite cannot change the system.

### Daily mail of failures and errors

Once a day, a mail lists the units which failed and the errors logged since the previous mail, and no mail is sent
when there were none (`journal-digest.py`, `install-journal-digest.sh`, `cron.daily/`). It reads the systemd
journal of the system and of every user. A failed unit is listed with the last lines its failed run logged, which
usually say why, and errors are grouped by what logged them and their text with the numbers in it left out, so that
one which repeats is listed once with how often and when. New crash reports in `/var/crash` are listed too, since
apport writes them without logging the crash as an error. Known noise can be left out with patterns in
`journal-digest.ignore`. `journal-digest --since -7d` can be used to get a week load of messages to determine
patterns from. With `--ignore journal-digest.ignore`, patterns are tried out from the repository before they are
installed.

### Battery charge thresholds on Framework laptops

GNOME's Preserve Battery Health keeps the battery between 40% and 60% while the laptop is docked most of the time,
and a quick settings toggle switches back to charging to 100% before traveling.

What it needs:

- The kernel's charge control enabled, which it keeps disabled on Framework laptops by default
  (`modprobe.d/cros-charge-control.conf`).
- The thresholds for UPower, which applies them for Preserve Battery Health (`udev/90-battery-charge-limit.rules`).
- The Preserve Battery Health extension for the quick settings toggle (`configure-gnome-desktop.sh`).
- The estimated time until the battery is empty or full, shown under the battery percentage in quick settings, also
  on the lock screen.
- The firmware's own charge limit at 99%[^charge-limit] and Battery Extender disabled, in the BIOS.

### Hardware tweaks

The touchscreen is disabled, the power button light is off, and the longer key repeat delay applies in the console
as well (`udev/`, `systemd/`).

The audio coprocessor of the APU exposes a digital microphone which has no microphones wired to it on this laptop,
and which the sound server otherwise offers as a second internal microphone beside the working one. Its card is kept
unregistered (`modprobe.d/snd-acp-legacy-mach.conf`). Without that card the Realtek codec is no longer configured
through ALSA UCM, so Sound settings offer one configuration whose ports follow the jacks in place of four
combinations of speakers, headphones and microphones which differ in which of them they leave out.

### Processor throttling

An indicator appears in the top bar while the processor is being held below the speed it would otherwise run
at, and names what is holding it there. Its menu breaks that down into the share of the last interval each
limit accounted for, alongside the fan speed, the package power and the temperatures.

The interfaces the usual tools read for this are Intel's and AMD publishes no equivalent, so the indicator
reads the APU's own metrics table at `/sys/class/drm/card*/device/gpu_metrics` instead. That table counts the
time spent limited by each reason: the sustained power limit, the fast and slow package power limits, a
temperature limit for the cores, for the graphics and for the SoC, and the platform's PROCHOT. It needs an APU
publishing the table in the `gpu_metrics_v3_0` layout, which Strix Point in the Ryzen AI 300 series does.

The menu also shows the fan's speed and the target speed the embedded controller (EC) is bringing it to, both as the
EC reports them through `cros_ec`'s hwmon device. The target is 0 while the fan is off. While
`framework_tool --fansetduty` holds the fan at a fixed duty cycle, the EC does not update the target, which then
keeps the last one its automatic control set. Both are given as a share of 6200 RPM, the top of the EC's fan table,
which the EC does not report. It comes from the EC's source code, where Framework sets the fan's `rpm_max` in
`zephyr/program/framework/azalea/fan.dtsi` of
[EmbeddedController](https://github.com/FrameworkComputer/EmbeddedController) (the EC of this laptop is built as its
`lilac` variant), and it is the fastest the EC's automatic control runs the fan. With `framework_tool --fansetduty`
the fan can spin faster, up to about 7600 RPM at a full duty cycle, which shows as more than 100%. The firmware's
ACPI fan declares 6100 RPM as its full speed in its `_FPS` table instead, and only on or off as its control state,
so it says less than the EC does.

Linux has no say in the fan, the EC runs it by itself from its own temperature sensors. According to the EC's source
code, which the EC's `EC_CMD_THERMAL_GET_THRESHOLD` host command confirms, the fan follows only the three F75303
sensors on the board. Each of them keeps the fan off up to 50 C and above that asks for a speed which grows from
2100 RPM to the top of the fan table at 70 C (69 C for the one next to the APU, and 46 C to 60 C for the one next to
the memory), and the EC runs the fan at the fastest speed any of them asks for. The fourth sensor, the APU's own
temperature which the EC reads over SB-TSI and which shows as `cpu@4c`, asks for the fan only from 103 C, and the EC
uses it to throttle and to shut down the APU when it overheats.

The indicator also turns red and shows THM, and its menu a warning, when the EC's fan control stops following the
processor's heat. After days of uptime the EC has been seen to stop updating its sensors. The three F75303 readings
come from one chip, which shares an I2C bus with the APU's SB-TSI, and the EC's driver for it keeps reporting the
last temperature it read when it cannot read the chip any more. The readings then stay at the values they had, and
the fan stays at the speed those values ask for, which is off when they stopped below 50 C. A suspend and resume
makes the EC read them again. The indicator tells that the fan does not follow the heat from either of two signs.
One is that no fan has spun for 30 seconds in a row while the hottest core was at 70 C or more: the board warms up
after the cores do, but starting from a cool board with the fan off, a working EC starts the fan at most about 10
seconds after the hottest core reaches 70 C, and under lighter loads before the cores reach it. The other is that
none of the board readings has changed for 2 minutes while the hottest core has been at 70 C or more for the last 30
seconds in a row: the readings are in whole degrees and can all stay the same for minutes while the processor idles,
but once it heats up, at least one of them changes within seconds. The warning stays until the sign which raised it
is gone, so until a fan spins again or a board reading changes.

### Cooling and performance tracked over time

Every day at 04:00, if the laptop is on AC power, its screen is locked and its load is low, a measurement records
how well it cools itself and how fast its processor is, so that both can be followed over months and years
(`health-benchmark.py`, `install-health-benchmark.sh`, `systemd/health-benchmark.*`). Dust builds up in the fan and
in the fins it blows through, and firmware, microcode and kernel updates, with the mitigations they bring, can make
the processor slower without anything else showing it. A day on which the laptop is suspended, in use or busy at
04:00 is skipped, and the measurement made the next day.

A measurement holds the fan at a full duty cycle and the power profile at performance for about 25 minutes. With
the fan at full speed at rest, the temperatures stand in for the room's, since no sensor measures it, and the fan's
speed shows the fan's state: it falls as the fan wears, and rises as dust clogs the fins, because a blocked fan has
less air to push. Then come 7-Zip's benchmark on all threads and on one, and perf's context switch and system call
benchmarks, which is where kernel mitigations cost the most. A steady load follows until the heatsink has warmed
up, which gives the thermal resistance of the cooling, the degrees the APU rises above the air inside per watt, and
the cooldown after it gives the time constant with which the temperature falls. Both grow as dust accumulates. The
fan runs at full speed throughout, so that dust shows in these two and not in the scores. Nothing is recorded when
other processes used more than 1% of the CPU time during any of the phases, since that would have cost the scores
about as much, nor when the screen is unlocked, AC power is disconnected or the laptop suspends during the
measurement. Such a day is skipped, while a measurement which fails, for example because the EC stopped reading its
sensors (see [Processor throttling](#processor-throttling)), leaves `health-benchmark.service` failed, which the
[daily mail](#daily-mail-of-failures-and-errors) then reports.

7-Zip is a static build of a pinned release, verified by its checksum, so that its scores stay comparable across
years and only the hardware, firmware, microcode and kernel can move them. Each measurement records those as well,
in `/var/lib/health-benchmark/results.csv`, which everyone can read, and keeps the samples it read every second in
`traces/` next to it.

`health-benchmark` without arguments reports the latest measurement as a share of the best one for each value, and
lists what changed since the measurement with the best performance. `sudo health-benchmark --run --note "..."`
measures right away, which marks a reference point, for example before and after cleaning the fan.

### Windows keep their size when the screen locks

GNOME Shell disables most extensions while the screen is locked, the dock among them, and enables them again after
unlocking. The dock reserves its part of the screen only while it is enabled, so windows snapped to a side or
maximized would grow while the screen is locked and shrink back a moment after unlocking. A terminal resizes its
contents with the window, which garbles the screen of programs like Claude Code running in it. The Reserve Space
extension stays enabled on the lock screen and keeps an invisible copy of every part of the screen reserved by the
dock, the top bar or another panel, so the space left for windows does not change. The copies follow the reserved
space a few seconds after it changes, for example when the dock is moved to another side of the screen.

### Signal and Slack open where they were placed

Signal and Slack start at login on their own workspace and in their own place on the screen. On Wayland a window
cannot tell GNOME where it should be, and Electron does not use the session management protocol through which GNOME
could remember that for it, so GNOME places their windows anew at every start. The Auto Move Windows extension moves
them to their workspace, and the Smart Auto Move extension then restores their monitor, position and size, recorded
in its settings (`gnome-extensions/smart-auto-move.dconf`). Positions are kept per monitor, by the name of its
connector, and are recorded for the laptop's own screen, where the windows also open while an external monitor is
connected.

### Firefox windows return to their workspaces

Firefox restores its windows to the workspace, monitor, position and size they had when it quit. It names its
windows to GNOME through the Wayland session management protocol, and GNOME remembers their places under those
names. Three things stand in the way on Ubuntu 26.04, each patched until the fix ships:

- Mutter 50 offers the protocol only under its draft name, behind a debug switch (`patches/mutter/`, a backport
  from mutter 51).
- GTK 3 lacks the signal Firefox needs to name a window before it is shown (`patches/gtk+3.0/`, a backport from
  GTK's 3.24 branch).
- Firefox turns the protocol off on GNOME, because of a bug of its own which makes it crash when a session is
  restored from the History menu, and a second one which leaves windows hidden on the first start
  (`patches/firefox/`).

Firefox therefore comes from the Mozilla Team PPA, which publishes the source to rebuild it with, unlike Mozilla's
own repository (requested in [Mozilla bug 2087959](https://bugzilla.mozilla.org/show_bug.cgi?id=2087959)). Its build
is also brought closer to Mozilla's: the libraries Mozilla runs in a WebAssembly sandbox, for fonts, spell checking,
XML and audio, are sandboxed here too, where the PPA's build leaves them out.

### Firefox's sandbox has its user namespaces

Firefox isolates its content processes in user namespaces of their own. Ubuntu allows unprivileged user namespaces
only to programs with an AppArmor profile which grants them, and the apparmor package has one for Firefox. Ubuntu's
7.0 kernel never applies it: the parser compiles the profile's pattern for Firefox's path into a matcher with a
differentially encoded state, which the kernel's matcher for attaching profiles mishandles, so Firefox runs
unconfined, is denied the namespaces, and its content processes are isolated by seccomp alone. This happens with
Mozilla's own package too. `configure-gnome-desktop.sh` adds `Optimize=no-diff-encode` to
`/etc/apparmor/parser.conf`, so the parser does not use that encoding, and compiles the profile again. The kernel bug
is [Ubuntu bug 2172229](https://bugs.launchpad.net/bugs/2172229), fixed in mainline by commit
[e667fc72f7f4](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=e667fc72f7f4a54674962c324048ca2cc58bc703).
Once the running kernel has it, the line in `parser.conf` and its call in the script can go.

### Magnet links open in a running Transmission

Opening a magnet link or a torrent file while Transmission already runs starts a second Transmission, which passes
the link over the session bus to the one already running and exits. Ubuntu confines Transmission with an AppArmor
profile which allows it to own its name on the session bus, but not these calls between two of its instances, so
without rules added to the profile (`apparmor.d/local/transmission-gtk`) the link is lost and nothing happens. The
missing rules are reported in [Ubuntu bug 2169433](https://bugs.launchpad.net/ubuntu/+source/transmission/+bug/2169433).

### File indexing does not crash on vanished downloads

GNOME's file indexer, localsearch, extracts metadata from new files in the background. Its extractor crashed on a
temporary file of a download which was deleted before the extractor could read it: the file was handled as an
extraction without metadata, recording it failed, and preparing the error report freed memory it did not own.
Ubuntu's localsearch and tinysparql are patched to handle such a file as a failed extraction and to report the
error without crashing (`patches/localsearch/`, `patches/tinysparql/`). The fixes are proposed upstream in
[localsearch merge request 707](https://gitlab.gnome.org/GNOME/localsearch/-/merge_requests/707) and
[tinysparql merge request 819](https://gitlab.gnome.org/GNOME/tinysparql/-/merge_requests/819), and the patches can be
dropped once Ubuntu ships versions which include them.

### Chromium and Electron applications do not crash at logout and shutdown

Signal, Slack, Claude Desktop and other applications built on Electron crashed at every logout and shutdown with
"GPU process isn't usable. Goodbye.", and Chromium itself does the same. Chromium moves its browser process into a
systemd scope of its own, so that xdg-desktop-portal can identify it, but leaves its zygotes, GPU process and other
helpers in the scope GNOME launched it in. Only GNOME's scopes are tied to the session, so at logout systemd stops
the helpers while the browser process keeps running, fails to start its GPU process again, and crashes. A drop-in
for the user service manager ties every application scope to the session, so that the application's own scope is
stopped at logout too. Stopping both scopes at once is not enough, because the helpers can still die before the
browser process starts to shut down, and stopping the application's own scope first is not enough either, because
Chromium starts its renderers in that scope, and a browser process whose renderers die under it can stall until it
is killed. So at logout a user service sends SIGTERM to the browser process alone, which Chromium names its scope
after, and waits for it to exit, and every application scope is ordered to stop only after that service. The browser
process then shuts down while its helpers still run, and ends them itself (`stop-application-scopes.sh`,
`systemd/user/`, installed by `configure-gnome-desktop.sh`).

At a shutdown or a reboot that ordering is not enough. logind starts the shutdown as soon as gnome-session released
its delay inhibitor, which it does right before it exits, and the helpers then sometimes died before the user's
service manager even started to stop the graphical session. So a second user service holds a delay inhibitor of its
own. When logind announces a shutdown or a reboot, the service stops the first one, which lets the browser processes
exit, and releases its inhibitor only after that, so that logind starts the shutdown once the applications exited
(`stop-application-scopes-before-shutdown.py`). logind waits for an inhibitor for at most `InhibitDelayMaxSec=`, 5
seconds by default, which unattended-upgrades raises to 30 seconds. GNOME asks logind for the shutdown only once it is
confirmed, so cancelling its confirmation dialog leaves the applications running. Electron offers logind's
announcement to applications as `powerMonitor`'s `shutdown` event, but Signal, Slack and Claude Desktop do not listen
for it.

GNOME Shell is not ordered relative to the application scopes either, so at logout and at shutdown systemd stopped it
while the applications were still quitting. GTK, which Chromium loads, ends a process whose Wayland connection breaks
at once, with exit status 1, and Claude Desktop was cut off in the middle of its shutdown. A drop-in orders the Shell
to stop only after the service which lets the browser processes exit, too, so that the Shell is still there while
they quit (`systemd/user/org.gnome.Shell@.service.d/`).

The cause is reported to Chromium as [issue 580525223](https://issues.chromium.org/issues/580525223), and the
drop-ins and the services can go once that is fixed. Separately, Chromium and Electron do not learn about a logout
from the portal through which GNOME announces it, which would let them exit before anything is stopped:
[Chromium issue 580443832](https://issues.chromium.org/issues/580443832) and
[Electron issue 55551](https://github.com/electron/electron/issues/55551).

### Claude Desktop and Slack show their tray icon after every login

Claude Desktop and Slack often started at login without their icon in the top bar, while they kept running. The icon
is shown through the StatusNotifierItem protocol, for which the Ubuntu AppIndicators extension owns the name
`org.kde.StatusNotifierWatcher` on the session bus. Chromium, on which both are built, checks whether that name has an
owner only when the application creates its tray icon, and without one uses a GTK status icon, which is not shown
under Wayland, and does not check again. GNOME Shell enables the AppIndicators extension only after every extension in
the user's own directory, one after another, while gnome-session starts the applications at login as soon as the Shell
reports it is ready, which it does before it has enabled its extensions. So whichever application created its tray
icon a fraction of a second too early had no icon until it was restarted, or until its tray icon was turned off and on
in its settings, which creates the icon again. Their entries in `~/.config/autostart` start them only once the name
has an owner (`wait-for-status-notifier-watcher.sh`, `autostart/`, installed by `configure-gnome-desktop.sh`). An entry
is installed only in place of one the application wrote itself, when its setting to start at login is on, and the
application writes its own entry again when that setting is turned off and on, until the script runs again.

The entries and the script can go once Chromium waits for the name to have an owner, as libayatana-appindicator does,
or once GNOME Shell enables its extensions before it reports that it is ready. Extensions being enabled only after
the Shell started up is also why they cannot change its startup animation any more,
[GNOME Shell issue 8917](https://gitlab.gnome.org/GNOME/gnome-shell/-/issues/8917).

### snapd's desktop helper starts with the desktop

snapd generates the user service of snapd-desktop-integration, which installs theme snaps and shows notifications
about snap updates, to start with the user's service manager instead of with the graphical session. It started before
there was a display, failed with "Failed to open display" until its restarts reached the session, and failed again at
every logout when the display went away. A drop-in and a link in `graphical-session.target.wants` start it with the
session instead, skip its start without a display, and stop it with the session while the display is still there
(`systemd/user/snap.snapd-desktop-integration.snapd-desktop-integration.service.d/`, installed by
`configure-gnome-desktop.sh`). The snap's launcher also exits with status 2 whenever it wants to be started again,
including when the session ends and when systemd stops it, so the drop-in counts that status as success and still
restarts it after it. snapd is changing its user services this way in
[snapd pull request 16687](https://github.com/canonical/snapd/pull/16687), which needs pull requests 16685 and 16686.
Once a snapd with it is installed, the generated unit has `Requisite=` and `PartOf=graphical-session.target`, and the
drop-in's `[Unit]` section, the link and their lines in `configure-gnome-desktop.sh` can go. The pull request does not
change the launcher's exit status, so the `[Service]` section stays until snapd-desktop-integration does, as asked in
[snapd-desktop-integration issue 174](https://github.com/canonical/snapd-desktop-integration/issues/174).

### GDM does not fail at shutdown

GDM lists the users for the login screen through accountsservice, but its unit is not ordered relative to
accounts-daemon, so at shutdown systemd stopped both at once. When accounts-daemon was gone first, GDM logged "Failed
to list cached users" and exited with status 1, which left `gdm.service` failed at almost every shutdown. A drop-in
orders GDM after accounts-daemon, so that it is stopped first (`systemd/gdm.service.d/`, installed by
`configure-gnome-desktop.sh`). The same ordering is proposed for GDM's own unit in
[GDM merge request 418](https://gitlab.gnome.org/GNOME/gdm/-/merge_requests/418), and the drop-in can go once Ubuntu
ships a GDM which includes it.

### The encrypted disk is not closed while in use at shutdown

The installer writes the crypttab entry of the encrypted device holding the root filesystem without the option
`x-initrd.attach`, so at every shutdown systemd tried to close the device while the root filesystem on it was still
mounted, and `systemd-cryptsetup` failed with "Device or resource busy". The device is closed anyway in the final
phase of the shutdown, after the root filesystem is unmounted, and with the option systemd leaves it to that phase,
as systemd's crypttab(5) recommends for this device. `configure-gnome-desktop.sh` adds the option to the entry of
each encrypted device below `/`, keeps the previous file as `/etc/crypttab.bak`, and rebuilds the initramfs, which
keeps a copy of crypttab. The problem is reported as
[Launchpad bug 2152224](https://bugs.launchpad.net/ubuntu/+source/systemd/+bug/2152224). Fedora fixed it in its
installer with [anaconda pull request 6785](https://github.com/rhinstaller/anaconda/pull/6785).

### GNOME extensions

Extensions are installed from extensions.gnome.org and configured by `configure-gnome-desktop.sh`, with their
settings in `gnome-extensions/`. Custom extensions are installed from the repository.

## Requirements

- Ubuntu 26.04 desktop, with GNOME 50 on Wayland. The hardware parts (battery thresholds, touchscreen, power
  button light, and the BIOS settings below) assume a Framework Laptop 13 Pro.
- A few packages, installed before running the scripts:

  ```sh
  sudo apt install git devscripts sbuild mmdebstrap uidmap zstd
  ```

  Everything else the scripts use comes with Ubuntu's desktop installation, and the packages the configuration
  itself needs, such as GNOME Shell extensions, are installed by the scripts.

- Optionally a mail transport, such as `nullmailer`, which relays through your mail provider and queues mail
  while offline, with `MAILTO` set in `/etc/anacrontab`, so that the daily rebuild and the daily digest of
  failures and errors can mail their reports. Without one, their output is logged to syslog only.

## Applying everything

Run each script first without `--apply` to see what it would change. The general order is:

1. In the BIOS, set the battery charge limit to 99%[^charge-limit] and disable Battery Extender.
2. Add the Mozilla Team PPA with its source, from which Firefox is rebuilt, then lock down APT sources. The
   lockdown's checks expect the PPA, so it comes first:

   ```sh
   sudo add-apt-repository --enable-source ppa:mozillateam/ppa
   sudo ./lockdown-apt-sources.sh --apply
   ```

3. Build and install patched packages, then log out and back in. Between the previous step and the end of the build,
   do not run `apt upgrade`, which would install the PPA's unpatched Firefox until the rebuild replaces it. The first
   build takes about an hour, most of it Firefox:

   ```sh
   ./install-rebuild-patched-packages.sh --apply
   sudo rebuild-patched-packages --build
   sudo apt upgrade
   ```

4. Configure the desktop, the keyboard, and the terminal, in this order:

   ```sh
   ./configure-gnome-desktop.sh --apply
   ./configure-gnome-keys.sh --apply
   ./configure-ptyxis.sh --apply
   ```

5. Install the daily measurement of cooling and performance, and the daily mail of failures and errors:

   ```sh
   ./install-health-benchmark.sh --apply
   ./install-journal-digest.sh --apply
   ```

`./all.sh` runs steps 2 to 5 in this order, showing what each script would change, and `./all.sh --apply`
applies them all, leaving logging out and back in for the end. Run it as your own user, without sudo: it calls
sudo itself for the steps which need root, and asks for the password then.

The scripts shown without sudo run as your own user and call sudo themselves for the parts which need root.
`all.sh` and the configure scripts also refuse to run as root, since as root they have no session bus and
cannot reach your settings. The ones shown with sudo write only system files and refuse to run without it,
although their dry run works as your own user.
Each script's header documents what it does and why in more detail.

## GitHub mirror

There is also a [read-only GitHub mirror available](https://github.com/mitar/ubuntu-config),
if you need to fork the project there.

## License

This project is open source software released under the [Apache 2.0 license](./LICENSE).

[^charge-limit]:
    At 100% the firmware's own charge limit is switched off, and while it is off the firmware clears the battery
    charge sustainer every second, which also removes the thresholds set from Linux. Below 100% it sets its own
    range, from 5% below the limit up to the limit, only when the limit changes or the embedded controller starts,
    so the thresholds set from Linux stay in effect. See `modprobe.d/cros-charge-control.conf`.
