import json
import threading

import pytest

from precision_kvm.client_ui import ClientController


def config_path(tmp_path):
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"host":"pc.local","port":24819,
                                "fingerprint":"a"*64,"token":"private-pairing"*4}))
    return path


def test_worker_start_stop_and_duplicate_launch(tmp_path):
    entered = threading.Event()

    def runner(config, stop_event, status):
        status("Connected")
        entered.set()
        stop_event.wait(2)

    controller = ClientController(config_path(tmp_path), runner)
    assert controller.start()
    assert entered.wait(1)
    assert controller.active
    assert not controller.start()
    controller.stop()
    controller.worker.join(1)
    assert not controller.active
    messages = list(controller.messages.queue)
    assert "Connected" in messages
    assert messages[-1] == "Disconnected - client stopped"


def test_host_edit_preserves_pairing_and_rejects_commands(tmp_path):
    controller = ClientController(config_path(tmp_path))
    controller.save_host("192.168.1.2")
    assert controller.configuration()["host"] == "192.168.1.2"
    assert controller.configuration()["token"] == "private-pairing"*4
    with pytest.raises(ValueError):
        controller.save_host("host; bad-command")


def test_status_queue_is_bounded_and_keeps_recent_updates(tmp_path):
    controller = ClientController(config_path(tmp_path))
    for index in range(1000):
        controller.post_status(str(index))
    assert controller.messages.qsize() == 128
    assert list(controller.messages.queue)[-1] == "999"


def test_runner_failure_is_visible_without_credentials(tmp_path):
    def runner(*args, **kwargs):
        raise RuntimeError("Accessibility unavailable")

    controller = ClientController(config_path(tmp_path), runner)
    controller.start()
    controller.worker.join(1)
    messages = "\n".join(controller.messages.queue)
    assert "Accessibility unavailable" in messages
    assert "private-pairing" not in messages


def test_first_launch_without_pairing_is_safe(tmp_path):
    controller = ClientController(tmp_path / "new-client.json")
    assert not controller.paired
    assert not controller.start()
    assert not controller.active
    assert "Import" in controller.messages.get_nowait()


def test_import_pairing_does_not_import_unexpected_preferences(tmp_path):
    source = config_path(tmp_path)
    imported = json.loads(source.read_text())
    imported["local_smooth_scroll"] = True
    source.write_text(json.dumps(imported))
    controller = ClientController(tmp_path / "local" / "client.json")
    controller.import_pairing(source)
    assert controller.paired
    assert "local_smooth_scroll" not in controller.configuration()


def test_save_tweaks_validates_values_and_keeps_credentials(tmp_path):
    controller = ClientController(config_path(tmp_path))
    values = {"smooth_scroll":True,"smooth_scroll_ms":"40","smooth_scroll_hz":"120",
              "scroll_gain_x":"1","scroll_gain_y":"2","keymap_preset":"option_cmd",
              "windows_home_end":True,"windows_word_nav":True,"local_smooth_scroll":False}
    controller.save_tweaks(values)
    assert controller.configuration()["tweaks"]["scroll_gain_y"] == 2
    assert controller.paired
    with pytest.raises(ValueError):
        controller.save_tweaks(dict(values,smooth_scroll_ms="nan"))
