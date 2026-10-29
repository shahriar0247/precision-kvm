"""Free-form display layout.

Every screen — local monitors and remote peer displays — is a rectangle in
one shared coordinate space (the Windows virtual desktop). A peer can sit
anywhere: along an edge, at a corner, or offset along another monitor. The
host decides a cursor crossing purely from geometry, so arbitrary monitor
arrangements and client-to-client chains share one model.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys

Rect = tuple[int, int, int, int]  # x, y, width, height in virtual pixels

EDGE_GAP = 8       # forgiven placement error between a monitor edge and a peer
PEER_WIDTH = 1920  # assumed size when deriving a rect from a side description
PEER_HEIGHT = 1080


def inside(rect: Rect, x: int, y: int) -> bool:
    rx, ry, rw, rh = rect
    return rx <= x < rx + rw and ry <= y < ry + rh


def clamp(rect: Rect, x: float, y: float) -> tuple[int, int]:
    rx, ry, rw, rh = rect
    return (min(rx + rw - 1, max(rx, round(x))),
            min(ry + rh - 1, max(ry, round(y))))


def union_bounds(rects: list[Rect]) -> Rect:
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    return x0, y0, x1 - x0, y1 - y0


def primary(rects: list[Rect]) -> Rect:
    """The primary monitor always owns virtual origin (0, 0)."""
    for rect in rects:
        if inside(rect, 0, 0):
            return rect
    return rects[0]


def monitors() -> list[Rect]:
    """Every local display rectangle in virtual screen coordinates."""
    if sys.platform != "win32":
        return []
    user = ctypes.windll.user32
    found: list[Rect] = []

    class MonitorRect(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    callback_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR,
                                    wintypes.HDC, ctypes.POINTER(MonitorRect),
                                    wintypes.LPARAM)

    @callback_t
    def each(_monitor, _dc, rect, _extra):
        r = rect.contents
        found.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    if not user.EnumDisplayMonitors(None, None, each, 0) or not found:
        return [(0, 0, user.GetSystemMetrics(0), user.GetSystemMetrics(1))]
    return found


def default_peer(mons: list[Rect], side: str, align: float = 0.5,
                 span: float = 0.6) -> Rect:
    """Rectangle equivalent of the legacy 'side + alignment + span' model."""
    px, py, pw, ph = primary(mons)
    if side == "left":
        return (px - PEER_WIDTH, py + round((align - span / 2) * ph),
                PEER_WIDTH, round(span * ph))
    if side == "right":
        return (px + pw, py + round((align - span / 2) * ph),
                PEER_WIDTH, round(span * ph))
    if side == "top":
        return (px + round((align - span / 2) * pw), py - PEER_HEIGHT,
                round(span * pw), PEER_HEIGHT)
    return (px + round((align - span / 2) * pw), py + ph,
            round(span * pw), PEER_HEIGHT)


def hit(mons: list[Rect], peers: dict[str, Rect], x: int, y: int,
        gap: int = EDGE_GAP):
    """Resolve a cursor position to a peer crossing.

    Returns ``(name, side, nx, ny)`` — peer name, the direction the cursor
    leaves the local desktop, and the peer-relative entry position — or
    ``None`` when the cursor is not poised at a peer-facing boundary.
    """
    limit = gap * gap
    for name, rect in peers.items():
        cx, cy = clamp(rect, x, y)
        dx, dy = cx - x, cy - y
        distance2 = dx * dx + dy * dy
        if not 0 < distance2 <= limit:
            continue
        step_x = x + (1 if dx > 0 else -1 if dx < 0 else 0)
        step_y = y + (1 if dy > 0 else -1 if dy < 0 else 0)
        if any(inside(m, step_x, step_y) for m in mons):
            continue  # still inside the local desktop union
        side = "left" if dx < 0 else "right" if dx > 0 else None
        if side is None or abs(dy) > abs(dx):
            side = "top" if dy < 0 else "bottom"
        rx, ry, rw, rh = rect
        nx = (cx - rx) / max(1, rw - 1)
        ny = (cy - ry) / max(1, rh - 1)
        return name, side, nx, ny
    return None


def return_point(mons: list[Rect], peer: Rect, nx: float, ny: float):
    """Map a peer-normalized exit fraction to a local cursor landing point."""
    rx, ry, rw, rh = peer
    px = rx + min(1.0, max(0.0, nx)) * max(1, rw - 1)
    py = ry + min(1.0, max(0.0, ny)) * max(1, rh - 1)
    x = y = 0
    best = None
    for mon in mons:
        cx, cy = clamp(mon, px, py)
        distance2 = (cx - px) ** 2 + (cy - py) ** 2
        if best is None or distance2 < best:
            best, x, y, m = distance2, cx, cy, mon
    mx, my, mw, mh = m
    # Landing exactly on the boundary would instantly re-cross; step inward
    # along the axis where the peer actually lies outside this monitor.
    if px < mx:
        x = min(mx + mw - 2, x + 2)
    elif px > mx + mw - 1:
        x = max(mx + 1, x - 2)
    if py < my:
        y = min(my + mh - 2, y + 2)
    elif py > my + mh - 1:
        y = max(my + 1, y - 2)
    return x, y


def snap(peer: Rect, mons: list[Rect], threshold: int) -> Rect:
    """Magnetic snap like the OS display settings: pull a peer edge flush."""
    px, py, pw, ph = peer
    dx = dy = None
    for mx, my, mw, mh in mons:
        for edge_x in (mx, mx + mw):
            for peer_x in (px, px + pw):
                candidate = edge_x - peer_x
                if abs(candidate) <= threshold and (dx is None or abs(candidate) < abs(dx)):
                    dx = candidate
        for edge_y in (my, my + mh):
            for peer_y in (py, py + ph):
                candidate = edge_y - peer_y
                if abs(candidate) <= threshold and (dy is None or abs(candidate) < abs(dy)):
                    dy = candidate
    return px + (dx or 0), py + (dy or 0), pw, ph
