"""Validated Windows physical-key to Mac key mappings. No OS calls."""
from __future__ import annotations

# Windows set-1 scan codes -> Apple virtual key codes.
SCAN_TO_MAC = dict(zip(
    (0x1E,0x30,0x2E,0x20,0x12,0x21,0x22,0x23,0x17,0x24,0x25,0x26,0x32,
     0x31,0x18,0x19,0x10,0x13,0x1F,0x14,0x16,0x2F,0x11,0x2D,0x15,0x2C,
     0x02,0x03,0x04,0x05,0x06,0x07,0x08,0x09,0x0A,0x0B,
     0x0C,0x0D,0x1A,0x1B,0x2B,0x27,0x28,0x29,0x33,0x34,0x35),
    (0,11,8,2,14,3,5,4,34,38,40,37,46,45,31,35,12,15,1,17,32,9,13,7,16,6,
     18,19,20,21,23,22,26,28,25,29,27,24,33,30,42,41,39,50,43,47,44)))
VK_TO_MAC = {
    0x08:51,0x09:48,0x0D:36,0x1B:53,0x20:49,0x14:57,
    0x21:116,0x22:121,0x23:119,0x24:115,0x25:123,0x26:126,
    0x27:124,0x28:125,0x2D:114,0x2E:117,
    0x5B:55,0x5C:54,0xA0:56,0xA1:60,0xA2:59,0xA3:62,0xA4:58,0xA5:61,
    0x60:82,0x61:83,0x62:84,0x63:85,0x64:86,0x65:87,0x66:88,
    0x67:89,0x68:91,0x69:92,0x6A:67,0x6B:69,0x6D:78,0x6E:65,0x6F:75,
    0x70:122,0x71:120,0x72:99,0x73:118,0x74:96,0x75:97,
    0x76:98,0x77:100,0x78:101,0x79:109,0x7A:103,0x7B:111,
}
MEDIA_KEYS = {0xAD:7,0xAE:1,0xAF:0,0xB0:17,0xB1:18,0xB2:16,0xB3:16}
VK_TO_MAC.update({0x7C:105,0x7D:107,0x7E:113,0x7F:106,0x80:64,
                  0x81:79,0x82:80,0x83:90,0x90:71})


def mac_keycode(vk: int, scan: int = 0, extended: bool = False) -> int | None:
    """Translate special VKs first so extended navigation is unambiguous."""
    if vk == 0x0D and extended:
        return 76
    if vk == 0x10:
        return 60 if scan == 0x36 else 56
    if vk == 0x11:
        return 62 if extended else 59
    if vk == 0x12:
        return 61 if extended else 58
    if vk in VK_TO_MAC:
        return VK_TO_MAC[vk]
    if scan in SCAN_TO_MAC:
        return SCAN_TO_MAC[scan]
    # Support synthetic producers that only provide a virtual-key code.
    alphabet = dict(zip(range(65,91), (0,11,8,2,14,3,5,4,34,38,40,37,46,
                                     45,31,35,12,15,1,17,32,9,13,7,16,6)))
    return alphabet.get(vk)


SOURCE_KEYS = {chr(code): code for code in range(65, 91)}
SOURCE_KEYS.update({str(n): 0x30+n for n in range(10)})
SOURCE_KEYS.update({f"F{n}": 0x6F+n for n in range(1, 25)})
SOURCE_KEYS.update({f"Numpad{n}": 0x60+n for n in range(10)})
SOURCE_KEYS.update({
    "Backspace":0x08,"Tab":0x09,"Enter":0x0D,"CapsLock":0x14,"Escape":0x1B,
    "Space":0x20,"PageUp":0x21,"PageDown":0x22,"End":0x23,"Home":0x24,
    "Left":0x25,"Up":0x26,"Right":0x27,"Down":0x28,"Insert":0x2D,"Delete":0x2E,
    "LeftWindows":0x5B,"RightWindows":0x5C,"Menu":0x5D,
    "LeftShift":0xA0,"RightShift":0xA1,"LeftCtrl":0xA2,"RightCtrl":0xA3,
    "LeftAlt":0xA4,"RightAlt":0xA5,"Multiply":0x6A,"Add":0x6B,"Subtract":0x6D,
    "Decimal":0x6E,"Divide":0x6F,"NumLock":0x90,"ScrollLock":0x91,
    "VolumeMute":0xAD,"VolumeDown":0xAE,"VolumeUp":0xAF,"NextTrack":0xB0,
    "PreviousTrack":0xB1,"StopMedia":0xB2,"PlayPause":0xB3,
    "Semicolon":0xBA,"Equal":0xBB,"Comma":0xBC,"Minus":0xBD,"Period":0xBE,
    "Slash":0xBF,"Backquote":0xC0,"LeftBracket":0xDB,"Backslash":0xDC,
    "RightBracket":0xDD,"Quote":0xDE,"PrintScreen":0x2C,"Pause":0x13,
})
TARGET_KEYS: dict[str, tuple[str, int] | None] = {"Disabled": None}
for _name, _vk in SOURCE_KEYS.items():
    _code = mac_keycode(_vk)
    if _code is not None:
        TARGET_KEYS[_name] = ("key", _code)
    elif _vk in MEDIA_KEYS:
        TARGET_KEYS[_name] = ("media", MEDIA_KEYS[_vk])
TARGET_KEYS.update({"Command":("key",55),"RightCommand":("key",54),
                    "Control":("key",59),"RightControl":("key",62),
                    "Option":("key",58),"RightOption":("key",61),
                    "Shift":("key",56),"Fn":("key",63),"NumpadEnter":("key",76),
                    "NumpadEqual":("key",81),"Help":("key",114)})
# US physical alphanumeric/punctuation positions, for VK-only user mappings.
for _vk, _code in zip(range(0x30,0x3A), (29,18,19,20,21,23,22,26,28,25)):
    TARGET_KEYS[chr(_vk)] = ("key",_code)
for _name, _code in {"Semicolon":41,"Equal":24,"Comma":43,"Minus":27,
                     "Period":47,"Slash":44,"Backquote":50,"LeftBracket":33,
                     "Backslash":42,"RightBracket":30,"Quote":39}.items():
    TARGET_KEYS[_name] = ("key",_code)
KEY_NAMES = list(SOURCE_KEYS)
MAC_KEY_NAMES = list(TARGET_KEYS)
PRESETS = ("native", "option_cmd", "ctrl_cmd", "mac_layout")


def _lookup(value: str, candidates: dict):
    folded = value.casefold().replace(" ", "").replace("_", "")
    aliases = {"esc":"Escape","ctrl":"LeftCtrl","lctrl":"LeftCtrl",
               "rctrl":"RightCtrl","alt":"LeftAlt","win":"LeftWindows",
               "command":"Command","cmd":"Command","none":"Disabled",
               "block":"Disabled","disable":"Disabled","return":"Enter"}
    canonical = aliases.get(folded,value).casefold().replace(" ", "").replace("_", "")
    for name, result in candidates.items():
        if name.casefold().replace(" ", "").replace("_", "") == canonical:
            return name, result
    raise ValueError(f"Unknown key: {value}")


def source_id(value: str | int) -> str:
    if isinstance(value, bool):
        raise ValueError("A key cannot be a boolean")
    text = str(value).strip()
    if text.casefold().startswith("scan:"):
        fields = text.split(":")
        if len(fields) != 3:
            raise ValueError("Physical source must be scan:NUMBER:0 or scan:NUMBER:1")
        scan = int(fields[1],0) if fields[1].lower().startswith("0x") else int(fields[1])
        if not 0 <= scan <= 0xFFFF or fields[2] not in ("0","1"):
            raise ValueError("Invalid scan source")
        return f"scan:{scan}:{fields[2]}"
    if isinstance(value,int) or text.lower().startswith("0x") or (text.isdigit() and len(text)>1):
        vk = int(text,16) if text.lower().startswith("0x") else int(text)
    else:
        _,vk = _lookup(text,SOURCE_KEYS)
    if not 0 <= vk <= 255:
        raise ValueError("Virtual key must be between 0 and 255")
    return f"0x{vk:02X}"


def target_name(value: str | None) -> str:
    if value is None:
        return "Disabled"
    text = str(value).strip()
    if text.lower().startswith("0x"):
        vk = int(text,16)
        name = next((name for name,code in SOURCE_KEYS.items() if code==vk),None)
        if name is None or name not in TARGET_KEYS:
            raise ValueError("Destination VK has no Mac equivalent; use mac:KEYCODE")
        return name
    if text.casefold().startswith("mac:"):
        numeric = text.split(":",1)[1]
        code = int(numeric,16) if numeric.lower().startswith("0x") else int(numeric)
        if not 0 <= code <= 127:
            raise ValueError("Mac key code must be between 0 and 127")
        return f"mac:{code}"
    name,_ = _lookup(text,TARGET_KEYS)
    return name


def normalize_keymap(mapping: dict) -> dict[str,str]:
    if not isinstance(mapping,dict) or len(mapping)>512:
        raise ValueError("Key mappings must be an object with at most 512 entries")
    return {source_id(source):target_name(target) for source,target in mapping.items()}


def effective_vk(event: dict) -> int:
    vk = int(event["vk"])
    extended = bool(event.get("extended",False))
    if vk == 0x10:
        return 0xA1 if int(event.get("scan",0))==0x36 else 0xA0
    if vk == 0x11:
        return 0xA3 if extended else 0xA2
    if vk == 0x12:
        return 0xA5 if extended else 0xA4
    return vk


class KeyMapper:
    def __init__(self,mapping=None,preset="native"):
        if preset not in PRESETS:
            raise ValueError("Invalid keymap preset")
        self.mapping = normalize_keymap(mapping or {})
        self.preset = preset

    def has_override(self,event: dict) -> bool:
        vk,scan,extended = effective_vk(event),int(event.get("scan",0)),bool(event.get("extended",False))
        return any(identifier in self.mapping for identifier in
                   (f"scan:{scan}:{int(extended)}",f"0x{vk:02X}",f"0x{int(event['vk']):02X}"))

    def target(self,event: dict) -> tuple[str,int] | None:
        vk,scan,extended = effective_vk(event),int(event.get("scan",0)),bool(event.get("extended",False))
        identifiers = (f"scan:{scan}:{int(extended)}",f"0x{vk:02X}",f"0x{int(event['vk']):02X}")
        for identifier in identifiers:
            if identifier in self.mapping:
                name = self.mapping[identifier]
                return ("key",int(name[4:])) if name.startswith("mac:") else TARGET_KEYS[name]
        if self.preset == "ctrl_cmd":
            mapped = {0xA2:55,0xA3:54,0x5B:59,0x5C:62}.get(vk)
            if mapped is not None:
                return "key",mapped
        if self.preset in ("option_cmd", "mac_layout"):
            mapped = {0x5B:58,0x5C:61,0xA4:55,0xA5:54}.get(vk)
            if mapped is not None:
                return "key",mapped
        if vk in MEDIA_KEYS:
            return "media",MEDIA_KEYS[vk]
        code = mac_keycode(vk,scan,extended)
        if code is not None:
            return "key",code
        return TARGET_KEYS.get(next((name for name,value in SOURCE_KEYS.items() if value==vk),""))
