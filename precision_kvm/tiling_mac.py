"""macOS tiling backend — AX window control plus a standalone chord tap.

Importing this module never posts input events or installs a tap. The
backend needs Accessibility permission (the KVM client already holds it).
Window tokens are stable integers re-matched by (pid, title) so the engine
sees consistent identities across enumerations.

The chord tap watches only tiling chords and tiling drags; while a KVM
session controls this Mac the tap stands down (the forwarded "tiling"
events drive the engine instead), so chords are never applied twice.
"""
from __future__ import annotations

import os
import sys
import threading

if sys.platform != "darwin":
    raise OSError("MacTiling requires macOS")

import AppKit
import ApplicationServices as ax_services
import Quartz

from .keymap import SCAN_TO_MAC, VK_TO_MAC
from .tiling import chord_action

AX = ax_services  # AXUIElement* and AXValue* live here, not in Quartz

# Windows VK -> mac keycode (reverse of keymap), for chord translation.
MAC_TO_VK = {mac: vk for vk, mac in VK_TO_MAC.items()}
for vk, scan in ((65 + i, s) for i, s in enumerate(range(0x1E, 0x1E + 26))):
    MAC_TO_VK.setdefault(SCAN_TO_MAC.get(scan, -1), vk)
for vk, scan in ((0x31 + i, s) for i, s in
                 enumerate((0x0B, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A))):
    MAC_TO_VK.setdefault(SCAN_TO_MAC.get(scan, -1), vk)
MAC_TO_VK.pop(-1, None)

AX_STANDARD = ("AXStandardWindow", "AXDialog")


def _attr(ref, name):
    try:
        result = AX.AXUIElementCopyAttributeValue(ref, name, None)
    except Exception:
        return None
    if isinstance(result, tuple):
        code, value = result[0], result[1] if len(result) > 1 else None
        return value if code == 0 else None
    return result


def _set(ref, name, value):
    try:
        AX.AXUIElementSetAttributeValue(ref, name, value)
    except Exception:
        pass


def _pair(value, ktype):
    result = AX.AXValueGetValue(value, ktype, None)
    if isinstance(result, tuple):
        if len(result) >= 3 and isinstance(result[1], (int, float)):
            return (float(result[1]), float(result[2]))
        point = result[-1]
    else:
        point = result
    return (float(point.x), float(point.y))


def _rect_of(window):
    position = _attr(window, "AXPosition")
    size = _attr(window, "AXSize")
    if position is None or size is None:
        return None
    x, y = _pair(position, AX.kAXValueTypeCGPoint)
    w, h = _pair(size, AX.kAXValueTypeCGSize)
    if w < 80 or h < 60:
        return None
    return (round(x), round(y), round(w), round(h))


class MacTiling:
    """Backend for TilingController: enumerate, place, focus, hide windows."""

    def __init__(self):
        self._tokens: dict[int, dict] = {}
        self._next = 1
        self._focused_rect: tuple | None = None
        self._own = os.getpid()

    # -- token registry --------------------------------------------------------

    def _match(self, pid, title, rect, ref):
        for token, entry in self._tokens.items():
            if entry["pid"] == pid and entry["title"] == title:
                entry["ref"], entry["rect"] = ref, rect
                return token
        token = self._next
        self._next += 1
        self._tokens[token] = {"pid": pid, "title": title, "ref": ref, "rect": rect}
        return token

    def _entry(self, token):
        return self._tokens.get(token)

    # -- controller interface --------------------------------------------------

    def windows(self) -> list[tuple[int, tuple[int, int, int, int]]]:
        out = []
        for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
            if (app.activationPolicy() != AppKit.NSApplicationActivationPolicyRegular
                    or app.isHidden() or app.processIdentifier() == self._own):
                continue
            pid = app.processIdentifier()
            app_ref = AX.AXUIElementCreateApplication(pid)
            _set(app_ref, "AXManualAccessibility", True)
            windows = _attr(app_ref, "AXWindows")
            if not windows:
                continue
            for window in windows:
                subrole = _attr(window, "AXSubrole")
                if subrole not in AX_STANDARD:
                    continue
                if _attr(window, "AXMinimized"):
                    continue
                rect = _rect_of(window)
                if rect is None:
                    continue
                title = str(_attr(window, "AXTitle") or "")
                out.append((self._match(pid, title, rect, window), rect))
        return out

    def focused(self):
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if (app is None or app.isHidden()
                or app.activationPolicy() != AppKit.NSApplicationActivationPolicyRegular
                or app.processIdentifier() == self._own):
            return None
        app_ref = AX.AXUIElementCreateApplication(app.processIdentifier())
        window = _attr(app_ref, "AXFocusedWindow")
        if window is None:
            return None
        rect = _rect_of(window)
        self._focused_rect = rect
        title = str(_attr(window, "AXTitle") or "")
        for token, entry in self._tokens.items():
            if entry["pid"] == app.processIdentifier() and entry["title"] == title:
                entry["ref"], entry["rect"] = window, rect
                return token
        return None

    def work_area(self) -> tuple[int, int, int, int]:
        screens = AppKit.NSScreen.screens()
        if not screens:
            return (0, 0, 1440, 900)
        primary = screens[0].frame()
        base = primary.origin.y + primary.size.height
        point = None
        if self._focused_rect:
            rect = self._focused_rect
            point = (rect[0] + rect[2] / 2, rect[1] + rect[3] / 2)
        for screen in screens:
            frame = screen.frame()
            x, y = frame.origin.x, base - (frame.origin.y + frame.size.height)
            w, h = frame.size.width, frame.size.height
            if point is None or (x <= point[0] < x + w and y <= point[1] < y + h):
                visible = screen.visibleFrame()
                vx, vw = visible.origin.x, visible.size.width
                vy = base - (visible.origin.y + visible.size.height)
                return (round(vx), round(vy), round(vw), round(visible.size.height))
        return (0, 0, round(primary.size.width), round(primary.size.height))

    def set_rect(self, token, rect):
        entry = self._entry(token)
        if entry is None:
            return
        ref = entry["ref"]
        x, y, w, h = (round(v) for v in rect)
        _set(ref, "AXSize", AX.AXValueCreate(AX.kAXValueTypeCGSize, (w, h)))
        _set(ref, "AXPosition", AX.AXValueCreate(AX.kAXValueTypeCGPoint, (x, y)))

    def apply(self, ops):
        for op in ops:
            kind = op[0]
            if kind == "rect":
                self.set_rect(op[1], op[2])
            elif kind == "hide":
                entry = self._entry(op[1])
                if entry is not None:
                    _set(entry["ref"], "AXMinimized", True)
            elif kind == "show":
                entry = self._entry(op[1])
                if entry is not None:
                    _set(entry["ref"], "AXMinimized", False)
            elif kind == "focus":
                self._focus(op[1])

    def _focus(self, token):
        entry = self._entry(token)
        if entry is None:
            return
        _set(entry["ref"], "AXMinimized", False)
        _set(entry["ref"], "AXMain", True)
        try:
            AX.AXUIElementPerformAction(entry["ref"], "AXRaise")
        except Exception:
            pass
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(
            entry["pid"])
        if app is not None:
            app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)


class MacTilingTap:
    """Standalone CGEventTap: tiling chords and plain-mouse drags on this Mac
    while no KVM session controls it."""

    def __init__(self, controller, status=print):
        self.controller = controller
        self.status = status
        self._thread: threading.Thread | None = None
        self._runloop = None
        self._tap = None
        self._stopping = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="kvm-tiling-tap", daemon=True)
        self._thread.start()

    def stop(self):
        self._stopping.set()
        if self._runloop is not None:
            Quartz.CFRunLoopStop(self._runloop)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(3)

    def _run(self):
        codes = (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp,
                 Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp,
                 Quartz.kCGEventLeftMouseDragged)
        mask = 0
        for code in codes:
            mask |= Quartz.CGEventMaskBit(code)
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGHIDEventTap, Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault, mask, self._callback, None)
        if self._tap is None:
            self.status("Tiling tap unavailable — grant Accessibility access")
            return
        source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        loop = Quartz.CFRunLoopGetCurrent()
        self._runloop = loop
        Quartz.CFRunLoopAddSource(loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)
        try:
            Quartz.CFRunLoopRun()
        finally:
            Quartz.CGEventTapEnable(self._tap, False)
            Quartz.CFRunLoopRemoveSource(loop, source, Quartz.kCFRunLoopCommonModes)
            Quartz.CFMachPortInvalidate(self._tap)
            self._runloop = None

    def _callback(self, _proxy, kind, event, _refcon):
        try:
            if kind == Quartz.kCGEventTapDisabledByTimeout:
                Quartz.CGEventTapEnable(self._tap, True)
                return event
            controller = self.controller
            if not controller.enabled or controller.remote_active:
                return event
            if kind in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
                return self._key(kind, event)
            return self._mouse(kind, event)
        except Exception:
            return event

    def _key(self, kind, event):
        if kind != Quartz.kCGEventKeyDown:
            return event
        code = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))
        vk = MAC_TO_VK.get(code)
        if vk is None:
            return event
        flags = Quartz.CGEventGetFlags(event)
        mods = frozenset(mod for held, mod in (
            (flags & Quartz.kCGEventFlagMaskControl, "ctrl"),
            (flags & Quartz.kCGEventFlagMaskAlternate, "alt"),
            (flags & Quartz.kCGEventFlagMaskCommand, "win"),
            (flags & Quartz.kCGEventFlagMaskShift, "shift")) if held)
        action = chord_action(vk, mods)
        if action is None:
            return event
        self.controller.handle(action)
        return None  # swallow: the OS desktop switchers never see it

    def _mouse(self, kind, event):
        controller = self.controller
        location = Quartz.CGEventGetLocation(event)
        x, y = round(location.x), round(location.y)
        if kind == Quartz.kCGEventLeftMouseDown:
            if controller.press(x, y, "left"):
                return None
        elif kind == Quartz.kCGEventLeftMouseDragged:
            if controller.drag.active:
                controller.move(x, y)
                return None
        elif kind == Quartz.kCGEventLeftMouseUp:
            if controller.drag.active:
                controller.release(x, y)
                return None
        return event
