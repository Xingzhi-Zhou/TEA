`timescale 1ns/1ps
// rom_sync_stub.v — 同步 ROM（默认 LAT=1，带同步复位）
module rom_sync_stub #(
  parameter integer TCQ    = 1,              // 仿真用时序延迟（单位 = `timescale 时间单位）
  parameter integer AW     = 8,              // 地址位宽
  parameter integer DW     = 16,             // 数据位宽
  parameter integer LAT    = 1,              // 管线延迟：0/1/≥2
  parameter        MEMFILE = ""              // 初值文件（hex）
)(
  input  wire           clk,
  input  wire           rst,                 // 同步复位，高有效
  input  wire [AW-1:0]  addr,
  input  wire           req,
  output reg  [DW-1:0]  data,
  output reg            out_valid
);

  localparam integer DEPTH = (1 << AW);
  reg [DW-1:0] mem [0:DEPTH-1];

  // ====== 初始化：默认清零，若有 MEMFILE 则覆盖 ======
  integer i;
  initial begin
    for (i = 0; i < DEPTH; i = i + 1) mem[i] = {DW{1'b0}};
    if (MEMFILE != "") begin
      // 使用 hex 文件（$readmemh）
      $readmemh(MEMFILE, mem);
    end
    $display("[%0t][%m] MEMFILE=%s mem[0]=0x%0h mem[64]=0x%0h",
             $time, MEMFILE, mem[0], mem[64]);
    // $fflush(); // Icarus 可用；如需兼容性可移除
  end

  // ====== 三种延迟模式 ======
  generate
    // --- LAT == 0：无地址寄存、无数据流水，注册输出（同拍可见模拟，便于行为仿真） ---
    if (LAT == 0) begin : g_lat0
      always @(posedge clk) begin
        if (rst) begin
          data      <= #TCQ {DW{1'b0}};
          out_valid <= #TCQ 1'b0;
        end else begin
          // 同拍读取并注册（行为仿真：用 #TCQ 确保“沿后”更新）
          data      <= #TCQ mem[addr];
          out_valid <= #TCQ req;
        end
      end
    end
    // --- LAT == 1：1 拍地址寄存 + 1 拍数据输出 ---
    else if (LAT == 1) begin : g_lat1
      reg [AW-1:0] a1 = {AW{1'b0}};
      reg          v1 = 1'b0;
      always @(posedge clk) begin
        if (rst) begin
          a1        <= #TCQ {AW{1'b0}};
          v1        <= #TCQ 1'b0;
          data      <= #TCQ {DW{1'b0}};
          out_valid <= #TCQ 1'b0;
        end else begin
          // 接收请求与地址
          if (req) a1 <= #TCQ addr;
          v1        <= #TCQ req;
          // 读取上拍地址
          data      <= #TCQ mem[a1];
          out_valid <= #TCQ v1;
        end
      end
    end
    // --- LAT >= 2：多拍地址流水 + 末级读出 ---
    else begin : g_latN
      integer k;
      reg [AW-1:0] a_pipe [0:LAT-1];
      reg [LAT-1:0] vld_sr;

      // 复位初始化
      initial begin
        for (k = 0; k < LAT; k = k + 1) a_pipe[k] = {AW{1'b0}};
        vld_sr = {LAT{1'b0}};
      end

      always @(posedge clk) begin
        if (rst) begin
          for (k = 0; k < LAT; k = k + 1) a_pipe[k] <= #TCQ {AW{1'b0}};
          vld_sr   <= #TCQ {LAT{1'b0}};
          data     <= #TCQ {DW{1'b0}};
          out_valid<= #TCQ 1'b0;
        end else begin
          // 队首在 req=1 时加载新地址，否则保持
          if (req) a_pipe[0] <= #TCQ addr;
          // 地址流水
          for (k = 1; k < LAT; k = k + 1)
            a_pipe[k] <= #TCQ a_pipe[k-1];
          // 有效位移位
          vld_sr   <= #TCQ {vld_sr[LAT-2:0], req};
          // 用末级地址读
          data      <= #TCQ mem[a_pipe[LAT-1]];
          out_valid <= #TCQ vld_sr[LAT-1];
        end
      end
    end
  endgenerate

endmodule