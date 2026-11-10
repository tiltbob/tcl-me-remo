"""Bluetooth transport: an emulated LE remote control (HID over GATT) on top of BlueZ.

    gatt.py     the frozen HID GATT table as D-Bus objects
    advert.py   the on-demand LE advertisement
    agent.py    the pairing agent used inside the pairing window
    adapter.py  a live mirror of bluetoothd's adapters and devices
    rawhci.py   direct delivery of notifications when bluetoothd has lost the TV's subscription
    kernel.py   which kernels direct delivery leaves no trace on (pure; the doctor uses it too)
    mgmt.py     ending the TV link at once, through the kernel, before the machine sleeps
    store.py    the little that must be remembered across reboots
    remote.py   BleRemote, the state machine that implements interfaces.Remote
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .remote import BleRemote

__all__ = ["BleRemote"]


def __getattr__(name: str) -> Any:
    # BleRemote is loaded on first use, not with the package: kernel.py must stay importable
    # (by the doctor) where the transport, or the D-Bus library under it, is not.
    if name == "BleRemote":
        from .remote import BleRemote

        return BleRemote
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
