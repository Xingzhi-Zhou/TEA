# tb_utils/fxp.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

from bit_num import to_sbits, to_ubit, rsg, mask

# ---------- 基础工具 ----------
Rounding = Literal["trunc", "floor", "ceil", "nearest", "nearest_even"]

def _mask(n: int) -> int:
    return (1 << n) - 1

def _wrap(x: int, n: int) -> int:
    return x & _mask(n)

def _sat_signed(x: int, n: int) -> int:
    lo, hi = -(1 << (n - 1)), (1 << (n - 1)) - 1
    return min(max(x, lo), hi)

def _sat_unsigned(x: int, n: int) -> int:
    lo, hi = 0, (1 << n) - 1
    return min(max(x, lo), hi)

def _to_signed(x: int, n: int) -> int:
    x &= _mask(n)
    sign = 1 << (n - 1)
    return x - (1 << n) if (x & sign) else x

def _round_float_to_int(scaled: float, mode: Rounding) -> int:
    if mode == "trunc":
        return int(scaled)
    if mode == "floor":
        import math; return math.floor(scaled)
    if mode == "ceil":
        import math; return math.ceil(scaled)
    if mode == "nearest":
        return int(scaled + 0.5) if scaled >= 0 else int(scaled - 0.5)
    if mode == "nearest_even":
        import decimal
        return int(decimal.Decimal(scaled).to_integral_value(rounding=decimal.ROUND_HALF_EVEN))
    raise ValueError(f"bad rounding mode: {mode}")

def _round_shift_right(v: int, k: int, mode: Rounding) -> int:
    """
    把整数 v 右移 k 位并按 mode 取整（对称且含 tie 处理）。
    使用算术右移基数 base = floor_div(v, 2^k)，余数 rem ∈ [0, 2^k)。
    """
    if k <= 0:
        return v << (-k)
    base = v >> k  # 算术移位 == floor_div
    rem  = v - (base << k)  # 0..(2^k-1)
    half = 1 << (k - 1)

    if mode == "trunc":
        # 朝 0：正数取 base+ (rem>0?1:0)？——不，trunc 是朝 0 对“除后的小数”截断。
        # 这里 base 已经是 floor，因此：
        return base if v >= 0 else (base + (1 if rem != 0 else 0))
    if mode == "floor":
        return base
    if mode == "ceil":
        return base if rem == 0 else (base + 1)
    if mode == "nearest":
        if rem > half:
            return base + 1
        elif rem < half:
            return base
        else:  # tie: 远离 0
            return base + (1 if v >= 0 else 0)
    if mode == "nearest_even":
        if rem > half:
            return base + 1
        elif rem < half:
            return base
        else:  # tie: 选偶数
            return base if (base & 1) == 0 else (base + 1)
    raise ValueError(f"bad rounding mode: {mode}")

# ---------- Q“类型” ----------
@dataclass(frozen=True)
class QType:
    """
    定点“类型签名”：
      total_bits: 总位宽
      frac_bits : 小数位宽
      signed    : 是否有符号（两补码）
    QType(...) 可调用：QType(0.5) -> Fxp 值对象（像 float(...) 一样）
    """
    total_bits: int
    frac_bits: int
    signed: bool = True

    def __call__(self, x: int | float, rounding: Rounding = "nearest",
                 saturate: bool = True) -> "Fxp":
        """
        - x 是 float：按 Q 格式量化（四舍五入策略由 rounding 决定）
        - x 是 int  ：视为“已经乘以 2^frac_bits 的整数”，直接截断/饱和
        - x 是 Fxp  ：做 rescale 到当前 QType
        """
        if isinstance(x, Fxp):
            return x.rescale(self, rounding=rounding, saturate=saturate)
        if isinstance(x, float):
            scaled = _round_float_to_int(x * (1 << self.frac_bits), rounding)
        elif isinstance(x, int):
            scaled = x
        else:
            raise TypeError(f"unsupported value type: {type(x)}")

        raw = (_sat_signed(scaled, self.total_bits) if self.signed
               else _sat_unsigned(scaled, self.total_bits)) if saturate else _wrap(scaled, self.total_bits)
        return Fxp(self, raw)

    @staticmethod
    def Q(m: int, n: int, signed: bool = True, includes_sign: bool = True) -> "QType":
        """
        工厂：Q(m, n)
        - includes_sign=True：m 含符号位（你的项目约定：Q2.14 => 16b）
        - includes_sign=False：m 不含符号位（额外 +1 符号位）
        """
        total = (m + n) if includes_sign else ((1 if signed else 0) + m + n)
        return QType(total, n, signed)

    def label(self) -> str:
        pre = "Q" if self.signed else "UQ"
        m = self.total_bits - self.frac_bits
        sgn = "signed" if self.signed else "unsigned"
        return f"{pre}{m}.{self.frac_bits}/{self.total_bits}b {sgn}"

# ---------- Fxp“值” ----------
class Fxp:
    """
    定点值对象：支持 + - * 取负、int()/float()、rescale()。
    所有运算默认“环绕（wrap）”到结果格式的位宽（与硬件两补码一致）。
    """
    __slots__ = ("fmt", "raw")

    def __init__(self, fmt: QType, raw: int):
        self.fmt = fmt
        self.raw = _wrap(raw, fmt.total_bits)

    # 转换/显示
    def __int__(self) -> int:
        """位模式（0..2^N-1），可直接赋值到 cocotb 端口"""
        return _wrap(self.raw, self.fmt.total_bits)

    def __float__(self) -> float:
        """真实数值（按 signed 标志解释为有符号或无符号）"""
        val = _to_signed(self.raw, self.fmt.total_bits) if self.fmt.signed else int(self)
        return val / float(1 << self.fmt.frac_bits)

    def __repr__(self) -> str:
        nhex = (self.fmt.total_bits + 3) // 4
        return f"Fxp({self.fmt.label()}, raw=0x{int(self):0{nhex}X}, val={float(self):.6f})"

    # 内部：把自身数值按目标小数位对齐（返回 Python int，保留符号）
    def _align_to(self, frac_bits: int) -> int:
        v = _to_signed(self.raw, self.fmt.total_bits) if self.fmt.signed else int(self)
        shift = frac_bits - self.fmt.frac_bits
        if shift >= 0:
            return v << shift
        # 右移对齐采用“朝 0”截断，匹配硬件常见实现
        return v >> (-shift)

    # 求和目标格式：小数位对齐到较大值，总位宽按对齐后最大值 +1（预留进位）
    def _add_target_fmt(self, other: "Fxp") -> QType:
        frac = max(self.fmt.frac_bits, other.fmt.frac_bits)
        a_w = self.fmt.total_bits + max(0, frac - self.fmt.frac_bits)
        b_w = other.fmt.total_bits + max(0, frac - other.fmt.frac_bits)
        total = max(a_w, b_w) + 1
        signed = self.fmt.signed or other.fmt.signed
        return QType(total_bits=total, frac_bits=frac, signed=signed)

    # 加法
    def __add__(self, other: "Fxp") -> "Fxp":
        if not isinstance(other, Fxp):
            return NotImplemented
        tgt = self._add_target_fmt(other)
        raw = self._align_to(tgt.frac_bits) + other._align_to(tgt.frac_bits)
        return Fxp(tgt, raw)

    def __radd__(self, other: "Fxp") -> "Fxp":
        return self.__add__(other)

    def __iadd__(self, other: "Fxp") -> "Fxp":
        s = (self + other)
        self.fmt, self.raw = s.fmt, s.raw
        return self

    # 取负（无符号也允许，按模 2^N 产生补数）
    def __neg__(self) -> "Fxp":
        if self.fmt.signed:
            v = -_to_signed(self.raw, self.fmt.total_bits)
        else:
            v = (-int(self))  # 模 2^N 意义
        return Fxp(self.fmt, v)

    # 减法
    def __sub__(self, other: "Fxp") -> "Fxp":
        if not isinstance(other, Fxp):
            return NotImplemented
        return self.__add__(-other)

    def __rsub__(self, other: "Fxp") -> "Fxp":
        if not isinstance(other, Fxp):
            return NotImplemented
        return (-self).__add__(other)

    def __isub__(self, other: "Fxp") -> "Fxp":
        s = (self - other)
        self.fmt, self.raw = s.fmt, s.raw
        return self

    # 乘法（结果小数位相加，总位宽相加；符号为两者 OR）
    def __mul__(self, other: "Fxp") -> "Fxp":
        if not isinstance(other, Fxp):
            return NotImplemented
        a = _to_signed(self.raw, self.fmt.total_bits) if self.fmt.signed else int(self)
        b = _to_signed(other.raw, other.fmt.total_bits) if other.fmt.signed else int(other)
        prod = a * b
        tgt = QType(self.fmt.total_bits + other.fmt.total_bits,
                    self.fmt.frac_bits + other.fmt.frac_bits,
                    signed=(self.fmt.signed or other.fmt.signed))
        return Fxp(tgt, prod)

    def __rmul__(self, other: "Fxp") -> "Fxp":
        return self.__mul__(other)

    # 改变格式（右移时支持各种 rounding；最后 wrap 或 sat）
    def rescale(self, target: QType, rounding: Rounding = "trunc",
                saturate: bool = True) -> "Fxp":
        v = _to_signed(self.raw, self.fmt.total_bits) if self.fmt.signed else int(self)
        shift = target.frac_bits - self.fmt.frac_bits
        if shift >= 0:
            v = v << shift
        else:
            v = _round_shift_right(v, -shift, rounding)
        if target.signed:
            v = _sat_signed(v, target.total_bits) if saturate else _wrap(v, target.total_bits)
        else:
            v = _sat_unsigned(v, target.total_bits) if saturate else _wrap(v, target.total_bits)
        return Fxp(target, v)
    
    def __format__(self, spec: str) -> str:
        # 空 spec 就退回 repr
        if not spec:
            return repr(self)
        # 取类型字符（格式规范的最后一个字母）
        t = spec[-1]
        if t in "eEfFgG%":        # 浮点格式
            return format(float(self), spec)
        if t in "diouxXbB":       # 整数/进制/十六进制等
            return format(int(self), spec)
        # 其它情况一律按浮点处理
        return format(float(self), spec)

# ---------- 预置类型（与你当前约定：m 含符号位） ----------
Q2_14  = QType.Q(2, 14, signed=True,  includes_sign=True)
UQ2_14 = QType.Q(2, 14, signed=False, includes_sign=True)
Q1_15  = QType.Q(1, 15, signed=True,  includes_sign=True)
UQ1_15 = QType.Q(1, 15, signed=False, includes_sign=True)
Q4_28  = QType.Q(4, 28, signed=True,  includes_sign=True)
UQ4_28 = QType.Q(4, 28, signed=False, includes_sign=True)
Q6_10  = QType.Q(6, 10, signed=True,  includes_sign=True)


def qfmt(spec: str, includes_sign: bool = True) -> QType:
    s = spec.strip().lower()
    signed = not s.startswith("uq")
    body = s[2:] if s.startswith("uq") else s[1:]
    m_str, n_str = body.split(".")
    m, n = int(m_str), int(n_str)
    return QType.Q(m, n, signed=signed, includes_sign=includes_sign)

# ---------- 与当前 RTL 对齐的 FMA（p = a*b + c，不右移） ----------
def fma_raw2w(a: Fxp, b: Fxp, c: Fxp, out_type: QType) -> Fxp:
    """
    要求：c 的 frac_bits == a.frac + b.frac（例如 a/b=Q2.14 -> prod/c=Q4.28）
    返回：out_type 位宽（通常 2W）、不右移、不饱和（wrap）
    """
    prod = a * b  # Q: (ta+tb bits, fa+fb)
    if prod.fmt.frac_bits != c.fmt.frac_bits:
        raise ValueError("c.frac_bits must equal (a*b).frac_bits for raw2w")
    s = prod + c  # 自动按更大的 frac 对齐（这里应相同），总位宽按规则扩展
    return s.rescale(out_type, rounding="trunc", saturate=False)

def q_to_float(raw: int, total_bits: int, frac_bits: int) -> float:
    return to_sbits(raw, total_bits) / float(1 << frac_bits)

# ---------- QFormat 兼容接口 ----------
class QFormat:
    """
    兼容旧接口的 QFormat 类，包装 QType 功能
    用法: q = QFormat(int_bits, frac_bits, is_signed=True)
    """
    def __init__(self, int_bits: int, frac_bits: int, is_signed: bool = True):
        # 总位宽 = int_bits + frac_bits（int_bits 包含符号位）
        self.int_bits = int_bits
        self.frac_bits = frac_bits
        self.is_signed = is_signed
        self.total_bits = int_bits + frac_bits
        self.qtype = QType(self.total_bits, frac_bits, is_signed)
    
    def float2fix(self, x: float, rounding: Rounding = "nearest", saturate: bool = True) -> int:
        """将浮点数转换为定点整数（原始位模式）"""
        fxp = self.qtype(x, rounding=rounding, saturate=saturate)
        return int(fxp)
    
    def fix2float(self, raw: int) -> float:
        """将定点整数（原始位模式）转换为浮点数"""
        fxp = Fxp(self.qtype, raw)
        return float(fxp)