"""Bit-exact codec for the project's finite-saturating FP8 E4M3 format."""

from __future__ import annotations

import math


EXP_BITS = 4
MAN_BITS = 3
BIAS = 7
MAX_FINITE = 240.0
MIN_NORMAL = 2.0**-6
SUBNORMAL_STEP = 2.0**-9
CANONICAL_NAN = 0x7F


def decode(raw: int) -> float:
    """Decode an E4M3 byte.

    Exponent field 0xf follows IEEE-like Inf/NaN decoding. Arithmetic in this
    project saturates finite overflow to 0x77/0xf7 instead of producing Inf.
    """

    raw &= 0xFF
    sign = -1.0 if raw & 0x80 else 1.0
    exponent = (raw >> MAN_BITS) & 0xF
    fraction = raw & 0x7
    if exponent == 0:
        value = fraction * SUBNORMAL_STEP
    elif exponent == 0xF:
        return math.copysign(math.inf, sign) if fraction == 0 else math.nan
    else:
        value = (1.0 + fraction / 8.0) * 2.0 ** (exponent - BIAS)
    return math.copysign(value, sign)


def encode(value: float) -> int:
    """Quantize to E4M3 using RNE and finite saturation."""

    value = float(value)
    if math.isnan(value):
        return CANONICAL_NAN
    sign_bit = 0x80 if math.copysign(1.0, value) < 0 else 0
    magnitude = abs(value)
    if magnitude == 0.0:
        return sign_bit
    if math.isinf(magnitude) or magnitude >= MAX_FINITE:
        return sign_bit | 0x77

    if magnitude < MIN_NORMAL:
        fraction = round(magnitude / SUBNORMAL_STEP)
        if fraction <= 0:
            return sign_bit
        if fraction >= 8:
            # Match utils/engine.py: values entering the subnormal branch are
            # clamped to the largest subnormal instead of carrying into the
            # minimum normal value.
            return sign_bit | 0x07
        return sign_bit | int(fraction)

    exponent = math.floor(math.log2(magnitude))
    significand = magnitude / (2.0**exponent)
    fraction = round((significand - 1.0) * 8.0)
    if fraction == 8:
        exponent += 1
        fraction = 0
    exponent_field = exponent + BIAS
    if exponent_field >= 0xF:
        return sign_bit | 0x77
    return sign_bit | (int(exponent_field) << MAN_BITS) | int(fraction)


def sanitize(raw: int) -> int:
    """Apply the hardware input policy to an arbitrary byte."""

    raw &= 0xFF
    if (raw & 0x78) != 0x78:
        return raw
    if raw & 0x7:
        return CANONICAL_NAN
    return (raw & 0x80) | 0x77


def ordered_key(raw: int) -> int:
    """Return an unsigned key whose order matches finite FP8 numeric order."""

    raw = sanitize(raw)
    return (~raw & 0xFF) if raw & 0x80 else (raw ^ 0x80)
