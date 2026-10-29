"""Shared helpers for the Qt and Flet control panels."""
from __future__ import annotations

import os
from pathlib import Path
import sys

from . import geometry
from .config import identity, load_settings, local_ip, save_json, state_dir, validate_settings
from .session import HostSession

FRAMEWORKS = {"qt": "Qt 6", "flet": "Flet", "tk": "Tkinter"}


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


def switch_ui(framework: str, start: bool) -> None:
    """Persist the chosen UI framework and relaunch this executable in it."""
    settings = load_settings()
    settings["ui"] = framework
    save_json(state_dir() / "settings.json", settings)
    argv = [sys.executable, "gui", "--ui", framework]
    if start:
        argv.append("--start")
    os.execv(sys.executable, argv)


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
    peers = {name: geometry.snap_side((rect["x"], rect["y"], rect["w"], rect["h"]), mons)
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
    settings["peers"] = {name: dict(zip("xywh", (int(v) for v in geometry.snap_side(r, mons))))
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
