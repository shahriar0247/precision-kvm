from __future__ import annotations

import socket
import sys

from precision_kvm.screenshare import (DEFAULT_CFG, SCREEN_MAX_FRAME, _Bucket,
                                     jpeg_decode, jpeg_encode, normalize_cfg)
from precision_kvm.transport import FramedSocket


def test_normalize_cfg_clamps_ranges():
    cfg = normalize_cfg({"fps": 500, "quality": 200, "scale": 3.0,
                         "kbps": -5, "monitor": -2, "gray": 1})
    assert cfg["fps"] == 60 and cfg["quality"] == 95
    assert cfg["scale"] == 1.0 and cfg["kbps"] == 0 and cfg["monitor"] == 0
    assert cfg["gray"] is True
    assert normalize_cfg({}) == DEFAULT_CFG


def test_normalize_cfg_accepts_percent_strings():
    """UI comboboxes persist '75%' — normalize_cfg must not crash."""
    cfg = normalize_cfg({"scale": "75%", "fps": "30", "kbps": "junk"})
    assert cfg["scale"] == 0.75 and cfg["fps"] == 30
    assert cfg["kbps"] == DEFAULT_CFG["kbps"]


def test_jpeg_roundtrip_and_gray():
    from PIL import Image
    image = Image.new("RGB", (320, 200), (120, 40, 200))
    color = jpeg_encode(image, normalize_cfg({"quality": 50}))
    gray = jpeg_encode(image, normalize_cfg({"gray": True, "scale": 0.5}))
    assert jpeg_decode(color).size == (320, 200)
    assert jpeg_decode(gray).mode == "L" and jpeg_decode(gray).size == (160, 100)
    assert len(gray) < len(color)


def test_bucket_enforces_bitrate():
    bucket = _Bucket()
    assert bucket.allow(1000, 8)          # 8 kbps = 1000 B/s
    assert not bucket.allow(1, 8)         # budget exhausted this second
    assert bucket.allow(10_000_000, 0)    # zero = unlimited


def test_binary_frames_roundtrip():
    left, right = socket.socketpair()
    try:
        a, b = FramedSocket(left, SCREEN_MAX_FRAME), FramedSocket(right, SCREEN_MAX_FRAME)
        a.send_binary(b"\xff\xd8jpeg-bytes\xff\xd9")
        a.send({"type": "screen_cfg", "fps": 5})
        assert b.recv_binary() == b"\xff\xd8jpeg-bytes\xff\xd9"
        assert b.recv()["fps"] == 5
    finally:
        left.close()
        right.close()


def test_binary_over_limit_rejected():
    import pytest
    left, right = socket.socketpair()
    try:
        a = FramedSocket(left)
        with pytest.raises(Exception):
            a.send_binary(b"y" * (64 * 1024 + 1))
    finally:
        left.close()
        right.close()


def test_video_udp_encoder_command_hw_flags():
    """The H.264 pipeline must request realtime HW encode + resend headers —
    the two flags Parsec-style low latency depends on."""
    from precision_kvm import video_udp
    cfg = {"fps": 60, "kbps": 12000, "cursor": True}
    cmd = video_udp.encoder_command(cfg, "192.168.50.1", 24823, "/usr/bin/ffmpeg")
    assert cmd[0] == "/usr/bin/ffmpeg"
    assert "udp://192.168.50.1:24823" in cmd
    assert "-mpegts_flags" in cmd and "+resend_headers" in cmd
    if sys.platform == "darwin":
        assert "h264_videotoolbox" in cmd and "-realtime" in cmd
    else:
        assert any(enc in cmd for enc in
                   ("h264_vaapi", "h264_nvenc", "h264_qsv", "libx264"))


def test_video_udp_decoder_command_low_delay():
    from precision_kvm import video_udp
    cmd = video_udp.decoder_command(24823, "/usr/bin/ffmpeg")
    joined = " ".join(cmd)
    assert "nobuffer" in joined and "low_delay" in joined
    assert "udp://0.0.0.0:24823" in joined and "image2pipe" in joined


def test_read_ppm_parses_p6_stream():
    import io
    from precision_kvm import video_udp
    payload = b"P6\n4 2\n255\n" + bytes(range(24))
    frame = video_udp.read_ppm(io.BytesIO(payload))
    assert frame == (4, 2, bytes(range(24)))
    assert video_udp.read_ppm(io.BytesIO(b"")) is None
    assert video_udp.read_ppm(io.BytesIO(b"P5\n4 2\n255\n" + b"x" * 8)) is None
