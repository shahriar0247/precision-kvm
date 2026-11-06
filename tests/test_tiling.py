"""Tiling engine tests — pure logic, no platform backends."""
import unittest

from precision_kvm import tiling
from precision_kvm.tiling import DragController, TilingEngine, chord_action


class FakeBackend:
    def __init__(self):
        self.rects = {}
        self.shown = set()
        self.hidden = set()

    def windows(self):
        return [(win, rect) for win, rect in self.rects.items()]

    def focused(self):
        return None

    def work_area(self):
        return (0, 0, 1920, 1040)

    def set_rect(self, win, rect):
        self.rects[win] = rect

    def apply(self, ops):
        for op in ops:
            if op[0] == "rect":
                self.rects[op[1]] = op[2]
            elif op[0] == "show":
                self.hidden.discard(op[1])
                self.shown.add(op[1])
            elif op[0] == "hide":
                self.shown.discard(op[1])
                self.hidden.add(op[1])


def make_engine(windows, mode="bsp", area=(0, 0, 1920, 1040)):
    engine = TilingEngine(mode)
    engine.sync(windows, windows[-1] if windows else None, area)
    return engine


def overlap_free(rects):
    items = list(rects.items())
    for i, (_, a) in enumerate(items):
        for _, b in items[i + 1:]:
            if (a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
                    and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]):
                return False
    return True


class ChordTests(unittest.TestCase):
    def test_arrow_chords(self):
        self.assertEqual(chord_action(0x27, frozenset({"ctrl", "win"})), "focus_right")
        self.assertEqual(chord_action(0x25, frozenset({"ctrl", "alt"})), "focus_left")
        self.assertIsNone(chord_action(0x27, frozenset({"win"})))
        self.assertIsNone(chord_action(0x27, frozenset({"ctrl", "win", "alt"})))
        self.assertIsNone(chord_action(0x27, frozenset()))

    def test_digit_chords(self):
        self.assertEqual(chord_action(0x34, frozenset({"alt"})), "workspace_4")
        self.assertEqual(chord_action(0x31, frozenset({"ctrl", "alt"})), "send_workspace_1")
        self.assertIsNone(chord_action(0x34, frozenset({"ctrl", "win"})))
        self.assertIsNone(chord_action(0x34, frozenset()))

    def test_float_chord(self):
        self.assertEqual(chord_action(0x57, frozenset({"ctrl", "alt"})), "toggle_float")
        self.assertIsNone(chord_action(0x57, frozenset({"alt"})))


class LayoutTests(unittest.TestCase):
    def test_bsp_fills_area_without_overlap(self):
        for count in range(1, 8):
            engine = make_engine([f"w{i}" for i in range(count)])
            rects = engine._rects
            self.assertEqual(len(rects), count)
            self.assertTrue(overlap_free(rects), f"overlap with {count} windows")
            for rect in rects.values():
                self.assertGreaterEqual(rect[2], tiling.MIN_TILE)
                self.assertGreaterEqual(rect[3], tiling.MIN_TILE)

    def test_bsp_covers_area(self):
        engine = make_engine(["a", "b", "c"])
        area = engine.area
        total = sum(r[2] * r[3] for r in engine._rects.values())
        expected = area[2] * area[3] - tiling.GAP * 2 * (area[3] - tiling.GAP)
        self.assertGreater(total, expected * 0.95)

    def test_horizontal_equal_columns(self):
        engine = make_engine(["a", "b", "c"], mode="horizontal")
        rects = [engine._rects[w] for w in ("a", "b", "c")]
        self.assertEqual(len({r[2] for r in rects}), 1)
        self.assertTrue(overlap_free(engine._rects))

    def test_horizontal_keeps_focused_visible(self):
        engine = make_engine([f"w{i}" for i in range(8)], mode="horizontal")
        engine.area = (0, 0, 1000, 800)
        engine.space().focused = "w0"
        engine.handle("focus_right")
        rects = engine._rects
        focused = engine.space().focused
        rect = rects[focused]
        self.assertGreaterEqual(rect[0], 0)
        self.assertLessEqual(rect[0] + rect[2], 1000)


class FocusTests(unittest.TestCase):
    def test_focus_right_picks_right_neighbor(self):
        engine = make_engine(["a", "b"])
        rects = engine._rects
        self.assertLess(rects["a"][0], rects["b"][0])
        engine.space().focused = "a"
        engine.handle("focus_right")
        self.assertEqual(engine.space().focused, "b")

    def test_focus_up_down_in_bsp(self):
        engine = make_engine(["a", "b", "c"])
        # Force a vertical split: rebuild with tall area
        engine = TilingEngine("bsp")
        engine.sync(["a", "b", "c"], "a", (0, 0, 800, 1600))
        rects = engine._rects
        if rects["a"][3] < rects["a"][2]:  # a is in a horizontal (top/bottom) split
            engine.space().focused = "a"
            engine.handle("focus_down")
            self.assertNotEqual(engine.space().focused, "a")

    def test_focus_at_edge_stays(self):
        engine = make_engine(["a", "b"])
        engine.space().focused = "b"  # rightmost
        engine.handle("focus_right")
        self.assertEqual(engine.space().focused, "b")


class WorkspaceTests(unittest.TestCase):
    def test_switch_hides_and_shows(self):
        engine = make_engine(["a", "b"])
        ops = engine.handle("workspace_2")
        self.assertIn(("hide", "a"), ops)
        self.assertIn(("hide", "b"), ops)
        self.assertEqual(engine.current, 1)

    def test_send_moves_window_between_workspaces(self):
        engine = make_engine(["a", "b"])
        engine.space().focused = "a"
        engine.handle("send_workspace_2")
        self.assertEqual(engine.current, 1)
        self.assertIn("a", engine.space().windows())
        self.assertNotIn("a", engine.spaces[0].windows())
        engine.handle("workspace_1")
        self.assertIn("b", engine.space().windows())
        self.assertNotIn("a", engine.space().windows())

    def test_new_windows_go_to_current_workspace(self):
        engine = make_engine(["a"])
        engine.handle("workspace_3")
        engine.sync(["a", "new"], "new", engine.area)
        self.assertIn("new", engine.space().windows())
        self.assertNotIn("new", engine.spaces[0].windows())

    def test_eviction_after_two_missed_syncs(self):
        engine = make_engine(["a", "b"])
        engine.sync(["a"], "a", engine.area)       # first miss: kept
        self.assertIn("b", engine.space().windows())
        engine.sync(["a"], "a", engine.area)       # second miss: evicted
        self.assertNotIn("b", engine.space().windows())

    def test_toggle_float(self):
        engine = make_engine(["a", "b"])
        engine.handle("toggle_float")
        space = engine.space()
        self.assertIn("b", space.floats)
        self.assertEqual(len(tiling._leaves(space.root)), 1)
        engine.handle("toggle_float")
        self.assertNotIn("b", space.floats)
        self.assertEqual(len(tiling._leaves(space.root)), 2)


class DragTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.engine = make_engine(["a", "b"])
        self.drag = DragController(self.engine, self.backend)

    def test_press_on_caption_hijacks(self):
        rect = self.engine._rects["a"]
        self.assertTrue(self.drag.press(rect[0] + 10, rect[1] + 5))
        self.assertEqual(self.drag.mode, "move")

    def test_press_inside_body_passes_through(self):
        rect = self.engine._rects["a"]
        self.assertFalse(self.drag.press(rect[0] + 10, rect[1] + rect[3] - 10))
        self.assertIsNone(self.drag.mode)

    def test_press_on_edge_resizes(self):
        rect = self.engine._rects["a"]
        self.assertTrue(self.drag.press(rect[0] + rect[2] - 3, rect[1] + 40))
        self.assertEqual(self.drag.mode, "resize")
        self.assertIn("right", self.drag.edges)

    def test_move_drag_then_swap_on_drop(self):
        rect_a, rect_b = self.engine._rects["a"], self.engine._rects["b"]
        self.drag.press(rect_a[0] + 10, rect_a[1] + 5)
        self.drag.move(rect_b[0] + 100, rect_b[1] + 100)
        ops = self.drag.release(rect_b[0] + 100, rect_b[1] + 100)
        self.assertTrue(ops)
        self.assertEqual(self.engine._rects["a"], rect_b)
        self.assertEqual(self.engine._rects["b"], rect_a)

    def test_drop_on_empty_ground_floats(self):
        rect = self.engine._rects["a"]
        self.drag.press(rect[0] + 10, rect[1] + 5)
        self.drag.move(5000, 5000)
        self.drag.release(5000, 5000)
        self.assertIn("a", self.engine.space().floats)

    def test_resize_then_adapt(self):
        rect = self.engine._rects["a"]
        self.drag.press(rect[0] + rect[2] - 3, rect[1] + 40)
        self.drag.move(rect[0] + rect[2] // 2, rect[1] + 40)
        self.backend.rects["a"] = (rect[0], rect[1], rect[2] // 2, rect[3])
        ops = self.drag.release(rect[0] + rect[2] // 2, rect[1] + 40)
        self.assertTrue(ops)
        self.assertTrue(overlap_free(self.engine._rects))


if __name__ == "__main__":
    unittest.main()
