"""Windows tiling backend — window enumeration and placement via user32.

Importing this module never installs hooks or moves windows. All calls are
safe from any thread; each calling thread is switched to per-monitor-v2 DPI
awareness so rects are physical pixels everywhere.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
import threading

if not sys.platform == "win32":
    raise OSError("WindowsTiling requires Windows")

user = ctypes.WinDLL("user32", use_last_error=True)
dwm = ctypes.WinDLL("dwmapi", use_last_error=True)

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CAPTION = 0x00C00000
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_CLOAKED = 14
DWMWA_EXTENDED_FRAME_BOUNDS = 9
MONITOR_DEFAULTTONEAREST = 2
SW_MINIMIZE = 6
SW_RESTORE = 9
SW_SHOWNA = 8
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200
SWP_NOSENDCHANGING = 0x0400
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD]
user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user.MonitorFromWindow.restype = wintypes.HMONITOR
user.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user.SetForegroundWindow.argtypes = [wintypes.HWND]
user.GetForegroundWindow.restype = wintypes.HWND
user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD, ctypes.c_size_t]
user.IsWindowVisible.argtypes = [wintypes.HWND]
user.IsIconic.argtypes = [wintypes.HWND]
user.IsZoomed.argtypes = [wintypes.HWND]
user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user.GetWindowTextLengthW.restype = ctypes.c_int


class WindowsTiling:
    """Backend for TilingController: enumerate, place, focus, hide windows."""

    def __init__(self):
        self._offsets: dict[int, tuple[int, int, int, int]] = {}
        self._monitor = None
        self._last_ids: set[int] = set()
        self._local = threading.local()

    # -- helpers -------------------------------------------------------------

    def _dpi(self):
        if not hasattr(self._local, "done"):
            user.SetThreadDpiAwarenessContext(wintypes.HANDLE(-4))
            self._local.done = True

    def _frame_bounds(self, hwnd):
        rect = wintypes.RECT()
        if dwm.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                                      ctypes.byref(rect), ctypes.sizeof(rect)) == 0:
            return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        return None

    def _monitor_of(self, hwnd):
        return user.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)

    def _work_area(self, monitor) -> tuple[int, int, int, int]:
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not user.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return (0, 0, 1920, 1040)
        rc = info.rcWork
        return (rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top)

    # -- controller interface --------------------------------------------------

    def windows(self) -> list[tuple[int, tuple[int, int, int, int]]]:
        """Tiled candidates on the focused display, with their visual rects."""
        self._dpi()
        foreground = user.GetForegroundWindow()
        monitor = self._monitor_of(foreground) if foreground else None
        if monitor is None:
            monitor = self._monitor
        if monitor is None:
            monitor = self._monitor_of(user.GetDesktopWindow())
        self._monitor = monitor
        mine = wintypes.DWORD()
        mine.value = ctypes.windll.kernel32.GetCurrentProcessId()
        found: list[tuple[int, tuple[int, int, int, int]]] = []
        result = []

        @WNDENUMPROC
        def each(hwnd, _param):
            if not user.IsWindowVisible(hwnd) or user.IsIconic(hwnd):
                return True
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == mine.value:
                return True
            style = user.GetWindowLongPtrW(hwnd, GWL_STYLE)
            if not style & WS_CAPTION:
                return True
            if user.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                return True
            if not user.GetWindowTextLengthW(hwnd):
                return True
            cloaked = wintypes.DWORD()
            dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked),
                                       ctypes.sizeof(cloaked))
            if cloaked.value:
                return True
            if self._monitor_of(hwnd) != monitor:
                return True
            rect = wintypes.RECT()
            if not user.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if width < 80 or height < 60:
                return True
            visual = self._frame_bounds(hwnd) or (rect.left, rect.top, width, height)
            self._offsets[hwnd] = (rect.left - visual[0], rect.top - visual[1],
                                   rect.right - (visual[0] + visual[2]),
                                   rect.bottom - (visual[1] + visual[3]))
            found.append(hwnd)
            result.append((hwnd, visual))
            return True

        user.EnumWindows(each, 0)
        known = set(found)
        for hwnd in list(self._offsets):
            if hwnd not in known:
                del self._offsets[hwnd]
        self._last_ids = known
        return result

    def focused(self):
        self._dpi()
        hwnd = user.GetForegroundWindow()
        return hwnd if hwnd in getattr(self, "_last_ids", ()) else None

    def work_area(self) -> tuple[int, int, int, int]:
        self._dpi()
        monitor = self._monitor
        if monitor is None:
            foreground = user.GetForegroundWindow()
            monitor = self._monitor_of(foreground) if foreground \
                else self._monitor_of(user.GetDesktopWindow())
        return self._work_area(monitor)

    def _refresh_offset(self, win):
        rect = wintypes.RECT()
        visual = self._frame_bounds(win)
        if visual and user.GetWindowRect(win, ctypes.byref(rect)):
            self._offsets[win] = (rect.left - visual[0], rect.top - visual[1],
                                  rect.right - (visual[0] + visual[2]),
                                  rect.bottom - (visual[1] + visual[3]))

    def set_rect(self, win, rect):
        self._dpi()
        if user.IsZoomed(win):
            user.ShowWindow(win, SW_RESTORE)
            self._refresh_offset(win)
        dl, dt, dr, db = self._offsets.get(win, (0, 0, 0, 0))
        x, y, w, h = rect
        user.SetWindowPos(win, None, round(x) - dl, round(y) - dt,
                          round(w) + dl + dr, round(h) + dt + db,
                          SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER
                          | SWP_NOSENDCHANGING)

    def apply(self, ops):
        self._dpi()
        for op in ops:
            kind = op[0]
            if kind == "rect":
                self.set_rect(op[1], op[2])
            elif kind == "hide":
                user.ShowWindow(op[1], SW_MINIMIZE)
            elif kind == "show":
                if user.IsIconic(op[1]):
                    user.ShowWindow(op[1], SW_RESTORE)
                else:
                    user.ShowWindow(op[1], SW_SHOWNA)
            elif kind == "focus":
                self._focus(op[1])

    def _focus(self, hwnd):
        if user.IsIconic(hwnd):
            user.ShowWindow(hwnd, SW_RESTORE)
        user.keybd_event(VK_MENU, 0, 0, 0)
        user.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user.SetForegroundWindow(hwnd)
