# Building and releasing

## Current native targets

| Download | Role | Native build runner |
|---|---|---|
| `PrecisionKVM-VERSION-windows-x64.zip` | Windows input host and GUI | `windows-latest`, x64 Python |
| `PrecisionKVM-VERSION-windows-arm64.zip` | Windows on ARM input host and GUI | `windows-11-arm`, ARM64 Python 3.13 |
| `PrecisionKVM-VERSION-macos-arm64.zip` | Apple Silicon Mac client and GUI | `macos-15`, ARM64 Python |
| `PrecisionKVM-VERSION-macos-x64.zip` | Intel Mac client and GUI | `macos-15-intel`, x64 Python |
| `.whl` and `.tar.gz` | Python package and source | Python build backend |

These are four separate native binaries, not a universal binary. The current runtime supports a Windows host and a Mac client. Linux input capture/injection is not implemented or advertised as working. The platform-neutral wheel name describes Python packaging, not support for every operating system. Automated ARM64 builds verify packaging and tests; physical Windows ARM64 input hardware still requires device validation.

The native builds include Python and dependencies; users do not need to install Python. Unzip the Windows download and keep the executable with its `_internal` directory. Unzip the Mac download and copy `PrecisionKVM.app` into Applications. The Mac requires Accessibility access for input injection. Start the host, pair the Mac using a trusted fingerprint/token, and connect through the Mac client UI.

Bundles are currently unsigned and Mac bundles are not notarized. The operating system may warn or block them. No security settings are changed by the installer, and the project does not recommend disabling platform protections. Users who require trusted distribution should build/sign through their own organization or wait for signed releases.

## Local native build

Run on the target OS/architecture using Python 3.11, or Python 3.13 on Windows ARM64, then:

```sh
python -m pip install '.[dev]' 'pyinstaller>=6.22.3,<7'
# macOS: python -m pip install '.[mac,dev]' 'pyinstaller>=6.22.3,<7'
python tools/build_release.py
```

The helper reads the package version, runs `packaging/PrecisionKVM.spec`, creates an archive under `artifacts/`, and writes its `.sha256` sidecar. macOS uses `ditto` so framework symlinks, executable bits and app metadata survive archiving. It does not publish anything. `--skip-build` archives an already-built native bundle.

Native builds generate a deterministic monitor/link icon from `packaging/make_icon.py`: a multi-resolution Windows `.ico` or Mac `.icns`. Generated icons stay under `build/icons`; no external image download is required.

PyInstaller builds on the native OS and uses the selected Python architecture. Running a Windows build does not create a Mac app. Python, PyObjC, Tk and dependency versions constrain the minimum supported OS; each candidate release must be tested on the intended user machines rather than assuming universal backward compatibility.

## GitHub Actions

`.github/workflows/release.yml` builds the four native targets plus source distributions on a `v*` tag or manual workflow dispatch. Manual dispatch defaults to artifact-only builds. Tag pushes publish a release only after all build jobs pass. To publish manually, select **publish** and an existing stable tag matching `pyproject.toml`'s version. The `windows-11-arm` standard runner requires a public repository.

Before creating a tag:

1. Update the package version and release notes.
2. Run source checks and review secret exclusions.
3. Test the GUI, pairing, movement, key releases, scroll, clipboard and both emergency stops on actual machines.
4. Create the matching `vMAJOR.MINOR.PATCH` tag and push it when release publication is intended.

The workflow uploads archives, per-archive checksums, wheel/sdist and aggregate `SHA256SUMS.txt`. Verify a download by comparing its SHA256 with the trusted release checksum. A successful CI build confirms packaging and automated tests, not physical input smoothness or gesture compatibility.

References: [GitHub runner architectures](https://docs.github.com/en/actions/reference/runners/github-hosted-runners), [PyInstaller native build and macOS architecture options](https://pyinstaller.org/en/stable/usage.html).
