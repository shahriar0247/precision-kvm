"""Pure server-side emergency-stop recognition, independent of key remapping."""
from __future__ import annotations

import math
import time


class EmergencyMonitor:
    def __init__(self, escape_seconds=1.5):
        if not math.isfinite(escape_seconds) or not 0.25 <= escape_seconds <= 10:
            raise ValueError("Invalid emergency-stop thresholds")
        self.escape_seconds = escape_seconds
        self.escape_down_at: float | None = None
        self.triggered = False

    def reset(self):
        self.escape_down_at = None
        self.triggered = False

    def key(self, vk: int, down: bool, now: float | None = None) -> str | None:
        if vk != 0x1B:
            return None
        now = time.monotonic() if now is None else now
        if down and self.escape_down_at is None:
            self.escape_down_at = now
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
