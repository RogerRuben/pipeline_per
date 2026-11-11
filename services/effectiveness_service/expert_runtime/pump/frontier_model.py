# -*- coding: utf-8 -*-
"""Partially identified monotone feasibility frontiers for small, dirty datasets."""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy.optimize import linprog

from project_excel import AttributeSpec, ProjectDataError, ProjectDataset


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass
class FrontierSourceEffect:
    key: str
    label: str
    direction: str
    normalized_coefficient: float
    physical_coefficient: float
    source_unit: str
    normalized_lower: float
    normalized_upper: float
    physical_lower: float
    physical_upper: float


@dataclass
class FrontierAssessment:
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
    model_scope: str = "partial_identification_frontier"
    local_support_count: int = 0
    local_effective_support: float = 0.0
    nearest_scheme_ids: Optional[List[str]] = None
    constraint_mode: str = "minimum_frontier"
    feasibility_relevant: bool = True
    engineering_interpretation: str = ""
    feasibility_probability: float = 0.5
    frontier_point: Optional[float] = None
    frontier_lower: Optional[float] = None
    frontier_upper: Optional[float] = None
    uncertainty_width: Optional[float] = None
    evidence_strength: str = "low"


@dataclass
class FrontierObservation:
    params: Dict[str, Any]
    target_value: float
    label: str
    weight: float
    source: str
    evidence_id: str


@dataclass
class FrontierSideModel:
    side: str
    point_coefficients: List[float]
    ensemble_coefficients: List[List[float]]
    optimum_slack: float
    feasible_count: int
    infeasible_count: int
    observation_count: int
    source_signs: List[int]


@dataclass
class FrontierTargetModel:
    target_key: str
    target_label: str
    target_unit: str
    source_effects: List[FrontierSourceEffect]
    source_ranges: Dict[str, List[float]]
    target_min: float
    target_max: float
    constraint_mode: str
    lower_side: Optional[FrontierSideModel]
    upper_side: Optional[FrontierSideModel]
    sample_count: int
    expert_constraint_count: int
    confidence: str
    identifiability_score: float
    identifiability_label: str
    evidence_summary: Dict[str, Any]
    model_type: str = "interval_monotone_frontier"
    model_status: str = "frontier_interval"
    r2: Optional[float] = None
    rmse: Optional[float] = None
    training_coverage: Optional[float] = None
    condition_number: Optional[float] = None
    effective_rank: int = 0
    intercept: Optional[float] = None
    lower_offset: Optional[float] = None
    upper_offset: Optional[float] = None

    def normalized_row(self, params: Dict[str, Any]) -> np.ndarray:
        row = [1.0]
        for effect in self.source_effects:
            lo, hi = self.source_ranges[effect.key]
            row.append(clamp((float(params[effect.key]) - lo) / max(hi - lo, 1e-9), 0.0, 1.0))
        return np.asarray(row, dtype=float)

    def _original_target(self, oriented: float, side: str) -> float:
        span = max(self.target_max - self.target_min, 1e-9)
        z = clamp(float(oriented), 0.0, 1.0)
        if side == "maximum":
            z = 1.0 - z
        return self.target_min + z * span

    def side_interval(self, side_model: FrontierSideModel, params: Dict[str, Any]) -> Dict[str, float]:
        row = self.normalized_row(params)
        point_oriented = float(row @ np.asarray(side_model.point_coefficients, dtype=float))
        candidates = [
            float(row @ np.asarray(coefficients, dtype=float))
            for coefficients in side_model.ensemble_coefficients
        ] or [point_oriented]
        original = [self._original_target(value, side_model.side) for value in candidates]
        point = self._original_target(point_oriented, side_model.side)
        return {
            "point": point,
            "lower": min(original),
            "upper": max(original),
            "width": max(original) - min(original),
        }

    def band(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self.constraint_mode == "minimum_frontier" and self.lower_side is not None:
            interval = self.side_interval(self.lower_side, params)
            return {
                "predicted": interval["point"],
                "lower": interval["lower"],
                "upper": interval["upper"],
                "frontier_point": interval["point"],
                "frontier_lower": interval["lower"],
                "frontier_upper": interval["upper"],
                "uncertainty_width": interval["width"],
                "feasible_side": "above",
                "model_scope": "partial_identification_frontier",
            }
        if self.constraint_mode == "maximum_frontier" and self.upper_side is not None:
            interval = self.side_interval(self.upper_side, params)
            return {
                "predicted": interval["point"],
                "lower": interval["lower"],
                "upper": interval["upper"],
                "frontier_point": interval["point"],
                "frontier_lower": interval["lower"],
                "frontier_upper": interval["upper"],
                "uncertainty_width": interval["width"],
                "feasible_side": "below",
                "model_scope": "partial_identification_frontier",
            }
        if self.constraint_mode == "hard_band" and self.lower_side is not None and self.upper_side is not None:
            low = self.side_interval(self.lower_side, params)
            high = self.side_interval(self.upper_side, params)
            return {
                "predicted": 0.5 * (low["point"] + high["point"]),
                "lower": low["point"],
                "upper": high["point"],
                "frontier_point": 0.5 * (low["point"] + high["point"]),
                "frontier_lower": low["lower"],
                "frontier_upper": high["upper"],
                "lower_frontier": low,
                "upper_frontier": high,
                "uncertainty_width": low["width"] + high["width"],
                "feasible_side": "between",
                "model_scope": "partial_identification_frontier",
            }
        raise ProjectDataError(f"目标“{self.target_label}”的前沿模型不完整。")

    def predict(self, params: Dict[str, Any]) -> float:
        """Compatibility point estimate for callers written for the V14 proxy."""
        return float(self.band(params)["frontier_point"])

    def _one_sided_assessment(self, params: Dict[str, Any], band: Dict[str, Any]) -> FrontierAssessment:
        actual = float(params[self.target_key])
        point = float(band["frontier_point"])
        lower = float(band["frontier_lower"])
        upper = float(band["frontier_upper"])
        span = max(self.target_max - self.target_min, 1e-9)
        width = max(upper - lower, 0.015 * span)
        if self.constraint_mode == "minimum_frontier":
            if actual < lower:
                status = "below_band"
                probability = 0.03 * math.exp(-3.0 * (lower - actual) / span)
                distance = lower - actual
                interpretation = "低于当前证据能够支持的最低可实现前沿"
                message = (
                    f"当前{self.target_label}为 {actual:.4g} {self.target_unit}，低于最低可实现前沿的"
                    f"保守下界 {lower:.4g}；当前前沿估计 {point:.4g}，不确定区间"
                    f"[{lower:.4g}, {upper:.4g}]。"
                )
            elif actual >= upper:
                excess = actual - upper
                inefficient = excess > max(0.08 * span, 0.55 * width)
                status = "feasible_inefficient" if inefficient else "within_band"
                probability = 0.97
                distance = excess
                interpretation = (
                    "物理可行，但明显远离最低可实现前沿，联动效率偏低"
                    if inefficient
                    else "位于最低前沿的明确可实现一侧"
                )
                message = (
                    f"当前{self.target_label}为 {actual:.4g} {self.target_unit}，高于最低前沿不确定区间"
                    f"上界 {upper:.4g}；物理上可以实现，"
                    + ("但相对当前上游条件偏高，属于保守或低效率设计。" if inefficient else "且仍接近当前可实现前沿。")
                )
            else:
                status = "near_boundary"
                ratio = clamp((actual - lower) / width, 0.0, 1.0)
                probability = 0.12 + 0.76 * ratio
                distance = min(actual - lower, upper - actual)
                interpretation = "位于尚未夹紧的最低可实现前沿区间"
                message = (
                    f"当前{self.target_label}为 {actual:.4g} {self.target_unit}，落在最低前沿"
                    f"不确定区间 [{lower:.4g}, {upper:.4g}] 内，需要针对性夹逼。"
                )
        else:
            if actual > upper:
                status = "above_band"
                probability = 0.03 * math.exp(-3.0 * (actual - upper) / span)
                distance = actual - upper
                interpretation = "高于当前证据能够支持的最高可实现前沿"
                message = (
                    f"当前{self.target_label}为 {actual:.4g} {self.target_unit}，高于最高可实现前沿的"
                    f"保守上界 {upper:.4g}；当前前沿估计 {point:.4g}，不确定区间"
                    f"[{lower:.4g}, {upper:.4g}]。"
                )
            elif actual <= lower:
                excess = lower - actual
                inefficient = excess > max(0.08 * span, 0.55 * width)
                status = "feasible_inefficient" if inefficient else "within_band"
                probability = 0.97
                distance = excess
                interpretation = (
                    "物理可行，但明显远离最高可实现前沿，联动效率偏低"
                    if inefficient
                    else "位于最高前沿的明确可实现一侧"
                )
                message = (
                    f"当前{self.target_label}为 {actual:.4g} {self.target_unit}，低于最高可实现前沿"
                    f"不确定区间下界 {lower:.4g}；物理上可以实现，"
                    + ("但相对当前上游条件偏低，属于保守或低效率设计。" if inefficient else "且仍接近当前可实现前沿。")
                )
            else:
                status = "near_boundary"
                ratio = clamp((upper - actual) / width, 0.0, 1.0)
                probability = 0.12 + 0.76 * ratio
                distance = min(actual - lower, upper - actual)
                interpretation = "位于尚未夹紧的最高可实现前沿区间"
                message = (
                    f"当前{self.target_label}为 {actual:.4g} {self.target_unit}，落在最高前沿"
                    f"不确定区间 [{lower:.4g}, {upper:.4g}] 内，需要针对性夹逼。"
                )
        severity = clamp(1.0 - probability, 0.03, 1.0)
        return FrontierAssessment(
            target_key=self.target_key,
            target_label=self.target_label,
            target_unit=self.target_unit,
            actual=round(actual, 6),
            predicted=round(point, 6),
            lower=round(lower, 6),
            upper=round(upper, 6),
            residual=round(actual - point, 6),
            status=status,
            boundary_distance=round(distance, 6),
            severity=round(severity, 3),
            message=message,
            source_contributions=self.source_contributions(params),
            local_support_count=int(self.evidence_summary.get("total_constraints", 0)),
            local_effective_support=float(self.evidence_summary.get("effective_constraints", 0.0)),
            constraint_mode=self.constraint_mode,
            feasibility_relevant=status in {"below_band", "above_band", "near_boundary"},
            engineering_interpretation=interpretation,
            feasibility_probability=round(clamp(probability, 0.0, 1.0), 4),
            frontier_point=round(point, 6),
            frontier_lower=round(lower, 6),
            frontier_upper=round(upper, 6),
            uncertainty_width=round(upper - lower, 6),
            evidence_strength=self.confidence,
        )

    def assess(self, params: Dict[str, Any], band_override: Optional[Dict[str, Any]] = None) -> FrontierAssessment:
        band = band_override or self.band(params)
        if self.constraint_mode in {"minimum_frontier", "maximum_frontier"}:
            return self._one_sided_assessment(params, band)

        actual = float(params[self.target_key])
        low = band["lower_frontier"]
        high = band["upper_frontier"]
        lower_clear = float(low["lower"])
        lower_safe = float(low["upper"])
        upper_safe = float(high["lower"])
        upper_clear = float(high["upper"])
        if actual < lower_clear:
            status, probability = "below_band", 0.03
            message = f"{self.target_label}低于双侧硬带的最低可能下界 {lower_clear:.4g}。"
        elif actual > upper_clear:
            status, probability = "above_band", 0.03
            message = f"{self.target_label}高于双侧硬带的最高可能上界 {upper_clear:.4g}。"
        elif lower_safe <= actual <= upper_safe:
            status, probability = "within_band", 0.97
            message = f"{self.target_label}位于双侧硬带的明确可实现区域。"
        else:
            status, probability = "near_boundary", 0.5
            message = f"{self.target_label}位于双侧硬带尚未夹紧的边界区域。"
        return FrontierAssessment(
            target_key=self.target_key,
            target_label=self.target_label,
            target_unit=self.target_unit,
            actual=round(actual, 6),
            predicted=round(float(band["predicted"]), 6),
            lower=round(float(band["lower"]), 6),
            upper=round(float(band["upper"]), 6),
            residual=round(actual - float(band["predicted"]), 6),
            status=status,
            boundary_distance=round(min(abs(actual - lower_safe), abs(actual - upper_safe)), 6),
            severity=round(1.0 - probability, 3),
            message=message,
            source_contributions=self.source_contributions(params),
            local_support_count=int(self.evidence_summary.get("total_constraints", 0)),
            local_effective_support=float(self.evidence_summary.get("effective_constraints", 0.0)),
            constraint_mode=self.constraint_mode,
            feasibility_relevant=status != "within_band",
            engineering_interpretation="双侧物理硬带判断",
            feasibility_probability=probability,
            frontier_point=round(float(band["predicted"]), 6),
            frontier_lower=round(lower_clear, 6),
            frontier_upper=round(upper_clear, 6),
            uncertainty_width=round(float(band["uncertainty_width"]), 6),
            evidence_strength=self.confidence,
        )

    def source_contributions(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        output = []
        for effect in self.source_effects:
            lo, _hi = self.source_ranges[effect.key]
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
                    "physical_lower": effect.physical_lower,
                    "physical_upper": effect.physical_upper,
                    "target_contribution_from_generation_min": round(contribution, 6),
                    "target_unit": self.target_unit,
                }
            )
        return sorted(output, key=lambda item: abs(float(item["target_contribution_from_generation_min"])), reverse=True)

    def summary(self) -> Dict[str, Any]:
        data = asdict(self)
        data["source_effects"] = [asdict(item) for item in self.source_effects]
        data["frontier_uncertainty_width"] = self.evidence_summary.get("median_uncertainty_width")
        data["feasible_anchor_count"] = self.evidence_summary.get("feasible_constraints", 0)
        data["infeasible_anchor_count"] = self.evidence_summary.get("infeasible_constraints", 0)
        data["ensemble_size"] = self.evidence_summary.get("ensemble_size", 0)
        data["conflict_slack"] = self.evidence_summary.get("conflict_slack", 0.0)
        return data


def _normalized_target(value: float, target: AttributeSpec, side: str) -> float:
    assert target.generation_min is not None and target.generation_max is not None
    z = clamp(
        (float(value) - float(target.generation_min))
        / max(float(target.generation_max) - float(target.generation_min), 1e-9),
        0.0,
        1.0,
    )
    return 1.0 - z if side == "maximum" else z


def _normalized_row(params: Dict[str, Any], sources: Sequence[AttributeSpec]) -> np.ndarray:
    row = [1.0]
    for source in sources:
        assert source.generation_min is not None and source.generation_max is not None
        row.append(
            clamp(
                (float(params[source.key]) - float(source.generation_min))
                / max(float(source.generation_max) - float(source.generation_min), 1e-9),
                0.0,
                1.0,
            )
        )
    return np.asarray(row, dtype=float)


def _side_observations(
    project: ProjectDataset,
    target: AttributeSpec,
    evidence: Sequence[Dict[str, Any]],
    side: str,
) -> List[FrontierObservation]:
    rows = [
        FrontierObservation(
            params=dict(scheme.params),
            target_value=float(scheme.params[target.key]),
            label="feasible",
            weight=1.35,
            source="verified_historical",
            evidence_id=str(scheme.id),
        )
        for scheme in project.schemes
    ]
    for item in evidence:
        if item.get("status", "active") != "active" or item.get("label") not in {"feasible", "infeasible"}:
            continue
        params = item.get("params") or {}
        if target.key not in params:
            continue
        feedback = item.get("coupling_feedback") or {}
        if item.get("label") == "feasible":
            explicit = feedback.get("target_key") == target.key
            frontier = feedback.get("issue") == "frontier_feasible"
            rows.append(
                FrontierObservation(
                    params=dict(params),
                    target_value=float(params[target.key]),
                    label="feasible",
                    weight=2.0 if frontier else 1.15 if explicit else 0.72,
                    source="expert_frontier" if frontier else "expert_feasible",
                    evidence_id=str(item.get("id") or "expert"),
                )
            )
            continue
        if feedback.get("target_key") != target.key:
            continue
        issue = str(feedback.get("issue") or "")
        matches = issue == ("target_low" if side == "minimum" else "target_high")
        if not matches and not (issue == "mismatch" and feedback.get("constraint_mode") == "hard_band"):
            continue
        rows.append(
            FrontierObservation(
                params=dict(params),
                target_value=float(params[target.key]),
                label="infeasible",
                weight=2.4 * clamp(float(feedback.get("confidence", 0.82)), 0.45, 1.0),
                source="expert_infeasible",
                evidence_id=str(item.get("id") or "expert"),
            )
        )
    return rows


def _fit_frontier_side(
    project: ProjectDataset,
    target: AttributeSpec,
    sources: Sequence[AttributeSpec],
    edges: Sequence[Any],
    observations: Sequence[FrontierObservation],
    side: str,
) -> FrontierSideModel:
    if not observations:
        raise ProjectDataError(f"目标“{target.label}”没有任何可用于前沿学习的锚点。")
    beta_count = len(sources) + 1
    variable_count = beta_count + len(observations)
    signs = []
    for edge in edges:
        sign = 1 if edge.direction == "positive" else -1
        if side == "maximum":
            sign *= -1
        signs.append(sign)
    bounds: List[Tuple[Optional[float], Optional[float]]] = [(0.0, 1.0)]
    bounds.extend((0.0, 1.25) if sign > 0 else (-1.25, 0.0) for sign in signs)
    bounds.extend((0.0, 1.5) for _item in observations)
    rows = [_normalized_row(item.params, sources) for item in observations]
    targets = [_normalized_target(item.target_value, target, side) for item in observations]
    margin = 0.018
    a_ub: List[List[float]] = []
    b_ub: List[float] = []
    for index, (item, row, value) in enumerate(zip(observations, rows, targets)):
        constraint = [0.0] * variable_count
        if item.label == "feasible":
            constraint[:beta_count] = row.tolist()
            constraint[beta_count + index] = -1.0
            limit = value
        else:
            constraint[:beta_count] = (-row).tolist()
            constraint[beta_count + index] = -1.0
            limit = -(value + margin)
        a_ub.append(constraint)
        b_ub.append(limit)
    min_corner = np.asarray([1.0] + [0.0 if sign > 0 else 1.0 for sign in signs], dtype=float)
    max_corner = np.asarray([1.0] + [1.0 if sign > 0 else 0.0 for sign in signs], dtype=float)
    low_constraint = [0.0] * variable_count
    low_constraint[:beta_count] = (-min_corner).tolist()
    a_ub.append(low_constraint)
    b_ub.append(0.0)
    high_constraint = [0.0] * variable_count
    high_constraint[:beta_count] = max_corner.tolist()
    a_ub.append(high_constraint)
    b_ub.append(1.0)

    slack_weights = np.asarray([max(0.4, item.weight) for item in observations], dtype=float)
    slack_objective = np.zeros(variable_count, dtype=float)
    slack_objective[beta_count:] = slack_weights
    slack_solution = linprog(
        slack_objective,
        A_ub=np.asarray(a_ub, dtype=float),
        b_ub=np.asarray(b_ub, dtype=float),
        bounds=bounds,
        method="highs",
    )
    if not slack_solution.success:
        raise ProjectDataError(f"目标“{target.label}”的{side}前沿约束求解失败：{slack_solution.message}")
    optimum_slack = float(slack_objective @ slack_solution.x)

    mean_row = np.mean(np.vstack(rows), axis=0)
    point_objective = np.zeros(variable_count, dtype=float)
    point_objective[:beta_count] = -0.34 * mean_row
    for index, sign in enumerate(signs, start=1):
        point_objective[index] += 0.018 if sign > 0 else -0.018
    point_objective[beta_count:] = 9.0 * slack_weights
    point_solution = linprog(
        point_objective,
        A_ub=np.asarray(a_ub, dtype=float),
        b_ub=np.asarray(b_ub, dtype=float),
        bounds=bounds,
        method="highs",
    )
    if not point_solution.success:
        point_solution = slack_solution

    ensemble_a = list(a_ub)
    ensemble_b = list(b_ub)
    budget = [0.0] * variable_count
    budget[beta_count:] = slack_weights.tolist()
    ensemble_a.append(budget)
    ensemble_b.append(optimum_slack + 0.012 + 0.002 * len(observations))
    seed_text = f"{project.learning_fingerprint}:{target.key}:{side}:{len(observations)}"
    seed = int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest()[:12], 16)
    rng = random.Random(seed)
    objectives: List[np.ndarray] = []
    for index in range(beta_count):
        for direction in (-1.0, 1.0):
            objective = np.zeros(variable_count, dtype=float)
            objective[index] = direction
            objectives.append(objective)
    for _index in range(28):
        objective = np.zeros(variable_count, dtype=float)
        vector = np.asarray([rng.uniform(-1.0, 1.0) for _ in range(beta_count)], dtype=float)
        norm = float(np.linalg.norm(vector))
        objective[:beta_count] = vector / max(norm, 1e-9)
        objectives.append(objective)
    ensemble: List[List[float]] = [point_solution.x[:beta_count].tolist()]
    for objective in objectives:
        solution = linprog(
            objective,
            A_ub=np.asarray(ensemble_a, dtype=float),
            b_ub=np.asarray(ensemble_b, dtype=float),
            bounds=bounds,
            method="highs",
        )
        if not solution.success:
            continue
        coefficients = solution.x[:beta_count].tolist()
        if not any(np.max(np.abs(np.asarray(coefficients) - np.asarray(item))) < 1e-6 for item in ensemble):
            ensemble.append(coefficients)
    return FrontierSideModel(
        side=side,
        point_coefficients=point_solution.x[:beta_count].tolist(),
        ensemble_coefficients=ensemble,
        optimum_slack=round(optimum_slack, 8),
        feasible_count=sum(item.label == "feasible" for item in observations),
        infeasible_count=sum(item.label == "infeasible" for item in observations),
        observation_count=len(observations),
        source_signs=signs,
    )


def fit_frontier_target(
    project: ProjectDataset,
    target: AttributeSpec,
    sources: Sequence[AttributeSpec],
    edges: Sequence[Any],
    expert_evidence: Optional[Sequence[Dict[str, Any]]],
    constraint_mode: str,
) -> FrontierTargetModel:
    assert target.generation_min is not None and target.generation_max is not None
    evidence = list(expert_evidence or [])
    lower_side = None
    upper_side = None
    if constraint_mode in {"minimum_frontier", "hard_band"}:
        lower_side = _fit_frontier_side(
            project,
            target,
            sources,
            edges,
            _side_observations(project, target, evidence, "minimum"),
            "minimum",
        )
    if constraint_mode in {"maximum_frontier", "hard_band"}:
        upper_side = _fit_frontier_side(
            project,
            target,
            sources,
            edges,
            _side_observations(project, target, evidence, "maximum"),
            "maximum",
        )
    side_models = [item for item in (lower_side, upper_side) if item is not None]
    source_ranges = {
        source.key: [float(source.generation_min), float(source.generation_max)]
        for source in sources
    }
    span = max(float(target.generation_max) - float(target.generation_min), 1e-9)
    reference_params = [scheme.params for scheme in project.schemes]
    reference_params.extend(
        item.get("params") or {}
        for item in evidence
        if item.get("status", "active") == "active" and target.key in (item.get("params") or {})
    )
    widths = []
    for params in reference_params:
        if not all(source.key in params for source in sources):
            continue
        for side_model in side_models:
            row = _normalized_row(params, sources)
            predictions = []
            for coefficients in side_model.ensemble_coefficients:
                oriented = float(row @ np.asarray(coefficients, dtype=float))
                z = 1.0 - oriented if side_model.side == "maximum" else oriented
                predictions.append(float(target.generation_min) + clamp(z, 0.0, 1.0) * span)
            if predictions:
                widths.append((max(predictions) - min(predictions)) / span)
    median_width = float(np.median(widths)) if widths else 1.0
    infeasible_count = sum(item.infeasible_count for item in side_models)
    feasible_count = sum(item.feasible_count for item in side_models)
    conflict_slack = sum(item.optimum_slack for item in side_models)
    evidence_factor = min(1.0, infeasible_count / max(2.0 * len(sources), 2.0))
    identifiability = clamp((1.0 - median_width) * (0.30 + 0.70 * evidence_factor), 0.0, 1.0)
    confidence = "高" if identifiability >= 0.70 and conflict_slack < 0.03 else "中" if identifiability >= 0.38 else "低"
    identifiability_label = (
        "前沿已较充分夹紧"
        if identifiability >= 0.70
        else "前沿正在收缩"
        if identifiability >= 0.38
        else "方向已知、前沿仍宽"
    )
    primary = lower_side or upper_side
    assert primary is not None
    point_coefficients = np.asarray(primary.point_coefficients, dtype=float)
    ensemble_coefficients = [np.asarray(item, dtype=float) for item in primary.ensemble_coefficients]
    effects: List[FrontierSourceEffect] = []
    for index, (source, edge) in enumerate(zip(sources, edges), start=1):
        source_span = max(float(source.generation_max) - float(source.generation_min), 1e-9)
        orientation = -1.0 if primary.side == "maximum" else 1.0
        point = orientation * float(point_coefficients[index])
        candidates = [orientation * float(item[index]) for item in ensemble_coefficients]
        physical = point * span / source_span
        physical_candidates = [item * span / source_span for item in candidates]
        effects.append(
            FrontierSourceEffect(
                key=source.key,
                label=source.label,
                direction=edge.direction,
                normalized_coefficient=round(point, 8),
                physical_coefficient=round(physical, 8),
                source_unit=source.unit,
                normalized_lower=round(min(candidates), 8),
                normalized_upper=round(max(candidates), 8),
                physical_lower=round(min(physical_candidates), 8),
                physical_upper=round(max(physical_candidates), 8),
            )
        )
    expert_count = sum(
        item.get("status", "active") == "active"
        and item.get("label") in {"feasible", "infeasible"}
        for item in evidence
    )
    return FrontierTargetModel(
        target_key=target.key,
        target_label=target.label,
        target_unit=target.unit,
        source_effects=effects,
        source_ranges=source_ranges,
        target_min=float(target.generation_min),
        target_max=float(target.generation_max),
        constraint_mode=constraint_mode,
        lower_side=lower_side,
        upper_side=upper_side,
        sample_count=len(project.schemes),
        expert_constraint_count=expert_count,
        confidence=confidence,
        identifiability_score=round(identifiability, 4),
        identifiability_label=identifiability_label,
        evidence_summary={
            "historical_anchors": len(project.schemes),
            "expert_evidence": expert_count,
            "feasible_constraints": feasible_count,
            "infeasible_constraints": infeasible_count,
            "total_constraints": feasible_count + infeasible_count,
            "effective_constraints": round(feasible_count * 0.35 + infeasible_count, 3),
            "ensemble_size": sum(len(item.ensemble_coefficients) for item in side_models),
            "median_uncertainty_width": round(median_width * span, 6),
            "normalized_uncertainty_width": round(median_width, 6),
            "conflict_slack": round(conflict_slack, 8),
            "data_warning": (
                "历史样本与耦合趋势不一致或数量很少；仅作为已实现可行锚点，不作为物理方程等值样本。"
                if len(project.schemes) <= len(sources) + 1 or identifiability < 0.38
                else "当前证据能够初步约束前沿。"
            ),
        },
        intercept=round(float(point_coefficients[0]), 10),
        effective_rank=min(len(project.schemes) + infeasible_count, len(sources)),
    )
