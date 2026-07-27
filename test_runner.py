import argparse
import os
import subprocess
import sys
from cocotb_test.simulator import run

BASE     = os.path.dirname(os.path.abspath(__file__))
HDL_DIR  = os.path.join(BASE, "hdl")
TEST_DIR = os.path.join(BASE, "hdl_model")
UTIL_DIR = os.path.join(BASE, "utils")

SFU_CFG_DIR = os.path.join(BASE, "cfg")
SFU_ROM_DIR = os.path.join(BASE, "rom")
SFU_BUILD_TAG = ""

TCQ = 1
DSP_FMA_LAT = 1

ROM_AW = 8
ROM_DW = 16
ROM_LAT = 0
ROM_MEMFILE = "rom_aw8_dw16_ramp.hex"

BRAM_AW = 8
BRAM_DW = 16
BRAM_LAT = 1           # 0/1/≥2
BRAM_WR_MODE = 1       # 0: READ_FIRST, 1: WRITE_FIRST, 2: NO_CHANGE
BRAM_WPRI = 0          # 0: A优先, 1: B优先, 2: X(写冲突打X)
# 随机压力测试参数（test_bram_tdp_sync.py 会读取）
BRAM_SEED = 12345
BRAM_N_CYCLES = 300

RAM_AW = 8
RAM_DW = 16
RAM_LAT = 0  

def _env_for(level="INFO"):
    env = {
        "COCOTB_LOG_LEVEL": level,                       # TRACE/DEBUG/INFO/WARNING/ERROR
        "COCOTB_ANSI_OUTPUT": "0",                       # 日志去掉彩色控制符，便于 grep

        "TCQ": TCQ,
        "DSP_FMA_LAT": DSP_FMA_LAT,
        "ROM_AW": ROM_AW,
        "ROM_DW": ROM_DW,
        "ROM_LAT": ROM_LAT,
        "ROM_MEMFILE": ROM_MEMFILE,

        "BRAM_LAT": BRAM_LAT,
        "BRAM_WR_MODE": BRAM_WR_MODE,
        "BRAM_WPRI": BRAM_WPRI,
        "BRAM_SEED": BRAM_SEED,
        "BRAM_N_CYCLES": BRAM_N_CYCLES,

        "RAM_AW": RAM_AW,
        "RAM_DW": RAM_DW,
        "RAM_LAT": RAM_LAT,
        "SFU_CFG_DIR": SFU_CFG_DIR,
    }
    return {k: str(v) for k, v in env.items()}


def _sfu_build_dir(name):
    if SFU_BUILD_TAG:
        return os.path.join(BASE, "sim_build", SFU_BUILD_TAG, name)
    return os.path.join(BASE, "sim_build", name)

def run_dsp_fma():
    build = os.path.join(BASE, "sim_build", "dsp_fma")

    run(
        verilog_sources=[os.path.join(HDL_DIR, "dsp_fma_stub.v")],
        toplevel="dsp_fma_stub",
        toplevel_lang="verilog",
        module="test_dsp_fma",
        python_search=[TEST_DIR, UTIL_DIR],
        parameters={"W": 16, "LAT": DSP_FMA_LAT},
        sim_build=build,
        waves=True,
        force_compile=True,                        # 每次都强制重编译，避免缓存带来的参数失效
        extra_env=_env_for(level="INFO")
    )

def run_rom_sync():
    rom_hex = os.path.join(BASE, "rom", ROM_MEMFILE)

    build = os.path.join(BASE, "sim_build", "rom_sync")

    run(
        verilog_sources=[os.path.join(HDL_DIR, "rom_sync_stub.v")],
        toplevel="rom_sync_stub",
        toplevel_lang="verilog",
        module="test_rom_sync",
        python_search=[TEST_DIR, BASE, UTIL_DIR],
        parameters={"AW": ROM_AW, "DW": ROM_DW, "LAT": ROM_LAT, "MEMFILE": f'"{rom_hex}"'},
        sim_build=build,
        waves=True,
        force_compile=True,
        extra_env=_env_for(level="DEBUG"), 
    )

def run_ram_tdp_sync():
    build = os.path.join(BASE, "sim_build", "ram_tdp_sync")

    run(
        verilog_sources=[os.path.join(HDL_DIR, "ram_tdp_sync_stub.v")],
        toplevel="ram_tdp_sync_stub",
        toplevel_lang="verilog",
        module="test_ram_tdp_sync",
        python_search=[TEST_DIR, BASE, UTIL_DIR],
        parameters={"AW": RAM_AW, "DW": RAM_DW, "LAT": RAM_LAT},
        sim_build=build,
        waves=True,
        force_compile=True,
        extra_env=_env_for(level="DEBUG"), 
    )

def run_bram_tdp_sync():
    build = os.path.join(BASE, "sim_build", "bram_tdp_sync")

    run(
        verilog_sources=[os.path.join(HDL_DIR, "bram_tdp_sync_stub.v")],
        toplevel="bram_tdp_sync_stub",
        toplevel_lang="verilog",
        module="test_bram_tdp_sync",
        python_search=[TEST_DIR, BASE, UTIL_DIR],
        parameters={"AW": BRAM_AW, "DW": BRAM_DW, "LAT": BRAM_LAT, "WR_MODE": BRAM_WR_MODE, "WPRI": BRAM_WPRI,},
        sim_build=build,
        waves=True,
        force_compile=True,
        extra_env=_env_for(level="DEBUG"), 
    )

def run_sfu_q6_10():
    """Generate cfg-backed ROM contents and run the unified Q6.10 SFU."""
    import subprocess

    config_file = os.path.join(SFU_ROM_DIR, "sfu_q6_10_config.hex")
    subprocess.run(
        [
            "python",
            os.path.join(UTIL_DIR, "gen_sfu_q6_10_rom.py"),
            "--cfg-dir",
            SFU_CFG_DIR,
            "--output",
            config_file,
        ],
        cwd=BASE,
        check=True,
    )

    build = _sfu_build_dir("sfu_q6_10")
    verilog_sources = [
        os.path.join(HDL_DIR, "dsp48e_fma_stub.v"),
        os.path.join(HDL_DIR, "rom_sync_stub.v"),
        os.path.join(HDL_DIR, "SFU_Q6_10.v"),
    ]

    run(
        verilog_sources=verilog_sources,
        toplevel="SFU_Q6_10",
        toplevel_lang="verilog",
        module="test_sfu_q6_10",
        python_search=[TEST_DIR, BASE, UTIL_DIR],
        parameters={
            "DSP_LAT": DSP_FMA_LAT,
            "CONFIG_FILE": f'"{config_file}"',
        },
        compile_args=[f"-I{HDL_DIR}"],
        sim_build=build,
        waves=False,
        force_compile=True,
        extra_env=_env_for(level="INFO"),
    )

def run_sfu_q3_5():
    """Generate cfg-backed ROM contents and run the unified Q3.5 SFU."""
    import subprocess

    config_file = os.path.join(SFU_ROM_DIR, "sfu_q3_5_config.hex")
    subprocess.run(
        [
            "python",
            os.path.join(UTIL_DIR, "gen_sfu_q3_5_rom.py"),
            "--cfg-dir",
            SFU_CFG_DIR,
            "--output",
            config_file,
        ],
        cwd=BASE,
        check=True,
    )

    build = _sfu_build_dir("sfu_q3_5")
    verilog_sources = [
        os.path.join(HDL_DIR, "dsp48e_fma_stub.v"),
        os.path.join(HDL_DIR, "rom_sync_stub.v"),
        os.path.join(HDL_DIR, "SFU_Q3_5.v"),
    ]

    run(
        verilog_sources=verilog_sources,
        toplevel="SFU_Q3_5",
        toplevel_lang="verilog",
        module="test_sfu_q3_5",
        python_search=[TEST_DIR, BASE, UTIL_DIR],
        parameters={
            "DSP_LAT": DSP_FMA_LAT,
            "CONFIG_FILE": f'"{config_file}"',
        },
        compile_args=[f"-I{HDL_DIR}"],
        sim_build=build,
        waves=False,
        force_compile=True,
        extra_env=_env_for(level="INFO"),
    )

def run_sfu_fp8():
    """Generate the compact E4M3 image and run the unified FP8 SFU."""
    import subprocess

    config_file = os.path.join(SFU_ROM_DIR, "sfu_fp8_config.hex")
    subprocess.run(
        [
            "python",
            os.path.join(UTIL_DIR, "gen_sfu_fp8_rom.py"),
            "--cfg-dir",
            SFU_CFG_DIR,
            "--output",
            config_file,
        ],
        cwd=BASE,
        check=True,
    )

    build = _sfu_build_dir("sfu_fp8")
    verilog_sources = [
        os.path.join(HDL_DIR, "dsp48e_fma_stub.v"),
        os.path.join(HDL_DIR, "rom_sync_stub.v"),
        os.path.join(HDL_DIR, "SFU_FP8.v"),
    ]

    run(
        verilog_sources=verilog_sources,
        toplevel="SFU_FP8",
        toplevel_lang="verilog",
        module="test_sfu_fp8",
        python_search=[TEST_DIR, BASE, UTIL_DIR],
        parameters={
            "DSP_LAT": DSP_FMA_LAT,
            "CONFIG_FILE": f'"{config_file}"',
        },
        compile_args=[f"-I{HDL_DIR}"],
        sim_build=build,
        waves=False,
        force_compile=True,
        extra_env=_env_for(level="INFO"),
    )

def run_sfu_fp16():
    """Generate the compact binary16 image and run the unified FP16 SFU."""
    import subprocess

    config_file = os.path.join(SFU_ROM_DIR, "sfu_fp16_config.hex")
    subprocess.run(
        [
            "python",
            os.path.join(UTIL_DIR, "gen_sfu_fp16_rom.py"),
            "--cfg-dir",
            SFU_CFG_DIR,
            "--output",
            config_file,
        ],
        cwd=BASE,
        check=True,
    )
    build = _sfu_build_dir("sfu_fp16")
    run(
        verilog_sources=[
            os.path.join(HDL_DIR, "dsp48e_fma_stub.v"),
            os.path.join(HDL_DIR, "rom_sync_stub.v"),
            os.path.join(HDL_DIR, "SFU_FP16.v"),
        ],
        toplevel="SFU_FP16",
        toplevel_lang="verilog",
        module="test_sfu_fp16",
        python_search=[TEST_DIR, BASE, UTIL_DIR],
        parameters={
            "DSP_LAT": DSP_FMA_LAT,
            "CONFIG_FILE": f'"{config_file}"',
        },
        compile_args=[f"-I{HDL_DIR}"],
        sim_build=build,
        waves=False,
        force_compile=True,
        extra_env=_env_for(level="INFO"),
    )

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cfg-dir", default=os.path.join(BASE, "cfg"))
    parser.add_argument("--rom-dir", default=os.path.join(BASE, "rom"))
    parser.add_argument(
        "--build-tag",
        default="",
        help="Optional subdirectory below sim_build for isolated results",
    )
    parser.add_argument(
        "--target",
        choices=("all", "q6_10", "q3_5", "fp8", "fp16"),
        default="all",
        help="Run one SFU, or isolate and run all four (default)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    SFU_CFG_DIR = os.path.abspath(args.cfg_dir)
    SFU_ROM_DIR = os.path.abspath(args.rom_dir)
    SFU_BUILD_TAG = args.build_tag
    os.makedirs(SFU_ROM_DIR, exist_ok=True)
    runners = {
        "q6_10": run_sfu_q6_10,
        "q3_5": run_sfu_q3_5,
        "fp8": run_sfu_fp8,
        "fp16": run_sfu_fp16,
    }
    if args.target == "all":
        # cocotb_test can retain asyncio/subprocess state across repeated
        # simulator invocations.  Process isolation keeps a full regression
        # deterministic and also gives every simulator a clean environment.
        for target in runners:
            command = [
                sys.executable,
                os.path.abspath(__file__),
                "--cfg-dir",
                SFU_CFG_DIR,
                "--rom-dir",
                SFU_ROM_DIR,
                "--build-tag",
                SFU_BUILD_TAG,
                "--target",
                target,
            ]
            subprocess.run(command, cwd=BASE, check=True)
    else:
        runners[args.target]()
