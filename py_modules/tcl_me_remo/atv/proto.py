"""Protocol Buffers wire format, just enough for the Android TV Remote messages.

A message is described by a :class:`Schema`: a table of field names, numbers and kinds.
:func:`encode` and :func:`decode` turn plain dicts into bytes and back using such a table, so
the protocol modules never deal with tag bytes.  On the wire both ports carry
``varint(length) + message`` frames; :class:`FrameDecoder` reassembles them from a byte
stream that may be split anywhere.

Everything here treats its input as hostile: sizes are bounded, unknown fields are skipped,
and the only exception raised for bad data is :class:`ProtoError`.
"""

from __future__ import annotations

from typing import Any, Mapping

MAX_FRAME = 64 * 1024  # real messages are well under 1 KiB
MAX_DEPTH = 8

_WIRE_VARINT, _WIRE_FIXED64, _WIRE_BYTES, _WIRE_FIXED32 = 0, 1, 2, 5
_MAX_VARINT_BYTES = 10  # a 64-bit value
_MAX_LENGTH_BYTES = 5  # a frame length; far more than MAX_FRAME needs

# Field kinds.  Every scalar the protocol uses travels as a varint.
INT, BOOL, STRING, BYTES = "int", "bool", "string", "bytes"
_DEFAULTS: dict[str, Any] = {INT: 0, BOOL: False, STRING: "", BYTES: b""}


class ProtoError(ValueError):
    """The bytes are not a well-formed message or frame."""


class Field:
    __slots__ = ("name", "number", "kind", "message", "repeated")

    def __init__(self, name: str, number: int, spec: Any):
        self.name = name
        self.number = number
        self.repeated = isinstance(spec, list)
        if self.repeated:
            (spec,) = spec
        self.message: Schema | None = spec if isinstance(spec, Schema) else None
        self.kind: str = "message" if self.message is not None else spec
        if self.message is None and self.kind not in _DEFAULTS:
            raise ValueError(f"unknown field kind for {name}: {spec!r}")

    @property
    def wire_type(self) -> int:
        return _WIRE_VARINT if self.kind in (INT, BOOL) else _WIRE_BYTES


class Schema:
    """A message type: ``{field name: (number, kind)}``.

    ``kind`` is :data:`INT`, :data:`BOOL`, :data:`STRING`, :data:`BYTES`, another schema (a
    nested message), or a one-element list of any of those for a repeated field.

    ``proto3`` selects implicit presence: scalars equal to their default are left off the
    wire, as a proto3 encoder does.  Without it every value given is written (proto2).
    """

    def __init__(self, name: str, fields: Mapping[str, tuple[int, Any]], *, proto3: bool = False):
        self.name = name
        self.proto3 = proto3
        self.fields = [Field(field, number, spec) for field, (number, spec) in fields.items()]
        self.fields.sort(key=lambda field: field.number)
        self.by_name = {field.name: field for field in self.fields}
        self.by_number = {field.number: field for field in self.fields}
        if len(self.by_number) != len(self.fields):
            raise ValueError(f"duplicate field number in {name}")

    def __repr__(self) -> str:
        return f"<Schema {self.name}>"


# ---------------------------------------------------------------------------- varints


def encode_varint(value: int) -> bytes:
    """Base-128 varint.  Negative numbers are sent as 64-bit two's complement, as protobuf does."""
    if value < 0:
        value += 1 << 64
    if not 0 <= value < 1 << 64:
        raise ProtoError("number does not fit in 64 bits")
    out = bytearray()
    while value > 0x7F:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def decode_varint(data: bytes | bytearray | memoryview, pos: int = 0) -> tuple[int, int]:
    """Read a varint at ``pos``; returns ``(value, position after it)``."""
    value = 0
    for index in range(_MAX_VARINT_BYTES):
        if pos + index >= len(data):
            raise ProtoError("truncated varint")
        byte = data[pos + index]
        value |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            return value & 0xFFFFFFFFFFFFFFFF, pos + index + 1
    raise ProtoError("varint is too long")


# ---------------------------------------------------------------------------- messages


def encode(schema: Schema, values: Mapping[str, Any]) -> bytes:
    """Serialise ``values`` (field name -> value) in field-number order."""
    unknown = set(values) - set(schema.by_name)
    if unknown:
        raise KeyError(f"{schema.name} has no field {sorted(unknown)[0]!r}")
    out = bytearray()
    for field in schema.fields:
        value = values.get(field.name)
        if value is None:
            continue
        for item in value if field.repeated else (value,):
            if schema.proto3 and field.message is None and item == _DEFAULTS[field.kind]:
                continue
            out += encode_varint(field.number << 3 | field.wire_type)
            if field.kind in (INT, BOOL):
                out += encode_varint(int(item))
                continue
            if field.message is not None:
                body = encode(field.message, item)
            elif field.kind == STRING:
                body = str(item).encode("utf-8")
            else:
                body = bytes(item)
            out += encode_varint(len(body)) + body
    return bytes(out)


def decode(schema: Schema, data: bytes, *, _depth: int = 0) -> dict[str, Any]:
    """Parse one message.

    Scalars that are absent get their default (0, False, "", b"") and repeated fields an
    empty list; a nested message appears only if it was on the wire, so its presence can be
    tested with ``in``.  Fields the schema does not know, or that arrive with another wire
    type than expected, are skipped.
    """
    if _depth > MAX_DEPTH:
        raise ProtoError("message is nested too deeply")
    result: dict[str, Any] = {}
    for field in schema.fields:
        if field.repeated:
            result[field.name] = []
        elif field.message is None:
            result[field.name] = _DEFAULTS[field.kind]
    view = memoryview(data)
    pos, end = 0, len(data)
    while pos < end:
        key, pos = decode_varint(view, pos)
        number, wire_type = key >> 3, key & 7
        if number == 0:
            raise ProtoError("field number 0")
        raw: Any
        if wire_type == _WIRE_VARINT:
            raw, pos = decode_varint(view, pos)
        elif wire_type == _WIRE_BYTES:
            length, pos = decode_varint(view, pos)
            if length > end - pos:
                raise ProtoError("field runs past the end of the message")
            raw = bytes(view[pos : pos + length])
            pos += length
        elif wire_type in (_WIRE_FIXED64, _WIRE_FIXED32):
            size = 8 if wire_type == _WIRE_FIXED64 else 4
            if size > end - pos:
                raise ProtoError("field runs past the end of the message")
            pos += size
            continue  # no message we handle uses fixed-width fields
        else:
            raise ProtoError(f"unsupported wire type {wire_type}")
        field = schema.by_number.get(number)
        if field is None or field.wire_type != wire_type:
            continue
        value: Any
        if field.message is not None:
            value = decode(field.message, raw, _depth=_depth + 1)
        elif field.kind == BOOL:
            value = raw != 0
        elif field.kind == STRING:
            value = raw.decode("utf-8", "replace")
        else:
            value = raw
        if field.repeated:
            result[field.name].append(value)
        else:
            result[field.name] = value
    return result


# ---------------------------------------------------------------------------- framing


def encode_frame(message: bytes) -> bytes:
    if len(message) > MAX_FRAME:
        raise ProtoError("message is too large to send")
    return encode_varint(len(message)) + message


class FrameDecoder:
    """Turns a byte stream into messages.

    Feed it whatever arrived; it returns the messages completed so far and keeps the rest.
    A length above ``max_size`` is refused as soon as the length itself is known, before
    any of the body is buffered.
    """

    def __init__(self, max_size: int = MAX_FRAME):
        self._max_size = max_size
        self._buffer = bytearray()

    @property
    def buffered(self) -> int:
        """Bytes of an incomplete frame held so far."""
        return len(self._buffer)

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer += data
        frames: list[bytes] = []
        pos = 0
        try:
            while True:
                header = self._read_length(pos)
                if header is None:
                    break
                length, start = header
                if len(self._buffer) - start < length:
                    break
                frames.append(bytes(self._buffer[start : start + length]))
                pos = start + length
        finally:
            del self._buffer[:pos]  # once per call: many tiny frames must not cost quadratic time
        return frames

    def _read_length(self, pos: int) -> tuple[int, int] | None:
        """``(frame length, where its body starts)`` for the frame at ``pos``, if its length is complete."""
        length = 0
        for index in range(_MAX_LENGTH_BYTES):
            if pos + index >= len(self._buffer):
                return None
            byte = self._buffer[pos + index]
            length |= (byte & 0x7F) << (7 * index)
            if not byte & 0x80:
                if length > self._max_size:
                    raise ProtoError(f"frame of {length} bytes is larger than the {self._max_size} allowed")
                return length, pos + index + 1
        raise ProtoError("frame length is malformed")
