from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description="PrecisionKVM encrypted input sharing")
    commands = parser.add_subparsers(dest="command")
    gui = commands.add_parser("gui", help="Open Windows control panel")
    gui.add_argument("--start", action="store_true", help="Start sharing and connect the prepared Mac")
    client = commands.add_parser("client", help="Run paired macOS client (posts input)")
    client.add_argument("--config", type=Path, required=True)
    commands.add_parser("identity", help="Create local pairing identity, print public fingerprint only")
    commands.add_parser("doctor", help="Check dependencies and permissions without posting input")
    args = parser.parse_args()
    if args.command in (None, "gui"):
        from .ui import run_ui
        run_ui(start=getattr(args, "start", False))
    elif args.command == "client":
        from .session import run_client
        try:
            run_client(json.loads(args.config.read_text()))
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            parser.exit(1, f"Client stopped: {exc}\n")
    elif args.command == "identity":
        from .config import identity, state_dir
        _, _, _, fingerprint = identity()
        print(json.dumps({"state_directory": str(state_dir()), "fingerprint": fingerprint}, indent=2))
    elif args.command == "doctor":
        result: dict = {"platform": sys.platform, "python": sys.version.split()[0]}
        if sys.platform == "darwin":
            from .mac import MacInjector
            result["accessibility"] = MacInjector.accessibility_available()
            injector = MacInjector()
            result["display"] = [injector.width, injector.height]
        else:
            import tkinter
            from .windows import WindowsCapture
            result["tk"] = tkinter.TkVersion
            result["capture_import"] = WindowsCapture.__name__
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
