#!/usr/bin/python3 -I
"""Report the units which failed and the errors logged since the previous report, for a daily mail.

Reads the systemd journal, of the system and of every user, and prints:

- the units which failed, grouped by unit and result, with when and how often, and the last lines the latest failed
  run logged, which usually say why,
- the messages logged at priority err or more urgent, grouped by what logged them and by their text with the numbers
  in it left out, with when and how often.

It prints nothing when there is nothing to report, so that cron mails only when there is. Each report starts right
after the last journal entry the previous one read, so that days on which it did not run are covered by the next
report, and no entry is reported twice or left out. The position is kept as a journal cursor, which follows the
order in which journald wrote the entries, so that it holds even when the clock jumps.

Patterns in /etc/journal-digest/ignore leave out the failures and messages they match: one Python regular expression
per line, searched for in the line the report shows, "unit (system): result" or "unit (user): result" for a failure
and "identifier: message" for a message. Empty lines and lines starting with # are skipped.


Usage
-----
  journal-digest                 report since the previous report and remember where this one ended (root)
  journal-digest --since TIME    report since TIME, in any form journalctl accepts, such as -1d or -7d, without
                                 remembering anything, to see what a report holds
  journal-digest --ignore FILE   read the ignore patterns from FILE, for example to try out patterns before
                                 installing them, with --since
"""

import argparse
import collections
import datetime
import json
import os
import re
import subprocess
import sys
import time

STATE_DIR = '/var/lib/journal-digest'
# Holds the cursor of the last journal entry the previous report read, and when that report was made.
STATE_FILE = f'{STATE_DIR}/state.json'
IGNORE_FILE = '/etc/journal-digest/ignore'

# The message ID systemd logs, both as the system manager and as a user's manager, when a unit fails.
UNIT_FAILED = 'd9b373ed55a64feb8242e02dbe79a49c'
# Priority err and the more urgent ones.
ERROR_PRIORITIES = ('0', '1', '2', '3')
# What the first report covers, when there is no previous one.
FIRST_REPORT_S = 24 * 3600
CONTEXT_LINES = 5
MESSAGE_WIDTH = 300

FIELDS = [
    'MESSAGE', 'MESSAGE_ID', 'PRIORITY', 'SYSLOG_IDENTIFIER', '_COMM', 'UNIT', 'USER_UNIT', 'UNIT_RESULT',
    'INVOCATION_ID', 'USER_INVOCATION_ID',
]


def journal(arguments):
    """Runs journalctl with the given arguments and returns the entries it prints, as dicts.

    Exits with an error when journalctl fails.
    """
    result = subprocess.run(
        ['journalctl', '--no-pager', '--quiet', '--output=json', f'--output-fields={",".join(FIELDS)}', *arguments],
        capture_output=True)
    if result.returncode != 0:
        sys.exit(f'journal-digest: journalctl failed: {result.stderr.decode(errors="replace").strip()}')
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def text(value):
    """Returns a journal field as text. The JSON output gives a field which is not valid UTF-8 as a list of bytes."""
    if value is None:
        return ''
    if isinstance(value, list):
        return bytes(value).decode(errors='replace')
    return value


def identifier(entry):
    """Returns what logged an entry."""
    return text(entry.get('SYSLOG_IDENTIFIER')) or text(entry.get('_COMM')) or 'unknown'


def message(entry):
    """Returns the message of an entry on one line, shortened to MESSAGE_WIDTH."""
    line = ' '.join(text(entry.get('MESSAGE')).split())
    return line if len(line) <= MESSAGE_WIDTH else line[:MESSAGE_WIDTH - 3] + '...'


def entry_time(entry):
    """Returns when an entry was logged, as a local datetime."""
    return datetime.datetime.fromtimestamp(int(entry['__REALTIME_TIMESTAMP']) / 1e6).astimezone()


def read_ignore_patterns(path, required):
    """Returns the compiled patterns of an ignore file. A missing file has none, unless it is required.

    Exits with an error when a required file is missing, or naming the line when a pattern is not a valid regular
    expression.
    """
    patterns = []
    try:
        with open(path) as file:
            lines = file.read().splitlines()
    except FileNotFoundError:
        if required:
            sys.exit(f'journal-digest: {path} does not exist')
        return patterns
    for number, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        try:
            patterns.append(re.compile(line))
        except re.error as error:
            sys.exit(f'journal-digest: {path}:{number}: {error}')
    return patterns


def failure_context(entry):
    """Returns the last lines logged by the failed run of a unit, and by its manager about that run, before it failed.
    """
    invocation = text(entry.get('INVOCATION_ID')) or text(entry.get('USER_INVOCATION_ID'))
    if not invocation:
        return []
    until = int(entry['__REALTIME_TIMESTAMP']) / 1e6
    # A unit's own output carries its invocation in _SYSTEMD_INVOCATION_ID, and what the manager logs about it in
    # INVOCATION_ID, or USER_INVOCATION_ID for a user's manager. The terms joined by + match any of them.
    entries = journal([
        f'--until=@{until:.6f}', f'--lines={CONTEXT_LINES + 1}',
        f'_SYSTEMD_INVOCATION_ID={invocation}', '+', f'INVOCATION_ID={invocation}', '+',
        f'USER_INVOCATION_ID={invocation}',
    ])
    lines = [f'{entry_time(e):%H:%M:%S} {identifier(e)}: {message(e)}'
             for e in entries if text(e.get('MESSAGE_ID')) != UNIT_FAILED]
    return lines[-CONTEXT_LINES:]


def when(group):
    """Returns when the entries of a group were logged, as text."""
    first, last = f'{entry_time(group["first"]):%Y-%m-%d %H:%M}', f'{entry_time(group["last"]):%Y-%m-%d %H:%M}'
    return first if first == last else f'{first} to {last}'


def report(start, label, patterns):
    """Prints the failures and errors logged from where the journalctl arguments in start begin to the end of the
    journal, under a heading naming label as the start. Leaves out what the patterns match, and prints nothing when
    nothing is left.

    Returns the cursor of the last entry read, or None when no entry matched.
    """
    entries = journal([*start, *(f'PRIORITY={priority}' for priority in ERROR_PRIORITIES), '+',
                       f'MESSAGE_ID={UNIT_FAILED}'])

    failures = collections.OrderedDict()
    errors = collections.OrderedDict()
    for entry in entries:
        if text(entry.get('MESSAGE_ID')) == UNIT_FAILED:
            scope = 'user' if entry.get('USER_UNIT') else 'system'
            line = f'{text(entry.get("UNIT") or entry.get("USER_UNIT"))} ({scope}): {text(entry.get("UNIT_RESULT"))}'
            groups, key = failures, line
        else:
            line = f'{identifier(entry)}: {message(entry)}'
            # Entries which differ only in numbers, such as process IDs, addresses and counters, are the same message.
            groups, key = errors, re.sub(r'[0-9]+', 'N', line)
        if any(pattern.search(line) for pattern in patterns):
            continue
        group = groups.setdefault(key, {'count': 0, 'first': entry})
        group['count'] += 1
        group['last'] = entry
        group['line'] = line

    if failures or errors:
        print(f'Since {label}:')
    if failures:
        print()
        print(f'Failed units ({sum(group["count"] for group in failures.values())}):')
        for group in sorted(failures.values(), key=lambda group: int(group['last']['__REALTIME_TIMESTAMP'])):
            print()
            print(f'  {group["count"]:>4}x  {group["line"]}')
            print(f'         {when(group)}')
            context = failure_context(group['last'])
            if context:
                print('         the latest failed run logged last:')
                for line in context:
                    print(f'           {line}')
    if errors:
        print()
        print(f'Errors ({sum(group["count"] for group in errors.values())}):')
        for group in sorted(errors.values(), key=lambda group: -group['count']):
            print()
            print(f'  {group["count"]:>4}x  {group["line"]}')
            print(f'         {when(group)}')

    return entries[-1]['__CURSOR'] if entries else None


def newest_cursor():
    """Returns the cursor of the newest entry in the journal, or None when the journal is empty."""
    entries = journal(['--lines=1'])
    return entries[-1]['__CURSOR'] if entries else None


def read_state():
    """Returns the state the previous report left, with its cursor and time, or an empty one when there is none.

    A state which cannot be read is reported and treated as none.
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
        print(f'journal-digest: ignoring {STATE_FILE}, which cannot be read: {error}', file=sys.stderr)
        return {}


def write_state(state):
    """Replaces the state file with the given state."""
    os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
    temporary = f'{STATE_FILE}.new'
    with open(temporary, 'w') as file:
        json.dump(state, file)
        file.write('\n')
    os.replace(temporary, STATE_FILE)


def format_time(seconds):
    """Returns a time in seconds since the epoch as local time, to the minute."""
    return f'{datetime.datetime.fromtimestamp(seconds).astimezone():%Y-%m-%d %H:%M}'


def main():
    parser = argparse.ArgumentParser(
        description='Report the units which failed and the errors logged since the previous report.')
    parser.add_argument('--since', help='report since this time, in any form journalctl accepts, such as -1d or -7d, '
                                        'without remembering anything')
    parser.add_argument('--ignore', metavar='FILE', help=f'read the ignore patterns from FILE instead of {IGNORE_FILE}')
    args = parser.parse_args()
    patterns = read_ignore_patterns(args.ignore or IGNORE_FILE, required=args.ignore is not None)

    if args.since:
        report([f'--since={args.since}'], args.since, patterns)
        return

    if os.geteuid() != 0:
        sys.exit('journal-digest: remembering where a report ended requires root, use --since to only report')
    state = read_state()
    now = time.time()
    # Read before the report, so that when nothing matched, the next report starts after the entry which was the newest
    # then, and still reads what was logged while this one ran.
    newest = newest_cursor()
    if state.get('cursor'):
        start = [f'--after-cursor={state["cursor"]}']
        label = format_time(state['time']) if isinstance(state.get('time'), (int, float)) else 'the previous report'
    else:
        since = now - FIRST_REPORT_S
        start = [f'--since=@{since:.6f}']
        label = format_time(since)
    last = report(start, label, patterns)
    sys.stdout.flush()

    # The position is remembered only once the report is printed, so that one which failed is made again next time.
    # When entries matched, the next report starts after the last of them, otherwise after the entry which was the
    # newest before reading. Entries between the two did not match, so either skips nothing and repeats nothing.
    write_state({'cursor': last or newest or state.get('cursor'), 'time': now})


if __name__ == '__main__':
    main()
