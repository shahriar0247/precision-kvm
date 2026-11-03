# Changelog

## 0.9.7

- Fixed Mac → PC clipboard sharing never working: macOS 26/27 pasteboard privacy
  ("Paste from Other Apps") silently denies the client's background clipboard
  reads, so copies on the Mac were dropped without any error while PC → Mac
  kept working. The client now detects the denied state, reports it at connect
  time and on each unreadable copy, and points to the exact fix: set
  PrecisionKVM to **Allow** under System Settings › Privacy & Security ›
  Paste from Other Apps.

## 0.9.6

- Fixed edge crossings lost during continuous motion: Windows reports the
  unclamped cursor target to the low-level hook, so a fast push reports
  positions already inside the peer's zone (e.g. x = -55) where no boundary
  press was detected — you had to stop and re-approach to land exactly on the
  edge. Overshot positions are now projected back through the union side they
  exited and treated as an edge press, so the first push past the edge always
  crosses.

## 0.9.5

- Discovery advertises the primary LAN address first: multi-homed hosts
  (virtual/WSL/direct-link adapters) no longer lead clients to probe dead
  interface addresses before reaching the usable one.

## 0.9.4

- Fixed auto-discovery on multi-homed hosts: the client now probes every
  advertised address and falls back to the paired host, instead of blindly
  trusting the first advertised address — which could pick an unreachable
  secondary interface (e.g. a direct-link subnet) and leave input unable to
  connect while screen/clipboard stayed up.
- Discovery now resolves only before the first attempt and after failures,
  instead of every retry.
- Fixed `connect_mac` for app-bundle installs: it launches
  `~/Applications/PrecisionKVM.app` when present instead of always `cd`-ing into
  the old venv directory, which no longer exists after bundle installs and was
  logging "Client exited" every launch.

## 0.9.3

- Fixed the screen-share connect/drop loop: the channel's 2-second socket deadline was treated as a disconnect whenever the stream idled, so sessions flapped every couple of seconds and "Start stream" appeared to do nothing. Idle timeouts are now tolerated on both ends — only real failures drop.
- Full screen button: the feed pops into a borderless fullscreen window at native resolution — Esc or double-click exits.
- One-click stream presets: **LAN turbo** (30 fps · 75% scale · uncapped), **Balanced**, and **Slow link** (10 fps · grayscale · 4 Mbps cap) retune every knob at once.

## 0.9.2

- Faster edge crossing: the strips beside the peer box no longer swallow the cursor — hitting any part of a peer-facing edge crosses instantly into the nearest peer (entering at its nearest point), so fast continuous motion crosses in one motion instead of needing a second nudge.
- Screen share now draws the client's real cursor over the feed (toggleable) — the framebuffer alone had no pointer in it.

## 0.9.1

- Fixed host start failing with `could not convert string to float: '100%'` when the Screen share resolution scale had been saved — stream settings now tolerate persisted percent strings and junk values.

## 0.9.0

- Screen share tab: watch the client machine's screen while you control it — a dedicated last tab shows a live feed alongside normal edge-crossing KVM control.
- View-only by design: hovering or clicking the feed never sends input; the pointer still only crosses at the screen edge.
- Tunable stream: Start/Stop/Restart plus FPS, JPEG quality, resolution scale, bitrate cap, color vs black & white, and a client monitor picker — all applied live without reconnecting.
- Runs on its own authenticated TLS channel (port + 4), length-prefixed JPEG frames, so video can never interfere with input or clipboard.
- macOS asks for Screen Recording permission the first time a stream starts, then never again; Windows needs nothing.
- New `mss` dependency for native cross-platform capture.

## 0.8.0

- Install & connect over SSH: put `user@host` in the Client SSH target field, click **Install via SSH…** and the client installs itself on the remote machine (or upgrades it when it's running an older build), writes pairing, and starts connecting back — Windows clients need nothing else, macOS asks for Accessibility once.
- Connect only: the same dialog offers a plain connect that skips downloads entirely and just (re)writes pairing and launches the client.
- SSH keys are tried first; a password prompt appears only when the remote rejects them.
- Remote version awareness: deploying against an older client auto-upgrades it to the bundled release.
- Scrollable settings: every tab scrolls when the window is too small, so the panel works on short displays; the minimum size dropped to 660×540.
- Design polish: notes and status text re-wrap to the real window width instead of clipping.

## 0.7.0

- Multiple simultaneous clients: the host accepts and serves many peers at once; each connects to its own box in the layout.
- Freeform display layout: client boxes drag anywhere around the real monitor map and magnet-snap to the nearest edge — left, right, top, bottom and corners — keeping their position along the edge.
- Canvas now draws the actual monitor topology (all local displays) with peers at their real scaled rectangles; what you see is where you cross.
- Double-click a client name to rename it (identity doubles as the pairing name); right-click or select + Remove deletes a client; + Add client creates more.
- Clients announce their hostname over the protocol (`hello` frame, backward compatible); crossing routes input only to the matching peer, and crossing an unclaimed edge does nothing instead of stealing another client.
- Fixed the stale-position bug where a saved peer rect could end up at a wrong edge (the "move it slightly and it connects" symptom): stored rects are normalized through edge snapping on load, on save and inside the capture engine.
- Crossing side now comes from the actual geometry instead of the legacy side setting, so top/bottom crossings enter the client on the correct edge.

## 0.6.0

- Restored the native Tkinter control panel; the Qt, Flet and pywebview UIs were removed. Windows bundle drops to ~87 MB.
- One app, two roles: a Host/Client picker in the panel. Windows can act as a client (SendInput injection); macOS can act as a host (CGEventTap capture).
- Client mode shows a pairing import panel (file picker or pasted JSON); host-only fields hide.
- Port, TLS and mDNS discovery controls are back in the Connection tab; pairing export carries the TLS flag.
- Client mode no longer requires a peer address in settings validation.
- Fixed a crash where the monitor-drag canvas threw a TclError on every redraw.

## 0.5.0

- Experimental single pywebview panel for both roles (removed in 0.6.0).
- Host/client role plumbing: `windows_inject` SendInput replay, `mac_capture` CGEventTap host capture.

## 0.4.0

- Qt6 and Flet control panels with a UI switcher (removed in 0.6.0).
- mDNS host discovery/advertisement, configurable port, TLS toggle.
- Lock sync: locking Windows locks the client; input is never shared onto a lock screen.
- Monitor layout simplified to deterministic left/right edge snapping; stored positions self-heal.
- Removed the rapid-left-click emergency stop; Esc-hold and Ctrl+Alt+F12 remain.
- Start Menu entries and login autostart on Windows.

## 0.3.0

- `mac_mode`: optional Mac-style shortcuts on Windows — Alt as Command for copy/paste/search/quit/arrows while Alt+Tab stays unchanged.
- Draggable client box carries a real edge alignment; connector visuals.

## 0.2.0

- Native Mac client panel, Windows settings refresh and Mac Tweaks tabs.
- Default Alt/Command and Windows/Option swap, keeping Control unchanged.
- Arbitrary physical-key remapping, modifier presets and safe held-target cleanup.
- Raw/system relative mouse options, per-axis scrolling and bounded scroll easing.
- Windows Home/End and word-navigation preferences for Mac.
- Bidirectional text/image clipboard on a separate authenticated connection, with negotiated large-copy limits.
- Experimental native four-finger contact recognition and desktop shortcut bridging.
- Esc-hold/rapid-click server stops, one-second reconnect and user-scoped Mac startup.
- Native build matrix, portable packages, icons, SHA256 checksums and MIT project documentation.

## 0.1.0

- Initial Windows-to-Mac input-sharing engine, TLS pairing, Python host panel and transport measurements.
