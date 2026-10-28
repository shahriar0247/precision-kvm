"""Synthetic HID descriptors/reports; no raw input registration or live capture."""
import ctypes
from unittest.mock import Mock

import pytest

from precision_kvm.touchpad import (Contact, ContactFrames, FourFingerSwipe,
                                  HidButtonCaps, HidCaps, HidTouchpad, HidValueCaps,
                                  SUCCESS, WindowsTouchpads)


class Api:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


class FakeHid:
    """Fake C ABI with realistic descriptor collections and byte report frames."""
    def __init__(self, slots=1):
        self.slots = slots
        self.report_bytes = 4 + slots * 7
        self.HidP_GetCaps = Api(self.caps)
        self.HidP_GetValueCaps = Api(self.values)
        self.HidP_GetButtonCaps = Api(self.buttons)
        self.HidP_GetUsageValue = Api(self.value)
        self.HidP_GetUsages = Api(self.usages)

    def caps(self, data, out):
        cap = ctypes.cast(out, ctypes.POINTER(HidCaps)).contents
        cap.page, cap.usage, cap.input_bytes = 13, 5, self.report_bytes
        cap.input_values, cap.input_buttons = 2 + self.slots * 3, self.slots
        return SUCCESS

    def values(self, kind, values, count, data):
        definitions = [(13, 0x54, 0), (13, 0x56, 0)]
        for link in range(1, self.slots + 1):
            definitions.extend([(13, 0x51, link), (1, 0x30, link), (1, 0x31, link)])
        for cap, (page, usage, link) in zip(values, definitions):
            cap.page, cap.report, cap.link = page, 4, link
            cap.usages[0] = usage
            cap.logical_min, cap.logical_max = 0, 1000
        return SUCCESS

    def buttons(self, kind, buttons, count, data):
        for link, cap in enumerate(buttons, 1):
            cap.page, cap.report, cap.link = 13, 4, link
            cap.is_range = True
            cap.usages[0], cap.usages[1] = 0x42, 0x47
        return SUCCESS

    def value(self, kind, page, link, usage, out, data, report, size):
        raw = ctypes.string_at(report, size)
        if link == 0:
            number = raw[1] if usage == 0x54 else int.from_bytes(raw[2:4], "little")
        else:
            offset = 4 + (link - 1) * 7
            if page == 13:
                number = raw[offset]
            elif usage == 0x30:
                number = int.from_bytes(raw[offset + 1:offset + 3], "little")
            else:
                number = int.from_bytes(raw[offset + 3:offset + 5], "little")
        ctypes.cast(out, ctypes.POINTER(ctypes.c_uint32)).contents.value = number
        return SUCCESS

    def usages(self, kind, page, link, out, length, data, report, size):
        raw = ctypes.string_at(report, size)
        flags = raw[4 + (link - 1) * 7 + 5]
        active = ([0x42] if flags & 1 else []) + ([0x47] if flags & 2 else [])
        for index, usage in enumerate(active):
            out[index] = usage
        ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32)).contents.value = len(active)
        return SUCCESS

    def report(self, scan, count, contacts):
        raw = bytearray([4, count]) + scan.to_bytes(2, "little")
        for identifier, x, y, tip, confidence in contacts:
            raw.extend(bytes([identifier]) + x.to_bytes(2, "little") + y.to_bytes(2, "little")
                       + bytes([int(tip) | int(confidence) << 1, 0]))
        while len(raw) < self.report_bytes:
            raw.extend(bytes(7))
        return bytes(raw)


def contacts(x=.2, y=.3):
    return [Contact(index, x + index * .02, y) for index in range(4)]


def test_windows_hid_abi_layout_uses_fixed_size_windows_integers():
    assert ctypes.sizeof(HidCaps) == 64
    assert ctypes.sizeof(HidValueCaps) == 72
    assert ctypes.sizeof(HidButtonCaps) == 72
    assert HidValueCaps.logical_min.offset == 40
    assert HidValueCaps.usages.offset == 56


@pytest.mark.parametrize("dx,dy,action", [
    (.2, 0, "desktop_right"), (-.2, 0, "desktop_left"),
    (0, -.2, "mission_control"), (0, .2, "show_desktop"),
])
def test_four_contact_direction_threshold_and_one_action_until_lift(dx, dy, action):
    swipe = FourFingerSwipe()
    assert swipe.feed(contacts(.4, .4), 1) is None
    assert swipe.feed(contacts(.4, .4), 1.01) is None
    assert swipe.feed(contacts(.4 + dx, .4 + dy), 1.02) == action
    assert swipe.feed(contacts(.4 + dx, .4 + dy), 1.03) is None
    assert swipe.feed(contacts(.4, .4)[:3], 1.04) is None
    assert swipe.feed(contacts(.4 + dx, .4 + dy), 1.05) is None
    assert swipe.feed([], 1.06) is None
    assert not swipe.latched


def test_not_four_contacts_ids_or_confidence_never_fires():
    swipe = FourFingerSwipe()
    for index in range(4):
        assert swipe.feed(contacts(.1 + index * .2)[:3], 1 + index / 100) is None
    assert swipe.feed([Contact(0, .1, .2)] * 4, 2) is None
    assert swipe.feed([Contact(index, .2, .2, confidence=False) for index in range(4)], 2.01) is None
    assert swipe.feed([Contact(index, float("nan"), .2) for index in range(4)], 2.02) is None


def test_one_moving_finger_with_three_stationary_is_not_a_four_finger_swipe():
    swipe = FourFingerSwipe()
    original = contacts(.1, .3)
    swipe.feed(original, 1)
    swipe.feed(original, 1.01)
    moved = [Contact(0, .9, .3)] + original[1:]
    assert swipe.feed(moved, 1.02) is None


def test_hybrid_assembler_requires_same_scan_unique_ids_complete_count():
    frames = ContactFrames()
    assert frames.feed(10, 4, contacts()[:1]) is None
    assert frames.feed(10, 0, contacts()[1:2]) is None
    assert frames.feed(10, 0, contacts()[2:3]) is None
    assert frames.feed(10, 0, contacts()[3:]) == contacts()
    assert frames.feed(11, 0, []) == []
    assert frames.feed(12, 4, contacts()[:1]) is None
    assert frames.feed(13, 0, contacts()[1:]) == []
    assert frames.feed(14, 4, [contacts()[0]] * 4) is None
    assert frames.feed(15, 127, contacts()) is None


@pytest.mark.parametrize("slots", [1, 2, 4, 5])
def test_hid_parse_actual_style_hybrid_and_parallel_descriptors(slots):
    hid = FakeHid(slots)
    device = HidTouchpad(b"fake descriptor", hid=hid)
    actions = []
    for scan, x in ((100, 200), (101, 240), (102, 400), (103, 450)):
        items = [(identifier, x + identifier * 10, 300, True, True) for identifier in range(4)]
        for offset in range(0, 4, slots):
            action = device.parse(hid.report(scan, 4 if offset == 0 else 0, items[offset:offset + slots]))
            if action:
                actions.append(action)
    assert actions == ["desktop_right"]
    device.parse(hid.report(104, 0, []))
    assert not device.swipe.latched


def test_hid_malformed_wrong_report_and_partial_frame_do_not_trigger():
    hid = FakeHid()
    device = HidTouchpad(b"fake descriptor", hid=hid)
    assert device.parse(b"") is None
    raw = hid.report(1, 4, [(0, 200, 200, True, True)])
    assert device.parse(bytes([5]) + raw[1:]) is None
    assert device.parse(raw) is None
    assert device.parse(hid.report(2, 0, [(1, 800, 200, True, True)])) is None
    assert device.parse(hid.report(2, 0, [(2, 800, 200, True, True)])) is None
    assert not device.swipe.latched


def test_hid_reports_with_palm_confidence_ignored():
    hid = FakeHid(4)
    device = HidTouchpad(b"fake descriptor", hid=hid)
    for scan, x in enumerate((200, 300, 500)):
        items = [(identifier, x, 300, True, identifier != 3) for identifier in range(4)]
        assert device.parse(hid.report(scan, 4, items)) is None


def test_raw_packet_size_limits_and_device_cache_bound():
    reader = WindowsTouchpads.__new__(WindowsTouchpads)
    reader.devices = {}
    reader.descriptor = Mock(side_effect=ValueError("unsupported"))
    assert reader.feed(1, 999999, 1, b"") == []
    assert reader.feed(1, 1, 99, b"x" * 99) == []
    assert reader.feed(1, 4, 1, b"x") == []
    for handle in range(40):
        assert reader.feed(handle, 1, 1, b"x") == []
    assert len(reader.devices) == 16
    assert reader.descriptor.call_count == 16
