"""InputCapture portal bridge — runs under the *system* python (needs PyGObject).

Wayland gives clients no global cursor position, so the evdev capture guesses
with a virtual cursor that drifts away from the real one. This bridge asks the
compositor instead: pointer barriers on the zone boundary emit Activated with
the true position, so crossings are exact and never phantom.

Protocol: argv[1] is a JSON config — {"capabilities": u, "barriers":
[{"id": u, "peer": str, "side": str, "rect": [x1,y1,x2,y2]}]}.
stdout: READY / EDGE <id> <x> <y> / EDGE_END <id> / DISABLED / ERROR <msg>.
Each Activated is released immediately so barriers re-arm; EDGE_END follows a
spontaneous Deactivated. stderr is unused (inherited by the caller's logs).
"""
from __future__ import annotations

import json
import sys
import threading

BUS = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
IFACE = "org.freedesktop.portal.InputCapture"
REQ_IFACE = "org.freedesktop.portal.Request"
SES_IFACE = "org.freedesktop.portal.Session"


def out(*parts):
    print(*parts, flush=True)


def main() -> int:
    try:
        from gi.repository import Gio, GLib
    except ImportError:
        out("ERROR", "PyGObject not available on this interpreter")
        return 1

    cfg = json.loads(sys.argv[1])
    v = GLib.Variant

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    loop = GLib.MainLoop()
    state = {"session": None, "started": False, "zone_set": 0,
             "zone_ready": threading.Event()}

    def call(method, params, ptype, timeout=15000):
        res = bus.call_sync(BUS, PATH, IFACE, method, v(ptype, params), None,
                            Gio.DBusCallFlags.NONE, timeout, None)
        return res.unpack()[0] if res else None

    def set_barriers():
        barriers = [{"barrier_id": v("u", b["id"]),
                     "position": v("(iiii)", tuple(b["rect"]))}
                    for b in cfg["barriers"]]
        call("SetPointerBarriers",
             (state["session"], {}, barriers, state["zone_set"]),
             "(oa{sv}aa{sv}u)")
        call("Enable", (state["session"], {}), "(oa{sv})")

    def req_signal(_c, _s, _o, _i, _sig, params):
        response, results = params
        if response != 0:
            out("ERROR", f"portal request failed ({response})")
            return
        res = dict(results)
        if "failed_barriers" in res and res["failed_barriers"]:
            out("ERROR", f"barriers rejected: {res['failed_barriers']}")
        elif "zones" in res:
            state["zone_set"] = res["zone_set"]
            if not state["zone_ready"].is_set():
                state["zone_ready"].set()
                set_barriers()
                out("READY")
            else:
                set_barriers()  # monitor layout changed — re-arm edges
        elif res.get("capabilities") is not None and not state["started"]:
            state["started"] = True
            try:
                call("ConnectToEIS", (state["session"], {}), "(oa{sv})")
                call("GetZones", (state["session"], {}), "(oa{sv})")
            except Exception as exc:
                out("ERROR", str(exc))

    def ic_signal(_c, _s, _o, _i, sig, params):
        _handle, opts = params[0], dict(params[1])
        if sig == "Activated":
            aid = opts.get("activation_id", 0)
            bid = opts.get("barrier_id", 0)
            x, y = opts.get("cursor_position", (0.0, 0.0))
            call("Release", (state["session"],
                             {"activation_id": v("u", aid)}), "(oa{sv})")
            if bid:
                out("EDGE", bid, f"{x:.1f}", f"{y:.1f}")
        elif sig == "Deactivated":
            aid = opts.get("activation_id", 0)
            out("EDGE_END", aid)
        elif sig == "Disabled":
            out("DISABLED")
            try:
                call("Enable", (state["session"], {}), "(oa{sv})")
            except Exception:
                pass

    bus.signal_subscribe(BUS, REQ_IFACE, "Response", None, None, 0, req_signal)
    bus.signal_subscribe(BUS, IFACE, None, None, None, 0, ic_signal)
    bus.signal_subscribe(BUS, SES_IFACE, "Closed", None, None, 0,
                         lambda *a: loop.quit())

    try:
        opts = {"session_handle_token": v("s", "pkvmcapture"),
                "handle_token": v("s", "pkvmhand"),
                "capabilities": v("u", int(cfg.get("capabilities", 15)))}
        ret = call("CreateSession2", (opts,), "(a{sv})")
        state["session"] = str(ret["session_handle"])
        call("Start", (state["session"], "", {}), "(osa{sv})")
    except Exception as exc:
        out("ERROR", str(exc))
        return 1
    loop.run()
    out("CLOSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
