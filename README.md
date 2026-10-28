# PrecisionKVM

A Python control panel and native Windows → macOS keyboard/mouse sharing engine. The Mac is placed to the left by default; left, right, top and bottom arrangements are configurable. This shares input over the LAN, not video or a hardware BIOS console.

## Start

On the prepared Windows machine, run `Start-PrecisionKVM.ps1`, then **Start sharing host**. The button starts the host and uses key-authenticated SSH to run the prepared Mac client. Opening the control panel alone does not install input hooks. Settings lock while running; stop before changing them.

Alternatively:

```powershell
python -m precision_kvm gui
```

Move the pointer through the configured edge to control the Mac. Cross the corresponding Mac edge to return. **Ctrl+Alt+F12** immediately restores local control. **Stop / return locally** ends the session. Disconnecting releases injected keys and buttons. An unresponsive peer times out after approximately two seconds.

The keyboard dispatches events immediately. A keyboard “FPS” limiter would add latency and can delay releases, so the UI controls the mouse update limit independently. Key repeats follow Windows keyboard repeat settings. Volume up/down, mute, playback keys, mouse buttons, dragging, and both scroll axes are forwarded. Windows maps to Mac Command; optionally swap Ctrl and Command for familiar shortcuts.

## Preparation on another pair of machines

Windows requires Python 3.9+ with Tk and `python -m pip install -e .`. macOS requires Python 3.9+ and `python -m pip install '.[mac]'` in a virtual environment. The existing prepared Mac uses `~/Applications/PrecisionKVM/.venv`.

1. Run `python -m precision_kvm identity` on Windows. The certificate, private key, token and settings are generated **outside the repository**, under `%LOCALAPPDATA%/PrecisionKVM`.
2. Copy only the pairing token and public certificate fingerprint to a Mac-local configuration over trusted SSH. The Mac configuration is `~/.config/PrecisionKVM/client.json`, mode 600:

   ```json
   {"host":"WINDOWS_LAN_IP","port":24819,"fingerprint":"SHA256_CERTIFICATE_FINGERPRINT","token":"PAIRING_TOKEN"}
   ```

3. Grant Accessibility to the Mac Python executable/Terminal if required. `python -m precision_kvm doctor` checks permission and native dependencies without injecting input.
4. Allow the Windows Python executable's inbound TCP port 24819 on your private LAN if Windows Firewall prompts. No firewall rules are changed by this project.
5. For the control panel's SSH launch, deploy the source to `~/Applications/PrecisionKVM`, install dependencies in `.venv`, and enter `user@host` in **Mac SSH target**. SSH must already trust the host and have a working key. No SSH password is saved or passed.

You can instead leave the SSH target empty and start the Mac client manually:

```sh
cd ~/Applications/PrecisionKVM
.venv/bin/python -m precision_kvm client --config ~/.config/PrecisionKVM/client.json
```

The Mac client reconnects automatically after disconnects, attempting a new connection every second while the server is unavailable. Each failed TCP/TLS connection attempt has a 0.8-second connection timeout; retries do not overlap. Permission/configuration errors remain visible rather than looping silently. Only one client process runs per Mac user. `Start-Client.command` opens the client in Terminal for visible connection status; Ctrl+C stops it. Stop the Mac client too if you want to prevent it reconnecting when the Windows host starts again.

The Windows IP and Mac pairing configuration must be refreshed if the host address changes. The control panel's Mac address is used for local settings; SSH target determines which prepared Mac is launched. Client diagnostic output is saved in the Windows state directory as `mac-client.log`.

## Performance design

- Windows `WM_INPUT` supplies hardware-relative movement, independent of Windows pointer acceleration. Low-level hooks suppress local events during sharing and detect edge crossings. Hook callbacks enqueue and never perform socket writes.
- Motion uses selectable 120–1000 Hz pacing. Adjacent movement is summed, retaining total displacement rather than playing a backlog of obsolete cursor positions. Discrete key/button/wheel events bypass the motion limiter and retain their order.
- Native Quartz events inject directly on the Mac. Fractional pointer gain and scroll increments are retained. No interpolation animation or smoothing queue is used.
- TLS certificate pinning plus a random pairing token authenticates the connection. `TCP_NODELAY` disables Nagle delays. Frame sizes, queue count and queue age are bounded. Overflow fails closed rather than losing a key release.
- A scoped Windows scheduling-resolution request is restored when capture stops. No permanent OS, driver or power-plan tweaks are applied.

1000 Hz is an available transport ceiling, not a guarantee of physical input delivery or displayed cursor refresh. Mouse polling, OS scheduling, display refresh, network jitter and macOS event processing still apply. The control panel shows RTT, queue depth, maximum queue wait and coalescing counters. RTT is a round-trip network/application measurement, not one-way input-to-photon latency.

## Reproducible verification

```sh
python -m ruff check .
python -m mypy precision_kvm
python -m build --wheel
python -m pytest -q
python -m precision_kvm doctor
```

Tests use recording input backends and socket pairs, never global hooks or injected keystrokes. The real-Mac doctor and event-construction checks do not post events. Live capture, scrolling behavior, volume behavior and perceived jitter require an explicitly launched sharing session.

For an **input-free** TLS probe, run `python -m precision_kvm.benchmark server` on a machine with a local identity, then run this from its paired peer:

```sh
python -m precision_kvm.benchmark probe --config /path/to/probe-config.json --rates 120,240,500,1000 --seconds 3
```

The probe defaults to port 24820 and accepts one connection before exiting. Probe JSON has the same host/fingerprint/token shape as client configuration; do not commit it. Reports distinguish achieved rate, RTT percentiles, queue depth and unanswered echoes from hardware latency.

## Scope and limits

This first version supports one Windows host and one Mac client, using each machine's primary display as the switching boundary. It does not mirror video, share clipboard/files, support secure-desktop input, provide BIOS access, or handle arbitrary multi-monitor grids. Physical US key positions are translated; the Mac keyboard layout determines characters. Media key behavior can depend on the Mac's output device. Secure Input, login screens, elevated Windows applications and OS privacy protections can restrict capture/injection.

Source is stored in the private GitHub repository. Local secrets, pairing files, private keys and machine logs are excluded. Do not add local state directories to Git.

Native API references: [Windows Raw Input](https://learn.microsoft.com/en-us/windows/win32/inputdev/raw-input), [Windows low-level mouse hooks](https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelmouseproc), [Apple CGEvent](https://developer.apple.com/documentation/coregraphics/cgevent).
