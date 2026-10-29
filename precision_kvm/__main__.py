"""PrecisionKVM native launchers and command-line diagnostics."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import __version__


def main():
    from .config import state_dir
    if sys.stdout is None:
        sys.stdout = (state_dir() / "app.log").open("a", buffering=1)
    if sys.stderr is None:
        sys.stderr = (state_dir() / "app-error.log").open("a", buffering=1)
    parser = argparse.ArgumentParser(description="PrecisionKVM encrypted input and clipboard sharing")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")
    gui = commands.add_parser("gui", help="Open Windows host control panel")
    gui.add_argument("--start", action="store_true", help="Start sharing and connect the prepared Mac")
    gui.add_argument("--ui", choices=("qt", "flet", "tk"),
                   help="Control panel framework (default: saved setting)")
    client = commands.add_parser("client", help="Run reconnecting macOS client in a terminal")
    client.add_argument("--config", type=Path, required=True)
    client_gui = commands.add_parser("client-gui", help="Open native Mac client and tweaks panel")
    client_gui.add_argument("--config", type=Path)
    startup = commands.add_parser("install-startup", help="Start Mac client UI at login")
    startup.add_argument("--config", type=Path)
    commands.add_parser("remove-startup", help="Disable Mac login startup")
    commands.add_parser("identity", help="Create local pairing identity; print public fingerprint only")
    commands.add_parser("doctor", help="Check native dependencies and permissions without posting input")
    args = parser.parse_args()
    command = args.command or ("client-gui" if sys.platform == "darwin" else "gui")
    if command == "gui":
        if sys.platform != "win32":
            parser.error("The sharing host UI currently requires Windows; use client-gui on macOS")
        from .ui import run_ui
        run_ui(start=getattr(args, "start", False), ui=getattr(args, "ui", None))
    elif command in ("client", "client-gui"):
        if sys.platform != "darwin":
            parser.error("The input client requires macOS")
        import fcntl
        config_path = getattr(args, "config", None) or state_dir() / "client.json"
        try:
            with (state_dir() / "client.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    print("PrecisionKVM client is already running.", flush=True)
                    return
                if command == "client-gui":
                    from .client_ui import run_client_gui
                    run_client_gui(config_path)
                else:
                    from .session import run_client_forever
                    run_client_forever(json.loads(config_path.read_text()))
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            parser.exit(1, f"Client stopped: {exc}\n")
    elif command in ("install-startup", "remove-startup"):
        from .startup import install_startup, remove_startup
        if command == "install-startup":
            path = install_startup(getattr(args, "config", None) or state_dir() / "client.json")
            print(f"Mac login startup installed: {path}")
        else:
            remove_startup()
            print("Mac login startup disabled")
    elif command == "identity":
        from .config import identity
        _, _, _, fingerprint = identity()
        print(json.dumps({"state_directory": str(state_dir()), "fingerprint": fingerprint}, indent=2))
    elif command == "doctor":
        result: dict = {"platform": sys.platform, "python": sys.version.split()[0], "version": __version__}
        if sys.platform == "darwin":
            from .mac import MacInjector
            result["accessibility"] = MacInjector.accessibility_available()
            injector = MacInjector()
            result["display"] = [injector.width, injector.height]
        elif sys.platform == "win32":
            import tkinter
            from .windows import WindowsCapture
            result["tk"] = tkinter.TkVersion
            result["capture_import"] = WindowsCapture.__name__
        else:
            result["supported_input_platform"] = False
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
