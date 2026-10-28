"""Translate OS-generated gesture shortcut equivalents, never raw finger counts.

Windows may handle Precision Touchpad gestures directly in the shell instead of
emitting these shortcuts. In that case a user must configure advanced gestures
to keyboard equivalents; this module does not change global touchpad settings.
"""
from __future__ import annotations


class GestureShortcutBridge:
    def __init__(self):
        self.pressed: set[int] = set()

    def reset(self):
        self.pressed.clear()

    def feed(self, vk: int, down: bool) -> str | None:
        """Caller reserves injected sequences while sharing; emit once per press."""
        was_down = vk in self.pressed
        if down:
            self.pressed.add(vk)
        else:
            self.pressed.discard(vk)
            return None
        if was_down:
            return None
        win = bool(self.pressed & {0x5B, 0x5C})
        ctrl = bool(self.pressed & {0x11, 0xA2, 0xA3})
        if win and ctrl and vk in (0x25, 0x27):
            return "desktop_left" if vk == 0x25 else "desktop_right"
        if win and not ctrl and vk == 0x09:
            return "mission_control"
        if win and not ctrl and vk == 0x44:
            return "show_desktop"
        return None
