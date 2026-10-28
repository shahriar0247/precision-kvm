"""User-scoped macOS login startup for the client control panel."""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
import sys

from .config import state_dir

LABEL = "io.github.shahriar0247.precisionkvm"


def startup_definition(executable: str, config: Path, source_root: Path,
                       log_directory: Path, frozen=False) -> dict:
    arguments = [executable]
    if not frozen:
        arguments.extend(("-m", "precision_kvm"))
    arguments.extend(("client-gui", "--config", str(config.resolve())))
    result = {"Label": LABEL, "ProgramArguments": arguments, "RunAtLoad": True,
              "KeepAlive": False, "LimitLoadToSessionType": "Aqua",
              "ProcessType": "Interactive", "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
              "StandardOutPath": str(log_directory / "mac-gui.log"),
              "StandardErrorPath": str(log_directory / "mac-gui-error.log")}
    if not frozen:
        result["WorkingDirectory"] = str(source_root.resolve())
    return result


def startup_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")


def startup_enabled() -> bool:
    return startup_path().exists()


def install_startup(config_path: Path, start_now=True):
    if sys.platform != "darwin":
        raise OSError("Login startup is available for the macOS client")
    destination = startup_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    definition = startup_definition(sys.executable, Path(config_path), Path(__file__).resolve().parents[1],
                                    state_dir(), frozen=bool(getattr(sys, "frozen", False)))
    destination.write_bytes(plistlib.dumps(definition))
    destination.chmod(0o600)
    if start_now:
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", domain + "/" + LABEL],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        subprocess.run(["launchctl", "enable", domain + "/" + LABEL], check=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(destination)], check=True)
    return destination


def remove_startup():
    if sys.platform != "darwin":
        raise OSError("Login startup is available for the macOS client")
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    startup_path().unlink(missing_ok=True)
