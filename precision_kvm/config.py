"""Machine-local settings and identity; never stored in the source tree."""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import re
from pathlib import Path
import secrets
import socket
import ssl
import sys


def state_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".config"))
    path = base / "PrecisionKVM"
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        path.chmod(0o700)
    return path


def save_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    if sys.platform != "win32":
        temporary.chmod(0o600)
    temporary.replace(path)


def load_settings() -> dict:
    path = state_dir() / "settings.json"
    settings = {"mouse_hz": 120, "side": "left", "port": 24819,
                "peer": "10.0.0.189", "gain": 1.0, "scroll_gain": 1.0,
                "swap_ctrl_cmd": False, "ssh_target": "", "mouse_mode": "raw",
                "scroll_gain_x": 1.0, "scroll_gain_y": 1.0,
                "scroll_invert_x": False, "scroll_invert_y": False, "scroll_mode": "pixel",
                "keymap_preset": "option_cmd", "keymap": {}, "gesture_bridge": True,
                "native_touchpad": False,
                "clipboard_enabled": True, "clipboard_max_mb": 128,
                "mac_mode": False, "lock_sync": True,
                "peer_align": 0.5, "peer_span": 0.6,
                "peers": {}, "peer_size": [1920, 1080],
                "tls": True, "discovery": True, "role": "host",
                "emergency_escape_seconds": 1.5, "smooth_scroll": True,
                "smooth_scroll_ms": 60, "smooth_scroll_hz": 120,
                "windows_home_end": True, "windows_word_nav": True}
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        settings.update(saved)
        if "keymap_preset" not in saved and saved.get("swap_ctrl_cmd"):
            settings["keymap_preset"] = "ctrl_cmd"
        for axis in ("scroll_gain_x", "scroll_gain_y"):
            if axis not in saved:
                settings[axis] = saved.get("scroll_gain", 1.0)
    return settings


def validate_settings(settings: dict) -> dict:
    """Validate persisted/UI input before starting native hooks or services."""
    from .keymap import normalize_keymap
    result = dict(settings)
    for key, low, high in (("gain", 0.1, 5), ("scroll_gain", 0.1, 5),
                           ("scroll_gain_x", 0.1, 5), ("scroll_gain_y", 0.1, 5),
                           ("emergency_escape_seconds", 0.5, 10),
                           ("peer_align", 0.0, 1.0), ("peer_span", 0.1, 1.0)):
        value = float(result[key])
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
        result[key] = value
    for key, low, high in (("mouse_hz", 30, 1000), ("port", 1024, 65533),
                           ("clipboard_max_mb", 1, 512)):
        value = int(result[key])
        if not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
        result[key] = value
    for key, allowed in (("side", ("left", "right", "top", "bottom")),
                         ("mouse_mode", ("raw", "system")),
                         ("scroll_mode", ("pixel", "line")),
                         ("role", ("host", "client")),
                         ("keymap_preset", ("native", "ctrl_cmd", "mac_layout", "option_cmd"))):
        if result[key] not in allowed:
            raise ValueError(f"Invalid {key}")
    for key in ("tls", "discovery", "lock_sync"):
        result[key] = bool(result[key])
    peers = {}
    for name, rect in (result.get("peers") or {}).items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", str(name)) or not isinstance(rect, dict):
            raise ValueError("Invalid peer layout entry")
        values = []
        for field in ("x", "y", "w", "h"):
            value = int(rect[field])
            if not -32768 <= value <= 32768:
                raise ValueError("Peer layout is out of range")
            values.append(value)
        if not 64 <= values[2] <= 16384 or not 64 <= values[3] <= 16384:
            raise ValueError("Peer display size is out of range")
        peers[str(name)] = {"x": values[0], "y": values[1], "w": values[2], "h": values[3]}
    result["peers"] = peers
    size = [int(v) for v in result.get("peer_size", [1920, 1080])[:2]]
    if len(size) != 2 or not all(64 <= v <= 16384 for v in size):
        raise ValueError("peer_size is invalid")
    result["peer_size"] = size
    if not str(result["peer"]).strip():
        raise ValueError("Enter the Mac address")
    result["keymap"] = normalize_keymap(result["keymap"])
    for key, low, high in (("smooth_scroll_ms", 10, 150), ("smooth_scroll_hz", 30, 240)):
        value = int(result[key])
        if not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
        result[key] = value
    return result


def identity() -> tuple[Path, Path, str, str]:
    directory = state_dir()
    cert, key, pairing = (directory / name for name in ("server.pem", "server.key", "pairing.json"))
    if not cert.exists() or not key.exists():
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        private = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PrecisionKVM")])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                       .public_key(private.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(now - datetime.timedelta(minutes=5))
                       .not_valid_after(now + datetime.timedelta(days=3650))
                       .sign(private, hashes.SHA256()))
        key.write_bytes(private.private_bytes(serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        if sys.platform != "win32":
            key.chmod(0o600)
    if not pairing.exists():
        save_json(pairing, {"token": secrets.token_hex(32)})
    token = json.loads(pairing.read_text(encoding="utf-8"))["token"]
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
    return cert, key, token, fingerprint


def local_ip(peer: str) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((peer, 9))
        return str(probe.getsockname()[0])
