import pytest

from tcl_me_remo import tvdata


def test_link_presets_cover_all_four_ports():
    for preset in tvdata.LINK_PRESETS.values():
        assert sorted(preset.links) == [1, 2, 3, 4]
        for link in preset.links.values():
            assert link.startswith("content://android.media.tv/passthrough/com.tcl.tvinput%2F")
            assert "/" not in link.split("passthrough/", 1)[1]  # the id's slashes stay encoded
    catalog = tvdata.link_preset_catalog()
    assert catalog[0]["name"] == "tcl-mediatek" and set(catalog[0]["links"]) == {"1", "2", "3", "4"}


def test_mediatek_wake_data_matches_the_reference_filter():
    data = tvdata.mediatek_wake_data("A1:B2:C3:D4:E5:F6")
    # company id is carried separately; 16 bytes follow it: 1 ignored, address LSB first,
    # 4 ignored, then "CRKTM"
    assert data == bytes.fromhex("00" "f6e5d4c3b2a1" "00000000") + b"CRKTM"
    assert len(data) == 16
    with pytest.raises(ValueError):
        tvdata.mediatek_wake_data("nonsense")


def test_wake_beacon_selection():
    assert tvdata.wake_beacon("off", "", "A1:B2:C3:D4:E5:F6") is None
    assert tvdata.wake_beacon("mediatek", "", "") is None  # no TV known yet
    company, data = tvdata.wake_beacon("mediatek", "", "A1:B2:C3:D4:E5:F6")
    assert company == 0x0046 and data.endswith(b"CRKTM")
    # custom data is given in on-air order: company id little endian, then the payload
    assert tvdata.wake_beacon("custom", "1d00aabbcc", "") == (0x001D, bytes.fromhex("aabbcc"))
    assert tvdata.wake_beacon("custom", "", "A1:B2:C3:D4:E5:F6") is None
