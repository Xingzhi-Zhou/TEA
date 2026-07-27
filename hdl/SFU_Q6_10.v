`timescale 1ns/1ps

// Unified Q6.10 Taylor-reducible SFU.
//
// - One configuration word is read for every accepted input.
// - One normalization DSP is shared by all functions.
// - A degree-six Horner chain is fully spatially unrolled (six DSPs).
// - Lower-order polynomials are represented by leading zero coefficients.
// - A fixed opcode stream has II=1.  An opcode change stalls until the old
//   stream drains, which keeps the control path small and deterministic.
//
// Opcodes:
//   2 EXP, 3 reciprocal, 4 reciprocal sqrt, 5 sigmoid,
//   6 SiLU, 7 GELU, 8 tanh, 9 Mish.

module SFU_Q6_10 #(
  parameter integer TCQ        = 1,
  parameter integer DSP_LAT    = 1,
  parameter integer CONFIG_AW   = 7,
  parameter integer CONFIG_DW   = 144,
  parameter         CONFIG_FILE = ""
)(
  input  wire                 clk,
  input  wire                 rst,
  input  wire                 in_valid,
  output wire                 in_ready,
  input  wire [3:0]           sfu_op,
  input  wire signed [15:0]   a_in,
  output reg  signed [15:0]   c_out,
  output reg                  c_valid
);

  localparam [3:0] OP_EXP = 4'h2;
  localparam [3:0] OP_RCP = 4'h3;
  localparam [3:0] OP_RSQRT = 4'h4;
  localparam [3:0] OP_SIGMOID = 4'h5;
  localparam [3:0] OP_SILU    = 4'h6;
  localparam [3:0] OP_GELU    = 4'h7;
  localparam [3:0] OP_TANH    = 4'h8;
  localparam [3:0] OP_MISH    = 4'h9;
  localparam signed [15:0] ONE_Q10   = 16'sd1024;
  localparam signed [15:0] LOG2E_Q10 = 16'sd1477;
  localparam signed [15:0] LN2_Q10   = 16'sd710;

  function signed [15:0] coeff_at;
    input [143:0] word;
    input integer index;
    begin
      coeff_at = $signed(word[32 + index*16 +: 16]);
    end
  endfunction

  function [4:0] leading_one;
    input [15:0] value;
    integer bit_index;
    reg found;
    begin
      leading_one = 0;
      found = 0;
      for (bit_index = 15; bit_index >= 0; bit_index = bit_index - 1) begin
        if (!found && value[bit_index]) begin
          leading_one = bit_index[4:0];
          found = 1;
        end
      end
    end
  endfunction

  function signed [16:0] shift_to_mantissa;
    input signed [15:0] value;
    input signed [6:0] right_shift;
    reg signed [31:0] wide;
    begin
      wide = value;
      if (right_shift >= 0)
        shift_to_mantissa = wide >>> right_shift;
      else
        shift_to_mantissa = wide <<< (-right_shift);
    end
  endfunction

  // Convert a Q12.20 DSP result to Q6.10 using round-to-nearest, then
  // saturate.  The fitted coefficients and software reference are Q6.10.
  function signed [15:0] q10_from_dsp;
    input signed [47:0] value;
    reg signed [48:0] magnitude;
    reg signed [48:0] rounded;
    begin
      if (value >= 0) begin
        rounded = ($signed(value) + 49'sd512) >>> 10;
      end else begin
        magnitude = -$signed(value);
        rounded = -((magnitude + 49'sd512) >>> 10);
      end
      if (rounded > 49'sd32767)
        q10_from_dsp = 16'sh7fff;
      else if (rounded < -49'sd32768)
        q10_from_dsp = 16'sh8000;
      else
        q10_from_dsp = rounded[15:0];
    end
  endfunction

  function signed [6:0] q10_to_integer;
    input signed [15:0] value;
    reg signed [16:0] magnitude;
    reg signed [16:0] rounded;
    begin
      if (value >= 0)
        rounded = ($signed(value) + 17'sd512) >>> 10;
      else begin
        magnitude = -$signed(value);
        rounded = -((magnitude + 17'sd512) >>> 10);
      end
      q10_to_integer = rounded[6:0];
    end
  endfunction

  function signed [15:0] scale_pow2_sat;
    input signed [15:0] value;
    input signed [6:0] exponent;
    reg signed [63:0] wide;
    reg signed [63:0] magnitude;
    reg signed [63:0] rounded;
    integer shift;
    begin
      wide = value;
      if (exponent >= 0) begin
        if (exponent > 31)
          rounded = value[15] ? -64'sd32768 : 64'sd32767;
        else
          rounded = wide <<< exponent;
      end else begin
        shift = -exponent;
        if (shift > 31)
          rounded = 0;
        else if (value >= 0)
          rounded = (wide + (64'sd1 <<< (shift-1))) >>> shift;
        else begin
          magnitude = -wide;
          rounded = -((magnitude + (64'sd1 <<< (shift-1))) >>> shift);
        end
      end
      if (rounded > 64'sd32767)
        scale_pow2_sat = 16'sh7fff;
      else if (rounded < -64'sd32768)
        scale_pow2_sat = 16'sh8000;
      else
        scale_pow2_sat = rounded[15:0];
    end
  endfunction

  wire supported_op =
      (sfu_op == OP_EXP)     || (sfu_op == OP_RCP)  ||
      (sfu_op == OP_RSQRT)   || (sfu_op == OP_SIGMOID) ||
      (sfu_op == OP_SILU)    || (sfu_op == OP_GELU) ||
      (sfu_op == OP_TANH)    || (sfu_op == OP_MISH);

  // -----------------------------------------------------------------------
  // Unified configuration ROM and controller. Descriptor and packed
  // boundaries are read only on an opcode switch; parameter records use the
  // same single port during steady-state execution.
  // -----------------------------------------------------------------------
  localparam integer MAX_BOUNDARIES = 14;
  localparam [2:0] CFG_IDLE       = 3'd0;
  localparam [2:0] CFG_DESC_REQ   = 3'd1;
  localparam [2:0] CFG_DESC_WAIT  = 3'd2;
  localparam [2:0] CFG_BOUND_REQ  = 3'd3;
  localparam [2:0] CFG_BOUND_WAIT = 3'd4;

  reg [3:0] active_op;
  reg [3:0] pending_op;
  reg configured;
  reg [2:0] cfg_state;
  reg [7:0] inflight;
  reg [6:0] active_param_base;
  reg [4:0] active_segment_count;
  reg [1:0] boundary_word_count;
  reg [1:0] boundary_word_index;
  reg signed [15:0] boundary_regs [0:MAX_BOUNDARIES-1];

  wire loading = (cfg_state != CFG_IDLE);
  wire config_request =
      in_valid && supported_op && (cfg_state == CFG_IDLE) &&
      (inflight == 0) &&
      (!configured || (sfu_op != active_op));

  assign in_ready =
      supported_op && configured && !loading && (sfu_op == active_op);
  wire accept = in_valid && in_ready;

  // Shared segment classifier.
  reg [4:0] segment_index;
  integer class_i;
  always @* begin
    segment_index = 0;
    for (class_i = 0; class_i < MAX_BOUNDARIES; class_i = class_i + 1) begin
      if ((class_i < (active_segment_count - 1'b1)) &&
          ($signed(a_in) >= $signed(boundary_regs[class_i])))
        segment_index = class_i + 1;
    end
  end

  wire [CONFIG_AW-1:0] runtime_param_addr =
      active_param_base + segment_index;
  reg [CONFIG_AW-1:0] config_rom_addr;
  reg config_rom_req;
  always @* begin
    config_rom_addr = runtime_param_addr;
    config_rom_req = accept;
    case (cfg_state)
      CFG_DESC_REQ: begin
        config_rom_addr = pending_op - 4'd2;
        config_rom_req = 1'b1;
      end
      CFG_BOUND_REQ: begin
        config_rom_addr =
            7'd8 + ((pending_op - 4'd2) << 1) + boundary_word_index;
        config_rom_req = 1'b1;
      end
      default: begin end
    endcase
  end

  wire [CONFIG_DW-1:0] config_rom_data;
  wire config_rom_valid;
  rom_sync_stub #(
    .TCQ(TCQ), .AW(CONFIG_AW), .DW(CONFIG_DW), .LAT(0),
    .MEMFILE(CONFIG_FILE)
  ) u_config_rom (
    .clk(clk), .rst(rst), .addr(config_rom_addr), .req(config_rom_req),
    .data(config_rom_data), .out_valid(config_rom_valid)
  );

  reg rom_runtime_tag;
  always @(posedge clk) begin
    if (rst)
      rom_runtime_tag <= #TCQ 0;
    else
      rom_runtime_tag <= #TCQ accept;
  end
  wire [CONFIG_DW-1:0] param_data = config_rom_data;
  wire param_valid = config_rom_valid && rom_runtime_tag;

  integer cfg_i;
  always @(posedge clk) begin
    if (rst) begin
      active_op <= #TCQ 0;
      pending_op <= #TCQ 0;
      configured <= #TCQ 0;
      cfg_state <= #TCQ CFG_IDLE;
      active_param_base <= #TCQ 0;
      active_segment_count <= #TCQ 0;
      boundary_word_count <= #TCQ 0;
      boundary_word_index <= #TCQ 0;
      for (cfg_i = 0; cfg_i < MAX_BOUNDARIES; cfg_i = cfg_i + 1)
        boundary_regs[cfg_i] <= #TCQ 0;
    end else begin
      case (cfg_state)
        CFG_IDLE: begin
          if (config_request) begin
            pending_op <= #TCQ sfu_op;
            configured <= #TCQ 0;
            cfg_state <= #TCQ CFG_DESC_REQ;
            for (cfg_i = 0; cfg_i < MAX_BOUNDARIES; cfg_i = cfg_i + 1)
              boundary_regs[cfg_i] <= #TCQ 0;
          end
        end
        CFG_DESC_REQ:
          cfg_state <= #TCQ CFG_DESC_WAIT;
        CFG_DESC_WAIT: begin
          if (config_rom_valid) begin
            active_param_base <= #TCQ config_rom_data[6:0];
            active_segment_count <= #TCQ config_rom_data[11:7];
            boundary_word_count <= #TCQ config_rom_data[13:12];
            boundary_word_index <= #TCQ 0;
            if (config_rom_data[13:12] == 0) begin
              active_op <= #TCQ pending_op;
              configured <= #TCQ 1;
              cfg_state <= #TCQ CFG_IDLE;
            end else begin
              cfg_state <= #TCQ CFG_BOUND_REQ;
            end
          end
        end
        CFG_BOUND_REQ:
          cfg_state <= #TCQ CFG_BOUND_WAIT;
        CFG_BOUND_WAIT: begin
          if (config_rom_valid) begin
            for (cfg_i = 0; cfg_i < 9; cfg_i = cfg_i + 1) begin
              if ((boundary_word_index * 9 + cfg_i) < MAX_BOUNDARIES)
                boundary_regs[boundary_word_index * 9 + cfg_i] <= #TCQ
                    $signed(config_rom_data[cfg_i*16 +: 16]);
            end
            if ((boundary_word_index + 1'b1) >= boundary_word_count) begin
              active_op <= #TCQ pending_op;
              configured <= #TCQ 1;
              cfg_state <= #TCQ CFG_IDLE;
            end else begin
              boundary_word_index <= #TCQ boundary_word_index + 1'b1;
              cfg_state <= #TCQ CFG_BOUND_REQ;
            end
          end
        end
        default: begin
          configured <= #TCQ 0;
          cfg_state <= #TCQ CFG_IDLE;
        end
      endcase
    end
  end

  reg signed [15:0] rom_x;
  reg [3:0] rom_op;
  always @(posedge clk) begin
    if (rst) begin
      rom_x <= #TCQ 16'sd0;
      rom_op <= #TCQ 4'd0;
    end else if (accept) begin
      rom_x <= #TCQ a_in;
      rom_op <= #TCQ sfu_op;
    end
  end

  // -----------------------------------------------------------------------
  // Shared normalization DSP
  //
  // affine mode: (x * 1.0 + c) in Q12.20
  // EXP mode:    x * log2(e), followed by r=x-round(x/ln2)*ln2
  // -----------------------------------------------------------------------
  wire signed [15:0] norm_c = $signed(param_data[15:0]);
  wire signed [15:0] norm_inv_si = $signed(param_data[31:16]);
  wire signed [6:0] input_exp2 =
      $signed({2'b00, leading_one(rom_x[15:0])}) - 7'sd10;
  wire signed [6:0] rcp_k_dyn = input_exp2 + 7'sd1;
  wire signed [6:0] rsqrt_k_dyn = input_exp2 >>> 1;
  wire signed [16:0] rcp_mantissa =
      shift_to_mantissa(rom_x, rcp_k_dyn);
  wire signed [16:0] rsqrt_mantissa =
      shift_to_mantissa(rom_x, rsqrt_k_dyn <<< 1);
  wire signed [16:0] norm_source =
      (rom_op == OP_RCP) ? rcp_mantissa :
      (rom_op == OP_RSQRT) ? rsqrt_mantissa :
      {{1{rom_x[15]}}, rom_x};
  wire signed [17:0] norm_sum = $signed(norm_source) + $signed(norm_c);
  wire signed [26:0] norm_a = {{9{norm_sum[17]}}, norm_sum};
  wire signed [17:0] norm_b =
      (rom_op == OP_EXP) ? {{2{LOG2E_Q10[15]}}, LOG2E_Q10}
                         : {{2{norm_inv_si[15]}}, norm_inv_si};
  wire signed [47:0] norm_c_dsp = 48'sd0;
  wire signed [47:0] norm_p;
  wire norm_valid;

  dsp48e_fma_stub #(
    .TCQ(TCQ), .LAT(DSP_LAT), .WA(27), .WB(18), .WP(48)
  ) u_norm_dsp (
    .clk(clk), .rst(rst),
    .a(norm_a), .b(norm_b), .c(norm_c_dsp), .req(param_valid),
    .p(norm_p), .valid(norm_valid)
  );

  // Two metadata registers match the timing of dsp48e_fma_stub LAT=1.
  reg signed [15:0] norm_x_m0, norm_x_m1;
  reg [3:0] norm_op_m0, norm_op_m1;
  reg signed [6:0] norm_k_m0, norm_k_m1;
  reg [143:0] norm_param_m0, norm_param_m1;
  always @(posedge clk) begin
    if (rst) begin
      norm_x_m0 <= #TCQ 0; norm_x_m1 <= #TCQ 0;
      norm_op_m0 <= #TCQ 0; norm_op_m1 <= #TCQ 0;
      norm_k_m0 <= #TCQ 0; norm_k_m1 <= #TCQ 0;
      norm_param_m0 <= #TCQ 0; norm_param_m1 <= #TCQ 0;
    end else begin
      if (param_valid) begin
        norm_x_m0 <= #TCQ rom_x;
        norm_op_m0 <= #TCQ rom_op;
        norm_k_m0 <= #TCQ
            (rom_op == OP_RCP) ? rcp_k_dyn :
            (rom_op == OP_RSQRT) ? rsqrt_k_dyn : 7'sd0;
        norm_param_m0 <= #TCQ param_data;
      end
      norm_x_m1 <= #TCQ norm_x_m0;
      norm_op_m1 <= #TCQ norm_op_m0;
      norm_k_m1 <= #TCQ norm_k_m0;
      norm_param_m1 <= #TCQ norm_param_m0;
    end
  end

  wire signed [15:0] exp_log2_q10 = q10_from_dsp(norm_p);
  wire signed [6:0] exp_n = q10_to_integer(exp_log2_q10);
  wire signed [23:0] exp_n_ln2 = $signed(exp_n) * $signed(LN2_Q10);
  wire signed [16:0] exp_r_wide =
      $signed(norm_x_m1) - $signed(exp_n_ln2);
  wire signed [15:0] normalized_r =
      (norm_op_m1 == OP_EXP) ? exp_r_wide[15:0]
                             : q10_from_dsp(norm_p);
  wire signed [6:0] normalized_n =
      (norm_op_m1 == OP_EXP) ? exp_n :
      ((norm_op_m1 == OP_RCP) || (norm_op_m1 == OP_RSQRT))
          ? -norm_k_m1 : 7'sd0;

  // -----------------------------------------------------------------------
  // Fully unrolled six-FMA Horner pipeline
  // -----------------------------------------------------------------------
  wire signed [47:0] h_p [0:5];
  wire h_valid [0:5];

  reg signed [15:0] h_r_m0 [0:5];
  reg signed [15:0] h_r_m1 [0:5];
  reg signed [6:0] h_n_m0 [0:5];
  reg signed [6:0] h_n_m1 [0:5];
  reg [3:0] h_op_m0 [0:5];
  reg [3:0] h_op_m1 [0:5];
  reg [143:0] h_param_m0 [0:5];
  reg [143:0] h_param_m1 [0:5];

  genvar stage;
  generate
    for (stage = 0; stage < 6; stage = stage + 1) begin : g_horner
      wire stage_req = (stage == 0) ? norm_valid : h_valid[stage-1];
      wire signed [15:0] stage_r =
          (stage == 0) ? normalized_r : h_r_m1[stage-1];
      wire signed [6:0] stage_n =
          (stage == 0) ? normalized_n : h_n_m1[stage-1];
      wire [3:0] stage_op =
          (stage == 0) ? norm_op_m1 : h_op_m1[stage-1];
      wire [143:0] stage_param =
          (stage == 0) ? norm_param_m1 : h_param_m1[stage-1];
      wire signed [15:0] stage_acc =
          (stage == 0) ? coeff_at(stage_param, 0)
                       : q10_from_dsp(h_p[stage-1]);
      wire signed [15:0] stage_coeff = coeff_at(stage_param, stage+1);
      wire signed [26:0] stage_a = {{11{stage_acc[15]}}, stage_acc};
      wire signed [17:0] stage_b = {{2{stage_r[15]}}, stage_r};
      wire signed [47:0] stage_c =
          {{32{stage_coeff[15]}}, stage_coeff} <<< 10;

      dsp48e_fma_stub #(
        .TCQ(TCQ), .LAT(DSP_LAT), .WA(27), .WB(18), .WP(48)
      ) u_horner_dsp (
        .clk(clk), .rst(rst),
        .a(stage_a), .b(stage_b), .c(stage_c), .req(stage_req),
        .p(h_p[stage]), .valid(h_valid[stage])
      );

      always @(posedge clk) begin
        if (rst) begin
          h_r_m0[stage] <= #TCQ 0; h_r_m1[stage] <= #TCQ 0;
          h_n_m0[stage] <= #TCQ 0; h_n_m1[stage] <= #TCQ 0;
          h_op_m0[stage] <= #TCQ 0; h_op_m1[stage] <= #TCQ 0;
          h_param_m0[stage] <= #TCQ 0; h_param_m1[stage] <= #TCQ 0;
        end else begin
          if (stage_req) begin
            h_r_m0[stage] <= #TCQ stage_r;
            h_n_m0[stage] <= #TCQ stage_n;
            h_op_m0[stage] <= #TCQ stage_op;
            h_param_m0[stage] <= #TCQ stage_param;
          end
          h_r_m1[stage] <= #TCQ h_r_m0[stage];
          h_n_m1[stage] <= #TCQ h_n_m0[stage];
          h_op_m1[stage] <= #TCQ h_op_m0[stage];
          h_param_m1[stage] <= #TCQ h_param_m0[stage];
        end
      end
    end
  endgenerate

  wire signed [15:0] polynomial_q10 = q10_from_dsp(h_p[5]);
  wire signed [15:0] result_q10 =
      ((h_op_m1[5] == OP_EXP) ||
       (h_op_m1[5] == OP_RCP) ||
       (h_op_m1[5] == OP_RSQRT))
          ? scale_pow2_sat(polynomial_q10, h_n_m1[5])
          : polynomial_q10;

  always @(posedge clk) begin
    if (rst) begin
      c_out <= #TCQ 16'sd0;
      c_valid <= #TCQ 1'b0;
    end else begin
      c_valid <= #TCQ h_valid[5];
      if (h_valid[5])
        c_out <= #TCQ result_q10;
    end
  end

  always @(posedge clk) begin
    if (rst) begin
      inflight <= #TCQ 8'd0;
    end else begin
      case ({accept, h_valid[5]})
        2'b10: inflight <= #TCQ inflight + 1'b1;
        2'b01: inflight <= #TCQ inflight - 1'b1;
        default: inflight <= #TCQ inflight;
      endcase
    end
  end

endmodule
