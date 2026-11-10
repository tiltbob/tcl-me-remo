"""Bluetooth transport: an emulated LE remote control (HID over GATT) on top of BlueZ.

    gatt.py     the frozen HID GATT table as D-Bus objects
    advert.py   the on-demand LE advertisement
    agent.py    the pairing agent used inside the pairing window
    adapter.py  a live mirror of bluetoothd's adapters and devices
    rawhci.py   direct delivery of notifications when bluetoothd has lost the TV's subscription
    store.py    the little that must be remembered across reboots
    remote.py   BleRemote, the state machine that implements interfaces.Remote
"""

from .remote import BleRemote

__all__ = ["BleRemote"]
