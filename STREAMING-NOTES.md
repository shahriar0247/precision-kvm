# Low-Latency Screen & Webcam Streaming — Field Notes

Everything learned building a Parsec-style video plane between a Mac
(encoder) and a Linux viewer (decoder) over a direct gigabit Ethernet
link, plus what to do differently for a new Rust-backed application
that needs screenshare *and* webcam streaming.

Latency target worth aiming for: **glass-to-glass ≈ 10–30 ms on LAN**.
Parsec claims ~7 ms network time on LAN; the rest of the budget is
capture (~4–8 ms), encode (~4–8 ms), network (~1 ms wired), decode
(~4–8 ms), present (0–16 ms depending on render cadence). An ffmpeg
CLI pipeline measured here lands roughly **30–80 ms** end-to-end —
good enough for a "view/control" feed, not quite esports.

---

## 1. Architecture that actually works

Five stages, each in its own thread/process, never blocking each other:

```
capture → encode → UDP send → UDP recv → decode → present
```

Rules that mattered:

- **TCP for control only.** Config, auth, keyframe requests, stats.
  Never video. TCP head-of-line blocking turns a 1% loss into a full
  pipeline stall; UDP drops a frame and moves on — that *is* the
  latency win.
- **Newest-frame-only at every handoff.** Every queue in the path is a
  1-slot mailbox, not a FIFO: the decoder replaces the "latest frame"
  slot, the presenter reads whatever is newest and skips the rest.
  Lag compounds forever in queues; it self-heals in mailboxes.
- **Independent rates.** Capture at 60, encode at whatever the encoder
  can, display at whatever the UI can — never force them equal.
- **One send error must not kill the stream.** ffmpeg aborts the mux on
  `ENETUNREACH`/`EAGAIN`-ish write failures — a 200 ms link flap kills
  the whole encoder and you eat a respawn cycle. Parsec just keeps
  sending. If the new app owns the socket (it should — see §6), ignore
  transient send errors and keep going.
- **Restart-on-change, not mutate.** Encoder params (fps, bitrate, res,
  codec) apply by respawning the pipeline, not hot-mutating. Simpler,
  deterministic, no half-applied state. Debounce config changes
  (~400 ms) so typing a digit doesn't respawn per keystroke.

## 2. Capture — per-OS findings

### macOS (AVFoundation / ffmpeg `-f avfoundation -i "0:"`)

- **Pixel format is mandatory**: the screen device offers
  `uyvy422/yuyv422/nv12` only. Without `-pixel_format nv12` the demuxer
  fails to open entirely.
- `-framerate N` on the input is best-effort — ffmpeg logs
  "Configuration of video device failed, falling back to default" and
  still works. Harmless.
- `-capture_cursor 1` composites the pointer for free.
- **TCC (Screen Recording) is the hard gate**:
  - `CGPreflightScreenCaptureAccess()` lies *over SSH* — it reports the
    SSH session's context, not the GUI session's. Always check/trigger
    from the GUI-session process itself.
  - TCC attributes the grant to the **executable identity**:
    `/opt/homebrew/bin/python3` shows as "Python", ffmpeg shows as its
    own binary path, a bundled `.app` shows as its bundle name.
    Subprocesses spawned by a properly bundled app inherit the app's
    responsible-process identity — so an `.app` spawning ffmpeg works,
    but a bare python spawning ffmpeg needs *both* Python and ffmpeg
    granted.
  - Grants apply to **newly spawned** capture sessions — restart the
    encoder after granting, don't expect a live blocked capture to wake.
  - Blocked capture = process alive, 0% CPU, zero output, no error.
    That's the signature — don't debug "why no frames" before checking
    TCC.
- **Better-than-ffmpeg option**: `ScreenCaptureKit` (SCStream) gives
  zero-copy IOSurfaces at native refresh with per-window/per-display
  filters and no ffmpeg demuxer overhead — Parsec-equivalent capture.
  Via PyObjC (`ScreenCaptureKit` framework) in Python, or natively in
  Rust via `screencapturekit-rs`/`objc2-screen-capture-kit`. This is
  the single biggest capture upgrade available on macOS.

### Linux

- **`x11grab` under Wayland is a trap**: it only sees the XWayland
  root → black or nested-recursive output. Check `WAYLAND_DISPLAY`
  and *don't* fall back to it. Correct path is PipeWire via the
  `xdg-desktop-portal` ScreenCast interface (requires a consent prompt
  once). Under X11, `x11grab` is fine.
- PipeWire is also the zero-copy capture path (dmabuf surfaces →
  encoder without readback) — the real modern answer.

### Windows

- `gdigrab` works for ffmpeg but is slow/CPU-heavy. Real answer is
  **DXGI Desktop Duplication** — what Parsec uses; ~1 ms capture in
  hardware.

## 3. Encode — measured results and flags

Measured on Apple Silicon Mac, `testsrc2=1920x1080:rate=60` →
`h264_videotoolbox -realtime 1 -prio_speed 1 -allow_sw 0`:
**≈160 fps encode (2.7× realtime)** — 60 fps @1080p has huge headroom
on the encode side; encoder speed is *not* your bottleneck.

Working encoder recipe (H.264):

```
ffmpeg -framerate FPS -pixel_format nv12 -f avfoundation -i "0:"
  -c:v h264_videotoolbox -realtime 1 -prio_speed 1 -allow_sw 0
  [-vf scale=...]
  -g GOP -b:v BITRATE -maxrate BITRATE -bufsize BITRATE/2
  -f mpegts -mpegts_flags +resend_headers udp://HOST:PORT?localaddr=IP
```

- `-realtime 1 -prio_speed 1` = VideoToolbox low-latency mode (B-frames
  off implicitly). Non-realtime VT mode buffers frames — latency killer.
- **Short GOP**: `fps/2` (e.g. 30 @60fps) — joins and corruption resync
  fast. `-mpegts_flags +resend_headers` resends SPS/PPS so mid-stream
  join doesn't need a keyframe boundary. (Parsec also requests an IDR
  on packet loss — a feedback signal ffmpeg can't do; see §7.)
- **Encode at display resolution** — Parsec's real trick. Don't capture
  a 5K retina Mac and ship it to a 1080p panel. Best mode found: the
  viewer tells the sender *its* panel pixel size ("viewer" cap_res) and
  the sender scales to fit before encode:
  `scale=W:H:force_original_aspect_ratio=decrease` keeps aspect inside
  the box; `scale=-2:1080` caps height keeping aspect; plain
  `scale=iw*s:ih*s` for scale factors. Renegotiate on viewer resize
  (debounced).
- Software fallback: `libx264 -preset ultrafast -tune zerolatency`
  (tune sets bframes=0, no lookahead — required for latency).
  `libx265`/HEVC halves bitrate at a decode-latency and compat cost.
  VAAPI on Linux: `-vaapi_device /dev/dri/renderD128 -vf
  ...,format=nv12,hwupload -c:v h264_vaapi`.
- Bitrate sanity: 1080p60 H.264 clean-looking ≈ 15–25 Mbps; 40 Mbps is
  generous LAN "turbo"; 2000 kbps starves 1080p60 into smear — bitrate
  was a real reason a "60 fps" target looked bad before.
- Codec choice: H.264 everywhere = safest + fastest decode. HEVC only
  for bandwidth-constrained links; VT `hevc_videotoolbox` exists but
  decode support on the viewer side is the deciding factor.

## 4. Transport

- **MPEG-TS over UDP** — self-resyncing container, tolerates loss, no
  demuxer buffering drama. (RTP is the "proper" alternative if you
  build your own sender; TS is the ffmpeg-compatible one.)
- **macOS multihoming bug — important**: an *unbound* UDP socket on a
  Mac with multiple interfaces intermittently picks the wrong source /
  hits `ENETUNREACH` on a directly-connected subnet while TCP is fine.
  Fix: **`udp://host:port?localaddr=<client's own IP on that link>`**
  — the TCP control socket's `getsockname()` tells you which local IP
  the host is reached via; use it. This was a real failure, not
  theoretical.
- **EEE / link flaps**: `energy-efficient-ethernet` (and USB-ethernet
  adapters like r8152) cause micro link drops — UDP sees them as
  send errors instantly, TCP hides them via retries. Symptoms: encoder
  dies with "No route to host" while ssh/ping are fine. Fixes: pin link
  speed (`ifconfig en0 media 1000baseT mediaopt full-duplex` on macOS),
  disable EEE if ethtool cooperates, keep send errors non-fatal.
- Socket tuning: `fifo_size=1000000&overrun_nonfatal=1` on the recv
  URL; bigger kernel UDP buffers (`net.core.rmem_max`) if 60fps drops.
- **FEC / retransmit (the next level)**: Parsec runs a custom UDP
  protocol with NACK-triggered retransmits + FEC. ffmpeg can't do it —
  needs your own sender (trivial in Rust: packetize frames into
  datagrams, add a seq header, respond to NACKs on the TCP control
  channel, send IDR on loss-burst). Worth it for WiFi; nearly pointless
  on a wired direct link (<0.1% loss).

## 5. Decode + present

Decoder command that worked (sub-100 ms to first frame):

```
ffmpeg -fflags nobuffer -flags low_delay
  -probesize 32768 -analyzeduration 0
  -i "udp://0.0.0.0:PORT?fifo_size=1000000&overrun_nonfatal=1"
  -f image2pipe -vcodec ppm -
```

- PPM frames are trivially parseable (P6 + dims + RGB) — fine for a
  viewer UI, wasteful bandwidth-wise vs zero-copy. For the Rust app:
  feed the decoded YUV/RGB frame straight to a texture; skip PPM.
- Present loop decoupled from poll loop: a 25 ms dedicated video tick
  vs the shared 100 ms UI tick took perceived fps from ~10 to ~40.
- Render newest frame only, always; scale to panel at *display* time
  (viewer upscales — the feed always looks full-size regardless of
  encode res). Fit modes: `fill` (stretch), `width`, `contain`.
- Stats that users actually use to debug: transport (UDP/H264 vs
  JPEG/TCP), decoded resolution, decoded fps, frame age ("last frame
  N ms ago"), target (fps·codec·bitrate). Frame age is the best
  single "is it Parsec-fast" metric.

## 6. For the Rust backend — what to build vs reuse

**Keep as knowledge, not code**: the ffmpeg command strings above are
the actual deliverable — they encode everything learned about flags
and pitfalls. Porting them is copying strings.

Recommended Rust shape:

- **Control channel**: TCP/TLS or QUIC with the same config datagram
  the Python system uses (`enabled, fps, codec, kbps, cap_res,
  viewer_w/h, gop`). If you want one transport: **QUIC** (quinn) gives
  reliable control streams + unreliable datagrams in one connection —
  closest thing to Parsec's architecture in a crate.
- **Video plane**: own UDP socket, not ffmpeg's. Benefits: ignore send
  errors (survive link flaps), add seq/NACK/FEC later, embed a frame
  header (frame_id, fragment index) for partial-loss assembly.
- **Encode/decode**: `ffmpeg-next` wraps libavcodec (still gets you
  VideoToolbox/VAAPI); `gstreamer-rs` is the more powerful pipeline
  toolkit (better for PipeWire/webcam chains). Or literally spawn the
  ffmpeg CLI — honestly fine, process isolation is a feature.
- **Capture**: `scap` (cross-platform screen capture incl.
  ScreenCaptureKit on macOS), `screenshots`/`display-info` (simple),
  PipeWire directly on Linux, DXGI via `windows-capture` on Windows.
- **Webcam**: `nokhwa` (v4l2/AVFoundation/MSMf across OSes, gives raw
  or MJPEG frames). MJPEG-in webcam → decode once → re-encode H.264 →
  same UDP plane as the screen (two logical streams = two UDP ports or
  a stream-id in the packet header).
- **Present**: `wgpu`/`softbuffer`/`pixels` blit the decoded frame into
  the UI (egui texture works well), avoiding the PPM→PIL→Tk detour —
  that's the biggest end-to-end latency win left on this Linux side
  (PPM read + PIL thumbnail + Tk render is the slowest leg here).
- **Is Rust better/faster than the Python pipeline?** Mostly for:
  zero-copy capture→encode→present (skips PPM/marshal costs), owning
  the UDP socket (error tolerance + seq/NACK), and no GIL-style thread
  stalls. The *network* latency won't change — network and hardware
  encoders are already native. Expected gain: present+decode leg from
  ~15–40 ms to ~5–10 ms, and robustness under link flaps. Don't port
  for speed; port for ownership of the pipeline stages ffmpeg CLI
  can't give you.

## 7. Experiment backlog (things worth trying next)

1. **ScreenCaptureKit capture** instead of avfoundation — zero-copy,
   proper refresh-rate pacing, per-window options.
2. **IDR-on-loss**: receiver detects a UDP gap (seq numbers) → sends a
   `{"type":"idr"}` on the TCP control channel → encoder forces a
   keyframe. Cuts resync time after loss from GOP-length to ~RTT.
3. **Adaptive bitrate**: measure receiver-side loss+jitter, scale
   `-b:v` up/down periodically (Parsec does exactly this; trivial with
   your own sender, impossible with raw ffmpeg CLI).
4. **PipeWire capture on Linux** via xdg-desktop-portal ScreenCast —
   unlocks Wayland screen sharing and dmabuf zero-copy.
5. **Resilient sender shim**: ffmpeg → stdout → tiny Rust/Py UDP
   forwarder that swallows send errors → stream survives link flaps
   with zero respawn. Cheap experiment, immediate robustness win.
6. **60 fps verification run**: `testsrc2` at 1080p60 → VT encode →
   UDP → decoder fps counter (encoder side measured 160 fps headroom;
   end-to-end decode rate unverified pending TCC).
7. **Hardware decode on Linux** (VAAPI/vaapi h264 in the decoder
   command) — cuts decode CPU to near zero.
8. **GStreamer equivalent pipeline** as a CLI alternative:
   `gst-launch-1.0 avfvideosrc ! vtenc_h264 ! mpegtsmux ! udpsink` —
   lower buffering knobs than ffmpeg in places.

## 8. Operational gotchas checklist

- [ ] TCC identities are per-executable — document exactly which
      binary names appear in the user's Settings list.
- [ ] Grants apply at process spawn — respawn after grant, never assume
      a live process picks it up.
- [ ] `CGPreflightScreenCaptureAccess` over SSH ≠ GUI session truth.
- [ ] launchd `KeepAlive` + a fatal exit = respawn churn = perceived
      disconnects; poll/retry internally before exiting.
- [ ] launchd service disabled-state persists across plist
      deletion/recreation: `launchctl enable` before `bootstrap`.
- [ ] A bundled `.app` is the only way to get a friendly
      "PrecisionKVM"-style TCC name; PyInstaller-frozen binaries can't
      hot-swap code.
- [ ] mDNS/Zeroconf probes can hang the connect loop — bound every
      discovery call with a thread+join deadline.
- [ ] One TCP listener per side-channel; a stale process holding the
      port looks like a half-connected client — always enumerate all
      sockets a PID owns before diagnosing "connected but idle".

## 9. Suggested design for the new app

```
┌──────────────┐   QUIC/TCP-TLS: hello, cfg, stats, IDR-req, NACK
│ sender (Rust)│───────────────────────────────────────────┐
│ capture(scap)│                                            ▼
│   → scale    │   UDP datagrams: [stream][frame][frag][payload]
│   → VT/VAAPI │──────────────────────────────────────────►│
│   → pkt+send │                                   receiver (Rust)
└──────────────┘                                     decode(ffmpeg-next)
   2 logical streams: screen (port A) + webcam (port B) ──► present(wgpu)
```

- Stream header: `[stream_id:u8][frame_id:u32][frag_idx:u16][frag_cnt:u16]`
  — enough for reassembly + loss detection without RTP complexity.
- Config datagram identical to the one proven here — copy its shape.
- Webcam = second stream on its own port; same encode flags, usually
  lower fps target (30) and res (720p/1080p) — don't let it fight the
  screen stream for CPU; they're independent pipelines.
- Adaptive layer to add last (not first): receiver-side loss/jitter
  → bitrate hints back over control channel.

Biggest single pieces of advice: **own the UDP socket**, **mailbox
every frame handoff**, **encode at display size**, and **check TCC
before debugging anything on macOS**.
