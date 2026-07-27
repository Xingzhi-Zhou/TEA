#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 Verilog $readmemh 可加载的 ROM 文件（hex）
用法示例：
  python tools/gen_rom.py --aw 8 --dw 16 --pattern ramp   --outfile rom/rom_aw8_dw16_ramp.hex
  python tools/gen_rom.py --aw 8 --dw 16 --pattern random --seed 2025 --outfile rom/rom_aw8_dw16_rand.hex
  python tools/gen_rom.py --aw 8 --dw 16 --pattern const  --const 0xE000 --outfile rom/rom_aw8_dw16_const.hex
  python tools/gen_rom.py --aw 8 --dw 16 --pattern sine   --period 64 --outfile rom/rom_aw8_dw16_sine.hex
"""

import argparse, os, math, random
from pathlib import Path

def gen_values(aw: int, dw: int, pattern: str, **kwargs):
    depth = 1 << aw
    mask  = (1 << dw) - 1
    vals  = [0] * depth

    if pattern == "ramp":
        for i in range(depth):
            vals[i] = i & mask

    elif pattern == "random":
        seed = kwargs.get("seed", None)
        if seed is not None:
            random.seed(int(seed))
        for i in range(depth):
            vals[i] = random.randint(0, mask)

    elif pattern == "const":
        c = kwargs.get("const", 0)
        if isinstance(c, str):
            c = int(c, 0)
        c &= mask
        for i in range(depth):
            vals[i] = c

    elif pattern == "sine":
        # 生成“有符号两补码”正弦波（满幅），再按 dw 位掩码存成 hex
        period = int(kwargs.get("period", max(8, depth)))
        maxv = (1 << (dw - 1)) - 1
        for i in range(depth):
            x = math.sin(2.0 * math.pi * i / period)
            si = int(round(x * maxv))           # [-maxv, +maxv]
            vals[i] = si & mask                  # 存成两补码位模式
    else:
        raise ValueError(f"Unsupported pattern: {pattern}")

    return vals

def write_memh(path: str, values, dw: int, with_addr: bool):
    Path(os.path.dirname(path) or ".").mkdir(parents=True, exist_ok=True)
    width = (dw + 3) // 4  # 每个数据的十六进制位数
    with open(path, "w") as f:
        if with_addr:
            # 可选：写起始地址（$readmemh 支持 @<hexaddr> 跳转）
            f.write("@0\n")
        for v in values:
            f.write(f"{v:0{width}X}\n")

def main():
    p = argparse.ArgumentParser(description="Generate $readmemh hex file for ROM")
    p.add_argument("--aw", type=int, required=True, help="address width (AW)")
    p.add_argument("--dw", type=int, required=True, help="data width (DW)")
    p.add_argument("--pattern", choices=["ramp","random","const","sine"], required=True)
    p.add_argument("--outfile", type=str, required=True, help="output hex path, e.g. rom/rom_aw8_dw16.hex")
    p.add_argument("--seed", type=int, default=None, help="random seed (pattern=random)")
    p.add_argument("--const", type=str, default="0x0000", help="const value like 0xE000 (pattern=const)")
    p.add_argument("--period", type=int, default=64, help="sine period (pattern=sine)")
    p.add_argument("--with-addr", action="store_true", help="emit @0 header (readmemh address directive)")
    args = p.parse_args()

    vals = gen_values(args.aw, args.dw, args.pattern,
                      seed=args.seed, const=args.const, period=args.period)
    write_memh(args.outfile, vals, args.dw, with_addr=args.with_addr)
    print(f"[OK] wrote {len(vals)} words to {args.outfile}")

if __name__ == "__main__":
    main()
