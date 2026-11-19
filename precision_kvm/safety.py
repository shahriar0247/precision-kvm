"""Pure server-side emergency-stop recognition, independent of key remapping."""
from __future__ import annotations

import math
import time

PRESS_COUNT = 5      # rapid Escape taps that panic-stop the session
PRESS_WINDOW = 2.0   # seconds the taps must fit inside


class EmergencyMonitor:
    """Two gestures: hold Escape for `escape_seconds`, or tap it PRESS_COUNT
    times inside PRESS_WINDOW. A held key produces no further events, so the
    hold must also be re-checked on release — hence tick() runs everywhere."""

    def __init__(self, escape_seconds=1.5):
        if not math.isfinite(escape_seconds) or not 0.25 <= escape_seconds <= 10:
            raise ValueError("Invalid emergency-stop thresholds")
        self.escape_seconds = escape_seconds
        self.escape_down_at: float | None = None
        self._presses: list[float] = []
        self.triggered = False

    def reset(self):
        self.escape_down_at = None
        self._presses.clear()
        self.triggered = False

    def key(self, vk: int, down: bool, now: float | None = None) -> str | None:
        if vk != 0x1B:
            return None
        now = time.monotonic() if now is None else now
        if down and self.escape_down_at is None:
            self.escape_down_at = now
            self._presses = [t for t in self._presses if now - t <= PRESS_WINDOW]
            self._presses.append(now)
            if len(self._presses) >= PRESS_COUNT:
                self.triggered = True
                return "Escape-taps emergency stop"
        elif not down:
            reason = self.tick(now)
            self.escape_down_at = None
            return reason
        return self.tick(now)

    def tick(self, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        if (not self.triggered and self.escape_down_at is not None
                and now - self.escape_down_at >= self.escape_seconds):
            self.triggered = True
            return "Escape-hold emergency stop"
        return None
