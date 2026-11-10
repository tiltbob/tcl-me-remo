"""What the running kernel means for direct delivery (rawhci.py).  Pure functions.

A module of its own, with nothing but the standard library behind it, so that the doctor can
ask the same question as the transport without loading the transport: the doctor has to work
when the Bluetooth side, or the D-Bus library it needs, does not.

The fact these functions encode
    Mainline Linux clamps the per-connection packet counter from 6.17 on (``hci_event.c``
    ``hci_num_comp_pkts_evt``: "hcon %p sent %u < count %u"; ``hci_conn.c`` ``hci_conn_del``).
    The change went in between v6.17-rc3 and v6.17-rc4: release candidates 1 to 3 parse as
    6.17 and do not have it.

What they deliberately do not encode
    The clamp in ``hci_num_comp_pkts_evt`` was also backported to the stable series (it is in
    6.16.5, 6.12.45, 6.6.104, 6.1.150 and 5.15.191, and absent from the release before each;
    upstream 6.11.11 does not have it).  With it the counter cannot go below zero, so nothing
    is lost on those kernels either.  A vendor kernel's version does not say whether it
    carries the patch, though, so the rule stays "6.17 or newer": on a patched older kernel
    ``auto`` is more careful than it has to be, which is the side to err on.  Such a kernel
    gives itself away in ``dmesg`` ("sent 0 < count 1" for every injected packet).
"""

from __future__ import annotations

import platform
import re

#: First mainline release that clamps the per-connection and adapter-wide packet counters.
FIXED_KERNEL = (6, 17)
#: The first release candidate of FIXED_KERNEL that has the clamp.
FIXED_KERNEL_RC = 4

# "6.17.0-rc3", "6.17.0-rc3-1-mainline", "6.17.0-0.rc3.20250828git.fc43.x86_64", "6.17-rc3-amd64"
_RC = re.compile(r"(?<![a-z])rc(\d+)", re.IGNORECASE)
_PATCH_LEVEL_ZERO = re.compile(r"\d+\.\d+(\.0)?(?!\.?\d)")


def kernel_version(release: str | None = None) -> tuple[int, int] | None:
    """``"6.11.11-valve19-1-neptune-611"`` -> ``(6, 11)``; None if the text does not start like a version."""
    match = re.match(r"(\d+)\.(\d+)", release if release is not None else platform.release())
    return (int(match.group(1)), int(match.group(2))) if match else None


def direct_delivery_is_free(release: str | None = None) -> bool:
    """Whether injected packets are known to leave the kernel's packet accounting alone.

    True from Linux 6.17 on (from its fourth release candidate, to be exact).  A release that
    cannot be read counts as old: the decision this feeds ("use direct delivery without being
    asked") must err on the side of the user's other Bluetooth devices.
    """
    if release is None:
        release = platform.release()
    version = kernel_version(release)
    if version is None or version < FIXED_KERNEL:
        return False
    if version == FIXED_KERNEL and _PATCH_LEVEL_ZERO.match(release):
        # A release candidate of 6.17 itself (6.17.1-rc1 would be one of a later stable release).
        candidate = _RC.search(release)
        if candidate is not None and int(candidate.group(1)) < FIXED_KERNEL_RC:
            return False
    return True


def kernel_needs_credit_repair(release: str | None = None) -> bool:
    """True for kernels that may leak transmit credits after injected packets (rawhci.py).

    An unreadable release counts as old here too: the repair is harmless on a fixed kernel.
    """
    return not direct_delivery_is_free(release)
