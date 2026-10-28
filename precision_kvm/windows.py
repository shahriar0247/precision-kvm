"""Windows native capture. Importing this module never installs input hooks.

The callback must enqueue events without blocking. Return False to reject a
handoff; raising an exception immediately restores local control.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
import threading
import time
from typing import Any, Callable

from .gestures import GestureShortcutBridge
from .safety import EmergencyMonitor
from .touchpad import WindowsTouchpads


class WindowsCapture:
    """Share Windows input when crossing the configured primary-screen edge.

    Ctrl+Alt+F12 always returns control locally. Elevated applications and the
    Windows secure desktop are subject to Windows input isolation.
    """

    def __init__(self, on_event: Callable[[dict], object], side: str = "left",
                 high_resolution_timer: bool = False, on_emergency=None,
                 emergency_escape_seconds=1.5, emergency_click_count=6,
                 emergency_click_window=1.2, mouse_mode="raw", gesture_bridge=True,
                 native_touchpad=False):
        if side not in {"left", "right", "top", "bottom"}:
            raise ValueError("side must be left, right, top, or bottom")
        self.on_event = on_event
        self.side = side
        self.high_resolution_timer = high_resolution_timer
        if mouse_mode not in ("raw", "system"):
            raise ValueError("mouse_mode must be raw or system")
        self.mouse_mode = mouse_mode
        self.on_emergency = on_emergency or (lambda reason: None)
        self.safety = EmergencyMonitor(emergency_escape_seconds, emergency_click_count,
                                       emergency_click_window)
        self.gesture_bridge = gesture_bridge
        self.gestures = GestureShortcutBridge()
        self.native_touchpad = bool(native_touchpad)
        self.touchpads: WindowsTouchpads | None = None
        self._emergency_requested = False
        self.active = False
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._thread_id = 0
        self._keys: set[int] = set()
        self._buttons: set[str] = set()
        self._cooldown = 0.0
        self._anchor = (0, 0)
        self._entry_y = 0.5
        self._entry_x = 0.5
        self._hooks: list[Any] = []

    def start(self):
        if sys.platform != "win32":
            raise OSError("WindowsCapture requires Windows")
        if self._thread and self._thread.is_alive():
            return
        self.error = None
        self._ready.clear()
        self._stopping.clear()
        self._emergency_requested = False
        self.safety.reset()
        self.gestures.reset()
        if self.touchpads:
            self.touchpads.reset()
        self._thread = threading.Thread(target=self._run, name="kvm-native-hooks", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            self.stop()
            raise RuntimeError("Windows hook startup timed out")
        if self.error:
            raise RuntimeError(self.error)

    def stop(self):
        self._stopping.set()
        self.release()
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(3)

    def release(self, normalized_y=None, normalized_x=None):
        """Fail-safe local control, callable from any thread."""
        if normalized_y is not None:
            self._entry_y = min(1.0, max(0.0, float(normalized_y)))
        if normalized_x is not None:
            self._entry_x = min(1.0, max(0.0, float(normalized_x)))
        was_active = self.active
        self.active = False
        self.safety.reset()
        self.gestures.reset()
        if self.touchpads:
            self.touchpads.reset()
        self._cooldown = time.monotonic() + 0.6
        if was_active:
            # Place inside the entry edge, preventing immediate re-entry.
            x, y = self._return_point()
            ctypes.windll.user32.SetCursorPos(x, y)
            try:
                self.on_event({"type": "deactivate"})
            except Exception as exc:
                self.error = str(exc)

    return_local = release

    def _emergency_stop(self, reason):
        if self._emergency_requested:
            return
        self._emergency_requested = True
        self._stopping.set()
        self.release()
        # Parent stops the listener/session. Never block the hook message pump
        # on a network shutdown, GUI operation or join from within a callback.
        threading.Thread(target=self.on_emergency, args=(reason,), daemon=True,
                         name="kvm-emergency-stop").start()

    def _return_point(self):
        width, height = self._size
        x = min(width - 2, max(1, round(self._entry_x * (width - 1))))
        y = min(height - 2, max(1, round(self._entry_y * (height - 1))))
        if self.side == "left":
            x = 2
        elif self.side == "right":
            x = width - 3
        elif self.side == "top":
            y = 2
        else:
            y = height - 3
        return x, y

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

    def _at_edge(self, x, y):
        width, height = self._size
        return {"left": x <= 0, "right": x >= width - 1,
                "top": y <= 0, "bottom": y >= height - 1}[self.side]

    def _run(self):
        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        # Raw pixels and edge coordinates must use the same DPI coordinate space.
        if hasattr(user, "SetThreadDpiAwarenessContext"):
            user.SetThreadDpiAwarenessContext.argtypes = [wintypes.HANDLE]
            user.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
            user.SetThreadDpiAwarenessContext(wintypes.HANDLE(-4))
        # All pointer-sized callback and API values are explicitly declared.
        result_t = ctypes.c_ssize_t
        hook_t = ctypes.WINFUNCTYPE(result_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        class Mouse(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("data", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("extra", ctypes.c_size_t)]

        class Keyboard(ctypes.Structure):
            _fields_ = [("vk", wintypes.DWORD), ("scan", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("extra", ctypes.c_size_t)]

        user.SetWindowsHookExW.argtypes = [ctypes.c_int, hook_t, wintypes.HINSTANCE, wintypes.DWORD]
        user.SetWindowsHookExW.restype = wintypes.HANDLE
        user.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        user.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user.CallNextHookEx.restype = result_t
        kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel.GetModuleHandleW.restype = wintypes.HMODULE
        kernel.GetCurrentThreadId.restype = wintypes.DWORD
        user.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user.GetMessageW.restype = wintypes.BOOL
        user.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        user.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self._size = (user.GetSystemMetrics(0), user.GetSystemMetrics(1))
        self._anchor = (self._size[0] // 2, self._size[1] // 2)

        def next_hook(code, message, pointer):
            return user.CallNextHookEx(None, code, message, pointer)

        @hook_t
        def mouse_hook(code, message, pointer):
            if code < 0:
                return next_hook(code, message, pointer)
            try:
                data = ctypes.cast(pointer, ctypes.POINTER(Mouse)).contents
                if data.flags & 1:  # Never re-capture injected input.
                    return next_hook(code, message, pointer)
                if message == 0x0200:
                    x, y = data.pt.x, data.pt.y
                    if not self.active:
                        if (not self._stopping.is_set() and self._at_edge(x, y)
                                and not self._keys and not self._buttons
                                and time.monotonic() >= self._cooldown):
                            self._entry_x = x / max(1, self._size[0] - 1)
                            self._entry_y = y / max(1, self._size[1] - 1)
                            self.active = True
                            if self._emit({"type": "activate", "normalized_y": self._entry_y,
                                           "normalized_x": self._entry_x, "side": self.side}):
                                user.SetCursorPos(*self._anchor)
                                return 1
                    else:
                        if self.mouse_mode == "system":
                            dx, dy = x - self._anchor[0], y - self._anchor[1]
                            if dx or dy:
                                self._emit({"type": "move", "dx": dx, "dy": dy})
                        # Returning 1 leaves the local pointer at its anchor.
                        # Raw mode obtains hardware deltas solely via WM_INPUT.
                        return 1
                buttons = {0x0201: ("left", True), 0x0202: ("left", False),
                           0x0204: ("right", True), 0x0205: ("right", False),
                           0x0207: ("middle", True), 0x0208: ("middle", False)}
                if message in (0x020B, 0x020C):
                    button = "x1" if (data.data >> 16) == 1 else "x2"
                    buttons[message] = (button, message == 0x020B)
                if message in buttons:
                    button, down = buttons[message]
                    self._buttons.add(button) if down else self._buttons.discard(button)
                    if self.active:
                        reason = self.safety.click(button, down)
                        if reason:
                            self._emergency_stop(reason)
                            return 1
                        self._emit({"type": "button", "button": button, "down": down})
                        return 1
                if self.active and message in (0x020A, 0x020E):
                    delta = ctypes.c_short(data.data >> 16).value
                    self._emit({"type": "scroll", "dx": delta if message == 0x020E else 0,
                                "dy": delta if message == 0x020A else 0})
                    return 1
            except Exception as exc:
                self.error = str(exc)
                self.release()
            return next_hook(code, message, pointer)

        @hook_t
        def keyboard_hook(code, message, pointer):
            if code < 0:
                return next_hook(code, message, pointer)
            try:
                data = ctypes.cast(pointer, ctypes.POINTER(Keyboard)).contents
                down = message in (0x0100, 0x0104)
                if data.flags & 0x10:
                    if self.active and self.gesture_bridge:
                        # Native contacts take precedence: the system may emit
                        # a shortcut after the raw swipe, which must not repeat it.
                        action = None if self.native_touchpad else self.gestures.feed(data.vk, down)
                        if action:
                            self._emit({"type": "gesture", "action": action})
                        # Reserve injected input locally while sharing. Unknown
                        # sequences are discarded, never sent to either desktop.
                        return 1
                    return next_hook(code, message, pointer)
                self._keys.add(data.vk) if down else self._keys.discard(data.vk)
                if self.active:
                    reason = self.safety.key(data.vk, down)
                    if reason:
                        self._emergency_stop(reason)
                        return 1
                ctrl = bool(self._keys & {0x11, 0xA2, 0xA3})
                alt = bool(self._keys & {0x12, 0xA4, 0xA5})
                if data.vk == 0x7B and down and ctrl and alt:
                    self.release()
                    return 1
                if self.active:
                    self._emit({"type": "key", "vk": data.vk, "scan": data.scan,
                                "down": down, "extended": bool(data.flags & 1)})
                    return 1
            except Exception as exc:
                self.error = str(exc)
                self.release()
            return next_hook(code, message, pointer)

        # Keep callback objects alive until unhook has completed.
        self._callbacks = (mouse_hook, keyboard_hook)
        window = None
        class_name = "PrecisionKVMRawInput_" + str(id(self))
        wndproc_t = ctypes.WINFUNCTYPE(result_t, wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM)

        class WindowClass(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("proc", wndproc_t),
                        ("class_extra", ctypes.c_int), ("window_extra", ctypes.c_int),
                        ("instance", wintypes.HINSTANCE), ("icon", wintypes.HANDLE),
                        ("cursor", wintypes.HANDLE), ("background", wintypes.HANDLE),
                        ("menu", wintypes.LPCWSTR), ("name", wintypes.LPCWSTR)]

        class RawDevice(ctypes.Structure):
            _fields_ = [("page", wintypes.USHORT), ("usage", wintypes.USHORT),
                        ("flags", wintypes.DWORD), ("target", wintypes.HWND)]

        class RawHeader(ctypes.Structure):
            _fields_ = [("kind", wintypes.DWORD), ("size", wintypes.DWORD),
                        ("device", wintypes.HANDLE), ("parameter", wintypes.WPARAM)]

        user.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user.DefWindowProcW.restype = result_t
        user.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
        user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                        wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                        wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user.CreateWindowExW.restype = wintypes.HWND
        user.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID,
                                        ctypes.POINTER(wintypes.UINT), wintypes.UINT]
        user.GetRawInputData.restype = wintypes.UINT
        user.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RawDevice), wintypes.UINT, wintypes.UINT]
        user.DestroyWindow.argtypes = [wintypes.HWND]
        user.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user.DispatchMessageW.restype = result_t
        user.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user.TranslateMessage.restype = wintypes.BOOL
        user.PeekMessageW.restype = wintypes.BOOL
        user.RegisterClassW.restype = wintypes.ATOM
        user.UnregisterClassW.restype = wintypes.BOOL
        user.DestroyWindow.restype = wintypes.BOOL
        user.RegisterRawInputDevices.restype = wintypes.BOOL
        user.UnhookWindowsHookEx.restype = wintypes.BOOL
        timer = None
        safety_timer = 0
        raw_buffer = ctypes.create_string_buffer(256)
        header_size = ctypes.sizeof(RawHeader)
        if self.native_touchpad:
            self.touchpads = WindowsTouchpads()

        @wndproc_t
        def window_proc(hwnd, message, wp, lp):
            nonlocal raw_buffer
            if message == 0x0113 and self.active:
                reason = self.safety.tick()
                if reason:
                    self._emergency_stop(reason)
            if message == 0x00FF and self.active:
                try:
                    size = wintypes.UINT(len(raw_buffer))
                    count = user.GetRawInputData(lp, 0x10000003, raw_buffer,
                                                 ctypes.byref(size), header_size)
                    if count == 0xFFFFFFFF and len(raw_buffer) < size.value <= 128 * 1024:
                        raw_buffer = ctypes.create_string_buffer(size.value)
                        count = user.GetRawInputData(lp, 0x10000003, raw_buffer,
                                                     ctypes.byref(size), header_size)
                    if count != 0xFFFFFFFF and count >= header_size + 24:
                        header = RawHeader.from_buffer(raw_buffer)
                        flags = ctypes.c_ushort.from_buffer(raw_buffer, header_size).value
                        if header.kind == 0 and self.mouse_mode == "raw" and not flags & 1:
                            dx = ctypes.c_long.from_buffer(raw_buffer, header_size + 12).value
                            dy = ctypes.c_long.from_buffer(raw_buffer, header_size + 16).value
                            if dx or dy:
                                self._emit({"type": "move", "dx": dx, "dy": dy})
                    if count != 0xFFFFFFFF and count >= header_size + 8 and self.touchpads:
                        header = RawHeader.from_buffer(raw_buffer)
                        if header.kind == 2:
                            report_size = ctypes.c_uint32.from_buffer(raw_buffer, header_size).value
                            report_count = ctypes.c_uint32.from_buffer(raw_buffer, header_size + 4).value
                            payload = raw_buffer.raw[header_size + 8:count]
                            for action in self.touchpads.feed(header.device, report_size, report_count, payload):
                                self._emit({"type": "gesture", "action": action})
                except Exception as exc:
                    self.error = str(exc)
                    self.release()
            return user.DefWindowProcW(hwnd, message, wp, lp)

        self._window_callback = window_proc
        try:
            msg = wintypes.MSG()
            user.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            self._thread_id = kernel.GetCurrentThreadId()
            module = kernel.GetModuleHandleW(None)
            wc = WindowClass(0, window_proc, 0, 0, module, None, None, None, None, class_name)
            if not user.RegisterClassW(ctypes.byref(wc)):
                raise ctypes.WinError(ctypes.get_last_error())
            window = user.CreateWindowExW(0, class_name, class_name, 0, 0, 0, 0, 0,
                                          wintypes.HWND(-3), None, module, None)
            if not window:
                raise ctypes.WinError(ctypes.get_last_error())
            user.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
            user.SetTimer.restype = ctypes.c_size_t
            user.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
            user.KillTimer.restype = wintypes.BOOL
            safety_timer = user.SetTimer(window, 1, 50, None)
            if not safety_timer:
                raise ctypes.WinError(ctypes.get_last_error())
            devices = [RawDevice(1, 2, 0x00000100, window)]  # RIDEV_INPUTSINK
            if self.native_touchpad:
                devices.append(RawDevice(0x0D, 0x05, 0x00000100, window))
            registered = (RawDevice * len(devices))(*devices)
            if not user.RegisterRawInputDevices(registered, len(devices), ctypes.sizeof(RawDevice)):
                raise ctypes.WinError(ctypes.get_last_error())
            for kind, callback in ((14, mouse_hook), (13, keyboard_hook)):
                handle = user.SetWindowsHookExW(kind, callback, module, 0)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._hooks.append(handle)
            if self.high_resolution_timer:
                winmm = ctypes.WinDLL("winmm")
                winmm.timeBeginPeriod.argtypes = [wintypes.UINT]
                winmm.timeBeginPeriod.restype = wintypes.UINT
                winmm.timeEndPeriod.argtypes = [wintypes.UINT]
                winmm.timeEndPeriod.restype = wintypes.UINT
                if winmm.timeBeginPeriod(1) == 0:
                    timer = winmm
            self._ready.set()
            while not self._stopping.is_set():
                result = user.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                user.TranslateMessage(ctypes.byref(msg))
                user.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.release()
            if timer is not None:
                timer.timeEndPeriod(1)
            for handle in self._hooks:
                user.UnhookWindowsHookEx(handle)
            if window:
                if safety_timer:
                    user.KillTimer(window, safety_timer)
                device = RawDevice(1, 2, 1, None)  # RIDEV_REMOVE
                user.RegisterRawInputDevices(ctypes.byref(device), 1, ctypes.sizeof(device))
                if self.native_touchpad:
                    device = RawDevice(0x0D, 0x05, 1, None)
                    user.RegisterRawInputDevices(ctypes.byref(device), 1, ctypes.sizeof(device))
                user.DestroyWindow(window)
            user.UnregisterClassW(class_name, kernel.GetModuleHandleW(None))
            self._hooks.clear()
            self._keys.clear()
            self._buttons.clear()
            self._thread_id = 0
            self._ready.set()
