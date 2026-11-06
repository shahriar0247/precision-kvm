"""Platform-neutral tiling engine, chord protocol and drag controller.

Pure logic only — platform backends (tiling_win, tiling_mac) enumerate
windows and apply the operations returned here. Rects are (x, y, w, h)
with a top-left origin, matching both the Windows virtual desktop and
macOS CG global coordinates.

Layouts: "bsp" keeps a binary-split tree per workspace (each admitted
window splits the focused tile, axis chosen by the tile's aspect);
"horizontal" lays windows in columns and scrolls so the focused window
always stays fully on screen.
"""
from __future__ import annotations

import sys
import threading

GAP = 6               # pixels between tiles
EDGE = 12             # resize grab zone, pixels from a tile edge
CAPTION = 30          # move-drag band at the top of a tile, pixels
MIN_TILE = 96         # never lay out a tile smaller than this
WORKSPACES = 6

ARROWS = {0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down"}
DIGITS = {0x31: 1, 0x32: 2, 0x33: 3, 0x34: 4, 0x35: 5, 0x36: 6}


def chord_action(vk: int, mods: frozenset[str]) -> str | None:
    """Map a virtual-key chord to a tiling action; None when not ours.

    `mods` holds any of 'ctrl', 'win', 'alt', 'shift'. Arrow chords take
    Ctrl+Win or Ctrl+Alt — swallowing them overrides the OS desktop
    switchers on both platforms. Digits take Alt (switch workspace) or
    Ctrl+Alt (move the focused window there).
    """
    if vk in ARROWS and "ctrl" in mods and ("win" in mods) != ("alt" in mods):
        return "focus_" + ARROWS[vk]
    if vk in DIGITS and "alt" in mods:
        if "ctrl" in mods:
            return f"send_workspace_{DIGITS[vk]}"
        if "win" not in mods and "shift" not in mods:
            return f"workspace_{DIGITS[vk]}"
    if vk == 0x57 and "ctrl" in mods and "alt" in mods:  # W
        return "toggle_float"
    return None


class Leaf:
    __slots__ = ("win",)

    def __init__(self, win):
        self.win = win


class Split:
    __slots__ = ("axis", "fraction", "first", "second")

    def __init__(self, axis, fraction, first, second):
        self.axis, self.fraction = axis, fraction
        self.first, self.second = first, second


def _leaves(node):
    if isinstance(node, Split):
        return _leaves(node.first) + _leaves(node.second)
    if node is not None:
        return [node]
    return []


def _splittable(rect):
    """A tile must fit two MIN_TILE halves plus the gap along some axis."""
    if rect is None:
        return True  # no layout yet: trust the area
    return rect[2] - GAP >= 2 * MIN_TILE or rect[3] - GAP >= 2 * MIN_TILE


def _split_fraction(size, fraction):
    """Clamp a fraction so both halves stay at least MIN_TILE."""
    if size - GAP <= 2 * MIN_TILE:
        return 0.5
    low = MIN_TILE / (size - GAP)
    return min(1 - low, max(low, fraction))


def _layout(node, rect, out):
    """Assign a rect to every leaf under `node` inside `rect`."""
    if node is None:
        return
    if isinstance(node, Leaf):
        out[node.win] = rect
        return
    x, y, w, h = rect
    gap = GAP
    if node.axis == "x":
        cut = round((w - gap) * _split_fraction(w, node.fraction))
        _layout(node.first, (x, y, cut, h), out)
        _layout(node.second, (x + cut + gap, y, max(MIN_TILE, w - cut - gap), h), out)
    else:
        cut = round((h - gap) * _split_fraction(h, node.fraction))
        _layout(node.first, (x, y, w, cut), out)
        _layout(node.second, (x, y + cut + gap, w, max(MIN_TILE, h - cut - gap)), out)


def _prune(node, win):
    """Remove `win`'s leaf, promoting its sibling subtree in its place."""
    if isinstance(node, Leaf):
        return None if node.win == win else node
    node.first = _prune(node.first, win)
    node.second = _prune(node.second, win)
    if node.first is None:
        return node.second
    if node.second is None:
        return node.first
    return node


def _replace(node, target: Leaf, replacement):
    if node is target:
        return replacement
    if isinstance(node, Split):
        node.first = _replace(node.first, target, replacement)
        node.second = _replace(node.second, target, replacement)
    return node


def _find_leaf(node, win) -> Leaf | None:
    for leaf in _leaves(node):
        if leaf.win == win:
            return leaf
    return None


def _adapt(node, rect, actual):
    """Retune split fractions so the tree matches the real window rects."""
    if node is None or isinstance(node, Leaf):
        return
    x, y, w, h = rect
    if node.axis == "x":
        first = _leaves(node.first)
        span = (max(actual[leaf.win][0] + actual[leaf.win][2] for leaf in first
                    if leaf.win in actual) - x) if first else 0
        node.fraction = _split_fraction(w, span / (w - GAP)) if w > GAP else 0.5
        cut = round((w - GAP) * node.fraction)
        _adapt(node.first, (x, y, cut, h), actual)
        _adapt(node.second, (x + cut + GAP, y, max(1, w - cut - GAP), h), actual)
    else:
        first = _leaves(node.first)
        span = (max(actual[leaf.win][1] + actual[leaf.win][3] for leaf in first
                    if leaf.win in actual) - y) if first else 0
        node.fraction = _split_fraction(h, span / (h - GAP)) if h > GAP else 0.5
        cut = round((h - GAP) * node.fraction)
        _adapt(node.first, (x, y, w, cut), actual)
        _adapt(node.second, (x, y + cut + GAP, w, max(1, h - cut - GAP)), actual)


def _edges_at(rect, x, y):
    """Which tile edges the point sits within the grab zone of."""
    rx, ry, rw, rh = rect
    edges = set()
    if abs(x - rx) <= EDGE:
        edges.add("left")
    if abs(x - (rx + rw)) <= EDGE:
        edges.add("right")
    if abs(y - ry) <= EDGE:
        edges.add("top")
    if abs(y - (ry + rh)) <= EDGE:
        edges.add("bottom")
    return edges


class Workspace:
    __slots__ = ("root", "floats", "hidden", "focused")

    def __init__(self):
        self.root = None
        self.floats = set()
        self.hidden = set()
        self.focused = None

    def windows(self):
        return [leaf.win for leaf in _leaves(self.root)] + sorted(self.floats)


class TilingEngine:
    """Workspaces of windows laid out as BSP splits or horizontal columns."""

    def __init__(self, mode="bsp"):
        self.mode = mode if mode in ("bsp", "horizontal") else "bsp"
        self.spaces = [Workspace() for _ in range(WORKSPACES)]
        self.current = 0
        self.area = (0, 0, 1920, 1040)
        self._rects: dict = {}
        self._missing: dict = {}

    # -- reconciliation ------------------------------------------------------

    def sync(self, present, focused, area):
        """Reconcile with reality; returns layout ops for the current space.

        A window missing from two consecutive enumerations is evicted —
        one missed poll (a busy app, a transient AX failure) must not
        strip it from its workspace.
        """
        alive = set(present)
        tracked = {win for space in self.spaces for win in space.windows()}
        for win in tracked - alive:
            self._missing[win] = self._missing.get(win, 0) + 1
        for win in alive:
            self._missing.pop(win, None)
        for win, misses in list(self._missing.items()):
            if misses >= 2:
                self._evict(win)
                del self._missing[win]
        known = {win for space in self.spaces for win in space.windows()}
        for win in present:
            if win not in known:
                self._admit(self.space(), win)
                # Refresh rects so the next admission splits real tile sizes,
                # not the whole-area aspect (which builds one-sided chains).
                self._layout_ops()
        for space in self.spaces:
            if space.focused not in space.windows():
                wins = space.windows()
                space.focused = wins[0] if wins else None
        self.area = area
        return self._layout_ops()

    def _evict(self, win):
        for space in self.spaces:
            if space.root is not None:
                space.root = _prune(space.root, win)
            space.floats.discard(win)
            space.hidden.discard(win)
            if space.focused == win:
                space.focused = None

    def _admit(self, space, win):
        if space.root is None:
            space.root = Leaf(win)
            space.focused = win
            return
        anchor = _find_leaf(space.root, space.focused) if space.focused else None
        if anchor is not None:
            rect = self._rects.get(anchor.win) or self.area
            if not _splittable(rect):
                anchor = None
        else:
            rect = None
        if anchor is None or not _splittable(rect):
            # Fall back to the largest tile; float when nothing can split.
            best, best_area = None, 0
            for leaf in _leaves(space.root):
                leaf_rect = self._rects.get(leaf.win)
                if _splittable(leaf_rect):
                    area = (leaf_rect[2] * leaf_rect[3] if leaf_rect
                            else self.area[2] * self.area[3])
                    if area > best_area:
                        best, best_area = leaf, area
            anchor = best
            rect = (self._rects.get(best.win) if best else None) or self.area
        if anchor is None:
            space.floats.add(win)
            space.focused = win
            return
        axis = "x" if rect[2] >= rect[3] else "y"
        space.root = _replace(space.root, anchor,
                              Split(axis, 0.5, Leaf(anchor.win), Leaf(win)))
        space.focused = win

    # -- queries -------------------------------------------------------------

    def space(self) -> Workspace:
        return self.spaces[self.current]

    def rect_of(self, win):
        return self._rects.get(win)

    def window_at(self, x, y, exclude=None):
        for win, (rx, ry, rw, rh) in self._rects.items():
            if win != exclude and rx <= x < rx + rw and ry <= y < ry + rh:
                return win
        return None

    # -- operations ----------------------------------------------------------

    def handle(self, action: str):
        """Apply a chord action; returns backend ops."""
        if action.startswith("focus_"):
            return self._focus(action[6:])
        if action.startswith("workspace_"):
            return self._switch(int(action[10:]) - 1)
        if action.startswith("send_workspace_"):
            return self._send(int(action[15:]) - 1)
        if action == "toggle_float":
            return self._toggle_float()
        return []

    def _layout_ops(self):
        space = self.space()
        tiled = [leaf.win for leaf in _leaves(space.root)]
        rects = {}
        if self.mode == "bsp" and space.root is not None:
            _layout(space.root, self.area, rects)
        elif tiled:
            self._horizontal(tiled, space.focused, rects)
        self._rects = rects
        ops = [("rect", win, rect) for win, rect in rects.items()]
        ops.extend(("show", win) for win in list(space.hidden))
        space.hidden.clear()
        return ops

    def _horizontal(self, wins, focused, out):
        n = len(wins)
        x, y, w, h = self.area
        cols = min(n, 3)
        cw = (w - GAP * (cols - 1)) / cols
        total = n * cw + GAP * (n - 1)
        index = wins.index(focused) if focused in wins else 0
        offset = min(max(index * (cw + GAP) - (w - cw) / 2, 0.0), max(0.0, total - w))
        for j, win in enumerate(wins):
            out[win] = (round(x + j * (cw + GAP) - offset), y, round(cw), h)

    def _focus(self, direction):
        space = self.space()
        tiled = [leaf.win for leaf in _leaves(space.root)]
        if not tiled:
            return []
        current = space.focused if space.focused in tiled else tiled[0]
        if self.mode == "horizontal" and direction in ("left", "right"):
            step = 1 if direction == "right" else -1
            target = tiled[min(len(tiled) - 1, max(0, tiled.index(current) + step))]
        else:
            target = self._neighbor(current, direction, tiled)
        if target is None or target == current:
            return []
        space.focused = target
        ops = self._layout_ops()  # horizontal mode may scroll
        ops.append(("focus", target))
        return ops

    def _neighbor(self, current, direction, tiled):
        rect = self._rects.get(current)
        if rect is None:
            return None
        cx, cy = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
        best, score = None, None
        for win in tiled:
            if win == current:
                continue
            other = self._rects.get(win)
            if other is None:
                continue
            ox, oy = other[0] + other[2] / 2, other[1] + other[3] / 2
            dx, dy = ox - cx, oy - cy
            ahead = {"left": -dx, "right": dx, "up": -dy, "down": dy}[direction]
            side = abs(dy) if direction in ("left", "right") else abs(dx)
            if ahead > 0:
                value = ahead + side * 2.5
                if score is None or value < score:
                    best, score = win, value
        return best

    def _switch(self, index):
        if not 0 <= index < WORKSPACES or index == self.current:
            return []
        ops = []
        old = self.space()
        for win in old.windows():
            ops.append(("hide", win))
            old.hidden.add(win)
        self.current = index
        space = self.space()
        ops.extend(("show", win) for win in list(space.hidden))
        space.hidden.clear()
        ops.extend(self._layout_ops())
        target = space.focused if space.focused in space.windows() \
            else next(iter(space.windows()), None)
        if target is not None:
            space.focused = target
            ops.append(("focus", target))
        return ops

    def _send(self, index):
        if not 0 <= index < WORKSPACES or index == self.current:
            return []
        space = self.space()
        win = space.focused
        if win is None:
            return []
        if win in space.floats:
            space.floats.discard(win)
            self.spaces[index].floats.add(win)
        else:
            if space.root is not None:
                space.root = _prune(space.root, win)
            self._admit(self.spaces[index], win)
        space.focused = None
        return self._switch(index)

    def _toggle_float(self):
        space = self.space()
        win = space.focused
        if win is None:
            return []
        if win in space.floats:
            space.floats.discard(win)
            self._admit(space, win)
        else:
            if space.root is not None:
                space.root = _prune(space.root, win)
            space.floats.add(win)
        space.focused = win
        return self._layout_ops()

    def _toggle_float_at(self, win):
        """Float a specific window (drag-drop onto empty ground)."""
        space = self.space()
        if win in space.floats:
            return []
        space.floats.add(win)
        if space.root is not None:
            space.root = _prune(space.root, win)
        if space.focused == win:
            space.focused = None
        return self._layout_ops()

    # -- drag support ----------------------------------------------------------

    def swap(self, first, second):
        space = self.space()
        leaf_a, leaf_b = _find_leaf(space.root, first), _find_leaf(space.root, second)
        if leaf_a is not None and leaf_b is not None:
            leaf_a.win, leaf_b.win = leaf_b.win, leaf_a.win
            space.focused = first
        return self._layout_ops()

    def adapt(self, actual: dict):
        """Retune fractions from real rects after a free resize, then relayout."""
        space = self.space()
        if space.root is not None and self.mode == "bsp":
            _adapt(space.root, self.area, actual)
        return self._layout_ops()

    def relayout(self):
        return self._layout_ops()


class DragController:
    """Plain-mouse window drags: caption band moves the window, tile edges
    resize it. Hijacks the press only over tiled windows; everything else
    passes through untouched."""

    def __init__(self, engine: TilingEngine, backend):
        self.engine = engine
        self.backend = backend
        self.mode: str | None = None
        self.win = None
        self.edges: set[str] = set()
        self.grab = (0, 0)
        self.start_rect = (0, 0, 0, 0)

    @property
    def active(self):
        return self.mode is not None

    def press(self, x, y, button="left") -> bool:
        if button != "left" or self.active:
            return self.active
        win = self.engine.window_at(x, y)
        rect = self.engine.rect_of(win) if win is not None else None
        if rect is None or rect[2] < CAPTION * 2 or rect[3] < CAPTION * 2:
            return False
        edges = _edges_at(rect, x, y)
        # Outer layout boundaries cannot be resized outward.
        ax, ay, aw, ah = self.engine.area
        edges -= {"left"} if rect[0] <= ax else set()
        edges -= {"right"} if rect[0] + rect[2] >= ax + aw else set()
        edges -= {"top"} if rect[1] <= ay else set()
        edges -= {"bottom"} if rect[1] + rect[3] >= ay + ah else set()
        if edges:
            self.mode, self.edges = "resize", edges
        elif y - rect[1] <= CAPTION:
            self.mode = "move"
        else:
            return False
        self.win = win
        self.grab = (x - rect[0], y - rect[1])
        self.start_rect = rect
        return True

    def move(self, x, y):
        if self.mode == "move":
            self.backend.set_rect(self.win, (x - self.grab[0], y - self.grab[1],
                                             self.start_rect[2], self.start_rect[3]))
        elif self.mode == "resize":
            rx, ry, rw, rh = self.start_rect
            left = x if "left" in self.edges else rx
            top = y if "top" in self.edges else ry
            right = x if "right" in self.edges else rx + rw
            bottom = y if "bottom" in self.edges else ry + rh
            self.backend.set_rect(self.win, (min(left, right - MIN_TILE),
                                             min(top, bottom - MIN_TILE),
                                             max(MIN_TILE, abs(right - left)),
                                             max(MIN_TILE, abs(bottom - top))))

    def release(self, x, y):
        mode, win = self.mode, self.win
        self.mode = None
        if mode == "move":
            target = self.engine.window_at(x, y, exclude=win)
            if target is None:
                return self.engine._toggle_float_at(win)
            return self.engine.swap(win, target)
        if mode == "resize":
            actual = {w: rect for w, rect in self.backend.windows()}
            return self.engine.adapt(actual)
        return []


class TilingController:
    """Owns the engine and backend for one machine; chord and drag entry
    points are called from native hook threads and must never raise.

    A background sync loop keeps the layout live: new windows are tiled
    as they appear, closed ones pruned, and the layout reasserted —
    this is what makes the desktop actually tile, chords only steer it.
    """

    def __init__(self, backend, mode="bsp", status=print, interval=0.8):
        self.backend = backend
        self.engine = TilingEngine(mode)
        self.drag = DragController(self.engine, backend)
        self.enabled = True
        self.remote_active = False
        self._status = status
        self._interval = interval
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._loop, name="kvm-tiling-sync",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stopping.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(3)
        self._thread = None

    def _loop(self):
        while not self._stopping.wait(self._interval):
            if self.enabled and not self.drag.active:
                self.refresh()

    def set_enabled(self, value: bool):
        self.enabled = bool(value)

    def set_mode(self, mode: str):
        if mode in ("bsp", "horizontal") and mode != self.engine.mode:
            self.engine.mode = mode
            if self.enabled:
                self.refresh()

    def _safe(self, work):
        try:
            work()
        except Exception as exc:
            self._status(f"Tiling: {type(exc).__name__}: {exc}")

    def _sync_apply(self):
        """Reconcile with reality and apply pending layout ops.

        Rect ops are filtered against the windows' real positions: a window
        already sitting in its tile is left alone (no per-tick churn), while
        one the user dragged away is pulled back into its tile.
        """
        wins = self.backend.windows()
        actual = dict(wins)
        ops = self.engine.sync([win for win, _ in wins],
                               self.backend.focused(), self.backend.work_area())
        ops = [op for op in ops if op[0] != "rect" or actual.get(op[1]) != op[2]]
        if ops:
            self.backend.apply(ops)

    def refresh(self):
        self._safe(self._sync_apply)

    def handle(self, action: str):
        if self.enabled:
            self._safe(lambda: self._handle(action))

    def _handle(self, action):
        self._sync_apply()
        wins = self.backend.windows()
        actual = dict(wins)
        ops = [op for op in self.engine.handle(action)
               if op[0] != "rect" or actual.get(op[1]) != op[2]]
        if ops:
            self.backend.apply(ops)

    def press(self, x, y, button="left") -> bool:
        """True when the press was hijacked for a tiling drag."""
        if not self.enabled:
            return False
        state = []

        def work():
            self._sync_apply()
            state.append(self.drag.press(x, y, button))
        self._safe(work)
        return bool(state and state[0])

    def move(self, x, y):
        if self.drag.active:
            self._safe(lambda: self.drag.move(x, y))

    def release(self, x, y):
        if self.drag.active:
            def work():
                ops = self.drag.release(x, y)
                if ops:
                    self.backend.apply(ops)
            self._safe(work)


def make_controller(settings: dict, status=print):
    """Build a controller with the platform backend; None when unsupported."""
    if sys.platform == "darwin":
        from .tiling_mac import MacTiling
        backend = MacTiling()
    elif sys.platform == "win32":
        from .tiling_win import WindowsTiling
        backend = WindowsTiling()
    else:
        return None
    return TilingController(backend, str(settings.get("tiling_mode", "bsp")),
                            status=status)
