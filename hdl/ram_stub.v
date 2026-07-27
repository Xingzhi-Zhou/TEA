`timescale 1ns/1ps

module ram_stub #(
  parameter integer TCQ     = 1,
  parameter integer AW      = 8,
  parameter integer DW      = 16
)(
  input  wire               clk,
  input  wire               rst,

  input  wire [AW-1:0]      w_addr,
  input  wire               w_en,
  input  wire [DW-1:0]      din,

  input  wire [AW-1:0]      r_addr,
  input  wire               r_en,
  output reg  [DW-1:0]      dout,
  output reg                valid
);

  localparam integer DEPTH = (1 << AW);
  reg [DW-1:0] mem [0:DEPTH-1];

  // ====== 初始化：默认清零 ======
  integer i;
  initial begin
    for (i = 0; i < DEPTH; i = i + 1) mem[i] = {DW{1'b0}};
  end

  always @(posedge clk) begin
    if (rst) begin
      dout  <= #TCQ {DW{1'b0}};
      valid <= #TCQ 1'b0;
    end 
    else begin
      if (w_en) begin
        mem[w_addr] <= #TCQ din;
      end

      valid <= #TCQ r_en;
      if (r_en) begin
        if (w_en && (w_addr == r_addr)) begin
          dout <= #TCQ din;  // 读写同地址，读到新写入的数据
        end else begin
          dout <= #TCQ mem[r_addr];
        end
      end 
    end
  end

endmodule