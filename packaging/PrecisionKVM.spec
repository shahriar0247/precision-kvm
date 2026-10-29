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
# Bundled Flet desktop engine; flet_desktop checks its package dir first.
flet_client_zip = root / "build" / ("flet-macos.tar.gz" if sys.platform == "darwin" else "flet-windows.zip")
if flet_client_zip.is_file():
    datas.append((str(flet_client_zip), "flet_desktop/app"))
excludes = [
    # flet's dependency chain otherwise drags the whole scientific stack in.
    "pandas", "scipy", "torch", "torchvision", "torchaudio", "numba", "llvmlite",
    "pyarrow", "botocore", "boto3", "s3transfer", "sqlalchemy", "openpyxl",
    "onnxruntime", "opentelemetry", "tensorboard", "matplotlib", "sklearn",
    "skimage", "sympy", "IPython", "jupyter", "pytest", "flet.testing",
    "flet_web", "flet_cli", "uvicorn", "fastapi", "starlette",
]
# The UI only uses QtCore/QtGui/QtWidgets; skip the rest of the Qt stack.
excludes += [
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DQuick",
    "PySide6.Qt3DRender", "PySide6.QtAsyncio", "PySide6.QtAxContainer",
    "PySide6.QtBluetooth", "PySide6.QtCanvasPainter", "PySide6.QtCharts",
    "PySide6.QtConcurrent", "PySide6.QtDBus", "PySide6.QtDataVisualization",
    "PySide6.QtDesigner", "PySide6.QtGraphs", "PySide6.QtGraphsWidgets",
    "PySide6.QtHelp", "PySide6.QtHttpServer", "PySide6.QtLabsStyleKit",
    "PySide6.QtLocation", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetworkAuth", "PySide6.QtNfc", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtPositioning", "PySide6.QtPrintSupport",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2", "PySide6.QtQuickTest", "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSensors",
    "PySide6.QtSerialBus", "PySide6.QtSerialPort", "PySide6.QtSpatialAudio",
    "PySide6.QtSql", "PySide6.QtStateMachine", "PySide6.QtSvgWidgets",
    "PySide6.QtTest", "PySide6.QtTextToSpeech", "PySide6.QtUiTools",
    "PySide6.QtWebChannel", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets", "PySide6.QtWebView",
    "PySide6.QtXml", "PySide6.QtQmlFeatures", "PySide6.scripts",
    "PySide6.QtNetwork",
]
if sys.platform == "darwin":
    excludes += ["tkinter", "_tkinter", "precision_kvm.ui",
                 "precision_kvm.ui_qt", "precision_kvm.ui_flet",
                 "PySide6", "flet"]
    for package in ("Quartz", "AppKit", "Foundation", "objc"):
        package_datas, package_binaries, package_hidden = collect_all(package)
        datas += package_datas
        binaries += package_binaries
        hiddenimports += package_hidden
else:
    import re
    # Qt ships ~600 MB of modules the UI never loads. Keep Core/Gui/Widgets/
    # Svg/OpenGL and the plugins Qt needs to show a native window.
    _qt_dll_drop = re.compile(
        r"Qt6?(?:3D|AxContainer|Bluetooth|Charts|Concurrent|DBus|DataVisualization|"
        r"Designer|Graphs|Help|HttpServer|Labs|Location|Multimedia|Network|Nfc|Pdf|"
        r"Positioning|PrintSupport|Qml|Quick|RemoteObjects|Scxml|Sensors|Serial|"
        r"SpatialAudio|Sql|StateMachine|Test|TextToSpeech|UiTools|VirtualKeyboard|"
        r"Web|Xml|CanvasPainter)[^\\/]*\.(?:dll|pyd)$", re.I)
    _qt_plugin_keep = re.compile(
        r"PySide6[\\/]plugins[\\/](?:platforms|styles|iconengines|imageformats|"
        r"platforminputcontexts|platformthemes|generic)[\\/]", re.I)
    _qt_plugin_dir = re.compile(r"PySide6[\\/]plugins[\\/]", re.I)
    _qt_dir_drop = re.compile(
        r"PySide6[\\/](?:qml|resources|translations|metatypes)[\\/]", re.I)
    _av_dll = re.compile(
        r"(?:^|[\\/])(?:avcodec|avfilter|avformat|avutil|avdevice|swscale|"
        r"swresample|postproc)-?\d*\.dll$", re.I)

    def _qt_drop(source):
        source = source.replace("/", "\\")
        return (_av_dll.search(source) is not None
                or _qt_dll_drop.search(source) is not None
                or _qt_dir_drop.search(source) is not None
                or (_qt_plugin_dir.search(source) is not None
                    and _qt_plugin_keep.search(source) is None))

    hiddenimports.append("tkinter")
    hiddenimports += ["zeroconf", "precision_kvm.ui_qt", "precision_kvm.ui_flet"]
    for package in ("flet", "PySide6"):
        try:
            package_datas, package_binaries, package_hidden = collect_all(package)
            if package == "PySide6":
                package_datas = [e for e in package_datas if not _qt_drop(e[0])]
                package_binaries = [e for e in package_binaries if not _qt_drop(e[0])]
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
if sys.platform == "win32":
    # Hooks and dependency walking re-add Qt DLLs the module excludes removed;
    # drop them at collection time so the bundle only ships what the UI loads.
    all_binaries = [e for e in all_binaries if not _qt_drop(e[0])]
    all_datas = [e for e in all_datas if not _qt_drop(e[0])]
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
