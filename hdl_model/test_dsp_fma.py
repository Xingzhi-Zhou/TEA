# hdl_model/test_dsp_fma.py
import os, random, collections, cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles, Timer

from utils import to_sbits, rsg, Q2_14, Q4_28, fma_raw2w, q_to_float

TCQ = int(os.getenv("TCQ", "1"))  # 从环境变量读 LAT
LAT = int(os.getenv("DSP_FMA_LAT", "1"))  # 从环境变量读 LAT

@cocotb.test()
async def test_dsp_fma(dut):
    clk = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clk.start())
    W, WW = len(dut.a), len(dut.p)

    # 复位
    dut.rst.value = 1
    dut.req.value = 0
    dut.a.value = 0
    dut.b.value = 0
    dut.c.value = 0
    await ClockCycles(dut.clk, 2)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)

    p_deque = collections.deque()
    req_deque  = collections.deque()

    for _ in range(200):
        a, b, c = rsg(W), rsg(W), rsg(WW)
        # a = to_sbits(_, W)
        # b = to_sbits(_, W)
        # c = to_sbits(0, W)
        req = random.choice([0, 1])

        await RisingEdge(dut.clk)
        # 先读
        if _ > LAT+1:
            dut._log.debug(f"pop: {len(req_deque)}")
            req_record = req_deque.popleft()
            p_record = p_deque.popleft()
            dut._log.debug(f"pop: {len(req_deque)}, {req_record}, {p_record}")

            if req_record:
                p_got = dut.p.value.to_signed()
                assert p_got == p_record, f"p mismatch: got {p_got}, exp {p_record}"

        # 再写，保证时序
        await Timer(TCQ, unit="ns")
        dut.a.value = a
        dut.b.value = b
        dut.c.value = c
        dut.req.value = req

        req_deque.append(req)
        p_deque.append(to_sbits(a*b + c, WW))
        dut._log.debug(f"{a}, {b}, {c}, {req}, {to_sbits(a*b + c, WW)}")

    dut._log.debug(f"******")

    for _ in range(LAT+2):
        await RisingEdge(dut.clk)
        # 先读
        dut._log.debug(f"pop: {len(req_deque)}")
        req_record = req_deque.popleft()
        p_record = p_deque.popleft()
        dut._log.debug(f"pop: {len(req_deque)}, {req_record}, {p_record}")

        if req_record:
            p_got = dut.p.value.to_signed()
            assert p_got == p_record, f"p mismatch: got {p_got}, exp {p_record}"

        # 再写，保证时序
        await Timer(TCQ, unit="ns")
        dut.req.value = 0

    assert not p_deque, "pipeline not drained"

@cocotb.test()
async def test_dsp_fma_q2_14(dut):
    """Q2_14 × Q2_14 + Q4_28 -> Q4_28：在 clk 上升沿读、沿后 TCQ 再驱动"""
    clk = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clk.start())
    W, WW = len(dut.a), len(dut.p)

    assert W == 16 and WW == 32, "此用例按 Q2_14/Q4_28 设计（W=16, 2W=32）"

    # 复位
    dut.rst.value = 1
    dut.req.value = 0
    dut.a.value = 0
    dut.b.value = 0
    dut.c.value = 0
    await ClockCycles(dut.clk, 2)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)

    # 期望与 req 队列（每拍都 push，一一对应；只在 req=1 的拍才比较）
    p_deque   = collections.deque()
    req_deque = collections.deque()

    for i in range(200):
        # 生成浮点，再量化到 Q 格式 -> 原始位模式（int()）
        a_q = Q2_14(random.uniform(-1.9, 1.9))  # 16b Q2.14
        b_q = Q2_14(random.uniform(-1.9, 1.9))
        c_q = Q4_28(random.uniform(-1.0, 1.0))  # 32b Q4.28
        req = random.getrandbits(1)

        # 先在“本拍上升沿”读取、出队并对比上一事务
        await RisingEdge(dut.clk)
        if i > LAT + 1:
            req_record = req_deque.popleft()
            p_record   = p_deque.popleft()
            if req_record:
                p_got = dut.p.value.to_signed()
                assert p_got == p_record, f"p mismatch: got {p_got}, exp {p_record}"

        # 再在“沿后 TCQ”驱动下一拍要被采样的输入（只在上升沿改变输入）
        await Timer(TCQ, unit="ns")
        dut.a.value  = int(a_q)   # 16 位位模式
        dut.b.value  = int(b_q)
        dut.c.value  = int(c_q)   # 32 位位模式
        dut.req.value = req

        exp_raw = fma_raw2w(a_q, b_q, c_q, out_type=Q4_28)
        exp_sgn = to_sbits(int(exp_raw), WW)

        req_deque.append(req)
        p_deque.append(exp_sgn)


        dut._log.debug(
            f"[i={i}] a={a_q:+.5f} b={b_q:+.5f} c={c_q:+.5f} req={req} exp_raw=0x{exp_raw:08X} exp={exp_sgn:+d} *={q_to_float(exp_sgn, 16, 14):+.5f}"
        )


    for _ in range(LAT + 2):
        await RisingEdge(dut.clk)
        req_record = req_deque.popleft()
        p_record   = p_deque.popleft()
        if req_record:
            p_got = dut.p.value.to_signed()
            assert p_got == p_record, f"p mismatch: got {p_got}, exp {p_record}"
        await Timer(TCQ, unit="ns")
        dut.req.value = 0

    assert not p_deque, "pipeline not drained"

# @cocotb.test()
# async def test_dsp_fma_a(dut):
#     clk = Clock(dut.clk, 10, unit="ns")
#     cocotb.start_soon(clk.start())
#     W, WW = len(dut.a), len(dut.p)

#     # 复位
#     dut.rst.value = 1
#     dut.req.value = 0
#     dut.a.value = 0
#     dut.b.value = 0
#     dut.c.value = 0
#     await ClockCycles(dut.clk, 2)
#     dut.rst.value = 0
#     await ClockCycles(dut.clk, 2)

#     p_deque = collections.deque()

#     for _ in range(1, 200):
#         a = to_sbits(_, W)
#         b = to_sbits(_, W)
#         c = to_sbits(0, W)
#         req = random.choice([0, 1])

#         await RisingEdge(dut.clk)

#         # 先读
#         if dut.out_valid.value == 1:
#             p_record = p_deque.popleft()
#             p_got = dut.p.value.to_signed()
#             assert p_got == p_record, f"{_} mismatch: got {p_got}, exp {p_record}"


#         # 再写，保证时序
#         await Timer(TCQ, unit="ns")

#         dut.a.value = a
#         dut.b.value = b
#         dut.c.value = c

#         dut.req.value = req

#         if req:
#             p_deque.append(to_sbits(a*b + c, WW))

#     await RisingEdge(dut.clk)
#     # 先读
#     if dut.out_valid.value == 1:
#         p_record = p_deque.popleft()
#         p_got = dut.p.value.to_signed()
#         assert p_got == p_record, f"{_} mismatch: got {p_got}, exp {p_record}"
#     # 再写，保证时序
#     await Timer(TCQ, unit="ns")
#     dut.req.value = 0

#     for _ in range(LAT+1):
#         await RisingEdge(dut.clk)

#         if dut.out_valid.value == 1:
#             p_record = p_deque.popleft()
#             p_got = dut.p.value.to_signed()
#             assert p_got == p_record, f"{_} mismatch: got {p_got}, exp {p_record}"


#     assert not p_deque, "pipeline not drained"