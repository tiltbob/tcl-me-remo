"""The protobuf wire format and the message tables of the Wi-Fi assist.

Expected byte strings are written out by hand from the field tables (tag = number << 3 |
wire type, varints little-endian base 128), never produced by the code under test.
"""

import random

import pytest

from tcl_me_remo.atv import messages, proto
from tcl_me_remo.atv.proto import (
    BOOL,
    BYTES,
    INT,
    STRING,
    FrameDecoder,
    ProtoError,
    Schema,
    decode,
    decode_varint,
    encode,
    encode_frame,
    encode_varint,
)


def hx(text: str) -> bytes:
    return bytes.fromhex(text)


# ---------------------------------------------------------------------------- varints

VARINTS = [
    (0, "00"),
    (1, "01"),
    (127, "7f"),
    (128, "80 01"),
    (200, "c8 01"),
    (300, "ac 02"),
    (611, "e3 04"),
    (16383, "ff 7f"),
    (16384, "80 80 01"),
    (2**32 - 1, "ff ff ff ff 0f"),
    (2**63, "80 80 80 80 80 80 80 80 80 01"),
    (2**64 - 1, "ff ff ff ff ff ff ff ff ff 01"),
]


@pytest.mark.parametrize(("value", "encoded"), VARINTS)
def test_varint_encoding(value, encoded):
    assert encode_varint(value) == hx(encoded)
    assert decode_varint(hx(encoded)) == (value, len(hx(encoded)))


def test_varint_negative_numbers_are_64_bit_twos_complement():
    assert encode_varint(-1) == hx("ff ff ff ff ff ff ff ff ff 01")
    assert encode_varint(-2) == hx("fe ff ff ff ff ff ff ff ff 01")


def test_varint_out_of_range_is_refused():
    for value in (2**64, -(2**64) - 1):
        with pytest.raises(ProtoError):
            encode_varint(value)


def test_varint_decoding_at_an_offset_and_non_minimal_forms():
    assert decode_varint(hx("ff ac 02 05"), 1) == (300, 3)
    assert decode_varint(hx("80 00")) == (0, 2)  # padded zero: legal, if unusual
    assert decode_varint(bytearray(hx("81 80 00"))) == (1, 3)


@pytest.mark.parametrize("data", ["", "80", "ff ff", "80 80 80 80 80 80 80 80 80"])
def test_varint_truncated(data):
    with pytest.raises(ProtoError, match="truncated"):
        decode_varint(hx(data))


def test_varint_longer_than_64_bits_is_refused():
    with pytest.raises(ProtoError, match="too long"):
        decode_varint(hx("80 80 80 80 80 80 80 80 80 80 01"))
    with pytest.raises(ProtoError):
        decode_varint(b"\xff" * 4096)  # must not walk the whole buffer either


# ---------------------------------------------------------------------------- pairing messages

OK_HEADER = "08 02 10 c8 01"  # protocol_version = 2, status = 200


def test_pairing_request_bytes():
    expected = hx(OK_HEADER + "52 18 0a 09") + b"atvremote" + hx("12 0b") + b"tcl-me-remo"
    assert messages.pairing_request("tcl-me-remo") == expected


def test_options_bytes():
    # options (20): input_encodings (1) = [{type (1) = 3 hexadecimal, symbol_length (2) = 6}], preferred_role (3) = 1
    assert messages.options() == hx(OK_HEADER + "a2 01 08 0a 04 08 03 10 06 18 01")


def test_configuration_bytes():
    # configuration (30): encoding (1) = {3, 6}, client_role (2) = 1 input
    assert messages.configuration() == hx(OK_HEADER + "f2 01 08 0a 04 08 03 10 06 10 01")


def test_secret_bytes():
    digest = bytes(range(32))
    assert messages.secret(digest) == hx(OK_HEADER + "c2 02 22 0a 20") + digest


def test_decoding_what_a_tv_sends_while_pairing():
    ack = messages.decode_pairing(hx(OK_HEADER + "5a 00"))
    assert ack["status"] == 200 and ack["protocol_version"] == 2
    assert ack["pairing_request_ack"] == {"server_name": ""}
    assert "options" not in ack and "secret_ack" not in ack

    named = messages.decode_pairing(hx(OK_HEADER + "5a 09 0a 07") + b"Fake TV")
    assert named["pairing_request_ack"]["server_name"] == "Fake TV"

    # The TV's options: output_encodings (2) hexadecimal/6, preferred_role input.
    options = messages.decode_pairing(hx(OK_HEADER + "a2 01 08 12 04 08 03 10 06 18 01"))["options"]
    assert options["output_encodings"] == [{"type": 3, "symbol_length": 6}]
    assert options["input_encodings"] == [] and options["preferred_role"] == 1

    assert "configuration_ack" in messages.decode_pairing(hx(OK_HEADER + "fa 01 00"))

    secret_ack = messages.decode_pairing(hx(OK_HEADER + "ca 02 05 0a 03 01 02 03"))
    assert secret_ack["secret_ack"] == {"secret": b"\x01\x02\x03"}


def test_pairing_error_statuses():
    assert messages.decode_pairing(hx("10 90 03"))["status"] == 400
    assert messages.decode_pairing(hx("08 02 10 92 03"))["status"] == 402
    assert messages.decode_pairing(b"")["status"] == 0  # nothing said is not "OK"


# ---------------------------------------------------------------------------- remote messages


def test_remote_configure_bytes():
    # remote_configure (1): code1 (1) = 611, device_info (2) = {3: 1, 4: "1", 5: "atvremote", 6: "1.0.0"}
    expected = hx("0a 1c 08 e3 04 12 17 18 01 22 01 31 2a 09") + b"atvremote" + hx("32 05") + b"1.0.0"
    assert messages.remote_configure(611) == expected
    assert messages.WANTED_FEATURES == 611
    assert not messages.WANTED_FEATURES & (messages.FEATURE_IME | messages.FEATURE_VOICE)


def test_remote_set_active_and_ping_bytes():
    assert messages.remote_set_active(611) == hx("12 03 08 e3 04")
    assert messages.remote_set_active(622) == hx("12 03 08 ee 04")
    assert messages.ping_response(25) == hx("4a 02 08 19")
    assert messages.ping_response(0) == hx("4a 00")  # proto3 leaves a zero off the wire


@pytest.mark.parametrize(
    ("key", "direction", "encoded"),
    [
        (26, messages.DIRECTION_SHORT, "52 04 08 1a 10 03"),  # POWER
        (223, messages.DIRECTION_SHORT, "52 05 08 df 01 10 03"),  # SLEEP
        (224, messages.DIRECTION_SHORT, "52 05 08 e0 01 10 03"),  # WAKEUP
        (243, messages.DIRECTION_SHORT, "52 05 08 f3 01 10 03"),  # TV_INPUT_HDMI_1
        (24, messages.DIRECTION_START_LONG, "52 04 08 18 10 01"),  # VOLUME_UP down
        (23, messages.DIRECTION_END_LONG, "52 04 08 17 10 02"),  # DPAD_CENTER up
    ],
)
def test_key_inject_bytes(key, direction, encoded):
    assert messages.key_inject(key, direction) == hx(encoded)


def test_key_inject_defaults_to_a_short_press():
    assert messages.key_inject(3) == hx("52 04 08 03 10 03")


def test_app_link_bytes():
    link = "content://android.media.tv/passthrough/com.tcl.tvinput%2F.TvPassThroughService%2FHW15"
    assert len(link) == 0x55
    assert messages.app_link(link) == hx("d2 05 57 0a 55") + link.encode()
    long_link = "https://example.com/" + "a" * 200  # 220 bytes: lengths need two bytes each
    assert messages.app_link(long_link) == hx("d2 05 df 01 0a dc 01") + long_link.encode()


def test_decoding_what_a_tv_sends_on_the_command_port():
    # device_info: model (1), vendor (2), 3 = 1, 4 = "12", package_name (5), app_version (6): 25 bytes
    device = hx("0a 04") + b"C7K " + hx("12 03") + b"TCL" + hx("18 01 22 02") + b"12"
    device += hx("2a 03") + b"svc" + hx("32 01") + b"6"
    configure = messages.decode_remote(hx("0a 1e 08 ff 04 12 19") + device)["remote_configure"]
    assert configure["code1"] == 639
    assert configure["device_info"]["model"] == "C7K " and configure["device_info"]["vendor"] == "TCL"
    assert configure["device_info"]["app_version"] == "6"

    short = messages.decode_remote(hx("0a 03 08 ff 04"))["remote_configure"]
    assert short["code1"] == 639 and "device_info" not in short

    assert messages.decode_remote(hx("12 00")) == {"remote_set_active": {"active": 0}}
    assert messages.decode_remote(hx("c2 02 02 08 01")) == {"remote_start": {"started": True}}
    assert messages.decode_remote(hx("c2 02 00")) == {"remote_start": {"started": False}}
    assert messages.decode_remote(hx("42 04 08 19 10 07"))["remote_ping_request"] == {"val1": 25, "val2": 7}
    assert messages.decode_remote(hx("42 00"))["remote_ping_request"]["val1"] == 0
    assert messages.decode_remote(hx("1a 02 08 01"))["remote_error"]["value"] is True


def test_messages_this_client_does_not_use_decode_to_nothing():
    ime = hx("a2 01 16 0a 14 62 12") + b"org.droidtv.playtv"
    volume = hx("92 03 0f 08 09 10 0a 1a 07") + b"TPM171E" + hx("20 01")
    for unknown in (ime, volume, hx("f8 07 01")):
        assert messages.decode_remote(unknown) == {}


def test_remote_error_is_not_decoded_recursively():
    # remote_error (3) embeds a whole message in its field 2.  A hostile peer could nest
    # thousands of them; the embedded message stays opaque, so depth costs nothing.
    message = hx("1a 02 08 01")
    for _ in range(3000):
        body = hx("12") + encode_varint(len(message)) + message
        message = hx("1a") + encode_varint(len(body)) + body
    assert len(message) < proto.MAX_FRAME
    decoded = messages.decode_remote(message)["remote_error"]
    assert decoded["value"] is False and len(decoded["message"]) > 10000


# ---------------------------------------------------------------------------- the generic codec

PERSON = Schema("Person", {"name": (1, STRING), "age": (2, INT), "tags": (3, [STRING]), "admin": (4, BOOL)})
GROUP3 = Schema("Group", {"lead": (1, PERSON), "members": (2, [PERSON]), "blob": (15, BYTES)}, proto3=True)


def test_decode_fills_defaults_and_reports_presence_of_messages():
    assert decode(PERSON, b"") == {"name": "", "age": 0, "tags": [], "admin": False}
    group = decode(GROUP3, b"")
    assert group == {"members": [], "blob": b""} and "lead" not in group


def test_encode_decode_round_trip_with_nesting_and_repeats():
    value = {"lead": {"name": "Ada", "age": 36, "tags": ["a", "b"], "admin": True}, "members": [{"name": "Bo"}, {}]}
    data = encode(GROUP3, value)
    #       lead: len 15         name "Ada"       age 36  tags "a"   tags "b"   admin
    assert data == hx("0a 0f 0a 03 41 64 61 10 24 1a 01 61 1a 01 62 20 01 12 04 0a 02 42 6f 12 00")
    decoded = decode(GROUP3, data)
    assert decoded["lead"] == value["lead"]
    assert [member["name"] for member in decoded["members"]] == ["Bo", ""]


def test_proto2_writes_zeros_and_proto3_omits_them():
    assert encode(PERSON, {"name": "", "age": 0, "admin": False}) == hx("0a 00 10 00 20 00")
    assert encode(GROUP3, {"blob": b"", "lead": {}}) == hx("0a 00")  # a present sub-message is still written
    assert encode(PERSON, {"age": None}) == b""


def test_schema_definitions_are_checked():
    with pytest.raises(ValueError, match="kind"):
        Schema("Bad", {"x": (1, "float")})
    with pytest.raises(ValueError, match="duplicate"):
        Schema("Bad", {"x": (1, INT), "y": (1, STRING)})
    assert "Person" in repr(PERSON)
    assert [field.name for field in Schema("Order", {"b": (2, INT), "a": (1, INT)}).fields] == ["a", "b"]


def test_encode_refuses_unknown_field_names():
    with pytest.raises(KeyError):
        encode(PERSON, {"nmae": "typo"})


def test_unknown_fields_of_every_wire_type_are_skipped():
    data = (
        hx("f8 06 2a")  # field 111, varint
        + hx("f9 06 01 02 03 04 05 06 07 08")  # field 111, 64-bit
        + hx("fd 06 01 02 03 04")  # field 111, 32-bit
        + hx("fa 06 03 61 62 63")  # field 111, length-delimited
        + hx("10 07")  # age = 7
    )
    assert decode(PERSON, data)["age"] == 7


def test_known_field_with_the_wrong_wire_type_is_skipped():
    assert decode(PERSON, hx("12 01 41"))["age"] == 0  # age sent as bytes
    assert decode(PERSON, hx("08 05"))["name"] == ""  # name sent as a varint
    assert messages.decode_remote(hx("c0 02 01")) == {}  # remote_start as a varint


def test_last_value_wins_and_strings_survive_bad_utf8():
    assert decode(PERSON, hx("10 01 10 02"))["age"] == 2
    assert decode(PERSON, hx("0a 02 ff fe"))["name"] == "��"


@pytest.mark.parametrize(
    "data",
    [
        "0a",  # tag without length
        "0a 05 41",  # length beyond the end
        "0a ff ff ff ff 0f",  # absurd length
        "09 01 02",  # truncated 64-bit
        "0d 01",  # truncated 32-bit
        "0b",  # group start: not supported
        "0c",  # group end
        "0e",  # wire type 6 does not exist
        "00 01",  # field number 0
        "80",  # truncated tag
    ],
)
def test_malformed_messages_raise_proto_error(data):
    with pytest.raises(ProtoError):
        decode(PERSON, hx(data))


def test_nesting_depth_is_bounded():
    node = Schema("Node", {"child": (1, BYTES)})
    node.by_name["child"].message = node  # a recursive type, which the real tables avoid
    node.by_name["child"].kind = "message"
    data = b""
    for _ in range(proto.MAX_DEPTH + 2):
        data = hx("0a") + encode_varint(len(data)) + data
    with pytest.raises(ProtoError, match="nested"):
        decode(node, data)
    shallow = hx("0a 02 0a 00")
    assert decode(node, shallow) == {"child": {"child": {}}}


def test_random_bytes_never_raise_anything_but_proto_error():
    rng = random.Random(20261110)
    for _ in range(3000):
        data = rng.randbytes(rng.randrange(0, 40))
        for decoder in (messages.decode_remote, messages.decode_pairing):
            try:
                decoder(data)
            except ProtoError:
                pass


# ---------------------------------------------------------------------------- framing


def test_encode_frame_prefixes_the_length():
    assert encode_frame(b"") == hx("00")
    assert encode_frame(b"abc") == hx("03 61 62 63")
    assert encode_frame(b"x" * 200)[:2] == hx("c8 01")
    assert len(encode_frame(b"x" * proto.MAX_FRAME)) == proto.MAX_FRAME + 3
    with pytest.raises(ProtoError):
        encode_frame(b"x" * (proto.MAX_FRAME + 1))


def test_frame_decoder_whole_and_multiple_frames():
    decoder = FrameDecoder()
    assert decoder.feed(hx("03 61 62 63")) == [b"abc"]
    assert decoder.feed(hx("01 41 00 02 42 43")) == [b"A", b"", b"BC"]
    assert decoder.buffered == 0


def test_frame_decoder_tolerates_any_split():
    frames = [b"abc", b"", b"y" * 200, b"z" * 20000, b"\x80\x01", b"end"]
    stream = b"".join(varint + body for varint, body in ((encode_varint(len(f)), f) for f in frames))
    assert stream[5:7] == hx("c8 01")  # the 200-byte frame has a two-byte length

    one_by_one = FrameDecoder()
    out = []
    for index in range(len(stream)):
        out += one_by_one.feed(stream[index : index + 1])
    assert out == frames and one_by_one.buffered == 0

    rng = random.Random(7)
    for _ in range(50):
        decoder, out, pos = FrameDecoder(), [], 0
        while pos < len(stream):
            step = rng.choice((1, 2, 3, 7, 100, 4096, 9000))
            out += decoder.feed(stream[pos : pos + step])
            pos += step
        assert out == frames


def test_frame_decoder_keeps_partial_frames():
    decoder = FrameDecoder()
    assert decoder.feed(hx("c8")) == []  # half a length
    assert decoder.feed(hx("01")) == []
    assert decoder.feed(b"x" * 199) == []
    assert decoder.buffered == 201
    assert decoder.feed(b"x" + hx("01")) == [b"x" * 200]
    assert decoder.buffered == 1


def test_frame_decoder_refuses_an_oversized_frame_before_its_body_arrives():
    decoder = FrameDecoder(max_size=1000)
    assert decoder.feed(encode_varint(1000) + b"a" * 1000) == [b"a" * 1000]
    with pytest.raises(ProtoError, match="larger"):
        decoder.feed(encode_varint(1001))
    with pytest.raises(ProtoError):
        FrameDecoder().feed(hx("80 80 80 08"))  # 16 MiB announced; nothing buffered for it


@pytest.mark.parametrize("data", ["80 80 80 80 80", "ff ff ff ff ff 0f"])
def test_frame_decoder_refuses_a_malformed_length(data):
    with pytest.raises(ProtoError):
        FrameDecoder().feed(hx(data))


def test_frame_decoder_memory_stays_bounded_under_a_flood_of_small_frames():
    decoder = FrameDecoder()
    total = 0
    for _ in range(200):
        total += len(decoder.feed(hx("00") * 4096))
        assert decoder.buffered == 0
    assert total == 200 * 4096
