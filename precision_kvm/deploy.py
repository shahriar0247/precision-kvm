"""Push a PrecisionKVM client to a remote machine over SSH, then start it.

The remote side only needs an SSH login: this module detects macOS or
Windows, downloads the matching release bundle when the app is missing,
writes the pairing file (client.json) and launches the client so it
connects back to this host.
"""
from __future__ import annotations

import json
import re
import socket
from typing import Callable

from . import __version__
from .config import identity, local_ip

RELEASE_URL = "https://github.com/shahriar0247/precision-kvm/releases/download"

MAC_APP = "$HOME/Applications/PrecisionKVM.app"
MAC_BIN = f"{MAC_APP}/Contents/MacOS/PrecisionKVM"
MAC_CONFIG_DIR = "$HOME/.config/PrecisionKVM"
WIN_DIR = "$env:LOCALAPPDATA\\Programs\\PrecisionKVM"
WIN_EXE = f"{WIN_DIR}\\PrecisionKVM.exe"
WIN_CONFIG = "$env:LOCALAPPDATA\\PrecisionKVM\\client.json"

_TARGET = re.compile(r"(?P<user>[A-Za-z0-9_.-]+)@(?P<host>[A-Za-z0-9_.:-]+)")


def parse_target(target: str) -> tuple[str, str]:
    match = _TARGET.fullmatch(target.strip())
    if not match:
        raise ValueError("SSH target must look like user@host")
    return match.group("user"), match.group("host")


def asset_name(os_name: str, arch: str) -> str:
    if os_name == "darwin":
        arch = {"arm64": "arm64", "aarch64": "arm64"}.get(arch, "x64")
        return f"PrecisionKVM-{__version__}-macos-{arch}.zip"
    if os_name == "windows":
        return f"PrecisionKVM-{__version__}-windows-{'arm64' if 'arm' in arch.lower() else 'x64'}.zip"
    raise ValueError(f"Unsupported remote system: {os_name}")


def mac_install_command(url: str) -> str:
    return ("set -e; mkdir -p \"$HOME/Applications\"; "
            f"curl -fL --retry 2 -o /tmp/pkvm-install.zip '{url}'; "
            "unzip -oq /tmp/pkvm-install.zip -d \"$HOME/Applications\"; "
            "rm -f /tmp/pkvm-install.zip; "
            f"xattr -dr com.apple.quarantine \"{MAC_APP}\" 2>/dev/null || true; "
            "echo installed")


def mac_start_command() -> str:
    match = "'[P]recisionKVM.app/Contents/MacOS/PrecisionKVM'"
    return (f"pkill -f {match} 2>/dev/null; sleep 1; "
            f"nohup \"{MAC_BIN}\" client --config \"{MAC_CONFIG_DIR}/client.json\" "
            f">\"{MAC_CONFIG_DIR}/client.log\" 2>&1 & "
            f"sleep 1; if pgrep -f {match} >/dev/null; "
            "then echo running; else echo not running; fi")


def win_install_command(url: str) -> str:
    return ("powershell -NoProfile -ExecutionPolicy Bypass -Command \""
            "curl.exe -fL --retry 2 -o $env:TEMP\\pkvm-install.zip "
            f"'{url}' | Out-Null; "
            "if ($LASTEXITCODE) { exit $LASTEXITCODE }; "
            "Expand-Archive -Force -LiteralPath $env:TEMP\\pkvm-install.zip "
            "-DestinationPath $env:LOCALAPPDATA\\Programs; "
            "Remove-Item $env:TEMP\\pkvm-install.zip; "
            f"Set-Content -Path '{WIN_DIR}\\VERSION.txt' -Value '{__version__}'; "
            f"if (Test-Path '{WIN_EXE}') {{ 'installed' }} else {{ exit 1 }}\"")


def win_start_command() -> str:
    return ("powershell -NoProfile -ExecutionPolicy Bypass -Command \""
            "Get-Process PrecisionKVM -ErrorAction SilentlyContinue | Stop-Process -Force; "
            f"Start-Process -FilePath '{WIN_EXE}' "
            f"-ArgumentList 'client','--config','{WIN_CONFIG}' -WindowStyle Hidden; "
            "Start-Sleep -Seconds 2; "
            "if (Get-Process PrecisionKVM -ErrorAction SilentlyContinue) "
            "{ 'running' } else { 'not running' }\"")


def installed_probe(os_name: str) -> str:
    if os_name == "darwin":
        return (f"if test -x \"{MAC_BIN}\"; then echo present; "
                "else echo missing; fi")
    return ("powershell -NoProfile -Command \""
            f"if (Test-Path '{WIN_EXE}') {{ 'present' }}\"")


def version_probe(os_name: str) -> str:
    """Print the installed bundle's version, empty when unknown."""
    if os_name == "darwin":
        return ("/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "
                f"\"{MAC_APP}/Contents/Info.plist\" 2>/dev/null || true")
    return ("powershell -NoProfile -Command \""
            f"Get-Content '{WIN_DIR}\\VERSION.txt' -ErrorAction SilentlyContinue\"")


def version_tuple(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+(?:\.\d+){1,2})", text.strip())
    if not match:
        return None
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def config_sink(os_name: str) -> str:
    """Command that reads client.json contents from stdin."""
    if os_name == "darwin":
        return (f"mkdir -p \"{MAC_CONFIG_DIR}\" && "
                f"cat > \"{MAC_CONFIG_DIR}/client.json\" && chmod 600 \"{MAC_CONFIG_DIR}/client.json\"")
    return ("powershell -NoProfile -Command \""
            "New-Item -ItemType Directory -Force "
            "$env:LOCALAPPDATA\\PrecisionKVM | Out-Null; "
            "[Console]::In.ReadToEnd() | Set-Content -Encoding ascii "
            f"-Path '{WIN_CONFIG}'\"")


def _exec(ssh, command: str, status: Callable[[str], None], stdin: str | None = None,
          timeout: int = 240) -> str:
    source, stdout, stderr = ssh.exec_command(command, timeout=timeout)
    if stdin is not None:
        source.write(stdin)
    source.channel.shutdown_write()
    out = stdout.read().decode("utf-8", "replace").strip()
    err = stderr.read().decode("utf-8", "replace").strip()
    code = stdout.channel.recv_exit_status()
    if code != 0:
        raise RuntimeError(err or out or f"remote command failed ({code})")
    if err:
        status(err.splitlines()[-1])
    return out


def _connect(target: str, port: int, password: str | None,
             ask_password: Callable[[str], str | None] | None,
             status: Callable[[str], None]):
    try:
        import paramiko
    except ImportError:
        raise RuntimeError("SSH deploy needs paramiko — install it with: pip install paramiko")
    user, host = parse_target(target)
    status(f"Connecting to {target}…")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(host, port=port, username=user, timeout=12, banner_timeout=12,
                    auth_timeout=12, look_for_keys=True, allow_agent=True,
                    password=password or None)
    except Exception as first:
        if password or not ask_password:
            raise RuntimeError(f"SSH login failed: {first}")
        retry = ask_password(f"Password for {target} (keys were not accepted)")
        if not retry:
            raise RuntimeError("SSH login cancelled — no password given")
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, port=port, username=user, timeout=12, banner_timeout=12,
                    auth_timeout=12, look_for_keys=False, allow_agent=False,
                    password=retry)
    return ssh, user, host


def _detect(ssh) -> tuple[str, str]:
    try:
        out = _exec(ssh, "uname -s; uname -m", _noop)
    except RuntimeError:
        arch = _exec(ssh, "echo %PROCESSOR_ARCHITECTURE%", _noop)
        return "windows", arch or "x64"
    lines = out.splitlines()
    if lines and "Darwin" in lines[0]:
        return "darwin", lines[1].strip() if len(lines) > 1 else "arm64"
    raise ValueError(f"Unsupported remote system: {lines[0] if lines else 'unknown'}")


def _noop(_line: str) -> None:
    pass


def deploy(target: str, ssh_port: int, settings: dict, status: Callable[[str], None],
           password: str | None = None,
           ask_password: Callable[[str], str | None] | None = None,
           install: bool = True) -> None:
    """Start a PrecisionKVM client on `user@host`, installing/upgrading it first
    when `install` is set. With install=False it only (re)writes pairing and
    launches — plain connect, no downloads."""
    ssh, _user, host = _connect(target, ssh_port, password, ask_password, status)
    try:
        os_name, arch = _detect(ssh)
        label = "macOS" if os_name == "darwin" else "Windows"
        status(f"Remote system: {label} ({arch})")
        _, _, token, fingerprint = identity()
        host_ip = local_ip(socket.gethostbyname(host))
        config = {"host": host_ip, "port": settings["port"],
                  "token": token, "fingerprint": fingerprint,
                  "tls": bool(settings.get("tls", True)), "auto": True,
                  "clipboard_enabled": bool(settings.get("clipboard_enabled", True)),
                  "clipboard_max_mb": 512}
        present = "present" in _exec(ssh, installed_probe(os_name), _noop)
        needs_install = not present
        if present and install:
            remote = version_tuple(_exec(ssh, version_probe(os_name), _noop))
            current = version_tuple(__version__)
            if remote is None or (current and remote < current):
                status(f"Remote is {'.'.join(map(str, remote)) if remote else 'unknown'}"
                       f" — upgrading to {__version__}")
                needs_install = True
            else:
                status(f"PrecisionKVM {'.'.join(map(str, remote))} already installed")
        if needs_install:
            if not install:
                raise RuntimeError("PrecisionKVM is not installed there — "
                                   "use “Install && connect” first")
            name = asset_name(os_name, arch)
            url = f"{RELEASE_URL}/v{__version__}/{name}"
            status(f"Installing {name}…")
            command = (mac_install_command if os_name == "darwin"
                       else win_install_command)(url)
            _exec(ssh, command, status)
        status("Writing pairing file…")
        _exec(ssh, config_sink(os_name), status, stdin=json.dumps(config))
        status("Starting the client…")
        start_command = (mac_start_command if os_name == "darwin"
                         else win_start_command)()
        out = _exec(ssh, start_command, status)
        if "running" not in out:
            raise RuntimeError("client process did not stay up — check the remote log")
        status(f"Done — {host} client is starting and will connect to {host_ip}")
        if os_name == "darwin":
            status("On the Mac, grant Accessibility + Input Monitoring access when asked")
    finally:
        ssh.close()
