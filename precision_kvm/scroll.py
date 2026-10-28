"""Finite, distance-preserving scroll easing and optional macOS wheel smoothing.

Only wheel deltas are eased. Mouse movement is never buffered or interpolated.
Importing this module does not create a global event tap or post native input.
"""
from __future__ import annotations

import math
import sys
import threading
import time
from typing import Callable

SCROLL_EVENT_TAG = 0x50524B56


class ScrollIntegrator:
    """Pure finite cubic easing; callbacks, timers and native APIs live elsewhere.

    Each add emits 20% of the new net residual immediately. The remaining
    signed distance settles exactly within duration_ms after the latest input.
    Opposing input cancels existing residual rather than queuing another fling.
    """
    def __init__(self, duration_ms=60, initial_fraction=0.2):
        if not math.isfinite(duration_ms) or not 10 <= duration_ms <= 150:
            raise ValueError("Scroll duration must be between 10 and 150 ms")
        if not math.isfinite(initial_fraction) or not 0 <= initial_fraction <= 1:
            raise ValueError("Initial scroll fraction must be between zero and one")
        self.duration = duration_ms / 1000
        self.initial_fraction = initial_fraction
        self.pending_x = self.pending_y = 0.0
        self.last_time = self.deadline = 0.0

    @property
    def pending(self):
        return self.pending_x != 0 or self.pending_y != 0

    def add(self, dx, dy, now):
        dx, dy, now = float(dx), float(dy), float(now)
        if (not all(math.isfinite(value) for value in (dx, dy, now))
                or max(abs(dx), abs(dy)) > 1_000_000):
            raise ValueError("Invalid scroll displacement")
        if not dx and not dy:
            return 0.0, 0.0
        now = max(now, self.last_time)
        old_x, old_y = self.step(now)
        new_x, new_y = self.pending_x + dx, self.pending_y + dy
        if max(abs(new_x), abs(new_y)) > 1_000_000_000:
            raise ValueError("Scroll residual exceeds safety limit")
        first_x = new_x * self.initial_fraction
        first_y = new_y * self.initial_fraction
        self.pending_x, self.pending_y = new_x - first_x, new_y - first_y
        self.last_time, self.deadline = now, now + self.duration
        return old_x + first_x, old_y + first_y

    def step(self, now):
        now = float(now)
        if not math.isfinite(now):
            raise ValueError("Invalid scroll clock")
        now = max(now, self.last_time)
        if not self.pending:
            self.last_time = now
            return 0.0, 0.0
        if now >= self.deadline:
            result = self.pending_x, self.pending_y
            self.pending_x = self.pending_y = 0.0
            self.last_time = now
            return result
        previous_remaining = self.deadline - self.last_time
        remaining = self.deadline - now
        fraction = 1 - (remaining / previous_remaining) ** 3 if previous_remaining > 0 else 1
        dx, dy = self.pending_x * fraction, self.pending_y * fraction
        self.pending_x -= dx
        self.pending_y -= dy
        self.last_time = now
        return dx, dy

    def cancel(self):
        self.pending_x = self.pending_y = 0.0


class SmoothScrollEngine:
    """Lazy scroll-only worker with synchronous cancellation.

    on_step runs under the engine's RLock. Do not hold another lock acquired
    by on_step while calling add/cancel/stop, to avoid lock inversion.
    cancel returns only after any in-progress callback has completed.
    """
    def __init__(self, on_step: Callable[[float, float], None], rate_hz=120,
                 duration_ms=60):
        if type(rate_hz) is not int or not 30 <= rate_hz <= 240:
            raise ValueError("Scroll update rate must be between 30 and 240 Hz")
        self.on_step = on_step
        self.rate_hz, self.duration_ms = rate_hz, duration_ms
        self.integrator = ScrollIntegrator(duration_ms)
        self._condition = threading.Condition(threading.RLock())
        self._thread: threading.Thread | None = None
        self._stopped = False
        self.error: str | None = None

    def _emit(self, step):
        if step[0] or step[1]:
            self.on_step(*step)

    def add(self, dx, dy):
        with self._condition:
            if self._stopped:
                return False
            try:
                step = self.integrator.add(dx, dy, time.perf_counter())
                self._emit(step)
                if self.integrator.pending and self._thread is None:
                    self._thread = threading.Thread(target=self._run, name="kvm-scroll", daemon=True)
                    self._thread.start()
                self._condition.notify_all()
                return True
            except Exception as exc:
                self.error = str(exc)
                self.integrator.cancel()
                self._stopped = True
                self._condition.notify_all()
                return False

    def _run(self):
        period = 1 / self.rate_hz
        due = time.perf_counter() + period
        with self._condition:
            while not self._stopped:
                if not self.integrator.pending:
                    self._condition.wait()
                    due = time.perf_counter() + period
                    continue
                now = time.perf_counter()
                if now < due:
                    self._condition.wait(due - now)
                    continue
                try:
                    self._emit(self.integrator.step(now))
                except Exception as exc:
                    self.error = str(exc)
                    self.integrator.cancel()
                    self._stopped = True
                    return
                due = due + period if now - due < period else now + period

    def cancel(self):
        with self._condition:
            self.integrator.cancel()
            self._condition.notify_all()

    def stop(self):
        with self._condition:
            self.integrator.cancel()
            self._stopped = True
            self._condition.notify_all()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(3)
            if self._thread.is_alive():
                raise RuntimeError("Scroll worker did not stop")


class MacLocalScrollTap:
    """Optional global smoothing for discrete Mac mouse wheels only.

    Continuous/pixel trackpad scrolling and PrecisionKVM-tagged scroll events
    pass unchanged. Enabling the tap requires existing macOS permissions.
    No private gesture APIs or TCC changes are used.
    """
    def __init__(self, rate_hz=120, duration_ms=60, pixels_per_line=30,
                 status: Callable[[str], None] = print, quartz=None,
                 corefoundation=None, permission_check=None):
        if not math.isfinite(pixels_per_line) or not 1 <= pixels_per_line <= 200:
            raise ValueError("Wheel sensitivity must be between 1 and 200 pixels per line")
        if quartz is None:
            if sys.platform != "darwin":
                raise RuntimeError("Local mouse wheel smoothing requires macOS")
            import Quartz
            import CoreFoundation
            quartz, corefoundation = Quartz, CoreFoundation
        if corefoundation is None:
            raise ValueError("CoreFoundation is required")
        self.q, self.cf = quartz, corefoundation
        self.rate_hz, self.duration_ms = rate_hz, duration_ms
        self.pixels_per_line, self.status = pixels_per_line, status
        self.permission_check = permission_check
        self._engine = SmoothScrollEngine(self._emit_pixels, rate_hz, duration_ms)
        self._thread: threading.Thread | None = None
        self._ready, self._stopped = threading.Event(), threading.Event()
        self._active = False
        self._tap = self._loop = self._run_source = self._source = None
        self._post_lock = threading.RLock()
        self._carry_x = self._carry_y = 0.0
        self._flags = 0
        self._position = None
        self.error: str | None = None
        self._callback = self._tap_callback  # Keep the native callback alive.

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        check = self.permission_check
        if check is None:
            from .mac import MacInjector
            check = MacInjector.accessibility_available
        if not check():
            raise RuntimeError("Enable Accessibility for this Mac Python/Terminal before local wheel smoothing")
        self.error = None
        self._ready.clear()
        self._stopped.clear()
        self._engine = SmoothScrollEngine(self._emit_pixels, self.rate_hz, self.duration_ms)
        self._thread = threading.Thread(target=self._run, name="kvm-mac-wheel-tap", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            self.stop()
            raise RuntimeError("Mac mouse wheel tap startup timed out")
        if self.error:
            error = self.error
            self.stop()
            raise RuntimeError(error)

    def stop(self):
        self._stopped.set()
        self._active = False
        self._engine.stop()
        tap, loop = self._tap, self._loop
        if tap is not None:
            self.q.CGEventTapEnable(tap, False)
        if loop is not None:
            self.cf.CFRunLoopStop(loop)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(3)
            if self._thread.is_alive():
                raise RuntimeError("Mac mouse wheel tap did not stop")
        with self._post_lock:
            self._carry_x = self._carry_y = 0.0

    def _run(self):
        q, cf = self.q, self.cf
        try:
            if self._stopped.is_set():
                return
            self._source = q.CGEventSourceCreate(q.kCGEventSourceStatePrivate)
            if self._source is None:
                raise RuntimeError("Could not create Mac wheel event source")
            q.CGEventSourceSetLocalEventsSuppressionInterval(self._source, 0.0)
            self._tap = q.CGEventTapCreate(q.kCGSessionEventTap, q.kCGHeadInsertEventTap,
                q.kCGEventTapOptionDefault, 1 << q.kCGEventScrollWheel, self._callback, None)
            if self._tap is None:
                raise RuntimeError("macOS denied the wheel event tap; check Accessibility and Input Monitoring")
            self._run_source = cf.CFMachPortCreateRunLoopSource(None, self._tap, 0)
            if self._run_source is None:
                raise RuntimeError("Could not create Mac wheel tap run-loop source")
            self._loop = cf.CFRunLoopGetCurrent()
            cf.CFRunLoopAddSource(self._loop, self._run_source, cf.kCFRunLoopCommonModes)
            q.CGEventTapEnable(self._tap, True)
            self._active = not self._stopped.is_set()
            self._ready.set()
            while not self._stopped.is_set():
                result = cf.CFRunLoopRunInMode(cf.kCFRunLoopDefaultMode, 0.5, False)
                if result == cf.kCFRunLoopRunFinished:
                    raise RuntimeError("Mac wheel tap run loop unexpectedly finished")
        except Exception as exc:
            self.error = str(exc)
            self.status(f"Local wheel smoothing unavailable: {exc}")
        finally:
            self._active = False
            self._engine.cancel()
            if self._tap is not None:
                q.CGEventTapEnable(self._tap, False)
            if self._loop is not None and self._run_source is not None:
                cf.CFRunLoopRemoveSource(self._loop, self._run_source, cf.kCFRunLoopCommonModes)
            if self._tap is not None:
                cf.CFMachPortInvalidate(self._tap)
            self._tap = self._loop = self._run_source = self._source = None
            self._ready.set()

    def _tap_callback(self, proxy, event_type, event, refcon):
        q = self.q
        if not self._active or self._stopped.is_set():
            return event
        try:
            if event_type in (q.kCGEventTapDisabledByTimeout, q.kCGEventTapDisabledByUserInput):
                self._engine.cancel()
                tap = self._tap
                if tap is not None:
                    q.CGEventTapEnable(tap, True)
                return event
            if event_type != q.kCGEventScrollWheel:
                return event
            if q.CGEventGetIntegerValueField(event, q.kCGEventSourceUserData) == SCROLL_EVENT_TAG:
                return event
            if q.CGEventGetIntegerValueField(event, q.kCGScrollWheelEventIsContinuous):
                return event
            y = q.CGEventGetIntegerValueField(event, q.kCGScrollWheelEventDeltaAxis1)
            x = q.CGEventGetIntegerValueField(event, q.kCGScrollWheelEventDeltaAxis2)
            dy = y * self.pixels_per_line if y else q.CGEventGetIntegerValueField(event, q.kCGScrollWheelEventPointDeltaAxis1)
            dx = x * self.pixels_per_line if x else q.CGEventGetIntegerValueField(event, q.kCGScrollWheelEventPointDeltaAxis2)
            if not dx and not dy:
                return event
            flags = q.CGEventGetFlags(event)
            if flags != self._flags:
                self._engine.cancel()
                with self._post_lock:
                    self._carry_x = self._carry_y = 0.0
            with self._post_lock:
                self._flags = flags
                self._position = q.CGEventGetLocation(event)
            if self._engine.add(dx, dy):
                return None
        except Exception as exc:
            self.error = str(exc)
            self.status(f"Local wheel smoothing bypassed: {exc}")
        return event  # Fail open: never swallow a wheel event we cannot replace.

    def _emit_pixels(self, dx, dy):
        with self._post_lock:
            if not self._active or self._stopped.is_set():
                return
            self._carry_x += dx
            self._carry_y += dy
            x = math.trunc(self._carry_x + math.copysign(1e-9, self._carry_x))
            y = math.trunc(self._carry_y + math.copysign(1e-9, self._carry_y))
            if not x and not y:
                return
            self._carry_x -= x
            self._carry_y -= y
            q = self.q
            event = q.CGEventCreateScrollWheelEvent(self._source, q.kCGScrollEventUnitPixel, 2, y, x)
            if event is None:
                raise RuntimeError("macOS refused a smooth scroll event")
            q.CGEventSetIntegerValueField(event, q.kCGEventSourceUserData, SCROLL_EVENT_TAG)
            q.CGEventSetFlags(event, self._flags)
            if self._position is not None:
                q.CGEventSetLocation(event, self._position)
            q.CGEventPost(q.kCGSessionEventTap, event)
