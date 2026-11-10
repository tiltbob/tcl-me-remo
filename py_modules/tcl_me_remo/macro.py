"""Key macros: a tiny text language for "press these remote keys in this order".

A macro is a list of tokens separated by spaces and/or commas:

    HOME            tap a key (see keys.py for names; raw usages like C:0223 work too)
    DOWN*3          tap it three times
    HOME~1.5        hold it for 1.5 seconds (long press)
    2s   300ms      wait

Example - open the TV's input list, go to the top, pick the third entry:

    F3 1.5s UP*6 DOWN*2 OK

``{port}`` and ``{port-1}`` style placeholders are substituted with the configured HDMI port
before parsing (see :func:`render`), so a preset can say ``DOWN*{port}``.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from . import keys

# Longest macro text that is looked at at all.  It is the limit of the ``input.macro`` setting
# (config.py), applied to every other source of macro text as well: text arrives over the
# control socket, and parsing runs in the service's only event loop.
MAX_TEXT = 1024
MAX_STEPS = 64
MAX_REPEAT = 30
MAX_HOLD = 10.0
MAX_WAIT = 30.0
MAX_TOTAL_SECONDS = 120.0

_WAIT_RE = re.compile(r"^(\d+(?:\.\d+)?)(MS|S)$")
_PLACEHOLDER_RE = re.compile(r"\{\s*port\s*(?:([+-])\s*([0-9]+))?\s*\}", re.IGNORECASE)
# A piece of the user's text quoted in an error message is cut to this many characters: the
# message is stored (action history, log) and sent to every client that asks for the status.
QUOTE_CHARS = 40


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
            digits = match.group(2)
            # More digits than any port arithmetic needs: "a lot" (int() refuses very long
            # digit strings, and the result is clamped or rejected as a repeat count anyway).
            delta = int(digits) if len(digits) <= 6 else 10**6
            value = value + delta if match.group(1) == "+" else value - delta
        return str(max(0, value))

    return _PLACEHOLDER_RE.sub(sub, template)


def quote(text: str, limit: int = QUOTE_CHARS) -> str:
    """``repr(text)`` for an error message, cut short if the text is long."""
    return repr(text) if len(text) <= limit else repr(text[:limit] + "...")


def _number(text: str, what: str, token: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise MacroError(f"bad {what} in {quote(token)}") from None


def parse(text: str) -> list[Step]:
    """Parse macro text into steps.  Raises :class:`MacroError` with a readable reason.

    Text longer than MAX_TEXT is refused unread, which also bounds the work done here: at
    most MAX_TEXT / 2 tokens are ever looked at.
    """
    if len(text) > MAX_TEXT:
        raise MacroError(f"the macro is {len(text)} characters long; the limit is {MAX_TEXT}")
    steps: list[Step] = []
    for token in re.split(r"[\s,;]+", text.strip()):
        if not token:
            continue
        upper = token.upper()
        wait = _WAIT_RE.match(upper)
        if wait:
            seconds = float(wait.group(1)) / (1000.0 if wait.group(2) == "MS" else 1.0)
            if seconds > MAX_WAIT:
                raise MacroError(f"wait {quote(token)} is longer than {MAX_WAIT:g}s")
            if seconds > 0:
                steps.append(Step(wait=seconds))
            continue

        body, repeat, hold = upper, 1, 0.0
        if "*" in body:
            body, _, count = body.rpartition("*")
            # isdigit() alone also accepts characters int() does not take, such as "\u00b2".
            if not (count.isascii() and count.isdigit()) or body == "":
                raise MacroError(f"bad repeat count in {quote(token)} (write e.g. DOWN*3)")
            # Not int() on a long digit string: it refuses very long ones with a ValueError.
            repeat = int(count) if len(count) <= 6 else MAX_REPEAT + 1
            if repeat > MAX_REPEAT:
                raise MacroError(f"repeat count in {quote(token)} is more than {MAX_REPEAT}")
        if "~" in body:
            body, _, seconds_text = body.partition("~")
            hold = _number(seconds_text, "hold time", token)
            if not 0 < hold <= MAX_HOLD:
                raise MacroError(f"hold time in {quote(token)} must be between 0 and {MAX_HOLD:g} seconds")
        if repeat == 0:
            continue  # e.g. DOWN*{port-1} on port 1: a no-op, not an error
        try:
            if len(body) > QUOTE_CHARS:
                raise keys.UnknownKey(f"unknown key {quote(body)}")  # no key is that long; keep the message short
            key = keys.resolve(body)
        except keys.UnknownKey as exc:
            if re.match(r"^\d", body):
                raise MacroError(f"{quote(token)} is not a key or a wait (waits need a unit: 2s, 300ms)") from None
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


# --------------------------------------------------------------------------- built-in presets
#
# TCL's source list does not wrap around, so every preset first runs to one end of the list
# (more presses than the list can have entries) and then counts back to the wanted input.
# F3 is reported to open the source list on a TCL QM8; which key does so on a given
# firmware has to be confirmed by watching the TV.

for _preset in (
    Preset(
        name="tcl-row",
        title="TCL source row (2023 and newer)",
        description="F3 opens the source list, Left to the far end, Right once per HDMI number, OK. "
        "Assumes one entry (TV) sits before HDMI 1.",
        template="F3 1.5s LEFT*8 RIGHT*{port} OK",
    ),
    Preset(
        name="tcl-row-home",
        title="TCL source row, from the home screen",
        description="Same as the source row, but presses Home first. If the list does not open you end up "
        "on the home screen rather than in whatever was showing.",
        template="HOME 2s F3 1.5s LEFT*8 RIGHT*{port} OK",
    ),
    Preset(
        name="tcl-row-first",
        title="TCL source row, HDMI 1 listed first",
        description="For lists that start with HDMI 1 (no TV entry before it).",
        template="F3 1.5s LEFT*8 RIGHT*{port-1} OK",
    ),
    Preset(
        name="tcl-column",
        title="TCL source column (2022 firmware)",
        description="For the older vertical source list: F3, Up to the top, Down once per HDMI number, OK.",
        template="F3 1.5s UP*8 DOWN*{port} OK",
    ),
):
    register_preset(_preset)
del _preset
