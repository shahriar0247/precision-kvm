"""Session orchestration. Hooks are installed only after authenticated readiness."""
from __future__ import annotations

import socket
import re
import subprocess
import threading
import time
from typing import Callable, TYPE_CHECKING

from .config import identity, state_dir
from .clipboard import ClipboardClient, ClipboardServer
from .transport import (BackpressureError, EventBuffer, FramedSocket, ProtocolError,
                        Receiver, Sender, TLSListener, authenticate)

if TYPE_CHECKING:
    from .windows import WindowsCapture


class HostSession:
    def __init__(self, settings: dict, status: Callable[[str], None] = print):
        self.settings, self.status = settings, status
        self.stopped = threading.Event()
        self.capture: WindowsCapture | None = None
        self.sender: Sender | None = None
        self.channel: FramedSocket | None = None
        self.listener: TLSListener | None = None
        self.buffer: EventBuffer | None = None
        self.rtt_ms = 0.0
        self.thread: threading.Thread | None = None
        self._workers: list[threading.Thread] = []
        self.ready = threading.Event()
        self.remote_process: subprocess.Popen | None = None
        self._remote_lock = threading.Lock()
        self.clipboard: ClipboardServer | None = None
        self.emergency_reason = ""

    def start(self):
        if self.thread and self.thread.is_alive():
            raise RuntimeError("Host already running")
        self.stopped.clear()
        self.ready.clear()
        self.thread = threading.Thread(target=self._run, name="kvm-host", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self._disconnect()
        if self.clipboard:
            self.clipboard.stop()
        if self.listener:
            self.listener.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(4)
        with self._remote_lock:
            process = self.remote_process
            if process and process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        if process:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def emergency_stop(self, reason):
        """The native hook already released local input; fully stop the host."""
        self.emergency_reason = str(reason)
        self.stop()
        self.status(f"Emergency STOP: {reason} • start the host to resume")

    def connect_mac(self):
        """Launch the prepared client through key-authenticated SSH, explicitly from UI."""
        target = self.settings.get("ssh_target", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+", target):
            self.status("Enter a valid user@host SSH target, or start the Mac client manually")
            return
        if not self.ready.wait(4) or self.stopped.is_set():
            return
        try:
            with self._remote_lock:
                if self.stopped.is_set():
                    return
                with (state_dir() / "mac-client.log").open("ab") as log:
                    process = subprocess.Popen(
                        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target,
                         "cd ~/Applications/PrecisionKVM && exec .venv/bin/python -m precision_kvm "
                         "client --config ~/.config/PrecisionKVM/client.json"],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    self.remote_process = process
            process.wait()
            if not self.stopped.is_set() and process.returncode != 0:
                self.status("Mac client exited; see the local mac-client.log, then stop and reconnect")
        except Exception as exc:
            self.status(f"Cannot start Mac client: {exc}")

    def _disconnect(self):
        if self.capture:
            self.capture.stop()
        if self.sender:
            self.sender.stop()
        if self.channel:
            self.channel.close()

    def _emit(self, event):
        try:
            if event.get("type") == "activate":
                event = dict(event, side=self.settings["side"], gain=self.settings["gain"],
                             scroll_gain=self.settings["scroll_gain"],
                             swap_ctrl_cmd=self.settings["swap_ctrl_cmd"])
                for name in ("scroll_gain_x", "scroll_gain_y", "scroll_invert_x", "scroll_invert_y",
                             "scroll_mode", "keymap", "keymap_preset", "smooth_scroll",
                             "smooth_scroll_ms", "smooth_scroll_hz", "windows_home_end", "windows_word_nav"):
                    if name in self.settings:
                        event[name] = self.settings[name]
            if self.buffer:
                self.buffer.put(event)
                return True
            return False
        except Exception:
            # Hook callbacks must never wait for networking or leak exceptions.
            if self.capture:
                self.capture.return_local()
            if self.channel:
                self.channel.close()
            return False

    def _run(self):
        from .windows import WindowsCapture
        try:
            cert, key, token, _ = identity()
            self.listener = TLSListener(str(cert), str(key), port=int(self.settings["port"]))
            if self.settings.get("clipboard_enabled", False):
                self.clipboard = ClipboardServer(cert, key, token, port=int(self.settings["port"]) + 2,
                    max_bytes=int(self.settings.get("clipboard_max_mb", 128)) * 1024 * 1024,
                    status=lambda message: self.status(f"Clipboard: {message}"))
                self.clipboard.start()
            self.ready.set()
            self.status("Waiting for paired Mac; local input remains active")
            while not self.stopped.is_set():
                try:
                    self.channel = self.listener.accept()
                except socket.timeout:
                    continue
                except OSError as exc:
                    if self.stopped.is_set():
                        break
                    self.status(f"Connection rejected: {exc}")
                    continue
                try:
                    authenticate(self.channel, token, server=True)
                    ready = self.channel.recv()
                    if ready != {"type": "client_ready", "version": 1}:
                        raise RuntimeError("Mac has not granted Accessibility permission")
                    if self.stopped.is_set():
                        break
                    self.buffer = EventBuffer(int(self.settings["mouse_hz"]))
                    self.capture = WindowsCapture(self._emit, side=self.settings["side"],
                        high_resolution_timer=True, on_emergency=self.emergency_stop,
                        mouse_mode=self.settings.get("mouse_mode", "raw"),
                        gesture_bridge=self.settings.get("gesture_bridge", True),
                        native_touchpad=self.settings.get("native_touchpad", False),
                        emergency_escape_seconds=self.settings.get("emergency_escape_seconds", 1.5),
                        emergency_click_count=self.settings.get("emergency_click_count", 6),
                        emergency_click_window=self.settings.get("emergency_click_window", 1.2))
                    self.sender = Sender(self.channel, self.buffer)
                    writer = threading.Thread(target=self._writer, daemon=True, name="kvm-writer")
                    writer.start()
                    self._workers.append(writer)
                    self.capture.start()
                    self.status("Connected • move across the configured edge to share")
                    Receiver(self.channel, self._control).run()
                except Exception as exc:
                    if not self.stopped.is_set():
                        self.status(f"Disconnected: {type(exc).__name__}: {exc}")
                finally:
                    self._disconnect()
                    for worker in self._workers:
                        worker.join(3)
                    self._workers.clear()
                    self.capture = self.sender = self.channel = self.buffer = None
        except Exception as exc:
            if not self.stopped.is_set():
                self.status(f"Host stopped: {exc}")
        finally:
            self._disconnect()
            if self.clipboard:
                self.clipboard.stop()
            if self.listener:
                self.listener.close()

    def _writer(self):
        try:
            if self.sender:
                self.sender.run()
        except Exception:
            if self.capture:
                self.capture.return_local()

    def _control(self, event):
        if event.get("type") == "return" and self.capture:
            self.capture.return_local(event.get("normalized_y"), event.get("normalized_x"))
        elif event.get("type") == "ping":
            self._emit({"type": "pong", "stamp": event["stamp"]})
        elif event.get("type") == "rtt":
            self.rtt_ms = float(event["ms"])


def run_client_forever(config: dict, stop_event=None, status=print):
    """Retry on a one-second start-to-start cadence without overlapping attempts."""
    stop_event = stop_event if stop_event is not None else threading.Event()
    clipboard = None
    try:
        if config.get("clipboard_enabled", bool(config.get("host"))):
            clipboard = ClipboardClient(config,
                max_bytes=int(config.get("clipboard_max_mb", 128)) * 1024 * 1024,
                status=lambda message: status(f"Clipboard: {message}"))
            clipboard.start()
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                run_client(config, connect_timeout=0.8, stop_event=stop_event, status=status)
            except (OSError, EOFError, ProtocolError, BackpressureError) as exc:
                status(f"Server unavailable / disconnected ({type(exc).__name__}); retrying every second")
            if stop_event.wait(max(0.0, 1.0 - (time.monotonic() - started))):
                break
    finally:
        if clipboard:
            clipboard.stop()


def run_client(config: dict, connect_timeout=3, stop_event=None, status=print):
    from .mac import MacInjector
    from .transport import connect_tls

    if not MacInjector.accessibility_available(prompt=True):
        raise RuntimeError("Grant Accessibility access to this Python executable/Terminal in "
                           "System Settings > Privacy & Security > Accessibility, then reconnect")
    stopped = threading.Event()
    buffer = EventBuffer(120)
    injector = MacInjector(on_return=buffer.put)
    channel = None
    sender = None
    workers = []
    try:
        channel = connect_tls(config["host"], int(config["port"]), config["fingerprint"],
                              timeout=connect_timeout)
        authenticate(channel, config["token"], server=False)
        if stop_event is not None and stop_event.is_set():
            return
        channel.send({"type": "client_ready", "version": 1})
        sender = Sender(channel, buffer)

        def write():
            try:
                sender.run()
            except Exception:
                stopped.set()

        def heartbeat():
            while not stopped.wait(0.25):
                try:
                    buffer.put({"type": "ping", "stamp": time.perf_counter()})
                except Exception:
                    break

        def receive(event):
            if event.get("type") == "pong":
                buffer.put({"type": "rtt", "ms": (time.perf_counter() - event["stamp"]) * 1000})
            else:
                if event.get("type") == "activate":
                    tweaks = config.get("tweaks", {})
                    allowed = ("keymap_preset", "gain", "scroll_gain_x", "scroll_gain_y",
                               "scroll_invert_x", "scroll_invert_y", "scroll_mode",
                               "smooth_scroll", "smooth_scroll_ms", "smooth_scroll_hz",
                               "windows_home_end", "windows_word_nav")
                    if isinstance(tweaks, dict):
                        event = dict(event, **{name: tweaks[name] for name in allowed if name in tweaks})
                injector.handle(event)

        def watch_stop():
            while not stopped.wait(0.05):
                if stop_event.is_set():
                    channel.close()
                    break

        for target in (write, heartbeat):
            worker = threading.Thread(target=target, daemon=True)
            worker.start()
            workers.append(worker)
        if stop_event is not None:
            worker = threading.Thread(target=watch_stop, daemon=True)
            worker.start()
            workers.append(worker)
        status("Connected. Ctrl+Alt+F12 returns locally; hold Esc to stop the PC host.")
        Receiver(channel, receive, injector.release_all).run()
    finally:
        stopped.set()
        injector.release_all()
        if sender:
            sender.stop()
        elif channel:
            channel.close()
        buffer.close()
        for worker in workers:
            worker.join(3)
        if hasattr(injector, "close"):
            injector.close()
