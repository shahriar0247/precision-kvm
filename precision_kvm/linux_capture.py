"""Linux evdev capture. Importing this module never opens input devices.

Devices are read un-grabbed while local — the compositor keeps working and
the class tracks a virtual cursor from REL/ABS deltas. On a peer-edge hit
the devices are grabbed (EVIOCGRAB), stealing input from the compositor;
release()/return_local() ungrabs so control comes back instantly.

The virtual cursor cannot see the compositor's real pointer position, so
it converges to reality at screen edges (clamp) rather than exactly — a
documented approximation used by all evdev-level KVM switches.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time
from typing import Callable

from . import geometry
from .linux_input import (ABS_X, ABS_Y, BTN_LEFT, BTN_RIGHT, BTN_MIDDLE,
                          BTN_SIDE, BTN_EXTRA, BTN_TOUCH, EV_ABS, EV_KEY,
                          EV_REL, REL_HWHEEL, REL_HWHEEL_HI_RES, REL_WHEEL,
                          REL_WHEEL_HI_RES, REL_X, REL_Y, EvdevDevice,
                          evdev_key_id, input_devices, linux_monitors, selector)
from .safety import EmergencyMonitor

_BUTTON_NAMES = {BTN_LEFT: "left", BTN_RIGHT: "right", BTN_MIDDLE: "middle",
                 BTN_SIDE: "x1", BTN_EXTRA: "x2"}

PRESS_THRESHOLD = 240  # raw counts pushed into a boundary before it crosses
PRESS_GAP_S = 0.2      # pauses in pushing longer than this reset the count
RETURN_GRACE_S = 0.4   # the just-exited edge stays quiet this long on return

_BRIDGE = Path(__file__).with_name("portal_bridge.py")


def portal_barriers(peers: dict, bounds) -> tuple[list[dict], dict]:
    """Map each peer's facing edge onto a portal pointer barrier.

    Barriers must sit exactly on the outside boundary of the zone union —
    vertical at x = bounds left or right, horizontal at y = bounds top or
    bottom — and span only the edge segment the peer actually overlaps.
    Returns (bridge barrier specs, {barrier_id: (peer_name, side)}).
    """
    bx, by, bw, bh = bounds
    specs, ids, bid = [], {}, 0
    for name, (px, py, pw, ph) in peers.items():
        lo_y, hi_y = max(by, py), min(by + bh - 1, py + ph - 1)
        lo_x, hi_x = max(bx, px), min(bx + bw - 1, px + pw - 1)
        cx, cy = px + pw / 2, py + ph / 2
        if cx < bx and lo_y <= hi_y:
            side, seg = "left", (bx, lo_y, bx, hi_y)
        elif cx >= bx + bw and lo_y <= hi_y:
            side, seg = "right", (bx + bw, lo_y, bx + bw, hi_y)
        elif cy < by and lo_x <= hi_x:
            side, seg = "top", (lo_x, by, hi_x, by)
        elif cy >= by + bh and lo_x <= hi_x:
            side, seg = "bottom", (lo_x, by + bh, hi_x, by + bh)
        else:
            continue  # peer isn't flush with any edge — nowhere to cross
        bid += 1
        specs.append({"id": bid, "peer": name, "side": side,
                      "rect": [int(v) for v in seg]})
        ids[bid] = (name, side)
    return specs, ids


class LinuxCapture:
    """Share Linux input when the virtual cursor crosses the peer edge."""

    def __init__(self, on_event: Callable[[dict], object], side: str = "left",
                 on_emergency=None, emergency_escape_seconds=1.5,
                 lock_sync=True, peer_align=0.5, peer_span=0.6, peers=None,
                 touchpad_gain=1.0):
        if side not in {"left", "right", "top", "bottom"}:
            raise ValueError("side must be left, right, top, or bottom")
        self.on_event = on_event
        self.side = side
        self.on_emergency = on_emergency or (lambda reason: None)
        self.safety = EmergencyMonitor(emergency_escape_seconds)
        self.lock_sync = bool(lock_sync)
        self.locked = False
        self.peer_align = float(peer_align)
        self.peer_span = float(peer_span)
        self.peers = dict(peers) if peers else None
        self.touchpad_gain = float(touchpad_gain)
        self._peer_rect: tuple | None = None
        self._monitors: list | None = None
        self._hit: tuple | None = None
        self._entry_x = 0.5
        self._entry_y = 0.5
        self._emergency_requested = False
        self.peer_connected = False
        self.active = False
        self.error: str | None = None
        self._keys: set[int] = set()
        self._buttons: set[str] = set()
        self._vpos = [0.0, 0.0]
        self._move_dir = [0, 0]
        self._press_side: str | None = None
        self._press_amt = 0.0
        self._press_t = 0.0
        self._grace_until = 0.0
        self._portal = None          # bridge subprocess while armed
        self._portal_possible = False  # portal can arm — evdev edges stay off
        self._portal_rearms = 0
        self._portal_ids: dict = {}
        self._portal_aid: int | None = None
        self._debug = bool(os.environ.get("PRECISIONKVM_DEBUG"))
        self._abs_last: dict[int, dict[int, int]] = {}
        self._devices: list[EvdevDevice] = []
        self._keyboards: list[EvdevDevice] = []
        self._pointers: list[EvdevDevice] = []
        self._poller: select.poll | None = None
        self._fd_map: dict[int, EvdevDevice] = {}
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopping = threading.Event()

    def start(self):
        if sys.platform != "linux":
            raise OSError("LinuxCapture requires Linux")
        if self._thread and self._thread.is_alive():
            return
        self.error = None
        self._ready.clear()
        self._stopping.clear()
        self._emergency_requested = False
        self.safety.reset()
        self._press_side, self._press_amt = None, 0.0
        self._grace_until = 0.0
        self._thread = threading.Thread(target=self._run, name="kvm-evdev", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            self.stop()
            raise RuntimeError("evdev capture startup timed out")
        if self.error:
            raise RuntimeError(self.error)

    def stop(self):
        self._stopping.set()
        self.release()
        proc, self._portal = self._portal, None
        if proc is not None:
            proc.kill()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(3)

    def _emit(self, event):
        try:
            if self.on_event(event) is False:
                self.release()
                return False
            return True
        except Exception as exc:
            self.error = str(exc)
            self.release()
            return False

    def release(self, normalized_y=None, normalized_x=None):
        """Fail-safe local control: ungrab devices and resume local input."""
        if normalized_y is not None:
            self._entry_y = min(1.0, max(0.0, float(normalized_y)))
        if normalized_x is not None:
            self._entry_x = min(1.0, max(0.0, float(normalized_x)))
        was_active = self.active
        self.active = False
        self.safety.reset()
        self._press_side, self._press_amt = None, 0.0
        for device in self._devices:
            device.ungrab()
        aid, self._portal_aid = self._portal_aid, None
        if aid is not None:
            if was_active:
                x, y = self._return_point()
                self._portal_release(aid, x, y)  # compositor warps the cursor
            else:
                self._portal_release(aid)
        if was_active:
            self._grace_until = time.monotonic() + RETURN_GRACE_S
            x, y = self._return_point()
            self._vpos = [float(x), float(y)]
            try:
                self.on_event({"type": "deactivate"})
            except Exception as exc:
                self.error = str(exc)

    return_local = release

    def _emergency_stop(self, reason):
        if self._emergency_requested:
            return
        self._emergency_requested = True
        self.release()
        threading.Thread(target=self.on_emergency, args=(reason,), daemon=True,
                         name="kvm-emergency-stop").start()

    def reset_emergency(self):
        """Re-arm the panic gesture after the session handled one."""
        self._emergency_requested = False
        self.safety.reset()

    def _peer_map(self, mons):
        if self.peers:
            return {name: geometry.snap_to_edge(tuple(rect), mons)
                    for name, rect in self.peers.items()}
        return {"peer": geometry.default_peer(mons, self.side,
                                              self.peer_align, self.peer_span)}

    def _return_point(self):
        mons = self._monitors or [(0, 0, 1920, 1080)]
        peers = self._peer_map(mons)
        peer = self._peer_rect
        if peer is None or peer not in peers.values():
            peer = next(iter(peers.values()))
        return geometry.return_point(mons, peer, self._entry_x, self._entry_y)

    def _run(self):
        try:
            self._monitors = linux_monitors()
            bounds = geometry.union_bounds(self._monitors)
            self._vpos = [bounds[0] + bounds[2] / 2, bounds[1] + bounds[3] / 2]
            self._devices = input_devices()
            self._keyboards = [d for d in self._devices if d.is_keyboard]
            self._pointers = [d for d in self._devices if d.is_pointer]
            if not self._keyboards and not self._pointers:
                self.error = ("No readable input devices — join the 'input' "
                              "group (sudo usermod -aG input $USER), then re-login")
                self._ready.set()
                return
            self._fd_map = {d.fd: d for d in self._keyboards + self._pointers}
            self._poller = selector(list(self._fd_map))
            self._ready.set()
            if self._debug:
                for device in self._fd_map.values():
                    print(f"[kvm-debug] device fd={device.fd} "
                          f"name={device.name!r} pointer={device.is_pointer} "
                          f"keyboard={device.is_keyboard} rels={sorted(device.rels)} "
                          f"abss={sorted(device.abss)}", flush=True)
            # One edge mechanism only: when the compositor's InputCapture
            # portal can arm, the evdev pressure model never crosses — so
            # nothing can reach the peer before the consent dialog is
            # answered. evdev edges only exist where the portal cannot run.
            if self._portal_capable():
                self._portal_possible = True
                # Arming can take seconds (consent dialog, negotiation) —
                # run it beside the poll loop and swap in on READY.
                threading.Thread(target=self._portal_arm, args=(bounds,),
                                 daemon=True, name="kvm-portal-arm").start()
            while not self._stopping.is_set():
                for fd, _mask in self._poller.poll(200):
                    device = self._fd_map.get(fd)
                    if device is None:
                        continue
                    try:
                        for event in device.events():
                            self._handle(device, *event)
                    except OSError:
                        self._fd_map.pop(fd, None)
                        try:
                            self._poller.unregister(fd)
                        except KeyError:
                            pass
                        if device in self._pointers:
                            self._pointers.remove(device)
                        if device in self._keyboards:
                            self._keyboards.remove(device)
                        device.close()
        except Exception as exc:
            self.error = str(exc)
            self._ready.set()
        finally:
            for device in self._devices:
                device.close()

    def _handle(self, device: EvdevDevice, ev_type: int, code: int, value: int):
        if self.locked:
            return
        if ev_type == EV_KEY:
            if code == BTN_TOUCH:
                # A touchpad reports absolute positions; each contact must
                # start a fresh baseline or the first event diffs against the
                # previous touch and teleports the cursor across the screen.
                self._abs_last.pop(device.fd, None)
            elif code in _BUTTON_NAMES:
                self._button(code, value)
            elif value != 2:  # Kernel autorepeat (value 2) is not a new key event.
                self._key(code, bool(value))
        elif ev_type == EV_REL:
            self._rel(code, value)
        elif (ev_type == EV_ABS and device.is_pointer
              and not ({REL_X, REL_Y} & device.rels)):
            # Hybrids emitting REL+ABS for one motion would double-feed the
            # virtual cursor — REL wins; ABS only serves REL-less devices.
            self._abs(device, code, value)
        if self._debug and (ev_type == EV_ABS or code not in (REL_X, REL_Y)):
            print(f"[kvm-debug] {'abs' if ev_type == EV_ABS else 'key' if ev_type == EV_KEY else 'rel'} "
                  f"dev={device.name!r} code={code} value={value} active={self.active}",
                  flush=True)

    def _key(self, code: int, down: bool):
        key_id = evdev_key_id(code)
        if key_id is None:
            return
        vk, scan, extended = key_id
        self._keys.add(vk) if down else self._keys.discard(vk)
        if self.active:
            reason = self.safety.key(vk, down)
            if reason:
                self._emergency_stop(reason)
                return
        ctrl = bool(self._keys & {0x11, 0xA2, 0xA3})
        alt = bool(self._keys & {0x12, 0xA4, 0xA5})
        if vk == 0x7B and down and ctrl and alt:
            self.release()
            return
        if self.active:
            self._emit({"type": "key", "vk": vk, "scan": scan,
                        "down": down, "extended": extended})

    def _button(self, code: int, value: int):
        if value == 2:  # Ignore autorepeat on button codes too.
            return
        button = _BUTTON_NAMES[code]
        down = bool(value)
        self._buttons.add(button) if down else self._buttons.discard(button)
        if self.active:
            self._emit({"type": "button", "button": button, "down": down})

    def _rel(self, code: int, value: int):
        if self.active:
            if code == REL_X:
                self._emit({"type": "move", "dx": value, "dy": 0})
            elif code == REL_Y:
                self._emit({"type": "move", "dx": 0, "dy": value})
            elif code == REL_WHEEL:
                self._emit({"type": "scroll", "dx": 0, "dy": value * 120})
            elif code == REL_HWHEEL:
                self._emit({"type": "scroll", "dx": value * 120, "dy": 0})
            elif code == REL_WHEEL_HI_RES:
                self._emit({"type": "scroll", "dx": 0, "dy": value})
            elif code == REL_HWHEEL_HI_RES:
                self._emit({"type": "scroll", "dx": value, "dy": 0})
            return
        if code in (REL_X, REL_Y):
            self._vpos[code] += value
            if value:
                self._move_dir[code] = 1 if value > 0 else -1
            self._move_inactive(code, value)

    def _abs(self, device: EvdevDevice, code: int, value: int):
        if code not in (ABS_X, ABS_Y):
            return
        last = self._abs_last.setdefault(device.fd, {})
        previous = last.get(code, value)
        last[code] = value
        delta = value - previous
        if self.active:
            if delta:
                event = {"type": "move", "dx": 0, "dy": 0}
                event["dx" if code == ABS_X else "dy"] = delta * self.touchpad_gain
                self._emit(event)
        else:
            self._vpos[code] += delta
            if delta:
                self._move_dir[code] = 1 if delta > 0 else -1
            self._move_inactive(code, delta)

    def _move_inactive(self, axis: int, delta: float):
        if self._portal is not None or self._portal_possible:
            return  # compositor barriers own edges — armed, arming, or re-arming
        if (self._stopping.is_set() or not self.peer_connected
                or self._keys or self._buttons or self.active):
            self._press_side, self._press_amt = None, 0.0
            return
        mons = self._monitors or [(0, 0, 1920, 1080)]
        bounds = geometry.union_bounds(mons)
        x, y = geometry.clamp(bounds, self._vpos[0], self._vpos[1])
        self._vpos = [float(x), float(y)]
        hit = geometry.hit(mons, self._peer_map(mons), x, y)
        if hit is None:
            self._press_side, self._press_amt = None, 0.0
            return
        name, side, nx, ny = hit
        edge_axis = 0 if side in ("left", "right") else 1
        if side != self._press_side:
            self._press_side, self._press_amt = side, 0.0
        now = time.monotonic()
        if now - self._press_t > PRESS_GAP_S:
            self._press_amt = 0.0
        # Crossing needs pressure: motion must push INTO the boundary, and
        # it accumulates until a deliberate press crosses the threshold.
        # A cursor parked at the edge — or one the virtual position drifted
        # to — never builds pressure, and the just-exited edge stays quiet
        # for a beat after returning local.
        toward = (self._move_dir[edge_axis] < 0 if side in ("left", "top")
                  else self._move_dir[edge_axis] > 0)
        if now < self._grace_until or not toward:
            self._press_amt = 0.0
            return
        self._press_t = now  # tangential slides keep the press alive
        if axis == edge_axis:
            self._press_amt += abs(delta)
        if self._press_amt < PRESS_THRESHOLD:
            return
        self._press_side, self._press_amt = None, 0.0
        self._cross(hit, mons)

    def _cross(self, hit, mons):
        self._hit = hit
        name, side, nx, ny = hit
        self._peer_rect = self._peer_map(mons)[name]
        self._entry_x, self._entry_y = nx, ny
        self.active = True
        # Only devices we actually read: grabbing e.g. a power-button node
        # would eat its presses for the whole shared session.
        for device in self._keyboards + self._pointers:
            try:
                device.grab()
            except OSError:
                continue
        self._emit({"type": "activate", "normalized_y": ny,
                    "normalized_x": nx, "side": side, "peer": name})

    # ----- portal edge detection (Wayland) -----------------------------------

    def _portal_arm(self, bounds):
        proc = self._portal_start(bounds)
        if proc is None:
            return
        if self._stopping.is_set():
            proc.kill()
            return
        self._portal = proc
        self._portal_rearms = 0

    def _portal_capable(self):
        """A python3 with GI plus the bridge script — the portal can arm.
        Returns the interpreter path or None."""
        if not os.environ.get("WAYLAND_DISPLAY") or not _BRIDGE.exists():
            return None
        for py in ("/usr/bin/python3", "/usr/local/bin/python3"):
            if (Path(py).exists()
                    and subprocess.run([py, "-c", "import gi"],
                                       capture_output=True).returncode == 0):
                return py
        return None

    def _portal_start(self, bounds):
        """Arm compositor barriers via the portal bridge, if it can run."""
        peers = self._peer_map(self._monitors or [(0, 0, 1920, 1080)])
        specs, self._portal_ids = portal_barriers(peers, bounds)
        if not specs:
            return None
        py = self._portal_capable()
        if py is None:
            return None
        try:
            proc = subprocess.Popen(
                [py, str(_BRIDGE),
                 json.dumps({"capabilities": 15, "barriers": specs})],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError:
            return None
        # The portal's consent dialog can sit on screen a while before the
        # user approves — wait generously; evdev covers edges meanwhile.
        ready = select.select([proc.stdout], [], [], 90)[0]
        if not ready or proc.stdout.readline().strip() != "READY":
            proc.kill()
            return None
        threading.Thread(target=self._portal_reader, args=(proc,),
                         daemon=True, name="kvm-portal").start()
        return proc

    def _portal_reader(self, proc):
        for line in proc.stdout:
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "EDGE":
                name, side = self._portal_ids.get(int(parts[1]), (None, None))
                try:
                    aid = int(parts[2])
                except ValueError:
                    continue
                if name:
                    try:
                        self._portal_edge(name, side, aid,
                                          float(parts[3]), float(parts[4]))
                    except Exception:
                        pass
                else:
                    self._portal_release(aid)  # unknown barrier — free it
            elif len(parts) >= 2 and parts[0] == "EDGE_END":
                try:
                    if self._portal_aid == int(parts[1]):
                        self._portal_aid = None
                except ValueError:
                    pass
            elif parts and parts[0] in ("CLOSED", "ERROR"):
                break
        self._portal = None  # bridge died — re-arm; evdev edges stay off
        if self._portal_possible and not self._stopping.is_set():
            threading.Thread(target=self._portal_rearm, daemon=True,
                             name="kvm-portal-rearm").start()

    def _portal_rearm(self):
        time.sleep(1.0)
        if self._stopping.is_set() or self._portal is not None:
            return
        if self._portal_rearms >= 3:
            self.error = "Compositor input-capture unavailable — edge crossings disabled"
            return
        self._portal_rearms += 1
        self._portal_arm(geometry.union_bounds(
            self._monitors or [(0, 0, 1920, 1080)]))

    def _portal_release(self, aid, x=None, y=None):
        proc = self._portal
        if proc is None or proc.stdin is None:
            return
        try:
            coords = f" {x:.0f} {y:.0f}" if x is not None else ""
            proc.stdin.write(f"RELEASE {aid}{coords}\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def _portal_edge(self, name, side, aid, x, y):
        """A real edge crossing reported by the compositor."""
        if (self._stopping.is_set() or not self.peer_connected
                or self._keys or self._buttons or self.active
                or time.monotonic() < self._grace_until):
            self._portal_release(aid)
            return
        mons = self._monitors or [(0, 0, 1920, 1080)]
        peer = self._peer_map(mons).get(name)
        if peer is None:
            self._portal_release(aid)
            return
        cx, cy = geometry.clamp(peer, x, y)
        nx = (cx - peer[0]) / max(1, peer[2] - 1)
        ny = (cy - peer[1]) / max(1, peer[3] - 1)
        self._portal_aid = aid
        self._cross((name, side, nx, ny), mons)
