`timescale 1ns/1ps
// dsp_fma_stub.v — 行为级 FMA：P = A*B + C（默认 LAT=1，带同步复位）

module dsp_fma_stub #(
  parameter integer TCQ = 1,
  parameter integer W   = 16,
  parameter integer LAT = 1
)(
  input  wire                   clk,
  input  wire                   rst,          // 同步复位
  input  wire signed [W-1:0]    a,
  input  wire signed [W-1:0]    b,
  input  wire signed [2*W-1:0]  c,
  input  wire                   req,          // 本拍接受新操作
  output reg  signed [2*W-1:0]  p             // 仅在有新结果成熟的拍更新
);

generate
  // -------- LAT = 0：同拍更新（req=1 才更新），否则保持 --------
  if (LAT == 0) begin : g_lat0
    always @(posedge clk) begin
      if (rst) begin
        p <= #TCQ {2*W{1'b0}};
      end else if (req) begin
        p <= #TCQ $signed(a) * $signed(b) + $signed(c);
      end
      // else: 保持 p
    end
  end
  // -------- LAT = 1：单级流水 --------
  else if (LAT == 1) begin : g_lat1
    reg signed [W-1:0]    ar = {W{1'b0}};
    reg signed [W-1:0]    br = {W{1'b0}};
    reg signed [2*W-1:0]  cr = {2*W{1'b0}};
    reg vld = 1'b0;

    always @(posedge clk) begin
      if (rst) begin
        ar  <= #TCQ {W{1'b0}};
        br  <= #TCQ {W{1'b0}};
        cr  <= #TCQ {2*W{1'b0}};
        vld <= #TCQ 1'b0;
        p   <= #TCQ {2*W{1'b0}};
      end else begin
        if (req) begin
          ar <= #TCQ a; br <= #TCQ b; cr <= #TCQ c;   // 本拍锁存
        end
        vld <= #TCQ req;                    // 下一拍有效
        if (vld) begin
          p <= #TCQ $signed(ar) * $signed(br) + $signed(cr);  // 下一拍输出
        end
      end
    end
  end
  // -------- LAT >= 2：多级流水（移位寄存器） --------
  else begin : g_lat_ge2
    reg signed [W-1:0]    ar [0:LAT-1];
    reg signed [W-1:0]    br [0:LAT-1];
    reg signed [2*W-1:0]  cr [0:LAT-1];
    reg [LAT-1:0]         vld;

    integer i;
    always @(posedge clk) begin
      if (rst) begin
        for (i = 0; i < LAT; i = i + 1) begin
          ar[i] <= #TCQ {W{1'b0}};
          br[i] <= #TCQ {W{1'b0}};
          cr[i] <= #TCQ {2*W{1'b0}};
        end
        vld <= #TCQ {LAT{1'b0}};
        p   <= #TCQ {2*W{1'b0}};
      end else begin
        // 级间移位
        for (i = LAT-1; i > 0; i = i - 1) begin
          ar[i] <= #TCQ ar[i-1];
          br[i] <= #TCQ br[i-1];
          cr[i] <= #TCQ cr[i-1];
        end
        // 入口采样
        if (req) begin
          ar[0] <= #TCQ a;
          br[0] <= #TCQ b;
          cr[0] <= #TCQ c;
        end
        // 有效位移位（不会出现负下标）
        vld <= #TCQ {vld[LAT-2:0], req};

        // 末级有效时更新 p；否则保持
        if (vld[LAT-1]) begin
          p <= #TCQ $signed(ar[LAT-1]) * $signed(br[LAT-1]) + $signed(cr[LAT-1]);
        end
      end
    end
  end
endgenerate

endmodule