PrecisionKVM shares keyboard and mouse input from a Windows PC to a paired Mac.

Choose the download matching your machine:

- **Windows x64:** portable host/control panel.
- **Windows ARM64:** native Windows on ARM host/control panel.
- **macOS ARM64:** Apple Silicon client app.
- **macOS x64:** Intel Mac client app.
- **Wheel/source archive:** Python users and contributors.

Extract the complete archive before opening the app. Mac clients need Accessibility permission and trusted pairing information. Native downloads are currently unsigned, and Mac apps are not notarized. Linux capture/injection is not supported in this release.

Input timing depends on hardware, operating-system scheduling and the network. Automated test and transport measurements do not guarantee jitter-free physical input. Native four-finger touchpad support is optional and may also trigger Windows' local gesture behavior.

Clipboard sharing sends text/images to the paired machine when enabled; disable it when copying information that should stay local. Emergency server stop and the normal return shortcut remain available. See the repository README for setup, controls, supported platforms and limitations.

SHA256 checksums are included with the downloads.
