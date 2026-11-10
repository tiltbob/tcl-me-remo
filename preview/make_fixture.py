#!/usr/bin/env python3
"""Print what the real service answers to get_config and status, as JSON.

The browser preview's fake backend starts from this, so the schema, defaults, presets, key
names and the shape of the status come from the service's own code and cannot drift from it.
Run by preview/build.mjs; needs nothing beyond the repository and Python 3.11+.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "py_modules"))

from tcl_me_remo import config as config_mod  # noqa: E402
from tcl_me_remo.interfaces import BleState  # noqa: E402
from tcl_me_remo.service import Service  # noqa: E402


class StubRemote:
    """Just enough of a Bluetooth remote for the service to describe itself."""

    on_paired = None

    def __init__(self) -> None:
        self.state = BleState()

    def apply_config(self, cfg) -> None:
        pass


async def collect() -> dict:
    with tempfile.TemporaryDirectory() as empty_sysfs:
        service = Service(config_mod.default_config(), remote=StubRemote(), sys_root=empty_sysfs)
        return {
            "get_config": await service.rpc("get_config", {}),
            "status": await service.rpc("status", {}),
        }


if __name__ == "__main__":
    json.dump(asyncio.run(collect()), sys.stdout, indent=1)
    sys.stdout.write("\n")
