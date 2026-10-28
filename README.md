# PrecisionKVM

**Your Windows keyboard and mouse. Your Mac. One connected workspace.**

[![MIT license](https://img.shields.io/badge/license-MIT-40cdb6)](LICENSE)
[![Source version](https://img.shields.io/badge/source-0.2.0-365376)](CHANGELOG.md)

PrecisionKVM is an open-source input-sharing app with a Windows host panel, a native Mac client, encrypted clipboard sharing and Mac preferences for Windows users. Move through the edge of your PC screen to control the Mac, then move back to return.

[Quick start](#quick-start-from-source) · [Optional native builds](docs/releases.md) · [Report a bug](https://github.com/shahriar0247/precision-kvm/issues)

## Quick start from source

Python 3.9+ is required for source installations. Use Python 3.11+ for the native packaging tools.

On Windows:

    git clone https://github.com/shahriar0247/precision-kvm.git
    cd precision-kvm
    python -m pip install -e .
    python -m precision_kvm gui

On Mac, in a Python virtual environment:

    git clone https://github.com/shahriar0247/precision-kvm.git
    cd precision-kvm
    python -m pip install -e ".[mac]"
    python -m precision_kvm client-gui

Export pairing from the Windows panel, import it in the Mac panel, then connect. Accessibility approval is required on the Mac. Native packages can be built using the optional workflow; no public binary release is required to run the project.

## What you get

- **Fast relative input:** raw hardware deltas, selectable 120–1000 Hz mouse updates and immediate keyboard events.
- **Mac keyboard layout, on by default:** physical **Ctrl → Control**, **Alt → Command**, **Win → Option**. One checkbox switches it off.
- **Any-key remapping:** named keys, physical scan codes, Mac keys, media keys or Disabled; separate left/right modifiers.
- **Smooth scrolling:** adjustable easing, independent horizontal/vertical sensitivity, direction controls and pixel/line modes.
- **Mac Tweaks:** Windows-style Home/End, Ctrl word navigation/deletion and optional smoothing for a mouse connected directly to the Mac.
- **Text and image clipboard:** bidirectional, encrypted, chunked transfers with a default 128 MiB limit, configurable up to 512 MiB.
- **Monitor positioning:** put the Mac left, right, above or below the PC.
- **Gestures:** desktop/Mission Control shortcut translation and experimental native four-finger swipe recognition.
- **Easy recovery:** return shortcut, Esc-hold stop, rapid-click stop and a visible Stop button.
- **Automatic reconnect:** the Mac checks for the host every second while disconnected; optional Mac login startup.

This shares input and clipboard content. It does not stream video or replace a hardware KVM for BIOS/login screens.

## Optional native packages

The optional native build workflow produces packages containing Python and the app dependencies. These are build targets, not a claim that a public release has been published.

| Download | Role |
|---|---|
| Windows x64 ZIP | Windows host and settings panel |
| Windows ARM64 ZIP | Native Windows-on-Arm host |
| macOS ARM64 ZIP | Apple Silicon Mac client |
| macOS x64 ZIP | Intel Mac client |
| Wheel / source archive | Python users and contributors |

Extract the entire Windows ZIP before running the executable. On Mac, move PrecisionKVM.app into Applications before enabling login startup. Packages include SHA256 checksums. Native packages are unsigned; Mac packages are not notarized. Platform packages do not imply physical-device testing on every CPU/model.

**Linux input sharing is not implemented.** Source distributions can be inspected on Linux, but there is no Linux KVM client/host binary in this release.

## Connect your computers

1. Open PrecisionKVM on Windows. Choose your Mac's screen position in **Connection**.
2. Select **Export pairing file for Mac**. Transfer that file privately to your Mac.
3. Open the Mac app and select **Import pairing**. Choose the exported file.
4. Grant the Mac app Accessibility access if macOS requests it.
5. Start sharing on the PC and connect the Mac. Move across the chosen PC screen edge.

The pairing file contains a private token. Keep it out of source control and public messages. Normal connections use pinned TLS certificates and the token; the application does not require an SSH password.

Default ports are TCP **24819** for input and **24821** for clipboard. Allow these only on the LAN you intend to use. SSH launch is optional: a previously prepared Mac source installation can be started using a key-authenticated user@host target. The native Mac app can simply run independently and reconnect itself.

For the development installation already prepared on the two machines, use Start-PrecisionKVM.cmd on Windows. Start-Client.command launches the terminal client; the native Mac panel is available with:

    python -m precision_kvm client-gui --config /path/to/client.json

## Simple controls

| Action | Result |
|---|---|
| Cross the configured PC screen edge | Control the Mac |
| Cross the adjoining Mac edge | Return to the PC |
| **Ctrl+Alt+F12** | Return locally without stopping the host |
| **Hold physical Esc for 1.5 seconds** while remote | Stop the PC host and clipboard service |
| **Six left clicks within 1.2 seconds** while remote | Stop the PC host and clipboard service |
| **Stop sharing** | End sharing and restore local input |

Emergency thresholds are configurable. Emergency stops disarm edge entry and close the server, so a reconnecting Mac cannot resume sharing until you explicitly start it again. Earlier clicks are delivered normally; Esc is preferable when clicking could perform unwanted actions.

## Keyboard layout and remapping

The prominent **Mac keyboard layout** toggle is enabled by default:

| Physical PC key | Mac destination |
|---|---|
| Ctrl | Control |
| Alt | Command |
| Windows | Option |

Both option_cmd and mac_layout use this layout. **Control remains Control.** The advanced ctrl_cmd preset exists only for users explicitly choosing Ctrl/Command exchange; it is not the default.

Custom mappings override presets. Select a source and destination, then choose Add/replace. Advanced sources accept Windows VK codes such as 0x41 or physical identifiers such as scan:0x1e:0. Destinations include named keys, media keys, mac:0..127 and Disabled. Shared target keys remain held until every mapped source releases them.

Keyboard delivery has no FPS limiter. Windows repeat settings determine repeated key events. Volume, mute, media playback, dragging and extra mouse buttons are supported.

## Mouse, scrolling and Mac Tweaks

**Mouse & gestures** offers raw relative motion (recommended) or Windows-accelerated relative motion. Pointer sensitivity is a linear multiplier; there is no cursor smoothing queue.

**Mac Tweaks** offers:

- Smooth scrolling with a quick initial response and adjustable 10–150 ms settling.
- Scroll animation rates from 30–240 Hz.
- Independent horizontal/vertical sensitivity, inversion and pixel/line units.
- Home/End mapped to line start/end, including Shift selection.
- Physical Ctrl+arrows/Backspace/Delete mapped to Mac word navigation/deletion.
- On the Mac panel, optional smoothing for a directly connected mouse.

Only wheel movement is eased. Keyboard and mouse movement stay on their immediate path. Local Mac smoothing leaves continuous built-in trackpad scrolling alone, and skips tagged PrecisionKVM events to avoid double processing. It needs macOS permission to create an input event tap; a denied permission is shown in the panel rather than bypassed. No system preference is silently changed.

Mac-local preferences can override the PC defaults. Edit settings on the Windows panel and choose **Apply & reconnect**, or save/apply preferences in the Mac panel.

## Four-finger gestures

Optional native recognition reads Precision Touchpad contact reports and maps a four-finger swipe:

| Swipe | Mac action |
|---|---|
| Left / right | Previous / next desktop |
| Up | Mission Control |
| Down | Show Desktop |

The recognizer requires four stable contact IDs moving coherently, supports parallel and hybrid HID frames, and sends one action until all fingers lift. The actual ASUS Precision Touchpad descriptor was inspected successfully.

**Native recognition is experimental and off by default.** Windows may also perform its own gesture action. Set Windows four-finger gestures to Nothing before enabling this mode. These are discrete Mac actions, not continuous macOS multitouch animations. Physical gesture behavior requires testing on the user's trackpad.

The separate shortcut bridge translates injected Ctrl+Win+Left/Right, Win+Tab and Win+D while sharing. Other injected keyboard input is ignored when that bridge is enabled to prevent partial desktop chords leaking locally. Mac gestures use standard macOS shortcut bindings.

## Clipboard sharing

New text and PNG-image copies sync in both directions. Existing clipboard contents are baselined at connection time and are not overwritten just because you connect.

Windows bitmaps and Mac TIFF images are converted to PNG. PNG transparency is retained, with an additional Windows bitmap fallback for applications that need it. Files, rich HTML and clipboard history are outside this release.

The smaller peer limit is negotiated. Large transfers use their own TLS connection, 32 KiB chunks, checksums, bounded sizes and independent heartbeats. Images are structurally verified and decoded before replacing the local clipboard. Oversized copies are skipped with a status message. Clipboard contents are never logged or saved to disk.

## Mac login startup

For source installations:

    python -m precision_kvm install-startup --config /path/to/client.json

This installs a user LaunchAgent that opens the native Mac panel at login. It does not require root or an SSH password. The client then retries every second when the Windows host is unavailable. Quitting the panel stops the client for the current session.

To disable source-installation startup:

    python -m precision_kvm remove-startup

Keep a packaged Mac app at its final Applications location before installing its startup entry.

## Performance and validation

Synthetic tests on the actual gigabit Ethernet pair achieved roughly **993–999 Hz** at the 1000 Hz setting. A separate **32 MiB** clipboard-protocol transfer completed in **1.94 seconds** while the input probe held **998.73 Hz**, with **5,993/5,993 echoes** and approximately **0.90 ms median RTT**.

These measure transport, not physical input-to-photon latency. Rare timing spikes were observed and are preserved in the reports; there is no jitter-free guarantee. Separate connections prevent clipboard data entering the input queue, but CPU scheduling and the network still matter. See [measurement details](reports/README.md).

    python -m pip install -e ".[dev]"
    python -m ruff check .
    python -m mypy precision_kvm
    python -m build --wheel
    python -m pytest -q
    python -m precision_kvm doctor

For a Mac source client, install the mac extras as well:

    python -m pip install -e ".[mac]"

Tests use recording native boundaries, fake clipboard backends and local sockets. Mac native construction checks intercept posts. Automated tests do not replace physical scrolling/gesture/volume testing.

## Project and limitations

One Windows host and one Mac client are supported, using their primary displays as switching boundaries. Secure desktops, login screens, some elevated apps and macOS Secure Input can restrict capture/injection. The active Mac keyboard layout determines characters. Multi-monitor grids, Linux input backends and file transfer are future work.

Local state, pairing secrets, keys and logs stay outside the repository. On Windows, state is under %LOCALAPPDATA%/PrecisionKVM; on Mac, ~/.config/PrecisionKVM.

[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Release builds](docs/releases.md) · [Feature checklist](docs/features.md)

Released under the [MIT license](LICENSE).
