"""aio.wait_for: a time limit that cannot lose a cancellation."""

import asyncio
import re
from pathlib import Path

import pytest

from tcl_me_remo import aio

PACKAGE = Path(aio.__file__).parent


async def test_a_cancellation_that_meets_the_result_is_not_lost():
    # Python 3.11's asyncio.wait_for returns the result here and the task carries on,
    # uncancelled.  The Bluetooth transport's background task did exactly that when a "TV on"
    # called it off at the moment bluetoothd answered its last call, and "TV on" then waited
    # a minute for a task that had gone back to sleep.
    future = asyncio.get_running_loop().create_future()
    went_on = False

    async def worker():
        nonlocal went_on
        await aio.wait_for(future, 60)
        went_on = True
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(worker())
    await asyncio.sleep(0)       # the worker is waiting
    future.set_result("reply")   # the answer arrives ...
    task.cancel()                # ... and the task is called off in the same breath
    done, _pending = await asyncio.wait([task], timeout=5)
    try:
        assert task in done and task.cancelled() and not went_on
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_result_time_out_and_no_limit():
    async def answer(after):
        await asyncio.sleep(after)
        return "done"

    assert await aio.wait_for(answer(0), 5) == "done"
    assert await aio.wait_for(answer(0), None) == "done"
    with pytest.raises(TimeoutError):
        await aio.wait_for(answer(3600), 0.01)

    async def refuses():
        raise ValueError("no")

    with pytest.raises(ValueError):
        await aio.wait_for(refuses(), 5)


async def test_a_plain_cancellation_stays_a_cancellation():
    task = asyncio.ensure_future(aio.wait_for(asyncio.sleep(3600), 60))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_the_package_waits_through_the_helper():
    # asyncio.wait_for is fine on Python 3.12 and later, where CI would never notice it coming
    # back; on 3.11 it loses cancellations.  Nothing in the package calls it directly.
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        name = path.relative_to(PACKAGE).as_posix()
        if "_vendor" in path.parts or name == "aio.py":
            continue
        if re.search(r"\basyncio\.wait_for\(", path.read_text(encoding="utf-8")):
            offenders.append(name)
    assert offenders == []
