"""IEEE-754 binary16 codec helpers used by the FP16 SFU generator/tests."""

from __future__ import annotations

import math
import struct


CANONICAL_NAN = 0x7E00


def decode(raw: int) -> float:
    return struct.unpack("<e", int(raw & 0xFFFF).to_bytes(2, "little"))[0]


def encode(value: float) -> int:
    value = float(value)
    if math.isnan(value):
        return CANONICAL_NAN
    try:
        packed = struct.pack("<e", value)
    except OverflowError:
        packed = struct.pack("<H", 0xFC00 if value < 0 else 0x7C00)
    return int.from_bytes(packed, "little")


def ordered_key(raw: int) -> int:
    raw &= 0xFFFF
    if raw & 0x7FFF == 0:
        raw = 0
    return (~raw & 0xFFFF) if raw & 0x8000 else (raw ^ 0x8000)
