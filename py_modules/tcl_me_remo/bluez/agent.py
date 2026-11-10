"""The pairing agent we register with bluetoothd while pairing mode is open.

Why an agent is needed at all (kernel ``net/bluetooth/smp.c``, BlueZ ``src/adapter.c`` and
``src/device.c``): with LE Secure Connections the kernel raises a user-confirmation request
even for Just Works pairing, and bluetoothd forwards it as ``RequestAuthorization`` to the
*default* agent.  Without any agent the adapter is not even bondable.  The agent's capability
also becomes the adapter's IO capability, so ``NoInputNoOutput`` is what makes the TV fall
back to Just Works instead of asking for a passkey nobody can type.

The agent exists only inside the pairing window; outside it whoever was the default agent
before (Steam, a desktop applet, nobody) handles pairing exactly as if we were not there.
"""

from __future__ import annotations

import logging
from typing import Callable

from ..dbus import DBusError, ServiceInterface, method

log = logging.getLogger(__name__)

AGENT_PATH = "/org/tclmeremo/agent"
AGENT_CAPABILITY = "NoInputNoOutput"

ERR_REJECTED = "org.bluez.Error.Rejected"

#: ``(request kind, device object path) -> accept?``
Decision = Callable[[str, str], bool]


class PairingAgent(ServiceInterface):
    def __init__(self, decide: Decision, on_release: Callable[[], None] | None = None):
        super().__init__("org.bluez.Agent1")
        self._decide = decide
        self._on_release = on_release

    def _ask(self, kind: str, device: str) -> None:
        try:
            accepted = bool(self._decide(kind, device))
        except Exception:  # noqa: BLE001 - when in doubt, refuse
            log.exception("pairing decision failed")
            accepted = False
        if not accepted:
            log.info("pairing: rejected %s from %s", kind, device)
            raise DBusError(ERR_REJECTED, "pairing is not open for this device")
        log.info("pairing: accepted %s from %s", kind, device)

    @method()
    def Release(self):
        # bluetoothd dropped us (it is shutting down, or another agent replaced ours).
        if self._on_release is not None:
            self._on_release()

    @method()
    def RequestAuthorization(self, device: "o"):
        self._ask("authorization", device)

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        # Not expected with NoInputNoOutput, but harmless: there is nothing to compare.
        self._ask("confirmation", device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        self._ask("service", device)

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        raise DBusError(ERR_REJECTED, "this agent has no keyboard")

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        raise DBusError(ERR_REJECTED, "this agent has no keyboard")

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        raise DBusError(ERR_REJECTED, "this agent has no display")

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        raise DBusError(ERR_REJECTED, "this agent has no display")

    @method()
    def Cancel(self):
        log.debug("pairing: bluetoothd cancelled the pending agent request")
