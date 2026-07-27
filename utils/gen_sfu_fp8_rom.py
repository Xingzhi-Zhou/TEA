#!/usr/bin/env python3
"""Generate the unified, compact FP8 E4M3 SFU configuration ROM.

Each word is 72 bits:
  0..7       descriptors for opcodes 2..9
  8..P-1     tightly packed boundary records (nine FP8 values per word)
  P..        per-segment records: c, si, and seven Horner coefficients
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from config_naming import resolve_config_path
from fp8_e4m3 import decode, encode


ROOT = Path(__file__).resolve().parents[1]
ROM_DEPTH = 128
WORD_HEX_DIGITS = 18
BOUNDARIES_PER_WORD = 9

FUNCTIONS = (
    (0x2, "EXP", "exp", "exp_fp8_highacc.json"),
    (0x3, "RCP", "reciprocal", "reciprocal_dynamick_wide_fp8.json"),
    (0x4, "RSQRT", "rsqrt", "rsqrt_fp8_seg1.json"),
    (0x5, "SIGMOID", "sigmoid", "sigmoid_fp8_highacc.json"),
    (0x6, "SILU", "silu", "silu_fp8_highacc.json"),
    (0x7, "GELU", "gelu", "gelu_fp8_highacc.json"),
    (0x8, "TANH", "tanh", "tanh_fp8_highacc.json"),
    (0x9, "MISH", "mish", "mish_fp8_highacc.json"),
)

NORM_MODE = {
    "affine_shift": 0,
    "exp_ln2": 1,
    "reciprocal_pow2": 2,
    "rsqrt_pow4": 3,
}


def get_block(entry: dict, cfg: dict, name: str) -> dict:
    block = entry.get(name, cfg.get(name))
    if not isinstance(block, dict):
        raise ValueError(f"missing {name} block")
    return block


def load_function(cfg_dir: Path, filename: str) -> tuple[dict, list[dict]]:
    cfg = json.loads((cfg_dir / filename).read_text())
    if cfg["number_format"] != "fp8":
        raise ValueError(f"{filename}: expected fp8")
    entries = cfg.get("segments")
    if not isinstance(entries, list):
        entries = [cfg]
    return cfg, entries


def encode_boundary_ceil(value: float) -> int:
    """Encode the first finite FP8 value that is >= a real boundary.

    Segment domains are expressed in real numbers, while an input has already
    been quantized to FP8. Nearest-rounding a boundary can move the transition
    to the wrong representable input, so boundary records use a numeric ceil.
    """

    candidates = [
        raw
        for raw in range(256)
        if ((raw >> 3) & 0xF) != 0xF and decode(raw) >= float(value)
    ]
    if not candidates:
        return 0x77
    return min(candidates, key=lambda raw: (decode(raw), raw & 0x80))


def pack_parameter(cfg: dict, entry: dict) -> int:
    norm = get_block(entry, cfg, "normalization")
    horner = get_block(entry, cfg, "horner")
    if int(norm.get("k", 0)) != 0:
        raise ValueError("FP8 hardware currently requires normalization k=0")
    coeffs = [float(value) for value in horner["coeffs"]]
    if len(coeffs) != int(horner["N"]) + 1 or len(coeffs) > 7:
        raise ValueError("FP8 hardware supports at most degree six")
    coeffs = [0.0] * (7 - len(coeffs)) + coeffs
    fields = [float(norm["c"]), float(norm["si"]), *coeffs]
    word = 0
    for lane, value in enumerate(fields):
        word |= encode(value) << (8 * lane)
    return word


def pack_descriptor(
    param_base: int,
    segment_count: int,
    boundary_base: int,
    boundary_count: int,
    boundary_words: int,
    order: int,
    norm_mode: int,
) -> int:
    # [6:0] param base, [11:7] segments, [18:12] boundary base,
    # [22:19] boundary count, [24:23] boundary words,
    # [27:25] order, [29:28] normalization mode.
    return (
        (param_base & 0x7F)
        | ((segment_count & 0x1F) << 7)
        | ((boundary_base & 0x7F) << 12)
        | ((boundary_count & 0xF) << 19)
        | ((boundary_words & 0x3) << 23)
        | ((order & 0x7) << 25)
        | ((norm_mode & 0x3) << 28)
    )


def generate(cfg_dir: Path, output: Path) -> None:
    loaded = []
    next_boundary = len(FUNCTIONS)
    for opcode, name, function, filename in FUNCTIONS:
        path = resolve_config_path(cfg_dir, function, "fp8", filename)
        cfg, entries = load_function(path.parent, path.name)
        boundaries = [
            encode_boundary_ceil(entry["domain"]["x_max"])
            for entry in entries[:-1]
        ]
        boundary_words = (
            len(boundaries) + BOUNDARIES_PER_WORD - 1
        ) // BOUNDARIES_PER_WORD
        loaded.append(
            (opcode, name, cfg, entries, boundaries, next_boundary, boundary_words)
        )
        next_boundary += boundary_words

    next_param = next_boundary
    words = [0] * ROM_DEPTH
    for function_index, (
        opcode,
        name,
        cfg,
        entries,
        boundaries,
        boundary_base,
        boundary_words,
    ) in enumerate(loaded):
        param_base = next_param
        first_norm = get_block(entries[0], cfg, "normalization")
        norm_mode = NORM_MODE[first_norm["mode"]]
        order = max(
            int(get_block(entry, cfg, "horner")["N"]) for entry in entries
        )
        words[function_index] = pack_descriptor(
            param_base,
            len(entries),
            boundary_base if boundaries else 0,
            len(boundaries),
            boundary_words,
            order,
            norm_mode,
        )

        for index, raw in enumerate(boundaries):
            address = boundary_base + index // BOUNDARIES_PER_WORD
            words[address] |= raw << (8 * (index % BOUNDARIES_PER_WORD))

        for entry in entries:
            if next_param >= ROM_DEPTH:
                raise ValueError("FP8 configuration exceeds ROM depth")
            words[next_param] = pack_parameter(cfg, entry)
            next_param += 1

        print(
            f"{name:8s} descriptor={function_index:2d} "
            f"boundary={boundary_base if boundaries else 0:2d} "
            f"parameter={param_base:2d} segments={len(entries):2d}"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(f"{word:0{WORD_HEX_DIGITS}x}\n" for word in words)
    )
    print(
        f"generated {next_param} used addresses "
        f"({ROM_DEPTH} total) in {output}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cfg-dir", type=Path, default=ROOT / "cfg")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "rom" / "sfu_fp8_config.hex"
    )
    args = parser.parse_args()
    generate(args.cfg_dir, args.output)


if __name__ == "__main__":
    main()
