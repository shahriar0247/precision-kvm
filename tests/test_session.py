"""Controller tests use doubles: no hooks, SSH process, or injected input."""
from unittest.mock import Mock, patch
import socket
import ssl

from precision_kvm.config import load_settings
from precision_kvm.session import HostSession, run_client
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


def test_bad_tls_handshake_does_not_stop_host_or_install_hooks():
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

    listener.accept.side_effect = accept
    with patch("precision_kvm.session.identity", return_value=("cert", "key", "t" * 32, "pin")), \
         patch("precision_kvm.session.TLSListener", return_value=listener), \
         patch("precision_kvm.windows.WindowsCapture") as capture:
        session._run()
        capture.assert_not_called()
    assert calls == 2
    assert any("Connection rejected" in message for message in messages)
    listener.close.assert_called()


def test_stop_before_ssh_launch_prevents_process_creation():
    session = HostSession(dict(load_settings(), ssh_target="user@example.local"), lambda value: None)
    session.ready.set()
    session.stopped.set()
    with patch("precision_kvm.session.subprocess.Popen") as launch:
        session.connect_mac()
        launch.assert_not_called()
