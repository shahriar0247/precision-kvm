from __future__ import annotations

import pytest

from precision_kvm import __version__, deploy


def test_parse_target_accepts_user_at_host():
    assert deploy.parse_target("ahmed@192.168.50.2") == ("ahmed", "192.168.50.2")
    assert deploy.parse_target("me@my-mac.local") == ("me", "my-mac.local")


def test_parse_target_rejects_options_and_empty():
    for bad in ("-oProxyCommand=x", "", "hostonly", "@host", "user@"):
        with pytest.raises(ValueError):
            deploy.parse_target(bad)


def test_asset_name_for_remote_platforms():
    assert deploy.asset_name("darwin", "arm64") == \
        f"PrecisionKVM-{__version__}-macos-arm64.zip"
    assert deploy.asset_name("darwin", "x86_64") == \
        f"PrecisionKVM-{__version__}-macos-x64.zip"
    assert deploy.asset_name("windows", "AMD64") == \
        f"PrecisionKVM-{__version__}-windows-x64.zip"
    assert deploy.asset_name("windows", "ARM64") == \
        f"PrecisionKVM-{__version__}-windows-arm64.zip"
    with pytest.raises(ValueError):
        deploy.asset_name("linux", "x86_64")


def test_remote_commands_reference_release_and_paths():
    url = f"{deploy.RELEASE_URL}/v{__version__}/pkg.zip"
    assert url in deploy.mac_install_command(url)
    assert url in deploy.win_install_command(url)
    assert "client --config" in deploy.mac_start_command()
    assert "client" in deploy.win_start_command() and "--config" in deploy.win_start_command()
    assert "client.json" in deploy.config_sink("darwin")
    assert "client.json" in deploy.config_sink("windows")


def test_version_tuple_parses_and_pads():
    assert deploy.version_tuple("0.8.0") == (0, 8, 0)
    assert deploy.version_tuple("1.2") == (1, 2, 0)
    assert deploy.version_tuple("v0.7.0\n") == (0, 7, 0)
    assert deploy.version_tuple("") is None
    assert deploy.version_tuple("garbage") is None


def test_remote_older_than_local_needs_upgrade():
    remote = deploy.version_tuple("0.7.0")
    current = deploy.version_tuple(__version__)
    assert remote < current  # the bundled build should outrank the last release
