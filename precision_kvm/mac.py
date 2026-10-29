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
import threading
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .scroll import SmoothScrollEngine

from .keymap import KeyMapper, effective_vk, mac_keycode as mac_keycode


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
        self.mapper = KeyMapper()
        self._held_sources: dict[tuple[int,int,bool], tuple[str,int] | None] = {}
        self._target_counts: dict[tuple[str,int], int] = {}
        self.scroll_gain_x = self.scroll_gain_y = 1.0
        self.scroll_invert_x = self.scroll_invert_y = False
        self.scroll_mode = "pixel"
        self.windows_home_end = False
        self.windows_word_nav = False
        self.smooth_scroll = False
        self.smooth_scroll_ms = 60.0
        self.smooth_scroll_hz = 120
        self._scroll_lock = threading.RLock()
        self._scroll_flags = 0
        self._smooth: SmoothScrollEngine | None = None
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
                            ((55,54),q.kCGEventFlagMaskCommand),
                            ((63,),q.kCGEventFlagMaskSecondaryFn)):
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

    def _mapped_key(self,event):
        source = (effective_vk(event),int(event.get("scan",0)),bool(event.get("extended",False)))
        down = bool(event["down"])
        if down:
            repeat = source in self._held_sources
            target = self._held_sources[source] if repeat else (
                self._navigation_target(event) or self.mapper.target(event))
            if not repeat:
                self._held_sources[source] = target
                if target is not None:
                    self._target_counts[target] = self._target_counts.get(target,0)+1
            if target is not None and (repeat or self._target_counts[target]==1):
                self._dispatch_target(target,True,repeat)
        else:
            target = self._held_sources.pop(source,None)
            if target is not None:
                count = self._target_counts[target]-1
                if count:
                    self._target_counts[target] = count
                else:
                    self._target_counts.pop(target,None)
                    self._dispatch_target(target,False)

    def _dispatch_target(self,target,down,repeat=False):
        kind,code = target
        if kind == "key":
            self._key(code,down)
        elif kind == "media":
            self._media(code,down)
        else:
            flags,key = code >> 8,code & 255
            event = self.q.CGEventCreateKeyboardEvent(self.source,key,down)
            if event is None:
                raise RuntimeError("macOS refused an editing shortcut event")
            self.q.CGEventSetFlags(event,flags)
            self.q.CGEventSetIntegerValueField(event,self.q.kCGKeyboardEventAutorepeat,int(repeat))
            self.q.CGEventPost(self.q.kCGHIDEventTap,event)

    def _navigation_target(self,event):
        if self.mapper.has_override(event):
            return None
        vk = effective_vk(event)
        ctrl = any(source[0] in (0xA2,0xA3) and target in (("key",59),("key",62))
                   for source,target in self._held_sources.items())
        flags = self._flags()
        shift = flags & self.q.kCGEventFlagMaskShift
        q = self.q
        chords = flags & (q.kCGEventFlagMaskAlternate | q.kCGEventFlagMaskCommand
                          | q.kCGEventFlagMaskSecondaryFn)
        if chords:
            return None
        if self.windows_home_end and vk in (0x24,0x23):
            key = (126 if vk==0x24 else 125) if ctrl else (123 if vk==0x24 else 124)
            flags = shift | self.q.kCGEventFlagMaskCommand
        elif self.windows_word_nav and ctrl and vk in (0x25,0x27,0x08,0x2E):
            key = {0x25:123,0x27:124,0x08:51,0x2E:117}[vk]
            flags = shift | self.q.kCGEventFlagMaskAlternate
        else:
            return None
        # Avoid releasing a destination already held by an unrelated mapped key.
        if key in self.keys:
            return None
        return "shortcut",key | (flags << 8)

    def _scroll_step(self,dx,dy):
        with self._scroll_lock:
            if not self.active:
                return
            self.scroll_x += dx
            self.scroll_y += dy
            x = math.trunc(self.scroll_x+math.copysign(1e-9,self.scroll_x))
            y = math.trunc(self.scroll_y+math.copysign(1e-9,self.scroll_y))
            self.scroll_x -= x
            self.scroll_y -= y
            if x or y:
                q = self.q
                unit = q.kCGScrollEventUnitPixel if self.scroll_mode=="pixel" else q.kCGScrollEventUnitLine
                event = q.CGEventCreateScrollWheelEvent(self.source,unit,2,y,-x)
                if event is None:
                    raise RuntimeError("macOS refused a scroll event")
                q.CGEventSetFlags(event,self._scroll_flags)
                q.CGEventSetIntegerValueField(event,q.kCGEventSourceUserData,0x50524B56)
                q.CGEventPost(q.kCGHIDEventTap,event)

    def _gesture(self,action):
        """Discrete OS shortcut equivalent, with held state preserved.

        Flags are applied only to temporary events. Existing physical modifier
        downs are not released/repressed, avoiding transient app interactions.
        macOS shortcut preferences must retain the corresponding bindings.
        """
        gestures = {"desktop_left":(self.q.kCGEventFlagMaskControl,123),
                    "desktop_right":(self.q.kCGEventFlagMaskControl,124),
                    "mission_control":(self.q.kCGEventFlagMaskControl,126),
                    "app_expose":(self.q.kCGEventFlagMaskControl,125),
                    "show_desktop":(0,103),
                    "lock_screen":(self.q.kCGEventFlagMaskControl
                                   | self.q.kCGEventFlagMaskCommand,12)}
        if action not in gestures:
            return
        flags,code = gestures[action]
        # A physically held destination is left alone rather than releasing it.
        if code in self.keys:
            return
        for down in (True,False):
            event = self.q.CGEventCreateKeyboardEvent(self.source,code,down)
            if event is None:
                raise RuntimeError("macOS refused to construct a gesture shortcut")
            self.q.CGEventSetFlags(event,flags)
            self.q.CGEventPost(self.q.kCGHIDEventTap,event)

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
            self.mapper = KeyMapper(event.get("keymap",{}),event.get(
                "keymap_preset","ctrl_cmd" if self.swap_ctrl_cmd else "native"))
            self.scroll_gain_x = float(event.get("scroll_gain_x",self.scroll_gain))
            self.scroll_gain_y = float(event.get("scroll_gain_y",self.scroll_gain))
            if not all(math.isfinite(v) and 0.01 <= v <= 20
                       for v in (self.scroll_gain_x,self.scroll_gain_y)):
                raise ValueError("Scroll gains must be finite and between 0.01 and 20")
            self.scroll_invert_x = bool(event.get("scroll_invert_x",False))
            self.scroll_invert_y = bool(event.get("scroll_invert_y",False))
            self.scroll_mode = event.get("scroll_mode","pixel")
            if self.scroll_mode not in ("pixel","line"):
                raise ValueError("Scroll mode must be pixel or line")
            self.windows_home_end = bool(event.get("windows_home_end",False))
            self.windows_word_nav = bool(event.get("windows_word_nav",False))
            self.smooth_scroll = bool(event.get("smooth_scroll",False))
            self.smooth_scroll_ms = float(event.get("smooth_scroll_ms",60))
            self.smooth_scroll_hz = int(event.get("smooth_scroll_hz",120))
            if (not math.isfinite(self.smooth_scroll_ms) or not 10 <= self.smooth_scroll_ms <= 150
                    or not 30 <= self.smooth_scroll_hz <= 240):
                raise ValueError("Invalid smooth scroll timing")
            if self._smooth:
                self._smooth.stop()
                self._smooth = None
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
        if kind == "lock":
            self.release_all()
            self._gesture("lock_screen")
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
            self._mapped_key(event)
        elif kind == "gesture":
            self._gesture(event.get("action"))
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
            factor = 30 if self.scroll_mode=="pixel" else 3
            dx,dy = float(event.get("dx",0)),float(event.get("dy",0))
            if not math.isfinite(dx) or not math.isfinite(dy):
                raise ValueError("Non-finite scroll movement")
            dx = dx/120*factor*self.scroll_gain_x*(-1 if self.scroll_invert_x else 1)
            dy = dy/120*factor*self.scroll_gain_y*(-1 if self.scroll_invert_y else 1)
            flags = self._flags()
            if flags != self._scroll_flags and self._smooth:
                self._smooth.cancel()
            self._scroll_flags = flags
            if self.smooth_scroll and self.scroll_mode=="pixel":
                if self._smooth is None:
                    from .scroll import SmoothScrollEngine
                    self._smooth = SmoothScrollEngine(self._scroll_step,rate_hz=self.smooth_scroll_hz,
                                                      duration_ms=self.smooth_scroll_ms)
                self._smooth.add(dx,dy)
            else:
                self._scroll_step(dx,dy)

    def release_all(self):
        """Release injected state even if this session is already inactive."""
        with self._scroll_lock:
            self.active = False
        if self._smooth:
            self._smooth.cancel()
        for (kind,code),count in tuple(self._target_counts.items()):
            if kind == "shortcut":
                self._dispatch_target((kind,code),False)
        self._held_sources.clear()
        self._target_counts.clear()
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

    def close(self):
        self.release_all()
        if self._smooth:
            self._smooth.stop()
            self._smooth = None
