import json
import math
import os
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

from utils.config_naming import resolve_config_path
from utils.engine import TRFEngine


ROOT = Path(__file__).resolve().parents[1]
CFG_DIR = Path(os.environ.get("SFU_CFG_DIR", ROOT / "cfg"))
LSB = 1.0 / 32.0
FUNCTIONS = (
    (0x2, "exp", "exp_q3_5_highacc.json", -4.0, 0.0),
    (0x3, "reciprocal", "reciprocal_dynamick_q3_5.json", 0.5, 4.0),
    (0x4, "rsqrt", "rsqrt_q3_5_seg1.json", 0.25, 4.0),
    (0x5, "sigmoid", "sigmoid_q3_5_highacc.json", -4.0, 4.0),
    (0x6, "silu", "silu_q3_5_highacc.json", -4.0, 4.0),
    (0x7, "gelu", "gelu_q3_5_highacc.json", -4.0, 4.0),
    (0x8, "tanh", "tanh_q3_5_highacc.json", -4.0, 4.0),
    (0x9, "mish", "mish_q3_5_highacc.json", -4.0, 4.0),
)


def to_raw(value: float) -> int:
    scaled = round(value * 32.0)
    return min(max(scaled, -128), 127) & 0xFF


def from_raw(raw: int) -> float:
    return (raw - 256 if raw & 0x80 else raw) / 32.0


def points(lo: float, hi: float, count: int = 41) -> list[float]:
    return [lo + (hi - lo) * i / (count - 1) for i in range(count)]


async def reset(dut):
    dut.rst.value = 1
    dut.in_valid.value = 0
    dut.sfu_op.value = 0
    dut.a_in.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)
    await Timer(2, unit="ns")


@cocotb.test()
async def test_sfu_q3_5_all_functions_ii1(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)

    for opcode, name, cfg_name, lo, hi in FUNCTIONS:
        path = resolve_config_path(CFG_DIR, name, "q3_5", cfg_name)
        config = json.loads(path.read_text())
        paper_domain = config.get("fit_metadata", {}).get("paper_domain")
        if isinstance(paper_domain, list) and len(paper_domain) == 2:
            lo, hi = map(float, paper_domain)
        model = TRFEngine.from_config(config)
        xs = points(lo, hi)
        expected = [model.run(model.qfmt.quantize(x)) for x in xs]
        received = []
        output_cycles = []
        cycle = 0
        first_accept_cycle = None

        async def tick():
            nonlocal cycle
            await RisingEdge(dut.clk)
            await Timer(2, unit="ns")
            cycle += 1
            if int(dut.c_valid.value):
                received.append(from_raw(int(dut.c_out.value) & 0xFF))
                output_cycles.append(cycle)

        for index, x in enumerate(xs):
            dut.sfu_op.value = opcode
            dut.a_in.value = to_raw(x)
            dut.in_valid.value = 1
            await Timer(1, unit="ns")
            if index == 0:
                while int(dut.in_ready.value) == 0:
                    await tick()
                first_accept_cycle = cycle + 1
            assert int(dut.in_ready.value) == 1
            await tick()

        dut.in_valid.value = 0
        watchdog = 80
        while len(received) < len(expected) and watchdog:
            await tick()
            watchdog -= 1

        assert len(received) == len(expected), (
            f"{name}: expected {len(expected)} outputs, got {len(received)}"
        )
        assert all(
            output_cycles[i] == output_cycles[i - 1] + 1
            for i in range(1, len(output_cycles))
        ), f"{name}: output II is not 1"

        errors = [abs(got - ref) for got, ref in zip(received, expected)]
        max_error = max(errors)
        dut._log.info(
            "%-10s samples=%d latency=%d max_hw_model_error=%.5f (%.1f LSB)",
            name,
            len(xs),
            output_cycles[0] - first_accept_cycle + 1,
            max_error,
            max_error / LSB,
        )
        assert max_error <= 3 * LSB, (
            f"{name}: hardware/model error exceeds 3 LSB"
        )
        await tick()


@cocotb.test()
async def test_sfu_q3_5_opcode_switch_drains(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await reset(dut)

    dut.sfu_op.value = 0x2
    dut.in_valid.value = 1
    dut.a_in.value = to_raw(0.0)
    await Timer(1, unit="ns")
    while int(dut.in_ready.value) == 0:
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
    for i in range(6):
        dut.a_in.value = to_raw(-0.25 * i)
        await Timer(1, unit="ns")
        assert int(dut.in_ready.value) == 1
        await RisingEdge(dut.clk)

    dut.sfu_op.value = 0x5
    dut.a_in.value = to_raw(0.0)
    stalled = 0
    for _ in range(40):
        await Timer(1, unit="ns")
        if int(dut.in_ready.value):
            break
        stalled += 1
        await RisingEdge(dut.clk)
    else:
        assert False, "opcode switch never became ready"
    assert stalled > 0

    await RisingEdge(dut.clk)
    dut.in_valid.value = 0
    for _ in range(40):
        await RisingEdge(dut.clk)
        await Timer(2, unit="ns")
        if int(dut.c_valid.value):
            if math.isclose(from_raw(int(dut.c_out.value) & 0xFF), 0.5, abs_tol=LSB):
                return
    assert False, "missing sigmoid result after opcode switch"
