"""Opt-in raw Precision Touchpad four-contact swipe recognition.

Only HID reports advertising the required descriptor usages are accepted.
This observes contact data; it does not suppress Windows' shell gestures.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import math
import sys
import time

SUCCESS = 0x00110000
MAX_RAW_BYTES = 128 * 1024


class HidCaps(ctypes.Structure):
    _fields_ = [("usage", ctypes.c_uint16), ("page", ctypes.c_uint16),
                ("input_bytes", ctypes.c_uint16), ("output_bytes", ctypes.c_uint16),
                ("feature_bytes", ctypes.c_uint16), ("reserved", ctypes.c_uint16 * 17),
                ("link_nodes", ctypes.c_uint16), ("input_buttons", ctypes.c_uint16),
                ("input_values", ctypes.c_uint16), ("input_indices", ctypes.c_uint16),
                ("output_buttons", ctypes.c_uint16), ("output_values", ctypes.c_uint16),
                ("output_indices", ctypes.c_uint16), ("feature_buttons", ctypes.c_uint16),
                ("feature_values", ctypes.c_uint16), ("feature_indices", ctypes.c_uint16)]


class HidValueCaps(ctypes.Structure):
    _fields_ = [("page", ctypes.c_uint16), ("report", ctypes.c_uint8),
                ("alias", ctypes.c_uint8), ("bitfield", ctypes.c_uint16),
                ("link", ctypes.c_uint16), ("link_usage", ctypes.c_uint16),
                ("link_page", ctypes.c_uint16), ("is_range", ctypes.c_uint8),
                ("string_range", ctypes.c_uint8), ("designator_range", ctypes.c_uint8),
                ("absolute", ctypes.c_uint8), ("has_null", ctypes.c_uint8),
                ("reserved", ctypes.c_uint8), ("bit_size", ctypes.c_uint16),
                ("report_count", ctypes.c_uint16), ("reserved2", ctypes.c_uint16 * 5),
                ("units_exp", ctypes.c_uint32), ("units", ctypes.c_uint32),
                ("logical_min", ctypes.c_int32), ("logical_max", ctypes.c_int32),
                ("physical_min", ctypes.c_int32), ("physical_max", ctypes.c_int32),
                ("usages", ctypes.c_uint16 * 8)]


class HidButtonCaps(ctypes.Structure):
    _fields_ = [("page", ctypes.c_uint16), ("report", ctypes.c_uint8),
                ("alias", ctypes.c_uint8), ("bitfield", ctypes.c_uint16),
                ("link", ctypes.c_uint16), ("link_usage", ctypes.c_uint16),
                ("link_page", ctypes.c_uint16), ("is_range", ctypes.c_uint8),
                ("string_range", ctypes.c_uint8), ("designator_range", ctypes.c_uint8),
                ("absolute", ctypes.c_uint8), ("reserved", ctypes.c_uint32 * 10),
                ("usages", ctypes.c_uint16 * 8)]


@dataclass(frozen=True)
class UsageValue:
    report: int
    page: int
    usage: int
    link: int
    minimum: int
    maximum: int


@dataclass(frozen=True)
class Contact:
    identifier: int
    x: float
    y: float
    tip: bool = True
    confidence: bool = True


class FourFingerSwipe:
    def __init__(self, threshold=0.12, stable_frames=3):
        if not math.isfinite(threshold) or not 0.03 <= threshold <= 0.5 or stable_frames < 2:
            raise ValueError("Invalid swipe thresholds")
        self.threshold, self.stable_frames = threshold, stable_frames
        self.reset()

    def reset(self):
        self.ids: frozenset[int] = frozenset()
        self.origin: tuple[float, float] | None = None
        self.frames = 0
        self.positions: dict[int, tuple[float, float]] = {}
        self.latched = False
        self.last_at = 0.0

    def feed(self, contacts: list[Contact], now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        if not contacts:
            self.reset()
            return None
        active = [contact for contact in contacts if contact.tip and contact.confidence]
        if not any(contact.tip for contact in contacts):
            self.reset()
            return None
        if now - self.last_at > .3:
            # Rebase after missing reports, retaining one-action-until-lift latch.
            self.origin, self.frames, self.ids = None, 0, frozenset()
        self.last_at = now
        if self.latched:
            return None
        identifiers = frozenset(contact.identifier for contact in active)
        if len(active) != 4 or len(identifiers) != 4:
            self.origin, self.frames, self.ids = None, 0, frozenset()
            return None
        if any(not math.isfinite(c.x) or not math.isfinite(c.y)
               or not 0 <= c.x <= 1 or not 0 <= c.y <= 1 for c in active):
            self.origin, self.frames, self.ids = None, 0, frozenset()
            return None
        center = (sum(c.x for c in active) / 4, sum(c.y for c in active) / 4)
        if identifiers != self.ids:
            self.ids, self.origin, self.frames = identifiers, center, 1
            self.positions = {contact.identifier: (contact.x, contact.y) for contact in active}
            return None
        self.frames += 1
        if self.frames < self.stable_frames or self.origin is None:
            return None
        dx, dy = center[0] - self.origin[0], center[1] - self.origin[1]
        if (abs(dx) >= self.threshold and abs(dx) > abs(dy) * 1.3
                and all((contact.x - self.positions[contact.identifier][0]) * (1 if dx > 0 else -1)
                        >= self.threshold * .35 for contact in active)):
            self.latched = True
            return "desktop_right" if dx > 0 else "desktop_left"
        if (abs(dy) >= self.threshold and abs(dy) > abs(dx) * 1.3
                and all((contact.y - self.positions[contact.identifier][1]) * (1 if dy > 0 else -1)
                        >= self.threshold * .35 for contact in active)):
            self.latched = True
            return "show_desktop" if dy > 0 else "mission_control"
        return None


class ContactFrames:
    """Assemble parallel/hybrid contact frames using scan time and contact count."""
    def __init__(self):
        self.scan: int | None = None
        self.expected = 0
        self.contacts: list[Contact] = []

    def feed(self, scan: int, count: int, contacts: list[Contact]) -> list[Contact] | None:
        if not 0 <= count <= 10 or len(contacts) > 10:
            self.scan, self.expected, self.contacts = None, 0, []
            return None
        if count:
            self.scan, self.expected, self.contacts = scan, count, []
        elif self.scan != scan:
            # A new zero-count scan reports all contacts lifted.
            self.scan, self.expected, self.contacts = scan, 0, []
            return []
        elif not self.expected:
            return None
        remaining = self.expected - len(self.contacts)
        self.contacts.extend(contacts[:remaining])
        if len(self.contacts) != self.expected:
            return None
        result = self.contacts
        self.expected, self.contacts = 0, []
        if len({contact.identifier for contact in result}) != len(result):
            return None
        return result


class HidTouchpad:
    def __init__(self, preparsed: bytes, hid=None):
        if hid is None:
            if sys.platform != "win32":
                raise OSError("Native touchpad reports require Windows")
            hid = ctypes.WinDLL("hid", use_last_error=True)
        self.hid = hid
        self.data = ctypes.create_string_buffer(preparsed)
        self._declare_api()
        caps = HidCaps()
        if hid.HidP_GetCaps(self.data, ctypes.byref(caps)) != SUCCESS:
            raise ValueError("Invalid HID preparsed data")
        if (caps.page != 0x0D or caps.usage != 0x05 or not 1 <= caps.input_bytes <= 4096
                or not 1 <= caps.input_values <= 512 or caps.input_buttons > 512):
            raise ValueError("Unsupported touchpad descriptor")
        self.report_bytes = caps.input_bytes
        values = (HidValueCaps * caps.input_values)()
        n = ctypes.c_uint16(caps.input_values)
        if hid.HidP_GetValueCaps(0, values, ctypes.byref(n), self.data) != SUCCESS:
            raise ValueError("Cannot inspect HID value caps")
        self.values: dict[tuple[int, int, int, int], UsageValue] = {}
        for cap in values[:n.value]:
            usages = range(cap.usages[0], cap.usages[1] + 1) if cap.is_range else [cap.usages[0]]
            if len(usages) > 256:
                continue
            for usage in usages:
                if (cap.page, usage) in ((1, 0x30), (1, 0x31), (0x0D, 0x51),
                                         (0x0D, 0x54), (0x0D, 0x56)):
                    value = UsageValue(cap.report, cap.page, usage, cap.link,
                                       cap.logical_min, cap.logical_max)
                    self.values[(cap.report, cap.page, usage, cap.link)] = value
        self.buttons: set[tuple[int, int, int]] = set()
        if caps.input_buttons:
            buttons = (HidButtonCaps * caps.input_buttons)()
            n = ctypes.c_uint16(caps.input_buttons)
            if hid.HidP_GetButtonCaps(0, buttons, ctypes.byref(n), self.data) != SUCCESS:
                raise ValueError("Cannot inspect HID button caps")
            for cap in buttons[:n.value]:
                if cap.page != 0x0D:
                    continue
                usages = range(cap.usages[0], cap.usages[1] + 1) if cap.is_range else [cap.usages[0]]
                for usage in (0x42, 0x47):
                    if usage in usages:
                        self.buttons.add((cap.report, cap.link, usage))
        self.links: dict[int, list[int]] = {}
        for report, page, usage, link in self.values:
            if page == 0x0D and usage == 0x51:
                required = [(report, 1, 0x30, link), (report, 1, 0x31, link)]
                if (all(key in self.values for key in required)
                        and (report, link, 0x42) in self.buttons):
                    self.links.setdefault(report, []).append(link)
        if not self.links:
            raise ValueError("Touchpad has no complete contact collections")
        self.frames = ContactFrames()
        self.swipe = FourFingerSwipe()

    def _declare_api(self):
        h = self.hid
        h.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HidCaps)]
        h.HidP_GetCaps.restype = ctypes.c_int32
        h.HidP_GetValueCaps.argtypes = [ctypes.c_int, ctypes.POINTER(HidValueCaps),
                                      ctypes.POINTER(ctypes.c_uint16), ctypes.c_void_p]
        h.HidP_GetValueCaps.restype = ctypes.c_int32
        h.HidP_GetButtonCaps.argtypes = [ctypes.c_int, ctypes.POINTER(HidButtonCaps),
                                       ctypes.POINTER(ctypes.c_uint16), ctypes.c_void_p]
        h.HidP_GetButtonCaps.restype = ctypes.c_int32
        h.HidP_GetUsageValue.argtypes = [ctypes.c_int, ctypes.c_uint16, ctypes.c_uint16,
            ctypes.c_uint16, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_uint32]
        h.HidP_GetUsageValue.restype = ctypes.c_int32
        h.HidP_GetUsages.argtypes = [ctypes.c_int, ctypes.c_uint16, ctypes.c_uint16,
            ctypes.POINTER(ctypes.c_uint16), ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        h.HidP_GetUsages.restype = ctypes.c_int32

    def _value(self, report, descriptor: UsageValue) -> int:
        value = ctypes.c_uint32()
        result = self.hid.HidP_GetUsageValue(0, descriptor.page, descriptor.link,
            descriptor.usage, ctypes.byref(value), self.data, report, len(report.raw) - 1)
        if result != SUCCESS:
            raise ValueError("Missing value in touchpad input report")
        return int(value.value)

    def parse(self, raw: bytes) -> str | None:
        if len(raw) != self.report_bytes:
            return None
        report_id = raw[0]
        links = self.links.get(report_id, [])
        if not links:
            return None
        count_cap = self.values.get((report_id, 0x0D, 0x54, 0))
        scan_cap = self.values.get((report_id, 0x0D, 0x56, 0))
        if count_cap is None or scan_cap is None:
            return None
        report = ctypes.create_string_buffer(raw)
        try:
            count = self._value(report, count_cap)
            scan = self._value(report, scan_cap)
            contacts = []
            for link in links:
                usages = (ctypes.c_uint16 * 32)()
                n = ctypes.c_uint32(32)
                status = self.hid.HidP_GetUsages(0, 0x0D, link, usages, ctypes.byref(n),
                                               self.data, report, len(raw))
                if status != SUCCESS or n.value > 32:
                    return None
                pressed = set(usages[:n.value])
                identifier = self._value(report, self.values[(report_id, 0x0D, 0x51, link)])
                values = []
                for usage in (0x30, 0x31):
                    cap = self.values[(report_id, 1, usage, link)]
                    value = self._value(report, cap)
                    if cap.maximum <= cap.minimum or not cap.minimum <= value <= cap.maximum:
                        return None
                    values.append((value - cap.minimum) / (cap.maximum - cap.minimum))
                contacts.append(Contact(identifier, values[0], values[1], tip=0x42 in pressed,
                    confidence=(report_id, link, 0x47) not in self.buttons or 0x47 in pressed))
            frame = self.frames.feed(scan, count, contacts)
            return self.swipe.feed(frame) if frame is not None else None
        except ValueError:
            return None


class WindowsTouchpads:
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.GetRawInputDeviceInfoW.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                                   ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self.user.GetRawInputDeviceInfoW.restype = ctypes.c_uint32
        self.devices: dict[int, HidTouchpad | None] = {}

    def descriptor(self, handle: int) -> HidTouchpad:
        size = ctypes.c_uint32()
        if self.user.GetRawInputDeviceInfoW(handle, 0x20000005, None, ctypes.byref(size)) == 0xFFFFFFFF:
            raise OSError("Cannot query touchpad descriptor")
        if not 1 <= size.value <= MAX_RAW_BYTES:
            raise ValueError("Invalid touchpad descriptor size")
        data = ctypes.create_string_buffer(size.value)
        if self.user.GetRawInputDeviceInfoW(handle, 0x20000005, data, ctypes.byref(size)) == 0xFFFFFFFF:
            raise OSError("Cannot read touchpad descriptor")
        return HidTouchpad(data.raw[:size.value])

    def reset(self):
        for device in self.devices.values():
            if device:
                device.frames = ContactFrames()
                device.swipe.reset()

    def feed(self, handle: int, report_size: int, report_count: int, payload: bytes) -> list[str]:
        if (not 1 <= report_size <= 4096 or not 1 <= report_count <= 32
                or len(payload) != report_size * report_count or len(payload) > MAX_RAW_BYTES):
            return []
        if handle not in self.devices:
            if len(self.devices) >= 16:
                return []
            try:
                self.devices[handle] = self.descriptor(handle)
            except (OSError, ValueError):
                self.devices[handle] = None
        device = self.devices[handle]
        if device is None:
            return []
        actions = []
        for offset in range(0, len(payload), report_size):
            action = device.parse(payload[offset:offset + report_size])
            if action:
                actions.append(action)
        return actions
