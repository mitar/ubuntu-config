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
a local APT repository. That repository is pinned above the Ubuntu archive, so the next ordinary `apt upgrade`
installs the patched build. A daily cron job keeps patched packages current, running root-owned copies of the
script and patches, installed with `install-rebuild-patched-packages.sh`.

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
version under the same name (`lockdown-apt-sources.sh`).

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
applies them all, leaving logging out and back in for the end.

The scripts run as your own user and call sudo themselves for the parts which need root.
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
