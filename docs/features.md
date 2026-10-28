# Requested feature checklist

| Request | Implementation | Verification |
|---|---|---|
| Low latency 120–1000 Hz input | Raw relative motion, native callbacks, phase-based timers, coalescing | Unit tests and actual LAN probes; physical jitter varies |
| Python UI, monitor location | Windows tabbed host, native AppKit Mac panel | UI construction and native Mac ABI checks |
| Any-key remapping | VK/scan sources, Mac/media/Disabled targets, held-target reference counts | Mapping, collision, repeat, modifier tests |
| Default Mac modifier toggle | Ctrl unchanged, Alt→Command, Win→Option; mac_layout alias matches | Both-side modifier and UI toggle tests |
| Scroll sensitivity/direction | Per-axis gain, inversion, pixel/line units, fractional preservation | Recording Quartz boundary tests |
| Smooth scrolling | Finite easing worker; optional local Mac mouse tap | Conservation, cancellation, reversal and fake tap tests |
| Mac Tweaks tab | Shared/local smoothing, Windows Home/End and word navigation | Controller, validation and shortcut tests |
| Four-finger gestures | Opt-in raw HID four-contact swipes plus shortcut bridge | Actual ASUS descriptor inspection and synthetic contact-frame tests; physical swipes still need user testing |
| Text and image clipboard | Dedicated authenticated TLS connection, PNG/DIB/TIFF conversion | Bidirectional fake backends, corrupt image, cap, slow-transfer and checksum tests |
| Large clipboard | Default 128 MiB; negotiated up to 512 MiB; chunked | Actual synthetic 32 MiB LAN transfer beside input probe |
| Emergency server stop | Physical Esc hold and rapid-click detection before remapping | Timer-independent safety and full server-stop tests |
| One-second client reconnect | Reconnecting client, single-instance lock, independent stop event | Reconnect tests and live host-restart check |
| Mac login startup | User LaunchAgent opens native client GUI | Plist/argument tests; installation checked on target Mac |
| GitHub/open-source packaging | MIT, contribution/security docs, release workflow, icons, checksums | Build artifacts only count as released after CI succeeds |
| Windows and Mac architectures | Windows x64/ARM64, Mac ARM64/Intel build targets | Native CI matrix; not every physical device tested |
| Linux (optional request) | Source distribution and explicit unsupported-platform diagnostics | No Linux input backend or native KVM binary claimed |

Runtime validation and current release artifacts are linked in the final handoff. Native gesture mode and local mouse event taps remain opt-in; no system trackpad preferences are silently changed.
