import unittest

from precision_kvm import geometry


MON = [(0, 0, 1920, 1080)]
DUAL = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]  # side-by-side desktops


class GeometryTest(unittest.TestCase):
    def test_edge_crossing_left(self):
        peers = {"mac": (-1920, 0, 1920, 1080)}
        hit = geometry.hit(MON, peers, 0, 500)
        self.assertIsNotNone(hit)
        name, side, nx, ny = hit
        self.assertEqual((name, side), ("mac", "left"))
        self.assertAlmostEqual(nx, 1.0, places=2)   # enters peer's right edge
        self.assertAlmostEqual(ny, 500 / 1079, places=2)
        self.assertIsNone(geometry.hit(MON, peers, 100, 500))

    def test_partial_overlap_limits_crossing(self):
        peers = {"mac": (-1920, 700, 1920, 380)}  # only the lower band touches
        self.assertIsNotNone(geometry.hit(MON, peers, 0, 800))
        self.assertIsNone(geometry.hit(MON, peers, 0, 200))

    def test_corner_crossing(self):
        peers = {"mac": (-1920, -1080, 1920, 1080)}  # up-left, flush corner
        hit = geometry.hit(MON, peers, 0, 0)
        self.assertIsNotNone(hit)
        name, side, nx, ny = hit
        self.assertAlmostEqual(nx, 1.0)
        self.assertAlmostEqual(ny, 1.0)

    def test_gap_too_wide_blocks_crossing(self):
        peers = {"mac": (-2400, 0, 1920, 1080)}  # 480px gap
        self.assertIsNone(geometry.hit(MON, peers, 0, 500))

    def test_second_monitor_blocks_but_edges_still_cross(self):
        peers = {"mac": (-1920, 0, 1920, 1080), "tv": (3840, 0, 1920, 1080)}
        # boundary between the two local monitors is interior: no crossing
        self.assertIsNone(geometry.hit(DUAL, peers, 1919, 500))
        self.assertEqual(geometry.hit(DUAL, peers, 0, 500)[1], "left")
        right = geometry.hit(DUAL, peers, 3839, 500)
        self.assertEqual((right[0], right[1]), ("tv", "right"))

    def test_return_point_lands_inside_union(self):
        peers = {"mac": (-1920, 0, 1920, 1080)}
        x, y = geometry.return_point(MON, peers["mac"], 0.5, 0.5)
        self.assertEqual((x, y), (2, 540))
        # corner return lands at the adjacent corner, nudged inward
        x, y = geometry.return_point(MON, (-1920, -1080, 1920, 1080), 1.0, 1.0)
        self.assertEqual((x, y), (2, 2))

    def test_snap_pulls_edges_flush(self):
        mons = [(0, 0, 1920, 1080)]
        # peer nearly flush on the left, shifted down a bit
        snapped = geometry.snap((-1932, 40, 1920, 1080), mons, threshold=20)
        self.assertEqual(snapped[0] + snapped[2], 0)
        # corner snap: both axes pull in
        snapped = geometry.snap((-1930, -1075, 1920, 1080), mons, threshold=20)
        self.assertEqual((snapped[0] + snapped[2], snapped[1] + snapped[3]), (0, 0))

    def test_default_peer_matches_legacy_model(self):
        rect = geometry.default_peer(MON, "left", align=0.5, span=0.6)
        self.assertEqual(rect[0] + rect[2], 0)
        self.assertAlmostEqual(rect[1] + rect[3] / 2, 540, delta=2)
        rect = geometry.default_peer(MON, "top", align=0.25, span=0.5)
        self.assertEqual(rect[1] + rect[3], 0)
        self.assertAlmostEqual(rect[0] + rect[2] / 2, 480, delta=2)


if __name__ == "__main__":
    unittest.main()
