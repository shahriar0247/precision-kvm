import unittest
from types import SimpleNamespace

from precision_kvm.mac import MacInjector, mac_keycode


class QuartzStub:
    """Record the OS boundary without posting input or requiring a Mac."""
    def __init__(self):
        self.events = []
        self.constants = {}

    def __getattr__(self,name):
        if name.startswith("kCG"):
            return self.constants.setdefault(name,1 << len(self.constants))
        raise AttributeError(name)

    def CGEventSourceCreate(self,*args): return object()
    def CGEventSourceSetLocalEventsSuppressionInterval(self,*args): pass
    def CGMainDisplayID(self): return 1
    def CGDisplayBounds(self,*args):
        return SimpleNamespace(origin=SimpleNamespace(x=0,y=0),size=SimpleNamespace(width=100,height=100))
    def CGEventCreate(self,*args): return {}
    def CGEventGetLocation(self,*args): return SimpleNamespace(x=50,y=50)
    def CGEventCreateMouseEvent(self,source,kind,position,button):
        return {"kind":kind,"position":position,"button":button}
    def CGEventCreateKeyboardEvent(self,source,code,down):
        return {"code":code,"down":down}
    def CGEventCreateScrollWheelEvent(self,source,unit,count,y,x):
        return {"wheel":(y,x)}
    def CGEventSetIntegerValueField(self,event,field,value): event[field]=value
    def CGEventSetFlags(self,event,flags): event["flags"]=flags
    def CGEventPost(self,tap,event): self.events.append(event)


class MappingTests(unittest.TestCase):
    def test_physical_letters_and_navigation(self):
        self.assertEqual(mac_keycode(65,0x1E),0)
        self.assertEqual(mac_keycode(0x25,0x4B,True),123)
        self.assertEqual(mac_keycode(0x0D,0x1C,True),76)
        self.assertEqual(mac_keycode(0x10,0x36),60)
        self.assertEqual(mac_keycode(0x11,0x1D,True),62)
        self.assertIsNone(mac_keycode(255))

    def setUp(self):
        self.q = QuartzStub()
        self.returns = []
        self.injector = MacInjector(self.returns.append,quartz=self.q)

    def activate(self):
        self.injector.handle({"type":"activate","normalized_y":0.5})

    def test_inactive_ignores_input(self):
        self.injector.handle({"type":"key","vk":65,"down":True})
        self.assertEqual(self.q.events,[])

    def test_left_mac_enters_at_right_and_returns(self):
        self.activate()
        self.assertEqual(self.injector.x,98)
        self.injector.handle({"type":"move","dx":-50,"dy":0})
        self.assertEqual(self.injector.x,48)
        self.injector.handle({"type":"move","dx":60,"dy":0})
        self.assertFalse(self.injector.active)
        self.assertEqual(self.returns,[{"type":"return","normalized_y":0.5}])

    def test_drag_does_not_return_and_releases_on_disconnect(self):
        self.activate()
        self.injector.handle({"type":"button","button":"left","down":True})
        self.injector.handle({"type":"move","dx":10,"dy":0})
        self.assertTrue(self.injector.active)
        self.assertEqual(self.q.events[-1]["kind"],self.q.kCGEventLeftMouseDragged)
        self.injector.handle({"type":"key","vk":0xA2,"down":True})
        self.injector.release_all()
        self.assertFalse(self.injector.keys | self.injector.buttons)
        self.assertFalse(self.injector.active)
        self.assertEqual(self.q.events[-1]["kind"],self.q.kCGEventLeftMouseUp)

    def test_wheel_both_axes_and_fractional_notches(self):
        self.activate()
        self.injector.handle({"type":"scroll","dy":120,"dx":120})
        self.assertEqual(self.q.events[-1]["wheel"],(30,-30))
        self.injector.handle({"type":"scroll","dy":2})
        count = len(self.q.events)
        self.injector.handle({"type":"scroll","dy":2})
        self.assertEqual(len(self.q.events),count+1)
        self.assertEqual(self.q.events[-1]["wheel"],(1,0))

    def test_modifier_and_repeat(self):
        self.activate()
        self.injector.handle({"type":"key","vk":0xA0,"down":True})
        self.injector.handle({"type":"key","vk":65,"down":True})
        self.assertEqual(self.q.events[-1]["flags"],self.q.kCGEventFlagMaskShift)
        self.injector.handle({"type":"key","vk":65,"down":True})
        self.assertEqual(self.q.events[-1][self.q.kCGKeyboardEventAutorepeat],1)
        self.injector.release_all()
        self.assertEqual(self.q.events[-1]["flags"],0)

    def test_volume_down_up_and_disconnect_release(self):
        class NSEventStub:
            @staticmethod
            def otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(*args):
                return SimpleNamespace(CGEvent=lambda: {"media_data":args[-2]})
        self.injector.appkit = SimpleNamespace(NSEvent=NSEventStub)
        self.activate()
        self.injector.handle({"type":"key","vk":0xAF,"down":True})
        self.assertEqual(self.q.events[-1]["media_data"],0xA00)
        self.injector.release_all()
        self.assertEqual(self.q.events[-1]["media_data"],0xB00)
        self.assertFalse(self.injector.media)

    def test_top_monitor_return(self):
        self.injector.handle({"type":"activate","side":"top","normalized_x":0.25})
        self.assertEqual((self.injector.x,self.injector.y),(24.75,98))
        self.injector.handle({"type":"move","dx":0,"dy":3})
        self.assertEqual(self.returns,[{"type":"return","normalized_y":0.25,"normalized_x":0.25}])

    def test_gain_fractional_motion_scroll_and_modifier_swap(self):
        self.injector.handle({"type":"activate","gain":0.25,"scroll_gain":2,"swap_ctrl_cmd":True})
        self.injector.handle({"type":"move","dx":-1,"dy":0})
        self.assertEqual(self.injector.x,97.75)
        self.injector.handle({"type":"move","dx":-3,"dy":0})
        self.assertEqual(self.injector.x,97)
        self.injector.handle({"type":"scroll","dy":120})
        self.assertEqual(self.q.events[-1]["wheel"],(60,0))
        self.injector.handle({"type":"key","vk":0xA2,"down":True})
        self.assertEqual(self.q.events[-1]["code"],55)
        self.assertEqual(self.q.events[-1]["flags"],self.q.kCGEventFlagMaskCommand)
        self.injector.release_all()
        self.assertEqual(self.q.events[-1]["flags"],0)

    def test_many_to_one_remap_releases_only_last_source(self):
        self.injector.handle({"type":"activate","keymap":{"A":"B","C":"B"}})
        for vk in (65,67):
            self.injector.handle({"type":"key","vk":vk,"down":True})
        count = len(self.q.events)
        self.injector.handle({"type":"key","vk":65,"down":False})
        self.assertEqual(len(self.q.events),count)
        self.assertIn(11,self.injector.keys)
        self.injector.handle({"type":"key","vk":67,"down":False})
        self.assertEqual(self.q.events[-1]["code"],11)
        self.assertFalse(self.q.events[-1]["down"])
        self.assertFalse(self.injector.keys)

    def test_mapping_release_retains_press_destination_and_blocked_key(self):
        self.injector.handle({"type":"activate","keymap":{"A":"Command","B":"Disabled"}})
        self.injector.handle({"type":"key","vk":65,"down":True})
        self.injector.mapper.mapping["0x41"] = "Control"
        self.injector.handle({"type":"key","vk":65,"down":False})
        self.assertEqual(self.q.events[-1]["code"],55)
        count = len(self.q.events)
        self.injector.handle({"type":"key","vk":66,"down":True})
        self.injector.handle({"type":"key","vk":66,"down":False})
        self.assertEqual(len(self.q.events),count)

    def test_independent_scroll_axes_inversion_and_line_mode(self):
        self.injector.handle({"type":"activate","scroll_gain_x":2,"scroll_gain_y":0.5,
                              "scroll_invert_y":True,"scroll_mode":"line"})
        self.injector.handle({"type":"scroll","dx":120,"dy":120})
        self.assertEqual(self.q.events[-1]["wheel"],(-1,-6))
        self.injector.handle({"type":"scroll","dy":120})
        self.assertEqual(self.q.events[-1]["wheel"],(-2,0))

    def test_gesture_keeps_held_modifiers_and_releases_only_temporary_key(self):
        self.activate()
        self.injector.handle({"type":"key","vk":0x5B,"down":True})
        before = self.injector.keys.copy()
        self.injector.handle({"type":"gesture","action":"desktop_left"})
        self.assertEqual(self.injector.keys,before)
        self.assertEqual(self.q.events[-2]["code"],123)
        self.assertTrue(self.q.events[-2]["down"])
        self.assertFalse(self.q.events[-1]["down"])
        self.assertEqual(self.q.events[-1]["flags"],self.q.kCGEventFlagMaskControl)
        self.injector.release_all()
        self.assertFalse(self.injector._held_sources)

    def test_remapped_fn_has_native_modifier_flag(self):
        self.injector.handle({"type":"activate","keymap":{"A":"Fn"}})
        self.injector.handle({"type":"key","vk":65,"down":True})
        self.injector.handle({"type":"key","vk":66,"down":True})
        self.assertEqual(self.q.events[-1]["flags"],self.q.kCGEventFlagMaskSecondaryFn)
        self.injector.release_all()
        self.assertEqual(self.q.events[-1]["flags"],0)

    def test_windows_home_end_and_shift_selection(self):
        self.injector.handle({"type":"activate","windows_home_end":True})
        self.injector.handle({"type":"key","vk":0xA0,"down":True})
        self.injector.handle({"type":"key","vk":0x24,"down":True})
        self.assertEqual(self.q.events[-1]["code"],123)
        self.assertEqual(self.q.events[-1]["flags"],
                         self.q.kCGEventFlagMaskCommand | self.q.kCGEventFlagMaskShift)
        self.injector.handle({"type":"key","vk":0x24,"down":False})
        self.assertFalse(self.q.events[-1]["down"])
        self.injector.release_all()

    def test_windows_word_navigation_release_and_custom_mapping_priority(self):
        self.injector.handle({"type":"activate","windows_word_nav":True,
                              "keymap":{"Backspace":"Delete"}})
        self.injector.handle({"type":"key","vk":0xA2,"down":True})
        self.injector.handle({"type":"key","vk":0x25,"down":True})
        self.assertEqual(self.q.events[-1]["flags"],self.q.kCGEventFlagMaskAlternate)
        self.injector.handle({"type":"key","vk":0xA2,"down":False})
        self.injector.handle({"type":"key","vk":0x25,"down":False})
        self.assertEqual(self.q.events[-1]["code"],123)
        self.assertEqual(self.q.events[-1]["flags"],self.q.kCGEventFlagMaskAlternate)
        self.injector.handle({"type":"key","vk":0xA2,"down":True})
        self.injector.handle({"type":"key","vk":0x08,"down":True})
        self.assertEqual(self.q.events[-1]["code"],117)
        self.assertEqual(self.q.events[-1]["flags"],self.q.kCGEventFlagMaskControl)
        self.injector.close()
        self.assertFalse(self.injector._target_counts)

    def test_lock_message_locks_screen_and_deactivates(self):
        self.activate()
        self.injector.handle({"type":"key","vk":0xA2,"down":True})
        self.injector.handle({"type":"lock"})
        self.assertFalse(self.injector.active)
        self.assertEqual(self.q.events[-2]["code"],12)
        self.assertTrue(self.q.events[-2]["down"])
        self.assertFalse(self.q.events[-1]["down"])
        flags = self.q.kCGEventFlagMaskControl | self.q.kCGEventFlagMaskCommand
        self.assertEqual(self.q.events[-2]["flags"],flags)
        count = len(self.q.events)
        self.injector.handle({"type":"move","dx":10,"dy":0})
        self.assertEqual(len(self.q.events),count)

    def test_lock_message_works_while_inactive(self):
        self.injector.handle({"type":"lock"})
        self.assertEqual(self.q.events[-1]["code"],12)
        self.assertFalse(self.q.events[-1]["down"])

    def test_smooth_scroll_is_tagged_and_cancelled_on_deactivate(self):
        from unittest.mock import patch
        self.injector.handle({"type":"activate","smooth_scroll":True})
        with patch("precision_kvm.scroll.SmoothScrollEngine") as engine:
            self.injector.handle({"type":"scroll","dy":120})
            engine.return_value.add.assert_called_once_with(0.0,30.0)
            self.injector._scroll_step(0,2)
            self.assertEqual(self.q.events[-1][self.q.kCGEventSourceUserData],0x50524B56)
            self.injector.release_all()
            engine.return_value.cancel.assert_called_once()
            count = len(self.q.events)
            self.injector._scroll_step(0,10)
            self.assertEqual(len(self.q.events),count)
            self.injector.close()
            engine.return_value.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
