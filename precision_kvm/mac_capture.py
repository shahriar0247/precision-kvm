"""macOS host capture — lets the Mac act as a KVM host.

A CGEventTap watches global input. While the cursor is inside the local
display union, events pass through untouched. Once the cursor reaches a
peer-facing edge the tap swallows events, forwards them to the peer, and
pins the logical cursor at the boundary. A 'return' control event from the
peer warps the cursor back inside the local desktop.

Key events are emitted as Windows vk codes so the peer's Windows injector
replays them unchanged.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable

from . import geometry
from .keymap import SCAN_TO_MAC, VK_TO_MAC

IS_MAC = sys.platform == "darwin"

if IS_MAC:
    import Quartz

# Apple virtual key code -> Windows VK (reverse of keymap.VK_TO_MAC),
# plus the alphabetic/digit rows that only exist in the scan-code table.
MAC_TO_VK = {mac: vk for vk, mac in VK_TO_MAC.items()}
for vk, scan in ((65 + i, s) for i, s in enumerate(range(0x1E, 0x1E + 26))):
    MAC_TO_VK.setdefault(SCAN_TO_MAC.get(scan, -1), vk)
SCAN_ROWS = [(0x30 + i, s) for i, s in enumerate((0x0B, 0x02, 0x03, 0x04, 0x05,
                                                0x06, 0x07, 0x08, 0x09, 0x0A))]
for vk, scan in SCAN_ROWS:
    MAC_TO_VK.setdefault(SCAN_TO_MAC.get(scan, -1), vk)
MAC_TO_VK.pop(-1, None)

# Modifier flag transitions arrive as flagsChanged; map each modifier keycode.
if IS_MAC:
    _EVT = {
        Quartz.kCGEventLeftMouseDown: ("button", "left", True),
        Quartz.kCGEventLeftMouseUp: ("button", "left", False),
        Quartz.kCGEventRightMouseDown: ("button", "right", True),
        Quartz.kCGEventRightMouseUp: ("button", "right", False),
        Quartz.kCGEventOtherMouseDown: ("button", "middle", True),
        Quartz.kCGEventOtherMouseUp: ("button", "middle", False),
    }
    _MOVE_EVENTS = frozenset((
        Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDragged,
        Quartz.kCGEventRightMouseDragged, Quartz.kCGEventOtherMouseDragged))


def _display_rects() -> list:
    """Local display rectangles in macOS global coordinates (top-left origin)."""
    if not IS_MAC:
        return []
    max_displays = 16
    err, ids, count = Quartz.CGGetActiveDisplayList(max_displays, None, None)
    if err or not count:
        return []
    rects = []
    for did in ids[:count]:
        b = Quartz.CGDisplayBounds(did)
        rects.append((int(b.origin.x), int(b.origin.y),
                      int(b.size.width), int(b.size.height)))
    # Normalise so the primary display (menubar screen) sits at origin, matching
    # the Windows virtual-desktop convention the protocol assumes.
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    return [(r[0] - x0, r[1] - y0, r[2], r[3]) for r in rects]


class MacCapture:
    """Host-side input capture on macOS. Same surface as WindowsCapture."""

    def __init__(self, on_event: Callable[[dict], bool],
                 side: str = "left", on_emergency: Callable[[str], None] | None = None,
                 peer_align: float = 0.5, peer_span: float = 0.6,
                 peers: dict | None = None, lock_sync: bool = True,
                 **_ignored):
        self.on_event = on_event
        self.on_emergency = on_emergency or (lambda reason: None)
        self.side = side
        self.peer_align = float(peer_align)
        self.peer_span = float(peer_span)
        self.peers = peers or {}
        self.lock_sync = bool(lock_sync)
        self.active = False
        self.peer_connected = False
        self.locked = False
        self.error = ""
        self._stopping = threading.Event()
        self._thread = None
        self._runloop = None
        self._tap = None
        self._last_pos = (0.0, 0.0)
        self._esc_since = 0.0
        self._peer_rect = None
        self._entry = (0.0, 0.0)
        self._mons = []

    # ----- lifecycle ---------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="kvm-mac-capture",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stopping.set()
        self.release()
        if self._runloop is not None:
            Quartz.CFRunLoopStop(self._runloop)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(4)

    def release(self):
        """Drop remote control; the next event resumes local delivery."""
        self.active = False

    def return_local(self, normalized_y=None, normalized_x=None):
        mons = self._mons or _display_rects()
        peer = self._peer_rect or self._peer_map(mons).get("peer", (0, 0, 0, 0))
        x, y = geometry.return_point(
            mons, peer, normalized_x if normalized_x is not None else 0.5,
            normalized_y if normalized_y is not None else 0.5)
        self.active = False
        self._last_pos = (x, y)
        Quartz.CGWarpMouseCursorPosition((x, y))
        Quartz.CGAssociateMouseAndMouseCursorPosition(True)

    # ----- peer geometry ------------------------------------------------------

    def _peer_map(self, mons):
        if self.peers:
            return {name: geometry.snap_side(tuple(rect), mons)
                    for name, rect in self.peers.items()}
        return {"peer": geometry.default_peer(mons, self.side,
                                              self.peer_align, self.peer_span)}

    # ----- event tap ----------------------------------------------------------

    def _run(self):
        self._mons = _display_rects() or [(0, 0, 1440, 900)]
        mask = 0
        for code in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown,
                     Quartz.kCGEventLeftMouseUp, Quartz.kCGEventRightMouseDown,
                     Quartz.kCGEventRightMouseUp, Quartz.kCGEventOtherMouseDown,
                     Quartz.kCGEventOtherMouseUp, Quartz.kCGEventLeftMouseDragged,
                     Quartz.kCGEventRightMouseDragged, Quartz.kCGEventOtherMouseDragged,
                     Quartz.kCGEventScrollWheel, Quartz.kCGEventKeyDown,
                     Quartz.kCGEventKeyUp, Quartz.kCGEventFlagsChanged):
            mask |= Quartz.CGEventMaskBit(code)
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGHIDEventTap, Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault, mask, self._callback, None)
        if self._tap is None:
            self.error = "CGEventTapCreate failed — grant Accessibility access"
            return
        source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        loop = Quartz.CFRunLoopGetCurrent()
        self._runloop = loop
        Quartz.CFRunLoopAddSource(loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)
        locker = threading.Thread(target=self._lock_watch, daemon=True,
                                  name="kvm-mac-lockwatch")
        locker.start()
        try:
            Quartz.CFRunLoopRun()
        finally:
            Quartz.CGEventTapEnable(self._tap, False)
            Quartz.CFRunLoopRemoveSource(loop, source, Quartz.kCFRunLoopCommonModes)
            Quartz.CFMachPortInvalidate(self._tap)
            self._runloop = None

    def _lock_watch(self):
        if not self.lock_sync:
            return
        was = False
        while not self._stopping.wait(0.5):
            try:
                info = Quartz.CGSessionCopyCurrentDictionary()
                locked = bool(info and info.get("CGSSessionScreenIsLocked"))
            except Exception:
                locked = False
            if locked and not was:
                self.locked = True
                self.release()
                self.on_event({"type": "lock"})
            elif not locked:
                self.locked = False
            was = locked

    def _callback(self, _proxy, kind, event, _refcon):
        if kind == Quartz.kCGEventTapDisabledByTimeout:
            Quartz.CGEventTapEnable(self._tap, True)
            return event
        if self.locked:
            return event  # lock screen: never share input
        if kind in _MOVE_EVENTS:
            return self._mouse(event)
        if kind in _EVT:
            what, button, down = _EVT[kind]
            if not self.active:
                return event
            self.on_event({"type": what, "button": button, "down": down})
            return None
        if kind == Quartz.kCGEventScrollWheel:
            if not self.active:
                return event
            dy = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGScrollWheelEventDeltaAxis1)
            dx = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGScrollWheelEventDeltaAxis2)
            self.on_event({"type": "scroll", "dx": int(dx) * 120,
                           "dy": int(dy) * 120})
            return None
        if kind in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp,
                    Quartz.kCGEventFlagsChanged):
            return self._key(kind, event)
        return event

    def _mouse(self, event):
        loc = Quartz.CGEventGetLocation(event)
        x, y = float(loc.x), float(loc.y)
        mons = self._mons
        if not self.active:
            hit = geometry.hit(mons, self._peer_map(mons), round(x), round(y))
            if hit is None or not self.peer_connected:
                self._last_pos = (x, y)
                return event
            name, side, nx, ny = hit
            self._peer_rect = self._peer_map(mons).get(name)
            self._entry = (nx, ny)
            self._last_pos = (x, y)
            self.active = True
            if self.on_event({"type": "activate", "normalized_x": nx,
                              "normalized_y": ny, "side": side}) is False:
                self.release()
                return event
            return None  # swallow the crossing move
        # Active: the event never reaches macOS; forward the delta.
        dx, dy = x - self._last_pos[0], y - self._last_pos[1]
        self._last_pos = (x, y)
        if dx or dy:
            self.on_event({"type": "move", "dx": dx, "dy": dy})
        return None

    def _key(self, kind, event):
        code = int(Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGKeyboardEventKeycode))
        flags = Quartz.CGEventGetFlags(event)
        cmd = bool(flags & Quartz.kCGEventFlagMaskCommand)
        ctrl = bool(flags & Quartz.kCGEventFlagMaskControl)
        if kind == Quartz.kCGEventFlagsChanged:
            # Flags events carry no up/down; the flag state decides it.
            mask_map = ((55, Quartz.kCGEventFlagMaskCommand),
                        (54, Quartz.kCGEventFlagMaskCommand),
                        (56, Quartz.kCGEventFlagMaskShift),
                        (60, Quartz.kCGEventFlagMaskShift),
                        (58, Quartz.kCGEventFlagMaskAlternate),
                        (61, Quartz.kCGEventFlagMaskAlternate),
                        (59, Quartz.kCGEventFlagMaskControl),
                        (62, Quartz.kCGEventFlagMaskControl))
            mask = next((m for c, m in mask_map if c == code), None)
            down = bool(flags & mask) if mask else False
        else:
            down = kind == Quartz.kCGEventKeyDown
        # Emergency: hold Esc to drop the session.
        if code == 53:
            if down and not self._esc_since:
                self._esc_since = time.monotonic()
            elif not down:
                self._esc_since = 0.0
            if self._esc_since and time.monotonic() - self._esc_since >= 1.5:
                self._esc_since = 0.0
                self.release()
                self.on_emergency("Escape held")
                return event
        if not self.active:
            # Ctrl+Alt+F12 eject mirrors the Windows path.
            if code == 111 and down and cmd and ctrl:
                self.release()
                self.on_emergency("Ctrl+Alt+F12")
                return event
            return event
        vk = MAC_TO_VK.get(code)
        if vk is None:
            return None  # swallow anyway while remote is active
        self.on_event({"type": "key", "vk": vk, "scan": 0,
                       "flags": 0, "down": down})
        return None
