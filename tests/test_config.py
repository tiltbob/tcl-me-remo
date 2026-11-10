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
    json.dumps(desc)  # must be serialisable for the control channel
