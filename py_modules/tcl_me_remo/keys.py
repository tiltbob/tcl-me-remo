"""Logical remote keys and the HID usages that produce them on an Android / Google TV.

A key is identified by a *spec* string.  Named keys (``WAKE``, ``DOWN``, ``HOME`` ...) cover
what a TV remote needs; raw specs reach any other usage without a code change, which matters
because the right key for a given TV firmware sometimes has to be found by experiment:

    K:52      Keyboard/Keypad page (0x07) usage 0x52
    C:0223    Consumer page (0x0C) usage 0x0223
    S:82      Generic Desktop System Control usage 0x82

The chain on the TV is: HID usage -> Linux key code (drivers/hid/hid-input.c) -> Android key
code (frameworks/base/data/keyboards/Generic.kl).  The ``android`` column below records the
Android key code each named key is expected to arrive as.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PAGE_KEYBOARD = "keyboard"
PAGE_CONSUMER = "consumer"
PAGE_SYSTEM = "system"

_PAGE_PREFIX = {"K": PAGE_KEYBOARD, "C": PAGE_CONSUMER, "S": PAGE_SYSTEM}
_PREFIX_OF_PAGE = {v: k for k, v in _PAGE_PREFIX.items()}

# Inclusive usage ranges each report in the report map can carry (see hid.py).
USAGE_RANGE = {
    PAGE_KEYBOARD: (0x04, 0xE7),
    PAGE_CONSUMER: (0x0001, 0x03FF),
    PAGE_SYSTEM: (0x81, 0x83),
}

_RAW_RE = re.compile(r"^([KCS]):(?:0X)?([0-9A-F]{1,4})$")
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class UnknownKey(ValueError):
    """The key spec is not a known name and not a valid raw usage."""


@dataclass(frozen=True)
class Key:
    spec: str          # canonical spec: the name, or e.g. "C:0223"
    page: str          # PAGE_KEYBOARD / PAGE_CONSUMER / PAGE_SYSTEM
    usage: int
    android: str = ""  # Android key code this is expected to become
    label: str = ""    # short human label


def _k(name: str, page: str, usage: int, android: str, label: str) -> Key:
    return Key(spec=name, page=page, usage=usage, android=android, label=label)


# fmt: off
_NAMED: tuple[Key, ...] = (
    # --- power (Generic Desktop System Control): discrete, not a toggle
    _k("WAKE",        PAGE_SYSTEM,   0x83,   "KEYCODE_WAKEUP",            "Wake (discrete on)"),
    _k("SLEEP",       PAGE_SYSTEM,   0x82,   "KEYCODE_SLEEP",             "Sleep (discrete off)"),
    _k("POWER",       PAGE_SYSTEM,   0x81,   "KEYCODE_POWER",             "Power (toggle)"),
    # --- navigation
    _k("UP",          PAGE_KEYBOARD, 0x52,   "KEYCODE_DPAD_UP",           "Up"),
    _k("DOWN",        PAGE_KEYBOARD, 0x51,   "KEYCODE_DPAD_DOWN",         "Down"),
    _k("LEFT",        PAGE_KEYBOARD, 0x50,   "KEYCODE_DPAD_LEFT",         "Left"),
    _k("RIGHT",       PAGE_KEYBOARD, 0x4F,   "KEYCODE_DPAD_RIGHT",        "Right"),
    _k("OK",          PAGE_CONSUMER, 0x0041, "KEYCODE_DPAD_CENTER",       "OK / Select"),
    _k("ENTER",       PAGE_KEYBOARD, 0x28,   "KEYCODE_ENTER",             "Enter"),
    _k("BACK",        PAGE_CONSUMER, 0x0224, "KEYCODE_BACK",              "Back"),
    _k("ESC",         PAGE_KEYBOARD, 0x29,   "KEYCODE_ESCAPE",            "Escape"),
    _k("HOME",        PAGE_CONSUMER, 0x0223, "KEYCODE_HOME",              "Home"),
    _k("MENU",        PAGE_CONSUMER, 0x0040, "KEYCODE_MENU",              "Menu"),
    # --- TV functions
    _k("TV",          PAGE_CONSUMER, 0x0089, "KEYCODE_TV",                "TV"),
    _k("SETTINGS",    PAGE_CONSUMER, 0x019F, "KEYCODE_SETTINGS",          "Settings"),
    _k("INFO",        PAGE_CONSUMER, 0x0060, "KEYCODE_INFO",              "Info"),
    _k("GUIDE",       PAGE_CONSUMER, 0x008D, "KEYCODE_GUIDE",             "Guide"),
    _k("SEARCH",      PAGE_CONSUMER, 0x0221, "KEYCODE_SEARCH",            "Search"),
    _k("CH_UP",       PAGE_CONSUMER, 0x009C, "KEYCODE_CHANNEL_UP",        "Channel up"),
    _k("CH_DOWN",     PAGE_CONSUMER, 0x009D, "KEYCODE_CHANNEL_DOWN",      "Channel down"),
    # --- audio / media
    _k("VOL_UP",      PAGE_CONSUMER, 0x00E9, "KEYCODE_VOLUME_UP",         "Volume up"),
    _k("VOL_DOWN",    PAGE_CONSUMER, 0x00EA, "KEYCODE_VOLUME_DOWN",       "Volume down"),
    _k("MUTE",        PAGE_CONSUMER, 0x00E2, "KEYCODE_VOLUME_MUTE",       "Mute"),
    _k("PLAY_PAUSE",  PAGE_CONSUMER, 0x00CD, "KEYCODE_MEDIA_PLAY_PAUSE",  "Play / pause"),
    _k("STOP",        PAGE_CONSUMER, 0x00B7, "KEYCODE_MEDIA_STOP",        "Stop"),
    _k("NEXT",        PAGE_CONSUMER, 0x00B5, "KEYCODE_MEDIA_NEXT",        "Next"),
    _k("PREVIOUS",    PAGE_CONSUMER, 0x00B6, "KEYCODE_MEDIA_PREVIOUS",    "Previous"),
    _k("REWIND",      PAGE_CONSUMER, 0x00B4, "KEYCODE_MEDIA_REWIND",      "Rewind"),
    _k("FAST_FORWARD", PAGE_CONSUMER, 0x00B3, "KEYCODE_MEDIA_FAST_FORWARD", "Fast forward"),
    # --- digits and function keys (keyboard page)
    _k("NUM0", PAGE_KEYBOARD, 0x27, "KEYCODE_0", "0"),
    *(_k(f"NUM{n}", PAGE_KEYBOARD, 0x1D + n, f"KEYCODE_{n}", str(n)) for n in range(1, 10)),
    *(_k(f"F{n}", PAGE_KEYBOARD, 0x39 + n, f"KEYCODE_F{n}", f"F{n}") for n in range(1, 13)),
)
# fmt: on

NAMED: dict[str, Key] = {key.spec: key for key in _NAMED}

# Friendly aliases accepted on input; the canonical name is what gets stored.
ALIASES: dict[str, str] = {
    "WAKEUP": "WAKE",
    "ON": "WAKE",
    "OFF": "SLEEP",
    "SELECT": "OK",
    "CENTER": "OK",
    "RETURN": "ENTER",
    "ESCAPE": "ESC",
    "VOLUME_UP": "VOL_UP",
    "VOLUME_DOWN": "VOL_DOWN",
    "VOL+": "VOL_UP",
    "VOL-": "VOL_DOWN",
    "PLAY": "PLAY_PAUSE",
    "PAUSE": "PLAY_PAUSE",
    "PREV": "PREVIOUS",
    "FF": "FAST_FORWARD",
    "CHANNEL_UP": "CH_UP",
    "CHANNEL_DOWN": "CH_DOWN",
}


def is_key_token(token: str) -> bool:
    """True if ``token`` is shaped like a key spec (named or raw), valid or not."""
    upper = token.strip().upper()
    return bool(_RAW_RE.match(upper) or _NAME_RE.match(upper) or upper in ALIASES or upper in NAMED)


def resolve(spec: str) -> Key:
    """Turn a spec (any case, aliases allowed) into a :class:`Key`, or raise UnknownKey."""
    upper = str(spec).strip().upper()
    if not upper:
        raise UnknownKey("empty key name")
    upper = ALIASES.get(upper, upper)
    named = NAMED.get(upper)
    if named is not None:
        return named
    raw = _RAW_RE.match(upper)
    if raw:
        page = _PAGE_PREFIX[raw.group(1)]
        usage = int(raw.group(2), 16)
        low, high = USAGE_RANGE[page]
        if not low <= usage <= high:
            raise UnknownKey(
                f"usage 0x{usage:X} is outside the {page} range 0x{low:X}-0x{high:X} in key {spec!r}"
            )
        width = 4 if page == PAGE_CONSUMER else 2
        return Key(spec=f"{raw.group(1)}:{usage:0{width}X}", page=page, usage=usage, label=f"raw {page} 0x{usage:X}")
    raise UnknownKey(
        f"unknown key {spec!r} (use a name such as HOME, DOWN, OK, WAKE, SLEEP or a raw usage like C:0223, K:52, S:82)"
    )


def catalog() -> list[dict[str, str]]:
    """All named keys, for UIs and ``tcl-me-remo keys``."""
    return [
        {
            "name": key.spec,
            "label": key.label,
            "page": key.page,
            "usage": f"0x{key.usage:04X}" if key.page == PAGE_CONSUMER else f"0x{key.usage:02X}",
            "android": key.android,
        }
        for key in _NAMED
    ]
