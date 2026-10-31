"""Shared helpers for the control panel."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from . import geometry
from .config import identity, local_ip, save_json, state_dir, validate_settings
from .session import HostSession, run_client_forever

CLIENT_CONFIG_KEYS = ("host", "port", "token", "fingerprint", "tls", "auto",
                      "clipboard_enabled", "clipboard_max_mb", "tweaks")


class ClientRunner:
    """Reconnecting client loop bound to a status callback."""

    def __init__(self, status):
        self.status = status
        self.stopped = threading.Event()
        self.thread = None

    @property
    def active(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.active:
            return
        path = state_dir() / "client.json"
        if not path.exists():
            self.status("Import the pairing file from the host first")
            return
        config = json.loads(path.read_text(encoding="utf-8"))
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=run_client_forever,
                                       args=(config, self.stopped, self.status),
                                       name="kvm-client", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(4)


def load_client() -> dict:
    path = state_dir() / "client.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def import_pairing(text: str) -> dict:
    """Merge pairing JSON into client.json; returns the stored config."""
    data = json.loads(text)
    config = {key: data[key] for key in CLIENT_CONFIG_KEYS if key in data}
    if not (config.get("host") and config.get("token") and config.get("fingerprint")):
        raise ValueError("Pairing file needs host, token and fingerprint")
    existing = load_client()
    existing.update(config)
    save_json(state_dir() / "client.json", existing)
    return existing


def collect(settings: dict, values: dict) -> dict:
    return validate_settings(dict(settings, **values))


def persist(settings: dict) -> dict:
    settings = validate_settings(settings)
    save_json(state_dir() / "settings.json", settings)
    return settings


def export_pairing(settings: dict, destination: Path) -> None:
    _, _, token, fingerprint = identity()
    save_json(Path(destination), {
        "host": local_ip(settings["peer"]), "port": settings["port"],
        "token": token, "fingerprint": fingerprint,
        "tls": bool(settings.get("tls", True)),
        "auto": bool(settings.get("discovery", True)),
        "clipboard_enabled": settings["clipboard_enabled"],
        "clipboard_max_mb": 512})


def start_host(settings: dict, status) -> HostSession:
    session = HostSession(settings, status)
    session.start()
    return session


def layout_transform(mons, peers, width: float, height: float,
                     margin: float = 36.0):
    """Scale the shared display space to a canvas; returns (scale, ox, oy)."""
    rects = list(mons) + [r for r in peers.values()]
    bx, by, bw, bh = geometry.union_bounds(rects)
    scale = min((width - margin * 2) / max(1, bw), (height - margin * 2) / max(1, bh))
    ox = margin + (width - margin * 2 - bw * scale) / 2 - bx * scale
    oy = margin + (height - margin * 2 - bh * scale) / 2 - by * scale
    return scale, ox, oy


def load_layout(settings: dict):
    """(monitors, peers) for the layout editor; peers fall back to the
    configured side when none have been placed yet."""
    mons = geometry.monitors() or [(0, 0, 1920, 1080)]
    peers = {name: geometry.snap_to_edge((rect["x"], rect["y"], rect["w"], rect["h"]), mons)
             for name, rect in (settings.get("peers") or {}).items()}
    if not peers:
        peers = {"mac": geometry.default_peer(mons, settings.get("side", "left"),
                                              settings.get("peer_align", 0.5),
                                              settings.get("peer_span", 0.6))}
        w, h = (int(v) for v in settings.get("peer_size", [1920, 1080]))
        x, y, _pw, _ph = peers["mac"]
        peers["mac"] = (x, y, w, h)
    return mons, peers


def save_peers(settings: dict, peers: dict) -> dict:
    settings = dict(settings)
    mons = geometry.monitors() or [(0, 0, 1920, 1080)]
    settings["peers"] = {name: dict(zip("xywh", (int(v) for v in geometry.snap_to_edge(r, mons))))
                         for name, r in peers.items()}
    return persist(settings)


def stats_line(session: HostSession | None) -> str:
    if not session or not session.buffer:
        return "No session measurements yet"
    metrics = session.buffer.metrics
    return (f"RTT {session.rtt_ms:.2f} ms  |  queue peak {metrics.queue_high_water}  |  "
            f"max queue {metrics.max_queue_ms:.2f} ms  |  merged moves {metrics.coalesced_moves:,}")


def status_tone(message: str) -> str:
    if message.startswith("Connected"):
        return "good"
    if any(word in message.lower() for word in
           ("error", "rejected", "emergency", "stopped", "unavailable")):
        return "bad"
    return "idle"
