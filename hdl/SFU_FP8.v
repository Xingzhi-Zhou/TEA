`timescale 1ns/1ps

// Unified FP8 E4M3 Taylor-reducible SFU.
//
// FP8 format:
//   sign[7], exponent[6:3], fraction[2:0], bias 7
//   subnormals enabled, round-to-nearest-even, finite overflow saturation
//
// Opcodes:
//   2 EXP, 3 reciprocal, 4 reciprocal sqrt, 5 sigmoid,
//   6 SiLU, 7 GELU, 8 tanh, 9 Mish.
//
// The six Horner multipliers are spatially unrolled. Each multiplier uses a
// DSP48E-compatible integer multiply on the decoded four-bit significands.
// The product and following coefficient addition are independently rounded
// to FP8, matching utils/engine.py's arithmetic order.

module SFU_FP8 #(
  parameter integer TCQ         = 1,
  parameter integer DSP_LAT     = 1,
  parameter integer CONFIG_AW   = 7,
  parameter integer CONFIG_DW   = 72,
  parameter         CONFIG_FILE = ""
)(
  input  wire       clk,
  input  wire       rst,
  input  wire       in_valid,
  output wire       in_ready,
  input  wire [3:0] sfu_op,
  input  wire [7:0] a_in,
  output reg  [7:0] c_out,
  output reg        c_valid
);

  localparam [3:0] OP_EXP     = 4'h2;
  localparam [3:0] OP_RCP     = 4'h3;
  localparam [3:0] OP_RSQRT   = 4'h4;
  localparam [3:0] OP_SIGMOID = 4'h5;
  localparam [3:0] OP_SILU    = 4'h6;
  localparam [3:0] OP_GELU    = 4'h7;
  localparam [3:0] OP_TANH    = 4'h8;
  localparam [3:0] OP_MISH    = 4'h9;
  localparam [7:0] FP8_QNAN   = 8'h7f;

  function [7:0] fp8_sanitize;
    input [7:0] value;
    begin
      if (value[6:3] != 4'hf)
        fp8_sanitize = value;
      else if (value[2:0] != 0)
        fp8_sanitize = FP8_QNAN;
      else
        fp8_sanitize = {value[7], 7'h77};
    end
  endfunction

  function fp8_is_nan;
    input [7:0] value;
    begin
      fp8_is_nan = (value[6:3] == 4'hf) && (value[2:0] != 0);
    end
  endfunction

  function [7:0] fp8_order_key;
    input [7:0] value;
    reg [7:0] clean;
    begin
      clean = fp8_sanitize(value);
      if (clean[6:0] == 0)
        clean = 0;
      fp8_order_key = clean[7] ? ~clean : (clean ^ 8'h80);
    end
  endfunction

  // Exact value representation: value = significand * 2^scale.
  function [3:0] fp8_significand;
    input [7:0] value;
    reg [7:0] clean;
    begin
      clean = fp8_sanitize(value);
      if (clean[6:3] == 0)
        fp8_significand = {1'b0, clean[2:0]};
      else
        fp8_significand = {1'b1, clean[2:0]};
    end
  endfunction

  function signed [8:0] fp8_scale;
    input [7:0] value;
    reg [7:0] clean;
    begin
      clean = fp8_sanitize(value);
      if (clean[6:3] == 0)
        fp8_scale = -9'sd9;
      else
        fp8_scale = $signed({1'b0, clean[6:3]}) - 9'sd10;
    end
  endfunction

  // Convert magnitude*2^scale to E4M3 with round-to-nearest-even.
  function [7:0] fp8_from_mag;
    input [17:0] magnitude;
    input signed [8:0] scale;
    input sign_value;
    integer bit_index;
    integer msb;
    integer unbiased_exp;
    integer shift;
    integer unit_shift;
    reg found;
    reg [63:0] wide;
    reg [63:0] quotient;
    reg [63:0] remainder;
    reg [63:0] half;
    reg [4:0] exponent_field;
    begin
      if (magnitude == 0) begin
        fp8_from_mag = {sign_value, 7'd0};
      end else begin
        msb = 0;
        found = 0;
        for (bit_index = 17; bit_index >= 0; bit_index = bit_index - 1) begin
          if (!found && magnitude[bit_index]) begin
            msb = bit_index;
            found = 1;
          end
        end
        wide = magnitude;
        unbiased_exp = msb + scale;

        if (unbiased_exp < -6) begin
          // Subnormal grid has a constant step of 2^-9.
          unit_shift = scale + 9;
          if (unit_shift >= 0) begin
            quotient = wide << unit_shift;
          end else begin
            shift = -unit_shift;
            if (shift >= 63) begin
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
          if (quotient >= 8)
            // Match utils/engine.py's subnormal-branch clamp.
            fp8_from_mag = {sign_value, 4'd0, 3'd7};
          else
            fp8_from_mag = {sign_value, 4'd0, quotient[2:0]};
        end else begin
          // Round the exact magnitude to a four-bit 1.xxx significand.
          shift = msb - 3;
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

          if (quotient >= 16) begin
            quotient = 8;
            unbiased_exp = unbiased_exp + 1;
          end
          exponent_field = unbiased_exp + 7;
          if (exponent_field >= 15)
            fp8_from_mag = {sign_value, 7'h77};
          else if (exponent_field <= 0)
            fp8_from_mag = {sign_value, 7'd0};
          else
            fp8_from_mag = {
              sign_value, exponent_field[3:0], quotient[2:0]
            };
        end
      end
    end
  endfunction

  // All finite E4M3 values are integral multiples of 2^-9.
  function signed [31:0] fp8_to_units;
    input [7:0] value;
    reg [7:0] clean;
    reg signed [31:0] magnitude;
    begin
      clean = fp8_sanitize(value);
      if (fp8_is_nan(clean)) begin
        fp8_to_units = 0;
      end else if (clean[6:3] == 0) begin
        magnitude = clean[2:0];
        fp8_to_units = clean[7] ? -magnitude : magnitude;
      end else begin
        magnitude = {1'b1, clean[2:0]} << (clean[6:3] - 1'b1);
        fp8_to_units = clean[7] ? -magnitude : magnitude;
      end
    end
  endfunction

  function [7:0] fp8_from_units;
    input signed [31:0] units;
    reg [17:0] magnitude;
    begin
      if (units < 0) begin
        magnitude = -units;
        fp8_from_units = fp8_from_mag(magnitude, -9'sd9, 1'b1);
      end else begin
        magnitude = units;
        fp8_from_units = fp8_from_mag(magnitude, -9'sd9, 1'b0);
      end
    end
  endfunction

  function [7:0] fp8_add;
    input [7:0] lhs;
    input [7:0] rhs;
    reg [7:0] lhs_clean;
    reg [7:0] rhs_clean;
    reg signed [31:0] sum;
    begin
      lhs_clean = fp8_sanitize(lhs);
      rhs_clean = fp8_sanitize(rhs);
      if (fp8_is_nan(lhs_clean) || fp8_is_nan(rhs_clean))
        fp8_add = FP8_QNAN;
      else begin
        sum = fp8_to_units(lhs_clean) + fp8_to_units(rhs_clean);
        fp8_add = fp8_from_units(sum);
      end
    end
  endfunction

  // Quantized E4M3 division.  The significands are only four bits wide;
  // carrying twelve quotient guard bits is sufficient to make the final
  // fp8_from_mag round-to-nearest-even decision exact for every finite
  // E4M3 operand pair.
  function [7:0] fp8_div;
    input [7:0] numerator_value;
    input [7:0] denominator_value;
    reg [7:0] numerator_clean;
    reg [7:0] denominator_clean;
    reg [17:0] quotient;
    reg signed [8:0] quotient_scale;
    begin
      numerator_clean = fp8_sanitize(numerator_value);
      denominator_clean = fp8_sanitize(denominator_value);
      if (fp8_is_nan(numerator_clean) ||
          fp8_is_nan(denominator_clean) ||
          (fp8_significand(denominator_clean) == 0)) begin
        fp8_div = FP8_QNAN;
      end else if (fp8_significand(numerator_clean) == 0) begin
        fp8_div = {
          numerator_clean[7] ^ denominator_clean[7], 7'd0
        };
      end else begin
        quotient =
            ({14'd0, fp8_significand(numerator_clean)} << 12) /
            fp8_significand(denominator_clean);
        quotient_scale =
            fp8_scale(numerator_clean) -
            fp8_scale(denominator_clean) - 9'sd12;
        fp8_div = fp8_from_mag(
            quotient,
            quotient_scale,
            numerator_clean[7] ^ denominator_clean[7]);
      end
    end
  endfunction

  function [7:0] fp8_negate;
    input [7:0] value;
    begin
      fp8_negate = fp8_is_nan(value) ? FP8_QNAN : {~value[7], value[6:0]};
    end
  endfunction

  function [7:0] fp8_scale_pow2;
    input [7:0] value;
    input signed [7:0] exponent;
    reg [7:0] clean;
    reg signed [8:0] target_scale;
    begin
      clean = fp8_sanitize(value);
      if (fp8_is_nan(clean))
        fp8_scale_pow2 = FP8_QNAN;
      else begin
        target_scale = fp8_scale(clean) + exponent;
        fp8_scale_pow2 = fp8_from_mag(
            {14'd0, fp8_significand(clean)}, target_scale, clean[7]);
      end
    end
  endfunction

  function signed [7:0] fp8_floor_log2;
    input [7:0] value;
    integer bit_index;
    integer highest;
    reg found;
    begin
      if (value[6:3] != 0) begin
        fp8_floor_log2 = $signed({1'b0, value[6:3]}) - 8'sd7;
      end else begin
        highest = 0;
        found = 0;
        for (bit_index = 2; bit_index >= 0; bit_index = bit_index - 1) begin
          if (!found && value[bit_index]) begin
            highest = bit_index;
            found = 1;
          end
        end
        fp8_floor_log2 = highest - 8'sd9;
      end
    end
  endfunction

  // RNE signed division by ln(2)=0.6875=352*2^-9.
  function signed [7:0] round_div_352;
    input signed [31:0] numerator;
    reg [31:0] magnitude;
    reg [31:0] quotient;
    reg [31:0] remainder;
    begin
      magnitude = numerator < 0 ? -numerator : numerator;
      quotient = magnitude / 352;
      remainder = magnitude - quotient * 352;
      if ((remainder > 176) ||
          ((remainder == 176) && quotient[0]))
        quotient = quotient + 1'b1;
      round_div_352 =
          numerator < 0 ? -$signed(quotient[7:0])
                        :  $signed(quotient[7:0]);
    end
  endfunction

  function [7:0] coeff_at;
    input [71:0] word;
    input integer index;
    begin
      coeff_at = word[16 + index*8 +: 8];
    end
  endfunction

  wire supported_op =
      (sfu_op == OP_EXP)     || (sfu_op == OP_RCP)  ||
      (sfu_op == OP_RSQRT)   || (sfu_op == OP_SIGMOID) ||
      (sfu_op == OP_SILU)    || (sfu_op == OP_GELU) ||
      (sfu_op == OP_TANH)    || (sfu_op == OP_MISH);

  // -----------------------------------------------------------------------
  // Unified configuration ROM. Descriptor and boundary records are loaded
  // only while switching opcode. The single ROM port then serves one
  // parameter word per accepted input in steady state.
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
  reg [6:0] active_boundary_base;
  reg [3:0] active_boundary_count;
  reg [1:0] boundary_word_count;
  reg [1:0] boundary_word_index;
  reg [7:0] boundary_regs [0:MAX_BOUNDARIES-1];

  wire loading = (cfg_state != CFG_IDLE);
  wire config_request =
      in_valid && supported_op && (cfg_state == CFG_IDLE) &&
      (inflight == 0) &&
      (!configured || (sfu_op != active_op));
  assign in_ready =
      supported_op && configured && !loading && (sfu_op == active_op);
  wire accept = in_valid && in_ready;

  reg [4:0] segment_index;
  integer class_i;
  always @* begin
    segment_index = 0;
    for (class_i = 0; class_i < MAX_BOUNDARIES; class_i = class_i + 1) begin
      if ((class_i < active_boundary_count) &&
          (fp8_order_key(a_in) >= fp8_order_key(boundary_regs[class_i])))
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
        config_rom_addr = active_boundary_base + boundary_word_index;
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
  wire [71:0] param_data = config_rom_data;
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
      active_boundary_base <= #TCQ 0;
      active_boundary_count <= #TCQ 0;
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
            active_boundary_base <= #TCQ config_rom_data[18:12];
            active_boundary_count <= #TCQ config_rom_data[22:19];
            boundary_word_count <= #TCQ config_rom_data[24:23];
            boundary_word_index <= #TCQ 0;
            if (config_rom_data[24:23] == 0) begin
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
              if (((boundary_word_index * 9 + cfg_i) <
                   active_boundary_count) &&
                  ((boundary_word_index * 9 + cfg_i) < MAX_BOUNDARIES))
                boundary_regs[boundary_word_index * 9 + cfg_i] <= #TCQ
                    config_rom_data[cfg_i*8 +: 8];
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

  reg [7:0] rom_x;
  reg [3:0] rom_op;
  always @(posedge clk) begin
    if (rst) begin
      rom_x <= #TCQ 0;
      rom_op <= #TCQ 0;
    end else if (accept) begin
      rom_x <= #TCQ fp8_sanitize(a_in);
      rom_op <= #TCQ sfu_op;
    end
  end

  // -----------------------------------------------------------------------
  // Input normalization. Affine functions use FP8 addition. EXP range
  // reduction is exact in 2^-9 units. RCP/RSQRT reuse the encoded exponent.
  // -----------------------------------------------------------------------
  wire [7:0] norm_c = param_data[7:0];
  wire [7:0] norm_si = param_data[15:8];
  wire signed [31:0] rom_x_units = fp8_to_units(rom_x);
  wire signed [7:0] exp_n = round_div_352(rom_x_units);
  wire signed [31:0] exp_r_units =
      rom_x_units - $signed(exp_n) * 32'sd352;
  wire signed [7:0] input_log2 = fp8_floor_log2(rom_x);
  wire signed [7:0] rcp_k = input_log2 + 1'b1;
  wire signed [7:0] rsqrt_k = input_log2 >>> 1;
  wire invalid_positive_domain =
      rom_x[7] || (rom_x[6:0] == 0) || fp8_is_nan(rom_x);
  wire [7:0] rcp_m =
      fp8_scale_pow2(rom_x, -rcp_k);
  wire [7:0] rsqrt_m =
      fp8_scale_pow2(rom_x, -(rsqrt_k <<< 1));
  wire [7:0] affine_r = fp8_div(fp8_add(rom_x, norm_c), norm_si);
  wire [7:0] rcp_r = fp8_div(fp8_add(rcp_m, norm_c), norm_si);
  wire [7:0] rsqrt_r = fp8_div(fp8_add(rsqrt_m, norm_c), norm_si);
  wire [7:0] normalized_r =
      (rom_op == OP_EXP) ? fp8_from_units(exp_r_units) :
      (rom_op == OP_RCP) ?
          (invalid_positive_domain ? FP8_QNAN : rcp_r) :
      (rom_op == OP_RSQRT) ?
          (invalid_positive_domain ? FP8_QNAN : rsqrt_r) :
      affine_r;
  wire signed [7:0] normalized_n =
      (rom_op == OP_EXP) ? exp_n :
      (rom_op == OP_RCP) ? -rcp_k :
      (rom_op == OP_RSQRT) ? -rsqrt_k : 8'sd0;

  // -----------------------------------------------------------------------
  // Fully unrolled six-stage FP8 Horner pipeline.
  // -----------------------------------------------------------------------
  wire signed [47:0] h_product [0:5];
  wire h_valid [0:5];
  wire [7:0] h_value [0:5];

  reg signed [8:0] h_scale_m0 [0:5];
  reg signed [8:0] h_scale_m1 [0:5];
  reg h_sign_m0 [0:5];
  reg h_sign_m1 [0:5];
  reg h_zero_m0 [0:5];
  reg h_zero_m1 [0:5];
  reg h_nan_m0 [0:5];
  reg h_nan_m1 [0:5];
  reg [7:0] h_coeff_m0 [0:5];
  reg [7:0] h_coeff_m1 [0:5];
  reg [7:0] h_r_m0 [0:5];
  reg [7:0] h_r_m1 [0:5];
  reg signed [7:0] h_n_m0 [0:5];
  reg signed [7:0] h_n_m1 [0:5];
  reg [3:0] h_op_m0 [0:5];
  reg [3:0] h_op_m1 [0:5];
  reg [71:0] h_param_m0 [0:5];
  reg [71:0] h_param_m1 [0:5];

  genvar stage;
  generate
    for (stage = 0; stage < 6; stage = stage + 1) begin : g_horner
      wire stage_req = (stage == 0) ? param_valid : h_valid[stage-1];
      wire [7:0] stage_r =
          (stage == 0) ? normalized_r : h_r_m1[stage-1];
      wire signed [7:0] stage_n =
          (stage == 0) ? normalized_n : h_n_m1[stage-1];
      wire [3:0] stage_op =
          (stage == 0) ? rom_op : h_op_m1[stage-1];
      wire [71:0] stage_param =
          (stage == 0) ? param_data : h_param_m1[stage-1];
      wire [7:0] stage_acc =
          (stage == 0) ? coeff_at(stage_param, 0)
                       : h_value[stage-1];
      wire [7:0] stage_coeff = coeff_at(stage_param, stage+1);
      wire [3:0] acc_sig = fp8_significand(stage_acc);
      wire [3:0] r_sig = fp8_significand(stage_r);
      wire signed [26:0] dsp_a = {23'd0, acc_sig};
      wire signed [17:0] dsp_b = {14'd0, r_sig};

      dsp48e_fma_stub #(
        .TCQ(TCQ), .LAT(DSP_LAT), .WA(27), .WB(18), .WP(48)
      ) u_horner_mul (
        .clk(clk), .rst(rst), .a(dsp_a), .b(dsp_b), .c(48'sd0),
        .req(stage_req), .p(h_product[stage]), .valid(h_valid[stage])
      );

      wire [7:0] rounded_product =
          h_nan_m1[stage] ? FP8_QNAN :
          h_zero_m1[stage] ? {h_sign_m1[stage], 7'd0} :
          fp8_from_mag(
              h_product[stage][17:0],
              h_scale_m1[stage],
              h_sign_m1[stage]);
      assign h_value[stage] =
          fp8_add(rounded_product, h_coeff_m1[stage]);

      always @(posedge clk) begin
        if (rst) begin
          h_scale_m0[stage] <= #TCQ 0;
          h_scale_m1[stage] <= #TCQ 0;
          h_sign_m0[stage] <= #TCQ 0;
          h_sign_m1[stage] <= #TCQ 0;
          h_zero_m0[stage] <= #TCQ 0;
          h_zero_m1[stage] <= #TCQ 0;
          h_nan_m0[stage] <= #TCQ 0;
          h_nan_m1[stage] <= #TCQ 0;
          h_coeff_m0[stage] <= #TCQ 0;
          h_coeff_m1[stage] <= #TCQ 0;
          h_r_m0[stage] <= #TCQ 0;
          h_r_m1[stage] <= #TCQ 0;
          h_n_m0[stage] <= #TCQ 0;
          h_n_m1[stage] <= #TCQ 0;
          h_op_m0[stage] <= #TCQ 0;
          h_op_m1[stage] <= #TCQ 0;
          h_param_m0[stage] <= #TCQ 0;
          h_param_m1[stage] <= #TCQ 0;
        end else begin
          if (stage_req) begin
            h_scale_m0[stage] <= #TCQ
                fp8_scale(stage_acc) + fp8_scale(stage_r);
            h_sign_m0[stage] <= #TCQ stage_acc[7] ^ stage_r[7];
            h_zero_m0[stage] <= #TCQ
                (fp8_significand(stage_acc) == 0) ||
                (fp8_significand(stage_r) == 0);
            h_nan_m0[stage] <= #TCQ
                fp8_is_nan(stage_acc) || fp8_is_nan(stage_r);
            h_coeff_m0[stage] <= #TCQ stage_coeff;
            h_r_m0[stage] <= #TCQ stage_r;
            h_n_m0[stage] <= #TCQ stage_n;
            h_op_m0[stage] <= #TCQ stage_op;
            h_param_m0[stage] <= #TCQ stage_param;
          end
          h_scale_m1[stage] <= #TCQ h_scale_m0[stage];
          h_sign_m1[stage] <= #TCQ h_sign_m0[stage];
          h_zero_m1[stage] <= #TCQ h_zero_m0[stage];
          h_nan_m1[stage] <= #TCQ h_nan_m0[stage];
          h_coeff_m1[stage] <= #TCQ h_coeff_m0[stage];
          h_r_m1[stage] <= #TCQ h_r_m0[stage];
          h_n_m1[stage] <= #TCQ h_n_m0[stage];
          h_op_m1[stage] <= #TCQ h_op_m0[stage];
          h_param_m1[stage] <= #TCQ h_param_m0[stage];
        end
      end
    end
  endgenerate

  wire [7:0] result_fp8 =
      ((h_op_m1[5] == OP_EXP) ||
       (h_op_m1[5] == OP_RCP) ||
       (h_op_m1[5] == OP_RSQRT))
          ? fp8_scale_pow2(h_value[5], h_n_m1[5])
          : h_value[5];

  always @(posedge clk) begin
    if (rst) begin
      c_out <= #TCQ 0;
      c_valid <= #TCQ 0;
    end else begin
      c_valid <= #TCQ h_valid[5];
      if (h_valid[5])
        c_out <= #TCQ result_fp8;
    end
  end

  always @(posedge clk) begin
    if (rst) begin
      inflight <= #TCQ 0;
    end else begin
      case ({accept, h_valid[5]})
        2'b10: inflight <= #TCQ inflight + 1'b1;
        2'b01: inflight <= #TCQ inflight - 1'b1;
        default: inflight <= #TCQ inflight;
      endcase
    end
  end

endmodule
