"""asyncio helpers that behave the same on every Python this project supports."""

from __future__ import annotations

import asyncio
from typing import Awaitable, TypeVar

T = TypeVar("T")


async def wait_for(awaitable: Awaitable[T], timeout: float | None) -> T:
    """``asyncio.wait_for`` that never loses a cancellation.

    On Python 3.11, ``asyncio.wait_for`` returns normally when its task is cancelled at the
    moment the thing it waits for completes: the cancellation is swallowed (fixed in 3.12,
    which rebuilt it on ``asyncio.timeout``).  A task cancelled like that carries on as if
    nothing had happened, and whoever cancelled it waits for it to end.  That is how a "TV on"
    could hang for a minute: it called off the background task that keeps the link up just as
    bluetoothd answered that task's last call, and the task went back to sleep instead.

    Raises ``TimeoutError`` after ``timeout`` seconds (``None``: never), as the original does.
    """
    async with asyncio.timeout(timeout):
        return await awaitable
