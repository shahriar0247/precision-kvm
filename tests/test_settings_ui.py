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
    ("gain", float("nan")), ("clipboard_max_mb", 513), ("emergency_click_count", 2),
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


def test_control_panel_constructs_and_collects_settings_without_starting():
    import tkinter as tk
    from precision_kvm.ui import ControlPanel
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("No desktop display available")
    root.withdraw()
    try:
        panel = ControlPanel(root)
        root.update_idletasks()
        assert panel.session is None
        assert panel.collect_settings()["mouse_mode"] in ("raw", "system")
        panel.variables["keymap_preset"].set("ctrl_cmd")
        assert panel.collect_settings()["keymap_preset"] == "ctrl_cmd"
        panel.option_command.set(True)
        panel.toggle_modifier_layout()
        assert panel.collect_settings()["keymap_preset"] == "option_cmd"
        panel.option_command.set(False)
        panel.toggle_modifier_layout()
        assert panel.collect_settings()["keymap_preset"] == "native"
        panel.source_key.set("CapsLock")
        panel.target_key.set("Escape")
        panel.add_mapping()
        assert panel.collect_settings()["keymap"]
    finally:
        root.destroy()
