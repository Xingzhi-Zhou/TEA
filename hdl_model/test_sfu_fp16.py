import json
import math
import os
from statistics import mean
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

from utils.config_naming import resolve_config_path
from utils.engine import TRFEngine
from fp16_ieee import CANONICAL_NAN, decode, encode


ROOT = Path(__file__).resolve().parents[1]
CFG_DIR = Path(os.environ.get("SFU_CFG_DIR", ROOT / "cfg"))
FUNCTIONS = (
    (0x2, "exp", "exp_fp16_highacc.json", -8.0, 0.0),
    (
        0x3,
        "reciprocal",
        "reciprocal_dynamick_wide_fp16_n6_opt.json",
        0.25,
        16.0,
    ),
    (0x4, "rsqrt", "rsqrt_fp16_seg1.json", 0.25, 16.0),
    (0x5, "sigmoid", "sigmoid_fp16_highacc.json", -8.0, 8.0),
    (0x6, "silu", "silu_fp16_highacc.json", -6.0, 6.0),
    (0x7, "gelu", "gelu_fp16_highacc.json", -6.0, 6.0),
    (0x8, "tanh", "tanh_fp16_highacc.json", -6.0, 6.0),
    (0x9, "mish", "mish_fp16_highacc.json", -6.0, 6.0),
)


def sampled_domain_patterns(config: dict, lo: float, hi: float) -> list[int]:
    all_patterns = sorted(
        (
            raw
            for raw in range(0x10000)
            if ((raw >> 10) & 0x1F) != 0x1F and lo <= decode(raw) <= hi
        ),
        key=decode,
    )
    count = min(321, len(all_patterns))
    selected = {
        all_patterns[round(i * (len(all_patterns) - 1) / (count - 1))]
        for i in range(count)
    }
    segments = config.get("segments")
    if isinstance(segments, list):
        for segment in segments[:-1]:
            boundary = float(segment["domain"]["x_max"])
            insertion = next(
                (
                    index
                    for index, raw in enumerate(all_patterns)
                    if decode(raw) >= boundary
                ),
                len(all_patterns) - 1,
            )
            for index in range(max(0, insertion - 2), min(len(all_patterns), insertion + 3)):
                selected.add(all_patterns[index])
    # Explicitly retain signed zero and a few subnormal values where in-domain.
    for raw in (0x0000, 0x8000, 0x0001, 0x8001, 0x03FF, 0x83FF):
        value = decode(raw)
        if lo <= value <= hi:
            selected.add(raw)
    return sorted(selected, key=lambda raw: (decode(raw), raw))


async def reset_dut(dut):
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


@cocotb.test()
async def test_sfu_fp16_all_functions_ii1(dut):
    await reset_dut(dut)
    cycle = 0

    async def tick(received, output_cycles):
        nonlocal cycle
        await RisingEdge(dut.clk)
        await Timer(2, unit="ns")
        cycle += 1
        if int(dut.c_valid.value):
            received.append(int(dut.c_out.value) & 0xFFFF)
            output_cycles.append(cycle)

    for opcode, name, cfg_name, lo, hi in FUNCTIONS:
        path = resolve_config_path(CFG_DIR, name, "fp16", cfg_name)
        config = json.loads(path.read_text())
        paper_domain = config.get("fit_metadata", {}).get("paper_domain")
        if isinstance(paper_domain, list) and len(paper_domain) == 2:
            lo, hi = map(float, paper_domain)
        model = TRFEngine.from_config(config)
        inputs = sampled_domain_patterns(config, lo, hi)
        reference_function = config["evaluation"]["reference_function"]
        ref_input_mode = config["evaluation"].get(
            "ref_input_mode", "quantized"
        )
        evaluations = [
            model.evaluate(
                decode(raw),
                reference_function,
                ref_input_mode=ref_input_mode,
            )
            for raw in inputs
        ]
        expected = [encode(result["output"]) for result in evaluations]
        received, output_cycles = [], []
        first_accept = None

        for index, raw in enumerate(inputs):
            dut.sfu_op.value = opcode
            dut.a_in.value = raw
            dut.in_valid.value = 1
            await Timer(1, unit="ns")
            if index == 0:
                watchdog = 50
                while not int(dut.in_ready.value) and watchdog:
                    await tick(received, output_cycles)
                    watchdog -= 1
                assert watchdog, f"{name}: configuration timeout"
                first_accept = cycle + 1
            assert int(dut.in_ready.value), f"{name}: II=1 input stall"
            await tick(received, output_cycles)

        dut.in_valid.value = 0
        watchdog = 100
        while len(received) < len(expected) and watchdog:
            await tick(received, output_cycles)
            watchdog -= 1

        assert len(received) == len(expected)
        assert all(
            output_cycles[i] == output_cycles[i - 1] + 1
            for i in range(1, len(output_cycles))
        ), f"{name}: output II != 1"
        mismatches = [
            (inputs[i], expected[i], received[i])
            for i in range(len(expected))
            if expected[i] != received[i]
        ]
        for raw, ref, got in mismatches[:12]:
            dut._log.error(
                "%s x=0x%04x (%g) ref=0x%04x (%g) got=0x%04x (%g)",
                name, raw, decode(raw), ref, decode(ref), got, decode(got),
            )
        assert not mismatches, f"{name}: {len(mismatches)} bit mismatches"

        hw_values = [decode(raw) for raw in received]
        model_values = [result["output"] for result in evaluations]
        reference_values = [
            result["reference_quantized"] for result in evaluations
        ]
        hw_model_abs = [
            abs(hw - model_value)
            for hw, model_value in zip(hw_values, model_values)
        ]
        hw_model_ulp = [
            error / model.qfmt.ulp_size(model_value)
            for error, model_value in zip(hw_model_abs, model_values)
        ]
        function_abs = [
            abs(hw - reference)
            for hw, reference in zip(hw_values, reference_values)
        ]
        function_ulp = [
            error / model.qfmt.ulp_size(reference)
            for error, reference in zip(function_abs, reference_values)
        ]
        dut._log.info(
            "%-10s patterns=%d latency=%d exact_match=yes "
            "hw-model[max_abs=%g max_ulp=%g] "
            "function[max_abs=%g mean_abs=%g max_ulp=%g mean_ulp=%g]",
            name,
            len(inputs),
            output_cycles[0] - first_accept + 1,
            max(hw_model_abs),
            max(hw_model_ulp),
            max(function_abs),
            mean(function_abs),
            max(function_ulp),
            mean(function_ulp),
        )
        await tick(received, output_cycles)


@cocotb.test()
async def test_sfu_fp16_opcode_switch_and_invalid_domain(dut):
    await reset_dut(dut)
    dut.sfu_op.value = 0x2
    dut.a_in.value = encode(-1.0)
    dut.in_valid.value = 1
    for _ in range(50):
        await Timer(1, unit="ns")
        if int(dut.in_ready.value):
            break
        await RisingEdge(dut.clk)
    else:
        assert False, "initial configuration timed out"
    for index in range(8):
        dut.a_in.value = encode(-index / 2)
        await Timer(1, unit="ns")
        assert int(dut.in_ready.value)
        await RisingEdge(dut.clk)

    dut.sfu_op.value = 0x4
    dut.a_in.value = encode(-1.0)
    stall = 0
    for _ in range(70):
        await Timer(1, unit="ns")
        if int(dut.in_ready.value):
            break
        stall += 1
        await RisingEdge(dut.clk)
    else:
        assert False, "opcode switch timed out"
    assert stall > 0
    await RisingEdge(dut.clk)
    dut.in_valid.value = 0
    for _ in range(60):
        await RisingEdge(dut.clk)
        await Timer(2, unit="ns")
        if int(dut.c_valid.value) and int(dut.c_out.value) == CANONICAL_NAN:
            return
    assert False, "negative RSQRT did not produce canonical NaN"
