"""Windows event injector — lets a Windows machine act as a KVM client.

Mirrors MacInjector's protocol surface (handle/release_all/close) using
SendInput-compatible user32 calls. Key events arrive as raw vk/scan values
produced by WindowsCapture, so replay is a direct pass-through.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import sys
from typing import Any, Callable

from . import geometry

user: Any = ctypes.windll.user32 if sys.platform == "win32" else None
kernel: Any = ctypes.windll.kernel32 if sys.platform == "win32" else None

if user is not None:
    user.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte,
                                 wintypes.DWORD, ctypes.c_size_t]
    user.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
    user.LockWorkStation.argtypes = []
    user.LockWorkStation.restype = wintypes.BOOL

KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001

# mouse_event flags, indexed by button number for the first three.
MOUSE_DOWN = (0x0002, 0x0008, 0x0020, 0x0080, 0x0080)   # L, R, M, X, X
MOUSE_UP = (0x0004, 0x0010, 0x0040, 0x0100, 0x0100)
XBUTTON_FOR = {3: 0x0001, 4: 0x0002}
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_MOVE = 0x0001


class WindowsInjector:
    """Receives input events on Windows and posts them as local input."""

    def __init__(self, on_return: Callable[[dict], None] | None = None,
                 on_error: Callable[[Exception], None] | None = None):
        self.on_return = on_return or (lambda event: None)
        self.on_error = on_error or (lambda exc: None)
        self.active = False
        self.side = "left"
        self.gain = 1.0
        self.scroll_gain = 1.0
        self.scroll_gain_x = self.scroll_gain_y = 1.0
        self.scroll_invert_x = self.scroll_invert_y = False
        self.scroll_mode = "pixel"
        self.buttons: set[int] = set()
        self.held_vks: set[int] = set()
        self.x = self.y = 0.0
        self.left = self.top = self.width = self.height = 0
        self.refresh_bounds()

    # ----- protocol ---------------------------------------------------------

    def refresh_bounds(self):
        mons = geometry.monitors() or [(0, 0, 1920, 1080)]
        self.left, self.top, self.width, self.height = geometry.union_bounds(mons)

    def handle(self, event: dict):
        kind = event.get("type")
        try:
            if kind == "activate":
                self._activate(event)
            elif kind in ("deactivate", "release"):
                self.release_all()
            elif kind == "lock":
                self.release_all()
                if user is not None:
                    user.LockWorkStation()
            elif not self.active:
                return
            elif kind == "move":
                self._move(event)
            elif kind == "button":
                self._button(event)
            elif kind == "scroll":
                self._scroll(event)
            elif kind == "key":
                self._key(event)
        except Exception as exc:  # malformed network input must not kill the loop
            self.on_error(exc)

    def _activate(self, event):
        self.release_all()
        self.refresh_bounds()
        side = event.get("side", self.side)
        if side not in ("left", "right", "top", "bottom"):
            raise ValueError("Invalid monitor side")
        self.side = side
        self.gain = float(event.get("gain", 1.0))
        self.scroll_gain = float(event.get("scroll_gain", 1.0))
        if not all(math.isfinite(v) and 0.01 <= v <= 20
                   for v in (self.gain, self.scroll_gain)):
            raise ValueError("Input gains must be finite and between 0.01 and 20")
        self.scroll_gain_x = float(event.get("scroll_gain_x", self.scroll_gain))
        self.scroll_gain_y = float(event.get("scroll_gain_y", self.scroll_gain))
        if not all(math.isfinite(v) and 0.01 <= v <= 20
                   for v in (self.scroll_gain_x, self.scroll_gain_y)):
            raise ValueError("Scroll gains must be finite and between 0.01 and 20")
        self.scroll_invert_x = bool(event.get("scroll_invert_x", False))
        self.scroll_invert_y = bool(event.get("scroll_invert_y", False))
        self.scroll_mode = event.get("scroll_mode", "pixel")
        if self.scroll_mode not in ("pixel", "line"):
            raise ValueError("Scroll mode must be pixel or line")
        axis = "normalized_x" if side in ("top", "bottom") else "normalized_y"
        fraction = max(0.0, min(1.0, float(event.get(axis, 0.5))))
        self.x = self.left + (self.width - 2 if side == "left" else 1)
        self.y = self.top + fraction * (self.height - 1)
        if side in ("top", "bottom"):
            self.x = self.left + fraction * (self.width - 1)
            self.y = self.top + (self.height - 2 if side == "top" else 1)
        self.active = True
        user.SetCursorPos(round(self.x), round(self.y))

    def _move(self, event):
        dx = float(event.get("dx", 0)) * self.gain
        dy = float(event.get("dy", 0)) * self.gain
        if not math.isfinite(dx) or not math.isfinite(dy):
            raise ValueError("Non-finite mouse movement")
        nx, ny = self.x + dx, self.y + dy
        crossing = ((self.side == "left" and nx >= self.left + self.width - 1 and dx > 0)
                    or (self.side == "right" and nx <= self.left and dx < 0)
                    or (self.side == "top" and ny >= self.top + self.height - 1 and dy > 0)
                    or (self.side == "bottom" and ny <= self.top and dy < 0))
        if crossing and not self.buttons:
            fraction = ((self.y - self.top) / max(1, self.height - 1)
                        if self.side in ("left", "right")
                        else (self.x - self.left) / max(1, self.width - 1))
            self.release_all()
            message = {"type": "return", "normalized_y": max(0.0, min(1.0, fraction))}
            if self.side in ("top", "bottom"):
                message["normalized_x"] = message["normalized_y"]
            self.on_return(message)
            return
        self.x = max(self.left, min(self.left + self.width - 1, nx))
        self.y = max(self.top, min(self.top + self.height - 1, ny))
        user.SetCursorPos(round(self.x), round(self.y))

    def _button(self, event):
        button = event.get("button")
        button = {"left": 0, "right": 1, "middle": 2, "x1": 3, "x2": 4}.get(button, button)
        button = int(button)
        if not 0 <= button <= 4:
            return
        down = bool(event.get("down"))
        if down:
            self.buttons.add(button)
        else:
            self.buttons.discard(button)
        data = XBUTTON_FOR.get(button, 0) if button >= 3 else 0
        user.mouse_event(MOUSE_DOWN[button] if down else MOUSE_UP[button],
                         0, 0, data, 0)

    def _scroll(self, event):
        dx = float(event.get("dx", 0))
        dy = float(event.get("dy", 0))
        if not math.isfinite(dx) or not math.isfinite(dy):
            raise ValueError("Non-finite scroll movement")
        factor = 1.0 if self.scroll_mode == "pixel" else 0.2
        dy = dy * self.scroll_gain_y * factor * (-1 if self.scroll_invert_y else 1)
        dx = dx * self.scroll_gain_x * factor * (-1 if self.scroll_invert_x else 1)
        if dy:
            user.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, int(dy), 0)
        if dx:
            user.mouse_event(MOUSEEVENTF_HWHEEL, 0, 0, int(dx), 0)

    def _key(self, event):
        vk = int(event.get("vk", 0)) & 0xFF
        scan = int(event.get("scan", 0)) & 0xFF
        down = bool(event.get("down"))
        flags = int(event.get("flags", 0))
        extended = KEYEVENTF_EXTENDEDKEY if flags & 0x01 else 0
        if down:
            self.held_vks.add(vk)
            user.keybd_event(vk, scan, extended, 0)
        else:
            self.held_vks.discard(vk)
            user.keybd_event(vk, scan, extended | KEYEVENTF_KEYUP, 0)

    def release_all(self):
        self.active = False
        for button in tuple(self.buttons):
            data = XBUTTON_FOR.get(button, 0) if button >= 3 else 0
            user.mouse_event(MOUSE_UP[button], 0, 0, data, 0)
        self.buttons.clear()
        for vk in tuple(self.held_vks):
            user.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        self.held_vks.clear()

    def close(self):
        self.release_all()
