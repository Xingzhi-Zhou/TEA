# hdl_model/test_rom_sync.py
import os, random, collections, cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles, Timer, ReadOnly


TCQ = int(os.getenv("TCQ", "1"))                      # ns
LAT = int(os.getenv("ROM_LAT", "1"))                  # 允许从ROM_LAT 读取
MEMFILE = os.getenv("ROM_MEMFILE", "")
ROM_AW = int(os.getenv("ROM_AW", "8")) 
ROM_DW = int(os.getenv("ROM_DW", "16"))

if MEMFILE and os.path.isabs(MEMFILE):
    MEMFILE = MEMFILE
else:
    REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    MEMFILE = os.path.join(REPO_ROOT, "rom", MEMFILE) if MEMFILE else ""

if not MEMFILE or not os.path.exists(MEMFILE):
    raise FileNotFoundError(f"ROM_MEMFILE not found: {MEMFILE!r} (RAW={MEMFILE!r})")


def _readmemh_py(path: str, depth: int, dw: int):
    """
    读取 $readmemh 风格的 hex 文件，支持 '@<addr>' 跳转（十六进制地址），
    其它 token 当作 16 进制数据顺序写入。
    返回长度 = depth 的 list，每项已按 dw 位掩码。
    """
    mem = [0] * depth
    if not path or not os.path.exists(path):
        return mem
    mask = (1 << dw) - 1
    addr = 0
    with open(path, "r") as f:
        for line in f:
            # 去注释（// 及其后面）
            s = line.split("//")[0].strip()
            if not s:
                continue
            for tok in s.split():
                if tok.startswith("@") or tok.startswith("@".lower()):
                    # 地址跳转（十六进制）
                    try:
                        addr = int(tok[1:], 16)
                    except Exception:
                        pass
                else:
                    if addr < depth:
                        try:
                            mem[addr] = int(tok.replace("_", ""), 16) & mask
                        except Exception:
                            pass
                    addr += 1
                    if addr >= depth:
                        break
            if addr >= depth:
                break
    return mem

@cocotb.test()
async def test_rom_random_reads(dut):
    """
    随机 req/addr 读，检查 out_valid 对齐与 data 正确性。
    对任意 LAT（0/1/≥2）均适用：只在 out_valid==1 时弹出期望进行比较。
    """
    # 启动时钟
    clk = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clk.start())

    AW = len(dut.addr)
    DW = len(dut.data)

    assert AW == ROM_AW and DW == ROM_DW

    DEPTH = 1 << AW
    MASK = (1 << DW) - 1

    # 复位
    dut.rst.value = 1
    dut.req.value = 0
    dut.addr.value = 0
    await ClockCycles(dut.clk, 2)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)

    # 读取参考内存（若未提供文件，全部为 0）
    ref_mem = _readmemh_py(MEMFILE, DEPTH, DW)
    dut._log.debug(f"ROM test: AW={AW} DW={DW} LAT={LAT} TCQ={TCQ}ns "
                  f"MEMFILE={'(none)' if not MEMFILE else MEMFILE}")

    # 期望 FIFO：每次驱动 req=1 时把 mem[addr] 入队；看到 out_valid=1 时弹出并比较
    exp_fifo = collections.deque()

    for i in range(200):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value):
            assert exp_fifo, "DUT out_valid=1 但期望 FIFO 为空（流水对齐错误）"
            exp = exp_fifo.popleft() & MASK
            got = int(dut.data.value) & MASK
            assert got == exp, f"[i={i}] data mismatch: got=0x{got:0{(DW+3)//4}X}, exp=0x{exp:0{(DW+3)//4}X}"

        req = random.getrandbits(1)
        addr = random.randrange(0, DEPTH)
        await Timer(TCQ, unit="ns")
        dut.req.value = req
        dut.addr.value = addr

        if req:
            exp_fifo.append(ref_mem[addr])

    for _ in range(LAT+2):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value):
            assert exp_fifo, "DUT out_valid=1 但期望 FIFO 为空（流水对齐错误）"
            exp = exp_fifo.popleft() & MASK
            got = int(dut.data.value) & MASK
            assert got == exp, f"[i={i}] data mismatch: got=0x{got:0{(DW+3)//4}X}, exp=0x{exp:0{(DW+3)//4}X}"

        await Timer(TCQ, unit="ns")
        dut.req.value = 0

    assert not exp_fifo, "pipeline not drained"

@cocotb.test()
async def test_rom_reset_midstream(dut):
    """
    中途复位：复位期间/之后 out_valid 应清零；复位后重新打 req 应能正常工作。
    """
    clk = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clk.start())

    AW = len(dut.addr)
    DW = len(dut.data)
    DEPTH = 1 << AW
    MASK = (1 << DW) - 1

    dut.rst.value = 1
    dut.req.value = 0
    dut.addr.value = 0
    await ClockCycles(dut.clk, 2)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)

    ref_mem = _readmemh_py(MEMFILE, DEPTH, DW)

    exp_fifo = collections.deque()

    for i in range(10):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value):
            assert exp_fifo, "DUT out_valid=1 但期望 FIFO 为空（流水对齐错误）"
            exp = exp_fifo.popleft() & MASK
            got = int(dut.data.value) & MASK
            assert got == exp, f"[i={i}] data mismatch: got=0x{got:0{(DW+3)//4}X}, exp=0x{exp:0{(DW+3)//4}X}"


        addr = random.randrange(0, DEPTH)
        await Timer(TCQ, unit="ns")
        dut.req.value = 1
        dut.addr.value = addr
        exp_fifo.append(ref_mem[addr])

    # 发复位（同步复位）：清空期望 FIFO，并确保 out_valid 变为 0
    await RisingEdge(dut.clk)
    await Timer(TCQ, unit="ns")
    dut.rst.value = 1
    dut.req.value = 0
    exp_fifo.clear()

    await RisingEdge(dut.clk)
    assert int(dut.out_valid.value) == 1

    await Timer(TCQ, unit="ns")
    dut.rst.value = 0

    await ClockCycles(dut.clk, 1)
    assert int(dut.out_valid.value) == 0

    # 复位后再次发起请求，验证能恢复
    for i in range(10):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value):
            assert exp_fifo, "DUT out_valid=1 但期望 FIFO 为空（流水对齐错误）"
            exp = exp_fifo.popleft() & MASK
            got = int(dut.data.value) & MASK
            assert got == exp, f"[i={i}] data mismatch: got=0x{got:0{(DW+3)//4}X}, exp=0x{exp:0{(DW+3)//4}X}"

        req = random.getrandbits(1)
        addr = random.randrange(0, DEPTH)
        await Timer(TCQ, unit="ns")
        dut.req.value = req
        dut.addr.value = addr

        if req:
            exp_fifo.append(ref_mem[addr])

    # 排空
    for _ in range(LAT+2):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value):
            assert exp_fifo, "DUT out_valid=1 但期望 FIFO 为空（流水对齐错误）"
            exp = exp_fifo.popleft() & MASK
            got = int(dut.data.value) & MASK
            assert got == exp, f"[i={i}] data mismatch: got=0x{got:0{(DW+3)//4}X}, exp=0x{exp:0{(DW+3)//4}X}"

        await Timer(TCQ, unit="ns")
        dut.req.value = 0

    assert not exp_fifo, "pipeline not drained"
