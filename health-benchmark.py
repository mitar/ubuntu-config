#!/usr/bin/python3 -I
"""Track how well this laptop cools itself and how fast its processor is, over months and years.

A measurement holds the fan at a full duty cycle and the power profile at performance for about 25 minutes, and
appends a row to /var/lib/health-benchmark/results.csv with what it found in each of its phases:

- idle: once the load has been low for a while, the temperatures, which stand in for the room's, and the fan's speed
  at a full duty cycle, which falls as the fan wears and rises as dust clogs the fins it blows through,
- benchmarks: 7-Zip's built-in benchmark on all threads and on one, and perf's context switch and system call
  benchmarks, with their scores and how long each ran,
- steady load: the temperature and the power once the heatsink has warmed up, which give the thermal resistance of
  the cooling in degrees per watt,
- cooldown: how fast the temperature falls after the load stops, given as the time constant of its decay.

The row also records the firmware, microcode, kernel and its mitigations, so that a change in the scores can be
traced to what changed with it. The samples read every second during the measurement are kept next to the results,
in traces/, so that they can be analyzed again later.

Without arguments it reports the latest measurement as a share of the best one recorded, which needs no root.


Usage
-----
  health-benchmark                       report the latest measurement against the best ones
  health-benchmark --run [--note TEXT]   measure now, once the load has dropped (root)
  health-benchmark --scheduled           measure if the laptop is on AC power with the screen locked, and skip
                                         otherwise (root, run once a day by health-benchmark.timer)
  health-benchmark --restore-fan         return the fan to the EC's automatic control if a measurement which ended
                                         abnormally left it at a fixed duty cycle (root)

A note marks a measurement in the report, for example "after cleaning the fan". A manual measurement first waits for
the load to drop. A measurement is not recorded when other processes used more than 1% of the CPU time during any
of its phases, since that would have cost the scores about as much. It is also abandoned when the screen is
unlocked during a scheduled measurement, AC power is disconnected, or the laptop suspends. These end it with a
message and a successful exit status, while failures, such as an EC which stopped reading its sensors, exit with
an error.


Comparability
-------------
Scores compare across years only if the benchmark itself stays the same, so 7-Zip is a static build of a pinned
release which install-health-benchmark.sh installs and verifies, and the report compares only measurements made
with the same binary. perf is updated with Ubuntu's packages, so its version is recorded. The fan runs at a
full duty cycle throughout, which keeps the scores apart from the state of the cooling: dust shows in the thermal
resistance and the cooldown, and reaches the scores only if the processor overheats even with the fan at full speed.
"""

import argparse
import collections
import contextlib
import csv
import datetime
import fcntl
import glob
import gzip
import hashlib
import math
import os
import platform
import re
import shutil
import signal
import statistics
import struct
import subprocess
import sys
import tempfile
import time

STATE_DIR = '/var/lib/health-benchmark'
RESULTS_FILE = f'{STATE_DIR}/results.csv'
TRACES_DIR = f'{STATE_DIR}/traces'
LOCK_FILE = '/run/health-benchmark.lock'
# Exists while a measurement holds the fan at a fixed duty cycle.
FAN_MARKER = '/run/health-benchmark.fan-fixed'
SEVENZIP = '/usr/local/lib/health-benchmark/7zzs'

PROFILE = 'performance'

# A phase is disturbed when other processes used more than this share of all CPU time during it. That costs the score
# on all threads about as much, and the changes the measurements are meant to show are a few percent.
FOREIGN_CPU_PERCENT = 1
# How long the load has to stay that low before a manual measurement starts, and how long it is waited for.
IDLE_REQUIRED_S = 180
IDLE_TIMEOUT_S = 1800
CLOCK_TICKS = os.sysconf('SC_CLK_TCK')

# The idle phase runs the fan at full speed for IDLE_PHASE_S, so that the air inside comes close to the room's, and
# averages its last AMBIENT_WINDOW_S.
IDLE_PHASE_S = 300
AMBIENT_WINDOW_S = 60
# The heatsink warms up over minutes, so the steady phase averages only its last STEADY_WINDOW_S.
STEADY_S = 600
STEADY_WINDOW_S = 240
# The cores cool within seconds of the load stopping and the heatsink over minutes. The fit skips the first
# COOLDOWN_SKIP_S, so that it follows the heatsink.
COOLDOWN_S = 300
COOLDOWN_SKIP_S = 20

# 7-Zip picks its dictionary size from the memory and the number of threads unless told, so it is fixed to 32 MB.
SEVENZIP_DICTIONARY = 25
SEVENZIP_PASSES_ALL = 3
SEVENZIP_PASSES_ONE = 3
PERF_PIPE_LOOPS = 3_000_000
PERF_SYSCALL_LOOPS = 100_000_000
# Both perf benchmarks run on one CPU, so that the pipe benchmark switches between its two tasks on one core rather
# than wherever the scheduler happens to place them. CPU 0 is one of the full Zen 5 cores.
PERF_CPU = 0

# At a full duty cycle the fan of this laptop spins at about 7600 RPM. Much less means it did not follow the command.
FAN_FULL_MIN_RPM = 4000
# Under the steady load the EC's sensor next to the APU warms by well over this. Less means the EC stopped reading
# its sensors, which leaves them at the values they last had.
EC_RISE_MIN_C = 5
# The APU's own temperature, as the EC reads it, may fail to read for a moment, but not for this many seconds in a row.
APU_SENSOR_FAILURES_MAX = 5
# A gap between the boot clock and the monotonic clock which grows by more than this means the system was suspended.
SUSPEND_GAP_S = 5

# struct gpu_metrics_v3_0 in drivers/gpu/drm/amd/include/kgd_pp_interface.h, at the offsets the kernel compiles it to.
# The table is identified by its size and revision, because amdgpu publishes it without describing its fields.
METRICS_SIZE = 264
METRICS_REVISION = 3
METRICS_TEMPERATURE_SOC = 6
METRICS_TEMPERATURE_CORE = 8
METRICS_TEMPERATURE_SKIN = 40
METRICS_AVERAGE_SOCKET_POWER = 112
# The entries of the per-core arrays which belong to a core on this processor: the four Zen 5 cores occupy the even
# entries 0 to 6 and the eight Zen 5c cores the entries 8 to 15. The other entries carry values unrelated to any core.
METRICS_CORE_SLOTS = (0, 2, 4, 6, 8, 9, 10, 11, 12, 13, 14, 15)
METRICS_UNSET = 0xFFFF
# Milliseconds spent limited by each reason since boot, in counters which wrap after about 49.7 days.
METRICS_RESIDENCY = {
    'prochot': 228,
    'spl': 232,
    'fppt': 236,
    'sppt': 240,
    'thm_core': 244,
    'thm_gfx': 248,
    'thm_soc': 252,
}
RESIDENCY_MODULUS = 2**32
POWER_LIMITS = ('spl', 'fppt', 'sppt')
THERMAL_LIMITS = ('thm_core', 'thm_gfx', 'thm_soc')

# The labels Framework's EC gives its temperature sensors, and the names they have here.
EC_SENSORS = {
    'local_f75303@4d': 'ec_local',
    'cpu_f75303@4d': 'ec_cpu',
    'ddr_f75303@4d': 'ec_ddr',
    'cpu@4c': 'ec_apu',
}

TEMPERATURES = ('tctl', 'core_max', 'soc', 'skin', 'ec_local', 'ec_cpu', 'ec_ddr', 'ec_apu')
TRACE_FIELDS = ('t', 'phase', 'busy_pct', 'power_w', 'fan_rpm', 'fan_target') + TEMPERATURES

FIELDS = [
    'timestamp', 'trigger', 'note', 'duration_s', 'foreign_cpu_pct', 'platform_profile',
    'bios_version', 'bios_date', 'ec_version', 'kernel', 'kernel_build', 'cmdline', 'microcode', 'cpu_model',
    'mem_total_kb', 'uptime_s', 'amd_pstate', 'governor', 'epp', 'vulnerabilities',
    'sevenzip_version', 'sevenzip_sha256', 'perf_version',
    'idle_busy_pct', 'idle_power_w', *(f'idle_{name}' for name in TEMPERATURES), 'fan_rpm_full',
    *(f'{prefix}_{name}' for prefix in ('sevenzip_all', 'sevenzip_one') for name in (
        'compress_mips', 'decompress_mips', 'mips', 'wall_s', 'power_w', 'tctl_max', 'thermal_pct', 'power_limit_pct')),
    *(f'{prefix}_{name}' for prefix in ('perf_pipe', 'perf_syscall') for name in ('ops', 'wall_s')),
    'steady_power_w', *(f'steady_{name}' for name in TEMPERATURES), 'steady_fan_rpm',
    'steady_thermal_pct', 'steady_power_limit_pct', 'steady_prochot_pct',
    'cooldown_tau_tctl_s', 'cooldown_tau_soc_s', 'cooldown_tau_ec_cpu_s', 'cooldown_end_tctl', 'cooldown_end_ec_cpu',
]


class Failure(Exception):
    """A measurement cannot be made or failed, for the reason in the message."""


class Skip(Exception):
    """A measurement was abandoned or is not recorded because its conditions did not hold, for the reason in the
    message. Unlike a Failure, this is an expected outcome."""


class Interrupted(BaseException):
    """A signal asked the process to stop. It is a BaseException so that only cleanup code handles it."""


def read_text(path):
    """Returns the contents of a file without surrounding whitespace, or None when it cannot be read."""
    try:
        with open(path) as file:
            return file.read().strip()
    except OSError:
        return None


def read_number(path, scale=1):
    """Returns the number a sysfs attribute holds divided by scale, or None when it cannot be read.

    Reading a hwmon attribute fails with ENODATA while its sensor is in an error state, which also gives None.
    """
    text = read_text(path)
    if text is None:
        return None
    try:
        return int(text) / scale
    except ValueError:
        return None


def find_hwmon(name):
    """Returns the directory of the hwmon device with the given name, or None. Their numbers change across boots."""
    for directory in sorted(glob.glob('/sys/class/hwmon/hwmon*')):
        if read_text(f'{directory}/name') == name:
            return directory
    return None


def read_cpu_times():
    """Returns the total and the idle CPU time since boot, in clock ticks, summed over all CPUs."""
    with open('/proc/stat') as file:
        fields = [int(value) for value in file.readline().split()[1:]]
    # The first eight fields are user, nice, system, idle, iowait, irq, softirq and steal. The guest times which follow
    # are already counted in user and nice.
    return sum(fields[:8]), fields[3] + fields[4]


def own_cpu_seconds():
    """Returns the CPU time used by this process and by those of its children which have been waited for, in seconds.
    """
    times = os.times()
    return times.user + times.system + times.children_user + times.children_system


def boot_gap():
    """Returns how far the boot clock, which counts time in suspend, is ahead of the monotonic clock, which does not."""
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()


def on_ac_power():
    """Returns whether any mains power supply is online."""
    for supply in glob.glob('/sys/class/power_supply/*'):
        if read_text(f'{supply}/type') == 'Mains' and read_text(f'{supply}/online') == '1':
            return True
    return False


def unlocked_session():
    """Returns whether a graphical user session is active and not locked, so whether someone may be using the laptop."""
    result = subprocess.run(['loginctl', 'list-sessions', '--no-legend'], capture_output=True, text=True)
    # Without the list of sessions nobody can be ruled out.
    if result.returncode != 0:
        return True
    for line in result.stdout.splitlines():
        session = line.split()[0]
        result = subprocess.run(
            ['loginctl', 'show-session', session, '-p', 'Class', '-p', 'Type', '-p', 'Active', '-p', 'LockedHint'],
            capture_output=True, text=True)
        # A session can end between the two calls, and then has nothing to show.
        if result.returncode != 0:
            continue
        properties = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if (properties.get('Class') == 'user' and properties.get('Type') in ('wayland', 'x11') and
                properties.get('Active') == 'yes' and properties.get('LockedHint') != 'yes'):
            return True
    return False


class Sensors:
    """Reads the temperatures, the power, the fan and the CPU load which a measurement samples."""

    def __init__(self):
        self.metrics_file = None
        for path in sorted(glob.glob('/sys/class/drm/card*/device/gpu_metrics')):
            try:
                with open(path, 'rb') as file:
                    if decode_metrics(file.read()) is not None:
                        self.metrics_file = path
                        break
            except OSError:
                continue
        if self.metrics_file is None:
            raise Failure('no device publishes a gpu_metrics table in the gpu_metrics_v3_0 layout')

        k10temp = find_hwmon('k10temp')
        self.tctl_file = None
        if k10temp:
            for label in glob.glob(f'{k10temp}/temp*_label'):
                if read_text(label) == 'Tctl':
                    self.tctl_file = label.replace('_label', '_input')
        if self.tctl_file is None:
            raise Failure('k10temp does not report Tctl')

        ec = find_hwmon('cros_ec')
        if ec is None:
            raise Failure('the cros_ec hwmon device is missing')
        self.fan_file = f'{ec}/fan1_input'
        self.fan_target_file = f'{ec}/fan1_target'
        self.ec_files = {}
        for label in glob.glob(f'{ec}/temp*_label'):
            name = EC_SENSORS.get(read_text(label))
            if name:
                self.ec_files[name] = label.replace('_label', '_input')
        missing = set(EC_SENSORS.values()) - set(self.ec_files)
        if missing:
            raise Failure(f'the EC does not report the sensors {", ".join(sorted(missing))}')

        self.cpu_times = read_cpu_times()

    def read(self):
        """Returns one sample: temperatures in degrees Celsius (None for a sensor which failed to read), the package
        power in watts, the fan speed and target in RPM, the share of CPU time busy since the previous sample, and the
        throttle residency counters."""
        with open(self.metrics_file, 'rb') as file:
            metrics = decode_metrics(file.read())
        if metrics is None:
            raise Failure('the gpu_metrics table changed its layout')
        total, idle = read_cpu_times()
        elapsed = total - self.cpu_times[0]
        busy = 100 * (elapsed - (idle - self.cpu_times[1])) / elapsed if elapsed > 0 else 0
        self.cpu_times = (total, idle)
        sample = {
            'busy_pct': busy,
            'power_w': metrics['power_w'],
            'fan_rpm': read_number(self.fan_file),
            'fan_target': read_number(self.fan_target_file),
            'tctl': read_number(self.tctl_file, 1000),
            'core_max': metrics['core_max'],
            'soc': metrics['soc'],
            'skin': metrics['skin'],
            'residency': metrics['residency'],
        }
        for name, path in self.ec_files.items():
            sample[name] = read_number(path, 1000)
        return sample


def decode_metrics(data):
    """Decodes a gpu_metrics table, or returns None when it is not in the gpu_metrics_v3_0 layout."""
    if len(data) != METRICS_SIZE:
        return None
    size, revision = struct.unpack_from('<HB', data, 0)
    if size != METRICS_SIZE or revision != METRICS_REVISION:
        return None
    core_max = None
    for slot in METRICS_CORE_SLOTS:
        (value,) = struct.unpack_from('<H', data, METRICS_TEMPERATURE_CORE + 2 * slot)
        if value != METRICS_UNSET:
            core_max = max(core_max or 0, value / 100)
    # Temperatures are in hundredths of a degree Celsius and power is in milliwatts.
    return {
        'core_max': core_max,
        'soc': struct.unpack_from('<H', data, METRICS_TEMPERATURE_SOC)[0] / 100,
        'skin': struct.unpack_from('<H', data, METRICS_TEMPERATURE_SKIN)[0] / 100,
        'power_w': struct.unpack_from('<I', data, METRICS_AVERAGE_SOCKET_POWER)[0] / 1000,
        'residency': {key: struct.unpack_from('<I', data, offset)[0] for key, offset in METRICS_RESIDENCY.items()},
    }


def residency_percent(first, last, keys):
    """Returns the largest share of the time between two samples which any of the given reasons limited the processor.
    """
    wall_ms = 1000 * (last['t'] - first['t'])
    if wall_ms <= 0:
        return None
    shares = [((last['residency'][key] - first['residency'][key]) % RESIDENCY_MODULUS) / wall_ms for key in keys]
    return 100 * max(shares)


def mean(samples, key):
    """Returns the mean of a value over samples, leaving out those in which it failed to read, or None."""
    values = [sample[key] for sample in samples if sample.get(key) is not None]
    return statistics.fmean(values) if values else None


def tail(samples, seconds):
    """Returns the samples from the last seconds of a list of samples."""
    end = samples[-1]['t']
    return [sample for sample in samples if sample['t'] >= end - seconds]


def fit_decay(points):
    """Fits y = a + b * exp(-t / tau) to (t, y) points by least squares.

    Returns tau in seconds, or None when there are too few points or they do not decay within the range searched.
    """
    if len(points) < 10:
        return None
    start = points[0][0]
    ts = [t - start for t, _ in points]
    ys = [y for _, y in points]

    # For a given tau the model is linear in a and b, so they come from ordinary least squares, which leaves a search
    # over tau alone.
    def residual(tau):
        xs = [math.exp(-t / tau) for t in ts]
        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        sxx = sum((x - mean_x) ** 2 for x in xs)
        if sxx == 0:
            return math.inf, 0
        b = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / sxx
        a = mean_y - b * mean_x
        return sum((a + b * x - y) ** 2 for x, y in zip(xs, ys)), b

    # A coarse geometric grid first, then a golden-section search between the neighbors of the best grid point.
    grid = [2 * 1.05**i for i in range(170)]
    errors = [residual(tau)[0] for tau in grid]
    best = min(range(len(grid)), key=errors.__getitem__)
    if best in (0, len(grid) - 1):
        return None
    low, high = grid[best - 1], grid[best + 1]
    ratio = (math.sqrt(5) - 1) / 2
    for _ in range(40):
        left = high - ratio * (high - low)
        right = low + ratio * (high - low)
        if residual(left)[0] < residual(right)[0]:
            high = right
        else:
            low = left
    tau = (low + high) / 2
    # A decay toward a lower temperature has a positive b. Anything else is not a cooldown.
    return tau if residual(tau)[1] > 0 else None


class Measurement:
    """Samples the sensors once a second through the phases of a measurement, accounts for the CPU time other
    processes use in each phase, and abandons the measurement when its conditions no longer hold: AC power, a locked
    screen for a scheduled one, no suspend, and working EC sensors."""

    def __init__(self, sensors, scheduled):
        self.sensors = sensors
        self.scheduled = scheduled
        self.start = time.monotonic()
        self.boot_gap = boot_gap()
        self.trace = []
        self.apu_failures = 0
        # The share of all CPU time which other processes used, by phase.
        self.foreign = {}

    @contextlib.contextmanager
    def accounted(self, phase):
        """Records in self.foreign[phase] the share of all CPU time which other processes used while the body runs.

        The body has to wait for every process it starts, because the CPU time of a child counts as this process's
        only once it has been waited for. Nothing is recorded when the body raises.
        """
        total, idle = read_cpu_times()
        own = own_cpu_seconds()
        yield
        total_after, idle_after = read_cpu_times()
        elapsed = total_after - total
        if elapsed <= 0:
            self.foreign[phase] = 0.0
            return
        # /proc/stat counts in clock ticks summed over all CPUs, so the elapsed total is the capacity of all of them.
        busy = (elapsed - (idle_after - idle)) / CLOCK_TICKS
        foreign = busy - (own_cpu_seconds() - own)
        self.foreign[phase] = max(0.0, 100 * foreign / (elapsed / CLOCK_TICKS))

    def describe_foreign(self):
        """Returns the share of CPU time other processes used in each phase so far, as text."""
        return ', '.join(f'{phase} {share:.2f}%' for phase, share in self.foreign.items())

    def check_foreign(self):
        """Raises Skip when other processes used more than FOREIGN_CPU_PERCENT of the CPU time in any phase so far."""
        phase = max(self.foreign, key=self.foreign.get)
        if self.foreign[phase] > FOREIGN_CPU_PERCENT:
            raise Skip(f'other processes used {self.foreign[phase]:.1f}% of the CPU time during {phase}, more than '
                       f'{FOREIGN_CPU_PERCENT}% ({self.describe_foreign()})')

    def sample(self, phase):
        """Reads one sample, checks the conditions, adds it to the trace and returns it."""
        sample = self.sensors.read()
        sample['t'] = time.monotonic() - self.start
        sample['phase'] = phase
        self.trace.append(sample)

        if boot_gap() - self.boot_gap > SUSPEND_GAP_S:
            raise Skip('the system was suspended')
        if not on_ac_power():
            raise Skip('AC power was disconnected')
        # loginctl is called only every few samples, since an unlock is noticed soon enough that way.
        if self.scheduled and len(self.trace) % 5 == 0 and unlocked_session():
            raise Skip('the screen was unlocked')
        if sample['ec_apu'] is None:
            self.apu_failures += 1
            if self.apu_failures >= APU_SENSOR_FAILURES_MAX:
                raise Failure('the EC cannot read the APU temperature')
        else:
            self.apu_failures = 0
        return sample

    def observe(self, phase, seconds=None, process=None):
        """Samples once a second for the given number of seconds, or until the given process exits, and returns the
        samples."""
        samples = []
        begin = time.monotonic()
        tick = begin
        while True:
            samples.append(self.sample(phase))
            if process is not None and process.poll() is not None:
                return samples
            if process is None and time.monotonic() - begin >= seconds:
                return samples
            tick += 1
            time.sleep(max(0, tick - time.monotonic()))

    def run(self, phase, command):
        """Runs a command while sampling, and returns its output, how long it ran in seconds, and the samples.

        Raises Failure when the command fails. The CPU time other processes use meanwhile is accounted to the phase.
        """
        with self.accounted(phase), tempfile.TemporaryFile() as output:
            begin = time.monotonic()
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
            try:
                samples = self.observe(phase, process=process)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
            wall = time.monotonic() - begin
            output.seek(0)
            text = output.read().decode(errors='replace')
        if process.returncode != 0:
            raise Failure(f'{command[0]} failed with exit code {process.returncode}: {text.strip()[-500:]}')
        return text, wall, samples


def framework_tool(*arguments):
    """Runs framework_tool with the given arguments, raising Failure when it fails."""
    path = shutil.which('framework_tool') or '/usr/bin/framework_tool'
    result = subprocess.run([path, *arguments], capture_output=True, text=True)
    if result.returncode != 0:
        raise Failure(f'framework_tool {" ".join(arguments)} failed: {(result.stdout + result.stderr).strip()}')


def fix_fan():
    """Holds the fan at a full duty cycle. The marker is created first, so that it exists whenever the fan might be
    fixed."""
    with open(FAN_MARKER, 'w'):
        pass
    framework_tool('--fansetduty', '100')


def restore_fan():
    """Returns the fan to the EC's automatic control."""
    framework_tool('--autofanctrl')
    with contextlib.suppress(FileNotFoundError):
        os.remove(FAN_MARKER)


def sevenzip(measurement, prefix, threads, passes):
    """Runs 7-Zip's benchmark and returns its results as columns with the given prefix."""
    command = [SEVENZIP, 'b', str(passes), f'-mmt{threads}', f'-md{SEVENZIP_DICTIONARY}']
    text, wall, samples = measurement.run(prefix, command)
    # The result is on the lines starting with Avr:, which has the speed, the CPU usage, the rating per usage and the
    # rating, for compressing and then for decompressing, and Tot:, which has the overall usage, rating per usage
    # and rating. Ratings are in MIPS.
    average = re.search(r'^Avr:\s+(.*)\|(.*)$', text, re.MULTILINE)
    total = re.search(r'^Tot:\s+(.*)$', text, re.MULTILINE)
    if not average or not total:
        raise Failure(f'7-Zip printed no result: {text.strip()[-500:]}')
    return {
        f'{prefix}_compress_mips': int(average.group(1).split()[3]),
        f'{prefix}_decompress_mips': int(average.group(2).split()[3]),
        f'{prefix}_mips': int(total.group(1).split()[2]),
        f'{prefix}_wall_s': wall,
        f'{prefix}_power_w': mean(samples, 'power_w'),
        f'{prefix}_tctl_max': max((s['tctl'] for s in samples if s['tctl'] is not None), default=None),
        f'{prefix}_thermal_pct': residency_percent(samples[0], samples[-1], THERMAL_LIMITS),
        f'{prefix}_power_limit_pct': residency_percent(samples[0], samples[-1], POWER_LIMITS),
    }


def perf_bench(measurement, prefix, arguments):
    """Runs a perf benchmark on PERF_CPU and returns its operations per second and how long it ran as columns with the
    given prefix, leaving them empty when perf fails, so that a broken perf does not cost the other results."""
    command = ['taskset', '-c', str(PERF_CPU), 'perf', 'bench', *arguments]
    try:
        text, wall, _ = measurement.run(prefix, command)
    except Failure as error:
        print(f'skipping {prefix}: {error}', flush=True)
        return {}
    match = re.search(r'^\s*([\d,]+)\s+ops/sec', text, re.MULTILINE)
    if not match:
        print(f'skipping {prefix}: perf printed no result', flush=True)
        return {}
    return {f'{prefix}_ops': int(match.group(1).replace(',', '')), f'{prefix}_wall_s': wall}


def averages(prefix, samples):
    """Returns the mean temperatures, power and fan speed of samples as columns with the given prefix."""
    columns = {f'{prefix}_power_w': mean(samples, 'power_w')}
    for name in TEMPERATURES:
        columns[f'{prefix}_{name}'] = mean(samples, name)
    return columns


def system_description():
    """Returns what describes the system a measurement ran on: firmware, microcode, kernel, mitigations and tools."""
    cpuinfo = read_text('/proc/cpuinfo') or ''
    meminfo = read_text('/proc/meminfo') or ''

    def search(pattern, text):
        match = re.search(pattern, text, re.MULTILINE)
        return match.group(1).strip() if match else None

    vulnerabilities = '; '.join(
        f'{os.path.basename(path)}: {read_text(path)}'
        for path in sorted(glob.glob('/sys/devices/system/cpu/vulnerabilities/*')))
    with open(SEVENZIP, 'rb') as file:
        sevenzip_sha256 = hashlib.sha256(file.read()).hexdigest()
    sevenzip_banner = subprocess.run([SEVENZIP, 'i'], capture_output=True, text=True).stdout
    perf = subprocess.run(['perf', '--version'], capture_output=True, text=True)
    return {
        'bios_version': read_text('/sys/class/dmi/id/bios_version'),
        'bios_date': read_text('/sys/class/dmi/id/bios_date'),
        'ec_version': search(r'^RW version:\s*(\S+)', read_text('/sys/class/chromeos/cros_ec/version') or ''),
        'kernel': platform.release(),
        'kernel_build': platform.version(),
        'cmdline': read_text('/proc/cmdline'),
        'microcode': search(r'^microcode\s*:\s*(\S+)', cpuinfo),
        'cpu_model': search(r'^model name\s*:\s*(.+)$', cpuinfo),
        'mem_total_kb': search(r'^MemTotal:\s*(\d+)', meminfo),
        'uptime_s': (read_text('/proc/uptime') or '').split(' ')[0],
        'amd_pstate': read_text('/sys/devices/system/cpu/amd_pstate/status'),
        'governor': read_text('/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor'),
        'epp': read_text('/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference'),
        'vulnerabilities': vulnerabilities,
        'sevenzip_version': search(r'^7-Zip \S+ (\S+)', sevenzip_banner),
        'sevenzip_sha256': sevenzip_sha256,
        'perf_version': perf.stdout.strip() if perf.returncode == 0 else None,
    }


def format_value(value):
    """Formats a value for the results file: floats with two decimals, None as empty."""
    if value is None:
        return ''
    if isinstance(value, float):
        return f'{value:.2f}'
    return str(value)


def append_result(row):
    """Appends a row to the results file, which root writes and everyone can read.

    When the file has other columns than FIELDS, it is rewritten with the columns of both, so that rows written by an
    older or a newer version of this script keep their values.
    """
    os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
    rows = []
    fields = list(FIELDS)
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, newline='') as file:
            reader = csv.DictReader(file)
            rows = list(reader)
            existing = reader.fieldnames or []
        if existing != FIELDS:
            fields = existing + [field for field in FIELDS if field not in existing]
    row = {key: format_value(value) for key, value in row.items()}
    if fields == FIELDS and os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, 'a', newline='') as file:
            csv.DictWriter(file, fieldnames=fields).writerow(row)
        return
    temporary = f'{RESULTS_FILE}.new'
    with open(temporary, 'w', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        writer.writerow(row)
    os.chmod(temporary, 0o644)
    os.replace(temporary, RESULTS_FILE)


def save_trace(timestamp, trace):
    """Writes the samples of a measurement to a compressed file in TRACES_DIR and returns its path."""
    os.makedirs(TRACES_DIR, mode=0o755, exist_ok=True)
    path = f'{TRACES_DIR}/{timestamp.replace(":", "")}.csv.gz'
    with gzip.open(path, 'wt', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=TRACE_FIELDS, extrasaction='ignore')
        writer.writeheader()
        for sample in trace:
            writer.writerow({key: format_value(sample.get(key)) for key in TRACE_FIELDS})
    os.chmod(path, 0o644)
    return path


def measure(trigger, note):
    """Makes one measurement with the fan at a full duty cycle and appends its result.

    The caller holds the power profile at performance. Raises Failure when the measurement cannot be made or is
    abandoned, and always returns the fan to the EC's automatic control.
    """
    profile = read_text('/sys/firmware/acpi/platform_profile')
    if profile != PROFILE:
        raise Failure(f'the platform profile is {profile} instead of {PROFILE}')
    if not os.access(SEVENZIP, os.X_OK):
        raise Failure(f'{SEVENZIP} is missing, run install-health-benchmark.sh')

    sensors = Sensors()
    measurement = Measurement(sensors, scheduled=trigger == 'scheduled')
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    row = {'timestamp': timestamp, 'trigger': trigger, 'note': note, 'platform_profile': profile}
    row.update(system_description())

    stop_signals = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)

    # A Ctrl-C reaches this process twice, from the terminal and passed on by powerprofilesctl, so later signals are
    # ignored from the first one on, because one arriving during the cleanup would cut it short.
    def interrupt(signum, _frame):
        for other in stop_signals:
            signal.signal(other, signal.SIG_IGN)
        raise Interrupted(signum)

    for signum in stop_signals:
        signal.signal(signum, interrupt)

    load = None
    try:
        fix_fan()

        print('idle: fan at full speed, measuring the temperatures at rest', flush=True)
        with measurement.accounted('idle'):
            samples = measurement.observe('idle', IDLE_PHASE_S)
        row['idle_busy_pct'] = mean(samples, 'busy_pct')
        # The other phases are checked at the end, but a system which is busy already is better left alone now than
        # after running the fan at full speed for another 20 minutes.
        measurement.check_foreign()
        window = tail(samples, AMBIENT_WINDOW_S)
        row.update(averages('idle', window))
        row['fan_rpm_full'] = mean(window, 'fan_rpm')
        if row['fan_rpm_full'] is None or row['fan_rpm_full'] < FAN_FULL_MIN_RPM:
            raise Failure(f'the fan reached only {row["fan_rpm_full"]} RPM at a full duty cycle')

        print('benchmarks: 7-Zip on all threads and on one, perf', flush=True)
        row.update(sevenzip(measurement, 'sevenzip_all', os.cpu_count(), SEVENZIP_PASSES_ALL))
        row.update(sevenzip(measurement, 'sevenzip_one', 1, SEVENZIP_PASSES_ONE))
        row.update(perf_bench(measurement, 'perf_pipe', ['sched', 'pipe', '-l', str(PERF_PIPE_LOOPS)]))
        row.update(perf_bench(measurement, 'perf_syscall', ['syscall', 'basic', '-l', str(PERF_SYSCALL_LOOPS)]))

        # 7-Zip's benchmark on all threads, with more passes than it can finish, is the steady load, so that the load
        # stays the same as long as the pinned binary does.
        print('steady load: 7-Zip on all threads until the heatsink has warmed up', flush=True)
        with measurement.accounted('steady'):
            load = subprocess.Popen(
                [SEVENZIP, 'b', '1000000', f'-mmt{os.cpu_count()}', f'-md{SEVENZIP_DICTIONARY}'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            window = tail(measurement.observe('steady', STEADY_S), STEADY_WINDOW_S)
            load.kill()
            load.wait()
            load = None
        row.update(averages('steady', window))
        row['steady_fan_rpm'] = mean(window, 'fan_rpm')
        row['steady_thermal_pct'] = residency_percent(window[0], window[-1], THERMAL_LIMITS)
        row['steady_power_limit_pct'] = residency_percent(window[0], window[-1], POWER_LIMITS)
        row['steady_prochot_pct'] = residency_percent(window[0], window[-1], ('prochot',))
        if row['steady_ec_cpu'] is None or row['steady_ec_cpu'] - row['idle_ec_cpu'] < EC_RISE_MIN_C:
            raise Failure('the EC sensor next to the APU did not warm up under load, the EC has probably stopped '
                          'reading its sensors (a suspend and resume makes it read them again)')

        print('cooldown: measuring how fast the temperature falls', flush=True)
        with measurement.accounted('cooldown'):
            samples = measurement.observe('cooldown', COOLDOWN_S)
        start = samples[0]['t']
        for name in ('tctl', 'soc', 'ec_cpu'):
            points = [(s['t'], s[name]) for s in samples if s['t'] - start >= COOLDOWN_SKIP_S and s[name] is not None]
            row[f'cooldown_tau_{name}_s'] = fit_decay(points)
        row['cooldown_end_tctl'] = mean(samples[-10:], 'tctl')
        row['cooldown_end_ec_cpu'] = mean(samples[-10:], 'ec_cpu')
    finally:
        # A signal would interrupt the cleanup, which must run to its end.
        for signum in stop_signals:
            signal.signal(signum, signal.SIG_IGN)
        if load is not None:
            load.kill()
            load.wait()
        # A failure here must not hide the one which ended the measurement. The marker stays, so that the next
        # scheduled attempt and the service's stop command try again.
        try:
            restore_fan()
        except Failure as error:
            print(f'health-benchmark: {error}', file=sys.stderr, flush=True)

    row['duration_s'] = time.monotonic() - measurement.start
    row['foreign_cpu_pct'] = max(measurement.foreign.values())
    print(f'other processes used of the CPU time: {measurement.describe_foreign()}', flush=True)
    measurement.check_foreign()
    append_result(row)
    trace = save_trace(timestamp, measurement.trace)
    print(f'done in {row["duration_s"] / 60:.0f} minutes, results in {RESULTS_FILE}, samples in {trace}', flush=True)


@contextlib.contextmanager
def measurement_lock():
    """Holds the lock which keeps two measurements from running at once. Yields whether it was acquired."""
    with open(LOCK_FILE, 'w') as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def wait_for_idle():
    """Waits until at most FOREIGN_CPU_PERCENT of all CPU time has been busy over the last IDLE_REQUIRED_S.

    Returns None when that happened, or the reason it stopped waiting: IDLE_TIMEOUT_S passing or a loss of AC power.
    """
    begin = time.monotonic()
    # Readings of the CPU times every few seconds, of which the oldest is kept just over IDLE_REQUIRED_S old, so that
    # the share is an average over that long and a short burst of activity does not start the wait over.
    readings = collections.deque([(begin, *read_cpu_times())])
    while True:
        time.sleep(5)
        now = time.monotonic()
        total, idle = read_cpu_times()
        readings.append((now, total, idle))
        while len(readings) > 2 and now - readings[1][0] >= IDLE_REQUIRED_S:
            readings.popleft()
        then, total_then, idle_then = readings[0]
        elapsed = total - total_then
        if now - then >= IDLE_REQUIRED_S and elapsed > 0:
            if 100 * (elapsed - (idle - idle_then)) / elapsed <= FOREIGN_CPU_PERCENT:
                return None
        if now - begin >= IDLE_TIMEOUT_S:
            return f'the load did not stay at or below {FOREIGN_CPU_PERCENT}% for {IDLE_REQUIRED_S} seconds'
        if not on_ac_power():
            return 'AC power was disconnected'


def launch(trigger, note):
    """Runs a measurement in a child process which holds the power profile at performance while it runs, and returns
    its exit code. The fan is restored here too, in case the child could not do it itself."""
    command = [
        'powerprofilesctl', 'launch', '--profile', PROFILE, '--reason', 'Measuring cooling and performance',
        '--appid', 'health-benchmark', '--',
        sys.executable, '-I', os.path.realpath(__file__), '--measure', '--trigger', trigger,
    ]
    if note:
        command += ['--note', note]
    process = subprocess.Popen(command)
    try:
        return process.wait()
    finally:
        # powerprofilesctl passes SIGINT and SIGTERM on to the child, which then cleans up, so waiting for it here
        # avoids restoring the fan while the child still holds it.
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=60)
        if os.path.exists(FAN_MARKER):
            try:
                restore_fan()
            except Failure as error:
                print(f'health-benchmark: {error}', file=sys.stderr, flush=True)


def require_root():
    """Exits with an error unless running as root."""
    if os.geteuid() != 0:
        sys.exit('health-benchmark: measuring requires root, because it sets the fan through the EC')


def run_manual(note):
    """Measures now, after waiting for the load to drop."""
    require_root()
    os.umask(0o022)
    with measurement_lock() as acquired:
        if not acquired:
            sys.exit('health-benchmark: another measurement is running')
        if not on_ac_power():
            sys.exit('health-benchmark: a measurement requires AC power')
        print(f'waiting for the load to stay at or below {FOREIGN_CPU_PERCENT}% for {IDLE_REQUIRED_S} seconds',
              flush=True)
        reason = wait_for_idle()
        if reason:
            sys.exit(f'health-benchmark: {reason}')
        sys.exit(launch('manual', note))


def run_scheduled():
    """Measures if the laptop is on AC power with the screen locked, and otherwise skips, until the next day."""
    require_root()
    os.umask(0o022)
    with measurement_lock() as acquired:
        if not acquired:
            print('not measuring: another measurement is running', flush=True)
            return
        if os.path.exists(FAN_MARKER):
            print('returning the fan to automatic control, which an earlier measurement left fixed', flush=True)
            restore_fan()
        if not on_ac_power():
            print('not measuring: not on AC power', flush=True)
            return
        if unlocked_session():
            print('not measuring: the screen is unlocked', flush=True)
            return
        sys.exit(launch('scheduled', None))


def run_restore_fan():
    """Returns the fan to automatic control if a measurement left it fixed and none is running."""
    require_root()
    with measurement_lock() as acquired:
        if acquired and os.path.exists(FAN_MARKER):
            restore_fan()


def read_results():
    """Returns the rows of the results file as dicts, or an empty list when there is none yet."""
    try:
        with open(RESULTS_FILE, newline='') as file:
            return list(csv.DictReader(file))
    except FileNotFoundError:
        return []


def number(row, key):
    """Returns a column of a row as a float, or None when it is empty or missing."""
    try:
        return float(row.get(key) or '')
    except ValueError:
        return None


def thermal_resistance(row):
    """Returns how many degrees the hottest point of the APU rises above the air inside per watt, under the steady
    load. The EC's board sensor at idle with the fan at full speed stands in for the air, since no sensor measures
    the room."""
    tctl, ambient, power = number(row, 'steady_tctl'), number(row, 'idle_ec_local'), number(row, 'steady_power_w')
    if tctl is None or ambient is None or not power:
        return None
    return (tctl - ambient) / power


# label, how to read the value from a row, unit, format, and whether higher is better
COOLING = [
    ('Thermal resistance', thermal_resistance, 'C/W', '.3f', False),
    ('Cooldown time constant', lambda row: number(row, 'cooldown_tau_ec_cpu_s'), 's', '.0f', False),
]
PERFORMANCE = [
    ('7-Zip, all threads', lambda row: number(row, 'sevenzip_all_mips'), 'MIPS', '.0f', True),
    ('7-Zip, one thread', lambda row: number(row, 'sevenzip_one_mips'), 'MIPS', '.0f', True),
    ('Context switches', lambda row: number(row, 'perf_pipe_ops'), 'ops/s', '.0f', True),
    ('System calls', lambda row: number(row, 'perf_syscall_ops'), 'ops/s', '.0f', True),
]
# Columns which can explain a change in the scores, shown when they differ between two measurements.
ATTRIBUTION = ['bios_version', 'ec_version', 'microcode', 'kernel', 'cmdline', 'amd_pstate', 'epp', 'mem_total_kb',
               'perf_version', 'vulnerabilities']


def best(rows, read, higher):
    """Returns the best value of a metric over rows and the row it came from, or (None, None)."""
    scored = [(value, row) for row in rows if (value := read(row)) is not None and value > 0]
    if not scored:
        return None, None
    return (max if higher else min)(scored, key=lambda pair: pair[0])


def share(value, best_value, higher):
    """Returns a value as a percentage of the best one, where 100% is the best."""
    if value is None or not best_value:
        return None
    return 100 * (value / best_value if higher else best_value / value)


def performance_share(row, bests):
    """Returns the geometric mean of a row's performance shares, or None when it has none."""
    shares = [share(read(row), bests[label], higher) for label, read, _, _, higher in PERFORMANCE]
    shares = [value for value in shares if value]
    return math.exp(statistics.fmean(math.log(value) for value in shares)) if shares else None


def local_date(row):
    """Returns the local date of a row's measurement."""
    return datetime.datetime.fromisoformat(row['timestamp']).astimezone().strftime('%Y-%m-%d')


def describe_changes(old, new):
    """Returns lines describing the attribution columns which differ between two rows."""
    lines = []
    for key in ATTRIBUTION:
        before, after = old.get(key, ''), new.get(key, '')
        if before == after:
            continue
        if key == 'vulnerabilities':
            entries_before = dict(entry.split(': ', 1) for entry in before.split('; ') if ': ' in entry)
            entries_after = dict(entry.split(': ', 1) for entry in after.split('; ') if ': ' in entry)
            for name in sorted(set(entries_before) | set(entries_after)):
                if entries_before.get(name) != entries_after.get(name):
                    lines.append(f'    {name}: {entries_before.get(name, "-")} -> {entries_after.get(name, "-")}')
        else:
            lines.append(f'    {key}: {before or "-"} -> {after or "-"}')
    return lines


def report():
    """Prints the latest measurement as a share of the best ones made under the same conditions."""
    rows = read_results()
    if not rows:
        print(f'No measurements in {RESULTS_FILE} yet.')
        return
    latest = rows[-1]
    # Only measurements with the same 7-Zip binary and power profile compare.
    rows = [row for row in rows if row.get('sevenzip_sha256') == latest.get('sevenzip_sha256') and
            row.get('platform_profile') == latest.get('platform_profile')]
    print(f'{len(rows)} comparable measurements from {local_date(rows[0])} to {local_date(rows[-1])}, the latest '
          f'{latest["trigger"]}' + (f', note: {latest["note"]}' if latest.get('note') else ''))

    def section(title, metrics):
        print()
        print(f'{title:<28}{"latest":>16}{"best":>16}  {"date":<10}{"share":>7}')
        bests = {}
        for label, read, unit, spec, higher in metrics:
            best_value, best_row = best(rows, read, higher)
            bests[label] = best_value
            value = read(latest)
            if best_value is None:
                continue
            ratio = share(value, best_value, higher)
            print(f'  {label:<26}' + (f'{value:{spec}} {unit:>5}' if value is not None else '-').rjust(16) +
                  f'{best_value:{spec}} {unit:>5}'.rjust(16) + f'  {local_date(best_row):<10}' +
                  (f'{ratio:6.1f}%' if ratio is not None else '-'.rjust(7)))
        return bests

    cooling_bests = section('Cooling', COOLING)

    print()
    fan = [(number(row, 'fan_rpm_full'), row) for row in rows if number(row, 'fan_rpm_full')]
    if fan:
        value = number(latest, 'fan_rpm_full')
        first, highest = fan[0][0], max(rpm for rpm, _ in fan)
        print(f'{"Fan at a full duty cycle":<28}' + (f'{value:.0f} RPM' if value else '-').rjust(16) +
              f'{highest:.0f} RPM'.rjust(16) + '  highest, ' + f'{first:.0f} RPM first')

    performance_bests = section('Performance', PERFORMANCE)
    shares = [(performance_share(row, performance_bests), row) for row in rows]
    shares = [(value, row) for value, row in shares if value]
    if shares:
        top_value, top_row = max(shares, key=lambda pair: pair[0])
        latest_share = performance_share(latest, performance_bests)
        print(f'  {"Overall (geometric mean)":<26}' + ''.rjust(32) + f'  {"":<10}' +
              (f'{latest_share:6.1f}%' if latest_share else '-'.rjust(7)))
        changes = describe_changes(top_row, latest)
        if changes:
            print()
            print(f'Changed since the measurement with the best overall performance ({local_date(top_row)}):')
            print('\n'.join(changes))

    print()
    print(f'{"Recent":<12}{"trigger":<11}{"cooling":>8}{"fan RPM":>9}{"perf":>8}  note')
    best_resistance = cooling_bests.get('Thermal resistance')
    for row in rows[-10:]:
        cooling = share(thermal_resistance(row), best_resistance, False)
        perf = performance_share(row, performance_bests)
        rpm = number(row, 'fan_rpm_full')
        print(f'{local_date(row):<12}{row["trigger"]:<11}' + (f'{cooling:7.1f}%' if cooling else '-'.rjust(8)) +
              (f'{rpm:9.0f}' if rpm else '-'.rjust(9)) + (f'{perf:7.1f}%' if perf else '-'.rjust(8)) +
              f'  {row.get("note") or ""}')


def main():
    parser = argparse.ArgumentParser(
        description='Track how well this laptop cools itself and how fast its processor is. Without options, report '
                    'the latest measurement against the best ones.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--run', action='store_true', help='measure now, once the load has dropped (root)')
    mode.add_argument('--scheduled', action='store_true',
                      help='measure if the laptop is on AC power with the screen locked, and skip otherwise (root)')
    mode.add_argument('--restore-fan', action='store_true',
                      help='return the fan to automatic control if a measurement left it fixed (root)')
    mode.add_argument('--measure', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--note', help='a note to record with a measurement, such as "after cleaning the fan"')
    parser.add_argument('--trigger', default='manual', help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.note and not (args.run or args.measure):
        parser.error('--note applies only to --run')
    try:
        if args.run:
            run_manual(args.note)
        elif args.scheduled:
            run_scheduled()
        elif args.restore_fan:
            run_restore_fan()
        elif args.measure:
            require_root()
            os.umask(0o022)
            measure(args.trigger, args.note)
        else:
            report()
    except Failure as error:
        sys.exit(f'health-benchmark: {error}')
    except Skip as reason:
        print(f'health-benchmark: not recording: {reason}', flush=True)
    except Interrupted:
        sys.exit('health-benchmark: interrupted')
    except KeyboardInterrupt:
        sys.exit('health-benchmark: interrupted')


if __name__ == '__main__':
    main()
