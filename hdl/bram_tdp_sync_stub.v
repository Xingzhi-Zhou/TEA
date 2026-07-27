`timescale 1ns/1ps
// bram_tdp_sync_stub.v — 真双端口（2RW）同步 BRAM 行为模型（单时钟）
// - 端口 A/B 对称，均支持读写与字节写
// - WR_MODE：写读同拍（同地址）时的数据返回策略：0 = READ_FIRST（返回旧值）、1 = WRITE_FIRST（返回新写入值）、2 = NO_CHANGE（保持上一拍输出）。
// - WSTRB：按字节写使能，宽度 = DW/8。
// - 同拍同地址双写冲突通过 WPRI 指定优先级：0=A 优先，1=B 优先，2=写入 X（便于暴露问题）
module bram_tdp_sync_stub #(
  parameter integer TCQ     = 1,
  parameter integer AW      = 8,
  parameter integer DW      = 16,
  parameter integer LAT     = 1,              // 端口各自的输出延迟
  parameter integer WR_MODE = 1,              // 0:READ_FIRST, 1:WRITE_FIRST, 2:NO_CHANGE
  parameter integer WPRI    = 1,              // 双写冲突优先级：0=A_LAST, 1=B_LAST, 2=X_ON_COLLISION
  parameter        MEMFILE  = ""
)(
  input  wire               clk,
  input  wire               rst,

  // Port A
  input  wire [AW-1:0]      a_addr,
  input  wire               a_req,
  input  wire [DW-1:0]      a_din,
  input  wire [DW/8-1:0]    a_wstrb,
  output reg  [DW-1:0]      a_dout,
  output reg                a_valid,

  // Port B
  input  wire [AW-1:0]      b_addr,
  input  wire               b_req,
  input  wire [DW-1:0]      b_din,
  input  wire [DW/8-1:0]    b_wstrb,
  output reg  [DW-1:0]      b_dout,
  output reg                b_valid
);

  localparam integer DEPTH = (1 << AW);
  localparam integer BYTES = (DW+7)/8;

  reg [DW-1:0] mem [0:DEPTH-1];

  integer i;
  initial begin
    for (i = 0; i < DEPTH; i = i + 1) mem[i] = {DW{1'b0}};
    if (MEMFILE != "") $readmemh(MEMFILE, mem);
    $display("[%0t][%m] MEMFILE=%s mem[0]=0x%0h mem[1]=0x%0h",
             $time, MEMFILE, mem[0], mem[1]);
  end

  function [DW-1:0] apply_wstrb;
    input [DW-1:0] oldv;
    input [DW-1:0] newv;
    input [BYTES-1:0] st;
    integer b;
    reg [DW-1:0] mask;
  begin
    mask = {DW{1'b0}};
    for (b = 0; b < BYTES; b = b + 1) begin
      if (st[b]) mask[b*8 +: 8] = 8'hFF;
    end
    apply_wstrb = (oldv & ~mask) | (newv & mask);
  end
  endfunction

  function [DW-1:0] next_read_value;
    input [DW-1:0] oldv;
    input [DW-1:0] newv;
    input          any_w;
    input [DW-1:0] prev_d;
  begin
    case (WR_MODE)
      0:  next_read_value = oldv;                 // READ_FIRST
      1:  next_read_value = any_w ? newv : oldv;  // WRITE_FIRST
      2:  next_read_value = any_w ? prev_d : oldv;// NO_CHANGE
      default: next_read_value = oldv;
    endcase
  end
  endfunction

  // 为了在 LAT≥1 时方便，把“要返回的值”在本拍先确定后进流水
  // 注意：A/B 同拍、同地址的双写冲突：按 WPRI 处理对 mem 的最终写入
  // （这和真实工艺库的冲突行为可能略有不同，但足够用作功能仿真）

  // 组合阶段：抓取旧值，形成“拟写入”与“拟返回”
  reg [DW-1:0] a_old, a_new, a_rdv;
  reg [DW-1:0] b_old, b_new, b_rdv;
  reg          a_w, b_w, a_reads, b_reads;
  reg          same_addr;

  always @(*) begin
    a_old = mem[a_addr];
    b_old = mem[b_addr];

    a_w    = a_req && (|a_wstrb);
    b_w    = b_req && (|b_wstrb);
    a_reads= a_req && ~(|a_wstrb);
    b_reads= b_req && ~(|b_wstrb);

    same_addr = (a_addr == b_addr);

    a_new = a_w ? apply_wstrb(a_old, a_din, a_wstrb) : a_old;
    b_new = b_w ? apply_wstrb(b_old, b_din, b_wstrb) : b_old;

    // 先按“本端口写”决定同拍读写语义
    a_rdv = next_read_value(a_old, a_new, a_w, a_dout);
    b_rdv = next_read_value(b_old, b_new, b_w, b_dout);

    // 跨端口同拍读/写同址时，也按 WR_MODE 处理 ===
    if (same_addr) begin
      // A 口本拍纯读，而 B 口本拍写同址
      if (a_reads && b_w) begin
        case (WR_MODE)
          0: a_rdv = a_old;   // READ_FIRST：返回旧值
          1: a_rdv = b_new;   // WRITE_FIRST：返回对端口的新值
          2: a_rdv = a_dout;  // NO_CHANGE：保持上一拍输出
          default: a_rdv = a_old;
        endcase
      end
      // B 口本拍纯读，而 A 口本拍写同址
      if (b_reads && a_w) begin
        case (WR_MODE)
          0: b_rdv = b_old;
          1: b_rdv = a_new;
          2: b_rdv = b_dout;
          default: b_rdv = b_old;
        endcase
      end
    end
  end


  // 数据/有效流水（每个端口独立）
  generate
    if (LAT == 0) begin : g0
      always @(posedge clk) begin
        if (rst) begin
          a_dout  <= #TCQ {DW{1'b0}};
          b_dout  <= #TCQ {DW{1'b0}};
          a_valid <= #TCQ 1'b0;
          b_valid <= #TCQ 1'b0;
        end else begin
          if (a_req) a_dout <= #TCQ a_rdv;
          if (b_req) b_dout <= #TCQ b_rdv;
          a_valid <= #TCQ a_req;
          b_valid <= #TCQ b_req;

          // 同拍对 mem 的最终写入（处理冲突）
          if (a_w && b_w && (a_addr == b_addr)) begin
            case (WPRI)
              0: mem[a_addr] <= #TCQ a_new;                  // A 优先
              1: mem[b_addr] <= #TCQ b_new;                  // B 优先
              2: mem[a_addr] <= #TCQ {DW{1'bx}};             // 写 X
              default: mem[b_addr] <= #TCQ b_new;
            endcase
          end else begin
            if (a_w) mem[a_addr] <= #TCQ a_new;
            if (b_w) mem[b_addr] <= #TCQ b_new;
          end
        end
      end
    end else if (LAT == 1) begin : g1
      reg [DW-1:0] a_d1 = {DW{1'b0}}, b_d1 = {DW{1'b0}};
      reg          a_v1 = 1'b0,        b_v1 = 1'b0;
      always @(posedge clk) begin
        if (rst) begin
          a_d1    <= #TCQ {DW{1'b0}};
          b_d1    <= #TCQ {DW{1'b0}};
          a_v1    <= #TCQ 1'b0;
          b_v1    <= #TCQ 1'b0;
          a_dout  <= #TCQ {DW{1'b0}};
          b_dout  <= #TCQ {DW{1'b0}};
          a_valid <= #TCQ 1'b0;
          b_valid <= #TCQ 1'b0;
        end else begin
          if (a_req) a_d1 <= #TCQ a_rdv;
          if (b_req) b_d1 <= #TCQ b_rdv;
          a_v1 <= #TCQ a_req;
          b_v1 <= #TCQ b_req;

          a_dout  <= #TCQ a_d1;
          b_dout  <= #TCQ b_d1;
          a_valid <= #TCQ a_v1;
          b_valid <= #TCQ b_v1;

          // 最终写入
          if (a_w && b_w && (a_addr == b_addr)) begin
            case (WPRI)
              0: mem[a_addr] <= #TCQ a_new;
              1: mem[b_addr] <= #TCQ b_new;
              2: mem[a_addr] <= #TCQ {DW{1'bx}};
              default: mem[b_addr] <= #TCQ b_new;
            endcase
          end else begin
            if (a_w) mem[a_addr] <= #TCQ a_new;
            if (b_w) mem[b_addr] <= #TCQ b_new;
          end
        end
      end
    end else begin : gN
      integer k;
      reg [DW-1:0] a_pipe [0:LAT-1];
      reg [DW-1:0] b_pipe [0:LAT-1];
      reg [LAT-1:0] a_vsr, b_vsr;
      initial begin
        for (k = 0; k < LAT; k = k + 1) begin
          a_pipe[k] = {DW{1'b0}};
          b_pipe[k] = {DW{1'b0}};
        end
        a_vsr = {LAT{1'b0}};
        b_vsr = {LAT{1'b0}};
      end

      always @(posedge clk) begin
        if (rst) begin
          for (k = 0; k < LAT; k = k + 1) begin
            a_pipe[k] <= #TCQ {DW{1'b0}};
            b_pipe[k] <= #TCQ {DW{1'b0}};
          end
          a_vsr    <= #TCQ {LAT{1'b0}};
          b_vsr    <= #TCQ {LAT{1'b0}};
          a_dout   <= #TCQ {DW{1'b0}};
          b_dout   <= #TCQ {DW{1'b0}};
          a_valid  <= #TCQ 1'b0;
          b_valid  <= #TCQ 1'b0;
        end else begin
          if (a_req) begin
            a_pipe[0] <= #TCQ a_rdv;
            for (k = 1; k < LAT; k = k + 1)
              a_pipe[k] <= #TCQ a_pipe[k-1];
            a_vsr <= #TCQ {a_vsr[LAT-2:0], 1'b1};
          end else begin
            a_vsr <= #TCQ {a_vsr[LAT-2:0], 1'b0};
          end

          if (b_req) begin
            b_pipe[0] <= #TCQ b_rdv;
            for (k = 1; k < LAT; k = k + 1)
              b_pipe[k] <= #TCQ b_pipe[k-1];
            b_vsr <= #TCQ {b_vsr[LAT-2:0], 1'b1};
          end else begin
            b_vsr <= #TCQ {b_vsr[LAT-2:0], 1'b0};
          end

          a_dout  <= #TCQ a_pipe[LAT-1];
          b_dout  <= #TCQ b_pipe[LAT-1];
          a_valid <= #TCQ a_vsr[LAT-1];
          b_valid <= #TCQ b_vsr[LAT-1];

          // 最终写入
          if (a_w && b_w && (a_addr == b_addr)) begin
            case (WPRI)
              0: mem[a_addr] <= #TCQ a_new;
              1: mem[b_addr] <= #TCQ b_new;
              2: mem[a_addr] <= #TCQ {DW{1'bx}};
              default: mem[b_addr] <= #TCQ b_new;
            endcase
          end else begin
            if (a_w) mem[a_addr] <= #TCQ a_new;
            if (b_w) mem[b_addr] <= #TCQ b_new;
          end
        end
      end
    end
  endgenerate

endmodule
