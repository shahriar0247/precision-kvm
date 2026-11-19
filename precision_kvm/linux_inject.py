"""Linux input injection through /dev/uinput. No global state on import.

Mirrors MacInjector's handle() contract: activate parses the host config
snapshot; move/button/scroll/key apply while active; crossing the return
edge calls on_return. The virtual cursor position is tracked locally —
Wayland gives injectors only deltas, so entry is approximated by slamming
the pointer onto the adjoining edge before accumulating deltas.
"""
from __future__ import annotations

import math
import subprocess
import sys
import threading
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .scroll import SmoothScrollEngine

from .geometry import primary
from .linux_input import (BTN_EXTRA, BTN_LEFT, BTN_MIDDLE, BTN_RIGHT, BTN_SIDE,
                          LINUX_KEYS, REL_HWHEEL, REL_HWHEEL_HI_RES, REL_WHEEL,
                          REL_WHEEL_HI_RES, REL_X, REL_Y, UInputDevice,
                          evdev_for_key, linux_monitors)

_BUTTON_CODES = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE,
                 "x1": BTN_SIDE, "x2": BTN_EXTRA,
                 0: BTN_LEFT, 1: BTN_RIGHT, 2: BTN_MIDDLE,
                 3: BTN_SIDE, 4: BTN_EXTRA}
_INJECT_KEYS = set(LINUX_KEYS) | set(_BUTTON_CODES.values())
_INJECT_RELS = {REL_X, REL_Y, REL_WHEEL, REL_HWHEEL,
                REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES}


class LinuxInjector:
    """One uinput virtual device; explicit release on every disconnect."""

    def __init__(self, on_return: Callable[[dict], None] | None = None,
                 side: str = "left", device=None):
        if device is None:
            if sys.platform != "linux":
                raise RuntimeError("LinuxInjector requires Linux")
            try:
                device = UInputDevice("PrecisionKVM Virtual Input",
                                      _INJECT_KEYS, _INJECT_RELS,
                                      pointing_stick=True)
            except OSError as exc:
                raise RuntimeError(
                    "Cannot create a virtual input device — grant access to "
                    "/dev/uinput (see tools/install_linux_input.sh)") from exc
        self.device = device
        self.on_return = on_return or (lambda event: None)
        if side not in ("left", "right", "top", "bottom"):
            raise ValueError("Invalid monitor side")
        self.side = side
        self.active = False
        self.gain = 1.0
        self.scroll_gain = 1.0
        self.scroll_gain_x = self.scroll_gain_y = 1.0
        self.scroll_invert_x = self.scroll_invert_y = False
        self.scroll_mode = "pixel"
        self.smooth_scroll = False
        self.smooth_scroll_ms = 60.0
        self.smooth_scroll_hz = 120
        self._scroll_lock = threading.RLock()
        self._scroll_flags = 0
        self._smooth: SmoothScrollEngine | None = None
        self.keys: set[int] = set()
        self.buttons: set[int] = set()
        self.scroll_x = self.scroll_y = 0.0
        self.refresh_bounds()

    def refresh_bounds(self):
        mons = linux_monitors()
        left, top, width, height = primary(mons)
        self.left, self.top = float(left), float(top)
        self.width, self.height = float(width), float(height)
        self.x, self.y = self.left + self.width / 2, self.top + self.height / 2

    def _slam(self):
        """Park the real cursor on the entry edge; deltas then track truth."""
        for _ in range(4):
            if self.side == "left":
                self.device.rel(REL_X, 100000)
            elif self.side == "right":
                self.device.rel(REL_X, -100000)
            elif self.side == "top":
                self.device.rel(REL_Y, 100000)
            else:
                self.device.rel(REL_Y, -100000)
        self.device.syn()

    def handle(self, event: dict):
        kind = event.get("type")
        if kind == "activate":
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
            self.smooth_scroll = bool(event.get("smooth_scroll", False))
            self.smooth_scroll_ms = float(event.get("smooth_scroll_ms", 60))
            self.smooth_scroll_hz = int(event.get("smooth_scroll_hz", 120))
            if (not math.isfinite(self.smooth_scroll_ms)
                    or not 10 <= self.smooth_scroll_ms <= 150
                    or not 30 <= self.smooth_scroll_hz <= 240):
                raise ValueError("Invalid smooth scroll timing")
            if self._smooth:
                self._smooth.stop()
                self._smooth = None
            axis = "normalized_x" if self.side in ("top", "bottom") else "normalized_y"
            fraction = max(0.0, min(1.0, float(event.get(axis, event.get("normalized_y", 0.5)))))
            self.x = self.left + (self.width - 2 if self.side == "left" else 1)
            self.y = self.top + fraction * (self.height - 1)
            if self.side in ("top", "bottom"):
                self.x = self.left + fraction * (self.width - 1)
                self.y = self.top + (self.height - 2 if self.side == "top" else 1)
            self.active = True
            self._slam()
            return
        if kind in ("deactivate", "release"):
            self.release_all()
            return
        if kind == "lock":
            self.release_all()
            self._gesture("lock_screen")
            return
        if not self.active:
            return
        if kind == "move":
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
            if dx:
                self.device.rel(REL_X, round(dx))
            if dy:
                self.device.rel(REL_Y, round(dy))
            self.device.syn()
        elif kind == "key":
            self._key(event)
        elif kind == "gesture":
            self._gesture(event.get("action"))
        elif kind == "button":
            code = _BUTTON_CODES.get(event["button"])
            if code is None:
                return
            down = bool(event["down"])
            self.buttons.add(code) if down else self.buttons.discard(code)
            self.device.key(code, down)
            self.device.syn()
        elif kind == "scroll":
            dx, dy = float(event.get("dx", 0)), float(event.get("dy", 0))
            if not math.isfinite(dx) or not math.isfinite(dy):
                raise ValueError("Non-finite scroll movement")
            dx = dx * self.scroll_gain_x * (-1 if self.scroll_invert_x else 1)
            dy = dy * self.scroll_gain_y * (-1 if self.scroll_invert_y else 1)
            if self.smooth_scroll and self.scroll_mode == "pixel":
                if self._smooth is None:
                    from .scroll import SmoothScrollEngine
                    self._smooth = SmoothScrollEngine(self._scroll_step,
                                                      rate_hz=self.smooth_scroll_hz,
                                                      duration_ms=self.smooth_scroll_ms)
                self._smooth.add(dx, dy)
            else:
                self._scroll_step(dx, dy)

    def _scroll_step(self, dx: float, dy: float):
        """Wire deltas are already in 1/120-notch units, matching *_HI_RES."""
        if self.scroll_mode == "line":
            self.scroll_x += dx
            self.scroll_y += dy
            hx, hy = round(self.scroll_x / 120), round(self.scroll_y / 120)
            if hx or hy:
                self.scroll_x -= hx * 120
                self.scroll_y -= hy * 120
                self.device.rel(REL_HWHEEL, hx)
                self.device.rel(REL_WHEEL, hy)
        else:
            if dx:
                self.device.rel(REL_HWHEEL_HI_RES, round(dx))
            if dy:
                self.device.rel(REL_WHEEL_HI_RES, round(dy))
        self.device.syn()

    def _key(self, event: dict):
        code = evdev_for_key(int(event["vk"]), int(event.get("scan", 0)),
                             bool(event.get("extended", False)))
        if code is None:
            return
        down = bool(event["down"])
        if down and code in self.keys:
            # Held key re-signalled by the host's autorepeat -> evdev repeat.
            self.device.emit(1, code, 2)
        else:
            self.device.key(code, down)
        self.device.syn()
        self.keys.add(code) if down else self.keys.discard(code)

    def _gesture(self, action):
        if action == "lock_screen":
            try:
                subprocess.Popen(["loginctl", "lock-session"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
        # Desktop-switch gestures are compositor-specific on Linux; ignored.

    def release_all(self):
        """Release injected state even if this session is already inactive."""
        with self._scroll_lock:
            self.active = False
        if self._smooth:
            self._smooth.cancel()
        for code in tuple(self.keys):
            self.device.key(code, False)
        for code in tuple(self.buttons):
            self.device.key(code, False)
        self.keys.clear()
        self.buttons.clear()
        self.scroll_x = self.scroll_y = 0.0
        self.device.syn()
        self.active = False

    def close(self):
        self.release_all()
        if self._smooth:
            self._smooth.stop()
            self._smooth = None
        self.device.close()
