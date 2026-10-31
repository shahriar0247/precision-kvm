# Changelog

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
