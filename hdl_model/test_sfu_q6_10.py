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
LSB = 1.0 / 1024.0

FUNCTIONS = (
    (0x2, "exp", "exp_q6_10_highacc.json", -8.0, 0.0),
    (
        0x3,
        "reciprocal",
        "reciprocal_dynamick_wide_q6_10_n6_opt.json",
        0.25,
        16.0,
    ),
    (0x4, "rsqrt", "rsqrt_q6_10_seg1.json", 0.25, 16.0),
    (0x5, "sigmoid", "sigmoid_q6_10_highacc.json", -8.0, 8.0),
    (0x6, "silu", "silu_q6_10_highacc.json", -6.0, 6.0),
    (0x7, "gelu", "gelu_q6_10_highacc.json", -6.0, 6.0),
    (0x8, "tanh", "tanh_q6_10_highacc.json", -6.0, 6.0),
    (0x9, "mish", "mish_q6_10_highacc.json", -6.0, 6.0),
)


def to_raw(value: float) -> int:
    scaled = round(value * 1024.0)
    scaled = min(max(scaled, -32768), 32767)
    return scaled & 0xFFFF


def from_raw(raw: int) -> float:
    if raw & 0x8000:
        raw -= 0x10000
    return raw / 1024.0


def sample_points(lo: float, hi: float, count: int = 49) -> list[float]:
    # Include endpoints and enough interior values to cross every fitted
    # segment while retaining a sustained II=1 burst.
    return [lo + (hi - lo) * i / (count - 1) for i in range(count)]


@cocotb.test()
async def test_sfu_q6_10_all_functions_ii1(dut):
    """Check cfg-loaded arithmetic and continuous-output II=1 bursts."""

    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    dut.in_valid.value = 0
    dut.sfu_op.value = 0
    dut.a_in.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)
    await Timer(2, unit="ns")

    global_max_error = 0.0

    for opcode, name, cfg_name, lo, hi in FUNCTIONS:
        path = resolve_config_path(CFG_DIR, name, "q6_10", cfg_name)
        config = json.loads(path.read_text())
        paper_domain = config.get("fit_metadata", {}).get("paper_domain")
        if isinstance(paper_domain, list) and len(paper_domain) == 2:
            lo, hi = map(float, paper_domain)
        model = TRFEngine.from_config(config)
        xs = sample_points(lo, hi)
        expected = [model.run(model.qfmt.quantize(x)) for x in xs]
        received: list[float] = []
        output_cycles: list[int] = []
        cycle = 0
        first_accept_cycle = None

        async def tick_and_sample():
            nonlocal cycle
            await RisingEdge(dut.clk)
            await Timer(2, unit="ns")
            cycle += 1
            if int(dut.c_valid.value):
                received.append(from_raw(int(dut.c_out.value) & 0xFFFF))
                output_cycles.append(cycle)

        # A fixed-opcode burst must be accepted on every cycle.
        for index, x in enumerate(xs):
            dut.sfu_op.value = opcode
            dut.a_in.value = to_raw(x)
            dut.in_valid.value = 1
            await Timer(1, unit="ns")
            if index == 0:
                while int(dut.in_ready.value) == 0:
                    await tick_and_sample()
                first_accept_cycle = cycle + 1
            assert int(dut.in_ready.value) == 1, (
                f"{name}: in_ready dropped inside a fixed-opcode burst"
            )
            await tick_and_sample()

        dut.in_valid.value = 0
        watchdog = 80
        while len(received) < len(expected) and watchdog:
            await tick_and_sample()
            watchdog -= 1

        assert len(received) == len(expected), (
            f"{name}: expected {len(expected)} results, got {len(received)}"
        )
        assert all(
            output_cycles[i] == output_cycles[i - 1] + 1
            for i in range(1, len(output_cycles))
        ), f"{name}: output valid is not continuous; II is not 1"

        errors = [abs(got - ref) for got, ref in zip(received, expected)]
        max_error = max(errors)
        global_max_error = max(global_max_error, max_error)
        dut._log.info(
            "%-10s samples=%d latency=%d cycles max_hw_model_error=%.7f (%.2f LSB)",
            name,
            len(xs),
            output_cycles[0] - first_accept_cycle + 1,
            max_error,
            max_error / LSB,
        )

        # Differences of a few LSB can arise from hardware rounding after
        # each DSP stage versus Python's ties-to-even conversion.
        assert max_error <= 8 * LSB, (
            f"{name}: hardware/model error {max_error} exceeds 8 LSB"
        )

        # Leave one idle cycle between opcodes.  The RTL is allowed to drain
        # on opcode switches, but fixed-opcode bursts remain II=1.
        await tick_and_sample()

    assert math.isfinite(global_max_error)


@cocotb.test()
async def test_sfu_q6_10_opcode_switch_drains(dut):
    """An opcode change may stall, but must resume after the old burst drains."""

    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    dut.in_valid.value = 0
    dut.sfu_op.value = 0
    dut.a_in.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)

    # Launch a short EXP burst at II=1.
    dut.sfu_op.value = 0x2
    dut.in_valid.value = 1
    dut.a_in.value = to_raw(0.0)
    await Timer(1, unit="ns")
    while int(dut.in_ready.value) == 0:
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
    for i in range(8):
        dut.a_in.value = to_raw(-0.25 * i)
        await Timer(1, unit="ns")
        assert int(dut.in_ready.value) == 1
        await RisingEdge(dut.clk)

    # Request SIGMOID immediately.  It must be backpressured while EXP data
    # remains in flight, then accepted once the pipeline is empty.
    dut.sfu_op.value = 0x5
    dut.a_in.value = to_raw(0.0)
    stall_cycles = 0
    for _ in range(40):
        await Timer(1, unit="ns")
        if int(dut.in_ready.value):
            break
        stall_cycles += 1
        await RisingEdge(dut.clk)
    else:
        assert False, "opcode switch never became ready"

    assert stall_cycles > 0, "opcode switch was expected to drain the old stream"
    await RisingEdge(dut.clk)  # accept sigmoid(0)
    dut.in_valid.value = 0

    for _ in range(40):
        await RisingEdge(dut.clk)
        await Timer(2, unit="ns")
        if int(dut.c_valid.value):
            # EXP outputs may still be visible around the hand-over.  The
            # final new-operation output is recognized by sigmoid(0)=0.5.
            if abs(from_raw(int(dut.c_out.value) & 0xFFFF) - 0.5) <= LSB:
                return
    assert False, "no valid SIGMOID result after opcode switch"
