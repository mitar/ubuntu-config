# Ubuntu desktop configuration

Scripts which turn an Ubuntu desktop installation into a
[malleable computer](https://world.hey.com/dhh/the-malleable-computer-7c187a9b): a system whose behavior is
changed to fit its user, not only its settings. On Linux the whole system is open source, so when a setting is
not enough, the program itself can be changed. With AI assistance, doing so has become practical even for parts
of the system one has never looked into, like the terminal or the window manager.

The configuration here is highly opinionated: it is how I like my computer. But the code is free to reuse. Take
whatever helps and adapt it to your own liking.

It targets Ubuntu 26.04 with GNOME 50 on a Framework Laptop 13 Pro.

## How it works

Most of the configuration is done by scripts which write settings (mostly through gsettings and dconf, and a few
system files through sudo). Every script by default only shows what it would change, and changes things only
when run with `--apply`. They are idempotent, so they can be rerun at any time to bring the system back to the
configuration or to see how it has drifted from it.

Where a setting is not enough, Ubuntu's own packages are patched. `patches/<source-package>/` contains patches
which are applied on top of Ubuntu's source package, together with tests for the patched behavior which run during
the build. `rebuild-patched-packages.sh` rebuilds a package whenever Ubuntu publishes a new version of it or its
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

What it takes:

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

What it takes:

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
hosts, so a vendor repository cannot get its packages installed unattended by claiming to be Ubuntu's security
pocket.

### Patched packages kept up to date

Patched packages are rebuilt whenever Ubuntu ships a new version or their patches change, and published to a local
APT repository which a daily cron job keeps current (`rebuild-patched-packages.sh`,
`install-rebuild-patched-packages.sh`, `cron.daily/`). They are built the way Ubuntu builds its own packages,
with sbuild in a throwaway chroot without network access, with build dependencies installed only inside it. Its
unshare mode needs no root even to set up the chroot, so everything runs as an unprivileged system user, and a
package's build scripts and test suite cannot change the system.

### Battery charge thresholds on Framework laptops

GNOME's Preserve Battery Health keeps the battery between 40% and 60% while the laptop is docked most of the time,
and a quick settings toggle switches back to charging to 100% before traveling.

What it takes:

- The kernel's charge control enabled, which it keeps disabled on Framework laptops by default
  (`modprobe.d/cros-charge-control.conf`).
- The thresholds for UPower, which applies them for Preserve Battery Health (`udev/90-battery-charge-limit.rules`).
- The Preserve Battery Health extension for the quick settings toggle (`configure-gnome-desktop.sh`).
- The estimated time until the battery is empty or full, shown under the battery percentage in quick settings.
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
seconds after the hottest core reaches 70 C, and under lighter loads before the cores get there. The other is that
none of the board readings has changed for 2 minutes while the hottest core has been at 70 C or more for the last 30
seconds in a row: the readings are in whole degrees and can all stay the same for minutes while the processor idles,
but once it heats up, at least one of them changes within seconds. The warning stays until the sign which raised it
is gone, so until a fan spins again or a board reading changes.

### Windows keep their size when the screen locks

GNOME Shell disables most extensions while the screen is locked, the dock among them, and enables them again after
unlocking. The dock reserves its part of the screen only while it is enabled, so windows snapped to a side or
maximized would grow while the screen is locked and shrink back a moment after unlocking. A terminal resizes its
contents with the window, which garbles the screen of programs like Claude Code running in it. The Reserve Space
extension stays enabled on the lock screen and keeps an invisible copy of every part of the screen reserved by the
dock, the top bar or another panel, so the space left for windows does not change. The copies follow the reserved
space a few seconds after it changes, for example when the dock is moved to another side of the screen.

### Magnet links open in a running Transmission

Opening a magnet link or a torrent file while Transmission already runs starts a second Transmission, which passes
the link over the session bus to the one already running and exits. Ubuntu confines Transmission with an AppArmor
profile which allows it to own its name on the session bus, but not these calls between two of its instances, so
without rules added to the profile (`apparmor.d/local/transmission-gtk`) the link is lost and nothing happens. The
missing rules are reported in [Ubuntu bug 2169433](https://bugs.launchpad.net/ubuntu/+source/transmission/+bug/2169433).

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
  while offline, with `MAILTO` set in `/etc/anacrontab`, so that the daily rebuild can mail its reports. Without
  one, its failures are logged to syslog only.

## Applying everything

Run each script first without `--apply` to see what it would change. The general order is:

1. In the BIOS, set the battery charge limit to 99%[^charge-limit] and disable Battery Extender.
2. Lock down APT sources:

   ```sh
   sudo ./lockdown-apt-sources.sh --apply
   ```

3. Build and install patched packages, then log out and back in:

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

`./all.sh` runs steps 2 to 4 in this order, showing what each script would change, and `./all.sh --apply`
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
