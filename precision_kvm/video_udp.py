"""Parsec-style video plane: hardware H.264 encode + UDP transport.

The TCP screen channel stays for authentication, config negotiation and the
JPEG fallback. When both ends can run ffmpeg the video itself moves to a
MPEG-TS/H.264 stream over UDP — no head-of-line blocking, hardware encode on
the sender, low-delay decode on the receiver. This mirrors how Parsec builds
its pipeline (HW H.264 + datagram transport) using off-the-shelf ffmpeg.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Callable

_FF_CANDIDATES = (
    "ffmpeg",                       # PATH
    "/opt/homebrew/bin/ffmpeg",     # Apple Silicon Homebrew
    "/usr/local/bin/ffmpeg",        # Intel Homebrew
    "/usr/bin/ffmpeg",
)


def ffmpeg_path() -> str | None:
    """First ffmpeg binary that exists — Homebrew is not on every PATH."""
    for candidate in _FF_CANDIDATES:
        found = shutil.which(candidate) if "/" not in candidate else (
            candidate if os.path.exists(candidate) else None)
        if found:
            return found
    return None


def can_encode() -> bool:
    """The client can offer H.264-over-UDP on this machine."""
    return ffmpeg_path() is not None and _capture_args() is not None


def can_decode() -> bool:
    return ffmpeg_path() is not None


def _capture_args() -> list[str] | None:
    """Platform screen-capture input options (Parsec: zero-copy OS capture)."""
    if sys.platform == "darwin":
        # AVFoundation screen device; capture_cursor composites the pointer in.
        return ["-f", "avfoundation", "-i", "0:"]
    if sys.platform == "win32":
        return ["-f", "gdigrab", "-i", "desktop"]
    if os.environ.get("WAYLAND_DISPLAY"):
        # x11grab under Wayland only sees the XWayland root (black/nested),
        # not the desktop — PipeWire capture is a later addition; JPEG wins.
        return None
    display = os.environ.get("DISPLAY")
    if display:
        return ["-f", "x11grab", "-i", display]
    return None


def encoder_command(cfg: dict, host: str, port: int, ffmpeg: str) -> list[str]:
    """Capture → HW H.264/HEVC → MPEG-TS over UDP to host:port.

    Low-latency flags follow Parsec's approach: realtime HW encoder priority,
    short GOP, resend_headers so mid-stream joins/losses resync quickly.
    """
    fps = max(1, int(cfg.get("fps", 30)))
    kbps = max(500, int(cfg.get("kbps", 8000)))
    codec = str(cfg.get("codec", "h264"))
    gop = int(cfg.get("gop") or 0) or max(fps // 2, 15)
    scale = min(1.0, max(0.1, float(cfg.get("scale", 1.0) or 1.0)))
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning"]
    capture = list(_capture_args() or [])
    if sys.platform == "darwin":
        # AVFoundation screen input only offers uyvy422/yuyv422/nv12 —
        # the default yuv420p makes the demuxer fail to open.
        cmd += ["-framerate", str(fps), "-capture_cursor",
                "1" if cfg.get("cursor", True) else "0",
                "-pixel_format", "nv12"]
    else:
        cmd += ["-framerate", str(fps)]
    cmd += capture
    # Downscale before encoding — cheaper encode and less bandwidth; the
    # viewer upscales to its panel so the user sees a full-size feed anyway.
    filters = [f"scale=iw*{scale:.3g}:ih*{scale:.3g}"] if scale < 1.0 else []
    if sys.platform == "darwin":
        vcodec = "hevc_videotoolbox" if codec == "hevc" else "h264_videotoolbox"
        cmd += ["-c:v", vcodec,
                "-realtime", "1", "-prio_speed", "1", "-allow_sw", "0"]
        if filters:
            cmd += ["-vf", ",".join(filters)]
    elif os.environ.get("DISPLAY") and os.path.exists("/dev/dri/renderD128"):
        vcodec = "hevc_vaapi" if codec == "hevc" else "h264_vaapi"
        filters = [f"scale=iw*{scale:.3g}:ih*{scale:.3g}"] if scale < 1.0 else []
        cmd += ["-vaapi_device", "/dev/dri/renderD128",
                "-vf", ",".join([*filters, "format=nv12,hwupload"]),
                "-c:v", vcodec]
    else:
        vcodec = "libx265" if codec == "hevc" else "libx264"
        cmd += ["-c:v", vcodec, "-preset", "ultrafast", "-tune", "zerolatency"]
        if filters:
            cmd += ["-vf", ",".join(filters)]
    cmd += ["-g", str(gop), "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k",
            "-bufsize", f"{max(kbps // 2, 500)}k",
            "-f", "mpegts", "-mpegts_flags", "+resend_headers",
            f"udp://{host}:{port}"]
    return cmd


def decoder_command(port: int, ffmpeg: str) -> list[str]:
    """UDP MPEG-TS in → raw PPM frames on stdout for the viewer."""
    return [ffmpeg, "-hide_banner", "-loglevel", "error",
            "-fflags", "nobuffer", "-flags", "low_delay",
            "-probesize", "32768", "-analyzeduration", "0",
            "-i", f"udp://0.0.0.0:{port}?fifo_size=1000000&overrun_nonfatal=1",
            "-f", "image2pipe", "-vcodec", "ppm", "-"]


def read_ppm(stream) -> tuple[int, int, bytes] | None:
    """Parse one P6 frame from an image2pipe stream; None on EOF/garbage."""
    magic = stream.readline()
    if not magic or magic[:2] != b"P6":
        return None

    def token():
        # Skip whitespace and '#' comments between header fields.
        buf = bytearray()
        while True:
            ch = stream.read(1)
            if not ch:
                return None
            if ch == b"#":
                stream.readline()
                continue
            if ch.isspace():
                if buf:
                    return bytes(buf)
                continue
            buf += ch

    try:
        width, height, maxval = int(token()), int(token()), int(token())
    except (TypeError, ValueError):
        return None
    if maxval != 255 or not (0 < width <= 16384 and 0 < height <= 16384):
        return None
    size = width * height * 3
    data = stream.read(size)
    return (width, height, data) if len(data) == size else None


def launch(command: list[str], stderr_target=None) -> subprocess.Popen | None:
    try:
        return subprocess.Popen(command, stdout=subprocess.PIPE,
                                stderr=stderr_target or subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL)
    except OSError:
        return None


def reap(proc: subprocess.Popen | None) -> None:
    """Terminate without inheriting zombie children."""
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(2)


__all__ = ["ffmpeg_path", "can_encode", "can_decode", "encoder_command",
           "decoder_command", "read_ppm", "launch", "reap", "Callable"]
