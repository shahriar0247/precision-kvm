"""Pure server-side emergency-stop recognition, independent of key remapping."""
from __future__ import annotations

from collections import deque
import math
import time


class EmergencyMonitor:
    def __init__(self, escape_seconds=1.5, click_count=6, click_window=1.2):
        if (not math.isfinite(escape_seconds) or not 0.25 <= escape_seconds <= 10
                or not isinstance(click_count, int) or not 3 <= click_count <= 20
                or not math.isfinite(click_window) or not 0.25 <= click_window <= 5):
            raise ValueError("Invalid emergency-stop thresholds")
        self.escape_seconds = escape_seconds
        self.click_count = click_count
        self.click_window = click_window
        self.escape_down_at: float | None = None
        self.clicks: deque[float] = deque(maxlen=click_count)
        self.triggered = False

    def reset(self):
        self.escape_down_at = None
        self.clicks.clear()
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

    def click(self, button: str, down: bool, now: float | None = None) -> str | None:
        if button != "left" or not down or self.triggered:
            return None
        now = time.monotonic() if now is None else now
        self.clicks.append(now)
        while self.clicks and now - self.clicks[0] > self.click_window:
            self.clicks.popleft()
        if len(self.clicks) >= self.click_count:
            self.triggered = True
            return "Rapid-click emergency stop"
        return None

    def tick(self, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        if (not self.triggered and self.escape_down_at is not None
                and now - self.escape_down_at >= self.escape_seconds):
            self.triggered = True
            return "Escape-hold emergency stop"
        return None
