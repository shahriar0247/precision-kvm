"""Linux evdev/uinput primitives shared by capture and injection.

No third-party dependencies: struct input_event is parsed directly and
grab/device ioctls go through fcntl.ioctl, mirroring how windows.py uses
raw ctypes. Only 64-bit and 32-bit little-endian Linux ABIs are handled;
the timeval field layout is chosen by struct size at import time.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re
import select
import struct
import subprocess
import xml.etree.ElementTree as ET

# --- evdev constants (linux/input-event-codes.h) ---
EV_SYN, EV_KEY, EV_REL, EV_ABS, EV_REP = 0, 1, 2, 3, 0x14
SYN_REPORT = 0
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0, 1, 6, 8
REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES = 11, 12
ABS_X, ABS_Y = 0, 1
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA = 0x110, 0x111, 0x112, 0x113, 0x114
BTN_MOUSE, BTN_TOUCH = 0x110, 0x14A
INPUT_PROP_POINTING_STICK = 5

_EVENT_FORMAT = "llHHi" if struct.calcsize("llHHi") == 24 else "iiHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FORMAT)


def _ioc(direction: int, family: int, code: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (family << 8) | code


_IOC_READ = 2
EVIOCGRAB = _ioc(1, 0x45, 0x90, 4)


def _eviocgbit(ev: int, length: int) -> int:
    return _ioc(_IOC_READ, 0x45, 0x20 + ev, length)


EVIOCGNAME = _ioc(_IOC_READ, 0x45, 0x06, 256)

# --- uinput constants (linux/uinput.h) ---
_UI = 0x55
UI_SET_EVBIT = _ioc(1, _UI, 100, 4)
UI_SET_KEYBIT = _ioc(1, _UI, 101, 4)
UI_SET_RELBIT = _ioc(1, _UI, 102, 4)
UI_SET_PROPBIT = _ioc(1, _UI, 110, 4)
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = _ioc(1, _UI, 3, 92)
BUS_USB = 0x03
UINPUT_NODE = "/dev/uinput"
INPUT_DIR = Path("/dev/input")


# evdev code -> (Windows VK, set-1 scan code, extended flag). Same triple
# WindowsCapture emits, so the wire payload is identical across platforms.
# Base AT keys keep the deliberate kernel identity: evdev code == set-1 scan.
LINUX_KEYS: dict[int, tuple[int, int, bool]] = {
    1: (0x1B, 0x01, False),   # KEY_ESC
    **{code: (0x30 + code - 2, code, False) for code in range(2, 12)},   # 1..0
    12: (0xBD, 0x0C, False),  # KEY_MINUS
    13: (0xBB, 0x0D, False),  # KEY_EQUAL
    14: (0x08, 0x0E, False),  # KEY_BACKSPACE
    15: (0x09, 0x0F, False),  # KEY_TAB
    **{c: (v, c, False) for c, v in zip(range(16, 26),  # KEY_Q..KEY_P
        (0x51, 0x57, 0x45, 0x52, 0x54, 0x59, 0x55, 0x49, 0x4F, 0x50))},
    26: (0xDB, 0x1A, False),  # KEY_LEFTBRACE
    27: (0xDD, 0x1B, False),  # KEY_RIGHTBRACE
    28: (0x0D, 0x1C, False),  # KEY_ENTER
    29: (0xA2, 0x1D, False),  # KEY_LEFTCTRL
    **{c: (v, c, False) for c, v in zip(range(30, 39),  # KEY_A..KEY_L
        (0x41, 0x53, 0x44, 0x46, 0x47, 0x48, 0x4A, 0x4B, 0x4C))},
    39: (0xBA, 0x27, False),  # KEY_SEMICOLON
    40: (0xDE, 0x28, False),  # KEY_APOSTROPHE
    41: (0xC0, 0x29, False),  # KEY_GRAVE
    42: (0xA0, 0x2A, False),  # KEY_LEFTSHIFT
    43: (0xDC, 0x2B, False),  # KEY_BACKSLASH
    **{c: (v, c, False) for c, v in zip(range(44, 51),  # KEY_Z..KEY_M
        (0x5A, 0x58, 0x43, 0x56, 0x42, 0x4E, 0x4D))},
    51: (0xBC, 0x33, False),  # KEY_COMMA
    52: (0xBE, 0x34, False),  # KEY_DOT
    53: (0xBF, 0x35, False),  # KEY_SLASH
    54: (0xA1, 0x36, False),  # KEY_RIGHTSHIFT
    55: (0x6A, 0x37, False),  # KEY_KPASTERISK
    56: (0xA4, 0x38, False),  # KEY_LEFTALT
    57: (0x20, 0x39, False),  # KEY_SPACE
    58: (0x14, 0x3A, False),  # KEY_CAPSLOCK
    **{c: (0x70 + c - 59, c, False) for c in range(59, 69)},   # KEY_F1..F10
    69: (0x90, 0x45, False),  # KEY_NUMLOCK
    70: (0x91, 0x46, False),  # KEY_SCROLLLOCK
    **{c: (0x67 + c - 71, c, False) for c in range(71, 74)},   # KEY_KP7..KP9
    74: (0x6D, 0x4A, False),  # KEY_KPMINUS
    **{c: (0x64 + c - 75, c, False) for c in range(75, 78)},   # KEY_KP4..KP6
    78: (0x6B, 0x4E, False),  # KEY_KPPLUS
    **{c: (0x61 + c - 79, c, False) for c in range(79, 82)},   # KEY_KP1..KP3
    82: (0x60, 0x52, False),  # KEY_KP0
    83: (0x6E, 0x53, False),  # KEY_KPDOT
    86: (0xE2, 0x56, False),  # KEY_102ND (ISO <>)
    87: (0x7A, 0x57, False),  # KEY_F11
    88: (0x7B, 0x58, False),  # KEY_F12
    96: (0x0D, 0x1C, True),   # KEY_KPENTER -> Return VK, extended scan
    97: (0xA3, 0x1D, True),   # KEY_RIGHTCTRL
    98: (0x6F, 0x35, True),   # KEY_KPSLASH
    99: (0x2C, 0x37, True),   # KEY_SYSRQ -> PrintScreen
    100: (0xA5, 0x38, True),  # KEY_RIGHTALT
    102: (0x24, 0x47, True),  # KEY_HOME
    103: (0x26, 0x48, True),  # KEY_UP
    104: (0x21, 0x49, True),  # KEY_PAGEUP
    105: (0x25, 0x4B, True),  # KEY_LEFT
    106: (0x27, 0x4D, True),  # KEY_RIGHT
    107: (0x23, 0x4F, True),  # KEY_END
    108: (0x28, 0x50, True),  # KEY_DOWN
    109: (0x22, 0x51, True),  # KEY_PAGEDOWN
    110: (0x2D, 0x52, True),  # KEY_INSERT
    111: (0x2E, 0x53, True),  # KEY_DELETE
    113: (0xAD, 0x20, True),  # KEY_MUTE
    114: (0xAE, 0x2E, True),  # KEY_VOLUMEDOWN
    115: (0xAF, 0x30, True),  # KEY_VOLUMEUP
    119: (0x13, 0x45, False), # KEY_PAUSE (e1 sequence; vk+scan is what apps see)
    125: (0x5B, 0x5B, True),  # KEY_LEFTMETA
    126: (0x5C, 0x5C, True),  # KEY_RIGHTMETA
    127: (0x5D, 0x5D, True),  # KEY_COMPOSE -> Menu
    140: (0xB7, 0x21, True),  # KEY_CALC
    142: (0x5F, 0x5F, True),  # KEY_SLEEP
    155: (0xB4, 0x6C, True),  # KEY_MAIL
    163: (0xB0, 0x19, True),  # KEY_NEXTSONG
    164: (0xB3, 0x22, True),  # KEY_PLAYPAUSE
    165: (0xB1, 0x10, True),  # KEY_PREVIOUSSONG
    166: (0xB2, 0x24, True),  # KEY_STOPCD
    172: (0xAC, 0x32, True),  # KEY_HOMEPAGE
    **{c: (0x7C + c - 183, 0, False) for c in range(183, 195)},  # KEY_F13..F24
}

# Reverse views for injection. The full triple is the preferred identifier:
# NumLock and Pause share (0x45, False) and are told apart only by VK.
TRIPLE_TO_EVDEV: dict[tuple[int, int, bool], int] = {
    (vk, scan, extended): code
    for code, (vk, scan, extended) in LINUX_KEYS.items() if scan
}
SCAN_TO_EVDEV: dict[tuple[int, bool], int] = {
    (scan, extended): code
    for code, (_vk, scan, extended) in LINUX_KEYS.items() if scan
}
VK_TO_EVDEV: dict[int, int] = {}
for _code, (_vk, _scan, _extended) in LINUX_KEYS.items():
    VK_TO_EVDEV.setdefault(_vk, _code)


def evdev_key_id(code: int) -> tuple[int, int, bool] | None:
    """(vk, scan, extended) for a captured evdev key code."""
    return LINUX_KEYS.get(code)


def evdev_for_key(vk: int, scan: int, extended: bool) -> int | None:
    """Evdev code to inject for a wire (vk, scan, extended) key event."""
    code = TRIPLE_TO_EVDEV.get((vk, scan, extended))
    if code is not None:
        return code
    return SCAN_TO_EVDEV.get((scan, extended), VK_TO_EVDEV.get(vk))


class EvdevDevice:
    """Read (and optionally grab) one /dev/input/event* node."""

    def __init__(self, path: Path):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.name = ""
        self.ev_types: set[int] = set()
        self.keys: set[int] = set()
        self.rels: set[int] = set()
        self.abss: set[int] = set()
        self.grabbed = False
        self._probe()

    def _bits(self, ev: int, count: int) -> set[int]:
        buf = bytearray((count + 7) // 8)
        try:
            fcntl.ioctl(self.fd, _eviocgbit(ev, len(buf)), buf, True)
        except OSError:
            return set()
        return {bit for bit in range(count) if buf[bit // 8] & (1 << (bit % 8))}

    def _probe(self):
        buf = bytearray(256)
        try:
            fcntl.ioctl(self.fd, EVIOCGNAME, buf, True)
            self.name = bytes(buf).split(b"\0", 1)[0].decode("utf-8", "replace")
        except OSError:
            self.name = self.path.name
        self.ev_types = self._bits(0, 32)
        if EV_KEY in self.ev_types:
            self.keys = self._bits(EV_KEY, 768)
        if EV_REL in self.ev_types:
            self.rels = self._bits(EV_REL, 16)
        if EV_ABS in self.ev_types:
            self.abss = self._bits(EV_ABS, 64)

    @property
    def is_keyboard(self) -> bool:
        return EV_KEY in self.ev_types and any(
            code in self.keys for code in (*range(30, 45), 28, 57))

    @property
    def is_pointer(self) -> bool:
        if {REL_X, REL_Y} <= self.rels:
            return True
        # Touchpads and absolute pointer devices.
        return {ABS_X, ABS_Y} <= self.abss and (
            BTN_TOUCH in self.keys or BTN_LEFT in self.keys)

    def grab(self):
        if not self.grabbed:
            fcntl.ioctl(self.fd, EVIOCGRAB, 1)
            self.grabbed = True

    def ungrab(self):
        if self.grabbed:
            try:
                fcntl.ioctl(self.fd, EVIOCGRAB, 0)
            except OSError:
                pass
            self.grabbed = False

    def events(self) -> list[tuple[int, int, int]]:
        """All queued (type, code, value) events; empty on EAGAIN."""
        out = []
        while True:
            try:
                chunk = os.read(self.fd, _EVENT_SIZE * 64)
            except BlockingIOError:
                break
            if not chunk:
                raise OSError(f"{self.path} closed")
            for off in range(0, len(chunk) - _EVENT_SIZE + 1, _EVENT_SIZE):
                _sec, _usec, ev_type, code, value = struct.unpack_from(
                    _EVENT_FORMAT, chunk, off)
                out.append((ev_type, code, value))
            if len(chunk) < _EVENT_SIZE * 64:
                break
        return out

    def close(self):
        self.ungrab()
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def input_devices() -> list[EvdevDevice]:
    """Openable input nodes, excluding our own virtual devices."""
    devices = []
    for path in sorted(INPUT_DIR.glob("event*")):
        try:
            device = EvdevDevice(path)
        except OSError:
            continue
        if device.name.startswith("PrecisionKVM"):
            device.close()
            continue
        devices.append(device)
    return devices


class UInputDevice:
    """A virtual keyboard+pointer created through /dev/uinput."""

    def __init__(self, name: str, keys: set[int], rels: set[int],
                 pointing_stick: bool = False):
        self.fd = os.open(UINPUT_NODE, os.O_WRONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            if rels:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REP)
            for code in keys:
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
            for code in rels:
                fcntl.ioctl(self.fd, UI_SET_RELBIT, code)
            if pointing_stick:
                # Trackpoints skip libinput's pointer-acceleration profile on
                # most stacks, keeping injected deltas close to raw counts.
                fcntl.ioctl(self.fd, UI_SET_PROPBIT, INPUT_PROP_POINTING_STICK)
            setup = struct.pack("80sHHHHI", name.encode()[:80],
                                BUS_USB, 0x045A, 0x0001, 1, 0)
            fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except Exception:
            os.close(self.fd)
            self.fd = -1
            raise

    def emit(self, ev_type: int, code: int, value: int):
        if self.fd >= 0:
            os.write(self.fd, struct.pack(_EVENT_FORMAT, 0, 0, ev_type, code, value))

    def key(self, code: int, down: bool):
        self.emit(EV_KEY, code, 1 if down else 0)

    def rel(self, code: int, value: int):
        if value:
            self.emit(EV_REL, code, int(value))

    def syn(self):
        self.emit(EV_SYN, SYN_REPORT, 0)

    def close(self):
        if self.fd >= 0:
            try:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = -1


def _monitors_xml(path: Path) -> list[tuple[int, int, int, int]]:
    """GNOME/Mutter logicalmonitor records; positions are already logical."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return []
    rects = []
    for monitor in root.iter("logicalmonitor"):
        try:
            x = int(monitor.findtext("x", "0"))
            y = int(monitor.findtext("y", "0"))
            scale = float(monitor.findtext("scale", "1"))
            mode = monitor.find("./monitor/mode")
            if mode is None:
                continue
            width = int(mode.findtext("width", "0"))
            height = int(mode.findtext("height", "0"))
            if width > 0 and height > 0 and scale > 0:
                rects.append((x, y, round(width / scale), round(height / scale)))
        except (TypeError, ValueError):
            continue
    return rects


def _xrandr_monitors() -> list[tuple[int, int, int, int]]:
    try:
        output = subprocess.run(["xrandr", "--listmonitors"], capture_output=True,
                                text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rects = []
    for match in re.finditer(r"(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)", output):
        width, height, x, y = (int(match.group(i)) for i in (1, 2, 3, 4))
        rects.append((x, y, width, height))
    return rects


def linux_monitors() -> list[tuple[int, int, int, int]]:
    """Display rectangles in compositor coordinates, best source first.

    PRECISION_KVM_MONITORS="x,y,w,h;x,y,w,h" overrides every source —
    useful when the compositor exposes no usable geometry query.
    """
    override = os.environ.get("PRECISION_KVM_MONITORS", "")
    rects: list[tuple[int, int, int, int]] = []
    for chunk in override.split(";"):
        fields = chunk.split(",")
        if len(fields) == 4:
            try:
                x, y, w, h = (int(f) for f in fields)
            except ValueError:
                continue
            rects.append((x, y, w, h))
    if rects:
        return rects
    rects = _monitors_xml(Path.home() / ".config/monitors.xml")
    return rects or _xrandr_monitors() or [(0, 0, 1920, 1080)]


def selector(fds: list[int]) -> select.poll:
    poller = select.poll()
    for fd in fds:
        poller.register(fd, select.POLLIN)
    return poller
