"""Tk control panel; networking and hooks never execute on the Tk thread."""
from __future__ import annotations

import queue
import logging
import threading
import tkinter as tk
from tkinter import ttk

from .config import load_settings, save_json, state_dir
from .session import HostSession


class ControlPanel:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = load_settings()
        self.session: HostSession | None = None
        self.messages: queue.Queue[str] = queue.Queue()
        root.title("PrecisionKVM • Input sharing")
        root.geometry("820x820")
        root.minsize(800, 780)
        root.configure(bg="#101722")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#101722")
        style.configure("TLabel", background="#101722", foreground="#d9e2ef", font=("Segoe UI", 11))
        style.configure("Title.TLabel", font=("Segoe UI", 24, "bold"), foreground="#ffffff")
        style.configure("Muted.TLabel", foreground="#91a2b9", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 11), padding=(18, 10))
        style.configure("TCheckbutton", background="#101722", foreground="#d9e2ef")
        panel = ttk.Frame(root, padding=28)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="PrecisionKVM", style="Title.TLabel").pack(anchor="w")
        ttk.Label(panel, text="Windows → macOS   /   Encrypted LAN input sharing",
                  style="Muted.TLabel").pack(anchor="w", pady=(3, 16))
        self.canvas = tk.Canvas(panel, height=160, bg="#172333", highlightthickness=0)
        self.canvas.pack(fill="x", pady=(0, 16))
        grid = ttk.Frame(panel)
        grid.pack(fill="x")
        self.peer = tk.StringVar(value=self.settings["peer"])
        self.ssh = tk.StringVar(value=self.settings["ssh_target"])
        self.side = tk.StringVar(value=self.settings["side"])
        self.hz = tk.StringVar(value=str(self.settings["mouse_hz"]))
        self.gain = tk.DoubleVar(value=self.settings["gain"])
        self.scroll = tk.DoubleVar(value=self.settings["scroll_gain"])
        self.swap = tk.BooleanVar(value=self.settings["swap_ctrl_cmd"])
        self.controls: list[ttk.Widget] = []
        rows = [("Mac address", ttk.Entry(grid, textvariable=self.peer)),
                ("Mac SSH target", ttk.Entry(grid, textvariable=self.ssh)),
                ("Monitor position", ttk.Combobox(grid, textvariable=self.side,
                 values=("left", "right", "top", "bottom"), state="readonly")),
                ("Mouse update limit", ttk.Combobox(grid, textvariable=self.hz,
                 values=("120", "144", "240", "500", "1000"), state="readonly")),
                ("Pointer sensitivity", ttk.Spinbox(grid, from_=0.1, to=5.0, increment=0.1, textvariable=self.gain)),
                ("Scroll sensitivity", ttk.Spinbox(grid, from_=0.1, to=5.0, increment=0.1, textvariable=self.scroll))]
        for row, (label, control) in enumerate(rows):
            ttk.Label(grid, text=label).grid(row=row, column=0, sticky="w", pady=5)
            control.grid(row=row, column=1, sticky="ew", padx=(24, 0), pady=5)
            self.controls.append(control)
        grid.columnconfigure(1, weight=1)
        checkbox = ttk.Checkbutton(grid, text="Use Ctrl for Mac Command shortcuts", variable=self.swap)
        checkbox.grid(row=len(rows), column=1, sticky="w", padx=(24, 0), pady=8)
        self.controls.append(checkbox)
        ttk.Label(panel, text="Keyboard: immediate delivery • Volume/media keys and two-axis scroll",
                  style="Muted.TLabel").pack(anchor="w", pady=(12, 3))
        ttk.Label(panel, text="Emergency return: Ctrl + Alt + F12   •   Settings apply on the next connection",
                  style="Muted.TLabel").pack(anchor="w")
        buttons = ttk.Frame(panel)
        buttons.pack(fill="x", pady=(20, 12))
        self.start_button = ttk.Button(buttons, text="Start sharing host", command=self.start)
        self.start_button.pack(side="left")
        ttk.Button(buttons, text="Stop / return locally", command=self.stop).pack(side="left", padx=10)
        self.status = tk.StringVar(value="Stopped • local input active")
        ttk.Label(panel, textvariable=self.status, wraplength=730).pack(anchor="w")
        self.stats = tk.StringVar(value="No session measurements yet")
        ttk.Label(panel, textvariable=self.stats, style="Muted.TLabel").pack(anchor="w", pady=5)
        self.side.trace_add("write", lambda *args: self.draw_layout())
        self.canvas.bind("<Configure>", lambda event: self.draw_layout())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    def draw_layout(self):
        canvas = self.canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 700)
        cx, cy = width / 2, 80
        side = self.side.get()
        dx, dy = {"left": (-190, 0), "right": (190, 0), "top": (0, -75),
                  "bottom": (0, 75)}[side]
        pcx, pcy = cx - dx / 2, cy - dy / 2
        mx, my = cx + dx / 2, cy + dy / 2
        w, h = (140, 54) if dy else (160, 90)
        canvas.create_line(pcx, pcy, mx, my, fill="#4bc6b2", width=2)
        for x, y, label, color in ((pcx, pcy, "THIS PC", "#365376"), (mx, my, "MAC CLIENT", "#237c73")):
            canvas.create_rectangle(x-w/2, y-h/2, x+w/2, y+h/2, fill=color, outline="#6d8aa6", width=1)
            canvas.create_text(x, y, text=label, fill="white", font=("Segoe UI", 11, "bold"))

    def start(self):
        try:
            if self.session:
                self.session.stop()
            settings = dict(self.settings, peer=self.peer.get().strip(), side=self.side.get(),
                            ssh_target=self.ssh.get().strip(),
                            mouse_hz=int(self.hz.get()), gain=float(self.gain.get()),
                            scroll_gain=float(self.scroll.get()), swap_ctrl_cmd=self.swap.get())
            if not settings["peer"] or not all(0.1 <= settings[k] <= 5 for k in ("gain", "scroll_gain")):
                raise ValueError("Enter a Mac address and sensitivity from 0.1 to 5.0")
            save_json(state_dir() / "settings.json", settings)
            self.settings = settings
            self.session = HostSession(settings, self.report_status)
            self.session.start()
            if settings["ssh_target"]:
                threading.Thread(target=self.session.connect_mac, daemon=True, name="kvm-ssh").start()
            self.start_button.state(["disabled"])
            for control in self.controls:
                control.state(["disabled"])
        except Exception as exc:
            self.status.set(str(exc))

    def stop(self):
        if self.session:
            self.session.stop()
            self.session = None
        self.start_button.state(["!disabled"])
        for control in self.controls:
            control.state(["!disabled"])
        self.status.set("Stopped • local input active")

    def report_status(self, message):
        logging.info(message)
        self.messages.put(message)

    def poll(self):
        while not self.messages.empty():
            self.status.set(self.messages.get_nowait())
        if self.session:
            metrics = self.session.buffer.metrics if self.session.buffer else None
            if metrics:
                self.stats.set(f"RTT {self.session.rtt_ms:.2f} ms  |  queue peak {metrics.queue_high_water}  |  "
                               f"max queue {metrics.max_queue_ms:.2f} ms  |  merged moves {metrics.coalesced_moves:,}")
            if self.session.thread and not self.session.thread.is_alive():
                self.start_button.state(["!disabled"])
                for control in self.controls:
                    control.state(["!disabled"])
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
