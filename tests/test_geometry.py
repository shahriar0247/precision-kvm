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

    def test_snap_to_edge_nearest_side_keeps_along_position(self):
        # dropped on the right: lands flush, keeps y
        snapped = geometry.snap_to_edge((1000, 200, 1920, 400), MON)
        self.assertEqual((snapped[0], snapped[1]), (1920, 200))
        # dropped above the top edge: snaps up, keeps x
        snapped = geometry.snap_to_edge((400, -500, 1920, 400), MON)
        self.assertEqual((snapped[0], snapped[1]), (400, -400))
        # bottom edge
        snapped = geometry.snap_to_edge((400, 1300, 1920, 400), MON)
        self.assertEqual((snapped[0], snapped[1]), (400, 1080))

    def test_snap_to_edge_clamps_to_facing_monitor(self):
        # staggered monitors: union left edge is faced only by the upper monitor
        staggered = [(0, 0, 1920, 1080), (1920, 540, 1920, 1080)]
        # dropped overlapping the PC's left area: snaps flush left with real overlap
        snapped = geometry.snap_to_edge((-500, 500, 1920, 1080), staggered)
        self.assertEqual(snapped[0], -1920)
        overlap = min(snapped[1] + snapped[3], 1080) - max(snapped[1], 0)
        self.assertGreater(overlap, 0)
        # crossing must be detected inside the snapped rect
        self.assertIsNotNone(geometry.hit(staggered, {"mac": snapped}, 0, snapped[1] + 5))
        # dropped below every left-facing span: slides into overlap anyway
        snapped = geometry.snap_to_edge((-500, 700, 1920, 380), staggered)
        self.assertEqual(snapped[0], -1920)
        overlap = min(snapped[1] + snapped[3], 1080) - max(snapped[1], 0)
        self.assertGreater(overlap, 0)

    def test_multi_peer_hit_resolution(self):
        peers = {"mac": (-1920, 0, 1920, 1080), "linux": (1920, 300, 1920, 1080)}
        self.assertEqual(geometry.hit(MON, peers, 0, 500)[0], "mac")
        self.assertEqual(geometry.hit(MON, peers, 1919, 500)[0], "linux")
        # "linux" only overlaps y>=300: crossing above it goes nowhere
        self.assertIsNone(geometry.hit(MON, peers, 1919, 100))

    def test_default_peer_matches_legacy_model(self):
        rect = geometry.default_peer(MON, "left", align=0.5, span=0.6)
        self.assertEqual(rect[0] + rect[2], 0)
        self.assertAlmostEqual(rect[1] + rect[3] / 2, 540, delta=2)
        rect = geometry.default_peer(MON, "top", align=0.25, span=0.5)
        self.assertEqual(rect[1] + rect[3], 0)
        self.assertAlmostEqual(rect[0] + rect[2] / 2, 480, delta=2)


if __name__ == "__main__":
    unittest.main()
