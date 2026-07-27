# hdl_model/test_rom_sync.py
import os, random, collections, cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles, Timer


TCQ = int(os.getenv("TCQ", "1"))
LAT = int(os.getenv("RAM_LAT", "0"))
RAM_AW = int(os.getenv("RAM_AW", "8")) 
RAM_DW = int(os.getenv("RAM_DW", "16"))

def drive_port(dut, a_req, a_addr, a_wr_en, a_din, b_req, b_addr, b_wr_en, b_din):
    # port a
    dut.a_req.value   = int(a_req)
    dut.a_addr.value  = int(a_addr)
    dut.a_wr_en.value = int(a_wr_en)
    dut.a_din.value   = int(a_din)
    # port b
    dut.b_req.value   = int(b_req)
    dut.b_addr.value  = int(b_addr)
    dut.b_wr_en.value = int(b_wr_en)
    dut.b_din.value   = int(b_din)

@cocotb.test()
async def test_ram_random_aw_br(dut):
    # clock
    clk = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clk.start())

    AW = len(dut.a_addr)
    DW = len(dut.a_dout)

    assert AW == RAM_AW and DW == RAM_DW

    DEPTH = 1 << AW

    mem = [0] *  DEPTH

    # reset
    dut.rst.value = 1
    drive_port(dut, 0, 0, 0, 0, 0, 0, 0, 0)
    await ClockCycles(dut.clk, 2)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)

    # write a
    for i in range(DEPTH):
        await RisingEdge(dut.clk)
        await Timer(TCQ, unit="ns")
        data = random.randrange(1, 4096)
        # data = i+1
        mem[i] = data
        drive_port(dut, 1, i, 1, data, 0, 0, 0, 0)

    # read b
    for i in range(DEPTH):
        await RisingEdge(dut.clk)

        if i > LAT+1:
            got_data = dut.b_dout.value.to_signed()
            exp_data = mem[i-(LAT+2)]
            assert got_data == exp_data,f"mismatch: got {got_data}, addr {i-(LAT+2)}, exp {mem[i-(LAT+2)]}"

        await Timer(TCQ, unit="ns")
        drive_port(dut, 0, 0, 0, 0, 1, i, 0, 0)

    await RisingEdge(dut.clk)
    got_data = dut.b_dout.value.to_signed()
    exp_data = mem[i-(LAT+1)]
    assert got_data == exp_data,f"mismatch: got {got_data}, addr {i-(LAT+1)}, exp {mem[i-(LAT+1)]}"

    await Timer(TCQ, unit="ns")
    drive_port(dut, 0, 0, 0, 0, 0, 0, 0, 0)

    await RisingEdge(dut.clk)
    got_data = dut.b_dout.value.to_signed()
    exp_data = mem[i-(LAT)]
    assert got_data == exp_data,f"mismatch: got {got_data}, addr {i-(LAT)}, exp {mem[i-(LAT)]}"

    if LAT:
        await RisingEdge(dut.clk)
        got_data = dut.b_dout.value.to_signed()
        exp_data = mem[i]
        assert got_data == exp_data,f"mismatch: got {got_data}, addr {i}, exp {mem[i]}"

