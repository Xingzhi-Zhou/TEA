#!/usr/bin/env python3
"""Generate the unified Q3.5 SFU configuration ROM.

Address layout (64-bit words):
  0..7    function descriptors for opcodes 2..9
  8..23   two packed boundary words per function
  24..    per-segment normalization and polynomial parameters
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from config_naming import resolve_config_path


ROOT = Path(__file__).resolve().parents[1]
DESC_BASE = 0
BOUND_BASE = 8
PARAM_BASE = 24
ROM_DEPTH = 128
BOUNDARIES_PER_WORD = 8

FUNCTIONS = (
    (0x2, "EXP", "exp", "exp_q3_5_highacc.json"),
    (0x3, "RCP", "reciprocal", "reciprocal_dynamick_q3_5.json"),
    (0x4, "RSQRT", "rsqrt", "rsqrt_q3_5_seg1.json"),
    (0x5, "SIGMOID", "sigmoid", "sigmoid_q3_5_highacc.json"),
    (0x6, "SILU", "silu", "silu_q3_5_highacc.json"),
    (0x7, "GELU", "gelu", "gelu_q3_5_highacc.json"),
    (0x8, "TANH", "tanh", "tanh_q3_5_highacc.json"),
    (0x9, "MISH", "mish", "mish_q3_5_highacc.json"),
)

NORM_MODE = {
    "affine_shift": 0,
    "exp_ln2": 1,
    "reciprocal_pow2": 2,
    "rsqrt_pow4": 3,
}


def q35_raw(value: float) -> int:
    scaled = round(float(value) * 32.0)
    return min(max(scaled, -128), 127) & 0xFF


def load_config(path: Path) -> tuple[dict, list[dict]]:
    cfg = json.loads(path.read_text())
    if cfg["number_format"] != "q3_5":
        raise ValueError(f"{path}: expected q3_5")
    entries = cfg["segments"] if isinstance(cfg.get("segments"), list) else [cfg]
    return cfg, entries


def get_block(entry: dict, cfg: dict, name: str) -> dict:
    block = entry.get(name, cfg.get(name))
    if not isinstance(block, dict):
        raise ValueError(f"missing {name} block")
    return block


def pack_parameter(cfg: dict, entry: dict) -> int:
    norm = get_block(entry, cfg, "normalization")
    horner = get_block(entry, cfg, "horner")
    if int(norm["k"]) != 0:
        raise ValueError("normalization k must be zero")
    coeffs = [float(v) for v in horner["coeffs"]]
    if len(coeffs) > 6:
        raise ValueError("Q3.5 hardware supports at most degree five")
    coeffs = [0.0] * (6 - len(coeffs)) + coeffs
    fields = [
        q35_raw(norm["c"]),
        q35_raw(1.0 / float(norm["si"])),
        *[q35_raw(v) for v in coeffs],
    ]
    word = 0
    for index, field in enumerate(fields):
        word |= field << (8 * index)
    return word


def pack_descriptor(
    param_base: int,
    segment_count: int,
    boundary_words: int,
    order: int,
    norm_mode: int,
) -> int:
    denorm_mode = 1 if norm_mode in {1, 2, 3} else 0
    return (
        (param_base & 0x7F)
        | ((segment_count & 0x1F) << 7)
        | ((boundary_words & 0x3) << 12)
        | ((order & 0x7) << 14)
        | ((norm_mode & 0x3) << 17)
        | ((denorm_mode & 0x3) << 19)
        | (1 << 21)
        | (1 << 22)
    )


def generate(cfg_dir: Path, output: Path) -> None:
    words = [0] * ROM_DEPTH
    next_param = PARAM_BASE

    for function_index, (opcode, name, function, filename) in enumerate(FUNCTIONS):
        path = resolve_config_path(cfg_dir, function, "q3_5", filename)
        cfg, entries = load_config(path)
        param_base = next_param
        boundaries = [
            q35_raw(entry["domain"]["x_max"])
            for entry in entries[:-1]
        ]
        boundary_words = (len(boundaries) + BOUNDARIES_PER_WORD - 1) // BOUNDARIES_PER_WORD
        first_norm = get_block(entries[0], cfg, "normalization")
        norm_mode = NORM_MODE[first_norm["mode"]]
        order = max(int(get_block(entry, cfg, "horner")["N"]) for entry in entries)

        words[function_index] = pack_descriptor(
            param_base, len(entries), boundary_words, order, norm_mode
        )
        for boundary_index, raw in enumerate(boundaries):
            word_index = boundary_index // BOUNDARIES_PER_WORD
            lane = boundary_index % BOUNDARIES_PER_WORD
            address = BOUND_BASE + 2 * function_index + word_index
            words[address] |= raw << (8 * lane)

        for entry in entries:
            if next_param >= ROM_DEPTH:
                raise ValueError("configuration image exceeds ROM depth")
            words[next_param] = pack_parameter(cfg, entry)
            next_param += 1

        print(
            f"{name:8s} descriptor={function_index:2d} "
            f"boundary={BOUND_BASE + 2*function_index:2d} "
            f"parameter={param_base:2d} segments={len(entries):2d}"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(f"{word:016x}\n" for word in words))
    print(f"generated {next_param} used addresses ({ROM_DEPTH} total) in {output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cfg-dir", type=Path, default=ROOT / "cfg")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "rom" / "sfu_q3_5_config.hex",
    )
    args = parser.parse_args()
    generate(args.cfg_dir, args.output)


if __name__ == "__main__":
    main()
