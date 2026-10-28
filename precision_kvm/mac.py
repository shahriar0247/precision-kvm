"""macOS input injection. Importing this module never posts input events.

Requires pyobjc-framework-Quartz and pyobjc-framework-Cocoa on macOS.
The mappings preserve physical US keyboard positions; the active Mac layout
decides which character is typed. Windows and Control remain Command and Control.
"""
from __future__ import annotations

import sys
import time
import math
import ctypes
from typing import Callable

# Windows set-1 scan codes -> Apple virtual key codes.
SCAN_TO_MAC = dict(zip(
    (0x1E,0x30,0x2E,0x20,0x12,0x21,0x22,0x23,0x17,0x24,0x25,0x26,0x32,
     0x31,0x18,0x19,0x10,0x13,0x1F,0x14,0x16,0x2F,0x11,0x2D,0x15,0x2C,
     0x02,0x03,0x04,0x05,0x06,0x07,0x08,0x09,0x0A,0x0B,
     0x0C,0x0D,0x1A,0x1B,0x2B,0x27,0x28,0x29,0x33,0x34,0x35),
    (0,11,8,2,14,3,5,4,34,38,40,37,46,45,31,35,12,15,1,17,32,9,13,7,16,6,
     18,19,20,21,23,22,26,28,25,29,27,24,33,30,42,41,39,50,43,47,44)))
VK_TO_MAC = {
    0x08:51,0x09:48,0x0D:36,0x1B:53,0x20:49,0x14:57,
    0x21:116,0x22:121,0x23:119,0x24:115,0x25:123,0x26:126,
    0x27:124,0x28:125,0x2D:114,0x2E:117,
    0x5B:55,0x5C:54,0xA0:56,0xA1:60,0xA2:59,0xA3:62,0xA4:58,0xA5:61,
    0x60:82,0x61:83,0x62:84,0x63:85,0x64:86,0x65:87,0x66:88,
    0x67:89,0x68:91,0x69:92,0x6A:67,0x6B:69,0x6D:78,0x6E:65,0x6F:75,
    0x70:122,0x71:120,0x72:99,0x73:118,0x74:96,0x75:97,
    0x76:98,0x77:100,0x78:101,0x79:109,0x7A:103,0x7B:111,
}
MEDIA_KEYS = {0xAD:7,0xAE:1,0xAF:0,0xB0:17,0xB1:18,0xB2:16,0xB3:16}


def mac_keycode(vk: int, scan: int = 0, extended: bool = False) -> int | None:
    """Translate special VKs first so extended navigation is unambiguous."""
    if vk == 0x0D and extended:
        return 76
    if vk == 0x10:
        return 60 if scan == 0x36 else 56
    if vk == 0x11:
        return 62 if extended else 59
    if vk == 0x12:
        return 61 if extended else 58
    if vk in VK_TO_MAC:
        return VK_TO_MAC[vk]
    if scan in SCAN_TO_MAC:
        return SCAN_TO_MAC[scan]
    # Support synthetic producers that only provide a virtual-key code.
    alphabet = dict(zip(range(65,91), (0,11,8,2,14,3,5,4,34,38,40,37,46,
                                     45,31,35,12,15,1,17,32,9,13,7,16,6)))
    return alphabet.get(vk)


class MacInjector:
    """One-thread-owned injector with explicit release on every disconnect.

    on_return receives {'type':'return', 'normalized_y': float} when the
    pointer crosses the edge adjoining the Windows machine. No polling loop
    or queued cursor animation is used: one received move is one CG event.
    """

    def __init__(self, on_return: Callable[[dict], None] | None = None,
                 side: str = "left", quartz=None, appkit=None):
        if quartz is None:
            if sys.platform != "darwin":
                raise RuntimeError("MacInjector requires macOS")
            import Quartz as quartz_module
            import AppKit as appkit_module
            quartz, appkit = quartz_module, appkit_module
        self.q, self.appkit = quartz, appkit
        self.on_return = on_return or (lambda event: None)
        if side not in ("left", "right", "top", "bottom"):
            raise ValueError("Invalid monitor side")
        self.side = side
        self.active = False
        self.gain = 1.0
        self.scroll_gain = 1.0
        self.swap_ctrl_cmd = False
        self.keys: set[int] = set()
        self.media: set[int] = set()
        self.buttons: set[int] = set()
        self.scroll_x = self.scroll_y = 0.0
        self.last_click = (0, 0.0, 0)
        self.source = quartz.CGEventSourceCreate(quartz.kCGEventSourceStatePrivate)
        quartz.CGEventSourceSetLocalEventsSuppressionInterval(self.source, 0.0)
        self.refresh_bounds()

    @staticmethod
    def accessibility_available(prompt: bool = False) -> bool:
        if sys.platform != "darwin":
            return False
        # AX belongs to ApplicationServices, not the Quartz PyObjC module.
        # Use its stable C ABI so checking permission needs no extra PyObjC
        # framework package and never creates or posts an input event.
        application = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        application.AXIsProcessTrusted.argtypes = []
        application.AXIsProcessTrusted.restype = ctypes.c_bool
        if not prompt:
            return bool(application.AXIsProcessTrusted())
        core = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        core.CFDictionaryCreate.argtypes = [ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
        core.CFDictionaryCreate.restype = ctypes.c_void_p
        core.CFRelease.argtypes = [ctypes.c_void_p]
        core.CFRelease.restype = None
        key = ctypes.c_void_p.in_dll(application,"kAXTrustedCheckOptionPrompt")
        value = ctypes.c_void_p.in_dll(core,"kCFBooleanTrue")
        keys = (ctypes.c_void_p * 1)(key.value)
        values = (ctypes.c_void_p * 1)(value.value)
        options = core.CFDictionaryCreate(None,keys,values,1,None,None)
        if not options:
            raise RuntimeError("Could not create Accessibility permission options")
        application.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        application.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        try:
            return bool(application.AXIsProcessTrustedWithOptions(options))
        finally:
            core.CFRelease(options)

    def refresh_bounds(self):
        bounds = self.q.CGDisplayBounds(self.q.CGMainDisplayID())
        self.left, self.top = float(bounds.origin.x), float(bounds.origin.y)
        self.width, self.height = float(bounds.size.width), float(bounds.size.height)
        pos = self.q.CGEventGetLocation(self.q.CGEventCreate(None))
        self.x, self.y = float(pos.x), float(pos.y)

    def _flags(self) -> int:
        q = self.q
        flags = 0
        for codes, flag in (((56,60),q.kCGEventFlagMaskShift),
                            ((59,62),q.kCGEventFlagMaskControl),
                            ((58,61),q.kCGEventFlagMaskAlternate),
                            ((55,54),q.kCGEventFlagMaskCommand)):
            if self.keys.intersection(codes):
                flags |= flag
        return flags

    def _post(self, event):
        if event is None:
            raise RuntimeError("macOS refused to construct an input event")
        self.q.CGEventSetFlags(event, self._flags())
        self.q.CGEventPost(self.q.kCGHIDEventTap, event)

    def _mouse(self, kind, button=0, dx=0, dy=0, click_count=0):
        q = self.q
        event = q.CGEventCreateMouseEvent(self.source, kind, (self.x,self.y), button)
        q.CGEventSetIntegerValueField(event, q.kCGMouseEventDeltaX, round(dx))
        q.CGEventSetIntegerValueField(event, q.kCGMouseEventDeltaY, round(dy))
        if click_count:
            q.CGEventSetIntegerValueField(event,q.kCGMouseEventClickState,click_count)
        self._post(event)

    def _key(self, code: int, down: bool):
        repeat = down and code in self.keys
        if down:
            self.keys.add(code)
        else:
            self.keys.discard(code)
        event = self.q.CGEventCreateKeyboardEvent(self.source,code,down)
        self.q.CGEventSetIntegerValueField(event,self.q.kCGKeyboardEventAutorepeat,int(repeat))
        self._post(event)

    def _media(self, code: int, down: bool):
        if self.appkit is None:
            raise RuntimeError("AppKit is required for media keys")
        if down:
            self.media.add(code)
        else:
            self.media.discard(code)
        event = self.appkit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14, (0,0), 0xA00 if down else 0xB00, 0, 0, None, 8,
            (code << 16) | ((0xA if down else 0xB) << 8), -1)
        if event is None:
            raise RuntimeError("macOS refused a media event")
        self.q.CGEventPost(self.q.kCGHIDEventTap,event.CGEvent())

    def handle(self, event: dict):
        q = self.q
        kind = event.get("type")
        if kind == "activate":
            self.release_all()
            self.refresh_bounds()
            side = event.get("side",self.side)
            if side not in ("left","right","top","bottom"):
                raise ValueError("Invalid monitor side")
            self.side = side
            self.gain = float(event.get("gain",1.0))
            self.scroll_gain = float(event.get("scroll_gain",1.0))
            if not all(math.isfinite(v) and 0.01 <= v <= 20 for v in (self.gain,self.scroll_gain)):
                raise ValueError("Input gains must be finite and between 0.01 and 20")
            self.swap_ctrl_cmd = bool(event.get("swap_ctrl_cmd",False))
            axis = "normalized_x" if self.side in ("top","bottom") else "normalized_y"
            fraction = max(0.0,min(1.0,float(event.get(axis,event.get("normalized_y",0.5)))))
            self.x = self.left + (self.width-2 if self.side == "left" else 1)
            self.y = self.top + fraction*(self.height-1)
            if self.side in ("top","bottom"):
                self.x = self.left + fraction*(self.width-1)
                self.y = self.top + (self.height-2 if self.side == "top" else 1)
            self.active = True
            self._mouse(q.kCGEventMouseMoved)
            return
        if kind in ("deactivate","release"):
            self.release_all()
            return
        if not self.active:
            return
        if kind == "move":
            dx,dy = float(event.get("dx",0))*self.gain,float(event.get("dy",0))*self.gain
            if not math.isfinite(dx) or not math.isfinite(dy):
                raise ValueError("Non-finite mouse movement")
            nx,ny = self.x+dx,self.y+dy
            crossing = ((self.side == "left" and nx >= self.left+self.width-1 and dx>0)
                        or (self.side == "right" and nx <= self.left and dx<0)
                        or (self.side == "top" and ny >= self.top+self.height-1 and dy>0)
                        or (self.side == "bottom" and ny <= self.top and dy<0))
            if crossing and not self.buttons:
                fraction = ((self.y-self.top)/max(1,self.height-1) if self.side in ("left","right")
                            else (self.x-self.left)/max(1,self.width-1))
                self.release_all()
                message = {"type":"return","normalized_y":max(0.0,min(1.0,fraction))}
                if self.side in ("top","bottom"):
                    message["normalized_x"] = message["normalized_y"]
                self.on_return(message)
                return
            self.x = max(self.left,min(self.left+self.width-1,nx))
            self.y = max(self.top,min(self.top+self.height-1,ny))
            button = min(self.buttons) if self.buttons else 0
            move = (q.kCGEventLeftMouseDragged if button==0 else
                    q.kCGEventRightMouseDragged if button==1 else q.kCGEventOtherMouseDragged)
            self._mouse(move if self.buttons else q.kCGEventMouseMoved,button,dx,dy)
        elif kind == "key":
            vk,down = int(event["vk"]),bool(event["down"])
            if vk in MEDIA_KEYS:
                self._media(MEDIA_KEYS[vk],down)
            else:
                code = mac_keycode(vk,int(event.get("scan",0)),bool(event.get("extended",False)))
                if code is not None:
                    if self.swap_ctrl_cmd:
                        code = {59:55,62:54,55:59,54:62}.get(code,code)
                    self._key(code,down)
        elif kind == "button":
            button = event["button"]
            button = {"left":0,"right":1,"middle":2,"x1":3,"x2":4}.get(button,button)
            button = int(button)
            if button not in range(5):
                return
            down = bool(event["down"])
            if down:
                self.buttons.add(button)
                old,when,count = self.last_click
                count = count+1 if old==button and time.monotonic()-when<0.45 else 1
                self.last_click = (button,time.monotonic(),count)
            else:
                self.buttons.discard(button)
                count = self.last_click[2]
            types = ((q.kCGEventLeftMouseDown,q.kCGEventLeftMouseUp) if button==0 else
                     (q.kCGEventRightMouseDown,q.kCGEventRightMouseUp) if button==1 else
                     (q.kCGEventOtherMouseDown,q.kCGEventOtherMouseUp))
            self._mouse(types[0 if down else 1],button,click_count=count)
        elif kind == "scroll":
            # Windows WHEEL_DELTA=120; preserve high-resolution partial notches.
            self.scroll_x += float(event.get("dx",0))/120*30*self.scroll_gain
            self.scroll_y += float(event.get("dy",0))/120*30*self.scroll_gain
            x,y = int(self.scroll_x),int(self.scroll_y)
            self.scroll_x -= x
            self.scroll_y -= y
            if x or y:
                self._post(q.CGEventCreateScrollWheelEvent(self.source,q.kCGScrollEventUnitPixel,2,y,-x))

    def release_all(self):
        """Release injected state even if this session is already inactive."""
        for key in tuple(self.keys):
            self._key(key,False)
        for key in tuple(self.media):
            self._media(key,False)
        q = self.q
        for button in tuple(self.buttons):
            self.buttons.discard(button)
            kind = (q.kCGEventLeftMouseUp if button==0 else
                    q.kCGEventRightMouseUp if button==1 else q.kCGEventOtherMouseUp)
            self._mouse(kind,button)
        self.scroll_x = self.scroll_y = 0.0
        self.active = False
