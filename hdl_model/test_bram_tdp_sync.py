# -*- coding: utf-8 -*-
# test_bram_tdp_sync.py  —— “早采样”风格：↑clk后先读上一拍，再等TCQ后写下一拍
import os
import random
from collections import deque

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer, ReadOnly, ReadWrite

# ========= 环境参数 =========
def _get_int(name, default):
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default

TCQ_NS   = _get_int("TCQ", 1)
LAT_ENV  = _get_int("BRAM_LAT", 1)
WR_MODE  = _get_int("BRAM_WR_MODE", 1)  # 0:READ_FIRST,1:WRITE_FIRST,2:NO_CHANGE
WPRI     = _get_int("BRAM_WPRI", 1)     # 0:A优先,1:B优先,2:X
SEED     = _get_int("BRAM_SEED", 12345)
N_CYCLES = _get_int("BRAM_N_CYCLES", 200)

# ========= 工具 =========
def bits(sig) -> int:
    return len(sig)

def mask_n(nbits: int) -> int:
    return (1 << nbits) - 1 if nbits > 0 else 0

def is_xz(sig) -> bool:
    v = sig.value
    if hasattr(v, "is_resolvable"):
        return not v.is_resolvable
    s = v.binstr
    return any(c in "xXzZ" for c in s)

def to_int(sig):
    v = sig.value
    if hasattr(v, "is_resolvable") and not v.is_resolvable:
        return None
    if hasattr(v, "to_unsigned"):
        try:
            return int(v.to_unsigned())
        except Exception:
            pass
    if hasattr(v, "integer"):
        return v.integer
    return int(v)

def apply_wstrb(oldv: int, newv: int, wstrb: int, bytes_n: int) -> int:
    m = 0
    for b in range(bytes_n):
        if (wstrb >> b) & 1:
            m |= (0xFF << (8*b))
    return (oldv & ~m) | (newv & m)

# ========= 参考模型：含跨端口 WRITE_FIRST 语义 =========
class BramRefModel:
    def __init__(self, aw: int, dw: int, lat: int, wr_mode: int, wpri: int):
        self.AW = aw
        self.DW = dw
        self.BYTES = (dw + 7) // 8
        self.DEPTH = 1 << aw
        self.LAT = max(0, lat)
        self.WR_MODE = wr_mode
        self.WPRI = wpri
        self.mem = [0] * self.DEPTH
        self.last_dout_a = 0
        self.last_dout_b = 0
        self.pipe_a = deque([None] * self.LAT)
        self.pipe_b = deque([None] * self.LAT)

    def _next_read_value(self, oldv, newv, any_w, prev_dout):
        if self.WR_MODE == 0:   # READ_FIRST
            return oldv
        elif self.WR_MODE == 1: # WRITE_FIRST
            return newv if any_w else oldv
        else:                   # NO_CHANGE
            return prev_dout if any_w else oldv

    def step(self, a_req, a_addr, a_din, a_wstrb,
                   b_req, b_addr, b_din, b_wstrb):
        # 弹出“将被当前拍读取”的值（早采样：下一拍 RisingEdge 时读到的是这里返回的值）
        a_out = self.pipe_a.popleft() if self.LAT > 0 else None
        b_out = self.pipe_b.popleft() if self.LAT > 0 else None
        a_valid = a_out is not None if self.LAT > 0 else False
        b_valid = b_out is not None if self.LAT > 0 else False
        if a_valid: self.last_dout_a = a_out
        if b_valid: self.last_dout_b = b_out

        # 本拍输入作用
        a_old = self.mem[a_addr] if a_req else 0
        b_old = self.mem[b_addr] if b_req else 0
        a_any_w = (a_req and (a_wstrb != 0))
        b_any_w = (b_req and (b_wstrb != 0))
        a_new = apply_wstrb(a_old, a_din, a_wstrb, self.BYTES) if a_any_w else a_old
        b_new = apply_wstrb(b_old, b_din, b_wstrb, self.BYTES) if b_any_w else b_old
        a_new &= mask_n(self.DW); b_new &= mask_n(self.DW)

        a_rdv = self._next_read_value(a_old, a_new, a_any_w, self.last_dout_a) if a_req else None
        b_rdv = self._next_read_value(b_old, b_new, b_any_w, self.last_dout_b) if b_req else None

        # 跨端口同址：读/写同拍遵循 WR_MODE
        same_addr = (a_addr == b_addr)
        a_reads = (a_req and a_wstrb == 0)
        b_reads = (b_req and b_wstrb == 0)
        if same_addr:
            if a_reads and b_any_w:
                if   self.WR_MODE == 0: a_rdv = a_old
                elif self.WR_MODE == 1: a_rdv = b_new
                else:                   a_rdv = self.last_dout_a
            if b_reads and a_any_w:
                if   self.WR_MODE == 0: b_rdv = b_old
                elif self.WR_MODE == 1: b_rdv = a_new
                else:                   b_rdv = self.last_dout_b

        # 推入管线：将成为“下一次 RisingEdge 早采样”看到的值
        if self.LAT == 0:
            nxt_a_valid, nxt_b_valid = bool(a_req), bool(b_req)
            nxt_a_out,   nxt_b_out   = a_rdv, b_rdv
        else:
            self.pipe_a.append(a_rdv if a_req else None)
            self.pipe_b.append(b_rdv if b_req else None)
            nxt_a_valid = (self.pipe_a[0] is not None)
            nxt_b_valid = (self.pipe_b[0] is not None)
            nxt_a_out   = self.pipe_a[0]
            nxt_b_out   = self.pipe_b[0]

        # 最终写入（同址双写用 WPRI 裁决或写 X）
        if a_any_w and b_any_w and same_addr:
            if self.WPRI == 0:
                self.mem[a_addr] = a_new
            elif self.WPRI == 1:
                self.mem[b_addr] = b_new
            else:
                pass  # X-on-collision：不更新，留给 DUT 在 RTL 中写 X
        else:
            if a_any_w: self.mem[a_addr] = a_new
            if b_any_w: self.mem[b_addr] = b_new

        # 返回“下一次 RisingEdge 早采样要看到的期望”
        return (nxt_a_valid, nxt_a_out, nxt_b_valid, nxt_b_out)

# ========= DUT 端口 =========
async def reset_dut(dut, cycles=3):
    dut.rst.value = 1
    dut.a_req.value = 0; dut.b_req.value = 0
    dut.a_addr.value = 0; dut.b_addr.value = 0
    dut.a_din.value  = 0; dut.b_din.value  = 0
    dut.a_wstrb.value= 0; dut.b_wstrb.value= 0
    for _ in range(cycles):
        await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)  # 第一拍释放
    await ReadOnly()           # 早采样：此处读到的是“复位期的上一拍”
    # 不比对，留给主循环的 prev_exp 机制处理

def drive_one_cycle(dut, a_req, a_addr, a_din, a_wstrb,
                          b_req, b_addr, b_din, b_wstrb):
    dut.a_req.value   = int(a_req)
    dut.a_addr.value  = int(a_addr)
    dut.a_din.value   = int(a_din)
    dut.a_wstrb.value = int(a_wstrb)
    dut.b_req.value   = int(b_req)
    dut.b_addr.value  = int(b_addr)
    dut.b_din.value   = int(b_din)
    dut.b_wstrb.value = int(b_wstrb)

# ========= 一个“早采样周期”骨架：读上一拍 → 等TCQ → 写下一拍 =========
async def cycle_early(dut, prev_exp, next_inputs_fn, model):
    """
    prev_exp: 上一次驱动后由模型返回的期望 (a_v_exp, a_d_exp, b_v_exp, b_d_exp) 或 None
    next_inputs_fn(): 返回 (a_req,a_addr,a_din,a_wstrb,b_req,b_addr,b_din,b_wstrb)
    返回：新的 prev_exp（供下一拍使用）
    """
    # 1) 先读（上一拍）
    await RisingEdge(dut.clk)

    if prev_exp is not None:
        a_v_exp, a_d_exp, b_v_exp, b_d_exp = prev_exp

        a_v_got = int(dut.a_valid.value)
        b_v_got = int(dut.b_valid.value)
        assert a_v_got == int(bool(a_v_exp))
        assert b_v_got == int(bool(b_v_exp))

        if a_v_exp and not is_xz(dut.a_dout):
            assert (to_int(dut.a_dout) & mask_n(bits(dut.a_dout))) == (a_d_exp & mask_n(bits(dut.a_dout)))
        if b_v_exp and not is_xz(dut.b_dout):
            assert (to_int(dut.b_dout) & mask_n(bits(dut.b_dout))) == (b_d_exp & mask_n(bits(dut.b_dout)))

    # 2) 再写（下一拍）
    await Timer(TCQ_NS, unit="ns")

    (a_req,a_addr,a_din,a_wstrb,b_req,b_addr,b_din,b_wstrb) = next_inputs_fn()
    drive_one_cycle(dut, a_req,a_addr,a_din,a_wstrb,b_req,b_addr,b_din,b_wstrb)
    # 模型生成“下一次 RisingEdge 读到的期望”
    return model.step(a_req,a_addr,a_din,a_wstrb,b_req,b_addr,b_din,b_wstrb)

# ========= 用例 1：基础读写 + 字节写 =========
@cocotb.test()
async def test_basic_read_write_and_byte_enable(dut):
    clk = Clock(dut.clk, 5, unit="ns")
    cocotb.start_soon(clk.start())

    DW = bits(dut.a_din); AW = bits(dut.a_addr); BYTES = DW // 8
    model = BramRefModel(AW, DW, LAT_ENV, WR_MODE, WPRI)
    await reset_dut(dut)

    seq = deque()
    # A 写 3/7/12
    data_vec = [0xDEADBEEF & mask_n(DW), 0x12345678 & mask_n(DW), 0xCAFEBABE & mask_n(DW)]
    addrs = [3,7,12]
    for a, d in zip(addrs, data_vec):
        seq.append((1,a,d,(1<<BYTES)-1, 0,0,0,0))  # A写
        seq.append((0,0,0,0, 0,0,0,0))             # idle

    # B 读回
    for a, _ in zip(addrs, data_vec):
        seq.append((0,0,0,0, 1,a,0,0))             # B读

    # 字节写低两字节，然后 A 读回
    seq.append((1,20,0xBEEF,0b0011, 0,0,0,0))      # A按字节写
    seq.append((1,20,0,0, 0,0,0,0))                # A读

    def gen():
        return seq.popleft() if seq else (0,0,0,0, 0,0,0,0)

    prev_exp = None
    # 跑到序列耗尽，再多冲 3 拍把管线读干净
    for _ in range(len(seq)+8):
        prev_exp = await cycle_early(dut, prev_exp, gen, model)

# ========= 用例 2：同拍 A写/B读 同址，校验 WR_MODE =========
@cocotb.test()
async def test_same_cycle_read_write_semantics(dut):
    clk = Clock(dut.clk, 5, unit="ns")
    cocotb.start_soon(clk.start())

    DW = bits(dut.a_din); AW = bits(dut.a_addr); BYTES = DW // 8
    model = BramRefModel(AW, DW, LAT_ENV, WR_MODE, WPRI)
    await reset_dut(dut)

    addr = 5
    oldv = 0xA5A55A5A & mask_n(DW)
    newv = 0xDEADBEEF & mask_n(DW)

    seq = deque()
    # 先写旧值
    seq.append((1,addr,oldv,(1<<BYTES)-1, 0,0,0,0))
    # idle
    seq.append((0,0,0,0, 0,0,0,0))
    # 同拍：A写、B读同址
    seq.append((1,addr,newv,(1<<BYTES)-1, 1,addr,0,0))

    def gen():
        return seq.popleft() if seq else (0,0,0,0, 0,0,0,0)

    prev_exp = None
    for _ in range(len(seq)+6):
        prev_exp = await cycle_early(dut, prev_exp, gen, model)

# ========= 用例 3：同拍同址双写，核对 WPRI 最终值 =========
@cocotb.test()
async def test_dual_write_collision_wpri(dut):
    clk = Clock(dut.clk, 5, unit="ns")
    cocotb.start_soon(clk.start())

    DW = bits(dut.a_din); AW = bits(dut.a_addr); BYTES = DW // 8
    model = BramRefModel(AW, DW, LAT_ENV, WR_MODE, WPRI)
    await reset_dut(dut)

    addr = 9
    a_data = 0x11112222 & mask_n(DW)
    b_data = 0x33334444 & mask_n(DW)

    seq = deque()
    # 冲突写
    seq.append((1,addr,a_data,(1<<BYTES)-1, 1,addr,b_data,(1<<BYTES)-1))
    # 之后 A 读回
    seq.append((1,addr,0,0, 0,0,0,0))

    def gen():
        return seq.popleft() if seq else (0,0,0,0, 0,0,0,0)

    prev_exp = None
    for _ in range(len(seq)+6):
        prev_exp = await cycle_early(dut, prev_exp, gen, model)

# ========= 用例 4：随机压力（早采样时序） =========
@cocotb.test()
async def test_randomized_stress(dut):
    clk = Clock(dut.clk, 5, unit="ns")
    cocotb.start_soon(clk.start())

    DW = bits(dut.a_din); AW = bits(dut.a_addr); BYTES = DW // 8
    DEPTH = 1 << AW
    random.seed(SEED)
    model = BramRefModel(AW, DW, LAT_ENV, WR_MODE, WPRI)
    await reset_dut(dut)

    def rand_inputs():
        a_req = (random.random() < 0.5)
        b_req = (random.random() < 0.5)
        a_addr = random.randrange(DEPTH) if a_req else 0
        b_addr = random.randrange(DEPTH) if b_req else 0

        def rand_wstrb():
            if random.random() < 0.5:
                return 0
            m = 0
            for b in range(BYTES):
                if random.random() < 0.5:
                    m |= (1 << b)
            return m or 1

        a_wstrb = rand_wstrb() if a_req else 0
        b_wstrb = rand_wstrb() if b_req else 0
        a_din = random.getrandbits(DW) if (a_req and a_wstrb) else 0
        b_din = random.getrandbits(DW) if (b_req and b_wstrb) else 0
        return (a_req,a_addr,a_din,a_wstrb,b_req,b_addr,b_din,b_wstrb)

    prev_exp = None
    for _ in range(N_CYCLES):
        prev_exp = await cycle_early(dut, prev_exp, rand_inputs, model)

    # 冲掉管线（读出最后一次 prev_exp）
    for _ in range(LAT_ENV + 2):
        prev_exp = await cycle_early(dut, prev_exp, lambda: (0,0,0,0, 0,0,0,0), model)
