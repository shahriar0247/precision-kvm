# Native-only build. Run from the repository root with PyInstaller installed.
import os
from pathlib import Path
import platform
import runpy
import sys

from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent
version = os.environ.get("PRECISION_KVM_BUILD_VERSION", "0.1.0")
icon_path = root / "build" / "icons" / ("PrecisionKVM.icns" if sys.platform == "darwin" else "PrecisionKVM.ico")
runpy.run_path(str(root / "packaging" / "make_icon.py"))["create_icon"](icon_path)
datas = [(str(root / "LICENSE"), "."), (str(root / "README.md"), "."),
         (str(icon_path), "assets")]
if os.environ.get("PRECISION_KVM_OPENSSL_LICENSE"):
    datas.append((os.environ["PRECISION_KVM_OPENSSL_LICENSE"], "licenses/OpenSSL"))
binaries = []
hiddenimports = ["PIL.Image", "PIL.PngImagePlugin", "PIL.TiffImagePlugin"]
excludes = []
if sys.platform == "darwin":
    excludes += ["tkinter", "_tkinter", "precision_kvm.ui"]
    for package in ("Quartz", "AppKit", "Foundation", "objc"):
        package_datas, package_binaries, package_hidden = collect_all(package)
        datas += package_datas
        binaries += package_binaries
        hiddenimports += package_hidden
else:
    hiddenimports.append("tkinter")
    hiddenimports += ["zeroconf", "precision_kvm.ui_qt", "precision_kvm.ui_flet"]
    for package in ("flet", "PySide6"):
        try:
            package_datas, package_binaries, package_hidden = collect_all(package)
            datas += package_datas
            binaries += package_binaries
            hiddenimports += package_hidden
        except Exception:
            pass

analysis = Analysis(
    [str(root / "packaging" / "launcher.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="PrecisionKVM",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=platform.machine() if sys.platform == "darwin" else None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path),
)
executables = [executable]
extra_binaries = []
extra_datas = []
if sys.platform == "win32":
    analysis_flet = Analysis(
        [str(root / "packaging" / "launcher_flet.py")],
        pathex=[str(root)],
        binaries=[],
        datas=[],
        hiddenimports=hiddenimports,
        hookspath=[],
        runtime_hooks=[],
        excludes=excludes,
        noarchive=False,
    )
    archive_flet = PYZ(analysis_flet.pure)
    executables.append(EXE(
        archive_flet,
        analysis_flet.scripts,
        [],
        exclude_binaries=True,
        name="PrecisionKVM-Flet",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        argv_emulation=False,
        icon=str(icon_path),
    ))
all_binaries = analysis.binaries + (analysis_flet.binaries if sys.platform == "win32" else [])
all_datas = analysis.datas + (analysis_flet.datas if sys.platform == "win32" else [])
collection = COLLECT(
    *executables,
    all_binaries,
    all_datas,
    strip=False,
    upx=False,
    name="PrecisionKVM",
)
if sys.platform == "darwin":
    application = BUNDLE(
        collection,
        name="PrecisionKVM.app",
        bundle_identifier="io.github.shahriar0247.precisionkvm",
        icon=str(icon_path),
        info_plist={
            "CFBundleName": "PrecisionKVM",
            "CFBundleDisplayName": "PrecisionKVM",
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version,
            "NSHighResolutionCapable": True,
            "LSUIElement": False,
            "NSHumanReadableCopyright": "Copyright © 2026 Ahmed Shahriar. MIT License.",
        },
    )
