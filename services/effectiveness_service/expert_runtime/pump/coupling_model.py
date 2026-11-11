# -*- coding: utf-8 -*-
"""Monotonic directed-coupling surrogates learned from existing schemes."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy.optimize import lsq_linear

from frontier_model import FrontierTargetModel, fit_frontier_target
from project_excel import AttributeSpec, ProjectDataError, ProjectDataset


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def coupling_constraint_mode(project: ProjectDataset, target_key: str) -> str:
    """Resolve whether a monotone target is a one-sided frontier or a soft trend."""
    explicit_aliases = {
        "minimum_frontier": "minimum_frontier",
        "lower_frontier": "minimum_frontier",
        "下限前沿": "minimum_frontier",
        "最低可实现前沿": "minimum_frontier",
        "maximum_frontier": "maximum_frontier",
        "upper_frontier": "maximum_frontier",
        "上限前沿": "maximum_frontier",
        "最高可实现前沿": "maximum_frontier",
        "hard_band": "hard_band",
        "双侧硬带": "hard_band",
        "双侧硬约束": "hard_band",
        "expected_trend": "expected_trend",
        "soft_trend": "expected_trend",
        "趋势": "expected_trend",
        "软趋势": "expected_trend",
    }
    edges = [item for item in project.couplings if item.target_key == target_key]
    for edge in edges:
        raw = str(getattr(edge, "relation_type", "") or "").strip().lower()
        if raw in explicit_aliases:
            return explicit_aliases[raw]
    target = project.attribute_by_key()[target_key]
    if target.preference_direction == "lower_better":
        return "minimum_frontier"
    if target.preference_direction == "higher_better":
        return "maximum_frontier"
    return "expected_trend"


@dataclass
class SourceEffect:
    key: str
    label: str
    direction: str
    normalized_coefficient: float
    physical_coefficient: float
    source_unit: str
    normalized_lower: Optional[float] = None
    normalized_upper: Optional[float] = None
    physical_lower: Optional[float] = None
    physical_upper: Optional[float] = None


@dataclass
class TargetAssessment:
    target_key: str
    target_label: str
    target_unit: str
    actual: float
    predicted: float
    lower: float
    upper: float
    residual: float
    status: str
    boundary_distance: float
    severity: float
    message: str
    source_contributions: List[Dict[str, Any]]
    model_scope: str = "global_monotonic"
    local_support_count: int = 0
    local_effective_support: float = 0.0
    nearest_scheme_ids: Optional[List[str]] = None
    constraint_mode: str = "expected_trend"
    feasibility_relevant: bool = False
    engineering_interpretation: str = ""


@dataclass
class MonotonicTargetModel:
    target_key: str
    target_label: str
    target_unit: str
    source_effects: List[SourceEffect]
    intercept: float
    lower_offset: float
    upper_offset: float
    rmse: float
    r2: float
    sample_count: int
    training_coverage: float
    confidence: str
    target_min: float
    target_max: float
    source_ranges: Dict[str, List[float]]
    identifiability_score: float = 0.0
    identifiability_label: str = "未评估"
    condition_number: Optional[float] = None
    effective_rank: int = 0
    expert_constraint_count: int = 0
    constraint_mode: str = "expected_trend"

    def normalized_sources(self, params: Dict[str, Any]) -> List[float]:
        values = []
        for effect in self.source_effects:
            lo, hi = self.source_ranges[effect.key]
            values.append(clamp((float(params[effect.key]) - lo) / max(hi - lo, 1e-9), 0.0, 1.0))
        return values

    def predict_normalized(self, params: Dict[str, Any]) -> float:
        value = self.intercept
        for effect, source_value in zip(self.source_effects, self.normalized_sources(params)):
            value += effect.normalized_coefficient * source_value
        return value

    def denormalize_target(self, normalized: float) -> float:
        return self.target_min + normalized * (self.target_max - self.target_min)

    def predict(self, params: Dict[str, Any]) -> float:
        return self.denormalize_target(self.predict_normalized(params))

    def band(self, params: Dict[str, Any]) -> Dict[str, float]:
        predicted_normalized = self.predict_normalized(params)
        lower = self.denormalize_target(predicted_normalized + self.lower_offset)
        upper = self.denormalize_target(predicted_normalized + self.upper_offset)
        lo = clamp(min(lower, upper), self.target_min, self.target_max)
        hi = clamp(max(lower, upper), self.target_min, self.target_max)
        if hi - lo < 1e-9:
            pad = 0.02 * (self.target_max - self.target_min)
            lo = clamp(lo - pad, self.target_min, self.target_max)
            hi = clamp(hi + pad, self.target_min, self.target_max)
        return {"predicted": self.predict(params), "lower": lo, "upper": hi}

    def assess(
        self,
        params: Dict[str, Any],
        band_override: Optional[Dict[str, Any]] = None,
    ) -> TargetAssessment:
        band = band_override or self.band(params)
        actual = float(params[self.target_key])
        width = max(band["upper"] - band["lower"], 0.04 * (self.target_max - self.target_min), 1e-9)
        residual = actual - band["predicted"]
        scope = str(band.get("model_scope") or "global_monotonic")
        scope_text = "局部同规格历史实物条件带" if scope == "local_context" else "已有样本条件经验带"
        feasibility_relevant = False
        engineering_interpretation = ""
        if actual < band["lower"]:
            status = "below_band"
            distance = band["lower"] - actual
            severity = clamp(distance / width, 0.05, 1.0)
            feasibility_relevant = self.constraint_mode in {"minimum_frontier", "hard_band"}
            if self.constraint_mode == "minimum_frontier":
                engineering_interpretation = "可能是轻量化/低负担突破，也可能低于当前技术可实现前沿"
                message = (
                    f"当前 {self.target_label} 为 {actual:.3g} {self.target_unit}，低于{scope_text}下沿 "
                    f"{band['lower']:.3g}。如果确实能够实现，应作为更优前沿；如果当前技术做不到，应标记为目标过低。"
                )
            elif self.constraint_mode == "hard_band":
                engineering_interpretation = "低于明确的双侧物理带"
                message = (
                    f"当前 {self.target_label} 为 {actual:.3g} {self.target_unit}，低于双侧硬约束下沿 "
                    f"{band['lower']:.3g}，需要专家确认能否实现。"
                )
            elif self.constraint_mode == "maximum_frontier":
                engineering_interpretation = "可以实现，但目标性能偏低"
                message = (
                    f"当前 {self.target_label} 为 {actual:.3g} {self.target_unit}，低于常见条件带下沿 "
                    f"{band['lower']:.3g}。这通常仍可实现，只表示性能偏低。"
                )
            else:
                engineering_interpretation = "低于通常耦合趋势"
                message = (
                    f"当前 {self.target_label} 为 {actual:.3g} {self.target_unit}，低于{scope_text} "
                    f"[{band['lower']:.3g}, {band['upper']:.3g}]，表示偏离常见趋势。"
                )
        elif actual > band["upper"]:
            status = "above_band"
            distance = actual - band["upper"]
            severity = clamp(distance / width, 0.05, 1.0)
            feasibility_relevant = self.constraint_mode in {"maximum_frontier", "hard_band"}
            if self.constraint_mode == "minimum_frontier":
                engineering_interpretation = "可以实现，但目标值偏高、设计效率较低"
                message = (
                    f"当前 {self.target_label} 为 {actual:.3g} {self.target_unit}，高于通常条件带上沿 "
                    f"{band['upper']:.3g}。这通常仍可实现，只表示偏重或设计效率较低，不作为物理不可行。"
                )
            elif self.constraint_mode == "maximum_frontier":
                engineering_interpretation = "可能超过当前技术可实现上限"
                message = (
                    f"当前 {self.target_label} 为 {actual:.3g} {self.target_unit}，高于{scope_text}上沿 "
                    f"{band['upper']:.3g}，需要确认当前技术是否能达到。"
                )
            elif self.constraint_mode == "hard_band":
                engineering_interpretation = "高于明确的双侧物理带"
                message = (
                    f"当前 {self.target_label} 为 {actual:.3g} {self.target_unit}，高于双侧硬约束上沿 "
                    f"{band['upper']:.3g}，需要专家确认能否实现。"
                )
            else:
                engineering_interpretation = "高于通常耦合趋势"
                message = (
                    f"当前 {self.target_label} 为 {actual:.3g} {self.target_unit}，高于{scope_text} "
                    f"[{band['lower']:.3g}, {band['upper']:.3g}]。"
                )
        else:
            lower_distance = actual - band["lower"]
            upper_distance = band["upper"] - actual
            relative = min(lower_distance, upper_distance) / width
            if relative < 0.12:
                status = "near_boundary"
                severity = 0.30
                near_lower = lower_distance <= upper_distance
                feasibility_relevant = bool(
                    (near_lower and self.constraint_mode in {"minimum_frontier", "hard_band"})
                    or (not near_lower and self.constraint_mode in {"maximum_frontier", "hard_band"})
                )
                engineering_interpretation = (
                    "接近物理可实现前沿"
                    if feasibility_relevant
                    else "接近常见联动带边缘，但不在物理风险一侧"
                )
                message = f"{self.target_label}{engineering_interpretation}，可用于确认当前条件走廊。"
            else:
                status = "within_band"
                severity = 0.05
                engineering_interpretation = "符合当前规格下的常见联动趋势"
                message = (
                    f"{self.target_label} 位于{scope_text} "
                    f"[{band['lower']:.3g}, {band['upper']:.3g}] 内。"
                )
            distance = min(lower_distance, upper_distance)
        return TargetAssessment(
            target_key=self.target_key,
            target_label=self.target_label,
            target_unit=self.target_unit,
            actual=round(actual, 6),
            predicted=round(band["predicted"], 6),
            lower=round(band["lower"], 6),
            upper=round(band["upper"], 6),
            residual=round(residual, 6),
            status=status,
            boundary_distance=round(distance, 6),
            severity=round(severity, 3),
            message=message,
            source_contributions=self.source_contributions(params),
            model_scope=scope,
            local_support_count=int(band.get("support_count", 0)),
            local_effective_support=round(float(band.get("effective_support", 0.0)), 3),
            nearest_scheme_ids=list(band.get("nearest_scheme_ids") or []),
            constraint_mode=self.constraint_mode,
            feasibility_relevant=feasibility_relevant,
            engineering_interpretation=engineering_interpretation,
        )

    def source_contributions(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        output: List[Dict[str, Any]] = []
        for effect in self.source_effects:
            lo, _ = self.source_ranges[effect.key]
            value = float(params[effect.key])
            contribution = effect.physical_coefficient * (value - lo)
            output.append(
                {
                    "source_key": effect.key,
                    "source_label": effect.label,
                    "source_value": round(value, 6),
                    "source_unit": effect.source_unit,
                    "direction": effect.direction,
                    "physical_coefficient": effect.physical_coefficient,
                    "target_contribution_from_generation_min": round(contribution, 6),
                    "target_unit": self.target_unit,
                }
            )
        return sorted(
            output,
            key=lambda item: abs(float(item["target_contribution_from_generation_min"])),
            reverse=True,
        )

    def summary(self) -> Dict[str, Any]:
        data = asdict(self)
        data["source_effects"] = [asdict(item) for item in self.source_effects]
        return data


class CouplingSystem:
    def __init__(
        self,
        project: ProjectDataset,
        models: Dict[str, MonotonicTargetModel],
        direction_only: Optional[Dict[str, List[Any]]] = None,
        local_context: bool = False,
        context_schemes: Optional[List[Any]] = None,
    ):
        self.project = project
        self.models = models
        self.direction_only = direction_only or {}
        self.local_context = bool(local_context)
        self.context_schemes = list(context_schemes) if context_schemes is not None else list(project.schemes)
        self._context_profile_cache: Dict[str, Dict[str, Dict[str, float]]] = {}
        self.edges_by_target: Dict[str, List[Any]] = {}
        for edge in project.couplings:
            self.edges_by_target.setdefault(edge.target_key, []).append(edge)

    @classmethod
    def fit(
        cls,
        project: ProjectDataset,
        ridge: float = 0.025,
        local_context: bool = False,
        expert_evidence: Optional[List[Dict[str, Any]]] = None,
    ) -> "CouplingSystem":
        by_key = project.attribute_by_key()
        grouped: Dict[str, List[Any]] = {}
        for edge in project.couplings:
            grouped.setdefault(edge.target_key, []).append(edge)
        models: Dict[str, Any] = {}
        direction_only: Dict[str, List[Any]] = {}
        for target_key, edges in grouped.items():
            target_spec = by_key[target_key]
            constraint_mode = coupling_constraint_mode(project, target_key)
            if not target_spec.is_numeric:
                continue
            source_specs = [by_key[edge.source_key] for edge in edges]
            if any(not item.is_numeric for item in source_specs):
                raise ProjectDataError(f"目标“{target_spec.label}”的耦合源必须都是数值属性。")
            if constraint_mode in {"minimum_frontier", "maximum_frontier", "hard_band"}:
                if not project.schemes:
                    direction_only[target_key] = edges
                    continue
                models[target_key] = fit_frontier_target(
                    project,
                    target_spec,
                    source_specs,
                    edges,
                    expert_evidence,
                    constraint_mode,
                )
                continue
            target_expert_count = sum(
                1
                for item in expert_evidence or []
                if item.get("status", "active") == "active"
                and (item.get("coupling_feedback") or {}).get("target_key") == target_key
                and (item.get("coupling_feedback") or {}).get("issue")
                in {
                    "target_low",
                    "target_high",
                    "mismatch",
                    "feasible_anchor",
                    "frontier_feasible",
                }
            )
            if len(project.schemes) < 2:
                direction_only[target_key] = edges
                continue
            models[target_key] = cls._fit_target(
                project,
                target_spec,
                source_specs,
                edges,
                ridge,
                expert_evidence=expert_evidence,
                constraint_mode=constraint_mode,
            )
        return cls(project, models, direction_only=direction_only, local_context=local_context)

    @staticmethod
    def _fit_target(
        project: ProjectDataset,
        target: AttributeSpec,
        sources: List[AttributeSpec],
        edges: List[Any],
        ridge: float,
        expert_evidence: Optional[List[Dict[str, Any]]] = None,
        constraint_mode: str = "expected_trend",
    ) -> MonotonicTargetModel:
        assert target.generation_min is not None and target.generation_max is not None
        source_ranges: Dict[str, List[float]] = {}
        rows: List[List[float]] = []
        y_values: List[float] = []
        for scheme in project.schemes:
            row = [1.0]
            for source in sources:
                assert source.generation_min is not None and source.generation_max is not None
                source_ranges[source.key] = [source.generation_min, source.generation_max]
                row.append(
                    clamp(
                        (float(scheme.params[source.key]) - source.generation_min)
                        / max(source.generation_max - source.generation_min, 1e-9),
                        0.0,
                        1.0,
                    )
                )
            rows.append(row)
            y_values.append(
                (float(scheme.params[target.key]) - target.generation_min)
                / max(target.generation_max - target.generation_min, 1e-9)
            )
        relevant_expert_count = sum(
            1
            for item in expert_evidence or []
            if item.get("status", "active") == "active"
            and (item.get("coupling_feedback") or {}).get("target_key") == target.key
        )
        if len(rows) < 2:
            raise ProjectDataError(
                f"目标“{target.label}”只有 {len(rows)} 个历史样本和 {relevant_expert_count} 条专家约束，"
                "至少需要两个历史样本才能建立正则化单调耦合代理。"
            )
        x = np.asarray(rows, dtype=float)
        y = np.asarray(y_values, dtype=float)
        regularizer = np.zeros((len(sources), len(sources) + 1), dtype=float)
        for index in range(len(sources)):
            regularizer[index, index + 1] = ridge**0.5
        lower = [-np.inf]
        upper = [np.inf]
        for edge in edges:
            if edge.direction == "positive":
                lower.append(0.0)
                upper.append(np.inf)
            else:
                lower.append(-np.inf)
                upper.append(0.0)
        coefficient_bounds = (np.asarray(lower), np.asarray(upper))

        def solve(extra_rows: List[np.ndarray], extra_targets: List[float], extra_weights: List[float]) -> Any:
            matrices: List[np.ndarray] = [x, regularizer]
            targets: List[np.ndarray] = [y, np.zeros(len(sources), dtype=float)]
            for row, value, weight in zip(extra_rows, extra_targets, extra_weights):
                scale = math.sqrt(max(float(weight), 0.05))
                matrices.append(np.asarray([row * scale], dtype=float))
                targets.append(np.asarray([float(value) * scale], dtype=float))
            return lsq_linear(
                np.vstack(matrices),
                np.concatenate(targets),
                bounds=coefficient_bounds,
                method="trf",
            )

        result = solve([], [], [])
        if not result.success:
            raise ProjectDataError(f"目标“{target.label}”的单调耦合代理拟合失败：{result.message}")
        coefficients = result.x
        active_constraints: List[Dict[str, Any]] = []
        for item in expert_evidence or []:
            if item.get("status", "active") != "active":
                continue
            feedback = item.get("coupling_feedback") or {}
            if feedback.get("target_key") != target.key:
                continue
            params = item.get("params") or {}
            if target.key not in params or any(source.key not in params for source in sources):
                continue
            issue = str(feedback.get("issue") or "")
            if issue not in {
                "target_low",
                "target_high",
                "mismatch",
                "feasible_anchor",
                "frontier_feasible",
            }:
                continue
            row = [1.0]
            for source in sources:
                source_lo, source_hi = source_ranges[source.key]
                row.append(
                    clamp(
                        (float(params[source.key]) - source_lo) / max(source_hi - source_lo, 1e-9),
                        0.0,
                        1.0,
                    )
                )
            actual = (
                float(params[target.key]) - float(target.generation_min)
            ) / max(float(target.generation_max) - float(target.generation_min), 1e-9)
            active_constraints.append(
                {
                    "row": np.asarray(row, dtype=float),
                    "actual": actual,
                    "issue": issue,
                    "label": item.get("label"),
                    "confidence": clamp(float(feedback.get("confidence", 0.82)), 0.35, 1.0),
                }
            )

        # Expert feasibility feedback is an inequality, not a fabricated exact
        # physical equation. Iteratively project only violated inequalities into
        # the monotone regression so controlled-variable answers can refine the
        # coupling strength without overpowering manufactured samples.
        initial_residuals = y - x @ coefficients
        initial_scale = max(
            1.4826 * float(np.median(np.abs(initial_residuals - np.median(initial_residuals)))),
            float(np.sqrt(np.mean(initial_residuals**2))),
            0.018,
        )
        for _iteration in range(4):
            extra_rows: List[np.ndarray] = []
            extra_targets: List[float] = []
            extra_weights: List[float] = []
            for constraint in active_constraints:
                row = constraint["row"]
                actual = float(constraint["actual"])
                predicted = float(row @ coefficients)
                issue = constraint["issue"]
                margin = max(0.012, 0.35 * initial_scale)
                desired: Optional[float] = None
                if issue == "frontier_feasible":
                    if constraint_mode == "minimum_frontier" and predicted > actual:
                        desired = actual
                    elif constraint_mode == "maximum_frontier" and predicted < actual:
                        desired = actual
                elif issue == "feasible_anchor":
                    desired = actual
                elif issue == "target_low" and predicted < actual + margin:
                    desired = actual + margin
                elif issue == "target_high" and predicted > actual - margin:
                    desired = actual - margin
                elif issue == "mismatch":
                    desired = actual + margin if actual <= predicted else actual - margin
                if desired is None:
                    continue
                extra_rows.append(row)
                extra_targets.append(desired)
                base_weight = 0.80 if issue == "feasible_anchor" else 1.65
                extra_weights.append(base_weight * float(constraint["confidence"]))
            if not extra_rows:
                break
            updated = solve(extra_rows, extra_targets, extra_weights)
            if not updated.success:
                break
            if float(np.linalg.norm(updated.x - coefficients)) < 1e-7:
                coefficients = updated.x
                break
            coefficients = updated.x

        predicted = x @ coefficients
        residuals = y - predicted
        rmse_normalized = float(np.sqrt(np.mean(residuals**2)))
        y_mean = float(np.mean(y))
        total = float(np.sum((y - y_mean) ** 2))
        r2 = 1.0 - float(np.sum(residuals**2)) / max(total, 1e-12)
        center = float(np.median(residuals))
        robust_sigma = 1.4826 * float(np.median(np.abs(residuals - center)))
        half_width = max(1.65 * max(robust_sigma, rmse_normalized), 0.035)
        lower_offset = center - half_width
        upper_offset = center + half_width
        coverage = float(np.mean((residuals >= lower_offset) & (residuals <= upper_offset)))
        target_span = target.generation_max - target.generation_min
        design_rows = [list(row[1:]) for row in x]
        design_rows.extend(list(item["row"][1:]) for item in active_constraints)
        design = np.asarray(design_rows, dtype=float)
        centered = design - np.mean(design, axis=0, keepdims=True)
        singular_values = np.linalg.svd(centered, compute_uv=False) if centered.size else np.asarray([])
        largest = float(singular_values[0]) if len(singular_values) else 0.0
        smallest = float(singular_values[-1]) if len(singular_values) else 0.0
        effective_rank = int(np.linalg.matrix_rank(centered)) if centered.size else 0
        condition_number = (
            largest / max(smallest, 1e-12)
            if largest > 1e-12
            else float("inf")
        )
        rank_ratio = effective_rank / max(len(sources), 1)
        condition_score = 0.0 if not math.isfinite(condition_number) else 1.0 / (1.0 + math.log10(max(condition_number, 1.0)))
        coverage_score = min(1.0, len(design_rows) / max(4.0 * len(sources), 4.0))
        identifiability_score = clamp(rank_ratio * condition_score * coverage_score, 0.0, 1.0)
        identifiability_label = (
            "强度较可辨识"
            if identifiability_score >= 0.58
            else "强度初步可辨识"
            if identifiability_score >= 0.30
            else "方向已知、强度不可辨识"
        )

        covariance_design = np.column_stack([np.ones(len(design_rows)), design])
        covariance = np.linalg.pinv(
            covariance_design.T @ covariance_design
            + np.diag([1e-8] + [max(ridge, 1e-6)] * len(sources))
        )
        interval_scale = max(rmse_normalized, 0.012) * (1.0 + 1.8 * (1.0 - identifiability_score))
        coefficient_errors = np.sqrt(np.maximum(np.diag(covariance), 0.0)) * interval_scale * 1.96
        effects: List[SourceEffect] = []
        for effect_index, (source, edge, coefficient) in enumerate(zip(sources, edges, coefficients[1:]), start=1):
            assert source.generation_min is not None and source.generation_max is not None
            source_span = source.generation_max - source.generation_min
            physical = float(coefficient) * target_span / max(source_span, 1e-9)
            error = float(coefficient_errors[effect_index])
            normalized_low = float(coefficient) - error
            normalized_high = float(coefficient) + error
            if edge.direction == "positive":
                normalized_low = max(0.0, normalized_low)
            else:
                normalized_high = min(0.0, normalized_high)
            physical_low = normalized_low * target_span / max(source_span, 1e-9)
            physical_high = normalized_high * target_span / max(source_span, 1e-9)
            effects.append(
                SourceEffect(
                    key=source.key,
                    label=source.label,
                    direction=edge.direction,
                    normalized_coefficient=round(float(coefficient), 8),
                    physical_coefficient=round(physical, 8),
                    source_unit=source.unit,
                    normalized_lower=round(normalized_low, 8),
                    normalized_upper=round(normalized_high, 8),
                    physical_lower=round(min(physical_low, physical_high), 8),
                    physical_upper=round(max(physical_low, physical_high), 8),
                )
            )
        confidence = (
            "高"
            if r2 >= 0.70 and len(rows) >= 25 and identifiability_score >= 0.58
            else "中"
            if r2 >= 0.35 and identifiability_score >= 0.25
            else "低"
        )
        return MonotonicTargetModel(
            target_key=target.key,
            target_label=target.label,
            target_unit=target.unit,
            source_effects=effects,
            intercept=round(float(coefficients[0]), 10),
            lower_offset=round(lower_offset, 10),
            upper_offset=round(upper_offset, 10),
            rmse=round(rmse_normalized * target_span, 6),
            r2=round(r2, 6),
            sample_count=len(rows),
            training_coverage=round(coverage, 6),
            confidence=confidence,
            target_min=target.generation_min,
            target_max=target.generation_max,
            source_ranges=source_ranges,
            identifiability_score=round(identifiability_score, 4),
            identifiability_label=identifiability_label,
            condition_number=(
                round(condition_number, 4) if math.isfinite(condition_number) else None
            ),
            effective_rank=effective_rank,
            expert_constraint_count=len(active_constraints),
            constraint_mode=constraint_mode,
        )

    @staticmethod
    def _percentile(values: List[float], fraction: float) -> float:
        ordered = sorted(float(value) for value in values)
        if not ordered:
            return 0.0
        position = clamp(float(fraction), 0.0, 1.0) * (len(ordered) - 1)
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return ordered[lower]
        ratio = position - lower
        return ordered[lower] * (1.0 - ratio) + ordered[upper] * ratio

    @staticmethod
    def _weighted_quantile(values: List[float], weights: List[float], fraction: float) -> float:
        rows = sorted(zip(values, weights), key=lambda item: item[0])
        total = sum(max(0.0, float(weight)) for _value, weight in rows)
        if not rows or total <= 1e-12:
            return 0.0
        threshold = clamp(float(fraction), 0.0, 1.0) * total
        running = 0.0
        for value, weight in rows:
            running += max(0.0, float(weight))
            if running >= threshold:
                return float(value)
        return float(rows[-1][0])

    def _context_profiles(self, target_key: str) -> Dict[str, Dict[str, float]]:
        cached = self._context_profile_cache.get(target_key)
        if cached is not None:
            return cached
        profiles: Dict[str, Dict[str, float]] = {}
        for edge in self.edges_by_target.get(target_key, []):
            values = [float(item.params[edge.source_key]) for item in self.context_schemes]
            positive = bool(values) and min(values) > 0.0
            use_log = positive and max(values) / max(min(values), 1e-12) >= 4.0
            transformed = [math.log(value) if use_log else value for value in values]
            center = self._percentile(transformed, 0.50)
            deviations = [abs(value - center) for value in transformed]
            mad = self._percentile(deviations, 0.50)
            q10 = self._percentile(transformed, 0.10)
            q90 = self._percentile(transformed, 0.90)
            full_span = max(transformed) - min(transformed) if transformed else 0.0
            data_scale = max(1.4826 * mad, (q90 - q10) / 2.56, full_span / 8.0, 1e-9)
            # For positive variables spanning several multiples, raw variance is
            # dominated by the gap between product specifications. A capped
            # log-ratio scale keeps nearby values transferable while making
            # cross-order-of-magnitude evidence decay quickly.
            scale = (
                max(min(data_scale, math.log(1.8)), math.log(1.15))
                if use_log
                else data_scale
            )
            profiles[edge.source_key] = {
                "center": center,
                "scale": scale,
                "use_log": 1.0 if use_log else 0.0,
            }
        self._context_profile_cache[target_key] = profiles
        return profiles

    @staticmethod
    def _context_value(value: Any, profile: Dict[str, float]) -> float:
        numeric = float(value)
        if profile.get("use_log") and numeric > 0.0:
            numeric = math.log(numeric)
        return (numeric - float(profile["center"])) / max(float(profile["scale"]), 1e-12)

    def context_distance(self, target_key: str, left: Dict[str, Any], right: Dict[str, Any]) -> float:
        profiles = self._context_profiles(target_key)
        distances: List[float] = []
        for edge in self.edges_by_target.get(target_key, []):
            if edge.source_key not in left or edge.source_key not in right:
                continue
            profile = profiles[edge.source_key]
            distances.append(
                self._context_value(left[edge.source_key], profile)
                - self._context_value(right[edge.source_key], profile)
            )
        if not distances:
            return 0.0
        return math.sqrt(sum(value * value for value in distances) / len(distances))

    def context_weight(self, target_key: str, left: Dict[str, Any], right: Dict[str, Any]) -> float:
        distance = self.context_distance(target_key, left, right)
        if distance >= 4.0:
            return 0.0
        return math.exp(-0.5 * (distance / 1.15) ** 2)

    def band(self, target_key: str, params: Dict[str, Any]) -> Dict[str, Any]:
        model = self.models[target_key]
        global_band = model.band(params)
        if getattr(model, "model_type", "") == "interval_monotone_frontier":
            return {
                **global_band,
                "model_scope": "partial_identification_frontier",
                "support_count": int(model.evidence_summary.get("total_constraints", 0)),
                "effective_support": float(model.evidence_summary.get("effective_constraints", 0.0)),
                "nearest_scheme_ids": [],
            }
        if not self.local_context or len(self.context_schemes) < 4:
            return {
                **global_band,
                "model_scope": "global_monotonic",
                "support_count": len(self.context_schemes),
                "effective_support": float(len(self.context_schemes)),
                "nearest_scheme_ids": [],
            }

        ranked: List[Tuple[float, Any]] = sorted(
            (
                (self.context_distance(target_key, params, scheme.params), scheme)
                for scheme in self.context_schemes
            ),
            key=lambda item: (item[0], str(item[1].id)),
        )
        source_count = len(self.edges_by_target.get(target_key, []))
        neighbor_count = min(
            len(ranked),
            max(4, source_count + 2, int(math.ceil(math.sqrt(len(ranked)))) + 1),
        )
        neighbors = ranked[:neighbor_count]
        bandwidth = max(neighbors[-1][0], 0.70)
        distances = [distance for distance, _scheme in neighbors]
        weights = [math.exp(-0.5 * (distance / bandwidth) ** 2) for distance in distances]
        residuals = [
            float(scheme.params[target_key]) - model.predict(scheme.params)
            for _distance, scheme in neighbors
        ]
        residual_center = self._weighted_quantile(residuals, weights, 0.50)
        absolute_residuals = [abs(value - residual_center) for value in residuals]
        local_half_width = 1.35 * self._weighted_quantile(absolute_residuals, weights, 0.85)
        total_weight = sum(weights)
        effective_support = total_weight * total_weight / max(sum(value * value for value in weights), 1e-12)
        reliability = clamp((effective_support - 1.5) / 3.5, 0.20, 1.0)
        global_center = float(global_band["predicted"])
        global_half_width = 0.5 * (float(global_band["upper"]) - float(global_band["lower"]))
        predicted = global_center + reliability * residual_center
        target_span = max(model.target_max - model.target_min, 1e-9)
        half_width = max(
            0.018 * target_span,
            reliability * local_half_width + (1.0 - reliability) * global_half_width,
            0.30 * global_half_width,
        )
        lower = clamp(predicted - half_width, model.target_min, model.target_max)
        upper = clamp(predicted + half_width, model.target_min, model.target_max)

        actual = float(params[target_key])
        exact_scheme = next(
            (
                scheme
                for _distance, scheme in ranked
                if all(
                    abs(float(params[key]) - float(scheme.params[key])) <= 1e-9
                    for key in params
                    if key in scheme.params
                    and isinstance(params[key], (int, float))
                    and isinstance(scheme.params[key], (int, float))
                )
                and all(
                    params[key] == scheme.params[key]
                    for key in params
                    if key in scheme.params
                    and not isinstance(params[key], (int, float))
                )
            ),
            None,
        )
        if exact_scheme is not None:
            lower = min(lower, actual)
            upper = max(upper, actual)
        return {
            "predicted": predicted,
            "lower": lower,
            "upper": upper,
            "model_scope": "local_context",
            "support_count": neighbor_count,
            "effective_support": effective_support,
            "nearest_scheme_ids": [str(scheme.id) for _distance, scheme in neighbors[:5]],
            "nearest_context_distance": distances[0] if distances else None,
        }

    def assess(self, params: Dict[str, Any]) -> List[TargetAssessment]:
        return [model.assess(params, self.band(key, params)) for key, model in self.models.items()]

    def summaries(self) -> List[Dict[str, Any]]:
        output = [model.summary() for model in self.models.values()]
        for item in output:
            item["context_model"] = (
                "partially_identified_monotone_frontier"
                if item.get("model_type") == "interval_monotone_frontier"
                else "local_robust_neighbor_band"
                if self.local_context
                else "global_monotonic"
            )
        by_key = self.project.attribute_by_key()
        for target_key, edges in self.direction_only.items():
            target = by_key[target_key]
            output.append(
                {
                    "target_key": target_key,
                    "target_label": target.label,
                    "target_unit": target.unit,
                    "source_effects": [
                        {
                            "key": edge.source_key,
                            "label": edge.source_label,
                            "direction": edge.direction,
                            "normalized_coefficient": None,
                            "physical_coefficient": None,
                            "source_unit": by_key[edge.source_key].unit,
                        }
                        for edge in edges
                    ],
                    "intercept": None,
                    "lower_offset": None,
                    "upper_offset": None,
                    "rmse": None,
                    "r2": None,
                    "sample_count": len(self.project.schemes),
                    "training_coverage": None,
                    "confidence": "方向已知、强度待学习",
                    "identifiability_score": 0.0,
                    "identifiability_label": "方向已知、强度不可辨识",
                    "condition_number": None,
                    "effective_rank": 0,
                    "expert_constraint_count": 0,
                    "constraint_mode": coupling_constraint_mode(self.project, target_key),
                    "model_status": "direction_only",
                    "required_samples_for_point_fit": len(edges) + 2,
                    "target_min": target.generation_min,
                    "target_max": target.generation_max,
                }
            )
        return output

    def target_keys(self) -> set[str]:
        return set(self.edges_by_target)

    def constraint_mode(self, target_key: str) -> str:
        model = self.models.get(target_key)
        if model is not None:
            return model.constraint_mode
        return coupling_constraint_mode(self.project, target_key)

    def is_feasibility_violation(self, target_key: str, status: str) -> bool:
        mode = self.constraint_mode(target_key)
        return bool(
            (status == "below_band" and mode in {"minimum_frontier", "hard_band"})
            or (status == "above_band" and mode in {"maximum_frontier", "hard_band"})
        )

    def probe_definition(self, target_key: str) -> Dict[str, Any]:
        """Return declared directions for a low-burden controlled coupling probe."""
        by_key = self.project.attribute_by_key()
        edges = self.edges_by_target.get(target_key, [])
        target = by_key[target_key]
        return {
            "target_key": target_key,
            "target_label": target.label,
            "target_unit": target.unit,
            "model_status": "fitted" if target_key in self.models else "direction_only",
            "constraint_mode": self.constraint_mode(target_key),
            "sources": [
                {
                    "key": edge.source_key,
                    "label": edge.source_label,
                    "unit": by_key[edge.source_key].unit,
                    "direction": edge.direction,
                }
                for edge in edges
            ],
        }
