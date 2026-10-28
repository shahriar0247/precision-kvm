import pytest

from precision_kvm.keymap import (KeyMapper, MAC_KEY_NAMES, KEY_NAMES,
                                 normalize_keymap, source_id, target_name)


def test_normalize_sources_and_native_destinations():
    assert normalize_keymap({"A":"B","0xA2":"Command",65:"mac:0x7B",
                             "scan:0x1e:0":"Disabled"}) == {
        "0x41":"mac:123","0xA2":"Command","scan:30:0":"Disabled"}
    assert source_id("1") == "0x31"
    assert source_id("17") == "0x11"
    assert target_name("0x41") == "A"
    assert "F20" in KEY_NAMES and "F20" in MAC_KEY_NAMES


@pytest.mark.parametrize("mapping",[{"BAD":"A"},{"A":"BAD"},{256:"A"},
                                   {"A":"mac:128"},{"scan:1:2":"A"},
                                   {True:"A"}])
def test_invalid_mapping_rejected(mapping):
    with pytest.raises(ValueError):
        normalize_keymap(mapping)


def test_presets_and_overrides():
    assert KeyMapper(preset="ctrl_cmd").target({"vk":0x11,"scan":0x1D}) == ("key",55)
    assert KeyMapper(preset="ctrl_cmd").target({"vk":0x5B}) == ("key",59)
    assert KeyMapper(preset="mac_layout").target({"vk":0xA4}) == ("key",55)
    assert KeyMapper(preset="mac_layout").target({"vk":0x5B}) == ("key",58)
    assert KeyMapper({"LeftCtrl":"Escape"},"ctrl_cmd").target({"vk":0x11}) == ("key",53)
    assert KeyMapper({"A":"Disabled"}).target({"vk":65}) is None


@pytest.mark.parametrize("preset",["option_cmd","mac_layout"])
def test_option_command_swap_leaves_control_unchanged(preset):
    mapper = KeyMapper(preset=preset)
    assert mapper.target({"vk":0x5B}) == ("key",58)
    assert mapper.target({"vk":0x5C}) == ("key",61)
    assert mapper.target({"vk":0xA4}) == ("key",55)
    assert mapper.target({"vk":0xA5}) == ("key",54)
    assert mapper.target({"vk":0xA2}) == ("key",59)
    assert mapper.target({"vk":0xA3}) == ("key",62)
    assert KeyMapper({"LeftAlt":"Escape"},"option_cmd").target({"vk":0xA4}) == ("key",53)


def test_scan_override_is_more_specific_than_vk():
    mapper = KeyMapper({"Enter":"Space","scan:0x1C:1":"NumpadEnter"})
    assert mapper.target({"vk":13,"scan":0x1C,"extended":True}) == ("key",76)
    assert mapper.target({"vk":13,"scan":0x1C,"extended":False}) == ("key",49)


def test_letters_digits_and_media_can_be_destinations():
    assert KeyMapper({"A":"9"}).target({"vk":65}) == ("key",25)
    assert KeyMapper({"A":"VolumeUp"}).target({"vk":65}) == ("media",0)
    assert KeyMapper({"A":"F20"}).target({"vk":65}) == ("key",90)
    assert KeyMapper().target({"vk":ord("5")}) == ("key",23)
