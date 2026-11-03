"""Minimal Wayland client: adds a toplevel to an xdg_session_v1, then sends one request on its toplevel session.

Usage: rename_client.py rename|destroy|rename-check

Speaks the wire protocol directly. Prints each step and how the connection ended. rename-check also checks the
semantics: after renaming the first toplevel from toplevel-a to toplevel-b, a second toplevel can be added as
toplevel-a, and renaming the first one back to toplevel-a raises name_in_use.
"""
import os
import socket
import struct
import sys

action = sys.argv[1]
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(os.path.join(os.environ["XDG_RUNTIME_DIR"], os.environ["WAYLAND_DISPLAY"]))


def wl_string(s):
    if s is None:
        return struct.pack("<I", 0)
    b = s.encode() + b"\0"
    return struct.pack("<I", len(b)) + b + b"\0" * ((4 - len(b) % 4) % 4)


def send(obj, opcode, payload=b""):
    size = 8 + len(payload)
    sock.sendall(struct.pack("<II", obj, (size << 16) | opcode) + payload)


def u32(*vals):
    return b"".join(struct.pack("<I", v) for v in vals)


buf = b""


def read_events(until_callback=None, timeout=3.0):
    """Read events; return the globals seen, stop at wl_callback.done of until_callback."""
    global buf
    sock.settimeout(timeout)
    globals_ = {}
    while True:
        while len(buf) >= 8:
            obj, word = struct.unpack_from("<II", buf)
            size, opcode = word >> 16, word & 0xFFFF
            if len(buf) < size:
                break
            body = buf[8:size]
            buf = buf[size:]
            if obj == 2 and opcode == 0:  # wl_registry.global(name, interface, version)
                name, ln = struct.unpack_from("<II", body)
                iface = body[8:8 + ln - 1].decode()
                version = struct.unpack_from("<I", body, 8 + ln + ((4 - ln % 4) % 4))[0]
                globals_[iface] = (name, version)
            elif obj == 1 and opcode == 0:  # wl_display.error(object, code, message)
                eobj, code, ln = struct.unpack_from("<III", body)
                print("protocol error on object %d, code %d: %s" % (eobj, code, body[12:12 + ln - 1].decode()))
            elif until_callback and obj == until_callback and opcode == 0:
                return globals_
        try:
            data = sock.recv(65536)
        except socket.timeout:
            return globals_
        if not data:
            print("connection closed by the compositor")
            return None
        buf += data


send(1, 1, u32(2))           # wl_display.get_registry(new_id 2)
send(1, 0, u32(3))           # wl_display.sync(new_id 3)
g = read_events(until_callback=3)
for iface in ("wl_compositor", "xdg_wm_base", "xdg_session_manager_v1"):
    print("%s: %s" % (iface, g.get(iface)))


def bind(iface, version, new_id):
    name = g[iface][0]
    send(2, 0, u32(name) + wl_string(iface) + u32(version, new_id))


bind("wl_compositor", 4, 4)
bind("xdg_wm_base", 1, 5)
bind("xdg_session_manager_v1", 1, 6)
send(4, 0, u32(7))                    # wl_compositor.create_surface(new_id 7)
send(5, 2, u32(8, 7))                 # xdg_wm_base.get_xdg_surface(new_id 8, surface 7)
send(8, 1, u32(9))                    # xdg_surface.get_toplevel(new_id 9)
send(6, 1, u32(10, 1) + wl_string(None))   # xdg_session_manager_v1.get_session(new_id 10, launch, null)
send(10, 2, u32(11, 9) + wl_string("toplevel-a"))  # xdg_session_v1.add_toplevel(new_id 11, toplevel 9, name)
send(1, 0, u32(12))
r = read_events(until_callback=12)
print("after add_toplevel: %s" % ("connected" if r is not None else "disconnected"))
if action in ("rename", "rename-check"):
    send(11, 1, wl_string("toplevel-b"))  # xdg_toplevel_session_v1.rename(name)
else:
    send(11, 0)                           # xdg_toplevel_session_v1.destroy()
send(1, 0, u32(13))
r = read_events(until_callback=13)
print("after %s: %s" % (action, "connected" if r is not None else "disconnected"))
if action == "rename-check" and r is not None:
    send(4, 0, u32(14))                   # wl_compositor.create_surface(new_id 14), new ids being consecutive
    send(5, 2, u32(15, 14))               # xdg_wm_base.get_xdg_surface(new_id 15, surface 14)
    send(15, 1, u32(16))                  # xdg_surface.get_toplevel(new_id 16)
    send(10, 2, u32(17, 16) + wl_string("toplevel-a"))  # add_toplevel under the name the rename freed
    send(1, 0, u32(18))
    r = read_events(until_callback=18)
    print("second toplevel added as toplevel-a: %s" % ("connected" if r is not None else "disconnected"))
    if r is not None:
        send(11, 1, wl_string("toplevel-a"))  # rename the first one to the name now in use
        send(1, 0, u32(19))
        r = read_events(until_callback=19)
        print("first renamed back to toplevel-a: %s" % ("connected" if r is not None else "disconnected"))
