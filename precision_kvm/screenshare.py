"""View-only screen streaming over a dedicated token+TLS channel.

The client captures JPEG frames with mss and pushes them to the host
(binary frames, one direction). The host pushes JSON control messages
back (quality, fps, scale, gray, bitrate cap, monitor, enabled). The
stream is display-only: input still crosses at the screen edge, never
through the video view.
"""
from __future__ import annotations

import io
import socket
import threading
import time
from collections import deque
from typing import Callable

from .transport import (FramedSocket, ProtocolError, TLSListener, PlainListener,
                        authenticate, connect)

SCREEN_PORT_OFFSET = 4
SCREEN_MAX_FRAME = 8 * 1024 * 1024
DEFAULT_CFG = {"enabled": False, "fps": 15, "quality": 60, "scale": 1.0,
               "gray": False, "kbps": 8000, "monitor": 0}


def normalize_cfg(values: dict) -> dict:
    """Clamp untrusted/UI control values into the valid ranges."""
    cfg = dict(DEFAULT_CFG)
    cfg.update({k: v for k, v in (values or {}).items() if k in DEFAULT_CFG})
    cfg["enabled"] = bool(cfg["enabled"])
    cfg["gray"] = bool(cfg["gray"])
    cfg["fps"] = min(60, max(1, int(cfg["fps"])))
    cfg["quality"] = min(95, max(10, int(cfg["quality"])))
    cfg["scale"] = min(1.0, max(0.1, float(cfg["scale"])))
    cfg["kbps"] = min(200_000, max(0, int(cfg["kbps"])))
    cfg["monitor"] = max(0, int(cfg["monitor"]))
    return cfg


def jpeg_encode(image, cfg: dict) -> bytes:
    """PIL image → JPEG bytes honoring scale/gray/quality."""
    from PIL import Image
    if cfg["scale"] != 1.0:
        image = image.resize((max(1, round(image.width * cfg["scale"])),
                              max(1, round(image.height * cfg["scale"]))),
                             Image.Resampling.BILINEAR)
    if cfg["gray"]:
        image = image.convert("L")
    elif image.mode != "RGB":
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=cfg["quality"])
    return buffer.getvalue()


def jpeg_decode(payload: bytes):
    from PIL import Image
    return Image.open(io.BytesIO(payload))


class _Bucket:
    """Rolling one-second window byte budget; zero kbps means unlimited."""
    def __init__(self):
        self.events: deque[tuple[float, int]] = deque()
        self.used = 0

    def allow(self, nbytes: int, kbps: int) -> bool:
        if not kbps:
            return True
        now = time.monotonic()
        while self.events and now - self.events[0][0] > 1.0:
            self.used -= self.events.popleft()[1]
        if self.used + nbytes > kbps * 125:
            return False
        self.events.append((now, nbytes))
        self.used += nbytes
        return True


def _grab(sct, monitor: int):
    """One mss frame as a PIL image; index 0 spans the whole desktop."""
    from PIL import Image
    monitors = sct.monitors
    region = monitors[min(monitor, len(monitors) - 1)]
    shot = sct.grab(region)
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


class ScreenShareServer:
    """Host side: accepts the client's screen channel and exposes frames."""

    def __init__(self, cert, key, token, port: int,
                 cfg: dict | None = None,
                 status: Callable[[str], None] = print):
        self.cert, self.key, self.token, self.port = cert, key, token, int(port)
        self.cfg = normalize_cfg(cfg or {})
        self.status = status
        self.stopped = threading.Event()
        self.listener: TLSListener | PlainListener | None = None
        self.channel: FramedSocket | None = None
        self.thread: threading.Thread | None = None
        self._frame_lock = threading.Lock()
        self._latest: tuple | None = None   # (PIL.Image, dict stats)
        self._new = threading.Event()
        self._received = 0
        self._bytes = 0
        self._fps_start = 0.0
        self._fps_count = 0
        self._fps = 0.0

    @property
    def client_connected(self) -> bool:
        return self.channel is not None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stopped.clear()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="kvm-screen-server")
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.channel:
            self.channel.close()
        if self.listener:
            self.listener.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(5)

    def set_config(self, cfg: dict):
        """Live-apply controls; also pushed to the next client that connects."""
        self.cfg = normalize_cfg({**self.cfg, **cfg})
        channel = self.channel
        if channel:
            try:
                channel.send({"type": "screen_cfg", **self.cfg})
            except (OSError, EOFError, ProtocolError):
                pass

    def poll_frame(self):
        """Return (image, stats) once per new frame, else (None, stats)."""
        stats = {"fps": self._fps, "mbps": 0.0, "size": None,
                 "connected": self.client_connected}
        self._new.clear()
        with self._frame_lock:
            if self._latest is None:
                return None, stats
            image, info = self._latest
            self._latest = None
        stats.update(info)
        return image, stats

    def _run(self):
        try:
            if self.cert and self.key:
                self.listener = TLSListener(str(self.cert), str(self.key),
                                            port=self.port)
            else:
                self.listener = PlainListener(port=self.port)
            while not self.stopped.is_set():
                try:
                    self.channel = self.listener.accept()
                    self.channel.max_frame = SCREEN_MAX_FRAME
                    authenticate(self.channel, self.token, server=True)
                    self.channel.send({"type": "screen_cfg", **self.cfg})
                    self.status("Screen share client connected")
                    self._serve(self.channel)
                except socket.timeout:
                    continue
                except (OSError, EOFError, ProtocolError, ValueError):
                    if not self.stopped.is_set():
                        self.status("Screen share channel dropped; waiting for client")
                finally:
                    if self.channel:
                        self.channel.close()
                    self.channel = None
        except Exception as exc:
            if not self.stopped.is_set():
                self.status(f"Screen share unavailable: {type(exc).__name__}")
        finally:
            if self.listener:
                self.listener.close()

    def _serve(self, channel: FramedSocket):
        window_bytes = 0
        window_start = time.monotonic()
        while not self.stopped.is_set():
            payload = channel.recv_binary()
            image = jpeg_decode(payload)
            now = time.monotonic()
            self._received += 1
            window_bytes += len(payload)
            if now - self._fps_start >= 1.0:
                self._fps = self._fps_count / max(0.001, now - self._fps_start)
                self._fps_count = 0
                self._fps_start = now
                window_bytes = 0
                window_start = now
            self._fps_count += 1
            mbps = window_bytes * 8 / 1e6 / max(0.001, now - window_start)
            with self._frame_lock:
                self._latest = (image, {"size": (image.width, image.height),
                                        "mbps": mbps, "fps": self._fps})
            self._new.set()


class ScreenShareClient:
    """Client side: connects to the host and streams the local screen
    only while the host asks for it — the OS permission prompt appears
    on first use, not on pairing."""

    def __init__(self, config: dict, status: Callable[[str], None] = print):
        self.config, self.status = dict(config), status
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None
        self.channel: FramedSocket | None = None
        self._cfg = normalize_cfg({})
        self._cfg_lock = threading.Lock()
        self._cfg_new = threading.Event()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stopped.clear()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="kvm-screen-client")
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.channel:
            self.channel.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(5)

    def _run(self):
        try:
            import mss  # noqa: F401 — presence check; capture starts lazily
        except ImportError:
            self.status("Screen share needs the 'mss' package on the client")
            return
        while not self.stopped.is_set():
            started = time.monotonic()
            try:
                self.channel = connect(
                    self.config["host"],
                    int(self.config.get("screen_port",
                                        int(self.config["port"]) + SCREEN_PORT_OFFSET)),
                    self.config.get("fingerprint", ""),
                    tls=bool(self.config.get("tls", True)), timeout=0.8)
                self.channel.max_frame = SCREEN_MAX_FRAME
                authenticate(self.channel, self.config["token"], server=False)
                self._session(self.channel)
            except (OSError, EOFError, ProtocolError, ValueError):
                pass
            finally:
                if self.channel:
                    self.channel.close()
                self.channel = None
            self.stopped.wait(max(0.0, 1.0 - (time.monotonic() - started)))

    def _controls(self, channel: FramedSocket):
        """Reader for host→client JSON control messages."""
        try:
            while not self.stopped.is_set():
                message = channel.recv()
                if message.get("type") == "screen_cfg":
                    with self._cfg_lock:
                        self._cfg = normalize_cfg(
                            {k: v for k, v in message.items()})
                    self._cfg_new.set()
        except (OSError, EOFError, ProtocolError):
            pass
        finally:
            channel.close()

    def _session(self, channel: FramedSocket):
        reader = threading.Thread(target=self._controls, args=(channel,),
                                  daemon=True, name="kvm-screen-ctl")
        reader.start()
        self.status("Screen share connected — waiting for the host to stream")
        sct = None
        bucket = _Bucket()
        try:
            while not self.stopped.is_set() and reader.is_alive():
                with self._cfg_lock:
                    cfg = dict(self._cfg)
                if not cfg["enabled"]:
                    if sct is not None:
                        sct.close()
                        sct = None
                    self._cfg_new.wait(0.5)
                    self._cfg_new.clear()
                    continue
                if sct is None:
                    import mss
                    self._preflight()
                    sct = mss.mss()
                frame_start = time.monotonic()
                try:
                    image = _grab(sct, cfg["monitor"])
                    payload = jpeg_encode(image, cfg)
                    if bucket.allow(len(payload), cfg["kbps"]):
                        channel.send_binary(payload)
                except (OSError, EOFError, ProtocolError):
                    break
                except Exception as exc:
                    self.status(f"Capture failed: {type(exc).__name__}")
                    self.stopped.wait(1.0)
                    continue
                delay = 1.0 / cfg["fps"] - (time.monotonic() - frame_start)
                if delay > 0:
                    self.stopped.wait(delay)
        finally:
            if sct is not None:
                sct.close()

    def _preflight(self):
        """macOS Screen Recording: request once; the grant persists."""
        import sys
        if sys.platform != "darwin":
            return
        try:
            import Quartz  # type: ignore
            if not Quartz.CGPreflightScreenCaptureAccess():
                Quartz.CGRequestScreenCaptureAccess()
                self.status("Grant Screen Recording on the client when macOS asks")
        except Exception:
            pass
