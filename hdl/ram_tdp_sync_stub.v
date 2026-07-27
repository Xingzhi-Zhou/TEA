`timescale 1ns/1ps

module ram_tdp_sync_stub #(
  parameter integer TCQ     = 1,
  parameter integer AW      = 8,
  parameter integer DW      = 16,
  parameter integer LAT     = 0
)(
  input  wire               clk,
  input  wire               rst,

  // Port A
  input  wire [AW-1:0]      a_addr,
  input  wire               a_req,
  input  wire               a_wr_en,
  input  wire [DW-1:0]      a_din,
  output reg  [DW-1:0]      a_dout,
  output reg                a_valid,

  // Port B
  input  wire [AW-1:0]      b_addr,
  input  wire               b_req,
  input  wire               b_wr_en,
  input  wire [DW-1:0]      b_din,
  output reg  [DW-1:0]      b_dout,
  output reg                b_valid
);

  localparam integer DEPTH = (1 << AW);
  reg [DW-1:0] mem [0:DEPTH-1];

  // ====== 初始化：默认清零 ======
  integer i;
  initial begin
    for (i = 0; i < DEPTH; i = i + 1) mem[i] = {DW{1'b0}};
  end

  // ====== 两种延迟模式 ======
  generate
    if (LAT == 0) begin : lat0_block
      always @(posedge clk) begin
        if (rst) begin
          a_dout  <= #TCQ {DW{1'b0}};
          b_dout  <= #TCQ {DW{1'b0}};
          a_valid <= #TCQ 1'b0;
          b_valid <= #TCQ 1'b0;
        end 
        else begin
          if (a_req) begin
            a_valid <= #TCQ a_req;
            if (a_wr_en) begin
              mem[a_addr] <= #TCQ a_din;
            end else begin
              a_dout <= #TCQ mem[a_addr];
            end
          end

          if (b_req) begin
            b_valid <= #TCQ b_req;
            if (b_wr_en) begin
              mem[b_addr] <= #TCQ b_din;
            end else begin
              b_dout <= #TCQ mem[b_addr];
            end
          end
        end
      end
    end

    if (LAT == 1) begin : lat1_block
      reg [AW-1:0]  a_addr_t;
      reg           a_req_t;
      reg           a_wr_en_t;
      reg [DW-1:0]  a_din_t;

      reg [AW-1:0]  b_addr_t;
      reg           b_req_t;
      reg           b_wr_en_t;
      reg [DW-1:0]  b_din_t;

      always @(posedge clk) begin
        if (rst) begin
          a_dout  <= #TCQ {DW{1'b0}};
          b_dout  <= #TCQ {DW{1'b0}};
          a_valid <= #TCQ 1'b0;
          b_valid <= #TCQ 1'b0;

          a_addr_t  <= #TCQ {AW{1'b0}};
          a_din_t   <= #TCQ {DW{1'b0}};
          a_req_t   <= #TCQ 1'b0;
          a_wr_en_t <= #TCQ 1'b0;

          b_addr_t  <= #TCQ {AW{1'b0}};
          b_din_t   <= #TCQ {DW{1'b0}};
          b_req_t   <= #TCQ 1'b0;
          b_wr_en_t <= #TCQ 1'b0;
        end 
        else begin
          a_addr_t  <= #TCQ a_addr;
          a_din_t   <= #TCQ a_din;
          a_req_t   <= #TCQ a_req;
          a_wr_en_t <= #TCQ a_wr_en;

          b_addr_t  <= #TCQ b_addr;
          b_din_t   <= #TCQ b_din;
          b_req_t   <= #TCQ b_req;
          b_wr_en_t <= #TCQ b_wr_en;

          if (a_req_t) begin
            a_valid <= #TCQ a_req_t;
            if (a_wr_en_t) begin
              mem[a_addr_t] <= #TCQ a_din_t;
            end else begin
              a_dout <= #TCQ mem[a_addr_t];
            end
          end

          if (b_req_t) begin
            b_valid <= #TCQ b_req_t;
            if (b_wr_en_t) begin
              mem[b_addr_t] <= #TCQ b_din_t;
            end else begin
              b_dout <= #TCQ mem[b_addr_t];
            end
          end
        end
      end
    end
  endgenerate
  
endmodule