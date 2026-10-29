"""Build a native GUI bundle and portable archive; does not publish anything."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import platform
import subprocess
import sys
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def native_target(system: str, machine: str) -> str:
    architecture = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64",
                    "aarch64": "arm64"}.get(machine.lower())
    if system == "win32" and architecture in ("x64", "arm64"):
        return f"windows-{architecture}"
    if system == "darwin" and architecture in ("x64", "arm64"):
        return f"macos-{architecture}"
    raise ValueError(f"No native release target for {system}/{machine}; supported: Windows/macOS x64/arm64")


def _stage_flet_client() -> None:
    """Bundle the downloaded Flet desktop engine so first runs stay offline.

    flet_desktop looks for its platform archive inside the package at
    ``flet_desktop/app/flet-windows.zip`` before hitting the network.
    """
    if sys.platform != "win32":
        return
    cache = Path.home() / ".flet" / "client"
    sources = sorted(cache.glob("flet-desktop-full-*/flet")) if cache.is_dir() else []
    if not sources:
        print("note: Flet desktop client not cached; bundle will download on first run")
        return
    source = sources[-1]
    target = ROOT / "build" / "flet-windows.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    newest = max(path.stat().st_mtime for path in source.rglob("*") if path.is_file())
    if target.exists() and target.stat().st_mtime >= newest:
        return
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source.parent))
    print(f"Bundled Flet desktop client: {target}")


def write_checksum(path: Path) -> Path:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    checksum = path.with_suffix(path.suffix + ".sha256")
    checksum.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return checksum


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--skip-build", action="store_true", help="Archive an existing dist bundle")
    args = parser.parse_args()
    target = native_target(sys.platform, platform.machine())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    if not args.skip_build:
        _stage_flet_client()
        subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                        str(ROOT / "packaging" / "PrecisionKVM.spec")], cwd=ROOT,
                       env=dict(os.environ, PRECISION_KVM_BUILD_VERSION=version), check=True)
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output / f"PrecisionKVM-{version}-{target}.zip"
    if sys.platform == "darwin":
        bundle = ROOT / "dist" / "PrecisionKVM.app"
        if not (bundle / "Contents" / "MacOS" / "PrecisionKVM").is_file():
            raise FileNotFoundError("Expected a complete macOS .app bundle")
        # ditto retains framework symlinks, executable bits and bundle metadata.
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                        str(bundle), str(output)], check=True)
    else:
        bundle = ROOT / "dist" / "PrecisionKVM"
        if not (bundle / "PrecisionKVM.exe").is_file():
            raise FileNotFoundError("Expected the Windows executable and its _internal folder")
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(bundle.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(bundle.parent))
    write_checksum(output)
    print(output)


if __name__ == "__main__":
    main()
