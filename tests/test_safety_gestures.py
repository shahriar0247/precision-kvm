"""No hooks or input injection: pure recognition and capture fail-safe state."""
import threading
from unittest.mock import patch

import pytest

from precision_kvm.gestures import GestureShortcutBridge
from precision_kvm.safety import EmergencyMonitor
from precision_kvm.windows import WindowsCapture


def test_escape_hold_does_not_depend_on_keyboard_repeat():
    monitor = EmergencyMonitor()
    assert monitor.key(0x1B, True, 10) is None
    assert monitor.tick(11.49) is None
    assert monitor.tick(11.5) == "Escape-hold emergency stop"
    assert monitor.tick(15) is None


def test_short_escape_and_repeats_preserve_original_hold_time():
    monitor = EmergencyMonitor()
    monitor.key(0x1B, True, 10)
    monitor.key(0x1B, True, 10.5)
    assert monitor.tick(11.5)
    monitor.reset()
    monitor.key(0x1B, True, 20)
    monitor.key(0x1B, False, 20.5)
    assert monitor.tick(30) is None
    monitor.key(0x1B, True, 40)
    assert monitor.key(0x1B, False, 42) == "Escape-hold emergency stop"


def test_emergency_stop_is_latched_disarmed_and_returns_local_before_callback():
    calls = []
    completed = threading.Event()
    capture = None

    def stopped(reason):
        calls.append((reason, capture.active, capture._stopping.is_set()))
        completed.set()

    capture = WindowsCapture(lambda event: None, on_emergency=stopped)
    capture._size = (1920, 1080)
    capture.active = True
    with patch("precision_kvm.windows.ctypes.windll", create=True):
        capture._emergency_stop("test stop")
        assert completed.wait(1)
        capture._emergency_stop("duplicate")
    assert calls == [("test stop", False, True)]
    assert capture._emergency_requested


@pytest.mark.parametrize("kwargs", [
    {"escape_seconds": 0}, {"escape_seconds": float("nan")},
])
def test_invalid_safety_thresholds_rejected(kwargs):
    with pytest.raises(ValueError):
        EmergencyMonitor(**kwargs)


@pytest.mark.parametrize("keys,action", [
    ([0x5B, 0x11, 0x25], "desktop_left"),
    ([0xA3, 0x5C, 0x27], "desktop_right"),
    ([0x5B, 0x09], "mission_control"),
    ([0x5B, 0x44], "show_desktop"),
])
def test_gesture_chords_emit_once_per_press(keys, action):
    bridge = GestureShortcutBridge()
    results = [bridge.feed(key, True) for key in keys]
    assert results[-1] == action
    assert bridge.feed(keys[-1], True) is None
    for key in reversed(keys):
        assert bridge.feed(key, False) is None
    assert not bridge.pressed


def test_unrelated_injected_keys_are_not_translated_and_reset_clears_modifiers():
    bridge = GestureShortcutBridge()
    assert bridge.feed(0x41, True) is None
    bridge.feed(0x5B, True)
    bridge.reset()
    assert bridge.feed(0x44, True) is None


def test_mouse_modes_are_explicit():
    assert WindowsCapture(lambda event: None).mouse_mode == "raw"
    assert WindowsCapture(lambda event: None, mouse_mode="system").mouse_mode == "system"
    with pytest.raises(ValueError):
        WindowsCapture(lambda event: None, mouse_mode="absolute")
