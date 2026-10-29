"""Single native panel for host and client — one pywebview window, all platforms."""
from __future__ import annotations

import json
import logging
import queue
import sys
import threading

from . import geometry
from .config import (identity, load_settings, save_json, state_dir)
from .session import HostSession, run_client_forever
from . import ui_common

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None

CLIENT_PATH_KEYS = ("host", "port", "token", "fingerprint", "tls", "auto",
                    "clipboard_enabled", "clipboard_max_mb", "tweaks")


class ClientRunner:
    """Reconnecting client loop bound to this panel's status queue."""

    def __init__(self, status):
        self.status = status
        self.stopped = threading.Event()
        self.thread = None

    @property
    def active(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.active:
            return
        path = state_dir() / "client.json"
        if not path.exists():
            self.status("Import the pairing file from the host first")
            return
        config = json.loads(path.read_text(encoding="utf-8"))
        self.stopped = threading.Event()
        self.thread = threading.Thread(
            target=run_client_forever,
            args=(config, self.stopped, self.status),
            name="kvm-client", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(4)


class Api:
    """Object exposed to JS as window.pywebview.api. Methods return plain data."""

    def __init__(self, start: bool = False):
        self.settings = load_settings()
        self.messages: queue.Queue[str] = queue.Queue(maxsize=256)
        self.session: HostSession | None = None
        self.client = ClientRunner(self._status)
        self._window = None
        self._tray = None
        if start:
            self.start()

    # ----- lifecycle ---------------------------------------------------------

    def _status(self, message):
        logging.info("%s", message)
        try:
            self.messages.put_nowait(str(message))
        except queue.Full:
            try:
                self.messages.get_nowait()
                self.messages.put_nowait(str(message))
            except (queue.Empty, queue.Full):
                pass

    def poll(self):
        """JS heartbeat: latest status + running + stats."""
        message = None
        while not self.messages.empty():
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
        running = bool((self.session and self.session.thread
                        and self.session.thread.is_alive()) or self.client.active)
        return {"status": message or "",
                "running": running,
                "stats": ui_common.stats_line(self.session),
                "role": self.settings.get("role", "host")}

    def start(self):
        if self.settings.get("role", "host") == "client":
            self.client.start()
            return {"ok": True}
        if self.session and self.session.thread and self.session.thread.is_alive():
            return {"ok": False, "error": "Already running"}
        self.session = ui_common.start_host(self.settings, self._status)
        return {"ok": True}

    def stop(self):
        self.client.stop()
        if self.session:
            self.session.stop()
            self.session = None
        return {"ok": True}

    def connect_peer(self):
        if self.session:
            threading.Thread(target=self.session.connect_mac, daemon=True).start()
        return {"ok": True}

    # ----- settings ----------------------------------------------------------

    def state(self):
        _, _, _, fingerprint = identity()
        return {"settings": self.settings,
                "platform": sys.platform,
                "fingerprint": fingerprint,
                "state_dir": str(state_dir())}

    def save(self, patch):
        try:
            self.settings = ui_common.persist(ui_common.collect(self.settings, patch))
            if self.session and self.session.capture:
                self.session.capture.peers = {
                    name: (r["x"], r["y"], r["w"], r["h"])
                    for name, r in (self.settings.get("peers") or {}).items()}
            return {"ok": True, "settings": self.settings}
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}

    def set_role(self, role):
        if role not in ("host", "client"):
            return {"ok": False, "error": "role must be host or client"}
        was_running = self.poll()["running"]
        self.stop()
        self.save({"role": role})
        if was_running:
            self.start()
        return {"ok": True}

    # ----- layout ------------------------------------------------------------

    def layout(self):
        mons, peers = ui_common.load_layout(self.settings)
        return {"mons": [list(r) for r in mons],
                "peers": {name: list(r) for name, r in peers.items()}}

    def drop_peer(self, name, cx, cy):
        """JS sends the dropped centre point; we snap and persist."""
        mons, peers = ui_common.load_layout(self.settings)
        if name not in peers:
            return {"ok": False}
        rect = peers[name]
        x, y, w, h = rect
        peers[name] = geometry.snap_side((cx - w / 2, cy - h / 2, w, h), mons)
        self.settings = ui_common.save_peers(dict(self.settings), peers)
        if self.session and self.session.capture:
            self.session.capture.peers = dict(peers)
        return {"ok": True,
                "peers": {n: list(r) for n, r in peers.items()}}

    # ----- pairing / client --------------------------------------------------

    def export_pairing(self):
        if not self._window:
            return {"ok": False, "error": "Window not ready"}
        result = self._window.create_file_dialog(
            __import__("webview").SAVE_DIALOG, save_filename="PrecisionKVM-pairing.json")
        if not result:
            return {"ok": False}
        from pathlib import Path
        ui_common.export_pairing(self.settings, Path(result if isinstance(result, str) else result[0]))
        return {"ok": True}

    def client_state(self):
        path = state_dir() / "client.json"
        config = {}
        if path.exists():
            try:
                config = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        paired = bool(config.get("host") and config.get("token")
                      and len(config.get("fingerprint", "")) == 64)
        return {"paired": paired,
                "host": config.get("host", ""),
                "port": config.get("port", 24819),
                "auto": config.get("auto", True),
                "running": self.client.active}

    def import_pairing(self, text):
        try:
            data = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return {"ok": False, "error": "Not valid pairing JSON"}
        config = {key: data[key] for key in CLIENT_PATH_KEYS if key in data}
        if not (config.get("host") and config.get("token") and config.get("fingerprint")):
            return {"ok": False, "error": "Pairing file needs host, token and fingerprint"}
        path = state_dir() / "client.json"
        existing = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        existing.update(config)
        save_json(path, existing)
        return {"ok": True}

    def pick_pairing_file(self):
        if not self._window:
            return {"ok": False}
        result = self._window.create_file_dialog(__import__("webview").OPEN_DIALOG)
        if not result:
            return {"ok": False}
        try:
            from pathlib import Path
            text = Path(result[0]).read_text(encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return self.import_pairing(text)

    # ----- logs / window ------------------------------------------------------

    def logs(self):
        try:
            lines = (state_dir() / "host.log").read_text(
                encoding="utf-8", errors="replace").splitlines()[-400:]
        except OSError:
            lines = []
        return "\n".join(lines)

    def open_log_folder(self):
        import subprocess
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", str(state_dir())])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(state_dir())])
            else:
                subprocess.Popen(["xdg-open", str(state_dir())])
        except OSError:
            pass
        return {"ok": True}

    def hide_to_tray(self):
        if pystray is None or self._window is None:
            if self._window:
                self._window.destroy()
            return
        self._window.hide()
        if self._tray:
            return
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill="#141d2c",
                               outline="#3fd3bd", width=3)
        draw.rectangle((14, 18, 38, 34), fill="#2b4a75")
        draw.rectangle((40, 22, 52, 30), fill="#1f7a6c")
        draw.line((28, 40, 28, 50), fill="#3fd3bd", width=3)
        menu = pystray.Menu(
            pystray.MenuItem("Open", lambda *_: self._window.show(), default=True),
            pystray.MenuItem("Quit", lambda *_: self._quit()))
        self._tray = pystray.Icon("PrecisionKVM", image, "PrecisionKVM", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _quit(self):
        self.stop()
        if self._tray:
            self._tray.stop()
            self._tray = None
        if self._window:
            self._window.destroy()


HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>PrecisionKVM</title>
<style>
:root{--bg:#0c1320;--rail:#0a101b;--card:#141d2c;--card2:#182338;--edge:#22314a;
--ink:#e8eef7;--muted:#8fa3bd;--accent:#3fd3bd;--blue:#2b4a75;--green:#1f7a6c;
--bad:#e5646e;--good:#3fd37f;font:13px/1.45 "Segoe UI",system-ui,sans-serif;color-scheme:dark}
*{box-sizing:border-box;margin:0}body{background:var(--bg);color:var(--ink);font:inherit;
display:flex;height:100vh;overflow:hidden;user-select:none}
button,input,select,textarea{font:inherit;color:inherit}
nav{width:196px;background:var(--rail);border-right:1px solid var(--edge);display:flex;
flex-direction:column;padding:14px 10px;gap:2px;flex:none}
.brand{padding:8px 10px 18px;font-weight:700;font-size:15px;letter-spacing:.2px}
.brand span{color:var(--accent)}
.navbtn{display:flex;align-items:center;gap:9px;padding:9px 10px;border-radius:9px;
border:0;background:none;color:var(--muted);cursor:pointer;text-align:left;width:100%;
transition:background .12s}
.navbtn:hover{background:#101a2c;color:var(--ink)}
.navbtn.on{background:var(--card);color:var(--ink);box-shadow:inset 0 0 0 1px var(--edge)}
.navbtn .dot{width:6px;height:6px;border-radius:50%;background:transparent;transition:background .15s}
.navbtn.on .dot{background:var(--accent)}
nav .foot{margin-top:auto;padding:10px;color:var(--muted);font-size:11px}
main{flex:1;display:flex;flex-direction:column;min-width:0}
header{display:flex;align-items:center;gap:14px;padding:16px 22px 12px}
header h1{font-size:19px;font-weight:650}
.status{margin-left:auto;display:flex;align-items:center;gap:8px;color:var(--muted);
font-size:12.5px;max-width:46vw;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.status .dot{width:9px;height:9px;border-radius:50%;background:var(--muted);flex:none;transition:background .2s}
.status.good .dot{background:var(--good);box-shadow:0 0 8px #3fd37f88}
.status.bad .dot{background:var(--bad)}
.content{flex:1;overflow-y:auto;padding:6px 22px 22px}
.page{display:none;flex-direction:column;gap:14px;max-width:860px;animation:in .18s ease-out}
.page.on{display:flex}
@keyframes in{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){.page{animation:none}}
.card{background:var(--card);border:1px solid var(--edge);border-radius:14px;padding:18px}
.card h2{font-size:13px;font-weight:650;color:var(--muted);text-transform:uppercase;
letter-spacing:.6px;margin-bottom:14px}
.row{display:flex;align-items:center;justify-content:space-between;gap:14px;
padding:9px 0;border-bottom:1px solid #ffffff08}
.row:last-child{border-bottom:0}
.row .lbl{color:var(--ink)}.row .hint{font-size:11.5px;color:var(--muted);margin-top:2px}
.btn{background:var(--card2);border:1px solid var(--edge);border-radius:9px;
padding:8px 16px;cursor:pointer;transition:transform .08s,background .12s}
.btn:hover{background:#1e2c46}
.btn:active{transform:scale(.97)}
.btn.primary{background:var(--accent);color:#06251f;border-color:transparent;font-weight:650}
.btn.primary:hover{background:#45e0ca}
.btn.danger{color:var(--bad)}
.actions{display:flex;gap:10px;flex-wrap:wrap}
input[type=text],input[type=number],select,textarea{background:var(--card2);
border:1px solid var(--edge);border-radius:8px;padding:7px 10px;min-width:150px}
input:focus,select:focus,textarea:focus{outline:2px solid #3fd3bd55;outline-offset:1px;border-color:var(--accent)}
input[type=checkbox]{appearance:none;width:36px;height:20px;border-radius:10px;
background:var(--card2);border:1px solid var(--edge);position:relative;cursor:pointer;
transition:background .15s;flex:none}
input[type=checkbox]:checked{background:var(--accent);border-color:transparent}
input[type=checkbox]::after{content:"";position:absolute;top:2px;left:2px;width:14px;
height:14px;border-radius:50%;background:var(--ink);transition:left .15s}
input[type=checkbox]:checked::after{left:18px}
input[type=checkbox]:focus-visible{outline:2px solid #3fd3bd88;outline-offset:2px}
.seg{display:flex;background:var(--card2);border:1px solid var(--edge);border-radius:10px;padding:3px}
.seg button{border:0;background:none;color:var(--muted);padding:7px 18px;border-radius:8px;
cursor:pointer;transition:background .12s,color .12s}
.seg button.on{background:var(--accent);color:#06251f;font-weight:650}
.seg button:active{transform:scale(.96)}
#stage{position:relative;background:var(--card2);border:1px solid var(--edge);
border-radius:12px;height:380px;overflow:hidden}
.mon{position:absolute;background:var(--blue);border:1.5px solid #4d6a8c;border-radius:8px;
display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;letter-spacing:.4px}
.peer{position:absolute;background:var(--green);border:1.5px solid var(--accent);border-radius:8px;
display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;
cursor:grab;touch-action:none;z-index:2;box-shadow:0 2px 12px #0007}
.peer.grab{cursor:grabbing;box-shadow:0 6px 22px #000a}
.peer.snap{transition:left .26s cubic-bezier(.16,1,.3,1),top .26s cubic-bezier(.16,1,.3,1)}
.glow{position:absolute;background:var(--accent);opacity:.9;border-radius:2px;z-index:1}
.note{color:var(--muted);font-size:12px}
.log{white-space:pre-wrap;font:11.5px/1.5 "Cascadia Mono",Consolas,monospace;color:var(--muted);
background:var(--card2);border:1px solid var(--edge);border-radius:10px;padding:12px;
max-height:480px;overflow-y:auto;user-select:text}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.kv .item{background:var(--card2);border:1px solid var(--edge);border-radius:10px;padding:12px}
.kv .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
.kv .v{font-size:15px;font-weight:650;margin-top:3px;user-select:text}
textarea{width:100%;min-height:90px;user-select:text}
.err{color:var(--bad);font-size:12px;min-height:16px}
.maprow{display:flex;gap:8px;align-items:center;padding:5px 0}
.maprow code{color:var(--accent);user-select:text}
.mono{font-family:"Cascadia Mono",Consolas,monospace;font-size:11.5px;user-select:text}
</style></head><body>
<nav>
  <div class="brand">Precision<span>KVM</span></div>
  <button class="navbtn on" data-p="overview"><span class="dot"></span>Overview</button>
  <button class="navbtn" data-p="layout"><span class="dot"></span>Layout</button>
  <button class="navbtn" data-p="sharing"><span class="dot"></span>Sharing</button>
  <button class="navbtn" data-p="keys"><span class="dot"></span>Keys</button>
  <button class="navbtn" data-p="network"><span class="dot"></span>Network</button>
  <button class="navbtn" data-p="logs"><span class="dot"></span>Logs</button>
  <div class="foot" id="ver"></div>
</nav>
<main>
<header>
  <h1 id="title">Overview</h1>
  <div class="status" id="status"><span class="dot"></span><span id="stext">Idle</span></div>
</header>
<div class="content">
<section class="page on" id="p-overview">
  <div class="card"><h2>This device</h2>
    <div class="row"><div><div class="lbl">Role</div>
      <div class="hint">Host captures this machine's input · Client receives a remote host's input</div></div>
      <div class="seg" id="role"><button data-r="host">Host</button><button data-r="client">Client</button></div></div>
    <div class="row"><div><div class="lbl">Sharing</div>
      <div class="hint" id="runhint"></div></div>
      <div class="actions"><button class="btn primary" id="startbtn">Start</button>
      <button class="btn" id="stopbtn">Stop</button></div></div>
    <div class="row" id="connectrow"><div><div class="lbl">Peer</div>
      <div class="hint">Ask the paired peer to connect over SSH</div></div>
      <button class="btn" id="connectbtn">Connect peer</button></div>
  </div>
  <div class="card"><h2>Session</h2><div class="kv" id="stats"></div></div>
  <div class="card" id="clientcard" style="display:none"><h2>Pairing</h2>
    <div class="row"><div><div class="lbl">Pairing file</div>
      <div class="hint" id="pairhint">Load the JSON exported by the host</div></div>
      <div class="actions"><button class="btn" id="pickbtn">Choose file…</button></div></div>
    <textarea id="pairjson" placeholder="…or paste pairing JSON here"></textarea>
    <div class="actions"><button class="btn" id="importbtn">Import pasted JSON</button></div>
    <div class="err" id="pairerr"></div>
  </div>
</section>
<section class="page" id="p-layout">
  <div class="card"><h2>Monitors</h2>
    <div id="stage"></div>
    <p class="note" style="margin-top:10px">Drag the peer display left or right of THIS PC —
      it snaps flush to that edge. The glowing segment is where the cursor crosses.</p></div>
</section>
<section class="page" id="p-sharing"><div class="card" id="sharingform"></div></section>
<section class="page" id="p-keys">
  <div class="card" id="keysform"></div>
  <div class="card"><h2>Custom key mappings</h2>
    <div id="maplist"></div>
    <div class="actions" style="margin-top:8px">
      <input type="text" id="mapfrom" placeholder="Windows key (e.g. CapsLock)">
      <input type="text" id="mapto" placeholder="Mac key (e.g. Escape)">
      <button class="btn" id="mapadd">Add</button></div>
    <div class="err" id="maperr"></div></div>
</section>
<section class="page" id="p-network"><div class="card" id="netform"></div>
  <div class="card"><h2>Identity</h2>
    <div class="row"><div><div class="lbl">Certificate fingerprint</div>
      <div class="hint">Share privately; the peer pins it</div></div>
      <div class="mono" id="fp"></div></div>
    <div class="actions" style="margin-top:12px"><button class="btn" id="exportbtn">Export pairing file…</button></div>
  </div></section>
<section class="page" id="p-logs">
  <div class="card"><div class="actions" style="margin-bottom:12px">
    <button class="btn" id="logrefresh">Refresh</button>
    <button class="btn" id="logopen">Open log folder</button></div>
    <div class="log" id="logtext">No log lines yet</div></div>
</section>
</div></main>
<script>
const api = () => window.pywebview.api;
let S = {settings:{},running:false};
const $ = s => document.querySelector(s);

const FIELDS = {
sharing: [
 ["mouse_hz","Mouse update limit (Hz)","select",["120","144","240","500","1000"]],
 ["gain","Pointer gain","text"],["scroll_gain_x","Horizontal scroll gain","text"],
 ["scroll_gain_y","Vertical scroll gain","text"],
 ["scroll_invert_x","Invert horizontal scroll","bool"],
 ["scroll_invert_y","Invert vertical scroll","bool"],
 ["scroll_mode","Scroll mode","select",{"pixel":"Smooth pixels","line":"Traditional lines"}],
 ["smooth_scroll","Smooth scrolling","bool"],
 ["smooth_scroll_ms","Smooth scroll duration (ms)","text"],
 ["smooth_scroll_hz","Smooth scroll rate (Hz)","text"],
 ["clipboard_enabled","Share clipboard","bool"],
 ["clipboard_max_mb","Clipboard size limit (MB)","text"]],
keys: [
 ["keymap_preset","Modifier layout","select",
  {"option_cmd":"Mac layout · Alt = Command","native":"Windows modifiers",
   "ctrl_cmd":"Swap Ctrl and Command","mac_layout":"Mac layout · Control unchanged"}],
 ["mac_mode","Mac-style shortcuts on this PC","bool"],
 ["gesture_bridge","Bridge touchpad gestures","bool"],
 ["native_touchpad","Native touchpad gestures","bool"],
 ["windows_home_end","Windows Home/End line navigation","bool"],
 ["windows_word_nav","Ctrl+Arrow word navigation","bool"],
 ["lock_sync","Lock the peer when this PC locks","bool"],
 ["emergency_escape_seconds","Hold Esc to stop (seconds)","text"],
 ["mouse_mode","Mouse input mode","select",{"raw":"Raw relative · recommended","system":"Windows pointer acceleration"}]],
network: [
 ["peer","Peer address","text"],["ssh_target","Peer SSH target (user@host)","text"],
 ["port","Port","text"],["tls","Encrypt with TLS","bool"],
 ["discovery","Advertise on the network (mDNS)","bool"]],
};
const TITLES = {overview:"Overview",layout:"Monitor layout",sharing:"Sharing",
keys:"Keyboard & shortcuts",network:"Network & security",logs:"Activity log"};

function nav(id){
 document.querySelectorAll(".navbtn").forEach(b=>b.classList.toggle("on",b.dataset.p===id));
 document.querySelectorAll(".page").forEach(p=>p.classList.toggle("on",p.id==="p-"+id));
 $("#title").textContent=TITLES[id];
 if(id==="layout")renderLayout();if(id==="logs")refreshLogs();
}
document.querySelectorAll(".navbtn").forEach(b=>b.onclick=()=>nav(b.dataset.p));

function field(def){
 const [key,label,type,opts]=def,v=S.settings[key];
 const hint="";
 let ctl;
 if(type==="bool")ctl=`<input type="checkbox" data-k="${key}"${v?" checked":""}>`;
 else if(type==="select"){
   const list=Array.isArray(opts)?opts.map(o=>[o,o]):Object.entries(opts);
   ctl=`<select data-k="${key}">`+list.map(([val,lab])=>
     `<option value="${val}"${String(v)===val?" selected":""}>${lab}</option>`).join("")+"</select>";
 }else ctl=`<input type="text" data-k="${key}" value="${v??""}">`;
 return `<div class="row"><div class="lbl">${label}</div>${ctl}</div>`;
}
function renderForms(){
 for(const [page,defs] of Object.entries(FIELDS)){
   const el=document.getElementById(page+"form");
   if(el)el.innerHTML="<h2>"+TITLES[page]+"</h2>"+defs.map(field).join("");
 }
}
async function saveFromUI(){
 const patch={};
 document.querySelectorAll("[data-k]").forEach(el=>{
   patch[el.dataset.k]=el.type==="checkbox"?el.checked:el.value;});
 const r=await api().save(patch);
 if(r.ok)S.settings=r.settings;
 else toast(r.error||"Invalid setting");
}
document.addEventListener("change",e=>{
 if(e.target.matches("[data-k]"))saveFromUI();});

let toastTimer;
function toast(msg){
 const el=$("#stext");el.textContent=msg;
 clearTimeout(toastTimer);toastTimer=setTimeout(()=>poll(),4000);
}

async function poll(){
 const p=await api().poll();
 const st=$("#status");
 st.classList.toggle("good",p.running);
 if(p.status){$("#stext").textContent=p.status;
   st.classList.toggle("bad",/error|rejected|unavailable|stopped/i.test(p.status));}
 $("#runhint").textContent=p.running?"Sharing is running":"Sharing is stopped";
 $("#startbtn").textContent=p.running?"Restart":"Start";
 if(p.stats)$("#stats").innerHTML=`<div class="item" style="grid-column:1/-1"><div class="k">Metrics</div><div class="v">${p.stats}</div></div>`;
 syncRole(p.role);
}
function syncRole(role){
 document.querySelectorAll("#role button").forEach(b=>b.classList.toggle("on",b.dataset.r===role));
 $("#connectrow").style.display=role==="host"?"":"none";
 $("#clientcard").style.display=role==="client"?"":"none";
 if(role==="client")clientState();
}
document.querySelectorAll("#role button").forEach(b=>b.onclick=async()=>{
 const r=await api().set_role(b.dataset.r);if(!r.ok)toast(r.error);poll();});
$("#startbtn").onclick=async()=>{const r=await api().start();if(!r.ok)toast(r.error);poll();};
$("#stopbtn").onclick=async()=>{await api().stop();poll();};
$("#connectbtn").onclick=()=>api().connect_peer();

// ----- monitor layout ------------------------------------------------------
let lay={mons:[],peers:{}},scale=1,ox=0,oy=0;
async function renderLayout(){
 lay=await api().layout();
 const stage=$("#stage"),w=stage.clientWidth,h=stage.clientHeight;
 const rects=lay.mons.concat(Object.values(lay.peers));
 const bx=Math.min(...rects.map(r=>r[0])),by=Math.min(...rects.map(r=>r[1]));
 const bw=Math.max(...rects.map(r=>r[0]+r[2]))-bx,bh=Math.max(...rects.map(r=>r[1]+r[3]))-by;
 const m=40;scale=Math.min((w-2*m)/bw,(h-2*m)/bh);
 ox=m+(w-2*m-bw*scale)/2-bx*scale;oy=m+(h-2*m-bh*scale)/2-by*scale;
 stage.innerHTML="";
 lay.mons.forEach(r=>stage.appendChild(el("mon",r,"THIS PC")));
 for(const [name,r] of Object.entries(lay.peers)){
   const e=el("peer",r,name.toUpperCase());stage.appendChild(e);draggable(e,name);}
 drawGlow(stage);
}
function el(cls,r,label){
 const e=document.createElement("div");e.className=cls;
 e.style.cssText=`left:${r[0]*scale+ox}px;top:${r[1]*scale+oy}px;width:${r[2]*scale}px;height:${r[3]*scale}px`;
 e.textContent=label;return e;
}
function draggable(e,name){
 let gx=0,gy=0;
 e.addEventListener("pointerdown",ev=>{
   e.setPointerCapture(ev.pointerId);e.classList.remove("snap");e.classList.add("grab");
   gx=ev.clientX-parseFloat(e.style.left);gy=ev.clientY-parseFloat(e.style.top);});
 e.addEventListener("pointermove",ev=>{
   if(!e.classList.contains("grab"))return;
   e.style.left=(ev.clientX-gx)+"px";e.style.top=(ev.clientY-gy)+"px";});
 e.addEventListener("pointerup",async ev=>{
   e.classList.remove("grab");
   const cx=((ev.clientX-gx)+e.offsetWidth/2-ox)/scale;
   const cy=((ev.clientY-gy)+e.offsetHeight/2-oy)/scale;
   const r=await api().drop_peer(name,cx,cy);
   if(r.ok){
     lay.peers=r.peers;
     const nr=r.peers[name];
     e.classList.add("snap");
     e.style.left=(nr[0]*scale+ox)+"px";
     e.style.top=(nr[1]*scale+oy)+"px";
     setTimeout(renderLayout,280);}});
}
function drawGlow(stage){
 for(const r of Object.values(lay.peers))for(const m of lay.mons){
   const lx=Math.max(r[0],m[0]),hx=Math.min(r[0]+r[2],m[0]+m[2]);
   const ly=Math.max(r[1],m[1]),hy=Math.min(r[1]+r[3],m[1]+m[3]);
   if(hx>lx)for(const ey of [m[1],m[1]+m[3]])
     if(Math.abs(r[1]+r[3]-ey)<9||Math.abs(r[1]-ey)<9)seg(stage,lx,ey,hx-lx,0);
   if(hy>ly)for(const ex of [m[0],m[0]+m[2]])
     if(Math.abs(r[0]+r[2]-ex)<9||Math.abs(r[0]-ex)<9)seg(stage,ex,ly,0,hy-ly);
 }
}
function seg(stage,x,y,w,h){
 const s=document.createElement("div");s.className="glow";
 s.style.cssText=`left:${x*scale+ox-(w?0:1.5)}px;top:${y*scale+oy-(h?0:1.5)}px;`+
   `width:${Math.max(w*scale,3)}px;height:${Math.max(h*scale,3)}px`;
 stage.appendChild(s);
}
window.addEventListener("resize",()=>{if($("#p-layout").classList.contains("on"))renderLayout();});

// ----- keymap ----------------------------------------------------------------
async function renderKeymap(){
 const list=$("#maplist"),km=S.settings.keymap||{};
 list.innerHTML=Object.entries(km).map(([s,t])=>
   `<div class="maprow"><code>${s} → ${t}</code>
    <button class="btn danger" data-m="${s}">Remove</button></div>`).join("")||
   `<div class="note">No custom mappings</div>`;
 list.querySelectorAll("[data-m]").forEach(b=>b.onclick=async()=>{
   const r=await api().save({keymap:Object.fromEntries(
     Object.entries(S.settings.keymap).filter(([k])=>k!==b.dataset.m))});
   if(r.ok){S.settings=r.settings;renderKeymap();}});
}
$("#mapadd").onclick=async()=>{
 const s=$("#mapfrom").value.trim(),t=$("#mapto").value.trim();
 if(!s||!t){$("#maperr").textContent="Both keys are required";return;}
 const r=await api().save({keymap:{...S.settings.keymap,[s]:t}});
 if(r.ok){S.settings=r.settings;$("#mapfrom").value=$("#mapto").value="";$("#maperr").textContent="";renderKeymap();}
 else $("#maperr").textContent=r.error||"Invalid key";};

// ----- client pairing ---------------------------------------------------------
async function clientState(){
 const c=await api().client_state();
 $("#pairhint").textContent=c.paired?`Paired with ${c.host}:${c.port}`:"Load the JSON exported by the host";
}
$("#importbtn").onclick=async()=>{
 const r=await api().import_pairing($("#pairjson").value);
 $("#pairerr").textContent=r.ok?"":(r.error||"Import failed");
 if(r.ok){$("#pairjson").value="";clientState();}};
$("#pickbtn").onclick=async()=>{
 const r=await api().pick_pairing_file();
 if(!r.ok)$("#pairerr").textContent=r.error||"";
 else clientState();};

// ----- misc --------------------------------------------------------------------
$("#exportbtn").onclick=async()=>{const r=await api().export_pairing();
 toast(r.ok?"Pairing file written":(r.error||"Export cancelled"));};
async function refreshLogs(){
 const t=await api().logs();$("#logtext").textContent=t||"No log lines yet";
 $("#logtext").scrollTop=$("#logtext").scrollHeight;}
$("#logrefresh").onclick=refreshLogs;
$("#logopen").onclick=()=>api().open_log_folder();

async function boot(){
 const st=await api().state();S.settings=st.settings;
 $("#fp").textContent=st.fingerprint.slice(0,24)+"…";
 $("#fp").title=st.fingerprint;
 renderForms();syncRole(S.settings.role||"host");renderKeymap();poll();
 setInterval(poll,900);
}
window.addEventListener("pywebviewready",boot);
</script></body></html>"""


def run_ui(start: bool = False, ui: str | None = None):
    """Native WebView2/WKWebView window; identical surface on every platform."""
    import webview
    api = Api(start)
    window = webview.create_window(
        "PrecisionKVM", html=HTML, js_api=api,
        width=1080, height=740, min_size=(920, 620),
        background_color="#0c1320", text_select=False)
    api._window = window
    if pystray is not None:
        window.events.closing += lambda: (api.hide_to_tray(), True)[1]
    else:
        window.events.closed += api.stop
    try:
        webview.start(debug=False)
    finally:
        api.stop()
        if api._tray:
            api._tray.stop()
