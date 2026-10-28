"""Release target names must match the interpreter, not a requested ZIP label."""
import hashlib
import importlib.util
from pathlib import Path

import pytest


def build_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "build_release.py"
    spec = importlib.util.spec_from_file_location("precision_release_tools", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("system,machine,target", [
    ("win32", "AMD64", "windows-x64"),
    ("win32", "ARM64", "windows-arm64"),
    ("darwin", "arm64", "macos-arm64"),
    ("darwin", "x86_64", "macos-x64"),
])
def test_four_native_release_targets(system, machine, target):
    assert build_module().native_target(system, machine) == target


@pytest.mark.parametrize("system,machine", [("linux", "x86_64"), ("linux", "aarch64"), ("win32", "x86")])
def test_unsupported_targets_cannot_be_mislabeled(system, machine):
    with pytest.raises(ValueError):
        build_module().native_target(system, machine)


def test_checksum_is_for_complete_archive_and_names_download(tmp_path):
    archive = tmp_path / "PrecisionKVM-0.2.0-windows-arm64.zip"
    archive.write_bytes(b"synthetic complete archive")
    checksum = build_module().write_checksum(archive)
    assert checksum.read_text() == hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n"
