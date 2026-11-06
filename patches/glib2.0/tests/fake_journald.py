#!/usr/bin/env python3
#
# Listens where journald does for syslog messages and for the output of processes, and records both. See README.
#
# Usage: fake_journald.py OUT_DIR
#
# Runs as root in the container of run-scope-test.sh, which has no journald. Creates /dev/log, where syslog() sends
# its messages, and /run/systemd/journal/stdout. GLib treats a process's standard error as the journal when it is
# connected to that socket. Every datagram received on /dev/log is appended to OUT_DIR/syslog.log as a line of its
# own, and the output written to a stream to OUT_DIR/stream.log. Both sockets can be used by every user. Runs until it
# is killed.

import os
import pathlib
import socket
import sys
import threading

SYSLOG = "/dev/log"
STREAM = "/run/systemd/journal/stdout"

out_dir = pathlib.Path(sys.argv[1])
out_dir.mkdir(parents=True, exist_ok=True)
lock = threading.Lock()


def append(name, data):
    with lock, open(out_dir / name, "ab") as file:
        file.write(data)


def receive_syslog(sock):
    while True:
        data = sock.recv(65536)
        append("syslog.log", data.rstrip(b"\n") + b"\n")


def receive_stream(connection):
    with connection:
        while data := connection.recv(65536):
            append("stream.log", data)


def accept_streams(sock):
    while True:
        connection, _ = sock.accept()
        threading.Thread(target=receive_stream, args=(connection,), daemon=True).start()


for path in (SYSLOG, STREAM):
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(path).unlink(missing_ok=True)

syslog_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
syslog_socket.bind(SYSLOG)
stream_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
stream_socket.bind(STREAM)
stream_socket.listen()
for path in (SYSLOG, STREAM):
    os.chmod(path, 0o666)

threading.Thread(target=receive_syslog, args=(syslog_socket,), daemon=True).start()
accept_streams(stream_socket)
