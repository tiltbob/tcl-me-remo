"""HID report map for the emulated remote, and encoders for its input reports.

Three top-level application collections, each with its own report ID:

    1  Keyboard          2 bytes: two key usages (0x1E-0xF1; no letters, no modifiers)
    2  Consumer Control  4 bytes: two 16-bit usages (little endian), 0 = nothing pressed
    3  System Control    1 byte : bit0 Power Down, bit1 Sleep, bit2 Wake Up

The shape follows Google's reference Android TV remote, with the System Control collection
added because "System Wake Up" is the only standard usage that becomes KEY_WAKEUP.

THE REPORT MAP IS FROZEN.  A TV caches the report map (and attribute handles) of a bonded HID
device and Android before 14 never re-reads them, so changing a single byte here silently
breaks every existing pairing.  New keys must fit the declared usage ranges instead.

Over HID-over-GATT the report ID is not part of the characteristic value; it travels in the
Report Reference descriptor of each Report characteristic.  All ``*_report`` helpers here
therefore return the payload *without* the ID.
"""

from __future__ import annotations

from .keys import PAGE_CONSUMER, PAGE_KEYBOARD, PAGE_SYSTEM, Key

REPORT_ID_KEYBOARD = 1
REPORT_ID_CONSUMER = 2
REPORT_ID_SYSTEM = 3

REPORT_ID_OF_PAGE = {
    PAGE_KEYBOARD: REPORT_ID_KEYBOARD,
    PAGE_CONSUMER: REPORT_ID_CONSUMER,
    PAGE_SYSTEM: REPORT_ID_SYSTEM,
}

REPORT_LENGTH = {
    REPORT_ID_KEYBOARD: 2,
    REPORT_ID_CONSUMER: 4,
    REPORT_ID_SYSTEM: 1,
}

# fmt: off
REPORT_MAP = bytes([
    # ---- Keyboard ------------------------------------------------------------
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x06,        # Usage (Keyboard)
    0xA1, 0x01,        # Collection (Application)
    0x85, REPORT_ID_KEYBOARD,  # Report ID
    0x05, 0x07,        #   Usage Page (Keyboard/Keypad)
    0x19, 0x1E,        #   Usage Minimum (0x1E, the "1" key)
    0x29, 0xF1,        #   Usage Maximum (0xF1)
    0x15, 0x1E,        #   Logical Minimum (0x1E)
    0x26, 0xF1, 0x00,  #   Logical Maximum (0x00F1)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x02,        #   Report Count (2)
    0x81, 0x00,        #   Input (Data, Array)                two key slots
    0xC0,              # End Collection
    # ---- Consumer Control ----------------------------------------------------
    0x05, 0x0C,        # Usage Page (Consumer)
    0x09, 0x01,        # Usage (Consumer Control)
    0xA1, 0x01,        # Collection (Application)
    0x85, REPORT_ID_CONSUMER,  # Report ID
    0x19, 0x01,        #   Usage Minimum (0x0001)
    0x2A, 0xFF, 0x03,  #   Usage Maximum (0x03FF)
    0x15, 0x01,        #   Logical Minimum (1)
    0x26, 0xFF, 0x03,  #   Logical Maximum (0x03FF)
    0x75, 0x10,        #   Report Size (16)
    0x95, 0x02,        #   Report Count (2)
    0x81, 0x00,        #   Input (Data, Array)                two consumer usage slots
    0xC0,              # End Collection
    # ---- System Control ------------------------------------------------------
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x80,        # Usage (System Control)
    0xA1, 0x01,        # Collection (Application)
    0x85, REPORT_ID_SYSTEM,    # Report ID
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x03,        #   Report Count (3)
    0x09, 0x81,        #   Usage (System Power Down)          bit 0
    0x09, 0x82,        #   Usage (System Sleep)               bit 1
    0x09, 0x83,        #   Usage (System Wake Up)             bit 2
    0x81, 0x02,        #   Input (Data, Variable, Absolute)
    0x95, 0x05,        #   Report Count (5)
    0x81, 0x01,        #   Input (Constant)                   padding
    0xC0,              # End Collection
])
# fmt: on

# HID Information characteristic: bcdHID 1.11, country code 0, flags = RemoteWake.
HID_INFORMATION = bytes([0x11, 0x01, 0x00, 0x01])

# Report Reference descriptor "report type" values.
REPORT_TYPE_INPUT = 1
REPORT_TYPE_OUTPUT = 2
REPORT_TYPE_FEATURE = 3


def press_report(key: Key) -> tuple[int, bytes]:
    """Return ``(report_id, payload)`` for the report that presses ``key``."""
    if key.page == PAGE_KEYBOARD:
        return REPORT_ID_KEYBOARD, bytes([key.usage, 0])
    if key.page == PAGE_CONSUMER:
        return REPORT_ID_CONSUMER, key.usage.to_bytes(2, "little") + b"\x00\x00"
    if key.page == PAGE_SYSTEM:
        return REPORT_ID_SYSTEM, bytes([1 << (key.usage - 0x81)])
    raise ValueError(f"key {key.spec!r} has unknown page {key.page!r}")


def release_report(report_id: int) -> bytes:
    """Return the all-keys-up payload for ``report_id``."""
    return bytes(REPORT_LENGTH[report_id])
