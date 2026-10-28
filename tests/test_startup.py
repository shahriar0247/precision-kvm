from pathlib import Path
import plistlib

from precision_kvm.startup import startup_definition


def test_source_startup_has_safe_separate_arguments_and_no_credentials():
    definition = startup_definition("/path with spaces/python", Path("client.json"),
                                    Path("source"), Path("logs"))
    assert definition["ProgramArguments"][:4] == ["/path with spaces/python", "-m", "precision_kvm", "client-gui"]
    assert definition["RunAtLoad"] is True
    assert definition["KeepAlive"] is False
    assert "token" not in str(definition)
    assert plistlib.loads(plistlib.dumps(definition)) == definition


def test_packaged_app_startup_does_not_depend_on_source_or_python_module():
    definition = startup_definition("/Applications/PrecisionKVM.app/Contents/MacOS/PrecisionKVM",
                                    Path("client.json"), Path("source"), Path("logs"), frozen=True)
    assert definition["ProgramArguments"][1] == "client-gui"
    assert "WorkingDirectory" not in definition
