"""Flet (Flutter) control panel — Material 3 native rendering, no webview."""
from __future__ import annotations

import asyncio
import logging
import queue
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
PC_BLUE = "#2b4a75"
MAC_GREEN = "#1f7a6c"

PAGES = ("Overview", "Layout", "Sharing", "Keys", "Network", "Logs")


def run_ui(start: bool = False) -> None:
    import flet as ft

    logging.basicConfig(filename=str(state_dir() / "host.log"), level=logging.INFO,
                        format="%(asctime)s %(message)s")

    def main(page):
        FletPanel(page, autostart=start)

    ft.app(target=main)


class FletPanel:
    def __init__(self, page, autostart=False):
        import flet as ft
        self.ft = ft
        self.page = page
        self.settings = load_settings()
        self.session: HostSession | None = None
        self.messages: queue.Queue[str] = queue.Queue(maxsize=256)
        self.keymap = dict(self.settings["keymap"])
        self._fields: dict = {}
        self.mons, self.peers = ui_common.load_layout(self.settings)
        self._peer_box = None
        self._canvas_w, self._canvas_h = 780.0, 360.0
        self._closing = False
        self._tray = None

        page.title = "PrecisionKVM"
        page.theme_mode = ft.ThemeMode.DARK
        page.bgcolor = BG
        page.padding = 0
        page.window.width = 1040
        page.window.height = 800
        page.theme = ft.Theme(
            color_scheme=ft.ColorScheme(
                primary=ACCENT, on_primary="#08201c", surface=CARD,
                on_surface=INK, surface_container_lowest=BG,
                surface_container_highest="#1d2a3e", outline=EDGE,
                error=DANGER),
            font_family="Segoe UI")

        self.nav = ft.NavigationRail(
            selected_index=0, extended=True, min_width=140,
            bgcolor=CARD, indicator_color="#1e3048",
            destinations=[ft.NavigationRailDestination(
                icon=ft.Icons.CIRCLE_OUTLINED, selected_icon=ft.Icons.CIRCLE,
                label=name) for name in PAGES],
            on_change=self._nav)
        self.status_text = ft.Text("Stopped · local input active", size=14,
                                   weight=ft.FontWeight.W_600, color=INK)
        self.stats_text = ft.Text("No session measurements yet", size=12, color=MUTED)
        self.dot = ft.Icon(ft.Icons.CIRCLE, size=14, color=MUTED)
        self.overview_status = ft.Text("Stopped · local input active", size=14)
        self.body = ft.Container(expand=True)
        self.log_view = ft.TextField(read_only=True, multiline=True, expand=True,
                                     text_size=12, border_color=EDGE, bgcolor="#0a101b",
                                     text_style=ft.TextStyle(font_family="Cascadia Mono"))
        self._build_pages()
        self._poll_task = page.run_task(self._poll_loop)

        header = ft.Row([
            ft.Text("PrecisionKVM", size=24, weight=ft.FontWeight.W_700),
            ft.Container(expand=True),
            self.dot])
        sub = ft.Text("Low-latency input and clipboard sharing · Windows ⇄ macOS",
                      size=12, color=MUTED)
        other = "qt" if self.settings.get("ui") == "flet" else "flet"
        buttons = ft.Row([
            ft.FilledButton("Start sharing", icon=ft.Icons.PLAY_ARROW,
                            on_click=lambda e: self.start()),
            ft.OutlinedButton("Stop", icon=ft.Icons.STOP,
                              style=ft.ButtonStyle(color=DANGER,
                                                   side=ft.BorderSide(1, "#5a3245")),
                              on_click=lambda e: self.stop()),
            ft.Container(expand=True),
            ft.OutlinedButton(f"Try {ui_common.FRAMEWORKS[other]} UI",
                              on_click=lambda e: self._switch(other)),
            ft.OutlinedButton("Save settings", on_click=lambda e: self.save()),
        ])
        self.start_button = buttons.controls[0]
        right = ft.Container(
            expand=True, padding=ft.padding.only(top=20, right=22, bottom=16, left=22),
            content=ft.Column([
                header, sub,
                self.body,
                buttons, self.status_text, self.stats_text]))
        page.add(ft.Row([self.nav, right], expand=True, spacing=0))
        try:
            page.window.prevent_close = True
            page.window.on_event = self._window_event
        except Exception:
            pass
        self._init_tray()
        if autostart:
            page.run_task(self._delayed_start)

    async def _delayed_start(self):
        await asyncio.sleep(0.6)
        self.start()

    # ----- shared widgets ---------------------------------------------------

    def _card(self, title, *controls):
        ft = self.ft
        return ft.Container(
            bgcolor=CARD, border_radius=12, padding=18,
            border=ft.Border.all(1, EDGE),
            content=ft.Column([
                ft.Text(title.upper(), size=11, weight=ft.FontWeight.W_600,
                        color=MUTED), *controls], spacing=10))

    def _field(self, label, key, options=None, numeric=False, hint=None):
        ft = self.ft
        value = str(self.settings.get(key, ""))
        if options:
            control = ft.Dropdown(value=value if value in options else options[0],
                                  options=[ft.dropdown.Option(o) for o in options],
                                  border_color=EDGE, bgcolor="#0f1826",
                                  dense=True, expand=True)
            self._fields[key] = ("combo", control)
        elif numeric:
            control = ft.TextField(value=value, dense=True, border_color=EDGE,
                                   bgcolor="#0f1826", expand=True,
                                   keyboard_type=ft.KeyboardType.NUMBER)
            self._fields[key] = ("num", control)
        else:
            control = ft.TextField(value=value, dense=True, border_color=EDGE,
                                   bgcolor="#0f1826", expand=True, hint_text=hint)
            self._fields[key] = ("text", control)
        return ft.Row([ft.Text(label, size=13, expand=1), control])

    def _check(self, label, key):
        control = self.ft.Switch(label=label, value=bool(self.settings.get(key, False)),
                                 active_color=ACCENT, label_position=self.ft.LabelPosition.LEFT,
                                 label_style=self.ft.TextStyle(size=13))
        self._fields[key] = ("check", control)
        return control

    # ----- pages --------------------------------------------------------------

    def _build_pages(self):
        ft = self.ft
        overview = ft.Column([
            self._card("Status", self.overview_status),
            self._card("How it works", ft.Text(
                "Arrange your monitors in the Layout page — the Mac display can sit "
                "along any edge or corner of any screen. Crossing happens wherever "
                "the displays touch. Adjust ports, encryption and discovery under "
                "Network.", size=13, color=MUTED))], scroll=ft.ScrollMode.AUTO,
            expand=True, spacing=12)

        # --- layout canvas ---
        self.canvas_stack = ft.Stack(expand=True)
        self._render_layout()
        canvas_box = ft.Container(self.canvas_stack, bgcolor="#0a101b",
                                  border=ft.Border.all(1, EDGE), border_radius=12,
                                  height=400, expand=True)
        layout_page = ft.Column([
            canvas_box,
            ft.Text("Drag MAC anywhere — it snaps flush to any monitor edge or "
                    "corner. The glowing segment is where the cursor crosses.",
                    size=12, color=MUTED)], expand=True, spacing=10)

        sharing = ft.Column([
            self._card("Mouse",
                       self._field("Mouse update limit (Hz)", "mouse_hz",
                                   ["120", "144", "240", "500", "1000"]),
                       self._field("Pointer mode", "mouse_mode", ["raw", "system"]),
                       self._field("Pointer sensitivity", "gain", numeric=True)),
            self._card("Scrolling",
                       self._field("Vertical sensitivity", "scroll_gain_y", numeric=True),
                       self._field("Horizontal sensitivity", "scroll_gain_x", numeric=True),
                       self._field("Scroll units", "scroll_mode", ["pixel", "line"]),
                       self._check("Reverse vertical", "scroll_invert_y"),
                       self._check("Reverse horizontal", "scroll_invert_x")),
            self._card("Safety",
                       self._field("Hold Esc to stop (seconds)", "emergency_escape_seconds",
                                   numeric=True),
                       self._check("Bridge Windows desktop gestures to Mac", "gesture_bridge"),
                       self._check("Native four-finger swipes (experimental)", "native_touchpad"))],
            scroll=ft.ScrollMode.AUTO, expand=True, spacing=12)

        self.key_list = ft.Column(spacing=4, scroll=ft.ScrollMode.AUTO, height=140)
        self._refresh_keymap()
        self.source_key = ft.Dropdown(options=[ft.dropdown.Option(k) for k in KEY_NAMES],
                                      value=KEY_NAMES[0], dense=True, expand=True,
                                      border_color=EDGE)
        self.target_key = ft.Dropdown(options=[ft.dropdown.Option(k) for k in MAC_KEY_NAMES],
                                      value=MAC_KEY_NAMES[0], dense=True, expand=True,
                                      border_color=EDGE)
        keys = ft.Column([
            self._card("Mac behaviour",
                       self._check("Mac-style shortcuts on this PC — Alt acts as Command "
                                   "(Alt+C copy, Alt+Space search, Alt+Q quit; "
                                   "Alt+Tab unchanged)", "mac_mode"),
                       self._field("Keyboard preset on the Mac", "keymap_preset",
                                   ["option_cmd", "native", "ctrl_cmd", "mac_layout"]),
                       self._check("Home / End move to line start / end", "windows_home_end"),
                       self._check("Ctrl + arrows navigate words", "windows_word_nav")),
            self._card("Custom remapping",
                       self.key_list,
                       ft.Row([self.source_key, ft.Text("→"), self.target_key,
                               ft.FilledButton("Add / replace",
                                               on_click=lambda e: self._add_mapping()),
                               ft.OutlinedButton("Remove",
                                                 on_click=lambda e: self._remove_mapping())]))],
            scroll=ft.ScrollMode.AUTO, expand=True, spacing=12)

        network = ft.Column([
            self._card("Connection",
                       self._field("Mac address", "peer"),
                       self._field("Port", "port", numeric=True),
                       self._field("Mac SSH target (user@host)", "ssh_target"),
                       self._check("Encrypt connections (TLS) — recommended", "tls"),
                       self._check("Advertise this PC on the network (mDNS)", "discovery")),
            self._card("Clipboard",
                       self._check("Share clipboard text and images both ways",
                                   "clipboard_enabled"),
                       self._field("Maximum size (MiB)", "clipboard_max_mb", numeric=True),
                       ft.FilledButton("Export pairing file for Mac…",
                                       icon=ft.Icons.IOS_SHARE,
                                       on_click=lambda e: self._export_pairing()))],
            scroll=ft.ScrollMode.AUTO, expand=True, spacing=12)

        logs = ft.Column([self.log_view,
                          ft.Row([ft.OutlinedButton("Reload log file",
                                                    on_click=lambda e: self._reload_logs()),
                                  ft.OutlinedButton("Open log folder",
                                                    on_click=lambda e: self._open_logs())])],
                         expand=True, spacing=10)

        self._pages_list = [overview, layout_page, sharing, keys, network, logs]
        self.body.content = self._pages_list[0]

    def _nav(self, event):
        self.body.content = self._pages_list[int(event.control.selected_index)]
        self.page.update()

    # ----- layout canvas --------------------------------------------------------

    def _render_layout(self):
        ft = self.ft
        w, h = self._canvas_w, self._canvas_h
        scale, ox, oy = ui_common.layout_transform(self.mons, self.peers, w, h)
        self._layout_scale, self._layout_origin = scale, (ox, oy)
        controls = []
        for rect in self.mons:
            controls.append(ft.Container(
                left=rect[0] * scale + ox, top=rect[1] * scale + oy,
                width=rect[2] * scale, height=rect[3] * scale,
                bgcolor=PC_BLUE, border=ft.Border.all(1.5, "#4d6a8c"),
                border_radius=6,
                content=ft.Text("THIS PC", size=10, weight=ft.FontWeight.W_700),
                alignment=ft.alignment.center))
        for name, rect in self.peers.items():
            box = ft.Container(
                left=rect[0] * scale + ox, top=rect[1] * scale + oy,
                width=rect[2] * scale, height=rect[3] * scale,
                bgcolor=MAC_GREEN, border=ft.Border.all(1.5, ACCENT),
                border_radius=6,
                content=ft.Text(name.upper(), size=10, weight=ft.FontWeight.W_700),
                alignment=ft.alignment.center)
            detector = ft.GestureDetector(
                content=box, drag_interval=10,
                on_pan_start=lambda e, n=name: self._pan_start(n),
                on_pan_update=lambda e, n=name: self._pan(n, e.delta_x, e.delta_y),
                on_pan_end=lambda e, n=name: self._pan_end(n))
            controls.append(detector)
        controls.extend(self._overlap_marks(scale, ox, oy))
        self.canvas_stack.controls = controls

    def _overlap_marks(self, scale, ox, oy):
        marks = []
        for rect in self.peers.values():
            for m in self.mons:
                mx, my, mw, mh = m
                lo_x, hi_x = max(rect[0], mx), min(rect[0] + rect[2], mx + mw)
                if hi_x > lo_x:
                    for edge_y in (my, my + mh):
                        if (abs(rect[1] + rect[3] - edge_y) <= geometry.EDGE_GAP
                                or abs(rect[1] - edge_y) <= geometry.EDGE_GAP):
                            marks.append(self.ft.Container(
                                left=lo_x * scale + ox, top=edge_y * scale + oy - 1.5,
                                width=(hi_x - lo_x) * scale, height=3, bgcolor=ACCENT))
                lo_y, hi_y = max(rect[1], my), min(rect[1] + rect[3], my + mh)
                if hi_y > lo_y:
                    for edge_x in (mx, mx + mw):
                        if (abs(rect[0] + rect[2] - edge_x) <= geometry.EDGE_GAP
                                or abs(rect[0] - edge_x) <= geometry.EDGE_GAP):
                            marks.append(self.ft.Container(
                                left=edge_x * scale + ox - 1.5, top=lo_y * scale + oy,
                                width=3, height=(hi_y - lo_y) * scale, bgcolor=ACCENT))
        return marks

    def _pan_start(self, name):
        self._dragging = name

    def _pan(self, name, dx, dy):
        rect = self.peers[name]
        scale = self._layout_scale
        self.peers[name] = (rect[0] + dx / scale, rect[1] + dy / scale,
                            rect[2], rect[3])
        self._render_layout()
        self.page.update()

    def _pan_end(self, name):
        rect = self.peers[name]
        snapped = geometry.snap(
            (round(rect[0]), round(rect[1]), rect[2], rect[3]),
            self.mons, threshold=max(8, round(16 / self._layout_scale)))
        self.peers[name] = snapped
        self._render_layout()
        self.page.update()
        self._save_peers()

    def _save_peers(self):
        self.settings = ui_common.save_peers(dict(self.settings), self.peers)
        if self.session and self.session.capture:
            self.session.capture.peers = dict(self.peers)

    # ----- keymap / settings ------------------------------------------------------

    def _refresh_keymap(self):
        self.key_list.controls = [
            self.ft.Text(f"{s}  →  {t}", size=12, color=MUTED)
            for s, t in sorted(self.keymap.items())]

    def _add_mapping(self):
        try:
            self.keymap.update(normalize_keymap(
                {self.source_key.value: self.target_key.value}))
            self._refresh_keymap()
            self.page.update()
        except ValueError as exc:
            self._set_status(str(exc), "bad")

    def _remove_mapping(self):
        if self.keymap:
            self.keymap.pop(next(reversed(self.keymap)), None)
            self._refresh_keymap()
            self.page.update()

    def collect_settings(self):
        values = {}
        for key, (kind, control) in self._fields.items():
            if kind == "check":
                values[key] = control.value
            elif kind == "combo":
                values[key] = int(control.value) if str(control.value).isdigit() else control.value
            else:
                values[key] = control.value
        settings = dict(self.settings, keymap=dict(self.keymap), **values)
        return ui_common.collect(self.settings, settings)

    def save(self):
        try:
            self.settings = self.collect_settings()
            ui_common.persist(self.settings)
            self._set_status("Settings saved · start the host to apply", "idle")
        except Exception as exc:
            self._set_status(str(exc), "bad")

    def _export_pairing(self):
        try:
            settings = self.collect_settings()
            from pathlib import Path
            destination = state_dir() / "PrecisionKVM-pairing.json"
            ui_common.export_pairing(settings, destination)
            self._set_status(f"Pairing exported · {destination}", "good")
        except Exception as exc:
            self._set_status(f"Could not export pairing: {exc}", "bad")

    def _switch(self, framework):
        running = self.session is not None
        if self.session:
            self.session.stop()
            self.session = None
        ui_common.switch_ui(framework, start=running)

    # ----- session ------------------------------------------------------------------

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
            self.start_button.text = "Apply & reconnect"
            self.page.update()
        except Exception as exc:
            self._set_status(str(exc), "bad")

    def stop(self):
        if self.session:
            self.session.stop()
            self.session = None
        while not self.messages.empty():
            self.messages.get_nowait()
        self.start_button.text = "Start sharing"
        self._set_status("Stopped · local input active", "idle")

    def report_status(self, message):
        logging.info(message)
        try:
            self.messages.put_nowait(str(message))
        except queue.Full:
            pass

    def _set_status(self, message, tone):
        colors = {"good": ACCENT, "bad": DANGER, "idle": INK}
        self.status_text.value = message
        self.status_text.color = colors.get(tone, INK)
        self.overview_status.value = message
        self.dot.color = ACCENT if tone == "good" else DANGER if tone == "bad" else MUTED
        try:
            self.page.update()
        except Exception:
            pass

    def _reload_logs(self):
        try:
            lines = (state_dir() / "host.log").read_text(
                encoding="utf-8", errors="replace").splitlines()[-400:]
        except OSError:
            lines = []
        self.log_view.value = "\n".join(lines) or "No log entries yet."
        self.page.update()

    def _open_logs(self):
        import subprocess
        subprocess.Popen(["explorer", str(state_dir())])

    async def _poll_loop(self):
        while True:
            await asyncio.sleep(0.15)
            changed = False
            while not self.messages.empty():
                message = self.messages.get_nowait()
                self.log_view.value = (self.log_view.value or "") + message + "\n"
                self._set_status(message, ui_common.status_tone(message))
                changed = True
            if self.session:
                stats = ui_common.stats_line(self.session)
                if stats != self.stats_text.value:
                    self.stats_text.value = stats
                    changed = True
                if self.session.thread and not self.session.thread.is_alive():
                    self.start_button.text = "Start sharing"
                    changed = True
            if changed:
                try:
                    self.page.update()
                except Exception:
                    return

    # ----- tray / close ---------------------------------------------------------

    def _init_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            return
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((6, 6, 58, 58), radius=14, fill=MAC_GREEN)
        draw.rectangle((16, 20, 32, 32), fill=PC_BLUE)
        menu = pystray.Menu(
            pystray.MenuItem("Open PrecisionKVM", lambda *_: self._restore(), default=True),
            pystray.MenuItem("Quit", lambda *_: self._quit()))
        self._tray = pystray.Icon("precisionkvm", image,
                                  "PrecisionKVM — input sharing", menu)
        threading.Thread(target=self._tray.run, daemon=True, name="kvm-tray").start()

    def _window_event(self, event):
        kind = str(getattr(event, "type", getattr(event, "data", event))).lower()
        if "close" in kind:
            try:
                self.page.window.visible = False
                self.page.update()
            except Exception:
                self.page.window.minimized = True
                self.page.update()

    def _restore(self):
        self.page.window.visible = True
        self.page.update()

    def _quit(self):
        self._closing = True
        self.stop()
        if self._tray:
            self._tray.stop()
        self.page.window.destroy()
