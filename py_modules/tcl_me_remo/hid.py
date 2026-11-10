"""HID report map for the emulated remote, and encoders for its input reports.

Three top-level application collections, each with its own report ID:

    1  Keyboard          8 bytes: modifier bits, reserved, six key usages
    2  Consumer Control  2 bytes: one 16-bit usage (little endian), 0 = nothing pressed
    3  System Control    1 byte : bit0 Power Down, bit1 Sleep, bit2 Wake Up

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
    REPORT_ID_KEYBOARD: 8,
    REPORT_ID_CONSUMER: 2,
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
    0x19, 0xE0,        #   Usage Minimum (Left Control)
    0x29, 0xE7,        #   Usage Maximum (Right GUI)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x08,        #   Report Count (8)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)   modifier bits
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x01,        #   Input (Constant)                   reserved byte
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x00,  #   Logical Maximum (255)
    0x05, 0x07,        #   Usage Page (Keyboard/Keypad)
    0x19, 0x00,        #   Usage Minimum (0)
    0x2A, 0xFF, 0x00,  #   Usage Maximum (255)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x06,        #   Report Count (6)
    0x81, 0x00,        #   Input (Data, Array)                six key slots
    0xC0,              # End Collection
    # ---- Consumer Control ----------------------------------------------------
    0x05, 0x0C,        # Usage Page (Consumer)
    0x09, 0x01,        # Usage (Consumer Control)
    0xA1, 0x01,        # Collection (Application)
    0x85, REPORT_ID_CONSUMER,  # Report ID
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x03,  #   Logical Maximum (0x03FF)
    0x19, 0x00,        #   Usage Minimum (0)
    0x2A, 0xFF, 0x03,  #   Usage Maximum (0x03FF)
    0x75, 0x10,        #   Report Size (16)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x00,        #   Input (Data, Array)                one consumer usage
    0xC0,              # End Collection
    # ---- System Control ------------------------------------------------------
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x80,        # Usage (System Control)
    0xA1, 0x01,        # Collection (Application)
    0x85, REPORT_ID_SYSTEM,    # Report ID
    0x19, 0x81,        #   Usage Minimum (System Power Down)
    0x29, 0x83,        #   Usage Maximum (System Wake Up)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x03,        #   Report Count (3)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)   power-down, sleep, wake bits
    0x75, 0x05,        #   Report Size (5)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x01,        #   Input (Constant)                   padding
    0xC0,              # End Collection
])
# fmt: on

# HID Information characteristic: bcdHID 1.11, country code 0, flags (bit0 RemoteWake, bit1
# NormallyConnectable).
HID_INFORMATION = bytes([0x11, 0x01, 0x00, 0x03])

# Report Reference descriptor "report type" values.
REPORT_TYPE_INPUT = 1
REPORT_TYPE_OUTPUT = 2
REPORT_TYPE_FEATURE = 3


def press_report(key: Key) -> tuple[int, bytes]:
    """Return ``(report_id, payload)`` for the report that presses ``key``."""
    if key.page == PAGE_KEYBOARD:
        if 0xE0 <= key.usage <= 0xE7:  # a modifier is a bit, not a key slot
            return REPORT_ID_KEYBOARD, bytes([1 << (key.usage - 0xE0), 0, 0, 0, 0, 0, 0, 0])
        return REPORT_ID_KEYBOARD, bytes([0, 0, key.usage, 0, 0, 0, 0, 0])
    if key.page == PAGE_CONSUMER:
        return REPORT_ID_CONSUMER, key.usage.to_bytes(2, "little")
    if key.page == PAGE_SYSTEM:
        return REPORT_ID_SYSTEM, bytes([1 << (key.usage - 0x81)])
    raise ValueError(f"key {key.spec!r} has unknown page {key.page!r}")


def release_report(report_id: int) -> bytes:
    """Return the all-keys-up payload for ``report_id``."""
    return bytes(REPORT_LENGTH[report_id])
