import random
from typing import Optional

def mask(bits: int) -> int:
    """(1<<bits)-1"""
    return (1 << bits) - 1

def to_ubit(x: int, bits: int) -> int:
    """按无符号 two's complement 截断到 bits 位"""
    return x & mask(bits)

def to_sbits(x: int, bits: int) -> int:
    """按有符号 two's complement 截断到 bits 位（返回 Python 有符号整数）"""
    m = mask(bits)
    x &= m
    sign_bit = 1 << (bits - 1)
    return x - (1 << bits) if x & sign_bit else x

def rsg(nbits: int, rng: Optional[random.Random] = None) -> int:
    """随机有符号整数（nbits 位）"""
    r = rng if rng is not None else random
    lo, hi = -(1 << (nbits - 1)), (1 << (nbits - 1)) - 1
    return r.randint(lo, hi)