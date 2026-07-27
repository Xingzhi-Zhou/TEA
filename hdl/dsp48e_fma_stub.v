`timescale 1ns/1ps
// dsp48e_fma_stub.v — 行为级 FMA：P = A*B + C（默认 LAT=1，带同步复位）
//  Q6.10/Q1.15 等定点数据进来，不足位宽时要做好符号扩展：综合器会按 signed 自动扩，但上游连线建议显式拼接高位符号位，避免隐式截断。
// 送 a 时：{{(27-16){a16[15]}}, a16}
// 送 b 时：{{(18-16){b16[15]}}, b16}
// c/p 统一 48 位，行为模型内部按 48 位相加。

// 只要按上面改端口位宽，你原有的行为模型/流水（LAT、TCQ）可以保持不变。

module dsp48e_fma_stub #(
  parameter integer TCQ = 1,
  parameter integer LAT = 1,
  parameter integer WA  = 27,  // A 端口位宽（默认 27）
  parameter integer WB  = 18,  // B 端口位宽（默认 18）
  parameter integer WP  = 48   // P/C 位宽（默认 48）
)(
  input  wire                   clk,
  input  wire                   rst,              // 同步复位
  input  wire signed [WA-1:0]   a,                // A: 27-bit
  input  wire signed [WB-1:0]   b,                // B: 18-bit
  input  wire signed [WP-1:0]   c,                // C: 48-bit
  input  wire                   req,              // 本拍接受新操作
  output reg  signed [WP-1:0]   p,                // P: 48-bit
  output reg                    valid            // 输出结果有效
);

generate
  // -------- LAT = 0：同拍更新（req=1 才更新），否则保持 --------
  if (LAT == 0) begin : g_lat0
    always @(posedge clk) begin
      if (rst) begin
        p     <= #TCQ {WP{1'b0}};
        valid <= #TCQ 1'b0;
      end else if (req) begin
        p     <= #TCQ $signed(a) * $signed(b) + $signed(c);
        valid <= #TCQ 1'b1;
      end
      // else: 保持 p
    end
  end
  // -------- LAT = 1：单级流水 --------
  else if (LAT == 1) begin : g_lat1
    reg signed [WA-1:0]     ar = {WA{1'b0}};
    reg signed [WB-1:0]     br = {WB{1'b0}};
    reg signed [WP-1:0]     cr = {WP{1'b0}};
    reg vld = 1'b0;

    always @(posedge clk) begin
      if (rst) begin
        ar  <= #TCQ {WA{1'b0}};
        br  <= #TCQ {WB{1'b0}};
        cr  <= #TCQ {WP{1'b0}};
        vld <= #TCQ 1'b0;
        p   <= #TCQ {WP{1'b0}};
        valid <= #TCQ 1'b0;
      end else begin
        if (req) begin
          ar <= #TCQ a; br <= #TCQ b; cr <= #TCQ c;   // 本拍锁存
        end
        vld <= #TCQ req;                    // 下一拍有效
        if (vld) begin
          p <= #TCQ $signed(ar) * $signed(br) + $signed(cr);  // 下一拍输出
          valid <= #TCQ 1'b1;
        end else begin
          valid <= #TCQ 1'b0;
        end
      end
    end
  end
  // -------- LAT >= 2：多级流水（移位寄存器） --------
  else begin : g_lat_ge2
    reg signed [WA-1:0]    ar [0:LAT-1];
    reg signed [WB-1:0]    br [0:LAT-1];
    reg signed [WP-1:0]  cr [0:LAT-1];
    reg [LAT-1:0]         vld;

    integer i;
    always @(posedge clk) begin
      if (rst) begin
        for (i = 0; i < LAT; i = i + 1) begin
          ar[i] <= #TCQ {WA{1'b0}};
          br[i] <= #TCQ {WB{1'b0}};
          cr[i] <= #TCQ {WP{1'b0}};
        end
        vld <= #TCQ {LAT{1'b0}};
        p   <= #TCQ {WP{1'b0}};
        valid <= #TCQ 1'b0;
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
          valid <= #TCQ 1'b1;
        end else begin
          valid <= #TCQ 1'b0;
        end
      end
    end
  end
endgenerate

endmodule