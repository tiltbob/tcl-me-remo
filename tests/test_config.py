import json

import pytest

from tcl_me_remo import config as C


def test_defaults_round_trip():
    cfg = C.default_config()
    again, problems = C.from_dict(cfg.to_dict())
    assert problems == []
    assert again == cfg
    assert cfg.events.boot_tv_on and cfg.events.suspend_tv_off
    assert cfg.events.reboot_tv_off is False
    assert cfg.wifi.enabled is False  # Wi-Fi assist is strictly opt-in
    assert cfg.input.method == "none"  # no navigation keys until the user has tested a macro
    assert cfg.power.on_key == "WAKE" and cfg.power.off_key == "SLEEP" and cfg.power.on_repeat == 1


def test_direct_delivery_is_careful_unless_the_user_says_otherwise():
    """The shipped default must be the one that cannot stall the user's controllers.

    The transport's own tests run with ``raw_notify = "on"`` (they are about direct delivery
    itself), so this is where the default is pinned.
    """
    assert C.default_config().bluetooth.raw_notify == "auto"
    assert C.BluetoothConfig().raw_notify == "auto"
    leaf = C.describe()["bluetooth"]["raw_notify"]
    assert leaf["default"] == "auto" and leaf["choices"] == ("auto", "on", "off", "always")
    for word in ("'auto'", "'on'", "'off'", "'always'", "6.17"):
        assert word in leaf["help"]
    # A settings file that does not mention it, or holds something this version does not know
    # (an old file, a typo), ends up careful as well.
    assert C.from_dict({"bluetooth": {"tv_address": "AA:BB:CC:DD:EE:FF"}})[0].bluetooth.raw_notify == "auto"
    cfg, problems = C.from_dict({"bluetooth": {"raw_notify": "yes"}}, strict=False)
    assert cfg.bluetooth.raw_notify == "auto" and len(problems) == 1


@pytest.mark.parametrize("value", ["auto", "on", "off", "always"])
def test_every_direct_delivery_choice_is_accepted_and_survives_a_round_trip(value):
    cfg = C.apply_patch(C.default_config(), {"bluetooth": {"raw_notify": value}})
    assert cfg.bluetooth.raw_notify == value
    assert C.set_path(C.default_config(), "bluetooth.raw_notify", value).bluetooth.raw_notify == value
    again, problems = C.from_dict(cfg.to_dict())
    assert again.bluetooth.raw_notify == value and problems == []


def test_apply_patch_merges_and_validates():
    cfg = C.default_config()
    new = C.apply_patch(cfg, {"input": {"hdmi_port": 3}, "events": {"boot_tv_on": False}})
    assert new.input.hdmi_port == 3
    assert new.events.boot_tv_on is False
    assert new.events.resume_tv_on is True  # untouched siblings survive
    assert cfg.input.hdmi_port == 1  # original is not mutated


@pytest.mark.parametrize(
    "patch, path",
    [
        ({"input": {"hdmi_port": 5}}, "input.hdmi_port"),
        ({"input": {"hdmi_port": 1.5}}, "input.hdmi_port"),
        ({"input": {"hdmi_port": True}}, "input.hdmi_port"),
        ({"input": {"method": "telepathy"}}, "input.method"),
        ({"input": {"macro": "DOWN*999"}}, "input.macro"),
        ({"input": {"macro": "NOT_A_KEY"}}, "input.macro"),
        ({"input": {"preset": "nope"}}, "input.preset"),
        ({"wifi": {"link_preset": "nope"}}, "wifi.link_preset"),
        ({"bluetooth": {"wake_beacon": "loud"}}, "bluetooth.wake_beacon"),
        ({"bluetooth": {"raw_notify": "On"}}, "bluetooth.raw_notify"),
        ({"bluetooth": {"raw_notify": True}}, "bluetooth.raw_notify"),
        ({"bluetooth": {"raw_notify": "needed"}}, "bluetooth.raw_notify"),
        ({"bluetooth": {"raw_notify": ""}}, "bluetooth.raw_notify"),
        ({"bluetooth": {"wake_beacon_data": "46 0"}}, "bluetooth.wake_beacon_data"),
        ({"bluetooth": {"wake_beacon_data": "zz00ff"}}, "bluetooth.wake_beacon_data"),
        ({"bluetooth": {"wake_beacon_data": "4600"}}, "bluetooth.wake_beacon_data"),
        ({"bluetooth": {"wake_beacon_data": "00" * 30}}, "bluetooth.wake_beacon_data"),
        ({"power": {"on_key": "BANANA"}}, "power.on_key"),
        ({"bluetooth": {"tv_address": "nonsense"}}, "bluetooth.tv_address"),
        ({"bluetooth": {"adapter": "wlan0"}}, "bluetooth.adapter"),
        ({"bluetooth": {"device_name": "  "}}, "bluetooth.device_name"),
        ({"display": {"require_external": "maybe"}}, "display.require_external"),
        ({"enabled": "yes"}, "enabled"),
        ({"nope": 1}, "nope"),
        ({"input": {"nope": 1}}, "input.nope"),
        ({"input": 3}, "input"),
        ({"wifi": {"host": "bad host; rm -rf"}}, "wifi.host"),
        ({"display": {"wait_seconds": float("nan")}}, "display.wait_seconds"),
    ],
)
def test_apply_patch_rejects_bad_values(patch, path):
    with pytest.raises(C.ConfigError) as err:
        C.apply_patch(C.default_config(), patch)
    assert err.value.path == path


def test_values_are_normalised():
    cfg = C.apply_patch(
        C.default_config(),
        {
            "bluetooth": {"tv_address": "aa-bb-cc-dd-ee-0f"},
            "power": {"on_key": "wakeup"},
            "input": {"macro": "  home   1s  down*{port}  ok "},
        },
    )
    assert cfg.bluetooth.tv_address == "AA:BB:CC:DD:EE:0F"
    beacon = C.apply_patch(cfg, {"bluetooth": {"wake_beacon_data": "46:00 00 A1-B2-C3 d4e5f6"}})
    assert beacon.bluetooth.wake_beacon_data == "460000a1b2c3d4e5f6"
    assert cfg.power.on_key == "WAKE"
    assert cfg.input.macro == "home 1s down*{port} ok"


def test_set_path():
    cfg = C.set_path(C.default_config(), "input.hdmi_port", 2)
    assert cfg.input.hdmi_port == 2
    with pytest.raises(C.ConfigError):
        C.set_path(cfg, "", 1)
    with pytest.raises(C.ConfigError):
        C.set_path(cfg, "input.hdmi_port.extra", 1)


def test_load_missing_file_gives_defaults(tmp_path):
    cfg, problems = C.load(tmp_path / "nope.json")
    assert cfg == C.default_config()
    assert problems == []


def test_load_tolerates_damage(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not json")
    cfg, problems = C.load(path)
    assert cfg == C.default_config()
    assert problems and "not valid JSON" in problems[0]

    path.write_text(json.dumps({"input": {"hdmi_port": 99, "method": "wifi"}, "future_key": 1, "events": []}))
    cfg, problems = C.load(path)
    assert cfg.input.method == "wifi"  # good values survive
    assert cfg.input.hdmi_port == 1  # bad value falls back to the default
    assert cfg.events == C.EventsConfig()
    assert len(problems) == 3


def test_save_is_atomic_and_readable(tmp_path):
    path = tmp_path / "sub" / "config.json"
    cfg = C.apply_patch(C.default_config(), {"input": {"hdmi_port": 4}})
    C.save(cfg, path)
    assert json.loads(path.read_text())["input"]["hdmi_port"] == 4
    assert [p.name for p in path.parent.iterdir()] == ["config.json"]  # no temp files left behind
    loaded, problems = C.load(path)
    assert loaded == cfg and problems == []


def test_describe_lists_every_leaf_with_help():
    desc = C.describe()
    assert desc["input"]["hdmi_port"]["min"] == 1
    assert desc["input"]["hdmi_port"]["max"] == 4
    assert desc["input"]["method"]["choices"] == ("macro", "wifi", "none")
    assert desc["input"]["method"]["default"] == "none"
    json.dumps(desc)  # must be serialisable for the control channel


# --------------------------------------------------------------------------- a damaged file


DAMAGED = {
    "a Latin-1 byte (an editor that does not write UTF-8)": b'{"bluetooth": {"tv_name": "Caf\xe9 TV"}}',
    "UTF-16": '{"enabled": true}'.encode("utf-16"),
    "NaN for a whole number": b'{"input": {"hdmi_port": NaN}}',
    "an infinity for a whole number": b'{"input": {"hdmi_port": 1e999}}',
    "minus infinity for a number": b'{"display": {"wait_seconds": -Infinity}}',
    "a whole number too large for a float": b'{"display": {"wait_seconds": 1' + b"0" * 400 + b"}}",
    "a digit int() does not take": '{"input": {"macro": "DOWN*²"}}'.encode(),
    "nested too deeply": b"[" * 100_000,
    "nested too deeply inside a setting": b'{"input": {"hdmi_port": ' + b"[" * 100_000 + b"}}",
    "a list at the top": b"[1, 2, 3]",
    "NUL bytes": b"\x00" * 64,
    "nothing": b"",
    "a name that cannot be encoded": b'{"bluetooth": {"device_name": "\\ud800"}}',
    "a name with a NUL": b'{"bluetooth": {"device_name": "Deck\\u0000Remote"}}',
    "a number with more digits than Python reads": b'{"input": {"hdmi_port": ' + b"9" * 5000 + b"}}",
}


@pytest.mark.parametrize("name", list(DAMAGED))
def test_load_never_raises_and_says_what_is_wrong(tmp_path, name):
    path = tmp_path / "config.json"
    path.write_bytes(DAMAGED[name])
    cfg, problems = C.load(path)
    assert isinstance(cfg, C.Config) and problems, name
    assert all(len(problem) <= 400 for problem in problems)
    # What cannot be used is replaced by the default, and the result is a config that can be saved.
    assert cfg.input.hdmi_port == 1 and cfg.display.wait_seconds == 5.0
    assert cfg.bluetooth.device_name == "SteamOS Remote"
    C.save(cfg, tmp_path / "again.json")
    assert C.load(tmp_path / "again.json") == (cfg, [])
    assert path.read_bytes() == DAMAGED[name]  # load() itself changes nothing


def test_a_byte_order_mark_is_not_damage(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"input": {"hdmi_port": 3}}).encode())
    cfg, problems = C.load(path)
    assert cfg.input.hdmi_port == 3 and problems == []
    assert C.load_for_service(path) == (cfg, [], []) and path.exists()


def test_the_problem_list_stays_short_whatever_the_file_holds(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({**{f"junk{n}": n for n in range(500)}, "x" * 100_000: 1, "input": {"hdmi_port": 2}}))
    cfg, problems = C.load(path)
    assert cfg.input.hdmi_port == 2
    assert len(problems) <= 25 and sum(map(len, problems)) < 10_000 and "more problems" in problems[-1]


def test_a_file_that_is_no_use_is_moved_aside_before_anything_can_overwrite_it(tmp_path):
    path, kept = tmp_path / "config.json", tmp_path / "config.json.broken"
    good = C.apply_patch(C.default_config(), {"input": {"method": "macro", "hdmi_port": 3}})
    C.save(good, path)
    slip = path.read_text().replace('"hdmi_port": 3,', '"hdmi_port": 3,,')  # one comma too many
    path.write_text(slip)
    kept.write_text("an older broken file")

    cfg, problems, lasting = C.load_for_service(path)
    assert cfg == C.default_config()
    assert not path.exists() and kept.read_text() == slip  # nothing is lost, and the older one is replaced
    assert len(problems) == 1 and lasting == problems
    assert "not valid JSON" in problems[0] and str(kept) in problems[0]
    # The service now saves a setting: a new file, and the user's own is still there to be mended.
    C.save(C.apply_patch(cfg, {"events": {"reboot_tv_off": True}}), path)
    assert kept.read_text() == slip and C.load(path)[1] == []


@pytest.mark.parametrize("content", [b"\xff\xfe{", b"[1]", b'"text"', b"", b"[" * 100_000])
def test_every_kind_of_unusable_file_is_kept(tmp_path, content):
    path = tmp_path / "config.json"
    path.write_bytes(content)
    cfg, problems, lasting = C.load_for_service(path)
    assert cfg == C.default_config() and lasting and lasting == problems
    assert (tmp_path / "config.json.broken").read_bytes() == content and not path.exists()


def test_a_file_with_single_bad_values_stays_where_it_is(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"input": {"hdmi_port": 99, "method": "wifi"}, "future_key": 1}))
    cfg, problems, lasting = C.load_for_service(path)
    assert cfg.input.method == "wifi" and len(problems) == 2 and lasting == []
    assert path.exists() and not (tmp_path / "config.json.broken").exists()
    # and so does no file at all
    assert C.load_for_service(tmp_path / "nope.json") == (C.default_config(), [], [])
    assert list(tmp_path.iterdir()) == [path]


def test_a_file_that_cannot_be_moved_aside_is_said_to_be_in_danger(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("{ not json")

    def refuse(src, dst):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(C.os, "replace", refuse)
    cfg, problems, lasting = C.load_for_service(path)
    assert cfg == C.default_config() and lasting == problems
    assert "could not be moved" in problems[0] and "Read-only" in problems[0]


def test_a_later_start_still_says_where_the_settings_went(tmp_path):
    """The notice lived for one run of the service.  After a reboot with no setting changed
    the service ran on defaults again, the TV unpaired, and nothing said why."""
    path, kept = tmp_path / "config.json", tmp_path / "config.json.broken"
    wanted = C.apply_patch(C.default_config(), {"input": {"method": "macro", "hdmi_port": 3}})
    C.save(wanted, path)
    slip = path.read_text().replace('"hdmi_port": 3,', '"hdmi_port": 3,,')
    path.write_text(slip)
    first = C.load_for_service(path)
    assert first[2] and not path.exists()

    for _ in range(2):  # the next start, and the one after that
        cfg, problems, lasting = C.load_for_service(path)
        assert cfg == C.default_config() and len(problems) == 1 and lasting == problems
        assert str(kept) in problems[0] and "default" in problems[0] and "move it back" in problems[0]
        assert kept.read_text() == slip and not path.exists()  # looking changes nothing
    assert C.left_aside(path) == problems[0]
    assert C.load(path) == (C.default_config(), [])  # the plain loader reads one file and no more

    # It is over when the person has done what it says ...
    path.write_text(slip.replace(",,", ","))
    kept.unlink()
    assert C.load_for_service(path) == (wanted, [], [])
    # ... or has thrown the kept file away,
    path.unlink()
    assert C.load_for_service(path) == (C.default_config(), [], [])
    # ... or has made new settings since: then there is a settings file, and it is in order.
    kept.write_text(slip)
    C.save(C.default_config(), path)
    assert C.load_for_service(path) == (C.default_config(), [], [])
    # A directory of that name is not a file this program kept.
    path.unlink()
    kept.unlink()
    kept.mkdir()
    assert C.load_for_service(path) == (C.default_config(), [], [])


def test_the_name_of_a_setting_that_does_not_exist_is_not_repeated_at_length():
    for patch in ({"k" * 300_000: 1}, {"input": {"\u00e9" * 300_000: 1}}, {"x" * 300_000: {"y": 1}}):
        with pytest.raises(C.ConfigError) as err:
            C.apply_patch(C.default_config(), patch)
        assert "unknown setting" in str(err.value) and len(str(err.value)) < 200
    with pytest.raises(C.ConfigError) as err:
        C.set_path(C.default_config(), "input." + "z" * 5000, 1)
    assert str(err.value).startswith("input.zzz") and len(str(err.value)) < 200
    # Real names are given whole.
    with pytest.raises(C.ConfigError, match=r"^bluetooth\.idle_disconnect_seconds: must be"):
        C.set_path(C.default_config(), "bluetooth.idle_disconnect_seconds", -5)


# --------------------------------------------------------------------------- values from outside


@pytest.mark.parametrize(
    "patch, path",
    [
        ({"input": {"hdmi_port": float("nan")}}, "input.hdmi_port"),
        ({"input": {"hdmi_port": float("inf")}}, "input.hdmi_port"),
        ({"input": {"hdmi_port": 10**400}}, "input.hdmi_port"),
        ({"display": {"wait_seconds": 10**400}}, "display.wait_seconds"),
        ({"display": {"wait_seconds": float("-inf")}}, "display.wait_seconds"),
        ({"bluetooth": {"key_gap_ms": float("nan")}}, "bluetooth.key_gap_ms"),
        ({"input": {"macro": "OK*²"}}, "input.macro"),
        ({"input": {"macro": "\ud800"}}, "input.macro"),
        ({"wifi": {"app_link": "x\udfff"}}, "wifi.app_link"),
        ({"display": {"edid_match": "\ud800"}}, "display.edid_match"),
        ({"power": {"on_key": "K" * 33}}, "power.on_key"),
        ({"bluetooth": {"adapter": "hci0\n"}}, "bluetooth.adapter"),
        ({"bluetooth": {"adapter": "hci٣"}}, "bluetooth.adapter"),
    ],
)
def test_odd_values_are_refused_as_values_and_nothing_else_is_raised(patch, path):
    with pytest.raises(C.ConfigError) as err:
        C.apply_patch(C.default_config(), patch)
    assert err.value.path == path
    assert len(str(err.value)) < 300


def test_error_messages_do_not_carry_the_whole_value():
    for patch in (
        {"input": {"preset": "p" * 100_000}},
        {"wifi": {"link_preset": "p" * 100_000}},
        {"input": {"method": "m" * 100_000}},
        {"enabled": "y" * 100_000},
        {"input": {"hdmi_port": "9" * 100_000}},
        {"input": {"hdmi_port": [[1] * 1000] * 1000}},
        {"input": {"macro": "X" * 1000}},
        {"power": {"off_key": "X" * 32}},
    ):
        with pytest.raises(C.ConfigError) as err:
            C.apply_patch(C.default_config(), patch)
        assert len(str(err.value)) < 300, str(err.value)[:120]


def test_every_number_has_a_range_and_every_setting_a_help_text():
    def leaves(tree, prefix=""):
        for key, value in tree.items():
            if "type" in value and "default" in value:
                yield prefix + key, value
            else:
                yield from leaves(value, prefix + key + ".")

    found = dict(leaves(C.describe()))
    assert len(found) > 40
    for name, leaf in found.items():
        assert leaf.get("help"), name
        if leaf["type"] in ("int", "float"):
            assert "min" in leaf and "max" in leaf, name
    from tcl_me_remo import macro

    assert found["input.macro"]["maxlen"] == macro.MAX_TEXT


@pytest.mark.parametrize(
    "name",
    ["Deck\x00Remote", "Deck\nRemote", "\x1b[31mDeck", "Deck\x7f", "Deck\x85Remote", "\ud800", "Deck\udfff", "\t"],
)
def test_the_device_name_takes_no_control_characters_and_nothing_unencodable(name):
    """It is advertised and handed to BlueZ over D-Bus: a NUL there fails every advertisement."""
    with pytest.raises(C.ConfigError) as err:
        C.apply_patch(C.default_config(), {"bluetooth": {"device_name": name}})
    assert err.value.path == "bluetooth.device_name"


def test_the_device_name_is_measured_in_bytes():
    def set_name(name):
        return C.apply_patch(C.default_config(), {"bluetooth": {"device_name": name}}).bluetooth.device_name

    assert set_name("A" * 26) == "A" * 26
    assert set_name("Wohnzimmer Ü") == "Wohnzimmer Ü"
    assert set_name("é" * 13) == "é" * 13  # 26 bytes
    assert set_name("\U0001f3ae Deck") == "\U0001f3ae Deck"  # an emoji is fine, it is no control character
    for too_long in ("A" * 27, "é" * 14, "\U0001f3ae" * 7):
        with pytest.raises(C.ConfigError) as err:
            set_name(too_long)
        assert err.value.path == "bluetooth.device_name"
    with pytest.raises(C.ConfigError, match="26 bytes"):
        set_name("é" * 14)
    from tcl_me_remo.bluez import advert

    assert C.DEVICE_NAME_BYTES == advert.MAX_NAME_BYTES
    assert "26 bytes" in C.describe()["bluetooth"]["device_name"]["help"]


@pytest.mark.parametrize("section, field", [("bluetooth", "tv_name"), ("wifi", "name")])
def test_names_from_outside_are_cleaned_and_never_refused(section, field):
    """A TV's name arrives with its address; refusing the name would lose the pairing."""

    def stored(name):
        return getattr(getattr(C.apply_patch(C.default_config(), {section: {field: name}}), section), field)

    assert stored("Living Room TV") == "Living Room TV"
    assert stored("TV\nERROR tcl_me_remo.daemon: lost\x00 the\x1b[31m bus\x85") == (
        "TV ERROR tcl_me_remo.daemon: lost the [31m bus"
    )
    assert stored("TV \ud800 \udfff") == "TV"
    assert stored("テレビ \U0001f468‍\U0001f469 TV") == "テレビ \U0001f468‍\U0001f469 TV"
    assert stored("N" * 5000) == "N" * 64
    assert stored("\x00\n") == ""
    cfg, problems = C.from_dict({section: {field: "a\x00b"}}, strict=False)
    assert getattr(getattr(cfg, section), field) == "a b" and problems == []


# --------------------------------------------------------------------------- settings that depend on each other

LONG = "OK~10*30 OK~10*30 OK~10*30"  # every token valid; over fifteen minutes in all


def test_a_macro_that_would_be_refused_when_due_is_refused_when_set():
    base = C.apply_patch(C.default_config(), {"input": {"preset": "custom"}})
    with pytest.raises(C.ConfigError) as err:
        C.apply_patch(base, {"input": {"macro": LONG}})
    assert err.value.path == "input.macro" and "would take" in err.value.message and "120s" in err.value.message
    # ... also when it is the key timing that takes it over the limit
    many = C.apply_patch(base, {"input": {"macro": " ".join(["OK"] * 64)}})
    with pytest.raises(C.ConfigError) as err:
        C.apply_patch(many, {"bluetooth": {"key_gap_ms": 5000}})
    assert err.value.path == "bluetooth.key_gap_ms" and "would take" in err.value.message
    with pytest.raises(C.ConfigError) as err:
        C.apply_patch(many, {"bluetooth": {"key_gap_ms": 2000}, "input": {"hdmi_port": 2}, "enabled": False})
    assert err.value.path == "input.hdmi_port"
    # A macro that is stored but not the one in use is not what is checked.
    unused = C.apply_patch(C.default_config(), {"input": {"macro": LONG}})
    assert unused.input.preset == "tcl-row" and C.macro_runtime_problem(unused) == ""
    with pytest.raises(C.ConfigError) as err:
        C.apply_patch(unused, {"input": {"preset": "custom"}})
    assert err.value.path == "input.preset"
    # The built-in presets stay inside the limit with the slowest timings there are.
    slowest = {"bluetooth": {"key_gap_ms": 5000, "key_press_ms": 1000}}
    for preset in ("tcl-row", "tcl-row-home", "tcl-row-first", "tcl-column"):
        for port in (1, 2, 3, 4):
            C.apply_patch(C.default_config(), {"input": {"preset": preset, "hdmi_port": port}, **slowest})


def test_a_combination_that_is_already_over_the_limit_does_not_block_other_changes():
    """E.g. from an older file: load() takes each value for itself."""
    over, problems = C.from_dict({"input": {"preset": "custom", "macro": LONG}}, strict=False)
    assert problems == [] and C.macro_runtime_problem(over).startswith("macro would take")
    changed = C.apply_patch(over, {"events": {"reboot_tv_off": True}, "bluetooth": {"tv_name": "TV"}, "enabled": False})
    assert changed.events.reboot_tv_off and changed.input.macro == LONG
    # Mending it is possible from either side.
    assert C.apply_patch(over, {"input": {"macro": "HOME OK"}}).input.macro == "HOME OK"
    assert C.apply_patch(over, {"input": {"preset": "tcl-row"}}).input.preset == "tcl-row"


def test_what_the_help_texts_must_say():
    desc = C.describe()
    assert C.default_config().input.wifi_fallback_to_macro is False
    assert desc["input"]["wifi_fallback_to_macro"]["default"] is False
    assert "watch" in desc["input"]["wifi_fallback_to_macro"]["help"]
    # Like gatt_security next to it: a change of the adapter needs a restart, and says so.
    assert "restart" in desc["bluetooth"]["adapter"]["help"]
    assert "restart" in desc["bluetooth"]["gatt_security"]["help"]
