"""Pure capture state checks; these tests never install global input hooks."""
import unittest

from precision_kvm.windows import WindowsCapture


class CaptureStateTests(unittest.TestCase):
    def test_invalid_side(self):
        with self.assertRaises(ValueError):
            WindowsCapture(lambda event: None, "diagonal")

    def test_edges_and_safe_return(self):
        for side, point in (("left", (0, 100)), ("right", (1919, 100)),
                            ("top", (100, 0)), ("bottom", (100, 1079))):
            capture = WindowsCapture(lambda event: None, side)
            capture._size = (1920, 1080)
            self.assertTrue(capture._at_edge(*point))
            self.assertFalse(capture._at_edge(100, 100))
            x, y = capture._return_point()
            self.assertGreater(x, 0)
            self.assertLess(x, 1919)
            self.assertGreater(y, 0)
            self.assertLess(y, 1079)

    def test_callback_failure_recorded(self):
        def fail(event):
            raise RuntimeError("queue unavailable")
        capture = WindowsCapture(fail)
        self.assertFalse(capture._emit({"type": "move", "dx": 1, "dy": 2}))
        self.assertEqual(capture.error, "queue unavailable")
        self.assertFalse(capture.active)

    def test_callback_rejection(self):
        capture = WindowsCapture(lambda event: False)
        self.assertFalse(capture._emit({"type": "activate"}))
        self.assertFalse(capture.active)


if __name__ == "__main__":
    unittest.main()
