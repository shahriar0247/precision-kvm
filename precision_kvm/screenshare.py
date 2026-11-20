"""View-only screen streaming over a dedicated token+TLS channel.

The client captures JPEG frames with mss and pushes them to the host
(binary frames, one direction). The host pushes JSON control messages
back (quality, fps, scale, gray, bitrate cap, monitor, enabled). The
stream is display-only: input still crosses at the screen edge, never
through the video view.
"""
from __future__ import annotations

import io
import re
import socket
import sys
import tempfile
import threading
import time
from collections import deque
from typing import Callable

from .config import state_dir
from .transport import (FramedSocket, ProtocolError, TLSListener, PlainListener,
                        authenticate, connect)
from . import video_udp

SCREEN_PORT_OFFSET = 4
SCREEN_MAX_FRAME = 8 * 1024 * 1024
DEFAULT_CFG = {"enabled": False, "fps": 15, "quality": 60, "scale": 1.0,
               "gray": False, "kbps": 8000, "monitor": 0, "cursor": True,
               "codec": "h264", "gop": 0, "cap_res": "native",
               "preset": "ultrafast", "viewer_w": 0, "viewer_h": 0}

_CAP_RES = re.compile(r"(native|viewer|\d{3,4}|\d{2,5}x\d{2,5})")
_ENCODER_PRESETS = ("ultrafast", "superfast", "veryfast", "faster",
                    "fast", "medium")

# Classic pointer arrow, ~11×18 px, composited over the feed.
CURSOR_POLY = ((0, 0), (0, 16), (4, 12), (7, 18), (9, 17), (6, 11), (11, 11))


def _cursor_pos() -> tuple[int, int] | None:
    """OS cursor position in virtual-screen pixels (the framebuffer has no
    cursor in it — mss needs a composite)."""
    import sys
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            point = wintypes.POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
                return point.x, point.y
        elif sys.platform == "darwin":
            import Quartz  # type: ignore
            location = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            return round(location.x), round(location.y)
    except Exception:
        pass
    return None


def _draw_cursor(image, sct, monitor: int, scale: float) -> None:
    """Composite the local pointer glyph — scaled so its on-screen size
    stays constant regardless of stream resolution."""
    pos = _cursor_pos()
    if pos is None:
        return
    region = sct.monitors[min(monitor, len(sct.monitors) - 1)]
    x, y = pos[0] - region["left"], pos[1] - region["top"]
    if not (0 <= x < image.width and 0 <= y < image.height):
        return
    from PIL import ImageDraw
    size = 1.0 / max(0.1, scale)
    draw = ImageDraw.Draw(image)
    outline = [(x + px * size, y + py * size) for px, py in CURSOR_POLY]
    draw.polygon(outline, fill=(255, 255, 255), outline=(0, 0, 0))


def _num(value, default: float) -> float:
    """Coerce UI/persisted values — tolerates '75%' strings and junk."""
    if isinstance(value, str) and value.strip().endswith("%"):
        try:
            return float(value.strip().rstrip("%")) / 100
        except ValueError:
            return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_cfg(values: dict) -> dict:
    """Clamp untrusted/UI control values into the valid ranges."""
    cfg = dict(DEFAULT_CFG)
    cfg.update({k: v for k, v in (values or {}).items() if k in DEFAULT_CFG})
    cfg["enabled"] = bool(cfg["enabled"])
    cfg["gray"] = bool(cfg["gray"])
    cfg["cursor"] = bool(cfg["cursor"])
    cfg["fps"] = min(60, max(1, int(_num(cfg["fps"], DEFAULT_CFG["fps"]))))
    cfg["quality"] = min(95, max(10, int(_num(cfg["quality"], DEFAULT_CFG["quality"]))))
    cfg["scale"] = min(1.0, max(0.1, _num(cfg["scale"], DEFAULT_CFG["scale"])))
    cfg["kbps"] = min(200_000, max(0, int(_num(cfg["kbps"], DEFAULT_CFG["kbps"]))))
    cfg["monitor"] = max(0, int(_num(cfg["monitor"], DEFAULT_CFG["monitor"])))
    cfg["codec"] = cfg["codec"] if str(cfg["codec"]) in ("h264", "hevc") else "h264"
    cfg["gop"] = min(600, max(0, int(_num(cfg["gop"], DEFAULT_CFG["gop"]))))
    res = str(cfg["cap_res"])
    cfg["cap_res"] = res if _CAP_RES.fullmatch(res) else "native"
    cfg["preset"] = (cfg["preset"] if str(cfg["preset"]) in _ENCODER_PRESETS
                     else "ultrafast")
    cfg["viewer_w"] = min(16384, max(0, int(_num(cfg["viewer_w"], 0))))
    cfg["viewer_h"] = min(16384, max(0, int(_num(cfg["viewer_h"], 0))))
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
        self._decoder = None          # ffmpeg subprocess while UDP plane is live
        self._transport = "—"         # "UDP/H.264" | "JPEG/TCP" once frames flow
        self._last_frame_at = 0.0

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
        video_udp.reap(self._decoder)
        self._decoder = None
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
                 "connected": self.client_connected,
                 "transport": self._transport,
                 "age_ms": (time.monotonic() - self._last_frame_at) * 1000
                 if self._last_frame_at else None}
        self._new.clear()
        with self._frame_lock:
            if self._latest is None:
                return None, stats
            image, info = self._latest
            self._latest = None
        stats.update(info)
        stats["age_ms"] = (time.monotonic() - self._last_frame_at) * 1000
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
                    offer_udp = video_udp.can_decode()
                    self.channel.send({"type": "screen_cfg", **self.cfg,
                                       "video_udp": offer_udp,
                                       "udp_port": self.port})
                    self.status("Screen share client connected")
                    self._serve(self.channel, udp=offer_udp)
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

    def _serve(self, channel: FramedSocket, udp: bool = False):
        window_bytes = 0
        window_start = time.monotonic()
        if udp:
            threading.Thread(target=self._udp_decode, daemon=True,
                             name="kvm-screen-dec").start()
        try:
            while not self.stopped.is_set():
                try:
                    payload = channel.recv_binary()
                except socket.timeout:
                    continue  # idle stream — the socket deadline is not a drop
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
                self._transport = "JPEG/TCP"
                self._last_frame_at = now
                with self._frame_lock:
                    self._latest = (image, {"size": (image.width, image.height),
                                            "mbps": mbps, "fps": self._fps})
                self._new.set()
        finally:
            video_udp.reap(self._decoder)
            self._decoder = None

    def _udp_decode(self):
        """ffmpeg demuxes UDP MPEG-TS → PPM frames → the viewer's poll_frame."""
        ffmpeg = video_udp.ffmpeg_path()
        if not ffmpeg:
            return
        with tempfile.TemporaryFile() as errlog:
            proc = video_udp.launch(
                video_udp.decoder_command(self.port, ffmpeg), errlog)
            self._decoder = proc
            if proc is None or proc.stdout is None:
                return
            while not self.stopped.is_set() and proc.poll() is None:
                frame = video_udp.read_ppm(proc.stdout)
                if frame is None:
                    continue
                width, height, data = frame
                from PIL import Image
                image = Image.frombytes("RGB", (width, height), data)
                now = time.monotonic()
                self._received += 1
                self._fps_count += 1
                if now - self._fps_start >= 1.0:
                    self._fps = self._fps_count / max(0.001, now - self._fps_start)
                    self._fps_count = 0
                    self._fps_start = now
                self._transport = f"UDP/{self.cfg['codec'].upper()}"
                self._last_frame_at = now
                with self._frame_lock:
                    self._latest = (image, {"size": (width, height),
                                            "mbps": 0.0, "fps": self._fps})
                self._new.set()


class ScreenShareClient:
    """Client side: connects to the host and streams the local screen
    only while the host asks for it — the OS permission prompt appears
    on first use, not on pairing."""

    def __init__(self, config: dict, status: Callable[[str], None] = print):
        # Shared reference: discovery re-resolves must reach this retry loop.
        self.config, self.status = config, status
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None
        self.channel: FramedSocket | None = None
        self._cfg = normalize_cfg({})
        self._cfg_lock = threading.Lock()
        self._cfg_new = threading.Event()
        self._udp_offer: int | None = None   # UDP port the server listens on
        self._encoder = None                 # ffmpeg subprocess while streaming
        self._encoder_err = None             # stderr sink for ffmpeg diagnostics
        self._tcc_asked = False              # requested Screen Recording once
        self._tcc_denied = False             # last preflight state
        self._tcc_msg_t = 0.0                # throttled "grant needed" status
        self._sct = None                     # mss instance for in-process capture

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stopped.clear()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="kvm-screen-client")
        self.thread.start()

    def stop(self):
        self.stopped.set()
        video_udp.reap(self._encoder)
        self._encoder = None
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None
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

    def _encoder_log(self):
        """ffmpeg stderr — TCC/demux errors must be diagnosable, not devnull."""
        if self._encoder_err is None:
            try:
                self._encoder_err = (state_dir() / "screen-encoder.log").open("ab")
            except OSError:
                self._encoder_err = None
        return self._encoder_err

    def _capture_allowed(self) -> bool:
        """darwin Screen-Recording preflight in THIS process — the context TCC
        actually evaluates for a launchd client's children. First denial issues
        the real prompt so Settings gains the correct identity to toggle."""
        if sys.platform != "darwin":
            return True
        try:
            import Quartz  # type: ignore
        except Exception:
            return True  # can't check — let capture try and fail normally
        ok = bool(Quartz.CGPreflightScreenCaptureAccess())
        if ok:
            if self._tcc_denied:
                # Grant landed after an encoder was spawned: the parked process
                # stays denied forever, so force a fresh spawn under the grant.
                self._tcc_denied = False
                video_udp.reap(self._encoder)
                self._encoder = None
            return True
        self._tcc_denied = True
        if not self._tcc_asked:
            self._tcc_asked = True
            try:
                Quartz.CGRequestScreenCaptureAccess()
            except Exception:
                pass
        return False

    def _darwin_udp_stream(self, cfg: dict) -> None:
        """darwin UDP path — capture in-process and let ffmpeg encode-only.

        mss (CGWindowList) runs under THIS client's granted TCC identity;
        ffmpeg's own avfoundation capture is denied because modern macOS
        evaluates Screen Recording per binary, and a launchd-spawned encoder
        never gets a visible prompt to register its entry.
        """
        if self._sct is None:
            try:
                import mss
                self._sct = mss.mss()
            except Exception as exc:
                self.status(f"Screen: capture init failed: {exc}")
                self.stopped.wait(2.0)
                return
        mons = self._sct.monitors
        idx = min(max(1, int(cfg["monitor"]) + 1), len(mons) - 1)
        mon = mons[idx]
        ffmpeg = video_udp.ffmpeg_path()
        if not ffmpeg:
            self.stopped.wait(2.0)
            return
        cmd = video_udp.encoder_command(
            cfg, self.config["host"], self._udp_offer, ffmpeg,
            src_size=(mon["width"], mon["height"]))
        proc = video_udp.launch(cmd, self._encoder_log(), stdin_pipe=True)
        self._encoder = proc
        if proc is None or proc.stdin is None:
            self.status("Screen: hardware encoder failed to start")
            self.stopped.wait(2.0)
            return
        self.status("Screen: streaming H.264 over UDP (in-process capture)")
        interval = 1.0 / max(1, int(cfg["fps"]))
        while not self.stopped.is_set() and not self._cfg_new.is_set():
            started = time.monotonic()
            try:
                stdin = proc.stdin
                assert stdin is not None
                stdin.write(self._sct.grab(mon).bgra)
                stdin.flush()
            except (BrokenPipeError, OSError):
                break
            except Exception as exc:
                self.status(f"Screen: capture failed: {type(exc).__name__}")
                self.stopped.wait(1.0)
                continue
            if proc.poll() is not None:
                break
            self._cfg_new.wait(max(0.0, interval - (time.monotonic() - started)))
        video_udp.reap(proc)
        self._encoder = None

    def _controls(self, channel: FramedSocket):
        """Reader for host→client JSON control messages."""
        try:
            while not self.stopped.is_set():
                try:
                    message = channel.recv()
                except socket.timeout:
                    continue  # quiet control channel — not a disconnect
                if message.get("type") == "screen_cfg":
                    if message.get("video_udp") and message.get("udp_port"):
                        self._udp_offer = int(message["udp_port"])
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
                    video_udp.reap(self._encoder)
                    self._encoder = None
                    if sct is not None:
                        sct.close()
                        sct = None
                    self._cfg_new.wait(0.5)
                    self._cfg_new.clear()
                    continue
                if not self._capture_allowed():
                    # A TCC-parked encoder stays "alive" at 0% CPU forever —
                    # report the real blocker instead of silently spawning.
                    video_udp.reap(self._encoder)
                    self._encoder = None
                    if time.monotonic() - self._tcc_msg_t > 15:
                        self._tcc_msg_t = time.monotonic()
                        self.status("Screen: needs Screen Recording permission — "
                                    "enable this app in System Settings > "
                                    "Privacy & Security > Screen & System Audio Recording")
                    self.stopped.wait(2.0)
                    continue
                if self._udp_offer and sys.platform == "darwin":
                    self._darwin_udp_stream(cfg)
                    continue
                if self._udp_offer and video_udp.can_encode():
                    # HW-encode path — Parsec-style H.264 over UDP; ffmpeg does
                    # the capture+encode+datagram work in one subprocess.
                    if self._encoder is None or self._encoder.poll() is not None:
                        video_udp.reap(self._encoder)
                        ffmpeg = video_udp.ffmpeg_path()
                        cmd = video_udp.encoder_command(
                            cfg, self.config["host"], self._udp_offer, ffmpeg)
                        self._encoder = video_udp.launch(
                            cmd, self._encoder_log())
                        if self._encoder is None:
                            self.status("Screen: hardware encoder failed to start")
                            self.stopped.wait(2.0)
                        else:
                            self.status("Screen: streaming H.264 over UDP (hardware encode)")
                    # Any control change restarts the encoder with new opts.
                    if self._cfg_new.wait(0.5):
                        video_udp.reap(self._encoder)
                        self._encoder = None
                    self._cfg_new.clear()
                    continue
                if sct is None:
                    import mss
                    self._preflight()
                    sct = mss.mss()
                frame_start = time.monotonic()
                try:
                    image = _grab(sct, cfg["monitor"])
                    if cfg["cursor"]:
                        _draw_cursor(image, sct, cfg["monitor"], cfg["scale"])
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
