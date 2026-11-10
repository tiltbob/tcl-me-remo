"""Key macros: a tiny text language for "press these remote keys in this order".

A macro is a list of tokens separated by spaces and/or commas:

    HOME            tap a key (see keys.py for names; raw usages like C:0223 work too)
    DOWN*3          tap it three times
    HOME~1.5        hold it for 1.5 seconds (long press)
    2s   300ms      wait

Example - open the TV's input list, go to the top, pick the third entry:

    SETTINGS 1.5s UP*6 DOWN*2 OK

``{port}`` and ``{port-1}`` style placeholders are substituted with the configured HDMI port
before parsing (see :func:`render`), so a preset can say ``DOWN*{port}``.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from . import keys

MAX_STEPS = 64
MAX_REPEAT = 30
MAX_HOLD = 10.0
MAX_WAIT = 30.0
MAX_TOTAL_SECONDS = 120.0

_WAIT_RE = re.compile(r"^(\d+(?:\.\d+)?)(MS|S)$")
_PLACEHOLDER_RE = re.compile(r"\{\s*port\s*(?:([+-])\s*(\d+))?\s*\}", re.IGNORECASE)


class MacroError(ValueError):
    """The macro text is invalid."""


@dataclass(frozen=True)
class Step:
    """One macro step: either a key press (``key`` set) or a pause (``wait`` > 0)."""

    key: str = ""       # canonical key spec; empty for a wait step
    repeat: int = 1
    hold: float = 0.0   # seconds; 0 means a normal tap
    wait: float = 0.0   # seconds; only for wait steps

    @property
    def is_wait(self) -> bool:
        return not self.key


@dataclass(frozen=True)
class Preset:
    name: str
    title: str
    description: str
    template: str


# Built-in input-switching presets.  They are starting points: TV firmware differs, so the
# UI lets the user run them, watch the TV, and switch to a custom macro if needed.
PRESETS: dict[str, Preset] = {}


def register_preset(preset: Preset) -> None:
    validate(preset.template)  # must be valid for every port
    PRESETS[preset.name] = preset


def render(template: str, hdmi_port: int) -> str:
    """Substitute ``{port}``, ``{port-1}``, ``{port+2}`` placeholders (clamped at 0)."""

    def sub(match: re.Match[str]) -> str:
        value = hdmi_port
        if match.group(1):
            delta = int(match.group(2))
            value = value + delta if match.group(1) == "+" else value - delta
        return str(max(0, value))

    return _PLACEHOLDER_RE.sub(sub, template)


def _number(text: str, what: str, token: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise MacroError(f"bad {what} in {token!r}") from None


def parse(text: str) -> list[Step]:
    """Parse macro text into steps.  Raises :class:`MacroError` with a readable reason."""
    steps: list[Step] = []
    for token in re.split(r"[\s,;]+", text.strip()):
        if not token:
            continue
        upper = token.upper()
        wait = _WAIT_RE.match(upper)
        if wait:
            seconds = float(wait.group(1)) / (1000.0 if wait.group(2) == "MS" else 1.0)
            if seconds > MAX_WAIT:
                raise MacroError(f"wait {token!r} is longer than {MAX_WAIT:g}s")
            if seconds > 0:
                steps.append(Step(wait=seconds))
            continue

        body, repeat, hold = upper, 1, 0.0
        if "*" in body:
            body, _, count = body.rpartition("*")
            if not count.isdigit() or body == "":
                raise MacroError(f"bad repeat count in {token!r} (write e.g. DOWN*3)")
            repeat = int(count)
            if repeat > MAX_REPEAT:
                raise MacroError(f"repeat count in {token!r} is more than {MAX_REPEAT}")
        if "~" in body:
            body, _, seconds_text = body.partition("~")
            hold = _number(seconds_text, "hold time", token)
            if not 0 < hold <= MAX_HOLD:
                raise MacroError(f"hold time in {token!r} must be between 0 and {MAX_HOLD:g} seconds")
        if repeat == 0:
            continue  # e.g. DOWN*{port-1} on port 1: a no-op, not an error
        try:
            key = keys.resolve(body)
        except keys.UnknownKey as exc:
            if re.match(r"^\d", body):
                raise MacroError(f"{token!r} is not a key or a wait (waits need a unit: 2s, 300ms)") from None
            raise MacroError(str(exc)) from None
        steps.append(Step(key=key.spec, repeat=repeat, hold=hold))

    if len(steps) > MAX_STEPS:
        raise MacroError(f"macro has {len(steps)} steps; the limit is {MAX_STEPS}")
    return steps


def validate(text: str) -> str:
    """Check macro text (which may contain ``{port}`` placeholders) for every HDMI port.

    Returns the text with whitespace normalised.  Raises :class:`MacroError` if it is invalid.
    """
    cleaned = " ".join(text.split())
    for port in (1, 2, 3, 4):
        parse(render(cleaned, port))
    return cleaned


def _fmt_seconds(seconds: float) -> str:
    if seconds < 1 and round(seconds * 1000) == seconds * 1000:
        return f"{int(round(seconds * 1000))}ms"
    return f"{seconds:g}s"


def format_steps(steps: list[Step]) -> str:
    """Inverse of :func:`parse` (canonical text form)."""
    out: list[str] = []
    for step in steps:
        if step.is_wait:
            out.append(_fmt_seconds(step.wait))
            continue
        token = step.key
        if step.hold:
            token += f"~{step.hold:g}"
        if step.repeat != 1:
            token += f"*{step.repeat}"
        out.append(token)
    return " ".join(out)


def estimate_seconds(steps: list[Step], *, key_press: float, key_gap: float) -> float:
    """How long :func:`run` will take with the given timings."""
    total = 0.0
    for step in steps:
        if step.is_wait:
            total += step.wait
        else:
            total += step.repeat * ((step.hold or key_press) + key_gap)
    return total


def check_runnable(steps: list[Step], *, key_press: float, key_gap: float) -> None:
    total = estimate_seconds(steps, key_press=key_press, key_gap=key_gap)
    if total > MAX_TOTAL_SECONDS:
        raise MacroError(f"macro would take {total:.0f}s; the limit is {MAX_TOTAL_SECONDS:.0f}s")


SendKey = Callable[[str, float], Awaitable[None]]


async def run(
    steps: list[Step],
    send_key: SendKey,
    *,
    key_press: float,
    key_gap: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Play ``steps``.

    ``send_key(spec, hold_seconds)`` must press the key, keep it down for ``hold_seconds``
    and release it.  A gap of ``key_gap`` follows every key so the TV UI can keep up.
    """
    check_runnable(steps, key_press=key_press, key_gap=key_gap)
    for step in steps:
        if step.is_wait:
            await sleep(step.wait)
            continue
        for _ in range(step.repeat):
            await send_key(step.key, step.hold or key_press)
            await sleep(key_gap)
