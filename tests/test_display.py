import pytest

from tcl_me_remo import display
from tcl_me_remo.config import DisplayConfig


def make_edid(maker="TCL", name="TCL TV"):
    edid = bytearray(128)
    edid[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    word = 0
    for ch in maker:
        word = (word << 5) | (ord(ch) - ord("A") + 1)
    edid[8], edid[9] = word >> 8, word & 0xFF
    text = name.encode("ascii") + b"\n"
    edid[72:77] = b"\x00\x00\x00\xfc\x00"
    edid[77:90] = text.ljust(13, b" ")
    return bytes(edid)


def make_sysfs(tmp_path, connectors):
    drm = tmp_path / "class" / "drm"
    drm.mkdir(parents=True)
    (drm / "card0").mkdir()
    (drm / "renderD128").mkdir()
    (drm / "version").write_text("drm 1.1.0")
    for name, (status, edid) in connectors.items():
        d = drm / name
        d.mkdir()
        (d / "status").write_text(status + "\n")
        (d / "edid").write_bytes(edid or b"")
    return tmp_path


def test_parse_edid():
    assert display.parse_edid(make_edid("TCL", "TCL TV")) == ("TCL", "TCL TV")
    assert display.parse_edid(make_edid("VLV", "ANX7530 U")) == ("VLV", "ANX7530 U")
    assert display.parse_edid(b"") == ("", "")
    assert display.parse_edid(b"\x01" * 128) == ("", "")


def test_read_state_handheld_docked(tmp_path):
    root = make_sysfs(
        tmp_path,
        {
            "card0-eDP-1": ("connected", make_edid("VLV", "ANX7530 U")),
            "card0-DP-1": ("connected", make_edid("TCL", "TCL TV")),
            "card0-HDMI-A-1": ("disconnected", None),
            "card0-Writeback-1": ("unknown", None),
        },
    )
    state = display.read_display_state(root)
    assert [c.name for c in state.connectors] == ["DP-1", "HDMI-A-1", "eDP-1"]
    assert state.has_internal
    assert [c.label for c in state.external_connected] == ["DP-1 (TCL TCL TV)"]
    payload = state.to_dict()
    assert payload["has_internal"] is True
    assert payload["connectors"][0]["connected"] is True


def test_read_state_missing_sysfs(tmp_path):
    state = display.read_display_state(tmp_path / "nowhere")
    assert state.connectors == ()
    assert not state.has_internal


def _state(*connectors):
    return display.DisplayState(connectors=tuple(connectors))


EDP = display.Connector("eDP-1", "card0", "connected", True, "VLV", "ANX7530 U")
TV = display.Connector("HDMI-A-1", "card0", "connected", False, "TCL", "TCL TV")
MONITOR = display.Connector("DP-1", "card0", "connected", False, "DEL", "DELL U2720Q")
UNPLUGGED = display.Connector("HDMI-A-1", "card0", "disconnected", False)


@pytest.mark.parametrize(
    "mode, match, connectors, ok",
    [
        ("no", "", (EDP,), True),
        ("no", "TCL", (EDP,), True),
        ("auto", "", (EDP,), False),  # handheld, undocked
        ("auto", "", (EDP, UNPLUGGED), False),
        ("auto", "", (EDP, TV), True),  # handheld, docked
        ("auto", "", (TV,), True),  # console-style box
        ("auto", "", (UNPLUGGED,), True),  # box whose TV shows as unplugged while it is off
        ("auto", "", (), True),  # nothing readable: do not block a screenless box
        ("auto", "tcl", (EDP, MONITOR), False),  # docked at the desk, not at the TV
        ("auto", "tcl", (EDP, MONITOR, TV), True),
        ("auto", "tcl", (MONITOR,), False),
        ("auto", "tcl", (UNPLUGGED,), False),
        ("yes", "", (UNPLUGGED,), False),
        ("yes", "", (TV,), True),
        ("yes", "dell", (TV,), False),
    ],
)
def test_gate(mode, match, connectors, ok):
    cfg = DisplayConfig(require_external=mode, edid_match=match)
    gate = display.evaluate(cfg, _state(*connectors))
    assert gate.ok is ok, gate.reason
    assert gate.reason


async def test_wait_for_gate_retries_until_display_appears(tmp_path):
    root = make_sysfs(tmp_path, {"card0-eDP-1": ("connected", None), "card0-DP-1": ("disconnected", None)})
    cfg = DisplayConfig(require_external="auto", wait_seconds=0.0)
    assert (await display.wait_for_gate(cfg, sys_root=root, poll=0.01)).ok is False

    import asyncio

    async def plug_in():
        await asyncio.sleep(0.05)
        (root / "class" / "drm" / "card0-DP-1" / "status").write_text("connected\n")

    cfg = DisplayConfig(require_external="auto", wait_seconds=2.0)
    task = asyncio.create_task(plug_in())
    gate = await display.wait_for_gate(cfg, sys_root=root, poll=0.01)
    await task
    assert gate.ok
