"""Session orchestration. Hooks are installed only after authenticated readiness."""
from __future__ import annotations

import socket
import re
import ssl
import subprocess
import sys
import threading
import time
from typing import Callable, TYPE_CHECKING

from . import __version__
from .config import identity, state_dir
from .clipboard import ClipboardClient, ClipboardServer
from .transport import (BackpressureError, EventBuffer, FramedSocket, PlainListener,
                        ProtocolError, Receiver, Sender, TLSListener, authenticate,
                        connect, fetch_fingerprint)

if TYPE_CHECKING:
    from .discovery import Advertiser
    from .windows import WindowsCapture


class _PeerLink:
    """One live client connection, bound to a named layout peer."""
    __slots__ = ("channel", "sender", "buffer", "name", "worker")

    def __init__(self, channel, name=""):
        self.channel = channel
        self.name = str(name or "")
        self.sender: Sender | None = None
        self.buffer: EventBuffer | None = None
        self.worker: threading.Thread | None = None


class HostSession:
    def __init__(self, settings: dict, status: Callable[[str], None] = print):
        self.settings, self.status = settings, status
        self.stopped = threading.Event()
        self.capture: WindowsCapture | None = None
        self.links: list[_PeerLink] = []
        self.active_link: _PeerLink | None = None
        self._links_lock = threading.Lock()
        self.sender: Sender | None = None
        self.channel: FramedSocket | None = None
        self.listener: TLSListener | PlainListener | None = None
        self.buffer: EventBuffer | None = None
        self.rtt_ms = 0.0
        self.thread: threading.Thread | None = None
        self._workers: list[threading.Thread] = []
        self.ready = threading.Event()
        self.remote_process: subprocess.Popen | None = None
        self._remote_lock = threading.Lock()
        self.clipboard: ClipboardServer | None = None
        self.advertiser: Advertiser | None = None
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
        if self.capture:
            self.capture.stop()
        if self.clipboard:
            self.clipboard.stop()
        if self.advertiser:
            self.advertiser.stop()
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
            self.status("Enter a valid user@host SSH target, or start the client manually")
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
                self.status("Client exited; see the local client log, then stop and reconnect")
        except Exception as exc:
            self.status(f"Cannot start client: {exc}")

    def _disconnect(self):
        for link in list(self.links):
            if link.sender:
                link.sender.stop()
            link.channel.close()
        if self.sender:
            self.sender.stop()
        if self.channel:
            self.channel.close()

    def _link_for(self, peer_name: str) -> _PeerLink | None:
        """Route to the named peer's link; an unnamed link claims the peer."""
        with self._links_lock:
            for link in self.links:
                if link.name == peer_name:
                    return link
            for link in self.links:
                if not link.name:
                    link.name = peer_name
                    return link
        return None

    def _emit(self, event):
        try:
            if event.get("type") == "activate":
                event = dict(event, side=event.get("side") or self.settings["side"],
                             gain=self.settings["gain"],
                             scroll_gain=self.settings["scroll_gain"],
                             swap_ctrl_cmd=self.settings["swap_ctrl_cmd"])
                for name in ("scroll_gain_x", "scroll_gain_y", "scroll_invert_x", "scroll_invert_y",
                             "scroll_mode", "keymap", "keymap_preset", "smooth_scroll",
                             "smooth_scroll_ms", "smooth_scroll_hz", "windows_home_end",
                             "windows_word_nav", "win_mouse_accel_off", "win_lock_on_return"):
                    if name in self.settings:
                        event[name] = self.settings[name]
                link = self._link_for(str(event.get("peer") or ""))
                if link is None or link.buffer is None:
                    self.status(f"No client connected for '{event.get('peer')}' — crossing ignored")
                    return False
                self.active_link = link
                link.buffer.put(event)
                return True
            target = self.active_link
            if event.get("type") in ("deactivate", "return"):
                self.active_link = None
            if target is not None and target.buffer is not None:
                target.buffer.put(event)
                return True
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
        if sys.platform == "darwin":
            from .mac_capture import MacCapture as Capture
        else:
            from .windows import WindowsCapture as Capture
        try:
            cert, key, token, fingerprint = identity()
            tls = bool(self.settings.get("tls", True))
            listener_t = TLSListener if tls else PlainListener
            listener = listener_t(str(cert), str(key), port=int(self.settings["port"]))
            self.listener = listener
            if self.settings.get("discovery", True):
                from .discovery import Advertiser
                self.advertiser = Advertiser(self.settings["port"],
                    {"tls": int(tls), "fp": fingerprint, "ver": __version__})
                try:
                    self.advertiser.start()
                except Exception as exc:
                    self.status(f"Network discovery unavailable: {type(exc).__name__}")
                    self.advertiser = None
            if self.settings.get("clipboard_enabled", False):
                self.clipboard = ClipboardServer(cert if tls else None, key if tls else None,
                    token, port=int(self.settings["port"]) + 2,
                    max_bytes=int(self.settings.get("clipboard_max_mb", 128)) * 1024 * 1024,
                    status=lambda message: self.status(f"Clipboard: {message}"))
                self.clipboard.start()
            peers = {name: (rect["x"], rect["y"], rect["w"], rect["h"])
                     for name, rect in (self.settings.get("peers") or {}).items()}
            capture_kwargs = dict(side=self.settings["side"],
                on_emergency=self.emergency_stop,
                lock_sync=self.settings.get("lock_sync", True),
                peer_align=self.settings.get("peer_align", 0.5),
                peer_span=self.settings.get("peer_span", 0.6),
                peers=peers or None)
            if sys.platform == "win32":
                capture_kwargs.update(high_resolution_timer=True,
                    mouse_mode=self.settings.get("mouse_mode", "raw"),
                    gesture_bridge=self.settings.get("gesture_bridge", True),
                    native_touchpad=self.settings.get("native_touchpad", False),
                    emergency_escape_seconds=self.settings.get("emergency_escape_seconds", 1.5),
                    mac_mode=self.settings.get("mac_mode", False))
            self.capture = Capture(self._emit, **capture_kwargs)
            self.capture.start()
            self.ready.set()
            self.status("Waiting for paired client; local input remains active")
            while not self.stopped.is_set():
                try:
                    channel = listener.accept()
                except socket.timeout:
                    continue
                except OSError as exc:
                    if self.stopped.is_set():
                        break
                    self.status(f"Connection rejected: {exc}")
                    continue
                try:
                    authenticate(channel, token, server=True)
                    ready = channel.recv()
                    if not isinstance(ready, dict) or ready.get("type") != "client_ready":
                        raise RuntimeError("Client has not granted Accessibility permission")
                    if self.stopped.is_set():
                        channel.close()
                        break
                    link = _PeerLink(channel, ready.get("name", ""))
                    link.buffer = EventBuffer(int(self.settings["mouse_hz"]))
                    link.sender = Sender(channel, link.buffer)
                    with self._links_lock:
                        self.links.append(link)
                    self.channel = link.channel
                    self.sender = link.sender
                    self.buffer = link.buffer
                    if self.capture:
                        self.capture.peer_connected = True
                    link.worker = threading.Thread(target=self._serve, args=(link,),
                                                   daemon=True, name="kvm-peer")
                    link.worker.start()
                    self._workers.append(link.worker)
                except Exception as exc:
                    channel.close()
                    if not self.stopped.is_set():
                        self.status(f"Connection rejected: {type(exc).__name__}: {exc}")
        except Exception as exc:
            if not self.stopped.is_set():
                self.status(f"Host stopped: {exc}")
        finally:
            self._disconnect()
            if self.capture:
                self.capture.stop()
                self.capture = None
            if self.clipboard:
                self.clipboard.stop()
            if self.advertiser:
                self.advertiser.stop()
                self.advertiser = None
            if self.listener:
                self.listener.close()

    def _serve(self, link: _PeerLink):
        """Serve one connected client until it drops; the accept loop is free
        to take more clients meanwhile."""
        writer = threading.Thread(target=self._writer, args=(link,), daemon=True,
                                  name="kvm-peer-writer")
        writer.start()
        self._workers.append(writer)
        self.status(f"Connected • {link.name or 'client'} — cross the configured edge to share")
        try:
            Receiver(link.channel, lambda event: self._control(link, event),
                     link.channel.close).run()
        except Exception as exc:
            if not self.stopped.is_set():
                self.status(f"Disconnected: {type(exc).__name__}: {exc}")
        finally:
            if self.active_link is link:
                self.active_link = None
                if self.capture:
                    self.capture.return_local()
            with self._links_lock:
                if link in self.links:
                    self.links.remove(link)
                still_connected = bool(self.links)
            if link.sender:
                link.sender.stop()
            link.channel.close()
            if self.capture and not still_connected:
                self.capture.peer_connected = False
                self.capture.release()
            writer.join(3)
            if writer in self._workers:
                self._workers.remove(writer)

    def _writer(self, link: _PeerLink):
        try:
            if link.sender:
                link.sender.run()
        except Exception:
            if self.capture:
                self.capture.return_local()

    def _control(self, link: _PeerLink, event):
        kind = event.get("type")
        if kind == "return" and self.capture:
            self.capture.return_local(event.get("normalized_y"), event.get("normalized_x"))
            if self.active_link is link:
                self.active_link = None
        elif kind == "hello":
            link.name = str(event.get("name") or link.name)
        elif kind == "ping" and link.buffer is not None:
            link.buffer.put({"type": "pong", "stamp": event["stamp"]})
        elif kind == "rtt":
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
            if config.get("auto"):
                _resolve_host(config, status)
            try:
                run_client(config, connect_timeout=0.8, stop_event=stop_event, status=status)
            except (OSError, EOFError, ProtocolError, BackpressureError) as exc:
                status(f"Server unavailable / disconnected ({type(exc).__name__}); retrying every second")
            if stop_event.wait(max(0.0, 1.0 - (time.monotonic() - started))):
                break
    finally:
        if clipboard:
            clipboard.stop()


def _resolve_host(config: dict, status) -> None:
    """Fill host/port from mDNS when auto-discovery is enabled."""
    try:
        from .discovery import browse
        services = browse(1.5)
    except Exception:
        return
    if not services:
        return
    service = services[0]
    config["host"], config["port"] = service["host"], service["port"]
    config["tls"] = service["tls"]
    status(f"Discovered {service['name']} at {service['host']}:{service['port']}")


def run_client(config: dict, connect_timeout=3, stop_event=None, status=print):
    if sys.platform == "darwin":
        from .mac import MacInjector as Injector
        if not Injector.accessibility_available(prompt=True):
            raise RuntimeError("Grant Accessibility access to this Python executable/Terminal in "
                               "System Settings > Privacy & Security > Accessibility, then reconnect")
    else:
        from .windows_inject import WindowsInjector as Injector
    stopped = threading.Event()
    buffer = EventBuffer(120)
    injector = Injector(on_return=buffer.put)
    channel = None
    sender = None
    workers = []
    try:
        tls = bool(config.get("tls", True))
        try:
            channel = connect(config["host"], int(config["port"]),
                              config.get("fingerprint", ""), tls=tls,
                              timeout=connect_timeout)
        except ssl.SSLError:
            approve = config.get("approve")
            if not approve:
                raise
            actual = fetch_fingerprint(config["host"], int(config["port"]), connect_timeout)
            if not approve(config.get("fingerprint") or None, actual):
                raise ProtocolError("Server certificate rejected")
            channel = connect(config["host"], int(config["port"]), actual,
                              tls=tls, timeout=connect_timeout)
        authenticate(channel, config["token"], server=False)
        if stop_event is not None and stop_event.is_set():
            return
        channel.send({"type": "client_ready", "version": 1})
        sender = Sender(channel, buffer)
        buffer.put({"type": "hello", "name": config.get("name") or socket.gethostname()})

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
                               "windows_home_end", "windows_word_nav",
                               "win_mouse_accel_off", "win_lock_on_return")
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
