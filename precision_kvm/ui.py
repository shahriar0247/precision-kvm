"""Tabbed native settings; native input and networking stay off the Tk thread."""
from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from .config import identity, load_settings, local_ip, save_json, state_dir, validate_settings
from .keymap import KEY_NAMES, MAC_KEY_NAMES, normalize_keymap
from .session import HostSession

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
        self.keymap = dict(self.settings["keymap"])
        root.title("PrecisionKVM • Input sharing")
        root.geometry("940x880")
        root.minsize(880, 800)
        root.configure(bg="#101722")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#101722")
        style.configure("TLabel", background="#101722", foreground="#d9e2ef", font=("Segoe UI", 11))
        style.configure("Title.TLabel", font=("Segoe UI", 24, "bold"), foreground="white")
        style.configure("Muted.TLabel", foreground="#91a2b9", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 8))
        style.configure("TCheckbutton", background="#101722", foreground="#d9e2ef")
        style.configure("TNotebook", background="#101722", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 12), background="#172333", foreground="#9fb0c6")
        style.map("TNotebook.Tab", background=[("selected", "#263d54")], foreground=[("selected", "white")])
        style.configure("TEntry", fieldbackground="#172333", foreground="#e5edf8", padding=8)
        style.configure("TCombobox", fieldbackground="#172333", foreground="#e5edf8", padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", "#172333")], foreground=[("readonly", "#e5edf8")])
        style.configure("Accent.TButton", background="#40cdb6", foreground="#10221f", font=("Segoe UI", 11, "bold"))
        style.configure("Stop.TButton", background="#432b35", foreground="#ffb5bc")
        root.option_add("*TCombobox*Listbox.background", "#172333")
        root.option_add("*TCombobox*Listbox.foreground", "#e5edf8")
        panel = ttk.Frame(root, padding=24)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="PrecisionKVM", style="Title.TLabel").pack(anchor="w")
        ttk.Label(panel, text="Native input • Encrypted clipboard • Windows → macOS",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 18))
        notebook = ttk.Notebook(panel)
        notebook.pack(fill="both", expand=True)
        connection, inputs, keys, sharing, tweaks = (ttk.Frame(notebook, padding=18) for _ in range(5))
        for frame, title in ((connection, "Connection"), (inputs, "Mouse & gestures"),
                             (keys, "Key remapping"), (tweaks, "Mac Tweaks"), (sharing, "Clipboard & safety")):
            notebook.add(frame, text=title)
            frame.columnconfigure(1, weight=1)
        self.canvas = tk.Canvas(connection, height=170, bg="#172333", highlightthickness=0)
        self.canvas.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 18))
        self.field(connection, 1, "Mac address", "peer")
        self.field(connection, 2, "Mac SSH target", "ssh_target")
        self.field(connection, 3, "Monitor position", "side", ("left", "right", "top", "bottom"))
        self.field(connection, 4, "Mouse update limit (Hz)", "mouse_hz", ("120", "144", "240", "500", "1000"))
        pairing = ttk.Button(connection, text="Export pairing file for Mac…", command=self.export_pairing)
        pairing.grid(row=5, column=0, columnspan=2, sticky="w", pady=16)
        self.controls.append(pairing)
        self.note(connection, 6, "Import the pairing file in the Mac app to connect securely.\n"
                  "Edit settings anytime, then click Apply & reconnect. Keyboard delivery stays immediate.")
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
        self.field(sharing, 3, "Hold physical Esc to STOP (seconds)", "emergency_escape_seconds", bounds=(0.5, 10, 0.1))
        self.field(sharing, 4, "Rapid left-click count to STOP", "emergency_click_count", bounds=(4, 20, 1))
        self.field(sharing, 5, "Rapid-click time window (seconds)", "emergency_click_window", bounds=(0.3, 5, 0.1))
        self.note(sharing, 6, "Defaults: hold Esc 1.5 seconds OR left-click 6 times within 1.2 seconds.\n"
                  "These STOP the PC server and clipboard sharing, restore local input and\n"
                  "prevent edge re-entry until you explicitly start the host again.\n"
                  "Ctrl+Alt+F12 returns locally without stopping the server.")
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
        self.variables["side"].trace_add("write", lambda *args: self.draw_layout())
        self.canvas.bind("<Configure>", lambda event: self.draw_layout())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    def field(self, parent, row, label, key, values=None, bounds=None):
        options = DISPLAY_OPTIONS.get(key, {})
        variable = tk.StringVar(value=options.get(str(self.settings[key]), str(self.settings[key])))
        self.variables[key] = variable
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=7)
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

    def check(self, parent, row, label, key):
        variable = tk.BooleanVar(value=self.settings[key])
        self.variables[key] = variable
        control = ttk.Checkbutton(parent, text=label, variable=variable)
        control.grid(row=row, column=0, columnspan=2, sticky="w", pady=8)
        self.controls.append(control)

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
        return validate_settings(settings)

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
            from pathlib import Path
            save_json(Path(destination), {"host": local_ip(settings["peer"]), "port": settings["port"],
                "token": token, "fingerprint": fingerprint, "clipboard_enabled": settings["clipboard_enabled"],
                "clipboard_max_mb": 512})
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

    def draw_layout(self):
        canvas = self.canvas
        canvas.delete("all")
        cx, cy = max(canvas.winfo_width(), 700) / 2, 85
        side = self.variables["side"].get()
        dx, dy = {"left": (-190, 0), "right": (190, 0), "top": (0, -75), "bottom": (0, 75)}[side]
        w, h = (140, 54) if dy else (160, 90)
        canvas.create_line(cx-dx/2, cy-dy/2, cx+dx/2, cy+dy/2, fill="#4bc6b2", width=2)
        for x, y, label, color in ((cx-dx/2, cy-dy/2, "THIS PC", "#365376"),
                                   (cx+dx/2, cy+dy/2, "MAC CLIENT", "#237c73")):
            canvas.create_rectangle(x-w/2, y-h/2, x+w/2, y+h/2, fill=color, outline="#6d8aa6")
            canvas.create_text(x, y, text=label, fill="white", font=("Segoe UI", 11, "bold"))

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
            save_json(state_dir() / "settings.json", settings)
            self.settings = settings
            self.session = HostSession(settings, self.report_status)
            self.session.start()
            if settings["ssh_target"]:
                threading.Thread(target=self.session.connect_mac, daemon=True, name="kvm-ssh").start()
            self.set_running(True)
        except Exception as exc:
            self.status.set(str(exc))

    def stop(self):
        if self.session:
            self.session.stop()
            self.session = None
        while not self.messages.empty():
            self.messages.get_nowait()
        self.set_running(False)
        self.status.set("Stopped • local input active")

    def report_status(self, message):
        logging.info(message)
        try:
            self.messages.put_nowait(message)
        except queue.Full:
            try:
                self.messages.get_nowait()
                self.messages.put_nowait(message)
            except (queue.Empty, queue.Full):
                pass

    def poll(self):
        while not self.messages.empty():
            self.status.set(self.messages.get_nowait())
        if self.session:
            metrics = self.session.buffer.metrics if self.session.buffer else None
            if metrics:
                self.stats.set(f"RTT {self.session.rtt_ms:.2f} ms  |  queue peak {metrics.queue_high_water}  |  "
                               f"max queue {metrics.max_queue_ms:.2f} ms  |  merged moves {metrics.coalesced_moves:,}")
            if self.session.thread and not self.session.thread.is_alive():
                self.set_running(False)
        self.root.after(100, self.poll)

    def close(self):
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
