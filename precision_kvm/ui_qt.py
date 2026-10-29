"""PySide6 control panel — native desktop UI for PrecisionKVM."""
from __future__ import annotations

import logging
import queue
import subprocess
import sys
import threading

from . import geometry
from .config import load_settings, state_dir
from .keymap import KEY_NAMES, MAC_KEY_NAMES, normalize_keymap
from .session import HostSession
from . import ui_common

ACCENT = "#3fd3bd"
BG = "#0d1420"
CARD = "#141d2c"
EDGE = "#24344a"
INK = "#e6edf7"
MUTED = "#8b98ad"
DANGER = "#ff7a8a"

QSS = f"""
QMainWindow {{ background: {BG}; }}
QWidget {{ color: {INK}; font-family: "Segoe UI"; font-size: 13px; }}
QListWidget#nav {{ background: {CARD}; border: none; outline: none;
    padding: 12px 8px; font-size: 14px; }}
QListWidget#nav::item {{ padding: 12px 18px; border-radius: 8px; color: {MUTED}; }}
QListWidget#nav::item:selected {{ background: #1e3048; color: white; }}
QListWidget#nav::item:hover {{ background: #1a2536; }}
QFrame#card {{ background: {CARD}; border: 1px solid {EDGE}; border-radius: 12px; }}
QLabel#title {{ font-size: 24px; font-weight: 700; color: white; }}
QLabel#subtitle {{ color: {MUTED}; }}
QLabel#section {{ color: {MUTED}; font-size: 11px; font-weight: 600;
    letter-spacing: 1px; }}
QLabel#status {{ font-size: 14px; font-weight: 600; }}
QPushButton {{ background: #1d2a3e; border: 1px solid {EDGE};
    border-radius: 8px; padding: 10px 18px; }}
QPushButton:hover {{ background: #233349; }}
QPushButton:pressed {{ background: #2a3d58; }}
QPushButton#accent {{ background: {ACCENT}; color: #08201c;
    font-weight: 700; border: none; }}
QPushButton#accent:hover {{ background: #5ce0ca; }}
QPushButton#danger {{ background: #3a2230; color: {DANGER};
    border: 1px solid #5a3245; }}
QPushButton#danger:hover {{ background: #4a2a3c; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: #0f1826; border: 1px solid {EDGE}; border-radius: 8px;
    padding: 8px 10px; selection-background-color: #23475e; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 28px; }}
QComboBox QAbstractItemView {{ background: #0f1826; border: 1px solid {EDGE}; }}
QCheckBox {{ spacing: 10px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px;
    border: 1px solid {EDGE}; background: #0f1826; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QPlainTextEdit#log {{ background: #0a101b; border: 1px solid {EDGE};
    border-radius: 8px; color: #b8c6dd; font-family: "Cascadia Mono", monospace;
    font-size: 12px; }}
QGraphicsView {{ background: #0a101b; border: 1px solid {EDGE};
    border-radius: 12px; }}
QScrollArea {{ border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {EDGE}; border-radius: 5px;
    min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QToolTip {{ background: {CARD}; color: {INK}; border: 1px solid {EDGE}; }}
"""


def run_ui(start: bool = False) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    logging.basicConfig(filename=str(state_dir() / "host.log"), level=logging.INFO,
                        format="%(asctime)s %(message)s")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("PrecisionKVM")
    app.setStyleSheet(QSS)
    window = Window()
    window.show()
    if start:
        QTimer.singleShot(600, window.panel.start)
    sys.exit(app.exec())


try:
    from PySide6.QtWidgets import QMainWindow as _WindowBase
except ImportError:  # module still imports headless for tests
    _WindowBase = object


class Window(_WindowBase):
    def __init__(self):
        super().__init__()
        self.panel = Panel(self)
        self.panel.build()

    def closeEvent(self, event):
        self.panel.on_close(event)


class MonitorCanvas:
    """Local monitors fixed; peers freely draggable with magnetic snapping."""
    def __init__(self, panel):
        from PySide6.QtWidgets import QGraphicsScene
        self.panel = panel
        self.scene = QGraphicsScene(panel.window)
        self.mons, self.peers = ui_common.load_layout(panel.settings)
        self.items: dict = {}
        self.view = None
        self._scale = 1.0
        self._origin = (0.0, 0.0)

    def attach(self, view):
        self.view = view
        view.setScene(self.scene)
        self.redraw()

    def _transform(self):
        w = max(self.view.viewport().width(), 640)
        h = max(self.view.viewport().height(), 300)
        self._scale, ox, oy = ui_common.layout_transform(self.mons, self.peers, w, h)
        self._origin = (ox, oy)
        return self._scale, ox, oy

    def redraw(self):
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QBrush, QColor, QFont, QPen
        self.scene.clear()
        self.items = {}
        scale, ox, oy = self._transform()
        font = QFont("Segoe UI", 9, QFont.Bold)
        for rect in self.mons:
            item = self.scene.addRect(QRectF(rect[0] * scale + ox, rect[1] * scale + oy,
                                             rect[2] * scale, rect[3] * scale),
                                      QPen(QColor("#4d6a8c"), 1.5),
                                      QBrush(QColor("#2b4a75")))
            self._label(item, "THIS PC", font)
        for name, rect in self.peers.items():
            item = self.scene.addRect(QRectF(rect[0] * scale + ox, rect[1] * scale + oy,
                                             rect[2] * scale, rect[3] * scale),
                                      QPen(QColor(ACCENT), 1.5),
                                      QBrush(QColor("#1f7a6c")))
            item.setFlag(item.GraphicsItemFlag.ItemIsMovable, True)
            item.setFlag(item.GraphicsItemFlag.ItemSendsGeometryChanges, True)
            item.setCursor(Qt.CursorShape.SizeAllCursor)
            item.setZValue(1)
            item.setData(0, name)
            self._label(item, name.upper(), font)
            self.items[name] = item
        self._draw_overlap()

    def _label(self, item, text, font):
        from PySide6.QtCore import Qt
        label = self.scene.addText(text, font)
        label.setDefaultTextColor(Qt.GlobalColor.white)
        label.setZValue(item.zValue() + 0.1)
        item.setData(1, label)
        self.position_label(item)

    def position_label(self, item):
        label = item.data(1)
        if not label:
            return
        rect = item.sceneBoundingRect()
        bounds = label.boundingRect()
        label.setPos(rect.center().x() - bounds.width() / 2,
                     rect.center().y() - bounds.height() / 2)

    def _draw_overlap(self):
        """Glowing segments on shared boundaries = where the cursor crosses."""
        from PySide6.QtGui import QColor, QPen
        pen = QPen(QColor(ACCENT), 3)
        scale, ox, oy = self._scale, *self._origin
        for rect in self.peers.values():
            for m in self.mons:
                mx, my, mw, mh = m
                lo_x = max(rect[0], mx)
                hi_x = min(rect[0] + rect[2], mx + mw)
                if hi_x > lo_x:
                    for edge_y in (my, my + mh):
                        if abs(rect[1] + rect[3] - edge_y) <= geometry.EDGE_GAP or \
                           abs(rect[1] - edge_y) <= geometry.EDGE_GAP:
                            self.scene.addLine(lo_x * scale + ox, edge_y * scale + oy,
                                               hi_x * scale + ox, edge_y * scale + oy,
                                               pen).setZValue(2)
                lo_y = max(rect[1], my)
                hi_y = min(rect[1] + rect[3], my + mh)
                if hi_y > lo_y:
                    for edge_x in (mx, mx + mw):
                        if abs(rect[0] + rect[2] - edge_x) <= geometry.EDGE_GAP or \
                           abs(rect[0] - edge_x) <= geometry.EDGE_GAP:
                            self.scene.addLine(edge_x * scale + ox, lo_y * scale + oy,
                                               edge_x * scale + ox, hi_y * scale + oy,
                                               pen).setZValue(2)

    def snap_item(self, item):
        """Virtual-space snap, then re-render the item position."""
        name = item.data(0)
        rect = item.sceneBoundingRect()
        scale, ox, oy = self._scale, *self._origin
        virtual = (round((rect.x() - ox) / scale), round((rect.y() - oy) / scale),
                   round(rect.width() / scale), round(rect.height() / scale))
        snapped = geometry.snap(virtual, self.mons,
                                threshold=max(8, round(16 / scale)))
        if snapped != virtual:
            item.setPos(snapped[0] * scale + ox, snapped[1] * scale + oy)
            self.position_label(item)

    def commit(self):
        scale, ox, oy = self._scale, *self._origin
        rects = {}
        for name, item in self.items.items():
            rect = item.sceneBoundingRect()
            vx = round((rect.x() - ox) / scale)
            vy = round((rect.y() - oy) / scale)
            rects[name] = (vx, vy, self.peers[name][2], self.peers[name][3])
        self.peers = rects
        self.panel.peers_changed(rects)


class Panel:
    def __init__(self, window):
        self.window = window
        self.settings = load_settings()
        self.session: HostSession | None = None
        self.messages: queue.Queue[str] = queue.Queue(maxsize=256)
        self.keymap = dict(self.settings["keymap"])
        self._fields: dict = {}
        self._tray = None
        self._closing = False
        self._last_viewport = None

    def build(self):
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap
        from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget,
            QListWidgetItem, QMenu, QPushButton, QStackedWidget, QSystemTrayIcon,
            QVBoxLayout, QWidget)

        window = self.window
        window.setWindowTitle("PrecisionKVM")
        window.resize(1020, 780)
        window.setMinimumSize(880, 680)

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(190)
        for label in ("Overview", "Layout", "Sharing", "Keys", "Network", "Logs"):
            QListWidgetItem(label, self.nav)
        root.addWidget(self.nav)

        right = QWidget()
        column = QVBoxLayout(right)
        column.setContentsMargins(26, 22, 26, 18)
        column.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("PrecisionKVM")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch(1)
        self.dot = QLabel("●")
        self.dot.setStyleSheet(f"color: {MUTED}; font-size: 16px;")
        header.addWidget(self.dot)
        column.addLayout(header)
        sub = QLabel("Low-latency input and clipboard sharing · Windows ⇄ macOS")
        sub.setObjectName("subtitle")
        column.addWidget(sub)

        self.pages = QStackedWidget()
        column.addWidget(self.pages, 1)
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self._build_overview()
        self._build_layout()
        self._build_sharing()
        self._build_keys()
        self._build_network()
        self._build_logs()
        self.nav.setCurrentRow(0)
        root.addWidget(right, 1)
        window.setCentralWidget(central)

        bar = QHBoxLayout()
        self.start_button = QPushButton("Start sharing")
        self.start_button.setObjectName("accent")
        self.start_button.clicked.connect(self.start)
        bar.addWidget(self.start_button)
        stop = QPushButton("Stop")
        stop.setObjectName("danger")
        stop.clicked.connect(self.stop)
        bar.addWidget(stop)
        bar.addStretch(1)
        other = "flet" if self.settings.get("ui") == "qt" else "qt"
        switch = QPushButton(f"Try {ui_common.FRAMEWORKS[other]} UI")
        switch.setToolTip("Restart the app with the alternate interface")
        switch.clicked.connect(lambda: self._switch(other))
        bar.addWidget(switch)
        save = QPushButton("Save settings")
        save.clicked.connect(self.save)
        bar.addWidget(save)
        column.addLayout(bar)
        self.status_label = QLabel("Stopped · local input active")
        self.status_label.setObjectName("status")
        column.addWidget(self.status_label)
        self.stats_label = QLabel("No session measurements yet")
        self.stats_label.setObjectName("subtitle")
        column.addWidget(self.stats_label)

        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QBrush(QColor(ACCENT)))
        painter.drawRoundedRect(6, 6, 52, 52, 14, 14)
        painter.end()
        icon = QIcon(pixmap)
        window.setWindowIcon(icon)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(icon, window)
            menu = QMenu()
            menu.addAction("Open PrecisionKVM", self._restore)
            menu.addAction("Quit", self._quit)
            self._tray.setContextMenu(menu)
            self._tray.activated.connect(
                lambda reason: self._restore()
                if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
            self._tray.show()

        self._timer = QTimer(window)
        self._timer.timeout.connect(self._poll)
        self._timer.start(120)

    # ----- pages ----------------------------------------------------------

    def _page(self):
        from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)
        scroll.setWidget(inner)
        self.pages.addWidget(scroll)
        return inner

    def _card(self, page, title):
        from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        section = QLabel(title.upper())
        section.setObjectName("section")
        layout.addWidget(section)
        page.layout().addWidget(card)
        return layout

    def _field(self, card, label, key, values=None, numeric=False):
        from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QHBoxLayout,
                                       QLabel, QLineEdit, QSpinBox)
        row = QHBoxLayout()
        row.addWidget(QLabel(label), 1)
        if values:
            control = QComboBox()
            control.addItems([str(v) for v in values])
            control.setCurrentText(str(self.settings.get(key, values[0])))
            self._fields[key] = ("combo", control)
        elif numeric:
            if key in ("gain", "scroll_gain_x", "scroll_gain_y",
                       "emergency_escape_seconds"):
                control = QDoubleSpinBox()
                control.setRange(0.1, 10)
                control.setSingleStep(0.1)
                control.setValue(float(self.settings[key]))
            else:
                control = QSpinBox()
                control.setRange(1, 65535)
                control.setValue(int(self.settings[key]))
            self._fields[key] = ("num", control)
        else:
            control = QLineEdit(str(self.settings.get(key, "")))
            self._fields[key] = ("text", control)
        row.addWidget(control, 1)
        card.addLayout(row)

    def _check(self, card, label, key):
        from PySide6.QtWidgets import QCheckBox
        control = QCheckBox(label)
        control.setChecked(bool(self.settings.get(key, False)))
        self._fields[key] = ("check", control)
        card.addWidget(control)

    def _build_overview(self):
        from PySide6.QtWidgets import QLabel
        page = self._page()
        card = self._card(page, "Status")
        self.overview_status = QLabel("Stopped · local input active")
        self.overview_status.setWordWrap(True)
        card.addWidget(self.overview_status)
        card = self._card(page, "How it works")
        hint = QLabel("Arrange your monitors in the Layout page — the Mac display "
                      "can sit along any edge or corner of any screen.\n\n"
                      "Crossing happens wherever the displays touch. Adjust "
                      "ports, encryption and discovery under Network.")
        hint.setObjectName("subtitle")
        hint.setWordWrap(True)
        card.addWidget(hint)
        page.layout().addStretch(1)

    def _build_layout(self):
        from PySide6.QtCore import QEvent, QObject
        from PySide6.QtGui import QPainter
        from PySide6.QtWidgets import QGraphicsView, QLabel, QVBoxLayout, QWidget
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        view = QGraphicsView()
        view.setRenderHint(QPainter.RenderHint.Antialiasing)
        view.setMinimumHeight(380)
        layout.addWidget(view, 1)
        self.canvas = MonitorCanvas(self)
        self.canvas.attach(view)
        hint = QLabel("Drag MAC anywhere — it snaps flush to any monitor edge or "
                      "corner. The glowing segment is where the cursor crosses.")
        hint.setObjectName("subtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.pages.addWidget(widget)

        canvas = self.canvas
        scene = canvas.scene

        class Filter(QObject):
            def eventFilter(self, obj, event):
                if event.type() == QEvent.Type.GraphicsSceneMouseMove:
                    grabbed = scene.mouseGrabberItem()
                    if grabbed in canvas.items.values():
                        canvas.position_label(grabbed)
                elif event.type() == QEvent.Type.GraphicsSceneMouseRelease:
                    for item in canvas.items.values():
                        canvas.snap_item(item)
                    canvas.commit()
                    canvas.redraw()
                return False

        scene.installEventFilter(Filter(scene))

    def _build_sharing(self):
        page = self._page()
        card = self._card(page, "Mouse")
        self._field(card, "Mouse update limit (Hz)", "mouse_hz",
                    ["120", "144", "240", "500", "1000"])
        self._field(card, "Pointer mode", "mouse_mode", ["raw", "system"])
        self._field(card, "Pointer sensitivity", "gain", numeric=True)
        card = self._card(page, "Scrolling")
        self._field(card, "Vertical sensitivity", "scroll_gain_y", numeric=True)
        self._field(card, "Horizontal sensitivity", "scroll_gain_x", numeric=True)
        self._field(card, "Scroll units", "scroll_mode", ["pixel", "line"])
        self._check(card, "Reverse vertical", "scroll_invert_y")
        self._check(card, "Reverse horizontal", "scroll_invert_x")
        card = self._card(page, "Safety")
        self._field(card, "Hold Esc to stop (seconds)", "emergency_escape_seconds",
                    numeric=True)
        self._check(card, "Bridge Windows desktop gestures to Mac", "gesture_bridge")
        self._check(card, "Native four-finger swipes (experimental)", "native_touchpad")
        page.layout().addStretch(1)

    def _build_keys(self):
        from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel,
                                       QListWidget, QPushButton)
        page = self._page()
        card = self._card(page, "Mac behaviour")
        self._check(card, "Mac-style shortcuts on this PC — Alt acts as Command "
                    "(Alt+C copy, Alt+Space search, Alt+Q quit; Alt+Tab unchanged)",
                    "mac_mode")
        self._check(card, "Lock the Mac when this PC locks "
                    "(input is never shared on the lock screen)", "lock_sync")
        self._field(card, "Keyboard preset on the Mac", "keymap_preset",
                    ["option_cmd", "native", "ctrl_cmd", "mac_layout"])
        self._check(card, "Home / End move to line start / end", "windows_home_end")
        self._check(card, "Ctrl + arrows navigate words", "windows_word_nav")
        card = self._card(page, "Custom remapping")
        self.key_list = QListWidget()
        self.key_list.setMaximumHeight(160)
        card.addWidget(self.key_list)
        row = QHBoxLayout()
        self.source_key = QComboBox()
        self.source_key.setEditable(True)
        self.source_key.addItems(KEY_NAMES)
        self.target_key = QComboBox()
        self.target_key.setEditable(True)
        self.target_key.addItems(MAC_KEY_NAMES)
        row.addWidget(self.source_key, 1)
        row.addWidget(QLabel("→"))
        row.addWidget(self.target_key, 1)
        add = QPushButton("Add / replace")
        add.clicked.connect(self._add_mapping)
        remove = QPushButton("Remove")
        remove.setObjectName("danger")
        remove.clicked.connect(self._remove_mapping)
        row.addWidget(add)
        row.addWidget(remove)
        card.addLayout(row)
        self._refresh_keymap()
        page.layout().addStretch(1)

    def _build_network(self):
        from PySide6.QtWidgets import QPushButton
        page = self._page()
        card = self._card(page, "Connection")
        self._field(card, "Mac address", "peer")
        self._field(card, "Port", "port", numeric=True)
        self._field(card, "Mac SSH target (user@host)", "ssh_target")
        self._check(card, "Encrypt connections (TLS) — recommended", "tls")
        self._check(card, "Advertise this PC on the network (mDNS)", "discovery")
        card = self._card(page, "Clipboard")
        self._check(card, "Share clipboard text and images both ways",
                    "clipboard_enabled")
        self._field(card, "Maximum size (MiB)", "clipboard_max_mb", numeric=True)
        export = QPushButton("Export pairing file for Mac…")
        export.clicked.connect(self._export_pairing)
        card.addWidget(export)
        page.layout().addStretch(1)

    def _build_logs(self):
        from PySide6.QtWidgets import (QHBoxLayout, QPlainTextEdit, QPushButton,
                                       QVBoxLayout, QWidget)
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("log")
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, 1)
        row = QHBoxLayout()
        refresh = QPushButton("Reload log file")
        refresh.clicked.connect(self._reload_logs)
        row.addWidget(refresh)
        folder = QPushButton("Open log folder")
        folder.clicked.connect(lambda: subprocess.Popen(["explorer", str(state_dir())]))
        row.addWidget(folder)
        row.addStretch(1)
        layout.addLayout(row)
        self.pages.addWidget(widget)

    # ----- keymap ---------------------------------------------------------

    def _refresh_keymap(self):
        self.key_list.clear()
        for source, target in sorted(self.keymap.items()):
            self.key_list.addItem(f"{source}  →  {target}")

    def _add_mapping(self):
        try:
            self.keymap.update(normalize_keymap(
                {self.source_key.currentText(): self.target_key.currentText()}))
            self._refresh_keymap()
        except ValueError as exc:
            self._set_status(str(exc), "bad")

    def _remove_mapping(self):
        for item in self.key_list.selectedItems():
            self.keymap.pop(item.text().split("  →  ")[0], None)
        self._refresh_keymap()

    # ----- settings --------------------------------------------------------

    def collect_settings(self):
        values = {}
        for key, (kind, control) in self._fields.items():
            if kind == "check":
                values[key] = control.isChecked()
            elif kind == "num":
                values[key] = control.value()
            elif kind == "combo":
                text = control.currentText()
                values[key] = int(text) if text.isdigit() else text
            else:
                values[key] = control.text()
        settings = dict(self.settings, keymap=dict(self.keymap), **values)
        return ui_common.collect(self.settings, settings)

    def peers_changed(self, rects):
        self.settings = dict(self.settings,
                             peers={name: {"x": r[0], "y": r[1], "w": r[2], "h": r[3]}
                                    for name, r in rects.items()})
        self.save(quiet=True)
        if self.session and self.session.capture:
            self.session.capture.peers = dict(rects)

    def save(self, quiet=False):
        try:
            self.settings = self.collect_settings()
            ui_common.persist(self.settings)
            if not quiet:
                self._set_status("Settings saved · start the host to apply", "idle")
        except Exception as exc:
            self._set_status(str(exc), "bad")

    def _export_pairing(self):
        from pathlib import Path
        from PySide6.QtWidgets import QFileDialog
        try:
            settings = self.collect_settings()
            destination, _ = QFileDialog.getSaveFileName(
                self.window, "Export private Mac pairing",
                "PrecisionKVM-pairing.json", "Pairing file (*.json)")
            if not destination:
                return
            ui_common.export_pairing(settings, Path(destination))
            self._set_status("Pairing exported · import privately in the Mac app", "good")
        except Exception as exc:
            self._set_status(f"Could not export pairing: {exc}", "bad")

    def _switch(self, framework):
        running = self.session is not None
        if self.session:
            self.session.stop()
            self.session = None
        ui_common.switch_ui(framework, start=running)

    # ----- session ----------------------------------------------------------

    def start(self):
        try:
            settings = self.collect_settings()
            ui_common.persist(settings)
            self.settings = settings
            if self.session:
                self.session.stop()
            self.session = ui_common.start_host(settings, self.report_status)
            if settings.get("ssh_target"):
                threading.Thread(target=self.session.connect_mac, daemon=True,
                                 name="kvm-ssh").start()
            self.start_button.setText("Apply & reconnect")
        except Exception as exc:
            self._set_status(str(exc), "bad")

    def stop(self):
        if self.session:
            self.session.stop()
            self.session = None
        while not self.messages.empty():
            self.messages.get_nowait()
        self.start_button.setText("Start sharing")
        self._set_status("Stopped · local input active", "idle")

    def report_status(self, message):
        logging.info(message)
        try:
            self.messages.put_nowait(str(message))
        except queue.Full:
            pass

    def _set_status(self, message, tone):
        self.status_label.setText(message)
        self.overview_status.setText(message)
        colors = {"good": ACCENT, "bad": DANGER, "idle": INK}
        self.status_label.setStyleSheet(f"color: {colors.get(tone, INK)};")
        self.dot.setStyleSheet(
            f"color: {ACCENT if tone == 'good' else DANGER if tone == 'bad' else MUTED};"
            " font-size: 16px;")

    def _reload_logs(self):
        try:
            lines = (state_dir() / "host.log").read_text(
                encoding="utf-8", errors="replace").splitlines()[-400:]
        except OSError:
            lines = []
        self.log_view.setPlainText("\n".join(lines) or "No log entries yet.")

    def _poll(self):
        while not self.messages.empty():
            message = self.messages.get_nowait()
            self.log_view.appendPlainText(message)
            self._set_status(message, ui_common.status_tone(message))
        if self.session:
            self.stats_label.setText(ui_common.stats_line(self.session))
            if self.session.thread and not self.session.thread.is_alive():
                self.start_button.setText("Start sharing")
        viewport = self.canvas.view.viewport().size() if self.canvas.view else None
        if viewport and viewport != self._last_viewport:
            self._last_viewport = viewport
            self.canvas.redraw()

    # ----- tray / close ------------------------------------------------------

    def on_close(self, event):
        if self._tray and not self._closing:
            event.ignore()
            self.window.hide()
            self._tray.showMessage("PrecisionKVM", "Still sharing in the tray",
                                   self._tray.icon(), 2000)
        else:
            self.stop()
            event.accept()

    def _restore(self):
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def _quit(self):
        self._closing = True
        self.stop()
        if self._tray:
            self._tray.hide()
        self.window.close()
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()
