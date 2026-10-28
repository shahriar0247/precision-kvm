"""Pure/fake scroll tests: no global event taps and no native input posting."""
import math
import random
import threading
import time

import pytest

from precision_kvm.scroll import (
    SCROLL_EVENT_TAG, MacLocalScrollTap, ScrollIntegrator, SmoothScrollEngine,
)


def test_integrator_immediate_response_and_exact_finite_settle():
    curve = ScrollIntegrator(duration_ms=60)
    first = curve.add(12.5, -30.25, 10)
    assert first == pytest.approx((2.5, -6.05))
    steps = [first] + [curve.step(10 + index / 120) for index in range(1, 20)]
    assert sum(step[0] for step in steps) == pytest.approx(12.5)
    assert sum(step[1] for step in steps) == pytest.approx(-30.25)
    assert not curve.pending
    assert curve.step(20) == (0, 0)


def test_random_fractional_input_and_reversals_conserve_signed_distance():
    randomizer = random.Random(42)
    curve = ScrollIntegrator(duration_ms=80)
    expected_x = expected_y = sent_x = sent_y = 0.0
    now = 1.0
    for _ in range(1000):
        now += randomizer.uniform(0.001, 0.02)
        dx, dy = randomizer.uniform(-20, 20), randomizer.uniform(-20, 20)
        expected_x += dx
        expected_y += dy
        x, y = curve.add(dx, dy, now)
        sent_x += x
        sent_y += y
        x, y = curve.step(now + 0.0005)
        sent_x += x
        sent_y += y
    x, y = curve.step(now + 0.24)
    assert sent_x + x == pytest.approx(expected_x)
    assert sent_y + y == pytest.approx(expected_y)
    assert not curve.pending


def test_reversal_cancels_old_residual_instead_of_queuing_a_fling():
    curve = ScrollIntegrator(duration_ms=60)
    first = curve.add(0, 100, 1)
    reverse = curve.add(0, -100, 1)
    assert first[1] == 20
    assert reverse[1] < 0
    tail = curve.step(1.06)
    assert first[1] + reverse[1] + tail[1] == pytest.approx(0)
    assert not curve.pending


def test_cancel_discards_tail_and_engine_does_not_start_for_zero_input():
    curve = ScrollIntegrator()
    curve.add(1, 2, 1)
    curve.cancel()
    assert curve.step(2) == (0, 0)
    engine = SmoothScrollEngine(lambda x, y: None)
    try:
        assert engine.add(0, 0)
        assert engine._thread is None
    finally:
        engine.stop()


@pytest.mark.parametrize("kwargs", [dict(duration_ms=9), dict(duration_ms=151),
                                    dict(duration_ms=float("nan")), dict(rate_hz=29),
                                    dict(rate_hz=241)])
def test_invalid_settings_rejected(kwargs):
    with pytest.raises(ValueError):
        SmoothScrollEngine(lambda x, y: None, **kwargs)


def test_nonfinite_inputs_do_not_create_events():
    events = []
    engine = SmoothScrollEngine(lambda x, y: events.append((x, y)))
    assert not engine.add(math.inf, 1)
    assert engine.error
    assert events == []
    engine.stop()


def test_worker_settles_and_stops_without_posts_after_cancel():
    events = []
    engine = SmoothScrollEngine(lambda x, y: events.append((x, y)), duration_ms=30)
    try:
        assert engine.add(13.25, -71.5)
        time.sleep(0.1)
        assert sum(x for x, y in events) == pytest.approx(13.25)
        assert sum(y for x, y in events) == pytest.approx(-71.5)
        assert engine.add(20, 30)
        engine.cancel()
        count = len(events)
        time.sleep(0.06)
        assert len(events) == count
    finally:
        engine.stop()
    assert not engine.add(1, 1)
    assert not engine._thread.is_alive()


def test_cancel_waits_for_in_progress_callback_before_returning():
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def emit(x, y):
        calls.append((x, y))
        if len(calls) == 2:
            entered.set()
            assert release.wait(1)

    engine = SmoothScrollEngine(emit, duration_ms=80)
    engine.add(0, 100)
    assert entered.wait(1)
    cancel = threading.Thread(target=lambda: (engine.cancel(), cancelled.set()))
    cancel.start()
    assert not cancelled.wait(0.02)
    release.set()
    assert cancelled.wait(1)
    count = len(calls)
    time.sleep(0.03)
    assert len(calls) == count
    cancel.join(1)
    engine.stop()


class FakeQuartz:
    kCGEventScrollWheel = 22
    kCGEventTapDisabledByTimeout = 100
    kCGEventTapDisabledByUserInput = 101
    kCGEventSourceUserData = "tag"
    kCGScrollWheelEventIsContinuous = "continuous"
    kCGScrollWheelEventDeltaAxis1 = "line_y"
    kCGScrollWheelEventDeltaAxis2 = "line_x"
    kCGScrollWheelEventPointDeltaAxis1 = "point_y"
    kCGScrollWheelEventPointDeltaAxis2 = "point_x"
    kCGScrollEventUnitPixel = "pixel"
    kCGEventSourceStatePrivate = "private"
    kCGSessionEventTap = "session"
    kCGHeadInsertEventTap = "head"
    kCGEventTapOptionDefault = "filter"

    def __init__(self, deny=False):
        self.posts, self.enables, self.taps = [], [], []
        self.deny = deny

    def CGEventSourceCreate(self, state):
        return object()

    def CGEventSourceSetLocalEventsSuppressionInterval(self, source, interval):
        pass

    def CGEventTapCreate(self, *args):
        self.taps.append(args)
        return None if self.deny else object()

    def CGEventTapEnable(self, tap, enabled):
        self.enables.append(enabled)

    def CGEventGetIntegerValueField(self, event, field):
        return event.get(field, 0)

    def CGEventGetFlags(self, event):
        return event.get("flags", 0)

    def CGEventGetLocation(self, event):
        return event.get("position", (100, 200))

    def CGEventCreateScrollWheelEvent(self, source, unit, count, y, x):
        return {"unit": unit, "x": x, "y": y}

    def CGEventSetIntegerValueField(self, event, field, value):
        event[field] = value

    def CGEventSetFlags(self, event, flags):
        event["flags"] = flags

    def CGEventSetLocation(self, event, position):
        event["position"] = position

    def CGEventPost(self, location, event):
        self.posts.append(event)


class FakeCoreFoundation:
    kCFRunLoopCommonModes = "common"
    kCFRunLoopDefaultMode = "default"
    kCFRunLoopRunFinished = 1

    def __init__(self):
        self.stopped = threading.Event()
        self.invalidated = False

    def CFMachPortCreateRunLoopSource(self, allocator, tap, order):
        return object()

    def CFRunLoopGetCurrent(self):
        return object()

    def CFRunLoopAddSource(self, *args):
        pass

    def CFRunLoopRemoveSource(self, *args):
        pass

    def CFRunLoopRunInMode(self, mode, duration, return_after_source):
        self.stopped.wait(0.01)
        return 2

    def CFRunLoopStop(self, loop):
        self.stopped.set()

    def CFMachPortInvalidate(self, tap):
        self.invalidated = True


def fake_tap(**kwargs):
    q, cf = FakeQuartz(), FakeCoreFoundation()
    tap = MacLocalScrollTap(quartz=q, corefoundation=cf, permission_check=lambda: True, **kwargs)
    return tap, q, cf


def test_local_tap_passes_native_trackpad_and_tagged_events_unchanged():
    tap, q, cf = fake_tap()
    tap.start()  # Fake APIs only: never installs an operating-system tap.
    try:
        for event in ({"continuous": 1, "line_y": 10},
                      {"tag": SCROLL_EVENT_TAG, "line_y": 10}):
            assert tap._tap_callback(None, 22, event, None) is event
        assert q.posts == []
    finally:
        tap.stop()
    assert cf.invalidated


def test_local_discrete_wheel_uses_tagged_pixel_events_and_preserves_distance():
    tap, q, cf = fake_tap(duration_ms=20)
    tap.start()
    try:
        event = {"line_y": 2, "line_x": -1, "flags": 16, "position": (25, 50)}
        assert tap._tap_callback(None, 22, event, None) is None
        time.sleep(0.08)
        assert sum(event["y"] for event in q.posts) == 60
        assert sum(event["x"] for event in q.posts) == -30
        assert all(event["tag"] == SCROLL_EVENT_TAG and event["unit"] == "pixel" for event in q.posts)
        assert all(event["flags"] == 16 and event["position"] == (25, 50) for event in q.posts)
    finally:
        tap.stop()
    count = len(q.posts)
    assert tap._tap_callback(None, 22, {"line_y": 10}, None) is not None
    time.sleep(0.03)
    assert len(q.posts) == count


def test_event_tap_permission_failure_is_explicit_without_prompt_or_tap_creation():
    tap, q, cf = fake_tap()
    tap.permission_check = lambda: False
    with pytest.raises(RuntimeError, match="Accessibility"):
        tap.start()
    assert q.taps == []
    assert q.posts == []


def test_native_tap_denial_fails_open_and_surfaces_error():
    q, cf = FakeQuartz(deny=True), FakeCoreFoundation()
    tap = MacLocalScrollTap(quartz=q, corefoundation=cf, permission_check=lambda: True,
                            status=lambda message: None)
    with pytest.raises(RuntimeError, match="denied"):
        tap.start()
    assert q.posts == []
    assert not tap._active


def test_zero_delta_and_disabled_engine_leave_original_wheel_untouched():
    tap, q, cf = fake_tap()
    tap.start()
    try:
        event = {"line_y": 0}
        assert tap._tap_callback(None, 22, event, None) is event
        tap._engine.stop()
        event = {"line_y": 1}
        assert tap._tap_callback(None, 22, event, None) is event
        assert q.posts == []
    finally:
        tap.stop()
