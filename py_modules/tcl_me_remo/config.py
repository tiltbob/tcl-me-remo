"""Configuration model.

The whole configuration is one JSON document (see :func:`default_config` for the shape).
It is deliberately flat-ish and boring so that the Decky panel, the CLI and a text editor
can all edit it.

* :func:`load` never raises for a missing or damaged file: it falls back to defaults and
  reports the problem, because the service must still come up after a bad edit.
  :func:`load_for_service` also moves a file aside of which nothing could be used, so that
  the next save does not replace the user's settings with defaults.
* :func:`apply_patch` is the only way callers change settings at runtime.  It is strict:
  unknown keys and out-of-range values raise :class:`ConfigError` naming the offending path.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_type_hints

CONFIG_VERSION = 1

_MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
# What a Bluetooth LE advertisement has room for (bluez/advert.py, MAX_NAME_BYTES).
DEVICE_NAME_BYTES = 26
# A value quoted in an error message, and a line of the problem list, are cut to these
# lengths: both are kept and sent to every client that asks for the status.
_QUOTE_CHARS = 60
_PROBLEM_CHARS = 400
_MAX_PROBLEMS = 20
# Settings that decide how long the input macro takes; see apply_patch.
_MACRO_RUNTIME_SETTINGS = (
    ("input", "macro"),
    ("input", "preset"),
    ("input", "hdmi_port"),
    ("bluetooth", "key_press_ms"),
    ("bluetooth", "key_gap_ms"),
)


class ConfigError(ValueError):
    """A configuration value is invalid.  ``path`` is dotted, e.g. ``input.hdmi_port``."""

    def __init__(self, path: str, message: str):
        # The path of a setting that does not exist is whatever the caller sent: named cut
        # short, like any value (no real path comes near the length).
        shown = path if len(path) <= _QUOTE_CHARS else path[: _QUOTE_CHARS - 3] + "..."
        super().__init__(f"{shown}: {message}" if path else message)
        self.path = path
        self.message = message


def _meta(
    *,
    choices: tuple[str, ...] | None = None,
    min: float | None = None,
    max: float | None = None,
    maxlen: int | None = None,
    check: str | None = None,
    help: str = "",
) -> dict[str, Any]:
    return {
        "choices": choices,
        "min": min,
        "max": max,
        "maxlen": maxlen,
        "check": check,
        "help": help,
    }


@dataclass
class EventsConfig:
    """What to do on each lifecycle event of the SteamOS machine."""

    boot_tv_on: bool = field(default=True, metadata=_meta(help="Turn the TV on when the machine boots."))
    boot_switch_input: bool = field(default=True, metadata=_meta(help="Switch the TV input after boot."))
    resume_tv_on: bool = field(default=True, metadata=_meta(help="Turn the TV on when the machine wakes from suspend."))
    resume_switch_input: bool = field(default=True, metadata=_meta(help="Switch the TV input after wake."))
    suspend_tv_off: bool = field(default=True, metadata=_meta(help="Turn the TV off when the machine suspends."))
    shutdown_tv_off: bool = field(default=True, metadata=_meta(help="Turn the TV off when the machine powers off."))
    reboot_tv_off: bool = field(default=False, metadata=_meta(help="Turn the TV off when the machine reboots."))


@dataclass
class DisplayConfig:
    """Gate that stops a handheld from poking the TV while it is not plugged into it."""

    require_external: str = field(
        default="auto",
        metadata=_meta(
            choices=("auto", "yes", "no"),
            help="auto: require an external display only if the machine has a built-in panel.",
        ),
    )
    edid_match: str = field(
        default="",
        metadata=_meta(
            maxlen=64,
            help="If set, only act while a connected display's EDID maker/name contains this text.",
        ),
    )
    wait_seconds: float = field(
        default=5.0,
        metadata=_meta(min=0, max=60, help="How long to wait after boot/wake for the display to be detected."),
    )


@dataclass
class PowerConfig:
    on_key: str = field(
        default="WAKE",
        metadata=_meta(
            maxlen=32,
            check="key",
            help="Key sent to turn the TV on. If it is a power toggle (POWER or the same usage written raw), "
            "start-up and wake send it only when the last power key this service sent turned the TV off; "
            "'TV on' by hand always sends it.",
        ),
    )
    off_key: str = field(
        default="SLEEP",
        metadata=_meta(
            maxlen=32,
            check="key",
            help="Key sent to turn the TV off. A power toggle turns a TV that is already off on.",
        ),
    )
    on_repeat: int = field(
        default=1,
        metadata=_meta(min=1, max=5, help="How many times to send the on key. A power toggle is sent once."),
    )
    off_repeat: int = field(
        default=1,
        metadata=_meta(
            min=1,
            max=5,
            help="How many times to send the off key. A power toggle is sent once. Before suspend and shutdown "
            "only the repeats that fit into the time the machine waits are sent.",
        ),
    )
    repeat_gap: float = field(
        default=3.0,
        metadata=_meta(
            min=0.1,
            max=10,
            help="Seconds between repeats. Before suspend and shutdown the machine waits about 3.5 seconds in "
            "all, so a repeat of the off key only goes out there with a gap well under 3 seconds.",
        ),
    )


@dataclass
class InputConfig:
    method: str = field(
        default="none",
        metadata=_meta(
            choices=("macro", "wifi", "none"),
            help="How the TV input is switched. Starts as 'none': try a macro from the UI first.",
        ),
    )
    hdmi_port: int = field(default=1, metadata=_meta(min=1, max=4, help="HDMI port the SteamOS machine is on."))
    delay_after_wake: float = field(
        default=8.0,
        metadata=_meta(min=0, max=60, help="Seconds to wait after the on key before switching input."),
    )
    preset: str = field(
        default="tcl-row",
        metadata=_meta(check="preset", help="Name of a built-in macro preset, or 'custom' to use 'macro'."),
    )
    macro: str = field(
        default="",
        metadata=_meta(maxlen=1024, check="macro", help="Custom key macro, e.g. 'HOME 1s DOWN*2 OK'."),
    )
    wifi_fallback_to_macro: bool = field(
        default=False,
        metadata=_meta(
            help="If Wi-Fi input switching fails, play the Bluetooth macro instead. Off unless you turn it "
            "on: the macro presses keys blindly, so watch it work on this TV first."
        ),
    )


@dataclass
class BluetoothConfig:
    adapter: str = field(
        default="auto",
        metadata=_meta(
            maxlen=16,
            check="adapter",
            help="'auto' or an adapter name such as hci0. A change takes effect when the service restarts.",
        ),
    )
    device_name: str = field(
        default="SteamOS Remote",
        metadata=_meta(
            maxlen=DEVICE_NAME_BYTES,
            check="device_name",
            help="Name the TV shows while pairing. At most 26 bytes, which is fewer than 26 characters "
            "when some are not plain ASCII.",
        ),
    )
    tv_address: str = field(default="", metadata=_meta(check="mac", help="Address of the bonded TV (set by pairing)."))
    tv_name: str = field(
        default="",
        metadata=_meta(maxlen=64, check="outside_name", help="Name of the bonded TV (set by pairing)."),
    )
    connect_timeout: float = field(
        default=30.0,
        metadata=_meta(min=1, max=120, help="Seconds to wait for the TV to connect when turning it on."),
    )
    off_connect_timeout: float = field(
        default=3.0,
        metadata=_meta(min=0, max=30, help="Seconds to wait for the TV to connect when turning it off."),
    )
    settle_ms: int = field(
        default=1200,
        metadata=_meta(min=0, max=10000, help="Pause after the TV connects before the first key is sent."),
    )
    resubscribe_wait: float = field(
        default=6.0,
        metadata=_meta(
            min=0,
            max=30,
            help="After a reboot, seconds to wait for the TV to re-subscribe before using direct delivery.",
        ),
    )
    raw_notify: str = field(
        default="auto",
        metadata=_meta(
            choices=("auto", "on", "off", "always"),
            help="Direct delivery: send keys straight to the radio when BlueZ has lost the TV's subscription "
            "(after a reboot; Android 11 to 13). 'auto' uses it only where it has no side effects, on Linux "
            "6.17 and later. 'on' uses it on any kernel; on older ones it can disturb the kernel's Bluetooth "
            "packet accounting, which the service repairs afterwards (unverified). 'off' never uses it: such "
            "a TV then has to be paired again after each reboot. 'always' sends every key this way "
            "(diagnostic).",
        ),
    )
    reconnect_on_drop: bool = field(
        default=True,
        metadata=_meta(
            help="Keep calling the TV whenever it is not connected, so the link is up when it is needed. "
            "Turn off if the TV wakes up by itself; turning the TV off then gets a second attempt when the "
            "first could not reach it, so a TV that does not answer can hold a suspend for about 10 s and a "
            "shutdown for about 12 s."
        ),
    )
    wake_beacon: str = field(
        default="off",
        metadata=_meta(
            choices=("off", "mediatek", "custom"),
            help="Experimental: while turning the TV on, also broadcast the advertisement a suspended "
            "MediaTek TV listens for ('mediatek'), or the bytes in wake_beacon_data ('custom').",
        ),
    )
    wake_beacon_data: str = field(
        default="",
        metadata=_meta(
            check="hexdata",
            help="Manufacturer data for wake_beacon=custom, as hex in on-air order (company id first), "
            "for example a pattern captured from the TV's own remote.",
        ),
    )
    idle_disconnect_seconds: float = field(
        default=0.0,
        metadata=_meta(
            min=0,
            max=3600,
            help="Drop the link after this long without a key (0 = keep it). Helps adapters that cannot "
            "reconnect Bluetooth LE controllers while the TV is connected. When set, turning the TV off gets "
            "a second attempt when the first could not reach it, so a TV that does not answer can hold a "
            "suspend for about 10 s and a shutdown for about 12 s.",
        ),
    )
    gatt_security: str = field(
        default="encrypt",
        metadata=_meta(
            choices=("encrypt", "plain"),
            help="'plain' drops the encryption requirement from the HID attributes (bring-up aid; "
            "takes effect when the service restarts).",
        ),
    )
    key_press_ms: int = field(default=80, metadata=_meta(min=10, max=1000, help="How long a key tap is held."))
    key_gap_ms: int = field(default=350, metadata=_meta(min=20, max=5000, help="Pause between macro keys."))
    power_on_adapter: bool = field(
        default=False,
        metadata=_meta(help="Turn the Bluetooth adapter on if it is off when the TV must be reached."),
    )


@dataclass
class WifiConfig:
    """Opt-in assist over the Google TV remote protocol (Android TV Remote v2)."""

    enabled: bool = field(default=False, metadata=_meta(help="Enable the Wi-Fi assist."))
    host: str = field(default="", metadata=_meta(maxlen=253, check="host", help="IP address or hostname of the TV."))
    name: str = field(default="", metadata=_meta(maxlen=64, check="outside_name", help="Friendly name of the TV."))
    input_mode: str = field(
        default="app_link",
        metadata=_meta(
            choices=("app_link", "keycode"),
            help="app_link opens the TV-input link (reported to work on TCL sets); keycode sends the HDMI "
            "key, which TCL sets are reported to ignore.",
        ),
    )
    link_preset: str = field(
        default="tcl-mediatek",
        metadata=_meta(check="link_preset", help="Which built-in set of TV-input links to use, or 'custom'."),
    )
    app_link: str = field(
        default="",
        metadata=_meta(maxlen=512, help="Custom app link when link_preset is 'custom'; {port} is the HDMI number."),
    )
    power_fallback: bool = field(
        default=True,
        metadata=_meta(help="Use Wi-Fi for power on/off when the Bluetooth link is not available."),
    )
    connect_timeout: float = field(
        default=12.0,
        metadata=_meta(min=1, max=60, help="Seconds to keep trying to reach the TV over Wi-Fi."),
    )


@dataclass
class Config:
    version: int = field(
        default=CONFIG_VERSION,
        metadata=_meta(min=1, max=CONFIG_VERSION, help="Format version of the settings document."),
    )
    enabled: bool = field(default=True, metadata=_meta(help="Master switch for all automatic actions."))
    log_level: str = field(
        default="INFO",
        metadata=_meta(choices=("DEBUG", "INFO", "WARNING", "ERROR"), help="How much the service writes to its log."),
    )
    events: EventsConfig = field(default_factory=EventsConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    power: PowerConfig = field(default_factory=PowerConfig)
    input: InputConfig = field(default_factory=InputConfig)
    bluetooth: BluetoothConfig = field(default_factory=BluetoothConfig)
    wifi: WifiConfig = field(default_factory=WifiConfig)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def copy(self) -> Config:
        return copy.deepcopy(self)


# --------------------------------------------------------------------------- validation


def _short(value: Any) -> str:
    """``repr(value)`` for an error message, cut short: such messages are stored and passed on."""
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 - e.g. nested too deeply to print
        text = f"<{type(value).__name__}>"
    return text if len(text) <= _QUOTE_CHARS else text[: _QUOTE_CHARS - 3] + "..."


def _is_control(char: str) -> bool:
    """C0 and C1 control characters (and DEL): never part of a name."""
    code = ord(char)
    return code < 0x20 or 0x7F <= code <= 0x9F


def clean_name(text: str, limit: int) -> str:
    """Make a name that came from outside (the TV, the network) fit to store, show and log.

    Control characters and characters that cannot be encoded become spaces, runs of white
    space become one space, and the result is cut to ``limit`` characters.  Such a name is
    never a reason to refuse a setting: it arrives together with the TV's address.
    """
    kept = "".join(" " if _is_control(ch) or 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in text[: 4 * limit + 64])
    return " ".join(kept.split())[:limit].rstrip()


def _check_special(kind: str, path: str, value: Any) -> Any:
    """Checks that need knowledge from other modules; imported lazily to avoid cycles."""
    if kind == "nonempty":
        if not str(value).strip():
            raise ConfigError(path, "must not be empty")
        return value
    if kind == "device_name":
        # It goes on the air in the advertisement and over D-Bus to BlueZ: a NUL cannot be
        # sent to BlueZ at all, and the advertisement then fails for good.
        if not value.strip():
            raise ConfigError(path, "must not be empty")
        if any(_is_control(ch) for ch in value):
            raise ConfigError(path, "must not contain control characters (line breaks, escapes and the like)")
        size = len(value.encode("utf-8"))
        if size > DEVICE_NAME_BYTES:
            raise ConfigError(path, f"must be at most {DEVICE_NAME_BYTES} bytes in UTF-8 (this name takes {size})")
        return value
    if kind == "outside_name":
        return value  # cleaned in _coerce, before the length check
    if kind == "mac":
        if value == "":
            return value
        norm = str(value).strip().upper().replace("-", ":")
        if not _MAC_RE.match(norm):
            raise ConfigError(path, "must be a Bluetooth address like AA:BB:CC:DD:EE:FF (or empty)")
        return norm
    if kind == "adapter":
        if value != "auto" and not re.fullmatch(r"hci[0-9]{1,3}", str(value)):
            raise ConfigError(path, "must be 'auto' or an adapter name like hci0")
        return value
    if kind == "host":
        text = str(value).strip()
        if text and not re.match(r"^[A-Za-z0-9._:\-\[\]]+$", text):
            raise ConfigError(path, "must be an IP address or hostname")
        return text
    if kind == "key":
        from . import keys

        try:
            return keys.resolve(str(value)).spec
        except keys.UnknownKey as exc:
            raise ConfigError(path, str(exc)) from None
    if kind == "macro":
        from . import macro

        if str(value).strip() == "":
            return ""
        try:
            return macro.validate(str(value))
        except macro.MacroError as exc:
            raise ConfigError(path, str(exc)) from None
    if kind == "hexdata":
        text = re.sub(r"[\s:,-]", "", str(value)).lower()
        if text.startswith("0x"):
            text = text[2:]
        if text == "":
            return ""
        if len(text) % 2 or not re.fullmatch(r"[0-9a-f]+", text):
            raise ConfigError(path, "must be hexadecimal bytes, for example 4600 00 a1b2c3d4e5f6")
        # One legacy advertisement holds 31 bytes; a manufacturer-data field has 2 of overhead.
        if not 3 <= len(text) // 2 <= 29:
            raise ConfigError(path, "must be between 3 and 29 bytes (2 bytes company id, then the data)")
        return text
    if kind == "link_preset":
        from . import tvdata

        if value != "custom" and value not in tvdata.LINK_PRESETS:
            names = ", ".join(["custom", *tvdata.LINK_PRESETS])
            raise ConfigError(path, f"unknown link preset {_short(value)} (choose one of: {names})")
        return value
    if kind == "preset":
        from . import macro

        if value != "custom" and value not in macro.PRESETS:
            names = ", ".join(["custom", *macro.PRESETS])
            raise ConfigError(path, f"unknown preset {_short(value)} (choose one of: {names})")
        return value
    raise AssertionError(f"unknown check {kind!r}")


def _coerce(path: str, typ: Any, meta: dict[str, Any], value: Any) -> Any:
    if typ is bool:
        if not isinstance(value, bool):
            raise ConfigError(path, f"must be true or false, not {_short(value)}")
    elif typ is int:
        # NaN and the infinities are floats too (JSON as Python reads it has them, and 1e999
        # is one), and int() raises on them: look before converting.
        whole = isinstance(value, int) or (isinstance(value, float) and math.isfinite(value) and value == int(value))
        if isinstance(value, bool) or not whole:
            raise ConfigError(path, f"must be a whole number, not {_short(value)}")
        value = int(value)
    elif typ is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(path, f"must be a number, not {_short(value)}")
        try:
            value = float(value)
        except OverflowError:  # a whole number too large for a float
            raise ConfigError(path, "must be a finite number") from None
        if not math.isfinite(value):
            raise ConfigError(path, "must be a finite number")
    elif typ is str:
        if not isinstance(value, str):
            raise ConfigError(path, f"must be text, not {_short(value)}")
        if meta.get("check") == "outside_name":
            value = clean_name(value, meta.get("maxlen") or 64)
        else:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                # A lone surrogate (JSON can spell one): it could be kept in memory but not
                # written to a log, sent over D-Bus or shown.
                raise ConfigError(path, "must be text that can be written as UTF-8") from None
    else:  # pragma: no cover - guarded by the dataclass definitions above
        raise AssertionError(f"unsupported field type {typ!r} at {path}")

    if meta.get("choices") is not None and value not in meta["choices"]:
        raise ConfigError(path, f"must be one of {', '.join(meta['choices'])}, not {_short(value)}")
    if meta.get("min") is not None and value < meta["min"]:
        raise ConfigError(path, f"must be at least {meta['min']}")
    if meta.get("max") is not None and value > meta["max"]:
        raise ConfigError(path, f"must be at most {meta['max']}")
    if meta.get("maxlen") is not None and len(value) > meta["maxlen"]:
        raise ConfigError(path, f"must be at most {meta['maxlen']} characters")
    if meta.get("check"):
        value = _check_special(meta["check"], path, value)
    return value


def _build(cls: type, data: Any, path: str, *, strict: bool, problems: list[str]) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(path, "must be an object")
    hints = get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    for key in data:
        if key not in known:
            where = f"{path}.{key}" if path else str(key)
            if strict:
                raise ConfigError(where, "unknown setting")
            problems.append(f"{where}: unknown setting ignored")
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        where = f"{path}.{f.name}" if path else f.name
        typ = hints[f.name]
        if f.name not in data:
            continue
        raw = data[f.name]
        try:
            if is_dataclass(typ):
                kwargs[f.name] = _build(typ, raw, where, strict=strict, problems=problems)
            else:
                kwargs[f.name] = _coerce(where, typ, dict(f.metadata), raw)
        except ConfigError as exc:
            if strict:
                raise
            problems.append(f"{exc} (default used)")
        except (ValueError, ArithmeticError, TypeError, RecursionError) as exc:
            # A check that tripped over the value instead of judging it.  Still the value's
            # fault, and never a reason for the caller to fall over: load() must not raise.
            problem = ConfigError(where, f"is not a usable value ({type(exc).__name__})")
            if strict:
                raise problem from None
            problems.append(f"{problem} (default used)")
    return cls(**kwargs)


def from_dict(data: Any, *, strict: bool = True) -> tuple[Config, list[str]]:
    """Build a :class:`Config`.

    With ``strict`` any problem raises :class:`ConfigError`.  Without it, bad or unknown
    entries are replaced by defaults and described in the returned list.
    """
    problems: list[str] = []
    cfg = _build(Config, data, "", strict=strict, problems=problems)
    return cfg, problems


def default_config() -> Config:
    return Config()


def macro_runtime_problem(cfg: Config) -> str:
    """Why the input macro that ``cfg`` selects cannot be played ("" if it can).

    This is the check that needs more than one setting: how long the macro runs depends on
    the macro (or preset), the HDMI port and the key timings, and macros over
    ``macro.MAX_TOTAL_SECONDS`` are refused when they are due.
    """
    from . import macro

    preset = macro.PRESETS.get(cfg.input.preset)
    template = preset.template if preset is not None else cfg.input.macro
    try:
        steps = macro.parse(macro.render(template, cfg.input.hdmi_port))
        macro.check_runnable(
            steps, key_press=cfg.bluetooth.key_press_ms / 1000.0, key_gap=cfg.bluetooth.key_gap_ms / 1000.0
        )
    except macro.MacroError as exc:
        return str(exc)
    return ""


def apply_patch(cfg: Config, patch: Any) -> Config:
    """Return a new validated config with ``patch`` (a nested dict of changes) merged in.

    A patch that touches the input macro, the preset, the HDMI port or the key timings must
    leave a macro that can be played in the time allowed.  Other patches are not held to
    that: a combination that is already over the limit (an older file, say) is reported in
    the status, and must not make every unrelated change fail.
    """
    if not isinstance(patch, dict):
        raise ConfigError("", "patch must be an object")

    def merge(base: dict[str, Any], delta: dict[str, Any], path: str) -> dict[str, Any]:
        out = dict(base)
        for key, value in delta.items():
            where = f"{path}.{key}" if path else str(key)
            if key not in base:
                raise ConfigError(where, "unknown setting")
            if isinstance(base[key], dict):
                if not isinstance(value, dict):
                    raise ConfigError(where, "must be an object")
                out[key] = merge(base[key], value, where)
            else:
                out[key] = value
        return out

    merged = merge(cfg.to_dict(), patch, "")
    new_cfg, _ = from_dict(merged, strict=True)
    touched = [
        f"{section}.{name}"
        for section, name in _MACRO_RUNTIME_SETTINGS
        if isinstance(patch.get(section), dict) and name in patch[section]
    ]
    if touched:
        problem = macro_runtime_problem(new_cfg)
        if problem:
            about_macro = touched[0].startswith("input.")
            raise ConfigError(touched[0], problem if about_macro else f"with this value the input {problem}")
    return new_cfg


def set_path(cfg: Config, dotted: str, value: Any) -> Config:
    """Convenience for the CLI: ``set_path(cfg, "input.hdmi_port", 2)``."""
    parts = [p for p in dotted.split(".") if p]
    if not parts:
        raise ConfigError("", "empty setting name")
    patch: dict[str, Any] = {}
    cursor = patch
    for part in parts[:-1]:
        cursor[part] = {}
        cursor = cursor[part]
    cursor[parts[-1]] = value
    return apply_patch(cfg, patch)


def describe() -> dict[str, Any]:
    """Machine-readable description of every setting (type, limits, help) for UIs."""

    def walk(cls: type) -> dict[str, Any]:
        hints = get_type_hints(cls)
        out: dict[str, Any] = {}
        for f in fields(cls):
            typ = hints[f.name]
            if is_dataclass(typ):
                out[f.name] = walk(typ)
            else:
                meta = dict(f.metadata)
                out[f.name] = {
                    "type": typ.__name__,
                    "default": f.default,
                    **{k: v for k, v in meta.items() if v not in (None, "") and k != "check"},
                }
        return out

    return walk(Config)


# --------------------------------------------------------------------------- persistence


BROKEN_SUFFIX = ".broken"


def _cut(text: str, limit: int = _PROBLEM_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _read(path: Path) -> tuple[Config, list[str], bool]:
    """Load ``path``.  Returns the config, the problems, and whether the file was there but unusable."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return default_config(), [], False
    except OSError as exc:
        return default_config(), [f"could not read {path}: {exc}; using defaults"], True
    try:
        # utf-8-sig: a byte order mark, which some editors put in front, is not damage.
        data = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        return default_config(), [f"{path} is not UTF-8 text ({exc.reason} at byte {exc.start}); using defaults"], True
    except RecursionError:
        return default_config(), [f"{path} is not valid JSON (nested too deeply); using defaults"], True
    except ValueError as exc:
        return default_config(), [f"{path} is not valid JSON ({_cut(str(exc), 200)}); using defaults"], True
    try:
        cfg, problems = from_dict(data, strict=False)
    except ConfigError as exc:  # not an object at the top
        return default_config(), [f"{path}: {exc}; using defaults"], True
    except Exception as exc:  # noqa: BLE001 - a bug in a check must not keep the service from starting
        return default_config(), [f"{path} could not be used ({type(exc).__name__}: {exc}); using defaults"], True
    if len(problems) > _MAX_PROBLEMS:
        problems = [*problems[:_MAX_PROBLEMS], f"... and {len(problems) - _MAX_PROBLEMS} more problems in {path}"]
    return cfg, problems, False


def load(path: Path) -> tuple[Config, list[str]]:
    """Load the config file, tolerating absence and damage.  Never raises; changes nothing.

    Returns the config and a list of human-readable problems (empty when all is well).
    """
    try:
        cfg, problems, _ = _read(Path(path))
    except Exception as exc:  # noqa: BLE001 - see the module docstring: never, whatever the file holds
        cfg, problems = default_config(), [f"{path} could not be loaded ({type(exc).__name__}: {exc}); using defaults"]
    return cfg, [_cut(problem) for problem in problems]


def set_aside(path: Path) -> str:
    """Move a settings file that could not be used out of the way.  Never raises.

    It goes next to the original as ``config.json.broken`` (replacing an older one), where
    the next save cannot touch it.  Returns a sentence saying where it is, or why it is
    still in place.
    """
    path = Path(path)
    kept = path.with_name(path.name + BROKEN_SUFFIX)
    try:
        os.replace(path, kept)
    except OSError as exc:
        return f"It could not be moved out of the way ({exc}): the next change of a setting overwrites it."
    return (
        f"The file was kept as {kept}; settings are saved to a new {path.name} from now on. "
        f"To get the old settings back, mend that file, move it back to {path.name} and restart the service."
    )


def left_aside(path: Path) -> str:
    """The reminder for a start with no settings file and a kept one beside it ("" otherwise).

    That is how the service starts the next time after :func:`set_aside` if no setting was
    changed in between (after a reboot, say): on defaults, with the paired TV gone, and the
    notice of the first start gone too.  A later start no longer says it once the kept file
    has been moved back or deleted, or settings have been saved (which makes a new file).
    """
    path = Path(path)
    kept = path.with_name(path.name + BROKEN_SUFFIX)
    try:
        if path.exists() or path.is_symlink() or not kept.is_file():
            return ""
    except OSError:
        return ""
    return (
        f"The service started on default settings: there was no {path.name}, because an earlier one could not "
        f"be read and was kept as {kept}. To get those settings back, mend that file, move it back to "
        f"{path.name} and restart the service; if they are not wanted, delete it."
    )


def load_for_service(path: Path) -> tuple[Config, list[str], list[str]]:
    """What the service does at start-up: :func:`load`, and keep a file that was no use.

    A file of which nothing could be read (not JSON, not text, not an object) leaves the
    service running on defaults, and the first setting changed after that would be saved
    over it: a slip of the hand in an editor would cost every setting.  Such a file is
    therefore moved aside first (:func:`set_aside`), and a later start that finds only the
    kept file says so again (:func:`left_aside`).

    Returns the config, all problems, and those among them that must stay visible until the
    service restarts (the others are over once the settings have been saved again).
    """
    path = Path(path)
    try:
        cfg, problems, unusable = _read(path)
    except Exception as exc:  # noqa: BLE001 - as in load()
        cfg, unusable = default_config(), True
        problems = [f"{path} could not be loaded ({type(exc).__name__}: {exc}); using defaults"]
    if not unusable:
        problems = [_cut(problem) for problem in problems]
        reminder = _cut(left_aside(path), 600)
        return (cfg, [reminder, *problems], [reminder]) if reminder else (cfg, problems, [])
    note = _cut(f"{_cut(problems[0], 250)}. {set_aside(path)}", 800)
    return cfg, [note], [note]


def save(cfg: Config, path: Path) -> None:
    """Write the config atomically (write temp file, fsync, rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cfg.to_dict(), indent=2, sort_keys=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
