#!/usr/bin/env python3
"""Generate the compact unified binary16 SFU ROM (144-bit words)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from config_naming import resolve_config_path
from fp16_ieee import decode, encode


ROOT = Path(__file__).resolve().parents[1]
ROM_DEPTH = 128
BOUNDARIES_PER_WORD = 9
WORD_HEX_DIGITS = 36

FUNCTIONS = (
    (0x2, "EXP", "exp", "exp_fp16_highacc.json"),
    (0x3, "RCP", "reciprocal", "reciprocal_dynamick_wide_fp16_n6_opt.json"),
    (0x4, "RSQRT", "rsqrt", "rsqrt_fp16_seg1.json"),
    (0x5, "SIGMOID", "sigmoid", "sigmoid_fp16_highacc.json"),
    (0x6, "SILU", "silu", "silu_fp16_highacc.json"),
    (0x7, "GELU", "gelu", "gelu_fp16_highacc.json"),
    (0x8, "TANH", "tanh", "tanh_fp16_highacc.json"),
    (0x9, "MISH", "mish", "mish_fp16_highacc.json"),
)

NORM_MODE = {
    "affine_shift": 0,
    "exp_ln2": 1,
    "reciprocal_pow2": 2,
    "rsqrt_pow4": 3,
}

FINITE_VALUES = sorted(
    (
        (decode(raw), raw)
        for raw in range(0x10000)
        if ((raw >> 10) & 0x1F) != 0x1F
    ),
    key=lambda item: (item[0], item[1] & 0x8000),
)


def get_block(entry: dict, cfg: dict, name: str) -> dict:
    block = entry.get(name, cfg.get(name))
    if not isinstance(block, dict):
        raise ValueError(f"missing {name} block")
    return block


def load_function(cfg_dir: Path, filename: str) -> tuple[dict, list[dict]]:
    cfg = json.loads((cfg_dir / filename).read_text())
    if cfg["number_format"] != "fp16":
        raise ValueError(f"{filename}: expected fp16")
    entries = cfg.get("segments")
    if not isinstance(entries, list):
        entries = [cfg]
    return cfg, entries


def encode_boundary_ceil(value: float) -> int:
    for decoded, raw in FINITE_VALUES:
        if decoded >= float(value):
            return raw
    return 0x7BFF


def pack_parameter(cfg: dict, entry: dict) -> int:
    norm = get_block(entry, cfg, "normalization")
    horner = get_block(entry, cfg, "horner")
    coeffs = [float(value) for value in horner["coeffs"]]
    if len(coeffs) != int(horner["N"]) + 1 or len(coeffs) > 7:
        raise ValueError("FP16 hardware supports at most degree six")
    coeffs = [0.0] * (7 - len(coeffs)) + coeffs
    fields = [float(norm["c"]), float(norm["si"]), *coeffs]
    word = 0
    for lane, value in enumerate(fields):
        word |= encode(value) << (16 * lane)
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
        path = resolve_config_path(cfg_dir, function, "fp16", filename)
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

    words = [0] * ROM_DEPTH
    next_param = next_boundary
    for index, (
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
        words[index] = pack_descriptor(
            param_base,
            len(entries),
            boundary_base if boundaries else 0,
            len(boundaries),
            boundary_words,
            order,
            norm_mode,
        )
        for boundary_index, raw in enumerate(boundaries):
            address = boundary_base + boundary_index // BOUNDARIES_PER_WORD
            words[address] |= raw << (
                16 * (boundary_index % BOUNDARIES_PER_WORD)
            )
        for entry in entries:
            if next_param >= ROM_DEPTH:
                raise ValueError("FP16 configuration exceeds ROM depth")
            words[next_param] = pack_parameter(cfg, entry)
            next_param += 1
        print(
            f"{name:8s} descriptor={index:2d} "
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
        "--output", type=Path, default=ROOT / "rom" / "sfu_fp16_config.hex"
    )
    args = parser.parse_args()
    generate(args.cfg_dir, args.output)


if __name__ == "__main__":
    main()
