# Contributing

PrecisionKVM is MIT-licensed. Contributions that improve predictable input delivery, safety, cross-platform behavior and clear setup are welcome.

Use Python 3.11 for the development and release toolchain:

```sh
python -m venv .venv
# Activate the virtual environment for your shell.
python -m pip install -e '.[dev]'
# On macOS use: python -m pip install -e '.[mac,dev]'
python -m ruff check precision_kvm tests tools packaging/*.py
python -m mypy precision_kvm
python -m pytest -q
```

Keep changes focused and describe the concrete behavior, validation and any limits in your pull request. Add tests for new protocol/state behavior; input tests should use fake backends and never install global hooks or change a contributor's clipboard by default. Real input or clipboard smoke tests must be explicit and restore test state.

Protect the input path: clipboard transfers belong on their separate authenticated connection; key releases must remain ordered; queue overflow and disconnects must release injected state. Emergency stop recognition must happen before key remapping.

Do not commit pairing tokens, certificates/private keys, local settings, personal clipboard data, device-specific logs or binary build outputs. Use synthetic fixtures. For security issues, follow [SECURITY.md](SECURITY.md) rather than posting a working exploit publicly.

Native release builds require the target OS and architecture. See [release documentation](docs/releases.md). Windows ARM64 uses native Python 3.13 and PyInstaller 6.22.3 or newer. A Linux input backend is new platform work, not a change to the download label.
