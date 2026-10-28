"""Native macOS client panel. Importing never opens a window or posts input."""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
import queue
import re
import sys
import threading

from .config import save_json


class ClientController:
    """UI-independent worker lifecycle; cancellation is owned by the session."""
    def __init__(self, config_path: Path, runner=None):
        self.config_path = Path(config_path)
        self.runner = runner
        self.messages: queue.Queue[str] = queue.Queue(maxsize=128)
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
                runner(config, stop_event=self.stopped, status=self.post_status)
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



def run_client_gui(config_path: Path):
    if sys.platform != "darwin":
        raise OSError("The native client panel requires macOS")
    import AppKit as a
    import Foundation as f
    import objc
    from .config import state_dir
    logging.basicConfig(filename=str(state_dir() / "mac-client-status.log"), level=logging.INFO,
                        format="%(asctime)s %(message)s")

    controller = ClientController(config_path)
    config = controller.configuration()
    app = a.NSApplication.sharedApplication()
    app.setActivationPolicy_(a.NSApplicationActivationPolicyRegular)
    local_tap = None

    def update_local_scroll():
        nonlocal local_tap
        if local_tap:
            local_tap.stop()
            local_tap = None
        saved = controller.configuration()
        if saved.get("local_smooth_scroll",False):
            from .scroll import MacLocalScrollTap
            tweaks = saved.get("tweaks",{})
            local_tap = MacLocalScrollTap(rate_hz=int(tweaks.get("smooth_scroll_hz",120)),
                duration_ms=float(tweaks.get("smooth_scroll_ms",60)),
                status=lambda message: controller.post_status("Local scrolling: "+message))
            local_tap.start()

    class PrecisionKVMClientDelegate(f.NSObject,  # type: ignore[call-arg]
            protocols=[objc.protocolNamed("NSApplicationDelegate"), objc.protocolNamed("NSWindowDelegate")]):
        @objc.signature(b"v@:@")
        def toggle_(self, sender):
            if controller.active:
                controller.stop()
            else:
                try:
                    controller.save_host(str(self.host.stringValue()))
                    controller.start()
                except Exception as exc:
                    controller.post_status(str(exc))

        @objc.signature(b"v@:@")
        def importPairing_(self,sender):
            if controller.active:
                controller.post_status("Disconnect before importing another pairing file")
                return
            panel = a.NSOpenPanel.openPanel()
            panel.setTitle_("Import pairing from your Windows PC")
            panel.setAllowedFileTypes_(["json"])
            panel.setAllowsMultipleSelection_(False)
            panel.setCanChooseDirectories_(False)
            if panel.runModal()==a.NSModalResponseOK:
                try:
                    controller.import_pairing(str(panel.URL().path()))
                    self.host.setStringValue_(str(controller.configuration()["host"]))
                except Exception as exc:
                    controller.post_status(str(exc))

        @objc.signature(b"v@:@")
        def applyTweaks_(self,sender):
            try:
                values = {}
                for name,(kind,field) in self.fields.items():
                    values[name] = bool(field.state()) if kind=="bool" else str(field.stringValue())
                values["keymap_preset"] = "option_cmd" if values.pop("mac_layout") else "native"
                controller.save_tweaks(values)
                self.pending_restart = controller.active
                controller.stop()
                update_local_scroll()
                controller.post_status("Mac preferences saved - apply when you next enter this Mac")
            except Exception as exc:
                controller.post_status(str(exc))

        @objc.signature(b"v@:@")
        def poll_(self, timer):
            while True:
                try:
                    message = controller.messages.get_nowait()
                except queue.Empty:
                    break
                if message.startswith("Clipboard:"):
                    self.clipboard.setStringValue_(message[:800])
                elif message.startswith("Local scrolling:"):
                    self.local_status.setStringValue_(message[:800])
                else:
                    self.status.setStringValue_(message[:800])
                    color = (a.NSColor.systemGreenColor() if message.startswith("Connected")
                             else a.NSColor.systemOrangeColor() if "unavailable" in message.lower()
                             else a.NSColor.labelColor())
                    self.status.setTextColor_(color)
            active = controller.active
            self.button.setTitle_("Disconnect" if active else "Connect to PC")
            self.host.setEnabled_(not active)
            self.import_button.setEnabled_(not active)
            if self.pending_restart and not active and not self.quitting:
                self.pending_restart = False
                controller.start()
            if self.quitting and not active:
                timer.invalidate()
                app.replyToApplicationShouldTerminate_(True)

        @objc.signature(b"B@:@")
        def windowShouldClose_(self, sender):
            app.terminate_(None)
            return False

        @objc.signature(b"Q@:@")
        def applicationShouldTerminate_(self, sender):
            self.pending_restart = False
            controller.stop()
            if local_tap:
                local_tap.stop()
            if controller.active:
                self.quitting = True
                return a.NSTerminateLater
            return a.NSTerminateNow

    delegate = PrecisionKVMClientDelegate.alloc().init()
    delegate.quitting = delegate.pending_restart = False
    delegate.fields = {}
    app.setDelegate_(delegate)
    style = (a.NSWindowStyleMaskTitled | a.NSWindowStyleMaskClosable
             | a.NSWindowStyleMaskMiniaturizable | a.NSWindowStyleMaskResizable)
    window = a.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((0,0),(820,760)),style,a.NSBackingStoreBuffered,False)
    window.setTitle_("PrecisionKVM - Mac client")
    window.setMinSize_((780,720))
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

    muted = a.NSColor.secondaryLabelColor()
    label(content,"PrecisionKVM",((28,685),(760,42)),30)
    label(content,"MAC CLIENT  /  Your PC keyboard, mouse and clipboard",((30,651),(760,26)),13,muted)
    tabs = a.NSTabView.alloc().initWithFrame_(((20,92),(780,538)))
    tabs.setAutoresizingMask_(a.NSViewWidthSizable | a.NSViewHeightSizable)
    content.addSubview_(tabs)
    pages = {}
    for identifier,title in (("connection","Connection"),("tweaks","Mac Tweaks")):
        item = a.NSTabViewItem.alloc().initWithIdentifier_(identifier)
        item.setLabel_(title)
        view = a.NSView.alloc().initWithFrame_(((0,0),(756,500)))
        item.setView_(view)
        tabs.addTabViewItem_(item)
        pages[identifier] = view
    connection = pages["connection"]
    label(connection,"CONNECTION",((24,438),(700,20)),11,muted)
    delegate.status = label(connection,"Import pairing to get started" if not controller.paired
                            else "Ready to connect",((24,373),(700,56)),20)
    label(connection,"Windows server",((24,321),(170,26)),14)
    delegate.host = a.NSTextField.alloc().initWithFrame_(((198,319),(510,30)))
    delegate.host.setStringValue_(str(config.get("host","")))
    delegate.host.setPlaceholderString_("Windows IP address")
    delegate.host.setAutoresizingMask_(resizes)
    connection.addSubview_(delegate.host)
    delegate.clipboard = label(connection,"Clipboard: waiting for a connection",((24,261),(700,38)),13,muted)
    label(connection,"Automatic reconnect",((24,209),(700,28)),17)
    label(connection,"The client checks for your PC every second when disconnected.\n"
          "Move across the configured edge on your PC to control this Mac.\n\n"
          "First setup: export a pairing file from the PC, then import it here.\n"
          "Pairing tokens stay private and never appear in this panel.",
          ((24,72),(700,129)),14,muted)
    tweaks = pages["tweaks"]
    saved = config.get("tweaks",{})

    def check(name,title,y,default=False):
        button = a.NSButton.alloc().initWithFrame_(((24,y),(700,28)))
        button.setButtonType_(a.NSButtonTypeSwitch)
        button.setTitle_(title)
        value = saved.get(name,default)
        if name=="mac_layout":
            value = saved.get("keymap_preset","option_cmd") in ("option_cmd","mac_layout")
        elif name=="local_smooth_scroll":
            value = config.get(name,False)
        button.setState_(a.NSControlStateValueOn if value else a.NSControlStateValueOff)
        button.setAutoresizingMask_(resizes)
        tweaks.addSubview_(button)
        delegate.fields[name] = ("bool",button)

    def number(name,title,y,default):
        label(tweaks,title,((24,y),(485,26)),13)
        field = a.NSTextField.alloc().initWithFrame_(((533,y),(170,27)))
        field.setStringValue_(str(saved.get(name,default)))
        field.setAutoresizingMask_(a.NSViewMinXMargin | a.NSViewMinYMargin)
        tweaks.addSubview_(field)
        delegate.fields[name] = ("number",field)

    check("mac_layout","Mac keyboard layout: Alt = Command, Windows = Option",449,True)
    check("smooth_scroll","Smooth scrolling from the PC mouse",414,True)
    check("local_smooth_scroll","Also smooth this Mac's own mouse wheel",379,False)
    number("scroll_gain_y","Vertical scroll sensitivity",340,1)
    number("scroll_gain_x","Horizontal scroll sensitivity",304,1)
    check("scroll_invert_y","Reverse vertical scrolling",269)
    check("scroll_invert_x","Reverse horizontal scrolling",237)
    check("windows_home_end","Home / End move to the beginning / end of a line",205,True)
    check("windows_word_nav","Use Ctrl + arrows / Backspace / Delete for word navigation",173,True)
    number("smooth_scroll_ms","Smoothing duration (10-150 ms)",134,60)
    number("smooth_scroll_hz","Smoothing update rate (30-240 Hz)",98,120)
    delegate.local_status = label(tweaks,"Local wheel smoothing leaves trackpad scrolling unchanged.",
                                  ((24,56),(700,28)),11,muted)
    apply_button = a.NSButton.alloc().initWithFrame_(((24,10),(230,34)))
    apply_button.setTitle_("Apply Mac preferences")
    apply_button.setBezelStyle_(a.NSBezelStyleRounded)
    apply_button.setTarget_(delegate)
    apply_button.setAction_("applyTweaks:")
    apply_button.setAutoresizingMask_(a.NSViewMinYMargin)
    tweaks.addSubview_(apply_button)
    delegate.button = a.NSButton.alloc().initWithFrame_(((30,28),(210,42)))
    delegate.button.setTitle_("Connect to PC")
    delegate.button.setBezelStyle_(a.NSBezelStyleRounded)
    delegate.button.setTarget_(delegate)
    delegate.button.setAction_("toggle:")
    delegate.button.setAutoresizingMask_(a.NSViewMaxYMargin)
    content.addSubview_(delegate.button)
    delegate.import_button = a.NSButton.alloc().initWithFrame_(((260,28),(230,42)))
    delegate.import_button.setTitle_("Import pairing file...")
    delegate.import_button.setBezelStyle_(a.NSBezelStyleRounded)
    delegate.import_button.setTarget_(delegate)
    delegate.import_button.setAction_("importPairing:")
    delegate.import_button.setAutoresizingMask_(a.NSViewMaxYMargin)
    content.addSubview_(delegate.import_button)
    timer = f.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        .2,delegate,"poll:",None,True)
    window.center()
    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    try:
        update_local_scroll()
        controller.start()
        app.run()
    finally:
        controller.stop()
        if local_tap:
            local_tap.stop()
        if controller.worker:
            controller.worker.join(5)
        timer.invalidate()
