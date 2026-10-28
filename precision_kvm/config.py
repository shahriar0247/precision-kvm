"""Machine-local settings and identity; never stored in the source tree."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
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
                "swap_ctrl_cmd": False, "ssh_target": ""}
    if path.exists():
        settings.update(json.loads(path.read_text(encoding="utf-8")))
    return settings


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
