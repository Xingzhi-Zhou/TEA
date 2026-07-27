`timescale 1ns/1ps

module fifo_stub #(
  parameter integer TCQ     = 1,
  parameter integer AW      = 8,
  parameter integer DW      = 16
)(
  input  wire               clk,
  input  wire               rst,

  // 写接口
  input  wire               wr_en,
  input  wire [DW-1:0]      din,
  output wire               full,

  // 读接口
  input  wire               rd_en,
  output reg  [DW-1:0]      dout,
  output reg                valid,
  output wire               empty
);

  localparam integer DEPTH = (1 << AW);
  reg [DW-1:0] mem [0:DEPTH-1];
  reg [AW:0] wr_ptr;  // 写指针，多1位用于判断满
  reg [AW:0] rd_ptr;  // 读指针，多1位用于判断空

  // ====== 初始化：默认清零 ======
  integer i;
  initial begin
    for (i = 0; i < DEPTH; i = i + 1) mem[i] = {DW{1'b0}};
  end

  // FIFO 状态信号
  assign full  = (wr_ptr[AW] != rd_ptr[AW]) && (wr_ptr[AW-1:0] == rd_ptr[AW-1:0]);
  assign empty = (wr_ptr == rd_ptr);

  // 写操作
  always @(posedge clk) begin
    if (rst) begin
      wr_ptr <= #TCQ {(AW+1){1'b0}};
    end 
    else begin
      if (wr_en && !full) begin
        mem[wr_ptr[AW-1:0]] <= #TCQ din;
        wr_ptr <= #TCQ wr_ptr + 1'b1;
      end
    end
  end

  // 读操作
  always @(posedge clk) begin
    if (rst) begin
      rd_ptr <= #TCQ {(AW+1){1'b0}};
      dout   <= #TCQ {DW{1'b0}};
      valid  <= #TCQ 1'b0;
    end 
    else begin
      valid <= #TCQ rd_en && !empty;
      if (rd_en && !empty) begin
        dout   <= #TCQ mem[rd_ptr[AW-1:0]];
        rd_ptr <= #TCQ rd_ptr + 1'b1;
      end
    end
  end

endmodule