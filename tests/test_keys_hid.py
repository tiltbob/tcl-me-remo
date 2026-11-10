import pytest

from tcl_me_remo import hid, keys


def test_named_keys_resolve_case_insensitively_and_via_alias():
    assert keys.resolve("wake").spec == "WAKE"
    assert keys.resolve(" WakeUp ").spec == "WAKE"
    assert keys.resolve("select").spec == "OK"
    assert keys.resolve("NUM7").usage == 0x24
    assert keys.resolve("F3").usage == 0x3C  # the TCL input-list candidate
    assert keys.resolve("F12").usage == 0x45


def test_raw_specs_are_canonicalised_and_range_checked():
    assert keys.resolve("c:223").spec == "C:0223"
    assert keys.resolve("C:0x0223").spec == "C:0223"
    assert keys.resolve("k:3c").spec == "K:3C"
    assert keys.resolve("S:82") == keys.Key("S:82", keys.PAGE_SYSTEM, 0x82, label="raw system 0x82")
    # K:14 is the letter Q: declaring letters would make Android treat us as a full keyboard
    for bad in ("C:0400", "C:0000", "S:84", "K:14", "K:F2", "X:12", "C:", "", "no such key"):
        with pytest.raises(keys.UnknownKey):
            keys.resolve(bad)


def test_every_named_key_fits_its_report():
    for key in keys.NAMED.values():
        low, high = keys.USAGE_RANGE[key.page]
        assert low <= key.usage <= high, key
        report_id, payload = hid.press_report(key)
        assert len(payload) == hid.REPORT_LENGTH[report_id]
        assert payload != hid.release_report(report_id)
    assert len(keys.catalog()) == len(keys.NAMED)
    for alias, target in keys.ALIASES.items():
        assert target in keys.NAMED, alias


def test_discrete_power_reports():
    # WAKE: Generic Desktop System Wake Up, the only standard usage that becomes KEY_WAKEUP.
    assert hid.press_report(keys.resolve("WAKE")) == (hid.REPORT_ID_SYSTEM, b"\x04")
    # SLEEP: Consumer 0x32, not System Sleep (which Android 16 turns into LOCK).
    assert hid.press_report(keys.resolve("SLEEP")) == (hid.REPORT_ID_CONSUMER, b"\x32\x00\x00\x00")
    assert hid.press_report(keys.resolve("POWER")) == (hid.REPORT_ID_SYSTEM, b"\x01")
    assert hid.press_report(keys.resolve("S:82")) == (hid.REPORT_ID_SYSTEM, b"\x02")
    assert hid.release_report(hid.REPORT_ID_SYSTEM) == b"\x00"


def test_keyboard_and_consumer_reports():
    assert hid.press_report(keys.resolve("DOWN")) == (hid.REPORT_ID_CONSUMER, b"\x43\x00\x00\x00")
    assert hid.press_report(keys.resolve("OK")) == (hid.REPORT_ID_CONSUMER, b"\x41\x00\x00\x00")
    assert hid.press_report(keys.resolve("HOME")) == (hid.REPORT_ID_CONSUMER, b"\x23\x02\x00\x00")
    assert hid.press_report(keys.resolve("F3")) == (hid.REPORT_ID_KEYBOARD, b"\x3c\x00")
    assert hid.release_report(hid.REPORT_ID_KEYBOARD) == bytes(2)
    assert hid.release_report(hid.REPORT_ID_CONSUMER) == bytes(4)


def test_report_map_is_frozen():
    # A bonded TV caches the report map; changing it silently breaks existing pairings.
    assert hid.REPORT_MAP.hex() == (
        "05010906a10185010507191e29f1151e26f100750895028100c0"
        "050c0901a101850219012aff03150126ff03751095028100c0"
        "05010980a10185031500250175019503098109820983810295058101c0"
    )
    assert hid.HID_INFORMATION == bytes.fromhex("11010001")


def _walk_report_map(data: bytes):
    """Minimal HID item parser: yields (tag_byte_without_size, value) for short items."""
    i = 0
    while i < len(data):
        prefix = data[i]
        size = (0, 1, 2, 4)[prefix & 0x03]
        value = int.from_bytes(data[i + 1 : i + 1 + size], "little")
        yield prefix & 0xFC, value
        i += 1 + size
    assert i == len(data)


def test_report_map_is_well_formed_and_matches_report_lengths():
    depth = 0
    report_id = None
    size = count = 0
    bits: dict[int, int] = {}
    for tag, value in _walk_report_map(hid.REPORT_MAP):
        if tag == 0xA0:  # Collection
            depth += 1
        elif tag == 0xC0:  # End Collection
            depth -= 1
            assert depth >= 0
        elif tag == 0x84:  # Report ID
            report_id = value
        elif tag == 0x74:  # Report Size
            size = value
        elif tag == 0x94:  # Report Count
            count = value
        elif tag == 0x80:  # Input
            assert report_id is not None
            bits[report_id] = bits.get(report_id, 0) + size * count
    assert depth == 0
    assert {rid: n // 8 for rid, n in bits.items()} == hid.REPORT_LENGTH
    assert all(n % 8 == 0 for n in bits.values())
