`timescale 1ns/1ps

// Unified IEEE-754 binary16 Taylor-reducible SFU.
// Fixed-opcode streams have II=1. Opcode changes drain the old stream and
// load a descriptor plus packed boundaries from the shared configuration ROM.
module SFU_FP16 #(
  parameter integer TCQ         = 1,
  parameter integer DSP_LAT     = 1,
  parameter integer CONFIG_AW   = 7,
  parameter integer CONFIG_DW   = 144,
  parameter         CONFIG_FILE = ""
)(
  input  wire        clk,
  input  wire        rst,
  input  wire        in_valid,
  output wire        in_ready,
  input  wire [3:0]  sfu_op,
  input  wire [15:0] a_in,
  output reg  [15:0] c_out,
  output reg         c_valid
);

  localparam [3:0] OP_EXP = 4'h2, OP_RCP = 4'h3, OP_RSQRT = 4'h4;
  localparam [3:0] OP_SIGMOID = 4'h5, OP_SILU = 4'h6, OP_GELU = 4'h7;
  localparam [3:0] OP_TANH = 4'h8, OP_MISH = 4'h9;
  localparam [15:0] FP16_QNAN = 16'h7e00;

  function fp16_is_nan;
    input [15:0] value;
    begin
      fp16_is_nan = (value[14:10] == 5'h1f) && (value[9:0] != 0);
    end
  endfunction

  function fp16_is_inf;
    input [15:0] value;
    begin
      fp16_is_inf = (value[14:10] == 5'h1f) && (value[9:0] == 0);
    end
  endfunction

  function [15:0] fp16_order_key;
    input [15:0] value;
    reg [15:0] clean;
    begin
      clean = value;
      if (clean[14:0] == 0)
        clean = 0;
      fp16_order_key = clean[15] ? ~clean : (clean ^ 16'h8000);
    end
  endfunction

  // finite value = significand * 2^scale
  function [10:0] fp16_significand;
    input [15:0] value;
    begin
      fp16_significand =
          (value[14:10] == 0) ? {1'b0, value[9:0]}
                              : {1'b1, value[9:0]};
    end
  endfunction

  function signed [9:0] fp16_scale;
    input [15:0] value;
    begin
      fp16_scale = (value[14:10] == 0)
          ? -10'sd24
          : $signed({1'b0, value[14:10]}) - 10'sd25;
    end
  endfunction

  // RNE conversion of magnitude*2^scale to binary16.
  function [15:0] fp16_from_mag;
    input [55:0] magnitude;
    input signed [9:0] scale;
    input sign_value;
    integer i, msb, unbiased_exp, shift, unit_shift;
    reg found;
    reg [63:0] wide, quotient, remainder, half;
    reg [5:0] exponent_field;
    begin
      if (magnitude == 0) begin
        fp16_from_mag = {sign_value, 15'd0};
      end else begin
        msb = 0;
        found = 0;
        for (i = 55; i >= 0; i = i - 1) begin
          if (!found && magnitude[i]) begin
            msb = i;
            found = 1;
          end
        end
        wide = magnitude;
        unbiased_exp = msb + scale;
        if (unbiased_exp < -14) begin
          unit_shift = scale + 24;
          if (unit_shift >= 0) begin
            quotient = wide << unit_shift;
          end else begin
            shift = -unit_shift;
            if (shift >= 64) begin
              quotient = 0;
            end else begin
              quotient = wide >> shift;
              remainder = wide - (quotient << shift);
              half = 64'd1 << (shift - 1);
              if ((remainder > half) ||
                  ((remainder == half) && quotient[0]))
                quotient = quotient + 1'b1;
            end
          end
          if (quotient >= 1024)
            fp16_from_mag = {sign_value, 5'd1, 10'd0};
          else
            fp16_from_mag = {sign_value, 5'd0, quotient[9:0]};
        end else begin
          shift = msb - 10;
          if (shift > 0) begin
            quotient = wide >> shift;
            remainder = wide - (quotient << shift);
            half = 64'd1 << (shift - 1);
            if ((remainder > half) ||
                ((remainder == half) && quotient[0]))
              quotient = quotient + 1'b1;
          end else begin
            quotient = wide << (-shift);
          end
          if (quotient >= 2048) begin
            quotient = 1024;
            unbiased_exp = unbiased_exp + 1;
          end
          exponent_field = unbiased_exp + 15;
          if (exponent_field >= 31)
            fp16_from_mag = {sign_value, 5'h1f, 10'd0};
          else if (exponent_field <= 0)
            fp16_from_mag = {sign_value, 15'd0};
          else
            fp16_from_mag = {
              sign_value, exponent_field[4:0], quotient[9:0]
            };
        end
      end
    end
  endfunction

  // All finite binary16 values are multiples of 2^-24.
  function signed [63:0] fp16_to_units;
    input [15:0] value;
    reg signed [63:0] magnitude;
    begin
      if (value[14:10] == 0)
        magnitude = value[9:0];
      else
        magnitude = {1'b1, value[9:0]} << (value[14:10] - 1'b1);
      fp16_to_units = value[15] ? -magnitude : magnitude;
    end
  endfunction

  function [15:0] fp16_from_units;
    input signed [63:0] units;
    reg [55:0] magnitude;
    begin
      if (units < 0) begin
        magnitude = -units;
        fp16_from_units = fp16_from_mag(magnitude, -10'sd24, 1'b1);
      end else begin
        magnitude = units;
        fp16_from_units = fp16_from_mag(magnitude, -10'sd24, 1'b0);
      end
    end
  endfunction

  function [15:0] fp16_add;
    input [15:0] lhs;
    input [15:0] rhs;
    reg signed [63:0] sum;
    begin
      if (fp16_is_nan(lhs) || fp16_is_nan(rhs))
        fp16_add = FP16_QNAN;
      else if (fp16_is_inf(lhs) && fp16_is_inf(rhs) &&
               (lhs[15] != rhs[15]))
        fp16_add = FP16_QNAN;
      else if (fp16_is_inf(lhs))
        fp16_add = lhs;
      else if (fp16_is_inf(rhs))
        fp16_add = rhs;
      else begin
        sum = fp16_to_units(lhs) + fp16_to_units(rhs);
        fp16_add = fp16_from_units(sum);
      end
    end
  endfunction

  function [15:0] fp16_scale_pow2;
    input [15:0] value;
    input signed [9:0] exponent;
    reg signed [9:0] target_scale;
    begin
      if (fp16_is_nan(value))
        fp16_scale_pow2 = FP16_QNAN;
      else if (fp16_is_inf(value))
        fp16_scale_pow2 = value;
      else begin
        target_scale = fp16_scale(value) + exponent;
        fp16_scale_pow2 = fp16_from_mag(
            {45'd0, fp16_significand(value)}, target_scale, value[15]);
      end
    end
  endfunction

  function signed [9:0] fp16_floor_log2;
    input [15:0] value;
    integer i, highest;
    reg found;
    begin
      if (value[14:10] != 0) begin
        fp16_floor_log2 =
            $signed({1'b0, value[14:10]}) - 10'sd15;
      end else begin
        highest = 0;
        found = 0;
        for (i = 9; i >= 0; i = i - 1) begin
          if (!found && value[i]) begin
            highest = i;
            found = 1;
          end
        end
        fp16_floor_log2 = highest - 10'sd24;
      end
    end
  endfunction

  // Exact-enough rational division: 40 quotient guard bits plus sticky.
  function [15:0] fp16_div;
    input [15:0] numerator_value;
    input [15:0] denominator_value;
    reg [10:0] numerator_sig, denominator_sig;
    reg [55:0] numerator_wide, quotient, remainder;
    reg signed [9:0] quotient_scale;
    reg result_sign;
    begin
      result_sign = numerator_value[15] ^ denominator_value[15];
      if (fp16_is_nan(numerator_value) || fp16_is_nan(denominator_value) ||
          ((numerator_value[14:0] == 0) &&
           (denominator_value[14:0] == 0)) ||
          (fp16_is_inf(numerator_value) && fp16_is_inf(denominator_value)))
        fp16_div = FP16_QNAN;
      else if (fp16_is_inf(numerator_value) ||
               (denominator_value[14:0] == 0))
        fp16_div = {result_sign, 5'h1f, 10'd0};
      else if ((numerator_value[14:0] == 0) ||
               fp16_is_inf(denominator_value))
        fp16_div = {result_sign, 15'd0};
      else begin
        numerator_sig = fp16_significand(numerator_value);
        denominator_sig = fp16_significand(denominator_value);
        numerator_wide = {45'd0, numerator_sig} << 40;
        quotient = numerator_wide / denominator_sig;
        remainder = numerator_wide - quotient * denominator_sig;
        if (remainder != 0)
          quotient[0] = 1'b1;
        quotient_scale =
            fp16_scale(numerator_value) -
            fp16_scale(denominator_value) - 10'sd40;
        fp16_div = fp16_from_mag(quotient, quotient_scale, result_sign);
      end
    end
  endfunction

  function signed [9:0] round_div_ln2;
    input signed [63:0] numerator;
    reg [63:0] magnitude, quotient, remainder;
    begin
      magnitude = numerator < 0 ? -numerator : numerator;
      quotient = magnitude / 64'd11632640;
      remainder = magnitude - quotient * 64'd11632640;
      if ((remainder > 64'd5816320) ||
          ((remainder == 64'd5816320) && quotient[0]))
        quotient = quotient + 1'b1;
      round_div_ln2 =
          numerator < 0 ? -$signed(quotient[9:0])
                        :  $signed(quotient[9:0]);
    end
  endfunction

  function [15:0] coeff_at;
    input [143:0] word;
    input integer index;
    begin
      coeff_at = word[32 + index*16 +: 16];
    end
  endfunction

  wire supported_op =
      (sfu_op >= OP_EXP) && (sfu_op <= OP_MISH);

  // Compact shared ROM configuration loader.
  localparam integer MAX_BOUNDARIES = 14;
  localparam [2:0] CFG_IDLE=0, CFG_DESC_REQ=1, CFG_DESC_WAIT=2;
  localparam [2:0] CFG_BOUND_REQ=3, CFG_BOUND_WAIT=4;
  reg [3:0] active_op, pending_op;
  reg configured;
  reg [2:0] cfg_state;
  reg [7:0] inflight;
  reg [6:0] active_param_base, active_boundary_base;
  reg [3:0] active_boundary_count;
  reg [1:0] boundary_word_count, boundary_word_index;
  reg [15:0] boundary_regs [0:MAX_BOUNDARIES-1];

  wire config_request =
      in_valid && supported_op && (cfg_state == CFG_IDLE) &&
      (inflight == 0) && (!configured || sfu_op != active_op);
  assign in_ready =
      supported_op && configured && (cfg_state == CFG_IDLE) &&
      (sfu_op == active_op);
  wire accept = in_valid && in_ready;

  reg [4:0] segment_index;
  integer class_i;
  always @* begin
    segment_index = 0;
    for (class_i=0; class_i<MAX_BOUNDARIES; class_i=class_i+1)
      if ((class_i < active_boundary_count) &&
          (fp16_order_key(a_in) >= fp16_order_key(boundary_regs[class_i])))
        segment_index = class_i + 1;
  end

  wire [CONFIG_AW-1:0] runtime_addr =
      active_param_base + segment_index;
  reg [CONFIG_AW-1:0] rom_addr;
  reg rom_req;
  always @* begin
    rom_addr = runtime_addr;
    rom_req = accept;
    if (cfg_state == CFG_DESC_REQ) begin
      rom_addr = pending_op - 4'd2;
      rom_req = 1;
    end else if (cfg_state == CFG_BOUND_REQ) begin
      rom_addr = active_boundary_base + boundary_word_index;
      rom_req = 1;
    end
  end

  wire [CONFIG_DW-1:0] rom_data;
  wire rom_valid;
  rom_sync_stub #(
    .TCQ(TCQ), .AW(CONFIG_AW), .DW(CONFIG_DW), .LAT(0),
    .MEMFILE(CONFIG_FILE)
  ) u_config_rom (
    .clk(clk), .rst(rst), .addr(rom_addr), .req(rom_req),
    .data(rom_data), .out_valid(rom_valid)
  );

  reg runtime_tag;
  always @(posedge clk)
    if (rst) runtime_tag <= #TCQ 0;
    else runtime_tag <= #TCQ accept;
  wire param_valid = rom_valid && runtime_tag;

  integer cfg_i;
  always @(posedge clk) begin
    if (rst) begin
      active_op <= #TCQ 0; pending_op <= #TCQ 0;
      configured <= #TCQ 0; cfg_state <= #TCQ CFG_IDLE;
      active_param_base <= #TCQ 0; active_boundary_base <= #TCQ 0;
      active_boundary_count <= #TCQ 0;
      boundary_word_count <= #TCQ 0; boundary_word_index <= #TCQ 0;
      for (cfg_i=0; cfg_i<MAX_BOUNDARIES; cfg_i=cfg_i+1)
        boundary_regs[cfg_i] <= #TCQ 0;
    end else case (cfg_state)
      CFG_IDLE: if (config_request) begin
        pending_op <= #TCQ sfu_op;
        configured <= #TCQ 0;
        cfg_state <= #TCQ CFG_DESC_REQ;
        for (cfg_i=0; cfg_i<MAX_BOUNDARIES; cfg_i=cfg_i+1)
          boundary_regs[cfg_i] <= #TCQ 0;
      end
      CFG_DESC_REQ: cfg_state <= #TCQ CFG_DESC_WAIT;
      CFG_DESC_WAIT: if (rom_valid) begin
        active_param_base <= #TCQ rom_data[6:0];
        active_boundary_base <= #TCQ rom_data[18:12];
        active_boundary_count <= #TCQ rom_data[22:19];
        boundary_word_count <= #TCQ rom_data[24:23];
        boundary_word_index <= #TCQ 0;
        if (rom_data[24:23] == 0) begin
          active_op <= #TCQ pending_op;
          configured <= #TCQ 1;
          cfg_state <= #TCQ CFG_IDLE;
        end else cfg_state <= #TCQ CFG_BOUND_REQ;
      end
      CFG_BOUND_REQ: cfg_state <= #TCQ CFG_BOUND_WAIT;
      CFG_BOUND_WAIT: if (rom_valid) begin
        for (cfg_i=0; cfg_i<9; cfg_i=cfg_i+1)
          if (((boundary_word_index*9+cfg_i) < active_boundary_count) &&
              ((boundary_word_index*9+cfg_i) < MAX_BOUNDARIES))
            boundary_regs[boundary_word_index*9+cfg_i] <= #TCQ
                rom_data[cfg_i*16 +: 16];
        if ((boundary_word_index + 1'b1) >= boundary_word_count) begin
          active_op <= #TCQ pending_op;
          configured <= #TCQ 1;
          cfg_state <= #TCQ CFG_IDLE;
        end else begin
          boundary_word_index <= #TCQ boundary_word_index + 1'b1;
          cfg_state <= #TCQ CFG_BOUND_REQ;
        end
      end
      default: begin configured <= #TCQ 0; cfg_state <= #TCQ CFG_IDLE; end
    endcase
  end

  reg [15:0] rom_x;
  reg [3:0] rom_op;
  always @(posedge clk) begin
    if (rst) begin rom_x <= #TCQ 0; rom_op <= #TCQ 0; end
    else if (accept) begin rom_x <= #TCQ a_in; rom_op <= #TCQ sfu_op; end
  end

  // Normalization.
  wire [15:0] norm_c = rom_data[15:0];
  wire [15:0] norm_si = rom_data[31:16];
  wire signed [63:0] x_units = fp16_to_units(rom_x);
  wire signed [9:0] exp_n = round_div_ln2(x_units);
  wire signed [63:0] exp_r_units =
      x_units - $signed(exp_n) * 64'sd11632640;
  wire signed [9:0] input_log2 = fp16_floor_log2(rom_x);
  wire signed [9:0] rcp_k = input_log2 + 1'b1;
  wire signed [9:0] rsqrt_k = input_log2 >>> 1;
  wire invalid_positive =
      rom_x[15] || (rom_x[14:0] == 0) ||
      fp16_is_nan(rom_x) || fp16_is_inf(rom_x);
  wire [15:0] rcp_m = fp16_scale_pow2(rom_x, -rcp_k);
  wire [15:0] rsqrt_m =
      fp16_scale_pow2(rom_x, -(rsqrt_k <<< 1));
  wire [15:0] affine_sum = fp16_add(rom_x, norm_c);
  wire [15:0] norm_dividend =
      (rom_op == OP_RCP) ? fp16_add(rcp_m, norm_c) :
      (rom_op == OP_RSQRT) ? fp16_add(rsqrt_m, norm_c) :
      affine_sum;
  // One shared normalization divider. All fixed-opcode samples use this same
  // combinational path; the spatial Horner pipeline remains the II=1 core.
  wire [15:0] norm_div_result = fp16_div(norm_dividend, norm_si);
  wire [15:0] normalized_r =
      (rom_op == OP_EXP) ? fp16_from_units(exp_r_units) :
      (rom_op == OP_RCP) ?
        (invalid_positive ? FP16_QNAN : norm_div_result) :
      (rom_op == OP_RSQRT) ?
        (invalid_positive ? FP16_QNAN : norm_div_result) :
      norm_div_result;
  wire signed [9:0] normalized_n =
      (rom_op == OP_EXP) ? exp_n :
      (rom_op == OP_RCP) ? -rcp_k :
      (rom_op == OP_RSQRT) ? -rsqrt_k : 0;

  // Six spatial DSP significand multipliers with FP16 rounding after the
  // multiply and after the coefficient addition.
  wire signed [47:0] h_product [0:5];
  wire h_valid [0:5];
  wire [15:0] h_value [0:5];
  reg signed [9:0] h_scale_m0[0:5], h_scale_m1[0:5];
  reg h_sign_m0[0:5], h_sign_m1[0:5];
  reg h_zero_m0[0:5], h_zero_m1[0:5];
  reg h_nan_m0[0:5], h_nan_m1[0:5];
  reg h_inf_m0[0:5], h_inf_m1[0:5];
  reg [15:0] h_coeff_m0[0:5], h_coeff_m1[0:5];
  reg [15:0] h_r_m0[0:5], h_r_m1[0:5];
  reg signed [9:0] h_n_m0[0:5], h_n_m1[0:5];
  reg [3:0] h_op_m0[0:5], h_op_m1[0:5];
  reg [143:0] h_param_m0[0:5], h_param_m1[0:5];

  genvar stage;
  generate for (stage=0; stage<6; stage=stage+1) begin : g_horner
    wire stage_req = (stage==0) ? param_valid : h_valid[stage-1];
    wire [15:0] stage_r = (stage==0) ? normalized_r : h_r_m1[stage-1];
    wire signed [9:0] stage_n =
        (stage==0) ? normalized_n : h_n_m1[stage-1];
    wire [3:0] stage_op = (stage==0) ? rom_op : h_op_m1[stage-1];
    wire [143:0] stage_param =
        (stage==0) ? rom_data : h_param_m1[stage-1];
    wire [15:0] stage_acc =
        (stage==0) ? coeff_at(stage_param,0) : h_value[stage-1];
    wire [15:0] stage_coeff = coeff_at(stage_param,stage+1);
    wire [10:0] acc_sig = fp16_significand(stage_acc);
    wire [10:0] r_sig = fp16_significand(stage_r);

    dsp48e_fma_stub #(
      .TCQ(TCQ), .LAT(DSP_LAT), .WA(27), .WB(18), .WP(48)
    ) u_mul (
      .clk(clk), .rst(rst), .a({16'd0,acc_sig}), .b({7'd0,r_sig}),
      .c(48'sd0), .req(stage_req),
      .p(h_product[stage]), .valid(h_valid[stage])
    );

    wire [15:0] rounded_product =
        h_nan_m1[stage] ? FP16_QNAN :
        h_inf_m1[stage] ? {h_sign_m1[stage],5'h1f,10'd0} :
        h_zero_m1[stage] ? {h_sign_m1[stage],15'd0} :
        fp16_from_mag(
          {34'd0,h_product[stage][21:0]},
          h_scale_m1[stage], h_sign_m1[stage]);
    assign h_value[stage] =
        fp16_add(rounded_product,h_coeff_m1[stage]);

    always @(posedge clk) begin
      if (rst) begin
        h_scale_m0[stage]<=#TCQ 0; h_scale_m1[stage]<=#TCQ 0;
        h_sign_m0[stage]<=#TCQ 0; h_sign_m1[stage]<=#TCQ 0;
        h_zero_m0[stage]<=#TCQ 0; h_zero_m1[stage]<=#TCQ 0;
        h_nan_m0[stage]<=#TCQ 0; h_nan_m1[stage]<=#TCQ 0;
        h_inf_m0[stage]<=#TCQ 0; h_inf_m1[stage]<=#TCQ 0;
        h_coeff_m0[stage]<=#TCQ 0; h_coeff_m1[stage]<=#TCQ 0;
        h_r_m0[stage]<=#TCQ 0; h_r_m1[stage]<=#TCQ 0;
        h_n_m0[stage]<=#TCQ 0; h_n_m1[stage]<=#TCQ 0;
        h_op_m0[stage]<=#TCQ 0; h_op_m1[stage]<=#TCQ 0;
        h_param_m0[stage]<=#TCQ 0; h_param_m1[stage]<=#TCQ 0;
      end else begin
        if (stage_req) begin
          h_scale_m0[stage] <= #TCQ
              fp16_scale(stage_acc)+fp16_scale(stage_r);
          h_sign_m0[stage] <= #TCQ stage_acc[15]^stage_r[15];
          h_zero_m0[stage] <= #TCQ
              (stage_acc[14:0]==0)||(stage_r[14:0]==0);
          h_nan_m0[stage] <= #TCQ
              fp16_is_nan(stage_acc)||fp16_is_nan(stage_r)||
              ((fp16_is_inf(stage_acc)&&(stage_r[14:0]==0))||
               (fp16_is_inf(stage_r)&&(stage_acc[14:0]==0)));
          h_inf_m0[stage] <= #TCQ
              (fp16_is_inf(stage_acc)&&(stage_r[14:0]!=0))||
              (fp16_is_inf(stage_r)&&(stage_acc[14:0]!=0));
          h_coeff_m0[stage]<=#TCQ stage_coeff;
          h_r_m0[stage]<=#TCQ stage_r; h_n_m0[stage]<=#TCQ stage_n;
          h_op_m0[stage]<=#TCQ stage_op; h_param_m0[stage]<=#TCQ stage_param;
        end
        h_scale_m1[stage]<=#TCQ h_scale_m0[stage];
        h_sign_m1[stage]<=#TCQ h_sign_m0[stage];
        h_zero_m1[stage]<=#TCQ h_zero_m0[stage];
        h_nan_m1[stage]<=#TCQ h_nan_m0[stage];
        h_inf_m1[stage]<=#TCQ h_inf_m0[stage];
        h_coeff_m1[stage]<=#TCQ h_coeff_m0[stage];
        h_r_m1[stage]<=#TCQ h_r_m0[stage];
        h_n_m1[stage]<=#TCQ h_n_m0[stage];
        h_op_m1[stage]<=#TCQ h_op_m0[stage];
        h_param_m1[stage]<=#TCQ h_param_m0[stage];
      end
    end
  end endgenerate

  wire [15:0] result_fp16 =
      ((h_op_m1[5]==OP_EXP)||(h_op_m1[5]==OP_RCP)||
       (h_op_m1[5]==OP_RSQRT))
      ? fp16_scale_pow2(h_value[5],h_n_m1[5]) : h_value[5];

  always @(posedge clk) begin
    if (rst) begin c_out<=#TCQ 0; c_valid<=#TCQ 0; end
    else begin
      c_valid<=#TCQ h_valid[5];
      if (h_valid[5]) c_out<=#TCQ result_fp16;
    end
  end

  always @(posedge clk) begin
    if (rst) inflight<=#TCQ 0;
    else case ({accept,h_valid[5]})
      2'b10: inflight<=#TCQ inflight+1'b1;
      2'b01: inflight<=#TCQ inflight-1'b1;
      default: inflight<=#TCQ inflight;
    endcase
  end
endmodule
