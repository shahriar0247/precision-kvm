# PrecisionKVM

**Share one keyboard and mouse between your Windows PC and your Mac — over plain Ethernet.**

[![MIT license](https://img.shields.io/badge/license-MIT-40cdb6)](LICENSE)
[![Source version](https://img.shields.io/badge/source-0.6.0-365376)](CHANGELOG.md)

PrecisionKVM is an open-source, low-latency KVM: slide the cursor off the edge of one screen and you are controlling the other machine. Keyboard, mouse, scrolling, media keys, clipboard text and images all follow. Either machine can be the host or the client — pick a role in the panel and go.

[Download](https://github.com/shahriar0247/precision-kvm/releases) · [Quick start](#quick-start) · [Build from source](#build-from-source) · [Report a bug](https://github.com/shahriar0247/precision-kvm/issues)

## Downloads

Native packages include Python and every dependency — nothing else to install. Each download ships with a `.sha256` checksum file.

| Download | Runs on | Use it as |
|---|---|---|
| `PrecisionKVM-VERSION-windows-x64.zip` | Windows 10/11 x64 | Host or client |
| `PrecisionKVM-VERSION-windows-arm64.zip` | Windows 11 ARM64 | Host or client |
| `PrecisionKVM-VERSION-macos-arm64.zip` | Apple Silicon Mac | Client or host |
| `PrecisionKVM-VERSION-macos-x64.zip` | Intel Mac | Client or host |
| Wheel / source archive | Python users and contributors | — |

Packages are unsigned and the Mac bundle is not notarized — the OS may warn on first run (see [Troubleshooting](#troubleshooting)).

### Install on Windows

1. Download `…-windows-x64.zip` (or `-arm64` on ARM) and extract the whole archive somewhere permanent, e.g. `%LOCALAPPDATA%\Programs\PrecisionKVM`.
2. Run `PrecisionKVM.exe`. The control panel opens.
3. To keep it in the tray and start at login, pin it or add a shortcut to `shell:startup` with `PrecisionKVM.exe gui --start`.

### Install on macOS

1. Download `…-macos-arm64.zip` (Apple Silicon) or `…-macos-x64.zip` (Intel), unzip, and move `PrecisionKVM.app` into `/Applications`.
2. First launch: right-click → **Open** to pass Gatekeeper (the app is unsigned).
3. Grant **Accessibility** when macOS asks — it is required to inject input, and macOS revokes it every time the app is rebuilt or updated, so re-toggle it after each upgrade (System Settings → Privacy & Security → Accessibility → PrecisionKVM).
4. The app runs the client panel. A LaunchAgent starts it at login; it reconnects every second when the host is down.

## Quick start

The default pairing is a Windows host → Mac client:

1. On the **Windows** PC: open PrecisionKVM, keep **This device acts as: Host**. Drag the MAC CLIENT box left or right of THIS PC — it snaps to that edge. That edge is where you cross into the Mac.
2. Click **Export pairing file for the client…** and copy the file to the Mac (it contains a private token — keep it off chat and source control).
3. On the **Mac**: open PrecisionKVM, import the pairing file, grant Accessibility.
4. Press **Start sharing** on the PC. The Mac connects, then slide the mouse through the chosen edge.

To share **from** a Mac instead: on the Mac pick **Client → Host** is not needed — run the Mac app as host by building/hosting from source (see below), or simply put the Windows machine in **Client** role, import the pairing file on it, and press Start.

Reversing roles on the installed Windows build: choose **Client — receives a host's input**, import the pairing file produced by the other machine's host, press **Start**. Keyboard and mouse events are replayed locally via `SendInput`.

## Simple controls

| Action | Result |
|---|---|
| Cross the configured PC screen edge | Control the client |
| Cross the adjoining client edge | Return to the host |
| **Ctrl+Alt+F12** | Return locally without stopping the host |
| **Hold physical Esc for 1.5 s** while remote | Stop the host and clipboard service |
| **Stop sharing** | End sharing and restore local input |
| Close the window | Hide to the system tray (sharing keeps running) |

Emergency stops disarm edge entry and close the server, so a reconnecting client cannot resume sharing until you start it again. The Esc-hold threshold is configurable.

## What you get

- **Fast relative input:** raw hardware deltas, selectable 120–1000 Hz mouse updates, immediate keyboard events.
- **One app, two roles:** host captures and serves; client connects and injects. Windows and macOS on either side.
- **Mac keyboard layout, on by default:** physical **Ctrl → Control**, **Alt → Command**, **Win → Option**. One checkbox switches it off.
- **Mac-style shortcuts on Windows (optional):** one toggle makes Alt act as Command locally — Alt+C/V copy/paste, Alt+Space search, Alt+Q quit, Alt+arrows Home/End — while Alt+Tab keeps working.
- **Any-key remapping:** named keys, physical scan codes, Mac keys, media keys or Disabled; separate left/right modifiers.
- **Smooth scrolling:** adjustable easing, per-axis sensitivity, direction controls, pixel/line modes.
- **Mac Tweaks:** Windows-style Home/End, Ctrl word navigation/deletion, optional smoothing for a mouse connected directly to the Mac.
- **Text and image clipboard:** bidirectional, encrypted, chunked transfers with negotiated size limits.
- **Lock sync:** locking Windows locks the client; input is never shared onto a lock screen.
- **Gestures:** desktop/Mission Control shortcut translation and experimental native four-finger swipe recognition.
- **Left/right monitor placement:** drag the client box to either edge; crossing follows the shared edge.
- **Networking controls:** configurable port, TLS on/off, mDNS advertisement, pinned-certificate pairing.
- **Automatic reconnect:** the client checks for the host every second while disconnected.

This shares input and clipboard content. It does not stream video or replace a hardware KVM for BIOS/login screens.

## Networking and security

- Default input port: TCP **24819**; clipboard uses its own connection. Keep them on a trusted LAN.
- Connections are authenticated with the pairing token and pinned to the host's certificate fingerprint. Turning TLS off is possible but only sensible on a trusted wired network.
- mDNS discovery advertises/browses hosts on the LAN when enabled.
- The pairing file is the secret — anyone holding it can connect. Regenerate identity by deleting `server.pem`/`server.key`/`pairing.json` in the state directory.

Local state, pairing secrets, keys and logs live outside the repository: `%LOCALAPPDATA%\PrecisionKVM` on Windows, `~/.config/PrecisionKVM` on macOS.

## Build from source

Python 3.9+ runs the app; Python 3.11+ is recommended for packaging.

### Windows

```bat
git clone https://github.com/shahriar0247/precision-kvm.git
cd precision-kvm
python -m pip install -e ".[ui]"
python -m precision_kvm gui
```

### macOS

```sh
git clone https://github.com/shahriar0247/precision-kvm.git
cd precision-kvm
python -m pip install -e ".[mac]"
python -m precision_kvm client-gui
```

The Mac panel is native AppKit and ships without Tk. For login startup on macOS:

```sh
python -m precision_kvm install-startup --config /path/to/client.json
python -m precision_kvm remove-startup   # disable
```

### Linux

**Input capture/injection is not implemented on Linux.** The Python package installs and the test suite runs, but there is no Linux host or client binary. X11/Wayland backends are future work.

### Native packages

Run on the target OS/architecture:

```sh
python -m pip install '.[dev]' 'pyinstaller>=6.22.3,<7'
# macOS: python -m pip install '.[mac,dev]' 'pyinstaller>=6.22.3,<7'
python tools/build_release.py
```

Produces `artifacts/PrecisionKVM-VERSION-OS-ARCH.zip` plus a `.sha256` sidecar. `--skip-build` archives an existing `dist/` bundle. GitHub Actions builds all four native targets and the source distributions on every `v*` tag — see [docs/releases.md](docs/releases.md).

### Tests and checks

```sh
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check precision_kvm tests
python -m precision_kvm doctor
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Mac connects but input doesn't move | Re-toggle **Accessibility** for PrecisionKVM — a rebuild revokes it every time. |
| TLS/fingerprint error on connect | The host identity changed; export a fresh pairing file and re-import it. |
| App not listening on the port | Another instance holds it — close it, or change **Host port** in Connection. |
| "This app can't be opened" on macOS | Right-click → Open once; it is unsigned. |
| Sharing stops at lock screen | By design — input is never shared while either machine is locked. |

## Performance and limitations

Synthetic tests on a gigabit pair reached roughly **993–999 Hz** at the 1000 Hz setting, with ~**0.9 ms** median RTT and a 32 MiB clipboard transfer in under 2 s while the input probe held rate. These measure transport, not input-to-photon latency. See [measurement details](reports/README.md).

- One host ↔ one client is the supported topology; displays on either side are treated as a single shared edge.
- Secure desktops, login screens, elevated apps and macOS Secure Input can block capture/injection — by design of those OSes.
- Multi-client grids and Linux input backends are future work.

[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Release builds](docs/releases.md) · [Feature checklist](docs/features.md)

Released under the [MIT license](LICENSE).
