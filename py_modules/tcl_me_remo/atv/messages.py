"""The message tables of the Android TV Remote protocol v2 and builders for what we send.

Only the fields this client reads or writes are listed; anything else a TV sends is skipped
by the decoder.  Numbers are the published field numbers of the pairing ("Polo") and remote
message sets.
"""

from __future__ import annotations

from typing import Any

from .proto import BOOL, BYTES, INT, STRING, Schema, decode, encode

# ---------------------------------------------------------------------------- pairing, TCP 6467

PROTOCOL_VERSION = 2
STATUS_OK = 200
STATUS_ERROR = 400
STATUS_BAD_CONFIGURATION = 401
STATUS_BAD_SECRET = 402

ENCODING_HEXADECIMAL = 3
ROLE_INPUT = 1
CODE_LENGTH = 6  # characters shown on the TV
SERVICE_NAME = "atvremote"

_ENCODING = Schema("Encoding", {"type": (1, INT), "symbol_length": (2, INT)})
_EMPTY = Schema("Empty", {})

OUTER = Schema(
    "OuterMessage",
    {
        "protocol_version": (1, INT),
        "status": (2, INT),
        "pairing_request": (10, Schema("PairingRequest", {"service_name": (1, STRING), "client_name": (2, STRING)})),
        "pairing_request_ack": (11, Schema("PairingRequestAck", {"server_name": (1, STRING)})),
        "options": (
            20,
            Schema(
                "Options",
                {
                    "input_encodings": (1, [_ENCODING]),
                    "output_encodings": (2, [_ENCODING]),
                    "preferred_role": (3, INT),
                },
            ),
        ),
        "configuration": (30, Schema("Configuration", {"encoding": (1, _ENCODING), "client_role": (2, INT)})),
        "configuration_ack": (31, _EMPTY),
        "secret": (40, Schema("Secret", {"secret": (1, BYTES)})),
        "secret_ack": (41, Schema("SecretAck", {"secret": (1, BYTES)})),
    },
)


def _outer(kind: str, body: dict[str, Any]) -> bytes:
    return encode(OUTER, {"protocol_version": PROTOCOL_VERSION, "status": STATUS_OK, kind: body})


def pairing_request(client_name: str) -> bytes:
    return _outer("pairing_request", {"service_name": SERVICE_NAME, "client_name": client_name})


def options() -> bytes:
    encoding = {"type": ENCODING_HEXADECIMAL, "symbol_length": CODE_LENGTH}
    return _outer("options", {"input_encodings": [encoding], "preferred_role": ROLE_INPUT})


def configuration() -> bytes:
    encoding = {"type": ENCODING_HEXADECIMAL, "symbol_length": CODE_LENGTH}
    return _outer("configuration", {"encoding": encoding, "client_role": ROLE_INPUT})


def secret(digest: bytes) -> bytes:
    return _outer("secret", {"secret": digest})


def decode_pairing(data: bytes) -> dict[str, Any]:
    return decode(OUTER, data)


# ---------------------------------------------------------------------------- remote, TCP 6466

FEATURE_PING = 1 << 0
FEATURE_KEY = 1 << 1
FEATURE_IME = 1 << 2  # on-screen keyboard mirroring; makes some TVs show a hint.  Not requested.
FEATURE_VOICE = 1 << 3  # voice search sessions.  Not requested.
FEATURE_POWER = 1 << 5
FEATURE_VOLUME = 1 << 6
FEATURE_APP_LINK = 1 << 9
WANTED_FEATURES = FEATURE_PING | FEATURE_KEY | FEATURE_POWER | FEATURE_VOLUME | FEATURE_APP_LINK  # 611

DIRECTION_START_LONG = 1  # key down
DIRECTION_END_LONG = 2  # key up
DIRECTION_SHORT = 3  # one complete press

_DEVICE_INFO = Schema(
    "RemoteDeviceInfo",
    {
        "model": (1, STRING),
        "vendor": (2, STRING),
        "unknown1": (3, INT),
        "unknown2": (4, STRING),
        "package_name": (5, STRING),
        "app_version": (6, STRING),
    },
    proto3=True,
)

REMOTE = Schema(
    "RemoteMessage",
    {
        "remote_configure": (
            1,
            Schema("RemoteConfigure", {"code1": (1, INT), "device_info": (2, _DEVICE_INFO)}, proto3=True),
        ),
        "remote_set_active": (2, Schema("RemoteSetActive", {"active": (1, INT)}, proto3=True)),
        # RemoteError embeds the offending message; it is kept opaque (it is only reported).
        "remote_error": (3, Schema("RemoteError", {"value": (1, BOOL), "message": (2, BYTES)}, proto3=True)),
        "remote_ping_request": (8, Schema("RemotePingRequest", {"val1": (1, INT), "val2": (2, INT)}, proto3=True)),
        "remote_ping_response": (9, Schema("RemotePingResponse", {"val1": (1, INT)}, proto3=True)),
        "remote_key_inject": (
            10,
            Schema("RemoteKeyInject", {"key_code": (1, INT), "direction": (2, INT)}, proto3=True),
        ),
        "remote_start": (40, Schema("RemoteStart", {"started": (1, BOOL)}, proto3=True)),
        "remote_app_link_launch_request": (
            90,
            Schema("RemoteAppLinkLaunchRequest", {"app_link": (1, STRING)}, proto3=True),
        ),
    },
    proto3=True,
)


def remote_configure(features: int) -> bytes:
    device_info = {"unknown1": 1, "unknown2": "1", "package_name": "atvremote", "app_version": "1.0.0"}
    return encode(REMOTE, {"remote_configure": {"code1": features, "device_info": device_info}})


def remote_set_active(features: int) -> bytes:
    return encode(REMOTE, {"remote_set_active": {"active": features}})


def ping_response(value: int) -> bytes:
    return encode(REMOTE, {"remote_ping_response": {"val1": value}})


def key_inject(key_code: int, direction: int = DIRECTION_SHORT) -> bytes:
    return encode(REMOTE, {"remote_key_inject": {"key_code": key_code, "direction": direction}})


def app_link(link: str) -> bytes:
    return encode(REMOTE, {"remote_app_link_launch_request": {"app_link": link}})


def decode_remote(data: bytes) -> dict[str, Any]:
    return decode(REMOTE, data)
