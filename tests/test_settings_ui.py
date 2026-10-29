"""Settings validation and native UI construction, without hooks or clipboard access."""
from unittest.mock import Mock

import pytest

from precision_kvm.config import load_settings, validate_settings
from precision_kvm.session import HostSession


def test_settings_validate_remaps_and_all_sensitivity_fields():
    settings = dict(load_settings(), keymap={"CapsLock": "Escape"}, scroll_gain_x="2.5",
                    scroll_gain_y="0.5", mouse_mode="system", keymap_preset="mac_layout")
    parsed = validate_settings(settings)
    assert parsed["scroll_gain_x"] == 2.5
    assert parsed["keymap"]
    assert parsed["keymap_preset"] == "mac_layout"


@pytest.mark.parametrize("key,value", [
    ("gain", float("nan")), ("clipboard_max_mb", 513), ("emergency_escape_seconds", 0),
    ("scroll_mode", "unknown"), ("mouse_mode", "absolute"), ("keymap", {"A": "unknown"}),
])
def test_settings_reject_unsafe_or_invalid_values(key, value):
    with pytest.raises(ValueError):
        validate_settings(dict(load_settings(), **{key: value}))


def test_host_stop_releases_input_before_clipboard_wait():
    session = HostSession(load_settings(), lambda message: None)
    order = []
    session._disconnect = lambda: order.append("input")
    session.clipboard = Mock()
    session.clipboard.stop.side_effect = lambda: order.append("clipboard")
    session.stop()
    assert order[:2] == ["input", "clipboard"]


def test_emergency_stops_server_instead_of_only_returning_pointer():
    messages = []
    session = HostSession(load_settings(), messages.append)
    session._disconnect = Mock()
    session.listener = Mock()
    session.emergency_stop("Escape held")
    assert session.stopped.is_set()
    session.listener.close.assert_called_once()
    assert "Emergency STOP" in messages[-1]


def test_web_panel_collects_settings_without_starting():
    from precision_kvm import ui_common
    settings = load_settings()
    collected = ui_common.collect(settings, {"keymap_preset": "ctrl_cmd"})
    assert collected["keymap_preset"] == "ctrl_cmd"
    with pytest.raises(ValueError):
        ui_common.collect(settings, {"mouse_mode": "absolute"})


def test_peer_drops_snap_to_left_or_right_edge():
    from precision_kvm import geometry
    mons = [(0, 0, 1920, 1080)]
    peer = (0, 0, 1920, 1080)
    assert geometry.snap_side((-1400, 400, *peer[2:]), mons) == (-1920, 0, 1920, 1080)
    assert geometry.snap_side((900, 700, *peer[2:]), mons) == (1920, 0, 1920, 1080)
    # A stored diagonal position self-heals to whichever side it sits on.
    assert geometry.snap_side((-1834, 968, *peer[2:]), mons) == (-1920, 0, 1920, 1080)
