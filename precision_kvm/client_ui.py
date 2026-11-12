"""Native macOS control panel — one app, host or client chosen in the window.

Importing never opens a window or posts input. The client side connects to a
paired PC host; the host side shares this Mac's input to a paired PC. All
long-running work lives on session threads; the panel only polls a status
queue on a timer.
"""
from __future__ import annotations

import collections
import json
import logging
import math
from pathlib import Path
import queue
import re
import sys
import threading
import time

from .config import load_settings, save_json, state_dir, validate_settings


class _StatusQueue:
    """Bounded status queue shared by both controllers."""
    def __init__(self):
        self.messages: queue.Queue[str] = queue.Queue(maxsize=128)

    def post_status(self, message):
        logging.info("%s", message)
        try:
            self.messages.put_nowait(str(message))
        except queue.Full:
            try:
                self.messages.get_nowait()
                self.messages.put_nowait(str(message))
            except (queue.Empty, queue.Full):
                pass

    def drain(self):
        while True:
            try:
                yield self.messages.get_nowait()
            except queue.Empty:
                return


class ClientController(_StatusQueue):
    """UI-independent worker lifecycle; cancellation is owned by the session."""
    def __init__(self, config_path: Path, runner=None):
        super().__init__()
        self.config_path = Path(config_path)
        self.runner = runner
        self.stopped = threading.Event()
        self.worker: threading.Thread | None = None

    @property
    def active(self):
        return self.worker is not None and self.worker.is_alive()

    def configuration(self):
        if not self.config_path.exists():
            return {"host":"", "port":24819,"fingerprint":"","token":"","tweaks":{}}
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    @property
    def paired(self):
        config = self.configuration()
        fingerprint = config.get("fingerprint","")
        return (bool(config.get("host")) and isinstance(fingerprint,str) and len(fingerprint)==64
                and all(c in "0123456789abcdef" for c in fingerprint.lower())
                and isinstance(config.get("token"),str) and len(config["token"])>=32)

    def start(self):
        if self.active:
            return False
        if not self.paired:
            self.post_status("Import the pairing file from your PC to get started")
            return False
        config = self.configuration()
        self.stopped = threading.Event()

        def work():
            failed = False
            try:
                runner = self.runner
                if runner is None:
                    from .session import run_client_forever
                    runner = run_client_forever
                runner(config, stop_event=self.stopped, status=self.post_status,
                       config_path=self.config_path)
            except Exception as exc:
                failed = True
                self.post_status(f"Client error: {type(exc).__name__}: {exc}")
            finally:
                if not failed:
                    self.post_status("Disconnected - client stopped")

        self.post_status("Connecting - checks the server every second")
        self.worker = threading.Thread(target=work, daemon=True, name="kvm-mac-client")
        self.worker.start()
        return True

    def stop(self):
        self.stopped.set()
        if self.active:
            self.post_status("Disconnecting - releasing remote input")

    def save_host(self, host: str):
        host = host.strip()
        if not host or len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", host):
            raise ValueError("Enter the Windows IP address or hostname")
        if self.active:
            raise RuntimeError("Disconnect before changing the server")
        config = self.configuration()
        config["host"] = host
        save_json(self.config_path, config)

    def import_pairing(self,path):
        if self.active:
            raise RuntimeError("Disconnect before importing pairing")
        imported = json.loads(Path(path).read_text(encoding="utf-8"))
        config = self.configuration()
        for name in ("host","port","fingerprint","token"):
            if name not in imported:
                raise ValueError("This is not a PrecisionKVM pairing file")
            config[name] = imported[name]
        if (not isinstance(config["host"],str) or not config["host"]
                or type(config["port"]) is not int or not 1 <= config["port"] <= 65533
                or not isinstance(config["fingerprint"],str) or len(config["fingerprint"])!=64
                or any(c not in "0123456789abcdef" for c in config["fingerprint"].lower())
                or not isinstance(config["token"],str) or len(config["token"])<32):
            raise ValueError("Pairing details are invalid")
        self.config_path.parent.mkdir(parents=True,exist_ok=True)
        save_json(self.config_path,config)
        self.post_status("Pairing imported - ready to connect")

    def save_tweaks(self,values):
        allowed = {"smooth_scroll","smooth_scroll_ms","smooth_scroll_hz","scroll_gain_x",
                   "scroll_gain_y","scroll_invert_x","scroll_invert_y","windows_home_end",
                   "windows_word_nav","keymap_preset"}
        tweaks = {key:value for key,value in values.items() if key in allowed}
        for name,low,high in (("smooth_scroll_ms",10,150),("smooth_scroll_hz",30,240),
                              ("scroll_gain_x",0.1,5),("scroll_gain_y",0.1,5)):
            value = float(tweaks[name])
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")
            tweaks[name] = int(value) if name=="smooth_scroll_hz" else value
        if tweaks["keymap_preset"] not in ("native","option_cmd","ctrl_cmd","mac_layout"):
            raise ValueError("Invalid keyboard layout")
        config = self.configuration()
        config["tweaks"] = tweaks
        config["local_smooth_scroll"] = bool(values.get("local_smooth_scroll",False))
        self.config_path.parent.mkdir(parents=True,exist_ok=True)
        save_json(self.config_path,config)


class MouseRateMonitor:
    """Counts pointer-move events at the HID tap to estimate polling rate.

    Listen-only tap on a background run loop; each event contributes one
    timestamp. rate() is the count inside a rolling one-second window —
    a real measure of how often the device reports, not a simulation.
    """
    def __init__(self):
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._events: collections.deque = collections.deque(maxlen=65536)
        self.total = 0
        self.peak = 0
        self.error: str | None = None
        self._tap = self._loop = self._source = None
        self._callback = self._on_event  # keep the native callback alive

    def _on_event(self, _proxy, _kind, event, _refcon):
        with self._lock:
            self._events.append(time.perf_counter())
            self.total += 1
        return event

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self.error = None
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="kvm-mouse-rate",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        loop = self._loop
        if loop is not None:
            import CoreFoundation as cf
            cf.CFRunLoopStop(loop)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(3)

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def reset(self):
        with self._lock:
            self._events.clear()
            self.total = self.peak = 0

    def rate(self) -> int:
        """Pointer events seen in the last second; refreshes the peak."""
        cutoff = time.perf_counter() - 1.0
        with self._lock:
            while self._events and self._events[0] < cutoff:
                self._events.popleft()
            count = len(self._events)
            self.peak = max(self.peak, count)
            return count

    def _run(self):
        import Quartz
        import CoreFoundation as cf
        mask = 0
        for code in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDragged,
                     Quartz.kCGEventRightMouseDragged, Quartz.kCGEventOtherMouseDragged):
            mask |= Quartz.CGEventMaskBit(code)
        try:
            self._tap = Quartz.CGEventTapCreate(
                Quartz.kCGHIDEventTap, Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionListenOnly, mask, self._callback, None)
            if self._tap is None:
                self.error = ("macOS denied the mouse event tap — grant Input "
                              "Monitoring / Accessibility for PrecisionKVM")
                return
            self._source = cf.CFMachPortCreateRunLoopSource(None, self._tap, 0)
            self._loop = cf.CFRunLoopGetCurrent()
            cf.CFRunLoopAddSource(self._loop, self._source, cf.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(self._tap, True)
            self._ready.set()
            while not self._stop.is_set():
                result = cf.CFRunLoopRunInMode(cf.kCFRunLoopDefaultMode, 0.5, False)
                if result == cf.kCFRunLoopRunFinished:
                    return
        except Exception as exc:
            self.error = str(exc)
        finally:
            self._ready.set()
            if self._tap is not None:
                Quartz.CGEventTapEnable(self._tap, False)
            if self._loop is not None and self._source is not None:
                cf.CFRunLoopRemoveSource(self._loop, self._source,
                                         cf.kCFRunLoopCommonModes)
            if self._tap is not None:
                Quartz.CFMachPortInvalidate(self._tap)
            self._tap = self._loop = self._source = None


class HostController(_StatusQueue):
    """HostSession lifecycle for the panel; mirrors ClientController's surface."""
    def __init__(self):
        super().__init__()
        self.session = None
        self.settings_path = state_dir() / "settings.json"

    @property
    def active(self):
        return (self.session is not None and self.session.thread is not None
                and self.session.thread.is_alive())

    def configuration(self):
        return load_settings()

    def save(self, values):
        settings = self.configuration()
        settings.update(values)
        settings["role"] = "host"
        save_json(self.settings_path, validate_settings(settings))
        return settings

    def export_pairing(self, path, settings=None):
        from .config import identity, local_ip
        settings = settings or self.configuration()
        _, _, token, fingerprint = identity()
        peer = str(settings.get("peer","")).strip()
        try:
            host = local_ip(peer) if peer else self._local_ip()
        except OSError:
            host = self._local_ip()
        save_json(Path(path), {"host": host, "port": settings["port"],
            "tls": settings["tls"], "token": token, "fingerprint": fingerprint,
            "clipboard_enabled": settings["clipboard_enabled"], "clipboard_max_mb": 512})
        return host

    @staticmethod
    def _local_ip():
        import socket
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"

    def start(self):
        if self.active:
            return False
        settings = self.configuration()
        if not str(settings.get("peer","")).strip():
            self.post_status("Enter your PC's address first - it is used for pairing")
            return False
        if sys.platform == "darwin":
            from .mac import MacInjector
            if not MacInjector.accessibility_available(prompt=True):
                self.post_status("Grant Accessibility access to PrecisionKVM in "
                                 "System Settings > Privacy & Security > Accessibility")
                return False
        settings = validate_settings(dict(settings, role="host"))
        save_json(self.settings_path, settings)
        from .session import HostSession
        self.session = HostSession(settings, self.post_status)
        self.session.start()
        self.post_status("Starting host - waiting for a paired client")
        return True

    def stop(self):
        if self.session:
            self.session.stop()
            self.session = None
            self.post_status("Stopped - local input active")


def run_client_gui(config_path: Path):
    """Open the unified panel. `config_path` remains the client pairing file."""
    if sys.platform != "darwin":
        raise OSError("The native panel requires macOS")
    import AppKit as a
    import Foundation as f
    import objc
    logging.basicConfig(filename=str(state_dir() / "mac-client-status.log"), level=logging.INFO,
                        format="%(asctime)s %(message)s")

    client = ClientController(config_path)
    host = HostController()
    rate_monitor = MouseRateMonitor()
    config = client.configuration()
    settings = host.configuration()
    app = a.NSApplication.sharedApplication()
    app.setActivationPolicy_(a.NSApplicationActivationPolicyRegular)
    local_tap = None

    def update_local_scroll():
        nonlocal local_tap
        if local_tap:
            local_tap.stop()
            local_tap = None
        saved = client.configuration()
        if saved.get("local_smooth_scroll",False):
            from .scroll import MacLocalScrollTap
            tweaks = saved.get("tweaks",{})
            local_tap = MacLocalScrollTap(rate_hz=int(tweaks.get("smooth_scroll_hz",120)),
                duration_ms=float(tweaks.get("smooth_scroll_ms",60)),
                status=lambda message: client.post_status("Local scrolling: "+message))
            local_tap.start()

    class PrecisionKVMDelegate(f.NSObject,  # type: ignore[call-arg]
            protocols=[objc.protocolNamed("NSApplicationDelegate"), objc.protocolNamed("NSWindowDelegate")]):
        @objc.signature(b"v@:@")
        def toggleClient_(self, sender):
            if client.active:
                client.stop()
            else:
                try:
                    client.save_host(str(self.host_field.stringValue()))
                    client.start()
                except Exception as exc:
                    client.post_status(str(exc))

        @objc.signature(b"v@:@")
        def importPairing_(self,sender):
            if client.active:
                client.post_status("Disconnect before importing another pairing file")
                return
            panel = a.NSOpenPanel.openPanel()
            panel.setTitle_("Import pairing from your Windows PC")
            panel.setAllowedFileTypes_(["json"])
            panel.setAllowsMultipleSelection_(False)
            panel.setCanChooseDirectories_(False)
            if panel.runModal()==a.NSModalResponseOK:
                try:
                    client.import_pairing(str(panel.URL().path()))
                    self.host_field.setStringValue_(str(client.configuration()["host"]))
                except Exception as exc:
                    client.post_status(str(exc))

        @objc.signature(b"v@:@")
        def toggleHost_(self, sender):
            if host.active:
                host.stop()
            else:
                try:
                    settings = self._host_settings()
                    save_json(state_dir() / "settings.json",
                              validate_settings(dict(load_settings(), **settings, role="host")))
                    host.start()
                except Exception as exc:
                    host.post_status(str(exc))

        def _host_settings(self):
            try:
                port = int(str(self.port_field.stringValue()).strip())
            except ValueError:
                raise ValueError("Port must be a number between 1024 and 65533")
            sides = ("left","right","top","bottom")
            return {"peer": str(self.peer_field.stringValue()).strip(),
                    "port": port,
                    "tls": bool(self.tls_check.state()),
                    "discovery": bool(self.discovery_check.state()),
                    "clipboard_enabled": bool(self.clipboard_check.state()),
                    "side": sides[max(0, self.side_popup.indexOfSelectedItem())]}

        @objc.signature(b"v@:@")
        def exportPairing_(self,sender):
            try:
                settings = self._host_settings()
                panel = a.NSSavePanel.savePanel()
                panel.setTitle_("Export pairing for your PC")
                panel.setNameFieldStringValue_("PrecisionKVM-pairing.json")
                panel.setAllowedFileTypes_(["json"])
                if panel.runModal()==a.NSModalResponseOK:
                    address = host.export_pairing(str(panel.URL().path()),
                                                  dict(load_settings(), **settings))
                    host.post_status(f"Pairing exported - import it on the PC at {address}")
            except Exception as exc:
                host.post_status(str(exc))

        @objc.signature(b"v@:@")
        def roleChanged_(self,sender):
            role = "client" if sender.selectedSegment()==0 else "host"
            try:
                save_json(state_dir() / "settings.json",
                          dict(load_settings(), role=role))
            except Exception:
                pass
            self._apply_role()

        def _apply_role(self):
            is_client = self.role.selectedSegment()==0
            self.client_view.setHidden_(not is_client)
            self.host_view.setHidden_(is_client)
            title = ("Your PC controls this Mac" if is_client
                     else "This Mac controls your PC")
            self.connection_title.setStringValue_(title)

        @objc.signature(b"v@:@")
        def resetRate_(self,sender):
            rate_monitor.reset()

        @objc.signature(b"v@:@@")
        def tabView_didSelectTabViewItem_(self, tabView, item):
            if str(item.identifier()) == "mouse":
                rate_monitor.start()
            else:
                rate_monitor.stop()

        @objc.signature(b"v@:@")
        def applyTweaks_(self,sender):
            try:
                values = {}
                for name,(kind,field) in self.fields.items():
                    values[name] = bool(field.state()) if kind=="bool" else str(field.stringValue())
                values["keymap_preset"] = "option_cmd" if values.pop("mac_layout") else "native"
                client.save_tweaks(values)
                self.pending_restart = client.active
                client.stop()
                update_local_scroll()
                client.post_status("Preferences saved - they apply the next time the PC controls this Mac")
            except Exception as exc:
                client.post_status(str(exc))

        @objc.signature(b"v@:@")
        def poll_(self, timer):
            for message in client.drain():
                if message.startswith("Clipboard:"):
                    self.clipboard.setStringValue_(message[:800])
                elif message.startswith("Local scrolling:"):
                    self.local_status.setStringValue_(message[:800])
                else:
                    self.status.setStringValue_(message[:800])
                    self.status.setTextColor_(
                        a.NSColor.systemGreenColor() if message.startswith("Connected")
                        else a.NSColor.systemOrangeColor() if "unavailable" in message.lower()
                        else a.NSColor.labelColor())
            for message in host.drain():
                self.host_status.setStringValue_(message[:800])
                self.host_status.setTextColor_(
                    a.NSColor.systemGreenColor() if message.startswith("Connected")
                    else a.NSColor.systemOrangeColor() if "unavailable" in message.lower()
                    else a.NSColor.labelColor())
            busy = client.active or host.active
            self.connect_button.setTitle_("Disconnect" if client.active else "Connect to PC")
            self.host_field.setEnabled_(not client.active)
            self.import_button.setEnabled_(not client.active)
            self.share_button.setTitle_("Stop sharing" if host.active else "Start sharing")
            self.role.setEnabled_(not busy)
            self.rate_value.setStringValue_(f"{rate_monitor.rate()} Hz")
            self.rate_detail.setStringValue_(
                f"Peak {rate_monitor.peak} Hz · {rate_monitor.total} events")
            if rate_monitor.error:
                self.rate_status.setStringValue_(rate_monitor.error)
            elif rate_monitor.running:
                self.rate_status.setStringValue_(
                    "Listening — move your mouse in circles anywhere on screen")
            if self.pending_restart and not client.active and not self.quitting:
                self.pending_restart = False
                client.start()
            if self.quitting and not busy:
                timer.invalidate()
                app.replyToApplicationShouldTerminate_(True)

        @objc.signature(b"B@:@")
        def windowShouldClose_(self, sender):
            app.terminate_(None)
            return False

        @objc.signature(b"Q@:@")
        def applicationShouldTerminate_(self, sender):
            self.pending_restart = False
            client.stop()
            host.stop()
            rate_monitor.stop()
            if local_tap:
                local_tap.stop()
            if client.active or host.active:
                self.quitting = True
                return a.NSTerminateLater
            return a.NSTerminateNow

    delegate = PrecisionKVMDelegate.alloc().init()
    delegate.quitting = delegate.pending_restart = False
    delegate.fields = {}
    app.setDelegate_(delegate)
    style = (a.NSWindowStyleMaskTitled | a.NSWindowStyleMaskClosable
             | a.NSWindowStyleMaskMiniaturizable | a.NSWindowStyleMaskResizable)
    window = a.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((0,0),(700,720)),style,a.NSBackingStoreBuffered,False)
    window.setTitle_("PrecisionKVM")
    window.setMinSize_((660,660))
    window.setAppearance_(a.NSAppearance.appearanceNamed_(a.NSAppearanceNameDarkAqua))
    window.setBackgroundColor_(a.NSColor.colorWithRed_green_blue_alpha_(.063,.09,.133,1))
    window.setReleasedWhenClosed_(False)
    window.setDelegate_(delegate)
    content = window.contentView()
    resizes = a.NSViewWidthSizable | a.NSViewMinYMargin

    def label(parent,text,frame,size=14,color=None):
        field = a.NSTextField.labelWithString_(text)
        field.setFrame_(frame)
        field.setFont_(a.NSFont.systemFontOfSize_(size))
        field.setTextColor_(color or a.NSColor.labelColor())
        field.setAutoresizingMask_(resizes)
        field.setSelectable_(True)
        field.setMaximumNumberOfLines_(0)
        parent.addSubview_(field)
        return field

    def button(parent,title,frame,action,mask=None):
        control = a.NSButton.alloc().initWithFrame_(frame)
        control.setTitle_(title)
        control.setBezelStyle_(a.NSBezelStyleRounded)
        control.setTarget_(delegate)
        control.setAction_(action)
        control.setAutoresizingMask_(mask if mask is not None else resizes)
        parent.addSubview_(control)
        return control

    muted = a.NSColor.secondaryLabelColor()
    label(content,"PrecisionKVM",((24,652),(500,42)),28)
    label(content,"Keyboard, mouse and clipboard sharing between this Mac and your PC",
          ((26,622),(600,26)),12,muted)
    delegate.role = a.NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
        ["Client - controlled by your PC","Host - share this Mac's input"],
        0, delegate, "roleChanged:")
    delegate.role.setFrame_(((24,574),(500,30)))
    delegate.role.setAutoresizingMask_(resizes)
    delegate.role.setSelectedSegment_(0 if settings.get("role","client")!="host" else 1)
    content.addSubview_(delegate.role)

    tabs = a.NSTabView.alloc().initWithFrame_(((16,16),(668,540)))
    tabs.setAutoresizingMask_(a.NSViewWidthSizable | a.NSViewHeightSizable)
    content.addSubview_(tabs)
    pages = {}
    tabs.setDelegate_(delegate)
    for identifier,title in (("connection","Connection"),("prefs","Preferences"),
                             ("mouse","Mouse rate")):
        item = a.NSTabViewItem.alloc().initWithIdentifier_(identifier)
        item.setLabel_(title)
        view = a.NSView.alloc().initWithFrame_(tabs.contentRect())
        item.setView_(view)
        tabs.addTabViewItem_(item)
        pages[identifier] = view
    connection = pages["connection"]
    top = connection.bounds().size.height

    delegate.connection_title = label(
        connection,"Your PC controls this Mac",((20,top-44),(560,30)),18,muted)

    # --- Client view ------------------------------------------------------
    client_view = a.NSView.alloc().initWithFrame_(connection.bounds())
    client_view.setAutoresizingMask_(a.NSViewWidthSizable | a.NSViewHeightSizable)
    connection.addSubview_(client_view)
    delegate.client_view = client_view
    label(client_view,"CONNECTION",((4,top-92),(560,20)),11,muted)
    delegate.status = label(client_view,
        "Import pairing to get started" if not client.paired else "Ready to connect",
        ((4,top-158),(560,56)),20)
    label(client_view,"Windows server",((4,top-196),(170,26)),14)
    delegate.host_field = a.NSTextField.alloc().initWithFrame_(((178,top-198),(400,30)))
    delegate.host_field.setStringValue_(str(config.get("host","")))
    delegate.host_field.setPlaceholderString_("Windows IP address")
    delegate.host_field.setAutoresizingMask_(resizes)
    client_view.addSubview_(delegate.host_field)
    delegate.connect_button = button(client_view,"Connect to PC",
        ((4,top-258),(180,38)),"toggleClient:")
    delegate.import_button = button(client_view,"Import pairing file...",
        ((196,top-258),(200,38)),"importPairing:")
    delegate.clipboard = label(client_view,"Clipboard: waiting for a connection",
        ((4,top-310),(560,38)),13,muted)
    label(client_view,"Automatic reconnect",((4,top-352),(560,26)),15)
    label(client_view,"The client checks for your PC every second when disconnected.\n"
          "Move across the configured edge on your PC to control this Mac.\n\n"
          "First setup: export a pairing file on the PC, then import it here.\n"
          "Pairing tokens stay private and never appear in this panel.",
          ((4,40),(560,110)),13,muted)

    # --- Host view --------------------------------------------------------
    host_view = a.NSView.alloc().initWithFrame_(connection.bounds())
    host_view.setAutoresizingMask_(a.NSViewWidthSizable | a.NSViewHeightSizable)
    host_view.setHidden_(True)
    connection.addSubview_(host_view)
    delegate.host_view = host_view
    label(host_view,"SHARING",((4,top-92),(560,20)),11,muted)
    delegate.host_status = label(host_view,"Stopped - local input active",
        ((4,top-158),(560,56)),20)
    label(host_view,"Your PC's address",((4,top-196),(170,26)),14)
    delegate.peer_field = a.NSTextField.alloc().initWithFrame_(((178,top-198),(400,30)))
    delegate.peer_field.setStringValue_(str(settings.get("peer","")))
    delegate.peer_field.setPlaceholderString_("Windows PC IP address")
    delegate.peer_field.setAutoresizingMask_(resizes)
    host_view.addSubview_(delegate.peer_field)
    label(host_view,"Port",((4,top-238),(60,26)),14)
    delegate.port_field = a.NSTextField.alloc().initWithFrame_(((178,top-240),(100,30)))
    delegate.port_field.setStringValue_(str(settings.get("port",24819)))
    host_view.addSubview_(delegate.port_field)
    label(host_view,"Screen edge to cross",((290,top-238),(160,26)),13)
    delegate.side_popup = a.NSPopUpButton.alloc().initWithFrame_pullsDown_(
        ((452,top-242),(120,30)),False)
    delegate.side_popup.addItemsWithTitles_(["left","right","top","bottom"])
    try:
        delegate.side_popup.selectItemAtIndex_(("left","right","top","bottom").index(
            str(settings.get("side","left"))))
    except ValueError:
        delegate.side_popup.selectItemAtIndex_(0)
    delegate.side_popup.setAutoresizingMask_(a.NSViewMinXMargin | a.NSViewMinYMargin)
    host_view.addSubview_(delegate.side_popup)
    for name,title,y in (("tls_check","Encrypted connection (TLS, recommended)",top-286),
                         ("discovery_check","Advertise on the local network (mDNS)",top-318),
                         ("clipboard_check","Share clipboard text and images",top-350)):
        toggle = a.NSButton.alloc().initWithFrame_(((4,y),(560,26)))
        toggle.setButtonType_(a.NSButtonTypeSwitch)
        toggle.setTitle_(title)
        toggle.setState_(a.NSControlStateValueOn
                         if settings.get(name.rsplit("_check",1)[0],True)
                         else a.NSControlStateValueOff)
        toggle.setAutoresizingMask_(resizes)
        host_view.addSubview_(toggle)
        setattr(delegate,name,toggle)
    delegate.share_button = button(host_view,"Start sharing",
        ((4,top-410),(180,38)),"toggleHost:")
    button(host_view,"Export pairing file...",
        ((196,top-410),(200,38)),"exportPairing:")
    label(host_view,"Move the cursor across the chosen edge to control your PC.\n"
          "Hold Esc to stop sharing; Ctrl+Alt+F12 returns locally.\n\n"
          "Export the pairing file, then import it in the PrecisionKVM app on your PC.",
          ((4,40),(560,90)),13,muted)

    # --- Preferences tab: everything scrolls, nothing can overlap ---------
    prefs = pages["prefs"]
    scroller = a.NSScrollView.alloc().initWithFrame_(prefs.bounds())
    scroller.setAutoresizingMask_(a.NSViewWidthSizable | a.NSViewHeightSizable)
    scroller.setHasVerticalScroller_(True)
    scroller.setBorderType_(a.NSNoBorder)
    scroller.setDrawsBackground_(False)
    document = a.NSView.alloc().initWithFrame_(((0,0),(640,640)))
    document.setFlipped_(True)
    scroller.setDocumentView_(document)
    prefs.addSubview_(scroller)
    saved = config.get("tweaks",{})
    y = [16]

    def section(title):
        field = label(document,title,((20,y[0]),(560,22)),12,muted)
        y[0] += 34
        return field

    def check(name,title,default=False):
        control = a.NSButton.alloc().initWithFrame_(((20,y[0]),(580,28)))
        control.setButtonType_(a.NSButtonTypeSwitch)
        control.setTitle_(title)
        value = saved.get(name,default)
        if name=="mac_layout":
            value = saved.get("keymap_preset","option_cmd") in ("option_cmd","mac_layout")
        elif name=="local_smooth_scroll":
            value = config.get(name,False)
        control.setState_(a.NSControlStateValueOn if value else a.NSControlStateValueOff)
        document.addSubview_(control)
        delegate.fields[name] = ("bool",control)
        y[0] += 36

    def number(name,title,default):
        label(document,title,((20,y[0]),(430,26)),13)
        field = a.NSTextField.alloc().initWithFrame_(((460,y[0]),(140,27)))
        field.setStringValue_(str(saved.get(name,default)))
        document.addSubview_(field)
        delegate.fields[name] = ("number",field)
        y[0] += 36

    section("WHEN YOUR PC CONTROLS THIS MAC")
    check("mac_layout","Mac keyboard layout: Alt = Command, Windows = Option",True)
    check("smooth_scroll","Smooth scrolling from the PC mouse",True)
    number("scroll_gain_y","Vertical scroll sensitivity",1)
    number("scroll_gain_x","Horizontal scroll sensitivity",1)
    check("scroll_invert_y","Reverse vertical scrolling")
    check("scroll_invert_x","Reverse horizontal scrolling")
    check("windows_home_end","Home / End move to the beginning / end of a line",True)
    check("windows_word_nav","Use Ctrl + arrows / Backspace / Delete for word navigation",True)
    number("smooth_scroll_ms","Smoothing duration (10-150 ms)",60)
    number("smooth_scroll_hz","Smoothing update rate (30-240 Hz)",120)
    y[0] += 10
    section("THIS MAC'S OWN MOUSE")
    check("local_smooth_scroll","Also smooth this Mac's own mouse wheel")
    delegate.local_status = label(document,
        "Local wheel smoothing leaves trackpad scrolling unchanged.",
        ((20,y[0]),(560,24)),11,muted)
    y[0] += 40
    apply_button = a.NSButton.alloc().initWithFrame_(((20,y[0]),(200,34)))
    apply_button.setTitle_("Apply preferences")
    apply_button.setBezelStyle_(a.NSBezelStyleRounded)
    apply_button.setTarget_(delegate)
    apply_button.setAction_("applyTweaks:")
    document.addSubview_(apply_button)
    y[0] += 50
    document.setFrame_(((0,0),(640,max(640,y[0]))))

    # --- Mouse rate tab ---------------------------------------------------
    mouse = pages["mouse"]
    mtop = mouse.bounds().size.height
    label(mouse,"MOUSE POLLING RATE",((20,mtop-44),(560,20)),11,muted)
    delegate.rate_value = label(mouse,"0 Hz",((20,mtop-140),(560,80)),52)
    delegate.rate_detail = label(mouse,"Peak 0 Hz · 0 events",
        ((20,mtop-180),(560,28)),14,muted)
    delegate.rate_status = label(mouse,"Select this tab to start measuring",
        ((20,mtop-230),(560,44)),13)
    button(mouse,"Reset",((20,mtop-290),(120,32)),"resetRate:")
    label(mouse,"Counts real pointer-move events at the macOS HID event tap over a\n"
          "rolling one-second window — works with any mouse: built-in trackpad,\n"
          "USB or Bluetooth. A mouse rated 125 Hz shows ~125; 1000 Hz shows ~1000.\n"
          "The tap starts when this tab is open and stops when you leave it.\n"
          "Requires Input Monitoring / Accessibility permission.",
          ((20,40),(560,110)),13,muted)

    delegate._apply_role()
    timer = f.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        .2,delegate,"poll:",None,True)
    window.center()
    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    try:
        update_local_scroll()
        if settings.get("role","client") != "host":
            client.start()
        app.run()
    finally:
        client.stop()
        host.stop()
        rate_monitor.stop()
        if local_tap:
            local_tap.stop()
        if client.worker:
            client.worker.join(5)
        timer.invalidate()
