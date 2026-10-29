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
hiddenimports = ["PIL.Image", "PIL.PngImagePlugin", "PIL.TiffImagePlugin",
                 "zeroconf", "webview"]
excludes = [
    "pandas", "scipy", "torch", "torchvision", "torchaudio", "numba", "llvmlite",
    "pyarrow", "botocore", "boto3", "s3transfer", "sqlalchemy", "openpyxl",
    "onnxruntime", "opentelemetry", "tensorboard", "matplotlib", "sklearn",
    "skimage", "sympy", "IPython", "jupyter", "pytest",
    "PySide6", "flet", "flet_web", "flet_cli", "uvicorn", "fastapi", "starlette",
]
if sys.platform == "darwin":
    excludes += ["tkinter", "_tkinter"]
    hiddenimports += ["WebKit"]
    for package in ("Quartz", "AppKit", "Foundation", "objc", "WebKit"):
        try:
            package_datas, package_binaries, package_hidden = collect_all(package)
            datas += package_datas
            binaries += package_binaries
            hiddenimports += package_hidden
        except Exception:
            pass
else:
    # pywebview's WebView2 backend needs .NET interop assemblies and its
    # bundled JS/interop DLLs, which collect_all picks up from the package.
    hiddenimports += ["clr", "clr_loader", "pythonnet", "cffi", "pycparser"]
    for package in ("webview", "pythonnet", "clr_loader"):
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
collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
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
