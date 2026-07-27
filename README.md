# TEA: Taylor-Expansion-Inspired Unified Hardware Acceleration for High-Precision Non-Linearity in Edge AI

TEA is a unified hardware accelerator for nonlinear functions in Edge AI.
It employs a configurable, piecewise Taylor/Horner framework to support EXP,
Reciprocal, Rsqrt, Sigmoid, SiLU, GELU, Tanh, and Mish on the same hardware
datapath. Four numerical formats are provided: Q6.10, Q3.5, FP8 E4M3, and
FP16. A fixed-OPCODE stream achieves an initiation interval of `II=1`;
when the function changes, the pipeline is drained before the new parameters
are loaded from the shared configuration ROM.
For ease of use and learning, the complete workflow—from configuration ROM
generation and RTL simulation to result checking—can be run through Python.

## Environment Setup

The project has been verified with:

- Python 3.12
- cocotb 2.0.0
- cocotb-test 0.2.6
- Icarus Verilog 11.0
- GTKWave (optional, for waveform inspection)

An isolated Conda environment is recommended:

```bash
conda create -n tea python=3.12 -y
conda activate tea

python -m pip install cocotb==2.0.0 cocotb-test==0.2.6
```

On Ubuntu or Debian, install Icarus Verilog and the optional GTKWave viewer:

```bash
sudo apt-get update
sudo apt-get install iverilog gtkwave
```

Check the environment:

```bash
python --version
iverilog -V
```

## Quick Start

### Run the Full Test Suite

```bash
python -u test_runner.py
```

This command generates the configuration ROMs and runs the Q6.10, Q3.5, FP8,
and FP16 cocotb tests. The tests cover all eight functions, continuous
fixed-OPCODE traffic at `II=1`, and pipeline draining and recovery during an
OPCODE switch.

To test a single numerical format:

```bash
python -u test_runner.py --target q6_10
python -u test_runner.py --target q3_5
python -u test_runner.py --target fp8
python -u test_runner.py --target fp16
```
