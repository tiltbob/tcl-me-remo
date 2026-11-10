"""Pairing the Wi-Fi assist with a (fake) TV: the secret, the two-step flow, what is stored."""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import ssl
import stat
import time

import pytest
from fake_tv import (
    CLIENT_CERT,
    CLIENT_KEY,
    RECORD,
    expected_secret,
    fingerprint,
    free_port,
    make_certificate,
    new_assist,
    pair,
    running_tv,
    seed_identity,
)

from tcl_me_remo.atv import AtvAssist, certs
from tcl_me_remo.atv.pairing import CodeError, minimal_bytes, pairing_secret
from tcl_me_remo.interfaces import TransportError

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl tool")

NOT_PAIRING = {"paired": False, "pairing": False, "pairing_host": ""}


@pytest.fixture
async def tv(tmp_path):
    async with running_tv(tmp_path / "tv") as fake:
        yield fake


@pytest.fixture
def state(tmp_path):
    return tmp_path / "var" / "atv"


@pytest.fixture
async def assist(tv, state):
    """An assist that already has a key and certificate (creating them is tested separately)."""
    seed_identity(state)
    assist = new_assist(tv, state)
    yield assist
    await assist.close()


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


async def until(condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.01)


def other_code(state, tv) -> str:
    """A code that is well-formed and self-consistent, but for another nonce than the TV's."""
    nonce = bytes([int(tv.code[2:4], 16) ^ 0xFF, int(tv.code[4:6], 16)])
    client_der = ssl.PEM_cert_to_DER_cert((state / CLIENT_CERT).read_text())
    digest = expected_secret(client_der, tv.certificate_der, nonce)
    return f"{digest[0]:02X}{nonce.hex().upper()}"


# ---------------------------------------------------------------------------- the secret


def test_secret_reference_vector():
    """Worked out with the published recipe: SHA-256(client n | client e | server n | server e | nonce)."""
    digest = hashlib.sha256(bytes.fromhex("c0ffee01" "010001" "deadbeef" "010001" "abcd")).digest()
    assert digest.hex() == "8216d1d283f27463280c6c6b00be3b67aa31b7f8ba0e1e0da69c4bd325889c41"
    assert pairing_secret((0xC0FFEE01, 65537), (0xDEADBEEF, 65537), "82ABCD") == digest


def test_secret_accepts_lower_case_and_spaces():
    numbers = (0xC0FFEE01, 65537), (0xDEADBEEF, 65537)
    expected = pairing_secret(*numbers, "82ABCD")
    for typed in ("82abcd", " 82 AB cd\n", "82AbCd"):
        assert pairing_secret(*numbers, typed) == expected


def test_secret_uses_minimal_big_endian_numbers():
    assert minimal_bytes(65537) == b"\x01\x00\x01"
    assert minimal_bytes(0x00FF) == b"\xff"
    assert minimal_bytes(0x0ABC) == b"\x0a\xbc"  # an odd number of hex digits
    assert minimal_bytes(1 << 2047)[0] == 0x80 and len(minimal_bytes(1 << 2047)) == 256
    # client and server are not interchangeable, and the exponent takes part
    a, b = (0x0ABC, 3), (0xDEADBEEF, 65537)
    expected = hashlib.sha256(bytes.fromhex("0abc" "03" "deadbeef" "010001" "0102")).digest()
    code = f"{expected[0]:02X}0102"
    assert pairing_secret(a, b, code) == expected
    swapped = hashlib.sha256(bytes.fromhex("deadbeef" "010001" "0abc" "03" "0102")).digest()
    assert swapped != expected


@pytest.mark.parametrize("typed", ["", "82ABC", "82ABCDE", "82ABCG", "0x82AB", "82-ABCD", "８２ABCD", "²2ABCD"])
def test_secret_refuses_codes_of_the_wrong_shape(typed):
    with pytest.raises(CodeError, match="six characters"):
        pairing_secret((0xC0FFEE01, 65537), (0xDEADBEEF, 65537), typed)


def test_secret_check_byte_catches_typos():
    numbers = (0xC0FFEE01, 65537), (0xDEADBEEF, 65537)
    for typo in ("83ABCD", "82ABCE", "82BACD", "000000"):
        with pytest.raises(CodeError, match="not the code"):
            pairing_secret(*numbers, typo)


def test_secret_from_real_certificates_matches_an_independent_computation(tmp_path):
    """Numbers from our DER reader against numbers printed by the openssl tool."""
    seed_identity(tmp_path / "a")
    client_der = ssl.PEM_cert_to_DER_cert((tmp_path / "a" / CLIENT_CERT).read_text())
    server_cert, _ = make_certificate(tmp_path, "tv", "/CN=independent")
    server_der = ssl.PEM_cert_to_DER_cert(server_cert.read_text())
    nonce = bytes.fromhex("5a01")
    wanted = expected_secret(client_der, server_der, nonce)
    code = f"{wanted[0]:02x}5a01"
    ours = pairing_secret(certs.rsa_public_numbers(client_der), certs.rsa_public_numbers(server_der), code)
    assert ours == wanted


# ---------------------------------------------------------------------------- the flow


async def test_status_before_anything(tv, state):
    assert new_assist(tv, state).status() == NOT_PAIRING
    assert not state.exists()  # nothing is created until pairing is asked for


async def test_pairing_end_to_end_creates_identity_and_record(tv, state, caplog):
    caplog.set_level(logging.DEBUG)
    assist = new_assist(tv, state, client_name="Steam Deck")
    await assist.start_pairing(tv.host)
    assert assist.status() == {"paired": False, "pairing": True, "pairing_host": tv.host}
    assert tv.pairing_requests == [("atvremote", "Steam Deck")]
    assert len(tv.code) == 6
    code = tv.code

    await assist.finish_pairing(code)
    assert assist.status() == {"paired": True, "pairing": False, "pairing_host": ""}
    client_der = ssl.PEM_cert_to_DER_cert((state / CLIENT_CERT).read_text())
    assert tv.paired == {fingerprint(client_der)}
    await tv.idle()  # the pairing connection is closed

    assert mode(state) == 0o700
    assert {path.name: mode(path) for path in state.iterdir()} == {CLIENT_KEY: 0o600, CLIENT_CERT: 0o600, RECORD: 0o600}
    record = json.loads((state / RECORD).read_text())
    assert record == {"version": 1, "host": tv.host, "fingerprint": fingerprint(tv.certificate_der), "name": "Fake TV"}

    # Neither the code nor key material reaches the log, even at DEBUG.
    assert "it now shows a code" in caplog.text and "paired with Fake TV" in caplog.text
    assert code not in caplog.text and code.lower() not in caplog.text
    assert "PRIVATE KEY" not in caplog.text

    # A new process finds the pairing again.
    assert new_assist(tv, state).status()["paired"] is True


async def test_typo_is_caught_locally_and_the_same_connection_takes_the_right_code(tv, assist):
    await assist.start_pairing(tv.host)
    wrong = f"{int(tv.code[:2], 16) ^ 0x01:02X}{tv.code[2:]}"  # check byte off by one bit
    with pytest.raises(TransportError) as raised:
        await assist.finish_pairing(wrong)
    assert raised.value.code == "pairing_failed" and "type it again" in raised.value.message
    assert assist.status()["pairing"] is True and assist.status()["paired"] is False
    assert tv.bad_secrets == 0  # the TV was not asked: the attempt is not spent

    for malformed in ("", "12345", "GGGGGG", "1234567"):
        with pytest.raises(TransportError) as raised:
            await assist.finish_pairing(malformed)
        assert raised.value.code == "pairing_failed" and "six characters" in raised.value.message
    assert assist.status()["pairing"] is True

    await assist.finish_pairing(f" {tv.code[:3].lower()} {tv.code[3:]} ")
    assert assist.status() == {"paired": True, "pairing": False, "pairing_host": ""}
    assert len(tv.pairing_requests) == 1 and tv.bad_secrets == 0


async def test_code_the_tv_rejects_ends_the_session(tv, assist, state):
    await assist.start_pairing(tv.host)
    with pytest.raises(TransportError) as raised:
        await assist.finish_pairing(other_code(state, tv))
    assert raised.value.code == "pairing_failed" and "did not accept the code" in raised.value.message
    assert tv.bad_secrets == 1
    assert assist.status() == NOT_PAIRING
    assert not (state / RECORD).exists()
    await tv.idle()
    with pytest.raises(TransportError) as raised:
        await assist.finish_pairing("123456")
    assert raised.value.code == "pairing_failed" and "start pairing" in raised.value.message


async def test_finish_without_start(tv, assist):
    with pytest.raises(TransportError) as raised:
        await assist.finish_pairing("A1B2C3")
    assert raised.value.code == "pairing_failed"


async def test_cancel_drops_the_connection(tv, assist):
    await assist.cancel_pairing()  # nothing pending: not an error
    await assist.start_pairing(tv.host)
    code = tv.code
    assert tv.open_connections == 1
    await assist.cancel_pairing()
    assert assist.status() == NOT_PAIRING
    await tv.idle()
    with pytest.raises(TransportError) as raised:
        await assist.finish_pairing(code)
    assert raised.value.code == "pairing_failed"
    assert tv.paired == set()


async def test_held_pairing_times_out(tv, assist):
    assist.hold_seconds = 0.25
    began = time.monotonic()
    await assist.start_pairing(tv.host)
    code = tv.code
    assert assist.status()["pairing"] is True
    await asyncio.sleep(0.12)
    with pytest.raises(TransportError):
        await assist.finish_pairing("000000" if code != "000000" else "111111")  # a typo does not extend the hold
    assert assist.status()["pairing"] is True
    await until(lambda: not assist.status()["pairing"])
    assert 0.2 <= time.monotonic() - began <= 0.6
    assert assist.status() == NOT_PAIRING
    await tv.idle()
    with pytest.raises(TransportError) as raised:
        await assist.finish_pairing(code)
    assert raised.value.code == "pairing_failed"


async def test_tv_dismissing_the_dialog_ends_pairing(tv, assist):
    await assist.start_pairing(tv.host)
    tv.hang_up()
    await until(lambda: not assist.status()["pairing"])
    assert assist.status() == NOT_PAIRING


async def test_starting_again_replaces_the_pending_pairing(tv, assist):
    await assist.start_pairing(tv.host)
    first = tv.code
    await assist.start_pairing(tv.host)
    assert len(tv.pairing_requests) == 2
    await until(lambda: tv.open_connections == 1)
    assert assist.status() == {"paired": False, "pairing": True, "pairing_host": tv.host}
    await assist.finish_pairing(tv.code)
    assert assist.status()["paired"] is True
    assert first  # the first code was shown, then abandoned


async def test_pairing_tolerates_messages_of_unknown_kinds(tv, assist):
    tv.pair_noise = 3
    await pair(assist, tv)


@pytest.mark.parametrize("stage", ["request", "options", "configuration", "secret"])
async def test_pairing_tolerates_a_repeated_acknowledgement(tv, assist, stage):
    """A TV that says something twice is not a reason to fail (other clients do not mind either).

    ``pair`` finishes right after starting, so a repeated configuration_ack is still unread
    when the secret is sent: it must not be taken for the answer to the secret, nor burn the code.
    """
    tv.pair_repeat = stage
    await pair(assist, tv)
    assert tv.bad_secrets == 0 and len(tv.pairing_requests) == 1
    await tv.idle()


async def test_repeated_acknowledgement_seen_while_the_code_is_awaited(tv, assist):
    tv.pair_repeat = "configuration"
    await assist.start_pairing(tv.host)
    await asyncio.sleep(0.05)  # the watcher of the held connection reads the repeat
    assert assist.status()["pairing"] is True
    await assist.finish_pairing(tv.code)
    assert assist.status()["paired"] is True


async def test_repeats_without_end_fail_pairing(tv, assist):
    tv.pair_fault, tv.pair_fault_at = "silent", "secret"  # the TV never answers the secret itself
    assist.finish_seconds = 5.0
    await assist.start_pairing(tv.host)
    task = asyncio.create_task(assist.finish_pairing(tv.code))
    began = time.monotonic()
    while not task.done():
        assert time.monotonic() - began < 2.0  # long before the time limit: after a handful of repeats
        with contextlib.suppress(OSError):
            await tv.push(bytes.fromhex("08 02 10 c8 01 fa 01 00"))  # OK + configuration_ack (field 31), again
        await asyncio.sleep(0.005)
    with pytest.raises(TransportError) as raised:
        await task
    assert raised.value.code == "pairing_failed" and "too many" in raised.value.message
    assert assist.status() == NOT_PAIRING
    await tv.idle()


async def test_identity_is_kept_across_pairings_and_record_is_replaced(tv, assist, state, tmp_path):
    await pair(assist, tv)
    identity = (state / CLIENT_KEY).read_bytes(), (state / CLIENT_CERT).read_bytes()
    first_record = json.loads((state / RECORD).read_text())

    async with running_tv(tmp_path / "tv2", name="Bedroom TV") as second:
        again = new_assist(second, state)  # same state, as after a restart, pointed at another TV
        assert again.status()["paired"] is True
        await again.start_pairing("localhost")
        assert again.status()["paired"] is True  # the old pairing holds until the new one completes
        await again.finish_pairing(second.code)
        record = json.loads((state / RECORD).read_text())
        assert record["host"] == "localhost" and record["name"] == "Bedroom TV"
        assert record["fingerprint"] == fingerprint(second.certificate_der) != first_record["fingerprint"]
        assert ((state / CLIENT_KEY).read_bytes(), (state / CLIENT_CERT).read_bytes()) == identity
        assert sorted(path.name for path in state.iterdir()) == sorted([CLIENT_KEY, CLIENT_CERT, RECORD])


async def test_unpair_deletes_everything_and_the_next_pairing_gets_a_new_identity(tv, assist, state):
    await pair(assist, tv)
    old_cert = (state / CLIENT_CERT).read_bytes()
    await assist.start_pairing(tv.host)  # a pending pairing is dropped by unpair as well
    await assist.unpair()
    assert assist.status() == NOT_PAIRING
    assert list(state.iterdir()) == []
    await tv.idle()
    with pytest.raises(TransportError) as raised:
        await assist.send_key(tv.host, 26, timeout=1.0)
    assert raised.value.code == "wifi_not_paired"
    await assist.unpair()  # twice is fine

    await pair(assist, tv)
    assert (state / CLIENT_CERT).read_bytes() != old_cert
    assert mode(state / CLIENT_KEY) == 0o600
    assert len(tv.paired) == 2


# ---------------------------------------------------------------------------- failures


@pytest.mark.parametrize("stage", ["request", "options", "configuration"])
@pytest.mark.parametrize("fault", ["drop", "status", "garbage", "wrong_kind", "silent"])
async def test_faults_before_the_code_fail_pairing(tv, assist, stage, fault):
    tv.pair_fault, tv.pair_fault_at = fault, stage
    assist.start_seconds = 0.3 if fault == "silent" else 5.0
    began = time.monotonic()
    with pytest.raises(TransportError) as raised:
        await assist.start_pairing(tv.host)
    elapsed = time.monotonic() - began
    assert raised.value.code == "pairing_failed"
    assert "Error" not in raised.value.message and "Errno" not in raised.value.message
    assert 0.25 <= elapsed < 0.7 if fault == "silent" else elapsed < 2.0  # only silence waits for the deadline
    assert assist.status() == NOT_PAIRING
    await tv.idle()


@pytest.mark.parametrize("fault", ["drop", "status", "garbage", "silent"])
async def test_faults_after_the_code_fail_pairing(tv, assist, state, fault):
    tv.pair_fault, tv.pair_fault_at = fault, "secret"
    assist.finish_seconds = 0.3
    await assist.start_pairing(tv.host)
    with pytest.raises(TransportError) as raised:
        await assist.finish_pairing(tv.code)
    assert raised.value.code == "pairing_failed"
    assert assist.status() == NOT_PAIRING and not (state / RECORD).exists()
    await tv.idle()


async def test_pairing_with_nothing_listening_is_unreachable(tv, state):
    seed_identity(state)
    assist = new_assist(tv, state, pair_port=free_port())
    with pytest.raises(TransportError) as raised:
        await assist.start_pairing(tv.host)
    assert raised.value.code == "wifi_unreachable"
    assert assist.status() == NOT_PAIRING


async def test_pairing_with_a_tv_that_never_answers_is_unreachable_in_time(tv, state):
    seed_identity(state)
    assist = new_assist(tv, state, pair_port=await tv.start_mute_port())
    assist.start_seconds = 0.3
    began = time.monotonic()
    with pytest.raises(TransportError) as raised:
        await assist.start_pairing(tv.host)
    assert raised.value.code == "wifi_unreachable"
    assert 0.25 <= time.monotonic() - began <= 0.5
    await tv.idle()


async def test_pairing_port_that_refuses_tls_fails_pairing(tv, state):
    seed_identity(state)
    assist = new_assist(tv, state, pair_port=await tv.start_alert_port())
    with pytest.raises(TransportError) as raised:
        await assist.start_pairing(tv.host)
    assert raised.value.code == "pairing_failed" and "refused the secure connection" in raised.value.message


@pytest.mark.parametrize("host", ["", "   ", "two words", "x" * 300, "a..b", ".local", "x" * 64 + ".example"])
async def test_pairing_needs_a_plausible_address(tv, assist, host):
    with pytest.raises(TransportError) as raised:
        await assist.start_pairing(host)
    assert raised.value.code == "wifi_not_configured"
    assert raised.value.message in ("the TV address is not valid", "no TV address is set for the Wi-Fi assist")
    assert tv.pairing_requests == [] and assist.status() == NOT_PAIRING


async def test_pairing_without_openssl_says_so(tv, state):
    assist = new_assist(tv, state, openssl="/nonexistent/openssl-missing")
    with pytest.raises(TransportError) as raised:
        await assist.start_pairing(tv.host)
    assert raised.value.code == "wifi_error"
    assert "openssl" in raised.value.message and "not found" in raised.value.message
    assert assist.status() == NOT_PAIRING
    assert tv.pairing_requests == []
    assert not state.exists() or list(state.iterdir()) == []


async def test_existing_identity_is_used_even_without_openssl(tv, state):
    seed_identity(state)
    before = (state / CLIENT_CERT).read_bytes()
    assist = new_assist(tv, state, openssl="/nonexistent/openssl-missing")
    await pair(assist, tv)
    assert (state / CLIENT_CERT).read_bytes() == before


async def test_damaged_identity_is_reported_not_replaced(tv, state):
    state.mkdir(parents=True)
    (state / CLIENT_KEY).write_text("garbage")
    (state / CLIENT_CERT).write_text("garbage")
    assist = new_assist(tv, state)
    with pytest.raises(TransportError) as raised:
        await assist.start_pairing(tv.host)
    assert raised.value.code == "wifi_error" and "remove the Wi-Fi pairing" in raised.value.message
    assert (state / CLIENT_KEY).read_text() == "garbage"
    await assist.unpair()  # the way out the message names
    await pair(assist, tv)


async def test_cancelling_start_pairing_closes_the_socket(tv, assist):
    tv.pair_fault, tv.pair_fault_at = "silent", "options"
    task = asyncio.create_task(assist.start_pairing(tv.host))
    await until(lambda: tv.open_connections == 1 and len(tv.pairing_requests) == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await tv.idle()
    assert assist.status() == NOT_PAIRING
    tv.pair_fault = tv.pair_fault_at = ""
    await pair(assist, tv)  # and the assist is not left locked


async def test_cancelling_finish_pairing_drops_the_session(tv, assist):
    tv.pair_fault, tv.pair_fault_at = "silent", "secret"
    await assist.start_pairing(tv.host)
    task = asyncio.create_task(assist.finish_pairing(tv.code))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await tv.idle()
    assert assist.status() == NOT_PAIRING


@pytest.mark.parametrize("call", ["finish", "cancel", "start", "unpair"])
async def test_cancellation_while_taking_over_the_held_connection_does_not_leave_it_open(tv, assist, call):
    """Each of these first stops the watcher of the held connection.  Cancelled at that very
    point, none may leave a connection behind that nothing bounds any more."""
    assist.hold_seconds = 0.3
    await assist.start_pairing(tv.host)
    code = tv.code
    assert tv.open_connections == 1
    held = assist._session  # kept alive here: the connection must be closed on purpose, not by the garbage collector
    calls = {
        "finish": lambda: assist.finish_pairing(code),
        "cancel": assist.cancel_pairing,
        "start": lambda: assist.start_pairing(tv.host),
        "unpair": assist.unpair,
    }
    task = asyncio.create_task(calls[call]())
    await asyncio.sleep(0)  # the call has asked the watcher to stop and waits for it
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert assist.status() == NOT_PAIRING and not held.usable
    await tv.idle(1.0)  # closed at once, not after the hold time or never
    assert len(tv.pairing_requests) == 1 and tv.paired == set()
    await asyncio.sleep(0.4)  # past the original hold time: nothing fires late
    assert [other for other in asyncio.all_tasks() if other is not asyncio.current_task()] == []
    with pytest.raises(TransportError) as raised:
        await assist.finish_pairing(code)
    assert raised.value.code == "pairing_failed"
    await pair(assist, tv)  # and the assist is not left locked or confused


async def test_no_tasks_are_left_behind(tv, assist):
    await assist.start_pairing(tv.host)
    assert len(asyncio.all_tasks()) > 1  # the watcher of the held connection
    await assist.finish_pairing(tv.code)
    await tv.idle()
    await asyncio.sleep(0.02)
    assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []
    assert isinstance(assist, AtvAssist)


async def test_too_many_unknown_messages_fail_pairing(tv, assist):
    tv.pair_noise = 20
    with pytest.raises(TransportError) as raised:
        await assist.start_pairing(tv.host)
    assert raised.value.code == "pairing_failed" and "too many" in raised.value.message
    await tv.idle()


async def test_acknowledgement_that_does_not_echo_the_secret_is_accepted(tv, assist, caplog):
    caplog.set_level(logging.DEBUG)
    tv.pair_wrong_echo = True
    await pair(assist, tv)
    assert "does not echo" in caplog.text


@pytest.mark.parametrize(
    ("said", "reason"),
    [
        (bytes.fromhex("08 02 10 90 03"), "the TV ended pairing"),  # status 400
        (bytes.fromhex("0a 7f 01"), "does not understand"),
        (bytes.fromhex("08 02 10 c8 01"), ""),  # an empty OK message: ignored
    ],
)
async def test_what_the_tv_says_while_the_code_is_awaited(tv, assist, caplog, said, reason):
    caplog.set_level(logging.INFO)
    await assist.start_pairing(tv.host)
    await tv.push(said)
    if reason:
        await until(lambda: not assist.status()["pairing"])
        assert reason in caplog.text
        await tv.idle()
    else:
        await asyncio.sleep(0.1)
        await assist.finish_pairing(tv.code)
        assert assist.status()["paired"] is True


async def test_a_tv_that_chatters_while_the_code_is_awaited_is_dropped(tv, assist):
    await assist.start_pairing(tv.host)
    for _ in range(80):
        await tv.push(bytes.fromhex("08 02 10 c8 01"))
    await until(lambda: not assist.status()["pairing"])
    await tv.idle()


async def test_tv_with_a_certificate_that_is_not_rsa(state, tmp_path):
    seed_identity(state)
    async with running_tv(tmp_path / "ec", name="EC TV", elliptic=True) as tv:
        assist = new_assist(tv, state)
        with pytest.raises(TransportError) as raised:
            await assist.start_pairing(tv.host)
        assert raised.value.code == "pairing_failed" and "cannot pair with" in raised.value.message
        assert tv.pairing_requests == []
        await tv.idle()


async def test_key_creation_failures_are_reported_plainly(tv, state, tmp_path):
    failing = tmp_path / "openssl"
    failing.write_text("#!/bin/sh\nexit 1\n")
    failing.chmod(0o755)
    assist = new_assist(tv, state, openssl=str(failing))
    with pytest.raises(TransportError) as raised:
        await assist.start_pairing(tv.host)
    assert raised.value.code == "wifi_error" and "could not create the key" in raised.value.message
    assert assist.status() == NOT_PAIRING

    blocked = tmp_path / "a-file"
    blocked.write_text("in the way")
    assist = new_assist(tv, blocked / "atv")
    with pytest.raises(TransportError) as raised:
        await assist.start_pairing(tv.host)
    assert raised.value.code == "wifi_error" and "cannot write to" in raised.value.message
    assert "Errno" not in raised.value.message
