from __future__ import annotations

import argparse
import json
import math
import struct
from statistics import mean
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class NumberFormat:
	"""Numeric format abstraction used to simulate hardware quantization."""

	def quantize(self, x: float) -> float:
		raise NotImplementedError

	def ulp_size(self, x: float) -> float:
		"""Return local ULP spacing around x in this numeric format."""
		return abs(math.ulp(float(x)))


class FloatFormat(NumberFormat):
	"""No quantization, used as high-precision reference."""

	def quantize(self, x: float) -> float:
		return float(x)

	def ulp_size(self, x: float) -> float:
		return abs(math.ulp(float(x)))


class FP16Format(NumberFormat):
	"""IEEE 754 binary16 quantization."""

	_EXP_BITS = 5
	_MAN_BITS = 10
	_BIAS = 15

	def quantize(self, x: float) -> float:
		if math.isnan(x):
			return math.nan
		if math.isinf(x):
			return x
		try:
			return struct.unpack("e", struct.pack("e", float(x)))[0]
		except OverflowError:
			return math.copysign(math.inf, x)

	def ulp_size(self, x: float) -> float:
		ax = abs(float(x))
		if math.isnan(ax):
			return math.nan
		if math.isinf(ax):
			return math.inf
		return binary_float_ulp(ax, self._MAN_BITS, self._BIAS)


class FP8Format(NumberFormat):
	"""Software FP8 quantization model with E4M3 or E5M2 behavior."""

	def __init__(self, exp_bits: int, man_bits: int, bias: int):
		self.exp_bits = exp_bits
		self.man_bits = man_bits
		self.bias = bias
		self.max_exp_field = (1 << exp_bits) - 2
		self.min_normal_exp = 1 - bias
		self.max_normal_exp = self.max_exp_field - bias

	def _max_finite(self) -> float:
		return (2.0 - 2.0 ** (-self.man_bits)) * (2.0 ** self.max_normal_exp)

	def quantize(self, x: float) -> float:
		if math.isnan(x):
			return math.nan
		if x == 0.0:
			return 0.0
		if math.isinf(x):
			return math.copysign(self._max_finite(), x)

		sign = -1.0 if x < 0 else 1.0
		ax = abs(x)
		max_finite = self._max_finite()
		if ax >= max_finite:
			return sign * max_finite

		exp_unbiased = math.floor(math.log2(ax))
		if exp_unbiased < self.min_normal_exp:
			step = 2.0 ** (self.min_normal_exp - self.man_bits)
			q = round(ax / step) * step
			min_normal = 2.0 ** self.min_normal_exp
			q = min(q, min_normal - step)
			if q <= 0.0:
				return 0.0
			return sign * q

		exp_val = int(exp_unbiased)
		mant = (ax / (2.0 ** exp_val)) - 1.0
		mant_q = round(mant * (2 ** self.man_bits)) / (2 ** self.man_bits)
		if mant_q >= 1.0:
			exp_val += 1
			mant_q = 0.0
		if exp_val > self.max_normal_exp:
			return sign * max_finite
		return sign * (1.0 + mant_q) * (2.0 ** exp_val)

	def ulp_size(self, x: float) -> float:
		ax = abs(float(x))
		if math.isnan(ax):
			return math.nan
		if math.isinf(ax):
			return math.inf
		return binary_float_ulp(ax, self.man_bits, self.bias)


class FixedPointFormat(NumberFormat):
	"""Signed fixed-point Qm_n format."""

	def __init__(self, int_bits: int, frac_bits: int):
		self.int_bits = int_bits
		self.frac_bits = frac_bits
		self.step = 2.0 ** (-frac_bits)
		self.min_val = -(2.0 ** int_bits)
		self.max_val = (2.0 ** int_bits) - self.step

	def quantize(self, x: float) -> float:
		if math.isnan(x):
			return math.nan
		if math.isinf(x):
			return self.max_val if x > 0 else self.min_val
		q = round(x / self.step) * self.step
		return min(max(q, self.min_val), self.max_val)

	def ulp_size(self, x: float) -> float:
		return self.step


def binary_float_ulp(ax: float, man_bits: int, bias: int) -> float:
	"""ULP size for IEEE-like binary floating-point formats (using |x|)."""
	if ax == 0.0:
		return 2.0 ** (1 - bias - man_bits)
	min_normal_exp = 1 - bias
	exp_unbiased = math.floor(math.log2(ax))
	if exp_unbiased < min_normal_exp:
		return 2.0 ** (1 - bias - man_bits)
	return 2.0 ** (exp_unbiased - man_bits)


def build_number_format(fmt: str) -> NumberFormat:
	key = fmt.lower()
	if key == "float":
		return FloatFormat()
	if key == "fp16":
		return FP16Format()
	if key == "fp8":
		return FP8Format(exp_bits=4, man_bits=3, bias=7)
	if key == "fp8_e5m2":
		return FP8Format(exp_bits=5, man_bits=2, bias=15)
	if key == "q3_5":
		return FixedPointFormat(int_bits=3, frac_bits=5)
	if key == "q6_10":
		return FixedPointFormat(int_bits=6, frac_bits=10)
	raise ValueError("Unsupported format, use: float/fp8/fp8_e5m2/fp16/q3_5/q6_10")


@dataclass
class NormalizationParams:
	"""Normalization parameters.

	mode:
	- affine_shift: y = ((x + c) / si) >> k
	- exp_ln2: reduce x by ln(2), returns context n for denorm
	- reciprocal_pow2: dynamic x = m * 2^k reduction, returns context n=-k
	- rsqrt_pow4: dynamic x = m * 4^k reduction, returns context n=-k
	"""

	c: float
	si: float
	k: int
	mode: str = "affine_shift"


class Normalization:
	"""Hardware-style normalization module."""

	def __init__(self, params: NormalizationParams, qfmt: NumberFormat):
		if params.si == 0:
			raise ValueError("si must not be zero")
		self.params = params
		self.qfmt = qfmt

	@staticmethod
	def _signed_right_shift_scale(v: float, k: int) -> float:
		# Hardware-style signed shift semantics on real-valued simulation:
		# k >= 0: divide by 2^k, k < 0: multiply by 2^{-k}.
		return v / (2 ** k) if k >= 0 else v * (2 ** (-k))

	def run(self, x: float) -> Tuple[float, Dict[str, int]]:
		"""Run normalization and return (normalized_value, context)."""
		xq = self.qfmt.quantize(x)
		cq = self.qfmt.quantize(self.params.c)
		siq = self.qfmt.quantize(self.params.si)

		if self.params.mode == "affine_shift":
			t0 = self.qfmt.quantize(xq + cq)
			t1 = self.qfmt.quantize(t0 / siq)
			y = self.qfmt.quantize(self._signed_right_shift_scale(t1, self.params.k))
			return y, {}

		if self.params.mode == "exp_ln2":
			# x = n*ln2 + r, choose n by rounding so r is centered near 0.
			t0 = self.qfmt.quantize(xq + cq)
			n = int(round(t0 / siq))
			r = self.qfmt.quantize(t0 - n * siq)
			return r, {"n": n}

		if self.params.mode == "reciprocal_pow2":
			# Dynamic power-of-two reduction for reciprocal-like functions.
			# For x>0, choose k so m = x / 2^k is in [0.5, 1.0), then 1/x = (1/m) * 2^-k.
			# We forward n=-k so the unified denorm path (k_eff = k - n) applies +k shift.
			if xq <= 0.0:
				raise ValueError("reciprocal_pow2 normalization requires x > 0")
			k_dyn = int(math.floor(math.log2(xq))) + 1
			m = self.qfmt.quantize(self._signed_right_shift_scale(xq, k_dyn))
			t0 = self.qfmt.quantize(m + cq)
			t1 = self.qfmt.quantize(t0 / siq)
			y = self.qfmt.quantize(self._signed_right_shift_scale(t1, self.params.k))
			return y, {"n": -k_dyn}

		if self.params.mode == "rsqrt_pow4":
			# Dynamic power-of-four reduction for rsqrt.
			# For x>0, choose k so m = x / 4^k is in [1.0, 4.0), then rsqrt(x) = rsqrt(m) * 2^-k.
			# We forward n=-k so the unified denorm path (k_eff = k - n) applies +k shift.
			if xq <= 0.0:
				raise ValueError("rsqrt_pow4 normalization requires x > 0")
			exp2 = int(math.floor(math.log2(xq)))
			k_dyn = int(math.floor(exp2 / 2.0))
			m = self.qfmt.quantize(self._signed_right_shift_scale(xq, 2 * k_dyn))
			t0 = self.qfmt.quantize(m + cq)
			t1 = self.qfmt.quantize(t0 / siq)
			y = self.qfmt.quantize(self._signed_right_shift_scale(t1, self.params.k))
			return y, {"n": -k_dyn}

		raise ValueError(f"Unsupported normalization mode: {self.params.mode}")


@dataclass
class HornerParams:
	"""Parameters for Taylor polynomial via Horner scheme."""

	N: int
	coeffs: List[float]


class TaylorExpansionHorner:
	"""Taylor expansion module implemented with Horner evaluation."""

	def __init__(self, params: HornerParams, qfmt: NumberFormat):
		if params.N < 0:
			raise ValueError("N must be >= 0")
		if len(params.coeffs) != params.N + 1:
			raise ValueError("coeffs length must be N + 1")
		self.params = params
		self.qfmt = qfmt

	def run(self, x: float) -> float:
		"""Evaluate a degree-N polynomial using Horner: a0*x^N + ... + aN."""
		xq = self.qfmt.quantize(x)
		acc = self.qfmt.quantize(self.params.coeffs[0])
		for i in range(1, self.params.N + 1):
			acc = self.qfmt.quantize(self.qfmt.quantize(acc * xq) + self.params.coeffs[i])
		return acc


@dataclass
class DenormalizationParams:
	"""Denormalization parameters.

	Execution is unified to:
	- y = ((x + c) / di) >> k_eff
	where k_eff = k - n when runtime context contains n.
	"""

	c: float
	di: float
	k: int


class Denormalization:
	"""Hardware-style denormalization module (inverse of normalization)."""

	def __init__(self, params: DenormalizationParams, qfmt: NumberFormat):
		if params.di == 0:
			raise ValueError("di must not be zero")
		self.params = params
		self.qfmt = qfmt

	@staticmethod
	def _signed_right_shift_scale(v: float, k: int) -> float:
		# k >= 0: divide by 2^k, k < 0: multiply by 2^{-k}.
		return v / (2 ** k) if k >= 0 else v * (2 ** (-k))

	def run(self, x: float, context: Dict[str, int]) -> float:
		"""Run denormalization using polynomial output and normalization context."""
		return self.run_with_params(x, context, self.params)

	def run_with_params(
		self,
		x: float,
		context: Dict[str, int],
		params: DenormalizationParams,
	) -> float:
		"""Run denormalization with an explicit parameter set.

		Unified data path:
		1) t0 = q(x + c)
		2) t1 = q(t0 / di)
		3) y = q(t1 >> k_eff), with k_eff = k - n if n exists in context
		"""
		xq = self.qfmt.quantize(x)
		if params.di == 0:
			raise ValueError("effective di must not be zero")

		effective_k = int(params.k) - int(context.get("n", 0))
		t0 = self.qfmt.quantize(xq + params.c)
		t1 = self.qfmt.quantize(t0 / params.di)
		y = self.qfmt.quantize(self._signed_right_shift_scale(t1, effective_k))
		return y


class ParameterBridge:
	"""Bridge static parameters and runtime normalization results into denormalization."""

	def __init__(self, config: Dict[str, Any] | None = None):
		self.config = config or {}

	def _resolve_source(
		self,
		source: Any,
		norm_params: NormalizationParams,
		denorm_params: DenormalizationParams,
		norm_context: Dict[str, int],
	) -> float:
		if isinstance(source, (int, float)):
			return float(source)
		if not isinstance(source, str):
			raise ValueError(f"Unsupported bridge source type: {type(source)}")

		if source == "norm.c":
			return norm_params.c
		if source == "norm.si":
			return norm_params.si
		if source == "norm.k":
			return float(norm_params.k)

		if source == "denorm.c":
			return denorm_params.c
		if source == "denorm.di":
			return denorm_params.di
		if source == "denorm.k":
			return float(denorm_params.k)

		if source.startswith("norm_ctx."):
			key = source.split(".", 1)[1]
			if key not in norm_context:
				raise ValueError(f"Bridge source '{source}' missing in normalization context")
			return float(norm_context[key])

		# Allow numeric literals as strings.
		return float(source)

	def resolve(
		self,
		norm_params: NormalizationParams,
		denorm_params: DenormalizationParams,
		norm_context: Dict[str, int],
	) -> tuple[DenormalizationParams, Dict[str, int]]:
		"""Resolve effective denormalization params and forwarded runtime context."""
		effective = DenormalizationParams(
			c=denorm_params.c,
			di=denorm_params.di,
			k=denorm_params.k,
		)

		mappings = self.config.get("denorm_from")
		if isinstance(mappings, dict):
			if "c" in mappings:
				effective.c = float(
					self._resolve_source(mappings["c"], norm_params, denorm_params, norm_context)
				)
			if "di" in mappings:
				effective.di = float(
					self._resolve_source(mappings["di"], norm_params, denorm_params, norm_context)
				)
			if "k" in mappings:
				effective.k = int(
					self._resolve_source(mappings["k"], norm_params, denorm_params, norm_context)
				)

		forward = dict(norm_context)
		keys = self.config.get("forward_context_keys")
		if isinstance(keys, list):
			forward = {k: int(norm_context[k]) for k in keys if k in norm_context}

		return effective, forward


@dataclass
class SegmentSpec:
	"""One domain segment with dedicated params and runtime modules."""

	name: str
	x_min: Optional[float]
	x_max: Optional[float]
	norm_params: NormalizationParams
	horner_params: HornerParams
	denorm_params: DenormalizationParams
	bridge_config: Dict[str, Any] | None = None


class SegmentRuntime:
	"""Executable segment pipeline."""

	def __init__(self, spec: SegmentSpec, qfmt: NumberFormat):
		self.spec = spec
		self.norm = Normalization(spec.norm_params, qfmt)
		self.taylor = TaylorExpansionHorner(spec.horner_params, qfmt)
		self.denorm = Denormalization(spec.denorm_params, qfmt)
		self.bridge = ParameterBridge(spec.bridge_config)

	def run(self, x: float) -> float:
		z, norm_ctx = self.norm.run(x)
		effective_denorm_params, runtime_ctx = self.bridge.resolve(
			self.spec.norm_params,
			self.spec.denorm_params,
			norm_ctx,
		)
		p = self.taylor.run(z)
		return self.denorm.run_with_params(p, runtime_ctx, effective_denorm_params)

	def match(self, x: float, include_right_edge: bool) -> bool:
		left_ok = True if self.spec.x_min is None else x >= self.spec.x_min
		if self.spec.x_max is None:
			right_ok = True
		else:
			right_ok = x <= self.spec.x_max if include_right_edge else x < self.spec.x_max
		return left_ok and right_ok


class TRFEngine:
	"""Three-stage execution engine: normalization -> horner -> denormalization."""

	def __init__(
		self,
		norm_params: NormalizationParams,
		horner_params: HornerParams,
		denorm_params: DenormalizationParams,
		fmt: str,
		bridge_config: Dict[str, Any] | None = None,
		segments: List[SegmentSpec] | None = None,
	):
		qfmt = build_number_format(fmt)
		self.qfmt = qfmt

		if segments:
			self.segments = [SegmentRuntime(s, qfmt) for s in segments]
		else:
			single = SegmentSpec(
				name="seg0",
				x_min=None,
				x_max=None,
				norm_params=norm_params,
				horner_params=horner_params,
				denorm_params=denorm_params,
				bridge_config=bridge_config,
			)
			self.segments = [SegmentRuntime(single, qfmt)]

	def _select_segment(self, x: float) -> SegmentRuntime:
		if len(self.segments) == 1:
			return self.segments[0]
		for i, seg in enumerate(self.segments):
			if seg.match(x, include_right_edge=(i == len(self.segments) - 1)):
				return seg
		raise ValueError(f"Input x={x} does not match any segment domain")

	def run(self, x: float) -> float:
		seg = self._select_segment(x)
		return seg.run(x)

	def evaluate(
		self,
		x: float,
		reference_function: str,
		ref_input_mode: str = "quantized",
	) -> Dict[str, float]:
		"""Evaluate error using quantized/full reference-input mode (default: quantized)."""
		xq = self.qfmt.quantize(x)
		out = self.run(xq)
		if ref_input_mode == "quantized":
			ref_in = xq
		elif ref_input_mode == "full":
			ref_in = x
		else:
			raise ValueError("ref_input_mode must be one of: quantized, full")

		ref_raw = evaluate_reference_function(reference_function, ref_in)
		ref_q = self.qfmt.quantize(ref_raw)
		abs_err = abs(out - ref_q)
		rel_err = abs_err / abs(ref_q) if ref_q != 0 else math.inf
		ulp = self.qfmt.ulp_size(ref_q)
		ulp_err = abs_err / ulp if ulp not in {0.0, math.inf} else (0.0 if abs_err == 0 else math.inf)
		return {
			"x": x,
			"x_quantized": xq,
			"ref_input": ref_in,
			"ref_input_mode": ref_input_mode,
			"output": out,
			"reference_raw": ref_raw,
			"reference_quantized": ref_q,
			"abs_error": abs_err,
			"rel_error": rel_err,
			"ulp_size": ulp,
			"ulp_error": ulp_err,
		}

	def evaluate_domain(
		self,
		reference_function: str,
		x_min: float,
		x_max: float,
		samples: int,
		ref_input_mode: str = "quantized",
	) -> Dict[str, float]:
		"""Evaluate performance over a domain and return aggregate metrics."""
		if samples < 2:
			raise ValueError("samples must be >= 2")
		if x_max < x_min:
			raise ValueError("x_max must be >= x_min")

		xs = [x_min + (x_max - x_min) * i / (samples - 1) for i in range(samples)]
		metrics = []
		skipped = 0
		for x in xs:
			try:
				metrics.append(self.evaluate(x, reference_function, ref_input_mode=ref_input_mode))
			except ValueError:
				# Skip invalid points for specific references, e.g. reciprocal at x=0.
				skipped += 1

		if not metrics:
			raise ValueError("No valid samples in the provided domain")

		abs_errors = [m["abs_error"] for m in metrics]
		rel_errors = [m["rel_error"] for m in metrics if math.isfinite(m["rel_error"])]
		ulp_errors = [m["ulp_error"] for m in metrics if math.isfinite(m["ulp_error"])]

		worst_abs = max(metrics, key=lambda m: m["abs_error"])
		worst_rel = max((m for m in metrics if math.isfinite(m["rel_error"])), key=lambda m: m["rel_error"], default=None)
		worst_ulp = max((m for m in metrics if math.isfinite(m["ulp_error"])), key=lambda m: m["ulp_error"], default=None)

		return {
			"domain_x_min": x_min,
			"domain_x_max": x_max,
			"ref_input_mode": ref_input_mode,
			"samples_requested": float(samples),
			"samples_used": float(len(metrics)),
			"samples_skipped": float(skipped),
			"max_abs_error": max(abs_errors),
			"mean_abs_error": mean(abs_errors),
			"rmse_abs_error": math.sqrt(mean([e * e for e in abs_errors])),
			"x_at_max_abs_error": worst_abs["x"],
			"max_rel_error": max(rel_errors) if rel_errors else math.nan,
			"mean_rel_error": mean(rel_errors) if rel_errors else math.nan,
			"x_at_max_rel_error": worst_rel["x"] if worst_rel else math.nan,
			"max_ulp_error": max(ulp_errors) if ulp_errors else math.nan,
			"mean_ulp_error": mean(ulp_errors) if ulp_errors else math.nan,
			"x_at_max_ulp_error": worst_ulp["x"] if worst_ulp else math.nan,
		}

	@classmethod
	def from_config(cls, config: Dict[str, Any]) -> "TRFEngine":
		fmt = str(config["number_format"])

		# New style: segmented configuration.
		if isinstance(config.get("segments"), list):
			segments: List[SegmentSpec] = []
			default_norm = config.get("normalization")
			default_horner = config.get("horner")
			default_denorm = config.get("denormalization")
			default_bridge = config.get("parameter_bridge")

			for idx, seg_cfg in enumerate(config["segments"]):
				if not isinstance(seg_cfg, dict):
					raise ValueError(f"segments[{idx}] must be an object")

				norm_cfg = seg_cfg.get("normalization", default_norm)
				horn_cfg = seg_cfg.get("horner", default_horner)
				den_cfg = seg_cfg.get("denormalization", default_denorm)
				bridge_cfg = seg_cfg.get("parameter_bridge", default_bridge)
				if norm_cfg is None or horn_cfg is None or den_cfg is None:
					raise ValueError(
						f"segments[{idx}] missing normalization/horner/denormalization and no defaults"
					)

				norm_params = NormalizationParams(
					c=float(norm_cfg["c"]),
					si=float(norm_cfg["si"]),
					k=int(norm_cfg["k"]),
					mode=str(norm_cfg.get("mode", "affine_shift")),
				)
				horn_params = HornerParams(
					N=int(horn_cfg["N"]),
					coeffs=[float(v) for v in horn_cfg["coeffs"]],
				)
				denorm_params = DenormalizationParams(
					c=float(den_cfg["c"]),
					di=float(den_cfg["di"]),
					k=int(den_cfg["k"]),
				)

				x_min, x_max = _parse_segment_range(seg_cfg)
				segments.append(
					SegmentSpec(
						name=str(seg_cfg.get("name", f"seg{idx}")),
						x_min=x_min,
						x_max=x_max,
						norm_params=norm_params,
						horner_params=horn_params,
						denorm_params=denorm_params,
						bridge_config=bridge_cfg if isinstance(bridge_cfg, dict) else None,
					)
				)

			# dummy params unused when segments are provided
			dummy_norm = NormalizationParams(c=0.0, si=1.0, k=0)
			dummy_horner = HornerParams(N=0, coeffs=[0.0])
			dummy_denorm = DenormalizationParams(c=0.0, di=1.0, k=0)
			return cls(dummy_norm, dummy_horner, dummy_denorm, fmt, None, segments)

		# Backward-compatible single segment configuration.
		norm_cfg = config["normalization"]
		horn_cfg = config["horner"]
		den_cfg = config["denormalization"]
		norm_params = NormalizationParams(
			c=float(norm_cfg["c"]),
			si=float(norm_cfg["si"]),
			k=int(norm_cfg["k"]),
			mode=str(norm_cfg.get("mode", "affine_shift")),
		)
		horn_params = HornerParams(N=int(horn_cfg["N"]), coeffs=[float(v) for v in horn_cfg["coeffs"]])
		denorm_params = DenormalizationParams(
			c=float(den_cfg["c"]),
			di=float(den_cfg["di"]),
			k=int(den_cfg["k"]),
		)
		bridge_cfg = config.get("parameter_bridge")
		return cls(norm_params, horn_params, denorm_params, fmt, bridge_cfg)


def _parse_segment_range(seg_cfg: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
	"""Parse domain range for a segment.

	Accepted forms:
	- "range": [x_min, x_max]
	- "domain": {"x_min": ..., "x_max": ...}
	- direct "x_min" / "x_max"
	"""
	if "range" in seg_cfg:
		rr = seg_cfg["range"]
		if not isinstance(rr, list) or len(rr) != 2:
			raise ValueError("segment.range must be [x_min, x_max]")
		return _as_optional_float(rr[0]), _as_optional_float(rr[1])
	if "domain" in seg_cfg:
		d = seg_cfg["domain"]
		if not isinstance(d, dict):
			raise ValueError("segment.domain must be an object")
		return _as_optional_float(d.get("x_min")), _as_optional_float(d.get("x_max"))
	return _as_optional_float(seg_cfg.get("x_min")), _as_optional_float(seg_cfg.get("x_max"))


def _as_optional_float(v: Any) -> Optional[float]:
	if v is None:
		return None
	return float(v)


def load_config(config_path: str) -> Dict[str, Any]:
	with Path(config_path).open("r", encoding="utf-8") as f:
		return json.load(f)


def evaluate_reference_function(name: str, x: float) -> float:
	"""Compute reference function value in float before quantized evaluation."""
	key = name.lower()
	if key in {"reciprocal", "inv", "1/x"}:
		if x == 0.0:
			raise ValueError("Reference reciprocal is undefined at x=0")
		return 1.0 / x
	if key in {"rsqrt", "inverse_sqrt", "x^-0.5", "1/sqrt(x)"}:
		if x <= 0.0:
			raise ValueError("Reference rsqrt is undefined for x<=0")
		return 1.0 / math.sqrt(x)
	if key == "exp":
		return math.exp(x)
	if key == "sigmoid":
		if x >= 0:
			z = math.exp(-x)
			return 1.0 / (1.0 + z)
		z = math.exp(x)
		return z / (1.0 + z)
	if key == "tanh":
		return math.tanh(x)
	if key == "gelu":
		# Exact GELU definition: 0.5*x*(1+erf(x/sqrt(2))).
		return 0.5 * x * (1.0 + math.erf(x / math.sqrt(2.0)))
	if key == "silu":
		# SiLU / Swish: x * sigmoid(x)
		if x >= 0:
			z = math.exp(-x)
			sig = 1.0 / (1.0 + z)
		else:
			z = math.exp(x)
			sig = z / (1.0 + z)
		return x * sig
	if key == "swish":
		# Swish with beta=1 is equivalent to SiLU.
		if x >= 0:
			z = math.exp(-x)
			sig = 1.0 / (1.0 + z)
		else:
			z = math.exp(x)
			sig = z / (1.0 + z)
		return x * sig
	if key == "mish":
		# Mish: x * tanh(softplus(x)). Use stable softplus approximation.
		if x > 20.0:
			sp = x
		elif x < -20.0:
			sp = math.exp(x)
		else:
			sp = math.log1p(math.exp(x))
		return x * math.tanh(sp)
	if key == "sin":
		return math.sin(x)
	if key == "cos":
		return math.cos(x)
	if key == "identity":
		return x
	raise ValueError("Unsupported reference function: use reciprocal/rsqrt/exp/sigmoid/tanh/gelu/silu/swish/mish/sin/cos/identity")


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="TRF Engine: single x + JSON config")
	parser.add_argument("--config", type=str, required=True, help="Path to engine config JSON")
	parser.add_argument("--x", type=float, default=None, help="Single input x")
	parser.add_argument(
		"--eval-func",
		type=str,
		default="",
		help="Optional reference function for error evaluation: reciprocal/rsqrt/exp/sigmoid/tanh/gelu/silu/swish/mish/sin/cos/identity",
	)
	parser.add_argument("--domain-min", type=float, default=None, help="Optional domain evaluation start x")
	parser.add_argument("--domain-max", type=float, default=None, help="Optional domain evaluation end x")
	parser.add_argument(
		"--domain-samples",
		type=int,
		default=1001,
		help="Number of samples for domain evaluation (>=2)",
	)
	parser.add_argument(
		"--ref-input-mode",
		type=str,
		default="",
		choices=["", "quantized", "full"],
		help="Reference input mode for evaluation: quantized (default) or full",
	)
	return parser.parse_args()


if __name__ == "__main__":
	args = parse_args()
	config = load_config(args.config)
	engine = TRFEngine.from_config(config)

	do_domain = args.domain_min is not None and args.domain_max is not None
	if args.x is None and not do_domain:
		raise ValueError("Provide --x for single-point run or provide --domain-min/--domain-max")

	if args.x is not None:
		out = engine.run(args.x)
		print(out)

	# Priority: CLI --eval-func > config.evaluation.reference_function
	eval_func = args.eval_func.strip()
	if not eval_func:
		eval_cfg = config.get("evaluation", {})
		if isinstance(eval_cfg, dict):
			eval_func = str(eval_cfg.get("reference_function", "")).strip()

	if eval_func and args.x is not None:
		# Priority: CLI --ref-input-mode > config.evaluation.ref_input_mode > quantized
		eval_cfg = config.get("evaluation", {}) if isinstance(config.get("evaluation", {}), dict) else {}
		ref_input_mode = args.ref_input_mode.strip() or str(eval_cfg.get("ref_input_mode", "quantized")).strip()
		if not ref_input_mode:
			ref_input_mode = "quantized"
		metrics = engine.evaluate(args.x, eval_func, ref_input_mode=ref_input_mode)
		print(
			"eval: "
			f"func={eval_func}, "
			f"ref_input_mode={metrics['ref_input_mode']}, "
			f"ref_q={metrics['reference_quantized']}, "
			f"abs_err={metrics['abs_error']}, "
			f"rel_err={metrics['rel_error']}, "
			f"ulp_size={metrics['ulp_size']}, "
			f"ulp_err={metrics['ulp_error']}"
		)

	if do_domain and eval_func:
		eval_cfg = config.get("evaluation", {}) if isinstance(config.get("evaluation", {}), dict) else {}
		ref_input_mode = args.ref_input_mode.strip() or str(eval_cfg.get("ref_input_mode", "quantized")).strip()
		if not ref_input_mode:
			ref_input_mode = "quantized"
		domain = engine.evaluate_domain(
			reference_function=eval_func,
			x_min=args.domain_min,
			x_max=args.domain_max,
			samples=args.domain_samples,
			ref_input_mode=ref_input_mode,
		)
		print("domain_eval:")
		print(
			f"  range=[{domain['domain_x_min']}, {domain['domain_x_max']}], "
			f"ref_input_mode={domain['ref_input_mode']}, "
			f"samples={int(domain['samples_used'])}/{int(domain['samples_requested'])}, "
			f"skipped={int(domain['samples_skipped'])}"
		)
		print(
			f"  abs: max={domain['max_abs_error']}, mean={domain['mean_abs_error']}, "
			f"rmse={domain['rmse_abs_error']}, x_at_max={domain['x_at_max_abs_error']}"
		)
		print(
			f"  rel: max={domain['max_rel_error']}, mean={domain['mean_rel_error']}, "
			f"x_at_max={domain['x_at_max_rel_error']}"
		)
		print(
			f"  ulp: max={domain['max_ulp_error']}, mean={domain['mean_ulp_error']}, "
			f"x_at_max={domain['x_at_max_ulp_error']}"
		)

	if do_domain and not eval_func:
		raise ValueError("Domain evaluation requires reference function via --eval-func or config.evaluation.reference_function")
