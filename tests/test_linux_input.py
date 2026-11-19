"""Linux backends: table integrity and event translation only.

Per CONTRIBUTING: no global hooks, no /dev input access, no real clipboard.
Devices are fakes; the OS-specific ioctl paths are never exercised here.
"""
import pytest

from precision_kvm import linux_input
from precision_kvm.linux_capture import LinuxCapture
from precision_kvm.linux_inject import LinuxInjector


def test_linux_keys_table_is_coherent():
    from collections import Counter

    seen, triples = set(), set()
    for code, (vk, scan, extended) in linux_input.LINUX_KEYS.items():
        assert code not in seen
        seen.add(code)
        assert 0 <= vk <= 0xFF
        assert 0 <= scan <= 0x7F
        assert extended in (False, True)
        if scan:
            triple = (vk, scan, extended)
            assert triple not in triples, f"duplicate {triple}"
            triples.add(triple)
    # Hardware reality: NumLock and Pause both report set-1 scan 0x45
    # non-extended; it must stay the ONLY (scan, ext) collision.
    counts = Counter((s, e) for _, s, e in linux_input.LINUX_KEYS.values() if s)
    assert sorted(k for k, c in counts.items() if c > 1) == [(0x45, False)]


def test_key_translation_round_trips_and_scan_wins():
    for code, (vk, scan, extended) in linux_input.LINUX_KEYS.items():
        assert linux_input.evdev_key_id(code) == (vk, scan, extended)
        if scan:
            assert linux_input.evdev_for_key(vk, scan, extended) == code
    # Scan disambiguates keys sharing a VK (shift sides) or a family
    # (main Enter vs numpad Enter).
    assert linux_input.evdev_for_key(0x10, 0x36, False) == 54   # right shift
    assert linux_input.evdev_for_key(0x10, 0x2A, False) == 42   # left shift
    assert linux_input.evdev_for_key(0x0D, 0x1C, True) == 96    # numpad enter
    assert linux_input.evdev_for_key(0x0D, 0x1C, False) == 28   # main enter
    # VK-only producers still land on the right physical key.
    assert linux_input.evdev_for_key(0x5B, 0, False) == 125
    assert linux_input.evdev_for_key(0xFE, 0, False) is None
    # NumLock and Pause share scan 0x45 — only the full triple separates them.
    assert linux_input.evdev_for_key(0x90, 0x45, False) == 69
    assert linux_input.evdev_for_key(0x13, 0x45, False) == 119


class FakeDevice:
    def __init__(self):
        self.calls = []
        self.closed = False

    def emit(self, ev_type, code, value):
        self.calls.append(("emit", ev_type, code, value))

    def key(self, code, down):
        self.calls.append(("key", code, down))

    def rel(self, code, value):
        self.calls.append(("rel", code, value))

    def syn(self):
        self.calls.append(("syn",))

    def close(self):
        self.closed = True


def _injector(monkeypatch):
    monkeypatch.setattr("precision_kvm.linux_inject.linux_monitors",
                        lambda: [(0, 0, 1920, 1080)])
    returned = []
    device = FakeDevice()
    return (LinuxInjector(on_return=returned.append, side="left",
                          device=device), device, returned)


def _activate(injector, side="left", fraction=0.5):
    injector.handle({"type": "activate", "side": side, "normalized_y": fraction,
                     "normalized_x": fraction, "gain": 1.0, "scroll_gain": 1.0,
                     "scroll_mode": "pixel", "smooth_scroll": False})


def test_injector_activate_enters_on_far_edge_and_slams(monkeypatch):
    injector, device, _ = _injector(monkeypatch)
    _activate(injector, side="left", fraction=0.25)
    assert injector.active
    # Client on the host's left -> entry at the client's right edge.
    assert injector.x == injector.left + injector.width - 2
    assert injector.y == injector.top + 0.25 * (injector.height - 1)
    assert ("rel", linux_input.REL_X, 100000) in device.calls


def test_injector_move_emits_deltas_and_detects_return(monkeypatch):
    injector, device, returned = _injector(monkeypatch)
    _activate(injector, side="left")
    device.calls.clear()
    # Entry sits 2px inside the right edge; inward moves emit raw deltas.
    injector.handle({"type": "move", "dx": -10, "dy": -5})
    assert ("rel", linux_input.REL_X, -10) in device.calls
    assert ("rel", linux_input.REL_Y, -5) in device.calls
    assert not returned
    # Crossing back out through the right edge returns control.
    injector.handle({"type": "move", "dx": 5000, "dy": 0})
    assert len(returned) == 1
    assert returned[0]["type"] == "return"
    assert returned[0]["normalized_y"] == pytest.approx(0.5, abs=0.01)


def test_injector_scroll_uses_hi_res_120ths(monkeypatch):
    injector, device, _ = _injector(monkeypatch)
    _activate(injector)
    device.calls.clear()
    injector.handle({"type": "scroll", "dx": 0, "dy": 120})
    assert ("rel", linux_input.REL_WHEEL_HI_RES, 120) in device.calls
    device.calls.clear()
    injector.handle({"type": "scroll", "dx": -60, "dy": 0})
    assert ("rel", linux_input.REL_HWHEEL_HI_RES, -60) in device.calls


def test_injector_key_repeat_becomes_evdev_autorepeat(monkeypatch):
    injector, device, _ = _injector(monkeypatch)
    _activate(injector)
    device.calls.clear()
    injector.handle({"type": "key", "vk": 0x41, "scan": 0x1E, "down": True})
    assert ("key", 30, True) in device.calls
    injector.handle({"type": "key", "vk": 0x41, "scan": 0x1E, "down": True})
    assert ("emit", 1, 30, 2) in device.calls  # repeat, not a second press
    injector.handle({"type": "key", "vk": 0x41, "scan": 0x1E, "down": False})
    assert ("key", 30, False) in device.calls
    assert 30 not in injector.keys


def test_injector_release_all_clears_held_state(monkeypatch):
    injector, device, _ = _injector(monkeypatch)
    _activate(injector)
    injector.handle({"type": "key", "vk": 0x41, "scan": 0x1E, "down": True})
    injector.handle({"type": "button", "button": "left", "down": True})
    device.calls.clear()
    injector.release_all()
    assert ("key", 30, False) in device.calls
    assert ("key", linux_input.BTN_LEFT, False) in device.calls
    assert not injector.keys and not injector.buttons


def _capture(monkeypatch):
    monkeypatch.setattr("precision_kvm.linux_capture.linux_monitors",
                        lambda: [(0, 0, 1920, 1080)])
    events = []
    capture = LinuxCapture(events.append, side="left")
    capture._monitors = [(0, 0, 1920, 1080)]
    return capture, events


def test_capture_key_emits_wire_triple(monkeypatch):
    capture, events = _capture(monkeypatch)
    capture.active = True
    capture._handle(None, linux_input.EV_KEY, 30, 1)   # KEY_A down
    capture._handle(None, linux_input.EV_KEY, 97, 1)   # right ctrl down
    assert events[0] == {"type": "key", "vk": 0x41, "scan": 0x1E,
                        "down": True, "extended": False}
    assert events[1] == {"type": "key", "vk": 0xA3, "scan": 0x1D,
                        "down": True, "extended": True}


def test_capture_scroll_and_button_units(monkeypatch):
    capture, events = _capture(monkeypatch)
    capture.active = True
    capture._handle(None, linux_input.EV_REL, linux_input.REL_WHEEL, 1)
    capture._handle(None, linux_input.EV_REL, linux_input.REL_WHEEL_HI_RES, 40)
    capture._handle(None, linux_input.EV_KEY, linux_input.BTN_LEFT, 1)
    assert {"type": "scroll", "dx": 0, "dy": 120} in events
    assert {"type": "scroll", "dx": 0, "dy": 40} in events
    assert {"type": "button", "button": "left", "down": True} in events


def test_capture_edge_cross_activates_and_release_deactivates(monkeypatch):
    capture, events = _capture(monkeypatch)
    capture.peer_connected = True
    capture._vpos = [1.0, 540.0]
    # Touching the edge is not a crossing — a real one keeps pressing into it.
    capture._handle(None, linux_input.EV_REL, linux_input.REL_X, -60)
    assert not capture.active
    for _ in range(3):
        capture._handle(None, linux_input.EV_REL, linux_input.REL_X, -60)
    activation = next(e for e in events if e["type"] == "activate")
    assert activation["side"] == "left" and capture.active
    events.clear()
    capture.release(normalized_y=0.4)
    assert not capture.active
    assert events[-1] == {"type": "deactivate"}
    # The virtual cursor re-anchors inside the screen, ready to re-arm.
    assert 0 <= capture._vpos[0] <= 1920 and 0 <= capture._vpos[1] <= 1080


def test_capture_release_grace_blocks_immediate_regrab(monkeypatch):
    capture, events = _capture(monkeypatch)
    capture.peer_connected = True
    capture._vpos = [1.0, 540.0]
    for _ in range(4):
        capture._handle(None, linux_input.EV_REL, linux_input.REL_X, -60)
    assert capture.active
    capture.release()
    assert not capture.active
    # Landing ~2px from the edge, pressing again inside the grace window
    # must NOT re-grab — this was the "bounces back into the peer" bug.
    for _ in range(6):
        capture._handle(None, linux_input.EV_REL, linux_input.REL_X, -60)
    assert not capture.active
    # Once the grace window passes, a fresh deliberate press crosses again —
    # no spatial retreat required (that was the "can't cross back" bug).
    capture._grace_until = 0.0
    events.clear()
    for _ in range(4):
        capture._handle(None, linux_input.EV_REL, linux_input.REL_X, -60)
    assert capture.active


def test_capture_edge_slide_away_does_not_cross(monkeypatch):
    capture, events = _capture(monkeypatch)
    capture.peer_connected = True
    capture._vpos = [0.0, 540.0]
    capture._move_dir[0] = 1  # last X motion was AWAY from the left edge
    for _ in range(3):
        capture._handle(None, linux_input.EV_REL, linux_input.REL_Y, 3)
    assert not capture.active
    assert not any(e["type"] == "activate" for e in events)


def test_capture_parked_at_edge_never_builds_pressure(monkeypatch):
    capture, events = _capture(monkeypatch)
    capture.peer_connected = True
    capture._vpos = [0.0, 540.0]
    # Weak nudges at a pinned edge stay under the threshold — a drifted
    # virtual position can't phantom-cross on small moves.
    for _ in range(4):
        capture._handle(None, linux_input.EV_REL, linux_input.REL_X, -30)
    assert not capture.active


def test_portal_barriers_map_peer_edges():
    from precision_kvm.linux_capture import portal_barriers

    bounds = (0, 0, 1920, 1200)
    peers = {"MAC": (-1920, -21, 1920, 1080)}
    specs, ids = portal_barriers(peers, bounds)
    assert len(specs) == 1 and specs[0]["peer"] == "MAC"
    # Left edge sits at x=0 spanning the y-overlap of peer and bounds.
    assert specs[0]["rect"] == [0, 0, 0, 1058]
    assert ids[specs[0]["id"]] == ("MAC", "left")

    # A right-side peer yields a barrier just right of the union.
    specs, ids = portal_barriers({"PC": (1920, 0, 1920, 1200)}, bounds)
    assert specs[0]["rect"] == [1920, 0, 1920, 1199]
    assert ids[specs[0]["id"]] == ("PC", "right")

    # A peer flush to no edge produces no barrier — nowhere real to cross.
    specs, ids = portal_barriers({"FAR": (-5000, 5000, 100, 100)}, bounds)
    assert not specs and not ids


def test_injector_return_requires_sustained_edge_push(monkeypatch):
    from precision_kvm import linux_inject as inject_module

    monkeypatch.setattr(inject_module, "linux_monitors",
                        lambda: [(0, 0, 1920, 1200)])
    returns = []

    class FakeDevice:
        def rel(self, *args): pass
        def key(self, *args): pass
        def syn(self): pass
        def emit(self, *args): pass

    injector = LinuxInjector(on_return=returns.append, device=FakeDevice())
    injector.handle({"type": "activate", "side": "left", "normalized_y": 0.5})
    assert injector.active
    # Entry parks the cursor at the return edge — jitter must not bounce.
    for _ in range(10):
        injector.handle({"type": "move", "dx": 2, "dy": 0})
    assert not returns
    for _ in range(10):
        injector.handle({"type": "move", "dx": 30, "dy": 0})
    assert returns and returns[0]["type"] == "return"


def test_capture_suppresses_edge_cross_while_keys_held(monkeypatch):
    capture, events = _capture(monkeypatch)
    capture.peer_connected = True
    capture._keys.add(0x41)
    capture._vpos = [1.0, 540.0]
    capture._handle(None, linux_input.EV_REL, linux_input.REL_X, -5)
    assert not capture.active and not any(e["type"] == "activate" for e in events)


def test_capture_ctrl_alt_f12_returns_local(monkeypatch):
    capture, events = _capture(monkeypatch)
    capture.peer_connected = True
    capture.active = True
    capture._handle(None, linux_input.EV_KEY, 29, 1)   # left ctrl down
    capture._handle(None, linux_input.EV_KEY, 56, 1)   # left alt down
    capture._handle(None, linux_input.EV_KEY, 88, 1)   # KEY_F12 -> return local
    assert not capture.active
    assert events[-1] == {"type": "deactivate"}


def test_linux_clipboard_wayland_bundle(monkeypatch):
    import io as _io
    from PIL import Image

    from precision_kvm import clipboard as clip

    monkeypatch.setattr(clip.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(clip.LinuxClipboard, "_spawn_watch", lambda self: None)
    backend = clip.LinuxClipboard()
    assert backend._mode == "wayland"
    assert backend._watch is None

    png_buf = _io.BytesIO()
    Image.new("RGB", (2, 2), (255, 0, 0)).save(png_buf, format="PNG")
    png_bytes = png_buf.getvalue()

    def fake_out(args, limit=None):
        if args == ["-l"]:
            return clip.PNG.encode() + b"\n" + clip.TEXT.encode()
        if args == ["-t", clip.PNG]:
            return png_bytes
        if args == ["-n", "-t", clip.TEXT]:
            return b"hello"
        return None

    monkeypatch.setattr(backend, "_clip_out", fake_out)
    bundle = backend.read(1024 * 1024)
    assert bundle == {clip.PNG: png_bytes, clip.TEXT: b"hello"}

    written = []
    monkeypatch.setattr(clip.subprocess, "run",
                        lambda cmd, **kw: written.append((cmd, kw)))
    backend.write({clip.PNG: png_buf.getvalue(), clip.TEXT: b"t"}, 1024 * 1024)
    # A text+image bundle installs the PNG: one MIME type per selection owner.
    assert written[-1][0] == ["wl-copy", "-t", clip.PNG]
    assert written[-1][1]["input"][:8] == b"\x89PNG\r\n\x1a\n"


def test_linux_clipboard_requires_a_backend(monkeypatch):
    from precision_kvm import clipboard as clip

    monkeypatch.setattr(clip.shutil, "which", lambda tool: None)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    with pytest.raises(OSError):
        clip.LinuxClipboard()
