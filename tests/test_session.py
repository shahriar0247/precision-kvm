"""Controller tests use doubles: no hooks, SSH process, or injected input."""
from unittest.mock import Mock, patch
import socket
import ssl

from precision_kvm.config import load_settings
from precision_kvm.session import HostSession, run_client, run_client_forever
from precision_kvm.transport import BackpressureError


def test_activation_carries_settings_and_return_preserves_position():
    session = HostSession(load_settings(), lambda value: None)
    session.buffer = Mock()
    session.capture = Mock()
    assert session._emit({"type": "activate", "normalized_y": 0.3}) is True
    event = session.buffer.put.call_args.args[0]
    assert event["gain"] == session.settings["gain"]
    assert event["side"] == session.settings["side"]
    session._control({"type": "return", "normalized_y": 0.7, "normalized_x": 0.4})
    session.capture.return_local.assert_called_once_with(0.7, 0.4)


def test_overflow_restores_local_control_and_closes_connection():
    session = HostSession(load_settings(), lambda value: None)
    session.capture, session.channel, session.buffer = Mock(), Mock(), Mock()
    session.buffer.put.side_effect = BackpressureError("full")
    assert session._emit({"type": "key", "down": False}) is False
    session.capture.return_local.assert_called_once()
    session.channel.close.assert_called_once()


def test_client_denied_accessibility_never_connects_or_captures():
    with patch("precision_kvm.mac.MacInjector.accessibility_available", return_value=False), \
         patch("precision_kvm.transport.connect_tls") as connect:
        try:
            run_client({})
        except RuntimeError as exc:
            assert "Accessibility" in str(exc)
        else:
            raise AssertionError("Untrusted client should fail")
        connect.assert_not_called()


def test_emit_rejected_when_not_connected():
    session = HostSession(load_settings(), lambda value: None)
    assert session._emit({"type": "activate"}) is False


def test_ssh_target_validation_cannot_start_options_or_commands():
    session = HostSession(dict(load_settings(), ssh_target="-oProxyCommand=bad"), lambda value: None)
    with patch("precision_kvm.session.subprocess.Popen") as launch:
        session.connect_mac()
        launch.assert_not_called()


def test_bad_tls_handshake_keeps_host_running_without_sharing():
    # Hooks install at session start so local mac_mode works offline; a failed
    # handshake must never mark a peer connected.
    messages = []
    session = HostSession(load_settings(), messages.append)
    listener = Mock()
    calls = 0

    def accept():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ssl.SSLError("malformed handshake")
        session.stopped.set()
        raise socket.timeout()

    created = []

    class FakeCapture:
        def __init__(self, *args, **kwargs):
            self.peer_connected = False
            created.append(self)
        start = Mock()
        stop = Mock()
        release = Mock()
        return_local = Mock()

    listener.accept.side_effect = accept
    with patch("precision_kvm.session.identity", return_value=("cert", "key", "t" * 32, "pin")), \
         patch("precision_kvm.session.TLSListener", return_value=listener), \
         patch("precision_kvm.windows.WindowsCapture", FakeCapture):
        session._run()
    assert calls == 2
    assert len(created) == 1 and created[0].start.called
    assert created[0].peer_connected is False
    assert any("Connection rejected" in message for message in messages)
    listener.close.assert_called()


def test_stop_before_ssh_launch_prevents_process_creation():
    session = HostSession(dict(load_settings(), ssh_target="user@example.local"), lambda value: None)
    session.ready.set()
    session.stopped.set()
    with patch("precision_kvm.session.subprocess.Popen") as launch:
        session.connect_mac()
        launch.assert_not_called()


def test_reconnect_retries_every_second_and_stops_cleanly():
    stop = Mock()
    stop.is_set.return_value = False
    stop.wait.side_effect = [False, True]
    with patch("precision_kvm.session.run_client", side_effect=ConnectionRefusedError) as connect, \
         patch("precision_kvm.session.time.monotonic", side_effect=[10, 10.2, 11, 11.4]):
        run_client_forever({}, stop, lambda message: None)
    assert connect.call_count == 2
    assert abs(stop.wait.call_args_list[0].args[0] - 0.8) < 0.001
    assert abs(stop.wait.call_args_list[1].args[0] - 0.6) < 0.001
    assert connect.call_args.kwargs["stop_event"] is stop
    assert connect.call_args.kwargs["connect_timeout"] == 0.8


def test_reconnect_does_not_hide_permission_errors():
    import pytest
    with patch("precision_kvm.session.run_client", side_effect=RuntimeError("Accessibility required")):
        with pytest.raises(RuntimeError, match="Accessibility"):
            run_client_forever({})
