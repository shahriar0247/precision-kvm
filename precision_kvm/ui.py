"""Tabbed native settings; native input and networking stay off the Tk thread."""
from __future__ import annotations

import collections
import logging
import queue
import re
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:  # Tray support is optional; windowed mode still works.
    pystray = None

from .config import identity, load_settings, local_ip, save_json, state_dir, validate_settings
from . import geometry, ui_common
from .keymap import KEY_NAMES, MAC_KEY_NAMES, normalize_keymap
from .session import HostSession

def _overlap(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def _peer_side(rect, mons):
    """Which union edge a peer rectangle is flush to (legacy `side` setting)."""
    px, py, pw, ph = rect
    bx, by, bw, bh = geometry.union_bounds(mons)
    cx, cy = px + pw / 2, py + ph / 2
    return min(((abs(cx - bx), "left"), (abs(cx - bx - bw), "right"),
                (abs(cy - by), "top"), (abs(cy - by - bh), "bottom")))[1]


DISPLAY_OPTIONS = {
    "mouse_mode": {"raw": "Raw relative · recommended", "system": "Windows pointer acceleration"},
    "scroll_mode": {"pixel": "Smooth pixels", "line": "Traditional lines"},
    "keymap_preset": {"option_cmd": "Mac layout · Alt = Command, Win = Option", "native": "Windows modifiers",
                      "ctrl_cmd": "Swap Ctrl and Command", "mac_layout": "Mac layout · Control unchanged"},
}


class ControlPanel:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = load_settings()
        self.session: HostSession | None = None
        self.messages: queue.Queue[str] = queue.Queue(maxsize=128)
        self.controls: list[ttk.Widget] = []
        self.variables: dict[str, tk.Variable] = {}
        self._host_widgets: list[tk.Widget] = []
        self.client = ui_common.ClientRunner(self.report_status)
        self.keymap = dict(self.settings["keymap"])
        root.title("PrecisionKVM — Input sharing")
        root.geometry("960x900")
        root.minsize(900, 820)
        bg, card, edge = "#0d1420", "#141d2c", "#24344a"
        ink, muted, accent = "#e6edf7", "#8b98ad", "#3fd3bd"
        root.configure(bg=bg)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", background=bg, foreground=ink, font=("Segoe UI", 10))
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=ink, font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 22), foreground="white")
        style.configure("Muted.TLabel", foreground=muted, font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 10), padding=(14, 8),
                        background=card, bordercolor=edge, lightcolor=card, darkcolor=card)
        style.map("TButton", background=[("active", "#1d2a3e"), ("pressed", "#233349")])
        style.configure("TCheckbutton", background=bg, foreground=ink)
        style.map("TCheckbutton", background=[("active", bg)])
        style.configure("TRadiobutton", background=bg, foreground=ink)
        style.map("TRadiobutton", background=[("active", bg)])
        style.configure("TLabelframe", background=bg, bordercolor=edge)
        style.configure("TLabelframe.Label", background=bg, foreground=muted)
        style.configure("TNotebook", background=bg, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 10), background=card,
                        foreground=muted, font=("Segoe UI", 10))
        style.map("TNotebook.Tab", background=[("selected", "#1e3048")],
                  foreground=[("selected", "white")])
        style.configure("TEntry", fieldbackground=card, foreground=ink,
                        bordercolor=edge, lightcolor=card, darkcolor=card, padding=8)
        style.configure("TSpinbox", fieldbackground=card, foreground=ink,
                        bordercolor=edge, lightcolor=card, darkcolor=card, padding=6)
        style.configure("TCombobox", fieldbackground=card, foreground=ink,
                        bordercolor=edge, lightcolor=card, darkcolor=card, padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", card)],
                  foreground=[("readonly", ink)])
        style.configure("Treeview", background=card, fieldbackground=card,
                        foreground=ink, bordercolor=edge, rowheight=26)
        style.map("Treeview", background=[("selected", "#23475e")])
        style.configure("Treeview.Heading", background="#1a2536", foreground=muted,
                        font=("Segoe UI", 9, "bold"))
        style.configure("Accent.TButton", background=accent, foreground="#08201c",
                        font=("Segoe UI", 10, "bold"), bordercolor=accent)
        style.map("Accent.TButton", background=[("active", "#5ce0ca"), ("pressed", "#35bba6")])
        style.configure("Stop.TButton", background="#3a2230", foreground="#ffb5bc",
                        bordercolor="#5a3245")
        style.map("Stop.TButton", background=[("active", "#4a2a3c")])
        style.configure("Status.TLabel", foreground=accent, font=("Segoe UI", 10, "bold"))
        root.option_add("*TCombobox*Listbox.background", card)
        root.option_add("*TCombobox*Listbox.foreground", ink)
        root.option_add("*TCombobox*Listbox.selectBackground", "#23475e")
        panel = ttk.Frame(root, padding=(28, 24))
        panel.pack(fill="both", expand=True)
        header = ttk.Frame(panel)
        header.pack(fill="x")
        ttk.Label(header, text="PrecisionKVM", style="Title.TLabel").pack(side="left")
        self.dot = tk.Canvas(header, width=14, height=14, bg=bg, highlightthickness=0)
        self.dot.pack(side="right", pady=8)
        self._dot_item = self.dot.create_oval(3, 3, 11, 11, fill=muted, outline="")
        ttk.Label(panel, text="Low-latency input and clipboard sharing · Windows ⇄ macOS",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 16))
        role_row = ttk.Frame(panel)
        role_row.pack(fill="x", pady=(0, 12))
        ttk.Label(role_row, text="This device acts as:").pack(side="left")
        self.variables["role"] = tk.StringVar(value=self.settings.get("role", "host"))
        for value, label in (("host", "Host — captures this machine's input"),
                             ("client", "Client — receives a host's input")):
            ttk.Radiobutton(role_row, text=label, variable=self.variables["role"],
                            value=value, command=self._role_changed).pack(side="left", padx=(14, 0))
        notebook = ttk.Notebook(panel)
        notebook.pack(fill="both", expand=True)
        connection, inputs, keys, sharing, tweaks, logs = (ttk.Frame(notebook, padding=18) for _ in range(6))
        for frame, title in ((connection, "Connection"), (inputs, "Mouse & gestures"),
                             (keys, "Key remapping"), (tweaks, "Mac Tweaks"),
                             (sharing, "Clipboard & safety"), (logs, "Activity log")):
            notebook.add(frame, text=title)
            frame.columnconfigure(1, weight=1)
        logs.rowconfigure(0, weight=1)
        logs.columnconfigure(0, weight=1)
        self.log_text = tk.Text(logs, wrap="none", height=16, bg=card, fg=ink,
                                insertbackground=ink, relief="flat",
                                font=("Cascadia Mono", 9), state="disabled")
        scrollbar = ttk.Scrollbar(logs, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        log_buttons = ttk.Frame(logs)
        log_buttons.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Button(log_buttons, text="Refresh", command=self.refresh_logs).pack(side="left")
        ttk.Button(log_buttons, text="Open log folder",
                   command=lambda: subprocess.Popen(["explorer", str(state_dir())])).pack(side="left", padx=8)
        self.note(logs, 2, "Host events appear live while sharing; Refresh reloads the full log file.")
        self.canvas = tk.Canvas(connection, height=230, bg=card, highlightthickness=0)
        self.canvas.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self._drag: tuple[str, int, int] | None = None
        self._selected: str | None = None
        self._rename_win: int | None = None
        self._scale = (1.0, 0.0, 0.0)
        self._init_peers()
        self.canvas.bind("<ButtonPress-1>", self._press_peer)
        self.canvas.bind("<B1-Motion>", self._drag_peer)
        self.canvas.bind("<ButtonRelease-1>", self._drop_peer)
        self.canvas.bind("<Double-Button-1>", self._rename_peer)
        self.canvas.bind("<Button-3>", self._remove_at)
        tools = ttk.Frame(connection)
        tools.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))
        add_btn = ttk.Button(tools, text="+ Add client", command=self._add_peer)
        add_btn.pack(side="left")
        rm_btn = ttk.Button(tools, text="− Remove selected", command=self._remove_peer)
        rm_btn.pack(side="left", padx=8)
        self.controls.extend((add_btn, rm_btn))
        self._host_widgets.extend((tools,))
        self.field(connection, 2, "Mac address", "peer", host_only=True)
        self.field(connection, 3, "Mac SSH target", "ssh_target", host_only=True)
        self.field(connection, 4, "Mouse update limit (Hz)", "mouse_hz",
                   ("120", "144", "240", "500", "1000"), host_only=True)
        self.field(connection, 5, "Host port", "port", bounds=(1024, 65533, 1), host_only=True)
        self.check(connection, 6, "Encrypted connection (TLS, recommended)", "tls", host_only=True)
        self.check(connection, 7, "Advertise on the local network (mDNS discovery)", "discovery",
                   host_only=True)
        pairing = ttk.Button(connection, text="Export pairing file for the client…", command=self.export_pairing)
        pairing.grid(row=8, column=0, columnspan=2, sticky="w", pady=16)
        self.controls.append(pairing)
        self._host_widgets.append(pairing)
        note = ttk.Label(connection, style="Muted.TLabel", wraplength=780, justify="left",
            text="Drag each client box anywhere around your displays — it snaps flush to the "
                 "nearest edge, corners included. Double-click a name to rename it; right-click "
                 "a box (or select it and click Remove) to delete. Each connected client maps "
                 "to the box it controls.\n"
                 "Import the pairing file in the client app to connect securely.\n"
                 "Edit settings anytime, then click Apply & reconnect.")
        note.grid(row=9, column=0, columnspan=2, sticky="w", pady=(12, 8))
        self._host_widgets.append(note)
        self._host_widgets.append(self.canvas)
        self.client_frame = ttk.LabelFrame(connection, text="Client pairing", padding=12)
        self.client_frame.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self.client_frame.columnconfigure(1, weight=1)
        self.pair_status = tk.StringVar()
        ttk.Label(self.client_frame, textvariable=self.pair_status,
                  style="Muted.TLabel", wraplength=720, justify="left"
                  ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Button(self.client_frame, text="Choose pairing file…",
                   command=self.import_pairing_file).grid(row=1, column=0, sticky="w", pady=8)
        self.pair_text = tk.Text(self.client_frame, height=4, bg=card, fg=ink,
                                 insertbackground=ink, relief="flat", font=("Cascadia Mono", 9))
        self.pair_text.grid(row=2, column=0, columnspan=2, sticky="ew")
        ttk.Button(self.client_frame, text="Import pasted JSON",
                   command=self.import_pairing_text).grid(row=3, column=0, sticky="w", pady=8)
        self.client_frame.grid_remove()
        self.field(inputs, 0, "Relative movement source", "mouse_mode", ("raw", "system"))
        self.field(inputs, 1, "Pointer sensitivity", "gain", bounds=(0.1, 5, 0.1))
        self.field(inputs, 2, "Vertical scroll sensitivity", "scroll_gain_y", bounds=(0.1, 5, 0.1))
        self.field(inputs, 3, "Horizontal scroll sensitivity", "scroll_gain_x", bounds=(0.1, 5, 0.1))
        self.field(inputs, 4, "Scroll units", "scroll_mode", ("pixel", "line"))
        self.check(inputs, 5, "Reverse vertical scrolling", "scroll_invert_y")
        self.check(inputs, 6, "Reverse horizontal scrolling", "scroll_invert_x")
        self.check(inputs, 7, "Bridge Windows desktop gesture shortcuts to Mac", "gesture_bridge")
        self.check(inputs, 8, "Enable native four-finger swipe recognition (experimental)", "native_touchpad")
        self.note(inputs, 9, "Raw: hardware-relative motion with linear sensitivity and no added acceleration.\n"
                  "System: Windows-accelerated relative motion.\n\n"
                  "Ctrl+Win+Left/Right → Mac desktops; Win+Tab → Mission Control;\n"
                  "Win+D → Show Desktop. Four-finger swipes work if Windows emits these\n"
                  "shortcuts. Raw finger contacts are not forwarded. Other injected keyboard\n"
                  "events are ignored while the gesture bridge is enabled and sharing is active.\n"
                  "Native swipes: four fingers left/right switch Mac desktops; up opens Mission\n"
                  "Control; down shows Desktop. Windows may also act on these swipes.\n"
                  "Set Windows four-finger gestures to Nothing before enabling native mode.")
        self.option_command = tk.BooleanVar(value=self.settings["keymap_preset"] in ("option_cmd", "mac_layout"))
        modifier_toggle = ttk.Checkbutton(keys, text="Mac keyboard layout  ·  Alt → ⌘ Command, Win → ⌥ Option",
            variable=self.option_command, command=self.toggle_modifier_layout)
        modifier_toggle.grid(row=0, column=0, columnspan=2, sticky="w", pady=(6, 16))
        self.controls.append(modifier_toggle)
        self.field(keys, 1, "Advanced keyboard preset", "keymap_preset", ("option_cmd", "native", "ctrl_cmd", "mac_layout"))
        self.variables["keymap_preset"].trace_add("write", self.sync_modifier_toggle)
        self.note(keys, 2, "The Mac layout is on by default; Ctrl remains Control.\n"
                  "Custom rows below override the preset. Physical Esc always retains its emergency stop.")
        self.key_table = ttk.Treeview(keys, columns=("source", "target"), show="headings", height=8)
        self.key_table.heading("source", text="PC physical key")
        self.key_table.heading("target", text="Mac destination")
        self.key_table.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=12)
        keys.rowconfigure(3, weight=1)
        editor = ttk.Frame(keys)
        editor.grid(row=4, column=0, columnspan=2, sticky="ew")
        self.source_key = tk.StringVar(value="CapsLock")
        self.target_key = tk.StringVar(value="Escape")
        source = ttk.Combobox(editor, textvariable=self.source_key, values=KEY_NAMES, width=20)
        target = ttk.Combobox(editor, textvariable=self.target_key, values=MAC_KEY_NAMES, width=20)
        source.pack(side="left", padx=(0, 8))
        ttk.Label(editor, text="→").pack(side="left", padx=4)
        target.pack(side="left", padx=8)
        add = ttk.Button(editor, text="Add / replace", command=self.add_mapping)
        remove = ttk.Button(editor, text="Remove selected", command=self.remove_mapping)
        add.pack(side="left", padx=4)
        remove.pack(side="left", padx=4)
        self.controls.extend((source, target, add, remove))
        self.note(keys, 5, "Choose named keys, media keys or Disabled. Advanced sources accept Windows\n"
                  "virtual keys (0x41) or physical scan identifiers (scan:0x1e:0).")
        self.refresh_keymap()
        self.check(tweaks, 0, "Smooth scrolling on the Mac", "smooth_scroll")
        self.field(tweaks, 1, "Scroll smoothing strength (ms)", "smooth_scroll_ms", bounds=(10, 150, 5))
        self.field(tweaks, 2, "Scroll animation rate (Hz)", "smooth_scroll_hz", ("60", "120", "144", "240"))
        self.check(tweaks, 3, "Windows-style Home / End  ·  start / end of line", "windows_home_end")
        self.check(tweaks, 4, "Windows-style Ctrl + arrows / Backspace  ·  move / delete words", "windows_word_nav")
        self.note(tweaks, 5, "Scroll smoothing affects wheel motion only; mouse movement and keyboard\n"
                  "events stay on their immediate path. Lower strength feels more direct.\n\n"
                  "Mac app → Mac Tweaks also offers smoothing for a mouse plugged directly\n"
                  "into the Mac. Its built-in trackpad scrolling is left alone. The optional local\n"
                  "mouse setting needs macOS input permission; it never changes system settings.\n\n"
                  "The Mac can keep its own explicit tweaks, overriding the PC defaults.")
        self.check(sharing, 0, "Share clipboard text and images in both directions", "clipboard_enabled")
        self.field(sharing, 1, "Maximum clipboard size (MiB)", "clipboard_max_mb", bounds=(1, 512, 1))
        self.note(sharing, 2, "New copies sync over a separate encrypted connection, keeping large copies\n"
                  "out of the input queue. Connecting does not overwrite existing clipboard data.\n"
                  "Supports text and PNG images; files and rich HTML are not included.")
        self.check(sharing, 3, "Mac-style shortcuts on this PC — Alt sits where Command would: "
                  "Alt+C copy, Alt+Space search, Alt+Q quit, Alt+arrows Home/End, "
                  "Alt+Shift+3/4 screenshot; Alt+Tab stays unchanged", "mac_mode")
        self.check(sharing, 4, "Lock the Mac when this PC locks "
                  "(input is never shared on the lock screen)", "lock_sync")
        self.field(sharing, 5, "Hold physical Esc to STOP (seconds)", "emergency_escape_seconds", bounds=(0.5, 10, 0.1))
        self.note(sharing, 6, "Hold Esc for the chosen time to STOP the PC server and clipboard\n"
                  "sharing, restore local input and prevent edge re-entry until you start\n"
                  "the host again. Ctrl+Alt+F12 returns locally without stopping.\n\n"
                  "Mac-style shortcuts apply while input is local; over the KVM Alt already\n"
                  "maps to Command. In terminals Alt+C behaves exactly like Ctrl+C.")
        buttons = ttk.Frame(panel)
        buttons.pack(fill="x", pady=(18, 10))
        self.start_button = ttk.Button(buttons, text="Start sharing", style="Accent.TButton", command=self.start)
        self.start_button.pack(side="left")
        ttk.Button(buttons, text="Stop sharing", style="Stop.TButton", command=self.stop).pack(side="left", padx=8)
        save = ttk.Button(buttons, text="Save settings", command=self.save)
        save.pack(side="right")
        self.controls.append(save)
        self.status = tk.StringVar(value="Stopped • local input active")
        self.stats = tk.StringVar(value="No session measurements yet")
        ttk.Label(panel, textvariable=self.status, wraplength=850).pack(anchor="w")
        ttk.Label(panel, textvariable=self.stats, style="Muted.TLabel", wraplength=850).pack(anchor="w", pady=5)
        self._rtt: collections.deque[float] = collections.deque(maxlen=240)
        self.latency = tk.Canvas(panel, height=46, bg=card, highlightthickness=0)
        self.latency.pack(fill="x", pady=(6, 0))
        self.latency.bind("<Configure>", lambda _e: self._draw_latency())
        self.canvas.bind("<Configure>", lambda event: self.draw_layout())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._role_changed()
        root.after(100, self.poll)

    def field(self, parent, row, label, key, values=None, bounds=None, host_only=False):
        options = DISPLAY_OPTIONS.get(key, {})
        variable = tk.StringVar(value=options.get(str(self.settings[key]), str(self.settings[key])))
        self.variables[key] = variable
        caption = ttk.Label(parent, text=label)
        caption.grid(row=row, column=0, sticky="w", pady=7)
        control: ttk.Widget
        if values:
            control = ttk.Combobox(parent, textvariable=variable,
                                  values=[options.get(str(value), str(value)) for value in values], state="readonly")
        elif bounds:
            control = ttk.Spinbox(parent, textvariable=variable, from_=bounds[0], to=bounds[1], increment=bounds[2])
        else:
            control = ttk.Entry(parent, textvariable=variable)
        control.grid(row=row, column=1, sticky="ew", padx=(20, 0), pady=7)
        self.controls.append(control)
        if host_only:
            self._host_widgets.extend((caption, control))

    def check(self, parent, row, label, key, host_only=False):
        variable = tk.BooleanVar(value=self.settings[key])
        self.variables[key] = variable
        control = ttk.Checkbutton(parent, text=label, variable=variable)
        control.grid(row=row, column=0, columnspan=2, sticky="w", pady=8)
        self.controls.append(control)
        if host_only:
            self._host_widgets.append(control)

    @staticmethod
    def note(parent, row, text):
        ttk.Label(parent, text=text, style="Muted.TLabel", wraplength=780,
                  justify="left").grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 8))

    def refresh_keymap(self):
        for item in self.key_table.get_children():
            self.key_table.delete(item)
        for source, target in sorted(self.keymap.items()):
            self.key_table.insert("", "end", values=(source, target))

    def add_mapping(self):
        try:
            self.keymap.update(normalize_keymap({self.source_key.get(): self.target_key.get()}))
            self.refresh_keymap()
        except ValueError as exc:
            self.status.set(str(exc))

    def remove_mapping(self):
        for item in self.key_table.selection():
            self.keymap.pop(self.key_table.item(item, "values")[0], None)
        self.refresh_keymap()

    def collect_settings(self):
        settings = dict(self.settings, keymap=dict(self.keymap), swap_ctrl_cmd=False)
        settings.update({name: value.get() for name, value in self.variables.items()})
        for name, options in DISPLAY_OPTIONS.items():
            settings[name] = {label: value for value, label in options.items()}.get(settings[name], settings[name])
        mons = geometry.monitors() or [(0, 0, 1920, 1080)]
        settings["peers"] = {name: dict(zip("xywh", (int(v) for v in r)))
                             for name, r in self._peers.items()}
        if self._peers:
            first = next(iter(self._peers.values()))
            settings["side"] = _peer_side(first, mons)
        return validate_settings(settings)

    def _role_changed(self):
        client = self.variables["role"].get() == "client"
        for widget in self._host_widgets:
            widget.grid_remove() if client else widget.grid()
        if client:
            self.client_frame.grid()
            self._refresh_pair_status()
        else:
            self.client_frame.grid_remove()

    def _refresh_pair_status(self):
        config = ui_common.load_client()
        if config.get("host"):
            self.pair_status.set(f"Paired with {config['host']}:{config.get('port', 24819)}"
                                 " — press Start to connect.")
        else:
            self.pair_status.set("Export a pairing file on the host machine, then choose it"
                                 " here or paste its JSON contents.")

    def import_pairing_file(self):
        destination = filedialog.askopenfilename(parent=self.root,
            title="Import pairing file", filetypes=[("Pairing file", "*.json")])
        if not destination:
            return
        try:
            config = ui_common.import_pairing(Path(destination).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.pair_status.set(f"Import failed: {exc}")
            return
        self._refresh_pair_status()
        self.status.set(f"Pairing imported — connects to {config['host']} on Start")

    def import_pairing_text(self):
        text = self.pair_text.get("1.0", "end").strip()
        if not text:
            self.pair_status.set("Paste the pairing JSON first.")
            return
        try:
            config = ui_common.import_pairing(text)
        except ValueError as exc:
            self.pair_status.set(f"Import failed: {exc}")
            return
        self.pair_text.delete("1.0", "end")
        self._refresh_pair_status()
        self.status.set(f"Pairing imported — connects to {config['host']} on Start")

    def toggle_modifier_layout(self):
        preset = "option_cmd" if self.option_command.get() else "native"
        self.variables["keymap_preset"].set(DISPLAY_OPTIONS["keymap_preset"][preset])

    def sync_modifier_toggle(self, *args):
        selected = self.variables["keymap_preset"].get()
        self.option_command.set(selected in ("option_cmd", "mac_layout", DISPLAY_OPTIONS["keymap_preset"]["option_cmd"],
                                            DISPLAY_OPTIONS["keymap_preset"]["mac_layout"]))

    def export_pairing(self):
        try:
            settings = self.collect_settings()
            destination = filedialog.asksaveasfilename(parent=self.root, title="Export private Mac pairing",
                initialfile="PrecisionKVM-pairing.json", defaultextension=".json", filetypes=[("Pairing file", "*.json")])
            if not destination:
                return
            _, _, token, fingerprint = identity()
            save_json(Path(destination), {"host": local_ip(settings["peer"]), "port": settings["port"],
                "tls": settings["tls"], "token": token, "fingerprint": fingerprint,
                "clipboard_enabled": settings["clipboard_enabled"], "clipboard_max_mb": 512})
            self.status.set("Pairing exported • import privately in the Mac app")
        except (OSError, ValueError) as exc:
            self.status.set(f"Could not export pairing: {exc}")

    def save(self):
        try:
            self.settings = self.collect_settings()
            save_json(state_dir() / "settings.json", self.settings)
            self.status.set("Settings saved • start the host to apply")
        except (ValueError, TypeError, tk.TclError) as exc:
            self.status.set(str(exc))

    def _init_peers(self):
        self._mons = geometry.monitors() or [(0, 0, 1920, 1080)]
        peers = {name: geometry.snap_to_edge(
                    (int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])), self._mons)
                 for name, r in (self.settings.get("peers") or {}).items()
                 if all(k in r for k in ("x", "y", "w", "h"))}
        if not peers:
            w, h = (int(v) for v in self.settings.get("peer_size", [1920, 1080]))
            side = self.settings.get("side", "left")
            peers = {"mac": geometry.snap_to_edge(
                geometry.default_peer(self._mons, side, 0.5, 1.0)[:2] + (w, h), self._mons)}
        self._peers = peers

    def draw_layout(self):
        canvas = self.canvas
        canvas.delete("all")
        self._rename_win = None
        self._mons = geometry.monitors() or self._mons
        w_px, h_px = max(canvas.winfo_width(), 700), 230
        rects = list(self._mons) + list(self._peers.values())
        bx, by, bw, bh = geometry.union_bounds(rects)
        pad = 14.0
        k = min((w_px - 2 * pad) / bw, (h_px - 2 * pad) / bh)
        ox, oy = (w_px - bw * k) / 2 - bx * k, (h_px - bh * k) / 2 - by * k
        self._scale = (k, ox, oy)
        for i, (mx, my, mw, mh) in enumerate(self._mons):
            x0, y0, x1, y1 = mx * k + ox, my * k + oy, (mx + mw) * k + ox, (my + mh) * k + oy
            canvas.create_rectangle(x0, y0, x1, y1, fill="#2b4a75", outline="#4d6a8c",
                                    width=2, tags=("pcbox",))
            label = "THIS PC" if i == 0 else f"display {i + 1}"
            canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=label, fill="white",
                               font=("Segoe UI", 9, "bold"), tags=("pcbox",))
        for name, (px, py, pw, ph) in self._peers.items():
            x0, y0, x1, y1 = px * k + ox, py * k + oy, (px + pw) * k + ox, (py + ph) * k + oy
            selected = name == self._selected
            canvas.create_rectangle(x0, y0, x1, y1, fill="#1f7a6c",
                                    outline="#f2f4f8" if selected else "#4d8c7d",
                                    width=3 if selected else 2,
                                    tags=("peerbox", f"peer:{name}"))
            canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=name, fill="white",
                               font=("Segoe UI", 9, "bold"),
                               tags=("peerbox", f"peer:{name}"))
        canvas.tag_bind("peerbox", "<Enter>", lambda _e: canvas.configure(cursor="fleur"))
        canvas.tag_bind("peerbox", "<Leave>", lambda _e: canvas.configure(cursor=""))

    def _peer_at(self, event):
        item = self.canvas.find_closest(event.x, event.y)
        if not item:
            return None
        for tag in self.canvas.gettags(item[0]):
            if tag.startswith("peer:"):
                return tag.split(":", 1)[1]
        return None

    def _press_peer(self, event):
        name = self._peer_at(event)
        self._selected = name or self._selected
        if name:
            self._drag = (name, event.x, event.y)
            self.draw_layout()

    def _drag_peer(self, event):
        if self._drag is None:
            return
        name, lx, ly = self._drag
        self.canvas.move(f"peer:{name}", event.x - lx, event.y - ly)
        self._drag = (name, event.x, event.y)

    def _drop_peer(self, _event):
        if self._drag is None:
            return
        name = self._drag[0]
        self._drag = None
        box = self.canvas.bbox(f"peer:{name}")
        if not box:
            return
        k, ox, oy = self._scale
        rect = (round((box[0] - ox) / k), round((box[1] - oy) / k),
                round((box[2] - box[0]) / k), round((box[3] - box[1]) / k))
        self._peers[name] = geometry.snap_to_edge(rect, self._mons)
        self.draw_layout()

    def _rename_peer(self, event):
        name = self._peer_at(event)
        if not name or self._rename_win is not None:
            return
        box = self.canvas.bbox(f"peer:{name}")
        if not box:
            return
        entry = tk.Entry(self.canvas, width=max(8, len(name) + 2), justify="center")
        entry.insert(0, name)
        entry.select_range(0, "end")
        win = self.canvas.create_window((box[0] + box[2]) / 2, (box[1] + box[3]) / 2,
                                        window=entry)
        self._rename_win = win

        def commit(_e=None):
            new = entry.get().strip()
            self.canvas.delete(win)
            self._rename_win = None
            if (new and new != name and new not in self._peers
                    and re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", new)):
                self._peers[new] = self._peers.pop(name)
                if self._selected == name:
                    self._selected = new
            self.draw_layout()

        def cancel(_e=None):
            self.canvas.delete(win)
            self._rename_win = None
            self.draw_layout()

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)
        entry.bind("<Escape>", cancel)
        entry.focus_set()

    def _add_peer(self):
        index = 2
        name = "mac2"
        while name in self._peers:
            index += 1
            name = f"mac{index}"
        pw, ph = (int(v) for v in self.settings.get("peer_size", [1920, 1080]))
        bx, by, bw, bh = geometry.union_bounds(self._mons)
        step = ph + 160
        spots = ([(bx + bw, by + i * step) for i in range(4)]
                 + [(bx - pw, by + i * step) for i in range(4)]
                 + [(bx + i * (pw + 160), by - ph) for i in range(3)]
                 + [(bx + i * (pw + 160), by + bh) for i in range(3)])
        rect = geometry.snap_to_edge((bx + bw, by, pw, ph), self._mons)
        for x, y in spots:
            candidate = geometry.snap_to_edge((x, y, pw, ph), self._mons)
            if not any(_overlap(candidate, r) for r in self._peers.values()):
                rect = candidate
                break
        self._peers[name] = rect
        self._selected = name
        self.draw_layout()

    def _remove_peer(self):
        if self._selected and self._selected in self._peers:
            del self._peers[self._selected]
            self._selected = None
            self.draw_layout()

    def _remove_at(self, event):
        name = self._peer_at(event)
        if name and name in self._peers:
            del self._peers[name]
            if self._selected == name:
                self._selected = None
            self.draw_layout()

    def refresh_logs(self):
        try:
            lines = (state_dir() / "host.log").read_text(encoding="utf-8",
                                                         errors="replace").splitlines()[-400:]
        except OSError:
            lines = []
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", "\n".join(lines) or "No log entries yet.")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def set_running(self, running):
        self.start_button.configure(text="Apply & reconnect" if running else "Start sharing")
        self.start_button.state(["!disabled"])
        for control in self.controls:
            control.state(["!disabled"])

    def start(self):
        try:
            settings = self.collect_settings()
            if self.session:
                self.session.stop()
                self.session = None
            save_json(state_dir() / "settings.json", settings)
            self.settings = settings
            if settings.get("role", "host") == "client":
                self.client.start()
                self.set_running(True)
                return
            self.session = HostSession(settings, self.report_status)
            self.session.start()
            if settings["ssh_target"]:
                threading.Thread(target=self.session.connect_mac, daemon=True, name="kvm-ssh").start()
            self.set_running(True)
        except Exception as exc:
            self.status.set(str(exc))

    def stop(self):
        self.client.stop()
        if self.session:
            self.session.stop()
            self.session = None
        self._rtt.clear()
        self._draw_latency()
        while not self.messages.empty():
            self.messages.get_nowait()
        self.set_running(False)
        self.status.set("Stopped • local input active")

    def report_status(self, message):
        logging.info(message)
        self._append_log(message)
        if message.startswith("Connected"):
            self.dot.itemconfigure(self._dot_item, fill="#3fd3bd")
        elif any(word in message for word in ("error", "Error", "rejected", "Emergency", "stopped")):
            self.dot.itemconfigure(self._dot_item, fill="#ff7a8a")
        try:
            self.messages.put_nowait(message)
        except queue.Full:
            try:
                self.messages.get_nowait()
                self.messages.put_nowait(message)
            except (queue.Empty, queue.Full):
                pass

    def _append_log(self, line):
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.configure(state="disabled")
            self.log_text.see("end")
        except tk.TclError:
            pass

    def _draw_latency(self):
        canvas = self.latency
        canvas.delete("all")
        w, h = max(canvas.winfo_width(), 300), 46
        samples = list(self._rtt)
        if not samples:
            canvas.create_text(10, h / 2, anchor="w", fill="#8b98ad", font=("Segoe UI", 9),
                               text="Live round-trip latency graph appears while sharing — "
                                    "expect ~1 ms on wired LAN")
            return
        ceiling = max(1.0, max(samples) * 1.15)
        span = max(1, len(samples) - 1)
        points = []
        for i, value in enumerate(samples):
            points += [8 + (w - 118) * i / span, h - 6 - (h - 16) * min(value, ceiling) / ceiling]
        if len(points) >= 4:
            canvas.create_line(*points, fill="#3fd3bd", width=2, smooth=True)
        latest = samples[-1]
        color = "#3fd3bd" if latest <= 2 else "#ffb5bc"
        canvas.create_text(w - 8, 14, anchor="e", text=f"{latest:.2f} ms",
                           fill=color, font=("Segoe UI Semibold", 15))
        canvas.create_text(w - 8, 36, anchor="e", text="round-trip",
                           fill="#8b98ad", font=("Segoe UI", 8))

    def poll(self):
        while not self.messages.empty():
            self.status.set(self.messages.get_nowait())
        if self.session:
            metrics = self.session.buffer.metrics if self.session.buffer else None
            if metrics:
                self.stats.set(f"RTT {self.session.rtt_ms:.2f} ms  |  queue peak {metrics.queue_high_water}  |  "
                               f"max queue {metrics.max_queue_ms:.2f} ms  |  merged moves {metrics.coalesced_moves:,}")
            if self.session.rtt_ms:
                self._rtt.append(float(self.session.rtt_ms))
                self._draw_latency()
            if self.session.thread and not self.session.thread.is_alive():
                self.set_running(False)
        if self.client.active:
            self.stats.set("Client connected to host — input is controlled remotely")
        self.root.after(100, self.poll)

    def close(self):
        if pystray is None or self._closing:
            self.stop()
            self.root.destroy()
            return
        self._hide_to_tray()

    _closing = False

    def _tray_image(self):
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill="#141d2c", outline="#3fd3bd", width=3)
        draw.rectangle((14, 18, 38, 34), fill="#2b4a75")
        draw.rectangle((40, 22, 52, 30), fill="#1f7a6c")
        draw.line((28, 40, 28, 50), fill="#3fd3bd", width=3)
        return image

    def _hide_to_tray(self):
        self.root.withdraw()
        if self._tray:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Open PrecisionKVM",
                             lambda _i, _e: self.root.after(0, self._restore), default=True),
            pystray.MenuItem("Quit", lambda _i, _e: self.root.after(0, self._quit)))
        self._tray = pystray.Icon("precisionkvm", self._tray_image(),
                                  "PrecisionKVM — input sharing", menu)
        threading.Thread(target=self._tray.run, daemon=True, name="kvm-tray").start()

    _tray: "pystray.Icon | None" = None

    def _restore(self):
        if self._tray:
            self._tray.stop()
            self._tray = None
        self.root.deiconify()
        self.root.lift()

    def _quit(self):
        self._closing = True
        if self._tray:
            self._tray.stop()
            self._tray = None
        self.stop()
        self.root.destroy()


def run_ui(start=False):
    logging.basicConfig(filename=str(state_dir() / "host.log"), level=logging.INFO,
                        format="%(asctime)s %(message)s")
    root = tk.Tk()
    panel = ControlPanel(root)
    if start:
        root.after(500, panel.start)
    root.mainloop()
