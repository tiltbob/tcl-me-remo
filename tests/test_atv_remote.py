"""Commands over the Wi-Fi assist: one-shot sessions, error classification, time limits."""

import asyncio
import errno
import inspect
import logging
import shutil
import socket
import ssl
import time

import pytest
from fake_tv import CLIENT_CERT, CLIENT_KEY, free_port, new_assist, pair, parse, running_tv, seed_identity

from tcl_me_remo import interfaces
from tcl_me_remo.atv import AtvAssist, remote, util
from tcl_me_remo.atv.proto import ProtoError
from tcl_me_remo.interfaces import KEYCODE_SLEEP, KEYCODE_WAKEUP, TransportError

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl tool")

SHORT = 3  # RemoteDirection: one complete press
LINK = "content://android.media.tv/passthrough/com.tcl.tvinput%2F.TvPassThroughService%2FHW16"


@pytest.fixture
async def tv(tmp_path):
    async with running_tv(tmp_path / "tv") as fake:
        yield fake


@pytest.fixture
def state(tmp_path):
    return tmp_path / "var" / "atv"


@pytest.fixture
async def assist(tv, state):
    """An assist paired with the fake TV."""
    seed_identity(state)
    assist = new_assist(tv, state)
    await pair(assist, tv)
    await tv.idle()
    return assist


async def failure(awaitable) -> TransportError:
    with pytest.raises(TransportError) as raised:
        await awaitable
    message = raised.value.message
    assert message and len(message) < 160 and "\n" not in message
    assert "Error(" not in message and "Errno" not in message and "Traceback" not in message
    return raised.value


# ---------------------------------------------------------------------------- happy paths


async def test_send_key_is_one_short_press_on_a_one_shot_connection(tv, assist):
    await assist.send_key(tv.host, KEYCODE_SLEEP, timeout=3.0)
    assert tv.keys == [(223, SHORT)] and tv.links == []
    await tv.idle()
    assert tv.remote_connections == 1 and tv.endings == ["eof"]  # closed in an orderly way

    # The handshake: both of the TV's remote_configure messages were answered, then set_active.
    assert len(tv.configures) == 2
    for configure in tv.configures:
        assert configure[1] == [611]  # PING | KEY | POWER | VOLUME | APP_LINK; no IME, no voice
        assert parse(configure[2][0]) == {3: [1], 4: [b"1"], 5: [b"atvremote"], 6: [b"1.0.0"]}
    assert tv.actives == [611]

    await assist.send_key(tv.host, KEYCODE_WAKEUP, timeout=3.0)
    assert tv.keys == [(223, SHORT), (224, SHORT)]
    await tv.idle()
    assert tv.remote_connections == 2
    assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []


async def test_launch_app_link_sends_the_link_verbatim(tv, assist):
    await assist.launch_app_link(tv.host, LINK, timeout=3.0)
    assert tv.links == [LINK] and tv.keys == []
    await tv.idle()
    assert tv.endings == ["eof"]


async def test_probe_reports_power_state_and_name_without_sending_anything(tv, assist):
    assert await assist.probe(tv.host, timeout=3.0) == {"on": True, "name": "Fake Model"}
    tv.started = False
    assert await assist.probe(tv.host, timeout=3.0) == {"on": False, "name": "Fake Model"}
    tv.model = ""
    assert (await assist.probe(tv.host, timeout=3.0))["name"] == "FakeCo"
    tv.vendor = ""
    assert (await assist.probe(tv.host, timeout=3.0))["name"] == "Fake TV"  # the name learned when pairing
    assert tv.keys == [] and tv.links == []
    await tv.idle()


async def test_keys_are_sent_to_a_tv_in_standby_too(tv, assist):
    tv.started = False
    await assist.send_key(tv.host, KEYCODE_WAKEUP, timeout=3.0)
    assert tv.keys == [(224, SHORT)]


async def test_names_from_the_tv_are_cleaned(tv, assist):
    tv.model = "Evil\x1b[31m\nModel" + "x" * 500
    name = (await assist.probe(tv.host, timeout=3.0))["name"]
    assert name.startswith("Evil [31m Model") and len(name) <= 64


async def test_only_features_the_tv_offers_are_requested(tv, assist, caplog):
    tv.supported = 2 | 32 | 4 | 8  # KEY, POWER and the two we never ask for
    await assist.launch_app_link(tv.host, LINK, timeout=3.0)
    assert [configure[1] for configure in tv.configures] == [[34], [34]]
    assert tv.actives == [34]
    assert tv.links == [LINK]  # sent regardless; nothing says a TV refuses it
    assert "does not offer app links" in caplog.text


async def test_tv_that_does_not_offer_key_presses_gets_the_key_anyway(tv, assist, caplog):
    tv.supported = 1 | 512
    await assist.send_key(tv.host, 26, timeout=3.0)
    assert tv.keys == [(26, SHORT)] and tv.actives == [513]
    assert "does not offer key presses" in caplog.text


async def test_messages_split_into_single_bytes_are_reassembled(tv, assist):
    tv.dribble = True
    await assist.send_key(tv.host, 26, timeout=5.0)
    assert tv.keys == [(26, SHORT)]


async def test_pings_are_answered_during_the_handshake_and_while_lingering(tv, assist):
    tv.ping_first = True
    tv.ping_interval = 0.04
    assist.linger_seconds = 0.4
    began = time.monotonic()
    await assist.send_key(tv.host, 26, timeout=3.0)
    elapsed = time.monotonic() - began
    assert 0.4 <= elapsed < 0.8  # lingered, and no longer
    await tv.idle()
    assert tv.pongs[0] == 0  # the handshake ping carried value 0, which is echoed
    echoed = tv.pongs[1:]
    assert len(echoed) >= 4 and echoed == list(range(1, len(echoed) + 1))
    assert tv.pings_sent - len(echoed) <= 1  # at most the one in flight when we closed


@pytest.mark.parametrize("what", ["key", "probe"])
async def test_tv_that_ignores_our_goodbye_costs_little_extra_time(tv, assist, what):
    """The TV had its time during the linger; its answer to the TLS goodbye changes nothing."""
    tv.deaf = "command" if what == "key" else "start"
    assist.linger_seconds = 0.1
    began = time.monotonic()
    if what == "key":
        await assist.send_key(tv.host, 26, timeout=3.0)
        assert tv.keys == [(26, SHORT)]
    else:
        assert (await assist.probe(tv.host, timeout=3.0))["on"] is True
    assert time.monotonic() - began < 0.45  # linger + a short wait, not asyncio's long one
    assert remote.CLOSE_SECONDS <= 0.15


async def test_first_command_after_pairing_waits_for_the_tv_to_settle(tv, state):
    seed_identity(state)
    assist = new_assist(tv, state)
    assist.settle_seconds = 0.3
    await pair(assist, tv)
    error = await failure(assist.probe(tv.host, timeout=0.1))  # no time to wait: do not connect early
    assert error.code == "wifi_unreachable" and tv.remote_connections == 0
    began = time.monotonic()
    assert (await assist.probe(tv.host, timeout=3.0))["on"] is True
    assert 0.15 <= time.monotonic() - began < 0.6
    began = time.monotonic()
    await assist.probe(tv.host, timeout=3.0)
    assert time.monotonic() - began < 0.2


def test_assist_implements_the_interface():
    members = vars(interfaces.WifiAssist).items()
    wanted = [name for name, member in members if inspect.isfunction(member) and not name.startswith("_")]
    assert set(wanted) >= {"status", "discover", "start_pairing", "finish_pairing", "cancel_pairing", "unpair"}
    for name in wanted:
        ours = inspect.signature(getattr(AtvAssist, name))
        theirs = inspect.signature(getattr(interfaces.WifiAssist, name))
        assert list(ours.parameters) == list(theirs.parameters), name
        assert inspect.iscoroutinefunction(getattr(AtvAssist, name)) == (name != "status")


# ---------------------------------------------------------------------------- not paired


async def test_without_any_pairing_nothing_is_sent(tv, state):
    assist = new_assist(tv, state)
    for attempt in (assist.send_key(tv.host, 26, timeout=1.0), assist.probe(tv.host, timeout=1.0)):
        assert (await failure(attempt)).code == "wifi_not_paired"
    seed_identity(state)  # an identity alone (pairing never completed) is not a pairing
    assist = new_assist(tv, state)
    assert assist.status()["paired"] is False
    assert (await failure(assist.launch_app_link(tv.host, LINK, timeout=1.0))).code == "wifi_not_paired"
    assert tv.remote_connections == 0 and tv.open_connections == 0


async def test_identity_removed_behind_our_back(tv, state):
    seed_identity(state)
    first = new_assist(tv, state)
    await pair(first, tv)
    assist = new_assist(tv, state)  # a fresh start: nothing cached
    assert assist.status()["paired"] is True
    (state / CLIENT_KEY).unlink()
    (state / CLIENT_CERT).unlink()
    assert (await failure(assist.send_key(tv.host, 26, timeout=1.0))).code == "wifi_not_paired"
    assert assist.status()["paired"] is False


@pytest.mark.parametrize(
    ("reject", "tls"),
    [
        ("tls", None),  # TLS 1.3: our handshake completes, then the TV closes without a word
        ("tls", ssl.TLSVersion.TLSv1_2),  # TLS 1.2: the handshake itself is cut
        ("eof", None),
        ("reset", None),
        ("reset", ssl.TLSVersion.TLSv1_2),
    ],
)
async def test_tv_that_forgot_us_means_not_paired(tv, assist, reject, tls):
    tv.paired.clear()
    tv.reject = reject
    if tls is not None:
        tv.limit_tls(tls)
    began = time.monotonic()
    error = await failure(assist.send_key(tv.host, 26, timeout=3.0))
    assert error.code == "wifi_not_paired" and "pair" in error.message
    assert time.monotonic() - began < 1.0  # known at once, not after the timeout
    assert tv.keys == []
    assert assist.status()["paired"] is True  # the identity is kept: the TV may only have been restarting
    await tv.idle()


async def test_tls_alert_means_not_paired(tv, assist, state, caplog):
    caplog.set_level(logging.DEBUG)
    alerting = new_assist(tv, state, remote_port=await tv.start_alert_port())
    error = await failure(alerting.probe(tv.host, timeout=3.0))
    assert error.code == "wifi_not_paired"
    assert "CERTIFICATE_UNKNOWN" in caplog.text.upper()  # the detail is logged, not shown


async def test_changed_tv_certificate_is_refused(tv, assist):
    await assist.send_key(tv.host, 26, timeout=3.0)
    tv.change_certificate()
    for attempt in (assist.send_key(tv.host, 26, timeout=3.0), assist.probe(tv.host, timeout=3.0)):
        error = await failure(attempt)
        assert error.code == "wifi_not_paired"
        assert "identity changed" in error.message and "pair the Wi-Fi assist again" in error.message
    assert tv.keys == [(26, SHORT)]  # nothing reached the impostor
    assert tv.configures and len(tv.configures) == 2  # not even our half of the handshake
    await tv.idle()


async def test_pin_applies_whatever_the_address(tv, assist, state, tmp_path):
    """Another TV at another address is not the paired one either."""
    async with running_tv(tmp_path / "other", name="Other TV") as other:
        other.client_certs.append(state / CLIENT_CERT)
        other.paired = set(tv.paired)  # even if it would accept us
        elsewhere = new_assist(other, state)
        error = await failure(elsewhere.send_key("localhost", 26, timeout=3.0))
        assert error.code == "wifi_not_paired" and other.keys == []


# ---------------------------------------------------------------------------- unreachable (worth retrying)


async def test_nothing_listening_is_unreachable(tv, assist, state):
    closed = new_assist(tv, state, remote_port=free_port())
    began = time.monotonic()
    error = await failure(closed.send_key(tv.host, 26, timeout=3.0))
    assert error.code == "wifi_unreachable" and "refused" in error.message
    assert time.monotonic() - began < 0.5


async def test_unknown_host_name_is_unreachable(tv, assist):
    began = time.monotonic()
    error = await failure(assist.send_key("no-such-tv.invalid", 26, timeout=1.0))
    assert error.code == "wifi_unreachable"
    assert time.monotonic() - began < 1.2


@pytest.mark.parametrize("host", [".local", "a..b", "x" * 64 + ".example"])
async def test_host_names_the_resolver_refuses_are_a_configuration_mistake(tv, assist, host):
    """Final, like any other malformed address: trying again cannot help."""
    for attempt in (assist.probe(host, timeout=1.0), assist.send_key(host, 26, timeout=1.0)):
        error = await failure(attempt)
        assert error.code == "wifi_not_configured" and "not valid" in error.message
    assert tv.remote_connections == 0


async def test_connecting_by_name_sends_no_server_name(tv, assist):
    """By name or by address, the TV sees the same TLS handshake."""
    await assist.send_key("localhost", 26, timeout=3.0)
    assert tv.keys == [(26, SHORT)]
    assert tv.server_names and set(tv.server_names) == {None}


@pytest.mark.parametrize("behaviour", ["no_tls", "silent", "slow", "never_start"])
async def test_tv_not_answering_is_unreachable_within_the_time_limit(tv, assist, state, behaviour):
    if behaviour == "no_tls":
        assist = new_assist(tv, state, remote_port=await tv.start_mute_port())
    elif behaviour == "silent":
        tv.silent = True
    elif behaviour == "slow":
        tv.delay = 0.7
    else:
        tv.never_start = True
    began = time.monotonic()
    error = await failure(assist.send_key(tv.host, 26, timeout=0.4))
    elapsed = time.monotonic() - began
    assert error.code == "wifi_unreachable" and "did not answer" in error.message
    assert 0.25 <= elapsed <= 0.45, elapsed  # never later than asked; a little early to close properly
    assert tv.keys == []
    await tv.idle()  # the socket is closed, not left to the garbage collector


async def test_tv_hanging_up_mid_handshake_is_unreachable(tv, assist):
    tv.close_after_configure = True
    error = await failure(assist.send_key(tv.host, 26, timeout=3.0))
    assert error.code == "wifi_unreachable" and "closed the connection" in error.message


@pytest.mark.parametrize("how", ["close", "reset"])
@pytest.mark.parametrize("delay", [0.0, 0.05])
async def test_tv_dropping_the_connection_at_remote_start_never_counts_as_sent(tv, assist, how, delay, caplog):
    """Nothing is acknowledged, so a connection that goes as the command is written is the only
    sign that the command went nowhere: with no delay it is gone before the write, with a
    delay the write is on its way when the TV hangs up.  Either way the caller must retry."""
    caplog.set_level(logging.INFO)
    tv.drop_at_start, tv.drop_delay = how, delay
    assist.linger_seconds = 0.5
    began = time.monotonic()
    for attempt in (assist.send_key(tv.host, 26, timeout=3.0), assist.launch_app_link(tv.host, LINK, timeout=3.0)):
        error = await failure(attempt)
        assert error.code == "wifi_unreachable" and "closed the connection" in error.message
    assert time.monotonic() - began < 0.8  # known at once, not after the linger
    assert tv.keys == [] and tv.links == []
    assert "sent key" not in caplog.text and "asked the TV" not in caplog.text
    await tv.idle()


async def test_probe_needs_nothing_after_remote_start(tv, assist):
    tv.drop_at_start = "close"
    assert (await assist.probe(tv.host, timeout=3.0))["on"] is True


async def test_tv_hanging_up_during_the_linger_leaves_delivery_unknown(tv, assist, caplog):
    """The TV read the command and hung up.  From here that looks the same as a TV that hung
    up without reading it, so it is reported as worth another try (commands are idempotent)."""
    caplog.set_level(logging.INFO)
    tv.close_after_command = True
    assist.linger_seconds = 1.0
    began = time.monotonic()
    error = await failure(assist.send_key(tv.host, 26, timeout=3.0))
    assert error.code == "wifi_unreachable" and "closed the connection" in error.message
    assert time.monotonic() - began < 0.5  # no point lingering on a closed connection
    assert tv.keys == [(26, SHORT)]
    assert "may not have acted" in caplog.text and "sent key" not in caplog.text


async def test_time_limit_covers_the_linger(tv, assist):
    assist.linger_seconds = 5.0
    began = time.monotonic()
    await assist.send_key(tv.host, 26, timeout=0.5)
    elapsed = time.monotonic() - began
    assert 0.3 <= elapsed <= 0.55, elapsed
    assert tv.keys == [(26, SHORT)]
    await tv.idle()
    assert tv.endings == ["eof"]


@pytest.mark.parametrize("timeout", [0, -1, 0.0])
async def test_no_time_at_all_is_unreachable(tv, assist, timeout):
    assert (await failure(assist.send_key(tv.host, 26, timeout=timeout))).code == "wifi_unreachable"
    assert tv.remote_connections == 0


# ---------------------------------------------------------------------------- errors (final)


@pytest.mark.parametrize("behaviour", ["garbage", "oversize", "flood"])
async def test_nonsense_from_the_tv_is_an_error_and_is_noticed_at_once(tv, assist, behaviour):
    setattr(tv, behaviour, True)
    began = time.monotonic()
    error = await failure(assist.send_key(tv.host, 26, timeout=4.0))
    assert error.code == "wifi_error"
    assert time.monotonic() - began < 2.0
    assert tv.keys == []
    await tv.idle()


async def test_remote_error_during_the_handshake(tv, assist):
    tv.error_on = "handshake"
    error = await failure(assist.send_key(tv.host, 26, timeout=3.0))
    assert error.code == "wifi_error" and "reported an error" in error.message
    assert tv.keys == []


async def test_remote_error_in_answer_to_the_command(tv, assist):
    tv.error_on = "command"
    assist.linger_seconds = 0.5
    error = await failure(assist.launch_app_link(tv.host, LINK, timeout=3.0))
    assert error.code == "wifi_error"
    assert tv.links == [LINK]
    await tv.idle()


@pytest.mark.parametrize("keycode", [0, -5, 70000, True, "26", 26.0, None])
async def test_invalid_key_codes_are_refused_locally(tv, assist, keycode):
    assert (await failure(assist.send_key(tv.host, keycode, timeout=1.0))).code == "wifi_error"
    assert tv.remote_connections == 0


@pytest.mark.parametrize("link", ["", "   ", "x" * 3000, None])
async def test_invalid_links_are_refused_locally(tv, assist, link):
    assert (await failure(assist.launch_app_link(tv.host, link, timeout=1.0))).code == "wifi_error"
    assert tv.remote_connections == 0


async def test_invalid_host_and_timeout(tv, assist):
    assert (await failure(assist.send_key("", 26, timeout=1.0))).code == "wifi_not_configured"
    assert (await failure(assist.probe("a b", timeout=1.0))).code == "wifi_not_configured"
    assert (await failure(assist.send_key(tv.host, 26, timeout=float("nan")))).code == "wifi_error"
    assert (await failure(assist.send_key(tv.host, 26, timeout="soon"))).code == "wifi_error"


# ---------------------------------------------------------------------------- cancellation


@pytest.mark.parametrize("stage", ["tls", "handshake", "linger"])
async def test_cancellation_closes_the_socket(tv, assist, state, stage):
    if stage == "tls":
        assist = new_assist(tv, state, remote_port=await tv.start_mute_port())
    elif stage == "handshake":
        tv.silent = True
    else:
        assist.linger_seconds = 10.0
    task = asyncio.create_task(assist.send_key(tv.host, 26, timeout=10.0))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if tv.open_connections == 1 and (stage != "linger" or tv.keys):
            break
    assert tv.open_connections == 1
    began = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - began < 0.2  # cancellation is not held up by an orderly close
    await tv.idle(1.0)
    assert [other for other in asyncio.all_tasks() if other is not asyncio.current_task()] == []


async def test_concurrent_commands_do_not_disturb_each_other(tv, assist):
    await asyncio.gather(*(assist.send_key(tv.host, 20 + index, timeout=3.0) for index in range(5)))
    assert sorted(tv.keys) == [(20 + index, SHORT) for index in range(5)]
    await tv.idle()
    assert tv.remote_connections == 5


# ---------------------------------------------------------------------------- classification, case by case


def test_connect_errors():
    cases = [
        (TimeoutError(), "wifi_unreachable"),
        (ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"), "wifi_unreachable"),
        (OSError(errno.EHOSTUNREACH, "No route to host"), "wifi_unreachable"),
        (OSError(errno.ENETUNREACH, "Network is unreachable"), "wifi_unreachable"),
        (socket.gaierror(socket.EAI_NONAME, "Name or service not known"), "wifi_unreachable"),
        (OSError("Multiple exceptions: [Errno 111] Connect call failed ('::1', 6466, 0, 0)"), "wifi_unreachable"),
        (ssl.SSLError(1, "[SSL: SSLV3_ALERT_CERTIFICATE_UNKNOWN] sslv3 alert certificate unknown"), "wifi_not_paired"),
        (ssl.SSLError(1, "[SSL: TLSV1_ALERT_UNKNOWN_CA] tlsv1 alert unknown ca"), "wifi_not_paired"),
        (ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"), "wifi_not_paired"),
        (ConnectionAbortedError(), "wifi_not_paired"),
        (BrokenPipeError(), "wifi_not_paired"),
    ]
    for exc, code in cases:
        error = remote.connect_error("192.168.1.50", exc)
        assert error.code == code, exc
        assert "192.168.1.50" in error.message
        assert "Errno" not in error.message and "SSL:" not in error.message and "('" not in error.message
    assert "no route to host" in remote.connect_error("tv", OSError(errno.EHOSTUNREACH, "No route to host")).message
    assert util.describe(ConnectionResetError()) == "connection lost" and util.describe(OSError()) == "system error"
    assert util.clean_name("  a\tb\x00c  " + "d" * 100, 8) == "a b c dd"


def test_session_errors_depend_on_whether_the_tv_spoke():
    from tcl_me_remo.atv.channel import ChannelClosed

    alert = ssl.SSLError(1, "[SSL: TLSV13_ALERT_CERTIFICATE_REQUIRED] tlsv13 alert certificate required")
    for exc in (ChannelClosed(), ConnectionResetError(), BrokenPipeError(), alert):
        assert remote.session_error("tv", exc, heard_from_tv=False).code == "wifi_not_paired"
        assert remote.session_error("tv", exc, heard_from_tv=True).code == "wifi_unreachable"
    for heard in (False, True):
        assert remote.session_error("tv", TimeoutError(), heard_from_tv=heard).code == "wifi_unreachable"
        assert remote.session_error("tv", ProtoError("bad"), heard_from_tv=heard).code == "wifi_error"
