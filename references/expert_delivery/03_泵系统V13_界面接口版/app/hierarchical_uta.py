# -*- coding: utf-8 -*-
"""Requirement-aware two-channel hierarchical LP-UTA for system scoring."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.optimize import Bounds, LinearConstraint, linprog

try:
    from scipy.optimize import milp
except ImportError:  # SciPy < 1.9 keeps V11 usable without M3.
    milp = None

from system_excel import SystemProjectDataset, SystemRequirementSpec, SystemTask


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def sigmoid(value: float) -> float:
    value = clamp(value, -35.0, 35.0)
    return 1.0 / (1.0 + math.exp(-value))


def smooth_pchip_points(
    points: Sequence[Dict[str, Any]],
    x_key: str,
    value_keys: Sequence[str],
    count: int = 121,
) -> List[Dict[str, float]]:
    """Fit shape-preserving curves for presentation without changing scoring."""
    ordered = sorted(points, key=lambda item: float(item[x_key]))
    unique: List[Dict[str, Any]] = []
    for item in ordered:
        if unique and math.isclose(float(unique[-1][x_key]), float(item[x_key]), abs_tol=1e-12):
            unique[-1] = item
        else:
            unique.append(item)
    if len(unique) < 2:
        return [
            {x_key: float(item[x_key]), **{key: float(item[key]) for key in value_keys}}
            for item in unique
        ]
    x_values = np.asarray([float(item[x_key]) for item in unique], dtype=float)
    sample_x = np.linspace(float(x_values[0]), float(x_values[-1]), max(41, int(count)))
    output = [{x_key: round(float(value), 8)} for value in sample_x]
    for key in value_keys:
        y_values = np.asarray([float(item[key]) for item in unique], dtype=float)
        if len(unique) >= 3:
            sample_y = PchipInterpolator(x_values, y_values)(sample_x)
        else:
            sample_y = np.interp(sample_x, x_values, y_values)
        for row, value in zip(output, sample_y):
            row[key] = round(float(value), 6)
    return output


class SystemDirectScorer:
    """Aggregate generic product-to-system mappings and score task requirements."""

    def __init__(self, project: SystemProjectDataset, task: SystemTask):
        self.project = project
        self.task = task
        self.attribute_by_key = project.product.attribute_by_key()
        self.direct = project.direct_mappings()
        self.system_by_key = project.system_attribute_by_key()
        self.mappings_by_system: Dict[str, List[Any]] = {}
        for mapping in self.direct:
            self.mappings_by_system.setdefault(mapping.system_key, []).append(mapping)
        self.history_by_system: Dict[str, List[float]] = {
            key: [self.mapped_values(item.params)[key] for item in project.product.schemes]
            for key in self.mappings_by_system
        }

    @staticmethod
    def _target_score(value: float, target: float, lower_tolerance: float, upper_tolerance: float) -> float:
        distance = lower_tolerance if value < target else upper_tolerance
        ratio = abs(value - target) / max(distance, 1e-12)
        return SystemDirectScorer._smooth_requirement_score(ratio)

    @staticmethod
    def _smooth_requirement_score(normalized_distance: float) -> float:
        """Return 100 at the requirement and 80 at one configured distance.

        The rational tail remains strictly ordered instead of clipping all
        sufficiently distant schemes to zero.
        """
        distance = max(0.0, float(normalized_distance))
        return 100.0 / (1.0 + 0.25 * distance * distance)

    @staticmethod
    def _at_least_score(
        value: float,
        minimum: float,
        penalty_distance: float,
        cap: float,
        bonus: bool,
        bonus_distance: Optional[float] = None,
    ) -> float:
        if value < minimum:
            return SystemDirectScorer._smooth_requirement_score(
                (minimum - value) / max(penalty_distance, 1e-12)
            )
        if not bonus:
            return 100.0
        gain = (value - minimum) / max(
            penalty_distance if bonus_distance is None else bonus_distance,
            1e-12,
        )
        return 100.0 + max(0.0, cap - 100.0) * (1.0 - math.exp(-0.5 * gain))

    @staticmethod
    def _at_most_score(
        value: float,
        maximum: float,
        penalty_distance: float,
        cap: float,
        bonus: bool,
        bonus_distance: Optional[float] = None,
    ) -> float:
        if value > maximum:
            return SystemDirectScorer._smooth_requirement_score(
                (value - maximum) / max(penalty_distance, 1e-12)
            )
        if not bonus:
            return 100.0
        saving = (maximum - value) / max(
            penalty_distance if bonus_distance is None else bonus_distance,
            1e-12,
        )
        return 100.0 + max(0.0, cap - 100.0) * (1.0 - math.exp(-0.5 * saving))

    def mapped_values(self, params: Dict[str, Any]) -> Dict[str, float]:
        output: Dict[str, float] = {}
        for system_key, mappings in self.mappings_by_system.items():
            output[system_key] = sum(
                float(params[item.product_key]) * item.coefficient + item.constant for item in mappings
            )
        return output

    def relative_score(self, value: float, requirement: SystemRequirementSpec, higher: bool) -> float:
        history = self.history_by_system.get(requirement.system_key, [])
        if not history:
            return 100.0
        ordered = sorted(history)
        reference = statistics.median(ordered)
        lower_quartile = ordered[int(round(0.25 * (len(ordered) - 1)))]
        upper_quartile = ordered[int(round(0.75 * (len(ordered) - 1)))]
        scale = max(abs(upper_quartile - lower_quartile), abs(reference) * 0.10, 1e-9)
        direction = 1.0 if higher else -1.0
        cap = max(100.0, requirement.score_cap)
        standardized = direction * (value - reference) / scale
        if standardized >= 0:
            return 100.0 + (cap - 100.0) * math.tanh(standardized)
        return 100.0 - 60.0 * math.tanh(-standardized)

    def score_mapped_value(
        self,
        value: float,
        requirement: SystemRequirementSpec,
    ) -> Tuple[float, Optional[bool]]:
        """Score an already mapped system value for plotting and diagnostics."""
        kind = requirement.requirement_type
        if kind == "target":
            assert requirement.target_value is not None
            lower = requirement.lower_80_distance or self._default_distance(requirement, requirement.target_value)
            upper = requirement.upper_80_distance or self._default_distance(requirement, requirement.target_value)
            allowed_lower = (
                requirement.lower_compliance_tolerance
                if requirement.lower_compliance_tolerance is not None else lower
            )
            allowed_upper = (
                requirement.upper_compliance_tolerance
                if requirement.upper_compliance_tolerance is not None else upper
            )
            score = self._target_score(value, requirement.target_value, lower, upper)
            satisfied = abs(value - requirement.target_value) <= (
                allowed_lower if value < requirement.target_value else allowed_upper
            )
        elif kind == "at_least":
            assert requirement.minimum is not None
            distance = requirement.lower_80_distance or self._default_distance(requirement, requirement.minimum)
            bonus_distance = requirement.upper_80_distance or distance
            score = self._at_least_score(
                value,
                requirement.minimum,
                distance,
                requirement.score_cap,
                requirement.overachievement_bonus,
                bonus_distance,
            )
            satisfied = value >= requirement.minimum
        elif kind == "at_most" and requirement.maximum is not None:
            distance = requirement.upper_80_distance or self._default_distance(requirement, requirement.maximum)
            bonus_distance = requirement.lower_80_distance or distance
            score = self._at_most_score(
                value,
                requirement.maximum,
                distance,
                requirement.score_cap,
                requirement.overachievement_bonus,
                bonus_distance,
            )
            satisfied = value <= requirement.maximum
        elif kind == "at_most":
            score = self.relative_score(value, requirement, higher=False)
            satisfied = None
        elif kind == "within_range":
            score, satisfied = self._within_range_score(value, requirement)
        elif kind == "relative_higher":
            score = self.relative_score(value, requirement, higher=True)
            satisfied = None
        elif kind == "relative_lower":
            score = self.relative_score(value, requirement, higher=False)
            satisfied = None
        else:
            score = 100.0
            satisfied = None
        return clamp(score, 0.0, max(100.0, requirement.score_cap)), satisfied

    def requirement_curve(
        self,
        requirement: SystemRequirementSpec,
        point_count: int = 61,
    ) -> Dict[str, Any]:
        """Build a generic plotting range from requirements and robust history."""
        history = list(self.history_by_system.get(requirement.system_key, []))
        anchors = list(history)
        compliance_min: Optional[float] = None
        compliance_max: Optional[float] = None
        kind = requirement.requirement_type
        if kind == "target" and requirement.target_value is not None:
            target = requirement.target_value
            lower = requirement.lower_80_distance or self._default_distance(requirement, target)
            upper = requirement.upper_80_distance or self._default_distance(requirement, target)
            allowed_lower = requirement.lower_compliance_tolerance
            allowed_upper = requirement.upper_compliance_tolerance
            allowed_lower = lower if allowed_lower is None else allowed_lower
            allowed_upper = upper if allowed_upper is None else allowed_upper
            anchors.extend([target - 3.0 * lower, target, target + 3.0 * upper])
            compliance_min = target - allowed_lower
            compliance_max = target + allowed_upper
        elif kind == "at_least" and requirement.minimum is not None:
            distance = requirement.lower_80_distance or self._default_distance(requirement, requirement.minimum)
            bonus_distance = requirement.upper_80_distance or distance
            anchors.extend([
                requirement.minimum - 3.0 * distance,
                requirement.minimum + 3.0 * bonus_distance,
            ])
            compliance_min = requirement.minimum
        elif kind == "at_most" and requirement.maximum is not None:
            distance = requirement.upper_80_distance or self._default_distance(requirement, requirement.maximum)
            bonus_distance = requirement.lower_80_distance or distance
            anchors.extend([
                requirement.maximum - 3.0 * bonus_distance,
                requirement.maximum + 3.0 * distance,
            ])
            compliance_max = requirement.maximum
        elif kind == "within_range" and requirement.minimum is not None and requirement.maximum is not None:
            lower = requirement.lower_80_distance or self._default_distance(requirement, requirement.minimum)
            upper = requirement.upper_80_distance or self._default_distance(requirement, requirement.maximum)
            anchors.extend([requirement.minimum - 3.0 * lower, requirement.maximum + 3.0 * upper])
            compliance_min = requirement.minimum
            compliance_max = requirement.maximum
        if not anchors:
            anchors = [-1.0, 1.0]
        low = float(min(anchors))
        high = float(max(anchors))
        if math.isclose(low, high, rel_tol=0.0, abs_tol=1e-12):
            padding = max(abs(low) * 0.10, 1.0)
            low -= padding
            high += padding
        padding = 0.04 * (high - low)
        low -= padding
        high += padding
        points = []
        for raw_value in np.linspace(low, high, max(21, int(point_count))):
            score, satisfied = self.score_mapped_value(float(raw_value), requirement)
            points.append(
                {
                    "value": round(float(raw_value), 8),
                    "score": round(float(score), 5),
                    "satisfied": satisfied,
                }
            )
        return {
            "system_key": requirement.system_key,
            "label": requirement.system_attribute,
            "unit": requirement.system_unit,
            "requirement_type": kind,
            "requirement_text": self.attribute_assessment(
                {item.product_key: 0.0 for item in self.direct}, requirement
            )["requirement_text"],
            "score_cap": max(100.0, requirement.score_cap),
            "minimum": low,
            "maximum": high,
            "history_minimum": min(history) if history else None,
            "history_maximum": max(history) if history else None,
            "compliance_minimum": compliance_min,
            "compliance_maximum": compliance_max,
            "configured_not_learned": True,
            "points": points,
        }

    def _default_distance(self, requirement: SystemRequirementSpec, anchor: float) -> float:
        history = self.history_by_system.get(requirement.system_key, [])
        if abs(anchor) > 1e-12:
            return max(abs(anchor) * 0.10, 1e-9)
        if history:
            ordered = sorted(history)
            low = ordered[int(round(0.25 * (len(ordered) - 1)))]
            high = ordered[int(round(0.75 * (len(ordered) - 1)))]
            return max(abs(high - low) * 0.25, 1e-9)
        return 1.0

    def _within_range_score(self, value: float, requirement: SystemRequirementSpec) -> Tuple[float, bool]:
        assert requirement.minimum is not None and requirement.maximum is not None
        if requirement.minimum <= value <= requirement.maximum:
            return 100.0, True
        if value < requirement.minimum:
            distance = requirement.lower_80_distance or self._default_distance(requirement, requirement.minimum)
            return self._target_score(value, requirement.minimum, distance, distance), False
        distance = requirement.upper_80_distance or self._default_distance(requirement, requirement.maximum)
        return self._target_score(value, requirement.maximum, distance, distance), False

    def attribute_assessment(self, params: Dict[str, Any], requirement: SystemRequirementSpec) -> Dict[str, Any]:
        value = self.mapped_values(params)[requirement.system_key]
        kind = requirement.requirement_type
        relative_only = False
        requirement_value: Any = None
        normalized_deviation: Optional[float] = None
        deviation = 0.0
        required_change = 0.0
        required_change_action = "无需调整"
        comparison = "未配置要求"
        effective_lower_80_distance: Optional[float] = None
        effective_upper_80_distance: Optional[float] = None
        lower_80_distance_source: Optional[str] = None
        upper_80_distance_source: Optional[str] = None
        if kind == "target":
            assert requirement.target_value is not None
            target = requirement.target_value
            lower = requirement.lower_80_distance or self._default_distance(requirement, target)
            upper = requirement.upper_80_distance or self._default_distance(requirement, target)
            effective_lower_80_distance = lower
            effective_upper_80_distance = upper
            lower_80_distance_source = "configured" if requirement.lower_80_distance is not None else "automatic"
            upper_80_distance_source = "configured" if requirement.upper_80_distance is not None else "automatic"
            allowed_lower = (
                requirement.lower_compliance_tolerance
                if requirement.lower_compliance_tolerance is not None else lower
            )
            allowed_upper = (
                requirement.upper_compliance_tolerance
                if requirement.upper_compliance_tolerance is not None else upper
            )
            score = self._target_score(value, target, lower, upper)
            satisfied = abs(value - target) <= (allowed_lower if value < target else allowed_upper)
            scale = lower if value < target else upper
            deviation = value - target
            normalized_deviation = abs(deviation) / max(scale, 1e-12)
            required_change = target - value
            required_change_action = "提高" if required_change > 0 else "降低" if required_change < 0 else "无需调整"
            comparison = "低于目标" if deviation < 0 else "高于目标" if deviation > 0 else "达到目标"
            requirement_value = target
            requirement_text = (
                f"目标 {target:g}；下偏/上偏至80分的距离为 {lower:g}/{upper:g}；"
                f"达标允许偏差为 {allowed_lower:g}/{allowed_upper:g}"
            )
        elif kind == "at_least":
            assert requirement.minimum is not None
            distance = requirement.lower_80_distance or self._default_distance(requirement, requirement.minimum)
            bonus_distance = requirement.upper_80_distance or distance
            effective_lower_80_distance = distance
            lower_80_distance_source = "configured" if requirement.lower_80_distance is not None else "automatic"
            if requirement.overachievement_bonus:
                effective_upper_80_distance = bonus_distance
                upper_80_distance_source = "configured" if requirement.upper_80_distance is not None else "automatic"
            score = self._at_least_score(
                value,
                requirement.minimum,
                distance,
                requirement.score_cap,
                requirement.overachievement_bonus,
                bonus_distance,
            )
            satisfied = value >= requirement.minimum
            deviation = value - requirement.minimum
            normalized_deviation = max(0.0, -deviation) / max(distance, 1e-12)
            required_change = max(0.0, requirement.minimum - value)
            required_change_action = "提高" if required_change > 0 else "无需调整"
            comparison = "达到或超过下限" if satisfied else "低于下限"
            requirement_value = requirement.minimum
            requirement_text = f"不低于 {requirement.minimum:g}；低于要求至80分的距离为 {distance:g}"
            if requirement.overachievement_bonus:
                requirement_text += f"；高于要求的加分尺度为 {bonus_distance:g}"
        elif kind == "at_most" and requirement.maximum is not None:
            distance = requirement.upper_80_distance or self._default_distance(requirement, requirement.maximum)
            bonus_distance = requirement.lower_80_distance or distance
            effective_upper_80_distance = distance
            upper_80_distance_source = "configured" if requirement.upper_80_distance is not None else "automatic"
            if requirement.overachievement_bonus:
                effective_lower_80_distance = bonus_distance
                lower_80_distance_source = "configured" if requirement.lower_80_distance is not None else "automatic"
            score = self._at_most_score(
                value,
                requirement.maximum,
                distance,
                requirement.score_cap,
                requirement.overachievement_bonus,
                bonus_distance,
            )
            satisfied = value <= requirement.maximum
            deviation = value - requirement.maximum
            normalized_deviation = max(0.0, deviation) / max(distance, 1e-12)
            required_change = max(0.0, value - requirement.maximum)
            required_change_action = "降低" if required_change > 0 else "无需调整"
            comparison = "达到或优于上限" if satisfied else "超过上限"
            requirement_value = requirement.maximum
            requirement_text = f"不超过 {requirement.maximum:g}；超过要求至80分的距离为 {distance:g}"
            if requirement.overachievement_bonus:
                requirement_text += f"；低于要求的加分尺度为 {bonus_distance:g}"
        elif kind == "at_most":
            score = self.relative_score(value, requirement, higher=False)
            satisfied = None
            relative_only = True
            comparison = "按历史样本作相对越小越好比较"
            requirement_text = "未填写绝对上限，按历史样本中位数作相对越小越好比较"
        elif kind == "within_range":
            score, satisfied = self._within_range_score(value, requirement)
            effective_lower_80_distance = (
                requirement.lower_80_distance
                or self._default_distance(requirement, float(requirement.minimum))
            )
            effective_upper_80_distance = (
                requirement.upper_80_distance
                or self._default_distance(requirement, float(requirement.maximum))
            )
            lower_80_distance_source = "configured" if requirement.lower_80_distance is not None else "automatic"
            upper_80_distance_source = "configured" if requirement.upper_80_distance is not None else "automatic"
            requirement_value = [requirement.minimum, requirement.maximum]
            if value < requirement.minimum:
                distance = requirement.lower_80_distance or self._default_distance(requirement, requirement.minimum)
                deviation = value - requirement.minimum
                required_change = requirement.minimum - value
                required_change_action = "提高"
                normalized_deviation = required_change / max(distance, 1e-12)
                comparison = "低于要求区间"
            elif value > requirement.maximum:
                distance = requirement.upper_80_distance or self._default_distance(requirement, requirement.maximum)
                deviation = value - requirement.maximum
                required_change = requirement.maximum - value
                required_change_action = "降低"
                normalized_deviation = abs(required_change) / max(distance, 1e-12)
                comparison = "高于要求区间"
            else:
                comparison = "位于要求区间"
                normalized_deviation = 0.0
            requirement_text = f"位于 {requirement.minimum:g} 至 {requirement.maximum:g} 时为100分"
        elif kind == "relative_higher":
            score = self.relative_score(value, requirement, higher=True)
            satisfied = None
            relative_only = True
            comparison = "按历史样本作相对越大越好比较"
            requirement_text = "无绝对要求，按历史样本中位数作相对越大越好比较"
        elif kind == "relative_lower":
            score = self.relative_score(value, requirement, higher=False)
            satisfied = None
            relative_only = True
            comparison = "按历史样本作相对越小越好比较"
            requirement_text = "无绝对要求，按历史样本中位数作相对越小越好比较"
        else:
            score = 100.0
            satisfied = None
            requirement_text = "当前任务未填写对应要求"
        score = clamp(score, 0.0, max(100.0, requirement.score_cap))
        reference = None
        if isinstance(requirement_value, (int, float)):
            reference = abs(float(requirement_value))
        elif isinstance(requirement_value, list) and requirement_value:
            reference = max(abs(float(item)) for item in requirement_value if item is not None)
        deviation_percent = None if not reference else 100.0 * abs(deviation) / reference
        required_change_percent = None if not reference else 100.0 * abs(required_change) / reference
        sources = [
            {
                "mapping_id": item.mapping_id,
                "product_key": item.product_key,
                "product_label": item.product_label,
                "product_value": params[item.product_key],
                "coefficient": item.coefficient,
                "constant": item.constant,
                "mapped_contribution": round(float(params[item.product_key]) * item.coefficient + item.constant, 6),
            }
            for item in self.mappings_by_system[requirement.system_key]
        ]
        return {
            "system_key": requirement.system_key,
            "system_attribute": requirement.system_attribute,
            "value": round(value, 6),
            "unit": requirement.system_unit,
            "requirement_type": requirement.requirement_type,
            "requirement_value": requirement_value,
            "requirement_text": requirement_text,
            "score": round(score, 3),
            "satisfied": satisfied,
            "relative_only": relative_only,
            "comparison": comparison,
            "deviation": round(deviation, 6),
            "deviation_percent": None if deviation_percent is None else round(deviation_percent, 3),
            "normalized_deviation": None if normalized_deviation is None else round(normalized_deviation, 6),
            "required_change": round(required_change, 6),
            "required_change_action": required_change_action,
            "required_change_percent": None if required_change_percent is None else round(required_change_percent, 3),
            "score_curve": "smooth_rational_80_at_one_v5" if not relative_only else "robust_relative_v5",
            "hard_policy": requirement.hard_policy,
            "configured_weight": requirement.weight,
            "description": requirement.description,
            "mapping_sources": sources,
            "effective_lower_80_distance": (
                None if effective_lower_80_distance is None else round(float(effective_lower_80_distance), 8)
            ),
            "effective_upper_80_distance": (
                None if effective_upper_80_distance is None else round(float(effective_upper_80_distance), 8)
            ),
            "lower_80_distance_source": lower_80_distance_source,
            "upper_80_distance_source": upper_80_distance_source,
        }

    def assess(self, params: Dict[str, Any]) -> Dict[str, Any]:
        attributes = [self.attribute_assessment(params, item) for item in self.task.requirements]
        total_weight = sum(float(item["configured_weight"]) for item in attributes)
        direct_score = (
            sum(float(item["score"]) * float(item["configured_weight"]) for item in attributes) / total_weight
            if total_weight > 0 else 100.0
        )
        hard_cap: Optional[float] = None
        failed_requirements: List[Dict[str, Any]] = []
        for item in attributes:
            normalized_weight = float(item["configured_weight"]) / total_weight if total_weight > 0 else 0.0
            item["group_weight"] = round(normalized_weight, 6)
            item["direct_point_contribution"] = round(normalized_weight * (float(item["score"]) - 100.0), 3)
            if item["satisfied"] is False and item["hard_policy"] == "veto":
                hard_cap = 0.0
            elif item["satisfied"] is False and item["hard_policy"] == "cap_total":
                hard_cap = 100.0 if hard_cap is None else min(hard_cap, 100.0)
            if item["satisfied"] is False:
                failed_requirements.append(
                    {
                        "system_key": item["system_key"],
                        "system_attribute": item["system_attribute"],
                        "hard_policy": item["hard_policy"],
                        "required_change": item["required_change"],
                        "unit": item["unit"],
                    }
                )
        return {
            "task_id": self.task.task_id,
            "task_name": self.task.task_name,
            "score": round(direct_score, 3),
            "attributes": attributes,
            "available_attribute_count": len(attributes),
            "hard_cap": hard_cap,
            "compliance_status": "meets_all" if not failed_requirements else "has_gaps",
            "failed_requirement_count": len(failed_requirements),
            "failed_requirements": failed_requirements,
            "weighting": "按系统任务要求中的组内权重归一化汇总",
        }


class ProductFeatureSpace:
    """Benefit-oriented segmented features for non-direct product attributes."""

    def __init__(self, project: SystemProjectDataset, segments: int = 3):
        self.project = project
        self.segments = int(segments)
        mapping_keys = {item.product_key for item in project.product_mappings()}
        self.attributes = [
            item
            for item in project.product.attributes
            if item.key in mapping_keys
            and item.is_numeric
            and item.preference_direction in {"higher_better", "lower_better"}
        ]
        self.reference_params: Dict[str, float] = {}
        for spec in self.attributes:
            values = [float(item.params[spec.key]) for item in project.product.schemes]
            self.reference_params[spec.key] = statistics.median(values)

    @property
    def variable_count(self) -> int:
        return len(self.attributes) * self.segments

    def benefit(self, params: Dict[str, Any], spec: Any) -> float:
        assert spec.generation_min is not None and spec.generation_max is not None
        position = clamp(
            (float(params[spec.key]) - spec.generation_min)
            / max(spec.generation_max - spec.generation_min, 1e-12),
            0.0,
            1.0,
        )
        return position if spec.preference_direction == "higher_better" else 1.0 - position

    def vector(self, params: Dict[str, Any]) -> np.ndarray:
        values: List[float] = []
        for spec in self.attributes:
            scaled = self.benefit(params, spec) * self.segments
            values.extend(clamp(scaled - index, 0.0, 1.0) for index in range(self.segments))
        return np.asarray(values, dtype=float)

    def reference_vector(self) -> np.ndarray:
        return self.vector(self.reference_params)

    def slices(self) -> Iterable[Tuple[Any, slice]]:
        for index, spec in enumerate(self.attributes):
            start = index * self.segments
            yield spec, slice(start, start + self.segments)

    def stage_prior(self) -> np.ndarray:
        if not self.attributes:
            return np.zeros(0, dtype=float)
        stage_share = {1: 0.50, 2: 0.30, 3: 0.20}
        grouped: Dict[int, List[int]] = {}
        for index, spec in enumerate(self.attributes):
            grouped.setdefault(spec.design_stage, []).append(index)
        total = sum(stage_share.get(stage, 0.0) for stage in grouped)
        attribute_weights = np.zeros(len(self.attributes), dtype=float)
        if total <= 0:
            attribute_weights[:] = 1.0 / len(self.attributes)
        else:
            for stage, indices in grouped.items():
                share = stage_share.get(stage, 0.0) / total
                for index in indices:
                    attribute_weights[index] = share / len(indices)
        return np.repeat(attribute_weights / self.segments, self.segments)


class DirectRequirementFeatureSpace:
    """Requirement-anchored signed UTA features for direct system attributes."""

    MIN_SIDE_DISTANCE = 3.0

    def __init__(self, project: SystemProjectDataset, segments: int = 3):
        self.project = project
        self.segments = int(segments)
        direct_keys = {item.system_key for item in project.direct_mappings()}
        requirement_keys = {
            item.system_key for task in project.tasks for item in task.requirements
        }
        self.attributes = [
            item for item in project.system_attributes
            if item.key in direct_keys and item.key in requirement_keys
        ]
        self.attribute_by_key = {item.key: item for item in self.attributes}
        self.product_attribute_by_key = project.product.attribute_by_key()
        self.mappings_by_system: Dict[str, List[Any]] = {}
        for mapping in project.direct_mappings():
            self.mappings_by_system.setdefault(mapping.system_key, []).append(mapping)
        self.history_by_system = {
            key: [self.mapped_values(item.params)[key] for item in project.product.schemes]
            for key in self.mappings_by_system
        }
        self.mapped_ranges = {
            key: self._mapped_generation_range(key) for key in self.mappings_by_system
        }
        self.side_limits: Dict[str, Dict[str, float]] = self._build_side_limits()

    @property
    def variable_count(self) -> int:
        return len(self.attributes) * self.segments * 2

    def mapped_values(self, params: Dict[str, Any]) -> Dict[str, float]:
        return {
            system_key: sum(
                float(params[item.product_key]) * item.coefficient + item.constant
                for item in mappings
            )
            for system_key, mappings in self.mappings_by_system.items()
        }

    def _mapped_generation_range(self, system_key: str) -> Tuple[float, float]:
        low = 0.0
        high = 0.0
        for mapping in self.mappings_by_system[system_key]:
            spec = self.product_attribute_by_key[mapping.product_key]
            assert spec.generation_min is not None and spec.generation_max is not None
            values = [
                float(spec.generation_min) * mapping.coefficient + mapping.constant,
                float(spec.generation_max) * mapping.coefficient + mapping.constant,
            ]
            low += min(values)
            high += max(values)
        history = self.history_by_system.get(system_key, [])
        if history:
            low = min(low, min(history))
            high = max(high, max(history))
        return float(low), float(high)

    def _history_reference_scale(self, system_key: str) -> Tuple[float, float]:
        history = sorted(self.history_by_system.get(system_key, []))
        if not history:
            return 0.0, 1.0
        reference = float(statistics.median(history))
        lower = history[int(round(0.25 * (len(history) - 1)))]
        upper = history[int(round(0.75 * (len(history) - 1)))]
        scale = max(abs(float(upper) - float(lower)), abs(reference) * 0.10, 1e-9)
        return reference, scale

    def _default_distance(self, requirement: SystemRequirementSpec, anchor: float) -> float:
        if abs(anchor) > 1e-12:
            return max(abs(anchor) * 0.10, 1e-9)
        _reference, scale = self._history_reference_scale(requirement.system_key)
        return max(scale * 0.25, 1e-9)

    def branch_definition(
        self,
        requirement: SystemRequirementSpec,
    ) -> Dict[str, Dict[str, float]]:
        kind = requirement.requirement_type
        if kind == "target" and requirement.target_value is not None:
            target = float(requirement.target_value)
            return {
                "left": {
                    "anchor": target,
                    "scale": float(requirement.lower_80_distance or self._default_distance(requirement, target)),
                    "sign": -1.0,
                },
                "right": {
                    "anchor": target,
                    "scale": float(requirement.upper_80_distance or self._default_distance(requirement, target)),
                    "sign": -1.0,
                },
            }
        if kind == "at_least" and requirement.minimum is not None:
            anchor = float(requirement.minimum)
            scale = float(requirement.lower_80_distance or self._default_distance(requirement, anchor))
            return {
                "left": {"anchor": anchor, "scale": scale, "sign": -1.0},
                "right": {
                    "anchor": anchor,
                    "scale": float(requirement.upper_80_distance or scale),
                    "sign": 1.0 if requirement.overachievement_bonus else 0.0,
                },
            }
        if kind == "at_most" and requirement.maximum is not None:
            anchor = float(requirement.maximum)
            scale = float(requirement.upper_80_distance or self._default_distance(requirement, anchor))
            return {
                "left": {
                    "anchor": anchor,
                    "scale": float(requirement.lower_80_distance or scale),
                    "sign": 1.0 if requirement.overachievement_bonus else 0.0,
                },
                "right": {"anchor": anchor, "scale": scale, "sign": -1.0},
            }
        if kind == "within_range" and requirement.minimum is not None and requirement.maximum is not None:
            lower = float(requirement.minimum)
            upper = float(requirement.maximum)
            return {
                "left": {
                    "anchor": lower,
                    "scale": float(requirement.lower_80_distance or self._default_distance(requirement, lower)),
                    "sign": -1.0,
                },
                "right": {
                    "anchor": upper,
                    "scale": float(requirement.upper_80_distance or self._default_distance(requirement, upper)),
                    "sign": -1.0,
                },
            }
        reference, scale = self._history_reference_scale(requirement.system_key)
        higher = kind == "relative_higher"
        lower = kind in {"relative_lower", "at_most"}
        return {
            "left": {"anchor": reference, "scale": scale, "sign": -1.0 if higher else 1.0 if lower else 0.0},
            "right": {"anchor": reference, "scale": scale, "sign": 1.0 if higher else -1.0 if lower else 0.0},
        }

    def _build_side_limits(self) -> Dict[str, Dict[str, float]]:
        output = {
            item.key: {"left": self.MIN_SIDE_DISTANCE, "right": self.MIN_SIDE_DISTANCE}
            for item in self.attributes
        }
        for task in self.project.tasks:
            for requirement in task.requirements:
                if requirement.system_key not in output:
                    continue
                low, high = self.mapped_ranges[requirement.system_key]
                definitions = self.branch_definition(requirement)
                left = definitions["left"]
                right = definitions["right"]
                left_distance = max(0.0, (float(left["anchor"]) - low) / max(float(left["scale"]), 1e-12))
                right_distance = max(0.0, (high - float(right["anchor"])) / max(float(right["scale"]), 1e-12))
                output[requirement.system_key]["left"] = max(
                    output[requirement.system_key]["left"], left_distance
                )
                output[requirement.system_key]["right"] = max(
                    output[requirement.system_key]["right"], right_distance
                )
        return {
            key: {side: round(float(value), 8) for side, value in sides.items()}
            for key, sides in output.items()
        }

    def branch_slice(self, system_key: str, side: str) -> slice:
        attribute_index = next(index for index, item in enumerate(self.attributes) if item.key == system_key)
        side_offset = 0 if side == "left" else self.segments
        start = attribute_index * 2 * self.segments + side_offset
        return slice(start, start + self.segments)

    def attribute_slice(self, system_key: str) -> slice:
        attribute_index = next(index for index, item in enumerate(self.attributes) if item.key == system_key)
        start = attribute_index * 2 * self.segments
        return slice(start, start + 2 * self.segments)

    def _basis(self, distance: float, limit: float) -> np.ndarray:
        scaled = clamp(float(distance) / max(float(limit), 1e-12), 0.0, 1.0) * self.segments
        return np.asarray(
            [clamp(scaled - index, 0.0, 1.0) for index in range(self.segments)],
            dtype=float,
        )

    def attribute_vector(
        self,
        requirement: SystemRequirementSpec,
        mapped_value: float,
    ) -> np.ndarray:
        output = np.zeros(self.variable_count, dtype=float)
        definitions = self.branch_definition(requirement)
        for side in ("left", "right"):
            definition = definitions[side]
            sign = float(definition["sign"])
            if abs(sign) <= 1e-12:
                continue
            anchor = float(definition["anchor"])
            scale = max(float(definition["scale"]), 1e-12)
            distance = (
                max(0.0, anchor - float(mapped_value)) / scale
                if side == "left"
                else max(0.0, float(mapped_value) - anchor) / scale
            )
            part = self.branch_slice(requirement.system_key, side)
            output[part] = sign * self._basis(
                distance, self.side_limits[requirement.system_key][side]
            )
        return output

    def vector(self, params: Dict[str, Any], task: SystemTask) -> np.ndarray:
        mapped = self.mapped_values(params)
        output = np.zeros(self.variable_count, dtype=float)
        for requirement in task.requirements:
            if requirement.system_key in self.attribute_by_key:
                output += self.attribute_vector(requirement, mapped[requirement.system_key])
        return output

    def prior_distribution(self) -> np.ndarray:
        prior = np.zeros(self.variable_count, dtype=float)
        task_count = max(len(self.project.tasks), 1)
        for task in self.project.tasks:
            for requirement in task.requirements:
                if requirement.system_key not in self.attribute_by_key:
                    continue
                definitions = self.branch_definition(requirement)
                active_sides = [side for side in ("left", "right") if abs(definitions[side]["sign"]) > 1e-12]
                if not active_sides:
                    continue
                attribute_share = max(float(requirement.weight), 1e-6) / task_count
                for side in active_sides:
                    limit = self.side_limits[requirement.system_key][side]
                    sign = float(definitions[side]["sign"])
                    magnitudes = [0.0]
                    for index in range(1, self.segments + 1):
                        distance = limit * index / self.segments
                        if sign < 0:
                            magnitude = 100.0 - SystemDirectScorer._smooth_requirement_score(distance)
                        else:
                            cap = max(110.0, float(requirement.score_cap))
                            magnitude = (cap - 100.0) * (1.0 - math.exp(-0.5 * distance))
                        magnitudes.append(max(0.0, magnitude))
                    increments = np.diff(np.asarray(magnitudes, dtype=float))
                    increments = np.maximum(increments, 1e-6)
                    increments /= max(float(increments.sum()), 1e-12)
                    part = self.branch_slice(requirement.system_key, side)
                    prior[part] += increments * attribute_share / len(active_sides)
        total = float(prior.sum())
        if total <= 1e-12:
            prior[:] = 1.0 / max(self.variable_count, 1)
        else:
            prior /= total
        return prior


class _V5HierarchicalUTAModel:
    """LP-UTA with alpha constrained by a UI prior and bounds."""

    PRODUCT_GROUP_SPAN = 60.0
    STRICT_MARGIN = 1.0
    TIE_MARGIN = 2.0

    def __init__(
        self,
        project: SystemProjectDataset,
        task: SystemTask,
        alpha_prior: float = 0.70,
        alpha_min: float = 0.60,
        alpha_max: float = 0.90,
        segments: int = 3,
    ):
        if not 0.0 <= alpha_min <= alpha_prior <= alpha_max <= 1.0:
            raise ValueError("alpha参数必须满足 0 <= 下限 <= 默认值 <= 上限 <= 1。")
        self.project = project
        self.task = task
        self.direct_scorer = SystemDirectScorer(project, task)
        self.direct_scorers_by_task = {
            item.task_id: SystemDirectScorer(project, item) for item in project.tasks
        }
        self._snapshot_scorers: Dict[str, SystemDirectScorer] = {}
        self.space = ProductFeatureSpace(project, segments)
        self.segments = int(segments)
        self.alpha_prior = float(alpha_prior)
        self.alpha_min = float(alpha_min)
        self.alpha_max = float(alpha_max)
        self.alpha = 1.0 if not self.space.variable_count else self.alpha_prior
        self.increments = self._prior_increments(self.alpha)
        self.summary_data: Dict[str, Any] = {}

    def _prior_increments(self, alpha: float) -> np.ndarray:
        prior = self.space.stage_prior()
        return prior * self.PRODUCT_GROUP_SPAN * max(0.0, 1.0 - alpha)

    def marginal_curves(self) -> List[Dict[str, Any]]:
        """Expose learned and design-order-prior UTA marginal functions."""
        if not self.space.variable_count:
            return []
        normalized = self.increments / max(1.0 - self.alpha, 1e-12)
        prior = self.space.stage_prior() * self.PRODUCT_GROUP_SPAN
        curves: List[Dict[str, Any]] = []
        for spec, part in self.space.slices():
            assert spec.generation_min is not None and spec.generation_max is not None
            learned_increments = normalized[part]
            prior_increments = prior[part]
            reference_value = float(self.space.reference_params[spec.key])

            def utility(raw_value: float, increments: np.ndarray) -> float:
                benefit = self.space.benefit({spec.key: raw_value}, spec)
                scaled = benefit * self.segments
                basis = np.asarray(
                    [clamp(scaled - index, 0.0, 1.0) for index in range(self.segments)],
                    dtype=float,
                )
                return float(basis @ increments)

            reference_utility = utility(reference_value, learned_increments)
            raw_values = np.linspace(
                float(spec.generation_min),
                float(spec.generation_max),
                self.segments + 1,
            )
            points = [
                {
                    "raw_value": round(float(raw_value), spec.precision),
                    "utility": round(utility(float(raw_value), learned_increments), 6),
                    "prior_utility": round(utility(float(raw_value), prior_increments), 6),
                    "contribution_from_reference": round(
                        utility(float(raw_value), learned_increments) - reference_utility,
                        6,
                    ),
                }
                for raw_value in raw_values
            ]
            curves.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "unit": spec.unit,
                    "preference_direction": spec.preference_direction,
                    "marginal_trend": spec.marginal_trend,
                    "design_stage": spec.design_stage,
                    "attribute_weight": round(float(learned_increments.sum()) / self.PRODUCT_GROUP_SPAN, 6),
                    "utility_span": round(float(learned_increments.sum()), 6),
                    "reference_value": round(reference_value, spec.precision),
                    "reference_utility": round(reference_utility, 6),
                    "generation_minimum": spec.generation_min,
                    "generation_maximum": spec.generation_max,
                    "sample_minimum": spec.feasible_min,
                    "sample_maximum": spec.feasible_max,
                    "increments": [round(float(item), 8) for item in learned_increments],
                    "prior_increments": [round(float(item), 8) for item in prior_increments],
                    "points": points,
                }
            )
        return curves

    def requirement_curves(self) -> List[Dict[str, Any]]:
        return [self.direct_scorer.requirement_curve(item) for item in self.task.requirements]

    @staticmethod
    def active_evidence(evidence: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            item
            for item in evidence
            if item.get("status", "active") not in {"withdrawn", "rejected"}
            and item.get("relation") in {"A", "B", "tie"}
        ]

    def _direct_delta(self, item: Dict[str, Any]) -> float:
        snapshot = item.get("system_task_snapshot") or {}
        scorer: SystemDirectScorer
        if snapshot:
            identity = hashlib.sha256(
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            scorer = self._snapshot_scorers.get(identity)  # type: ignore[assignment]
            if scorer is None:
                task_data = dict(snapshot)
                task_data["requirements"] = tuple(
                    SystemRequirementSpec(**raw) for raw in task_data.get("requirements") or []
                )
                scorer = SystemDirectScorer(self.project, SystemTask(**task_data))
                self._snapshot_scorers[identity] = scorer
        else:
            task_id = str(item.get("system_task_id") or self.task.task_id)
            scorer = self.direct_scorers_by_task.get(task_id, self.direct_scorer)
        left = scorer.assess(item["params_a"])["score"]
        right = scorer.assess(item["params_b"])["score"]
        return float(left) - float(right)

    def _product_delta(self, item: Dict[str, Any]) -> np.ndarray:
        return self.space.vector(item["params_a"]) - self.space.vector(item["params_b"])

    def _shape_rows(self, total: int, theta_start: int) -> Tuple[List[np.ndarray], List[float]]:
        rows: List[np.ndarray] = []
        bounds: List[float] = []
        for spec, part in self.space.slices():
            indices = [theta_start + index for index in range(part.start, part.stop)]
            trend = spec.marginal_trend
            diminishing = "递减" in trend or "饱和" in trend
            increasing = "递增" in trend or ("加速" in trend and not diminishing)
            linear = "线性" in trend
            for left, right in zip(indices, indices[1:]):
                if diminishing or linear:
                    row = np.zeros(total)
                    row[right] = 1.0
                    row[left] = -1.0
                    rows.append(row)
                    bounds.append(0.0)
                if increasing or linear:
                    row = np.zeros(total)
                    row[left] = 1.0
                    row[right] = -1.0
                    rows.append(row)
                    bounds.append(0.0)
        return rows, bounds

    def _solve(self, observations: Sequence[Dict[str, Any]]) -> Tuple[float, np.ndarray, List[float], bool]:
        count = self.space.variable_count
        if not count:
            return 1.0, np.zeros(0), [0.0] * len(observations), True
        if not observations:
            return self.alpha_prior, self._prior_increments(self.alpha_prior), [], True

        evidence_count = len(observations)
        alpha_index = 0
        theta_start = 1
        slack_start = theta_start + count
        alpha_dev_pos = slack_start + evidence_count
        alpha_dev_neg = alpha_dev_pos + 1
        theta_dev_pos = alpha_dev_neg + 1
        theta_dev_neg = theta_dev_pos + count
        total = theta_dev_neg + count

        objective = np.zeros(total)
        for index, item in enumerate(observations):
            objective[slack_start + index] = max(0.2, float(item.get("confidence", 1.0)))
        objective[alpha_dev_pos] = 2.0
        objective[alpha_dev_neg] = 2.0
        objective[theta_dev_pos:theta_dev_pos + count] = 0.025
        objective[theta_dev_neg:theta_dev_neg + count] = 0.025

        equalities: List[np.ndarray] = []
        equality_values: List[float] = []
        normalization = np.zeros(total)
        normalization[alpha_index] = self.PRODUCT_GROUP_SPAN
        normalization[theta_start:theta_start + count] = 1.0
        equalities.append(normalization)
        equality_values.append(self.PRODUCT_GROUP_SPAN)

        alpha_prior_row = np.zeros(total)
        alpha_prior_row[alpha_index] = 1.0
        alpha_prior_row[alpha_dev_pos] = -1.0
        alpha_prior_row[alpha_dev_neg] = 1.0
        equalities.append(alpha_prior_row)
        equality_values.append(self.alpha_prior)

        prior = self.space.stage_prior()
        for index, share in enumerate(prior):
            row = np.zeros(total)
            row[theta_start + index] = 1.0
            row[alpha_index] = share * self.PRODUCT_GROUP_SPAN
            row[theta_dev_pos + index] = -1.0
            row[theta_dev_neg + index] = 1.0
            equalities.append(row)
            equality_values.append(share * self.PRODUCT_GROUP_SPAN)

        inequalities, inequality_values = self._shape_rows(total, theta_start)
        for index, item in enumerate(observations):
            direct_delta = self._direct_delta(item)
            product_delta = self._product_delta(item)
            expression = np.zeros(total)
            expression[alpha_index] = direct_delta
            expression[theta_start:theta_start + count] = product_delta
            slack_index = slack_start + index
            if item["relation"] == "A":
                row = -expression
                row[slack_index] = -1.0
                inequalities.append(row)
                inequality_values.append(-self.STRICT_MARGIN)
            elif item["relation"] == "B":
                row = expression
                row[slack_index] = -1.0
                inequalities.append(row)
                inequality_values.append(-self.STRICT_MARGIN)
            else:
                row = expression.copy()
                row[slack_index] = -1.0
                inequalities.append(row)
                inequality_values.append(self.TIE_MARGIN)
                row = -expression
                row[slack_index] = -1.0
                inequalities.append(row)
                inequality_values.append(self.TIE_MARGIN)

        bounds: List[Tuple[Optional[float], Optional[float]]] = [(self.alpha_min, self.alpha_max)]
        bounds.extend([(0.0, self.PRODUCT_GROUP_SPAN)] * count)
        bounds.extend([(0.0, None)] * evidence_count)
        bounds.extend([(0.0, None), (0.0, None)])
        bounds.extend([(0.0, None)] * (2 * count))
        result = linprog(
            objective,
            A_ub=np.asarray(inequalities) if inequalities else None,
            b_ub=np.asarray(inequality_values) if inequality_values else None,
            A_eq=np.asarray(equalities),
            b_eq=np.asarray(equality_values),
            bounds=bounds,
            method="highs",
        )
        if not result.success or result.x is None:
            return self.alpha_prior, self._prior_increments(self.alpha_prior), [0.0] * evidence_count, False
        return (
            float(result.x[alpha_index]),
            np.asarray(result.x[theta_start:theta_start + count], dtype=float),
            [float(item) for item in result.x[slack_start:slack_start + evidence_count]],
            True,
        )

    @staticmethod
    def _test_member(item: Dict[str, Any]) -> bool:
        identity = str(item.get("id") or item.get("interaction_id") or repr(item))
        digest = hashlib.sha256(identity.encode("utf-8")).digest()
        return int.from_bytes(digest[:2], "big") % 5 == 0

    def _utility_delta_with(self, item: Dict[str, Any], alpha: float, increments: np.ndarray) -> float:
        return alpha * self._direct_delta(item) + float(self._product_delta(item) @ increments)

    def _accuracy(self, evidence: Sequence[Dict[str, Any]], alpha: float, increments: np.ndarray) -> Optional[float]:
        if not evidence:
            return None
        correct = 0
        for item in evidence:
            delta = self._utility_delta_with(item, alpha, increments)
            if item["relation"] == "tie":
                correct += int(abs(delta) <= self.TIE_MARGIN)
            elif item["relation"] == "A":
                correct += int(delta > 0.0)
            else:
                correct += int(delta < 0.0)
        return correct / len(evidence)

    def fit(self, evidence: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        observations = self.active_evidence(evidence)
        evidence_by_task: Dict[str, int] = {}
        for item in observations:
            task_id = str(item.get("system_task_id") or self.task.task_id)
            evidence_by_task[task_id] = evidence_by_task.get(task_id, 0) + 1
        train = list(observations)
        test: List[Dict[str, Any]] = []
        test_accuracy: Optional[float] = None
        if len(observations) >= 8:
            test = [item for item in observations if self._test_member(item)]
            if not test:
                test = [observations[-1]]
            test_ids = {id(item) for item in test}
            train = [item for item in observations if id(item) not in test_ids]
            train_alpha, train_increments, _, _ = self._solve(train)
            test_accuracy = self._accuracy(test, train_alpha, train_increments)

        self.alpha, self.increments, slacks, success = self._solve(observations)
        training_accuracy = self._accuracy(observations, self.alpha, self.increments)
        tolerances = []
        for item, slack in zip(observations, slacks):
            tolerances.append(
                {
                    "evidence_id": item.get("id"),
                    "slack": round(slack, 5),
                    "high_tolerance": slack > 0.50,
                    "m3_conflict": False,
                }
            )
        attribute_weights: Dict[str, float] = {}
        denominator = max(self.PRODUCT_GROUP_SPAN * (1.0 - self.alpha), 1e-12)
        for spec, part in self.space.slices():
            attribute_weights[spec.key] = round(float(self.increments[part].sum()) / denominator, 6)
        high_tolerance = sum(1 for item in tolerances if item["high_tolerance"])
        consistency = (
            clamp(1.0 - high_tolerance / len(observations), 0.0, 1.0)
            if observations
            else None
        )
        self.summary_data = {
            "model_type": "safe_formula_smooth_requirement_shared_hierarchical_lp_uta_v5",
            "model_name": "平滑要求适配与安全公式共享双通道层次化LP-UTA V5",
            "status": "no_data" if not observations else "trained" if success else "fallback_prior",
            "task_id": "shared_system_preference",
            "active_scoring_task_id": self.task.task_id,
            "source_task_ids": sorted(evidence_by_task),
            "evidence_by_task": evidence_by_task,
            "segments": self.segments,
            "attribute_count": len(self.space.attributes),
            "utility_increment_variables": self.space.variable_count,
            "evidence_pairs": len(observations),
            "training_pairs": len(train),
            "test_pairs": len(test),
            "training_accuracy": None if training_accuracy is None else round(training_accuracy, 3),
            "test_accuracy": None if test_accuracy is None else round(test_accuracy, 3),
            "judgment_consistency": None if consistency is None else round(consistency, 3),
            "m1_consistent": bool(success and high_tolerance == 0),
            "m1_label": "一致" if success and high_tolerance == 0 else "存在需复核证据",
            "m2_total_slack": round(sum(slacks), 5),
            "high_tolerance_pairs": high_tolerance,
            "tolerances": tolerances,
            "alpha_prior": round(self.alpha_prior, 6),
            "alpha_min": round(self.alpha_min, 6),
            "alpha_max": round(self.alpha_max, 6),
            "learned_alpha": round(self.alpha, 6),
            "attribute_weights": attribute_weights,
            "increments": [round(float(item), 10) for item in self.increments],
            "product_reference_params": dict(self.space.reference_params),
            "product_attribute_keys": [item.key for item in self.space.attributes],
            "direct_reference_values": {
                key: round(float(statistics.median(values)), 10)
                for key, values in self.direct_scorer.history_by_system.items()
                if values
            },
            "direct_history_stats": {
                key: {
                    "minimum": round(float(min(values)), 10),
                    "median": round(float(statistics.median(values)), 10),
                    "maximum": round(float(max(values)), 10),
                }
                for key, values in self.direct_scorer.history_by_system.items()
                if values
            },
            "recommended_training_pairs": max(20, 4 * (len(self.space.attributes) + 1)),
            "score_definition": "任务参数化系统要求锚定直接贡献 + 跨任务共享成品性能边际效用",
            "protocol_used_for_training": False,
            "task_requirements_used_as_context": True,
            "task_requirement_values_frozen_into_model": False,
            "system_contract_version": self.project.contract_version,
        }
        self.summary_data["marginal_curves"] = self.marginal_curves()
        return dict(self.summary_data)

    def product_score(self, params: Dict[str, Any]) -> Tuple[float, List[Dict[str, Any]]]:
        if not self.space.variable_count or self.alpha >= 1.0 - 1e-9:
            return 100.0, []
        normalized = self.increments / max(1.0 - self.alpha, 1e-12)
        current = self.space.vector(params)
        reference = self.space.reference_vector()
        contributions: List[Dict[str, Any]] = []
        for spec, part in self.space.slices():
            delta = float((current[part] - reference[part]) @ normalized[part])
            contributions.append(
                {
                    "product_key": spec.key,
                    "product_label": spec.label,
                    "value": params[spec.key],
                    "reference_value": self.space.reference_params[spec.key],
                    "unit": spec.unit,
                    "group_point_contribution": round(delta, 3),
                    "total_point_contribution": round((1.0 - self.alpha) * delta, 3),
                    "direction": spec.preference_direction,
                }
            )
        score = 100.0 + sum(float(item["group_point_contribution"]) for item in contributions)
        return clamp(score, 40.0, 160.0), contributions

    def assess(self, params: Dict[str, Any]) -> Dict[str, Any]:
        direct = self.direct_scorer.assess(params)
        product_score, product_contributions = self.product_score(params)
        raw_total = self.alpha * float(direct["score"]) + (1.0 - self.alpha) * product_score
        hard_cap = direct.get("hard_cap")
        decision_score = raw_total if hard_cap is None else min(raw_total, float(hard_cap))
        direct_contributions = []
        for item in direct["attributes"]:
            copy = dict(item)
            copy["total_point_contribution"] = round(
                self.alpha * float(item.get("direct_point_contribution", 0.0)), 3
            )
            direct_contributions.append(copy)
        total_contributions = sorted(
            [
                {
                    "label": item["system_attribute"],
                    "channel": "direct_system",
                    "contribution": item["total_point_contribution"],
                }
                for item in direct_contributions
            ]
            + [
                {
                    "label": item["product_label"],
                    "channel": "product_performance",
                    "contribution": item["total_point_contribution"],
                }
                for item in product_contributions
            ],
            key=lambda item: abs(float(item["contribution"])),
            reverse=True,
        )
        confidence = min(0.98, 0.45 + 0.012 * int(self.summary_data.get("evidence_pairs", 0)))
        return {
            "method": "平滑要求适配与共享双通道层次化LP-UTA V5",
            "task": asdict(self.task),
            "total_score": round(raw_total, 2),
            "raw_total_score": round(raw_total, 2),
            "decision_score": round(decision_score, 2),
            "direct_score": round(float(direct["score"]), 2),
            "product_score": round(product_score, 2),
            "alpha": round(self.alpha, 4),
            "alpha_prior": round(self.alpha_prior, 4),
            "alpha_bounds": [round(self.alpha_min, 4), round(self.alpha_max, 4)],
            "direct_assessment": {**direct, "attributes": direct_contributions},
            "product_reference": "历史成品样本各属性稳健中位数（经验中性参考，不是系统要求）",
            "product_contributions": product_contributions,
            "top_contributors": total_contributions[:6],
            "confidence": round(confidence, 3),
            "hard_cap_applied": hard_cap,
            "compliance_status": direct.get("compliance_status"),
            "failed_requirement_count": direct.get("failed_requirement_count", 0),
            "formula": "U_total = alpha * U_direct + (1-alpha) * U_product",
        }

    def pair_probability(self, params_a: Dict[str, Any], params_b: Dict[str, Any]) -> float:
        left = self.assess(params_a)["total_score"]
        right = self.assess(params_b)["total_score"]
        return sigmoid((float(left) - float(right)) / 6.0)


class HierarchicalUTAModel(_V5HierarchicalUTAModel):
    """V11 requirement-anchored joint LP-UTA with separated score and weight."""

    DIRECT_SHAPE_BLEND_CAP = 0.35
    DIRECT_SHAPE_EVIDENCE_HALF_SATURATION = 4.0

    DIRECT_GROUP_SPAN = 60.0
    HIGH_TOLERANCE = 0.50
    M3_BIG_M = 2.0 * DIRECT_GROUP_SPAN + 2.0

    def __init__(
        self,
        project: SystemProjectDataset,
        task: SystemTask,
        alpha_prior: float = 0.70,
        alpha_min: float = 0.60,
        alpha_max: float = 0.90,
        segments: int = 3,
    ):
        super().__init__(project, task, alpha_prior, alpha_min, alpha_max, segments)
        self.direct_space = DirectRequirementFeatureSpace(project, segments)
        self.direct_increments = (
            self.direct_space.prior_distribution() * self.PRODUCT_GROUP_SPAN * self.alpha_prior
        )
        self.increments = (
            self.space.stage_prior() * self.PRODUCT_GROUP_SPAN * (1.0 - self.alpha_prior)
        )

    def _product_shape_rows(self, total: int, theta_start: int) -> Tuple[List[np.ndarray], List[float]]:
        rows: List[np.ndarray] = []
        bounds: List[float] = []
        for spec, part in self.space.slices():
            indices = [theta_start + index for index in range(part.start, part.stop)]
            trend = spec.marginal_trend
            diminishing = "递减" in trend or "饱和" in trend
            increasing = "递增" in trend or ("加速" in trend and not diminishing)
            if spec.preference_direction == "lower_better" and "负效用加速" in trend:
                diminishing = True
                increasing = False
            linear = "线性" in trend
            for left, right in zip(indices, indices[1:]):
                if diminishing or linear:
                    row = np.zeros(total)
                    row[right] = 1.0
                    row[left] = -1.0
                    rows.append(row)
                    bounds.append(0.0)
                if increasing or linear:
                    row = np.zeros(total)
                    row[left] = 1.0
                    row[right] = -1.0
                    rows.append(row)
                    bounds.append(0.0)
        return rows, bounds

    def _task_for_evidence(self, item: Dict[str, Any]) -> SystemTask:
        snapshot = item.get("system_task_snapshot") or {}
        if snapshot:
            task_data = dict(snapshot)
            task_data["requirements"] = tuple(
                SystemRequirementSpec(**raw) for raw in task_data.get("requirements") or []
            )
            return SystemTask(**task_data)
        task_id = str(item.get("system_task_id") or self.task.task_id)
        return next((task for task in self.project.tasks if task.task_id == task_id), self.task)

    def _direct_vector_delta(self, item: Dict[str, Any]) -> np.ndarray:
        task = self._task_for_evidence(item)
        return (
            self.direct_space.vector(item["params_a"], task)
            - self.direct_space.vector(item["params_b"], task)
        )

    def _joint_priors(self) -> Tuple[np.ndarray, np.ndarray]:
        direct = self.direct_space.prior_distribution() * self.PRODUCT_GROUP_SPAN * self.alpha_prior
        product = self.space.stage_prior() * self.PRODUCT_GROUP_SPAN * (1.0 - self.alpha_prior)
        return direct, product

    def _solve_joint(
        self,
        observations: Sequence[Dict[str, Any]],
    ) -> Tuple[np.ndarray, np.ndarray, List[float], bool]:
        direct_count = self.direct_space.variable_count
        product_count = self.space.variable_count
        direct_prior, product_prior = self._joint_priors()
        if not observations:
            return direct_prior, product_prior, [], True

        evidence_count = len(observations)
        direct_start = 0
        product_start = direct_count
        slack_start = product_start + product_count
        alpha_dev_pos = slack_start + evidence_count
        alpha_dev_neg = alpha_dev_pos + 1
        theta_count = direct_count + product_count
        theta_dev_pos = alpha_dev_neg + 1
        theta_dev_neg = theta_dev_pos + theta_count
        total = theta_dev_neg + theta_count

        objective = np.zeros(total)
        for index, item in enumerate(observations):
            objective[slack_start + index] = max(0.2, float(item.get("confidence", 1.0)))
        objective[alpha_dev_pos] = 2.0
        objective[alpha_dev_neg] = 2.0
        objective[theta_dev_pos:theta_dev_pos + theta_count] = 0.025
        objective[theta_dev_neg:theta_dev_neg + theta_count] = 0.025

        equalities: List[np.ndarray] = []
        equality_values: List[float] = []
        normalization = np.zeros(total)
        normalization[:theta_count] = 1.0
        equalities.append(normalization)
        equality_values.append(self.PRODUCT_GROUP_SPAN)

        alpha_prior_row = np.zeros(total)
        alpha_prior_row[direct_start:direct_start + direct_count] = 1.0
        alpha_prior_row[alpha_dev_pos] = -1.0
        alpha_prior_row[alpha_dev_neg] = 1.0
        equalities.append(alpha_prior_row)
        equality_values.append(self.alpha_prior * self.PRODUCT_GROUP_SPAN)

        combined_prior = np.concatenate([direct_prior, product_prior])
        for index, value in enumerate(combined_prior):
            row = np.zeros(total)
            row[index] = 1.0
            row[theta_dev_pos + index] = -1.0
            row[theta_dev_neg + index] = 1.0
            equalities.append(row)
            equality_values.append(float(value))

        inequalities, inequality_values = self._product_shape_rows(total, product_start)
        alpha_upper = np.zeros(total)
        alpha_upper[direct_start:direct_start + direct_count] = 1.0
        inequalities.append(alpha_upper)
        inequality_values.append(self.alpha_max * self.PRODUCT_GROUP_SPAN)
        alpha_lower = -alpha_upper
        inequalities.append(alpha_lower)
        inequality_values.append(-self.alpha_min * self.PRODUCT_GROUP_SPAN)

        for index, item in enumerate(observations):
            expression = np.zeros(total)
            expression[direct_start:direct_start + direct_count] = self._direct_vector_delta(item)
            expression[product_start:product_start + product_count] = self._product_delta(item)
            slack_index = slack_start + index
            if item["relation"] == "A":
                row = -expression
                row[slack_index] = -1.0
                inequalities.append(row)
                inequality_values.append(-self.STRICT_MARGIN)
            elif item["relation"] == "B":
                row = expression
                row[slack_index] = -1.0
                inequalities.append(row)
                inequality_values.append(-self.STRICT_MARGIN)
            else:
                row = expression.copy()
                row[slack_index] = -1.0
                inequalities.append(row)
                inequality_values.append(self.TIE_MARGIN)
                row = -expression
                row[slack_index] = -1.0
                inequalities.append(row)
                inequality_values.append(self.TIE_MARGIN)

        bounds: List[Tuple[Optional[float], Optional[float]]] = [
            (0.0, self.PRODUCT_GROUP_SPAN)
        ] * theta_count
        bounds.extend([(0.0, None)] * evidence_count)
        bounds.extend([(0.0, None), (0.0, None)])
        bounds.extend([(0.0, None)] * (2 * theta_count))
        result = linprog(
            objective,
            A_ub=np.asarray(inequalities) if inequalities else None,
            b_ub=np.asarray(inequality_values) if inequality_values else None,
            A_eq=np.asarray(equalities),
            b_eq=np.asarray(equality_values),
            bounds=bounds,
            method="highs",
        )
        if not result.success or result.x is None:
            return direct_prior, product_prior, [0.0] * evidence_count, False
        direct = np.asarray(result.x[direct_start:direct_start + direct_count], dtype=float)
        product = np.asarray(result.x[product_start:product_start + product_count], dtype=float)
        slacks = [float(item) for item in result.x[slack_start:slack_start + evidence_count]]
        return direct, product, slacks, True

    def _utility_delta_with_joint(
        self,
        item: Dict[str, Any],
        direct: np.ndarray,
        product: np.ndarray,
    ) -> float:
        return float(self._direct_vector_delta(item) @ direct + self._product_delta(item) @ product)

    def _joint_accuracy(
        self,
        evidence: Sequence[Dict[str, Any]],
        direct: np.ndarray,
        product: np.ndarray,
    ) -> Optional[float]:
        if not evidence:
            return None
        correct = 0
        for item in evidence:
            delta = self._utility_delta_with_joint(item, direct, product)
            if item["relation"] == "tie":
                correct += int(abs(delta) <= self.TIE_MARGIN)
            elif item["relation"] == "A":
                correct += int(delta > 0.0)
            else:
                correct += int(delta < 0.0)
        return correct / len(evidence)

    def _strict_joint_consistency(self, observations: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """M1: test whether one joint utility function satisfies every judgment without slack."""
        direct_count = self.direct_space.variable_count
        product_count = self.space.variable_count
        total = direct_count + product_count
        if not observations:
            return {"consistent": True, "solver_status": "no_evidence"}
        inequalities, limits = self._product_shape_rows(total, direct_count)
        alpha_upper = np.zeros(total)
        alpha_upper[:direct_count] = 1.0
        inequalities.append(alpha_upper)
        limits.append(self.alpha_max * self.PRODUCT_GROUP_SPAN)
        inequalities.append(-alpha_upper)
        limits.append(-self.alpha_min * self.PRODUCT_GROUP_SPAN)
        for item in observations:
            expression = np.concatenate([self._direct_vector_delta(item), self._product_delta(item)])
            if item["relation"] == "A":
                inequalities.append(-expression)
                limits.append(-self.STRICT_MARGIN)
            elif item["relation"] == "B":
                inequalities.append(expression)
                limits.append(-self.STRICT_MARGIN)
            else:
                inequalities.extend([expression, -expression])
                limits.extend([self.TIE_MARGIN, self.TIE_MARGIN])
        equality = np.ones((1, total))
        result = linprog(
            np.zeros(total),
            A_ub=np.asarray(inequalities) if inequalities else None,
            b_ub=np.asarray(limits) if limits else None,
            A_eq=equality,
            b_eq=np.asarray([self.PRODUCT_GROUP_SPAN]),
            bounds=[(0.0, self.PRODUCT_GROUP_SPAN)] * total,
            method="highs",
        )
        return {"consistent": bool(result.success), "solver_status": str(result.message)}

    def _joint_m3(self, observations: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """M3: locate a minimum-cardinality set of judgments that must be relaxed."""
        if not observations:
            return {
                "status": "not_required",
                "success": True,
                "minimum_conflicts": 0,
                "conflicting_evidence_ids": [],
            }
        if milp is None:
            return {
                "status": "solver_unavailable",
                "success": False,
                "minimum_conflicts": None,
                "conflicting_evidence_ids": [],
                "solver_status": "当前SciPy不提供milp，M3最小冲突集不可用。",
            }
        direct_count = self.direct_space.variable_count
        product_count = self.space.variable_count
        theta_count = direct_count + product_count
        evidence_count = len(observations)
        total = theta_count + evidence_count
        objective = np.zeros(total)
        objective[theta_count:] = 1.0
        integrality = np.zeros(total, dtype=int)
        integrality[theta_count:] = 1
        lower = np.zeros(total)
        upper = np.concatenate(
            [np.full(theta_count, self.PRODUCT_GROUP_SPAN), np.ones(evidence_count)]
        )
        rows, limits = self._product_shape_rows(total, direct_count)
        alpha_upper = np.zeros(total)
        alpha_upper[:direct_count] = 1.0
        rows.append(alpha_upper)
        limits.append(self.alpha_max * self.PRODUCT_GROUP_SPAN)
        rows.append(-alpha_upper)
        limits.append(-self.alpha_min * self.PRODUCT_GROUP_SPAN)
        for index, item in enumerate(observations):
            expression = np.zeros(total)
            expression[:direct_count] = self._direct_vector_delta(item)
            expression[direct_count:theta_count] = self._product_delta(item)
            conflict_index = theta_count + index
            if item["relation"] == "A":
                row = -expression
                row[conflict_index] = -self.M3_BIG_M
                rows.append(row)
                limits.append(-self.STRICT_MARGIN)
            elif item["relation"] == "B":
                row = expression
                row[conflict_index] = -self.M3_BIG_M
                rows.append(row)
                limits.append(-self.STRICT_MARGIN)
            else:
                for sign in (1.0, -1.0):
                    row = sign * expression
                    row[conflict_index] = -self.M3_BIG_M
                    rows.append(row)
                    limits.append(self.TIE_MARGIN)
        equality = np.zeros(total)
        equality[:theta_count] = 1.0
        matrix = np.vstack([np.asarray(rows), equality]) if rows else np.asarray([equality])
        result = milp(
            objective,
            integrality=integrality,
            bounds=Bounds(lower, upper),
            constraints=LinearConstraint(
                matrix,
                np.concatenate([np.full(len(rows), -np.inf), np.asarray([self.PRODUCT_GROUP_SPAN])]),
                np.concatenate([np.asarray(limits), np.asarray([self.PRODUCT_GROUP_SPAN])]),
            ),
            options={"time_limit": 10.0},
        )
        if not result.success or result.x is None:
            return {
                "status": "solver_failed",
                "success": False,
                "minimum_conflicts": None,
                "conflicting_evidence_ids": [],
                "solver_status": str(result.message),
            }
        selected = [
            observations[index].get("id")
            for index, value in enumerate(result.x[theta_count:])
            if value >= 0.5
        ]
        return {
            "status": "completed",
            "success": True,
            "minimum_conflicts": len(selected),
            "conflicting_evidence_ids": selected,
            "solver_status": str(result.message),
        }

    @staticmethod
    def _stable_digest(value: str) -> int:
        return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")

    def _joint_cross_validate(self, observations: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Task-stratified deterministic folds for the shared model."""
        items = list(observations)
        total_attributes = len(self.space.attributes) + len(self.direct_space.attributes)
        if len(items) < 12:
            return {"status": "insufficient_data", "folds": 0, "evaluated_pairs": 0, "accuracy": None}
        fold_count = min(5, max(3, len(items) // 8))
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for item in items:
            grouped.setdefault(str(item.get("system_task_id") or "unknown"), []).append(item)
        assignments: Dict[int, int] = {}
        for task_id, group in grouped.items():
            ordered = sorted(
                group,
                key=lambda item: self._stable_digest(
                    f"{task_id}|{item.get('id') or item.get('interaction_id') or repr(item)}"
                ),
            )
            for index, item in enumerate(ordered):
                assignments[id(item)] = index % fold_count
        results: List[Dict[str, Any]] = []
        weighted_correct = 0.0
        evaluated = 0
        for fold in range(fold_count):
            test = [item for item in items if assignments[id(item)] == fold]
            train = [item for item in items if assignments[id(item)] != fold]
            if not test or len(train) < max(6, total_attributes):
                continue
            direct, product, _slacks, success = self._solve_joint(train)
            if not success:
                continue
            accuracy = self._joint_accuracy(test, direct, product)
            assert accuracy is not None
            results.append(
                {
                    "fold": fold + 1,
                    "training_pairs": len(train),
                    "test_pairs": len(test),
                    "accuracy": round(accuracy, 3),
                }
            )
            weighted_correct += accuracy * len(test)
            evaluated += len(test)
        return {
            "status": "completed" if len(results) >= 2 else "insufficient_data",
            "folds": len(results),
            "evaluated_pairs": evaluated,
            "accuracy": round(weighted_correct / evaluated, 3) if evaluated else None,
            "fold_results": results,
            "method": "按系统任务分层的确定性K折交叉验证",
        }

    def _joint_whole_scheme_holdout(self, observations: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        items = [item for item in observations if item.get("scheme_a") and item.get("scheme_b")]
        scheme_ids = sorted({str(item[side]) for item in items for side in ("scheme_a", "scheme_b")})
        if len(items) < 12 or len(scheme_ids) < 5:
            return {
                "status": "insufficient_data",
                "held_out_schemes": [],
                "training_pairs": 0,
                "test_pairs": 0,
                "accuracy": None,
            }
        held_out = {item for item in scheme_ids if self._stable_digest(item) % 5 == 0}
        if not held_out or len(held_out) == len(scheme_ids):
            held_out = {scheme_ids[-1]}
        train = [
            item for item in items
            if str(item["scheme_a"]) not in held_out and str(item["scheme_b"]) not in held_out
        ]
        test = [
            item for item in items
            if str(item["scheme_a"]) in held_out or str(item["scheme_b"]) in held_out
        ]
        minimum_train = max(6, len(self.space.attributes) + len(self.direct_space.attributes))
        if len(train) < minimum_train or not test:
            return {
                "status": "insufficient_data",
                "held_out_schemes": sorted(held_out),
                "training_pairs": len(train),
                "test_pairs": len(test),
                "accuracy": None,
            }
        direct, product, _slacks, success = self._solve_joint(train)
        accuracy = self._joint_accuracy(test, direct, product) if success else None
        return {
            "status": "completed" if accuracy is not None else "solver_failed",
            "held_out_schemes": sorted(held_out),
            "training_pairs": len(train),
            "test_pairs": len(test),
            "accuracy": None if accuracy is None else round(accuracy, 3),
            "method": "整方案留出，检验未参加训练的新方案",
        }

    def _coverage_catalog(self) -> Dict[str, Dict[str, Any]]:
        catalog: Dict[str, Dict[str, Any]] = {}
        requirements = {item.system_key: item for item in self.task.requirements}
        for spec in self.direct_space.attributes:
            requirement = requirements.get(spec.key)
            if requirement is None:
                continue
            definitions = self.direct_space.branch_definition(requirement)
            for side in ("left", "right"):
                if abs(float(definitions[side]["sign"])) <= 1e-12:
                    continue
                for segment in range(self.segments):
                    slot_id = f"direct:{spec.key}:{side}:{segment}"
                    catalog[slot_id] = {
                        "slot_id": slot_id,
                        "channel": "direct_system",
                        "attribute_key": spec.key,
                        "attribute_label": spec.label,
                        "side": side,
                        "side_label": "要求以下" if side == "left" else "要求以上",
                        "segment": segment + 1,
                        "local": segment == 0,
                    }
        for spec in self.space.attributes:
            for segment in range(self.segments):
                slot_id = f"product:{spec.key}:{segment}"
                catalog[slot_id] = {
                    "slot_id": slot_id,
                    "channel": "product_performance",
                    "attribute_key": spec.key,
                    "attribute_label": spec.label,
                    "side": None,
                    "side_label": "全域",
                    "segment": segment + 1,
                    "local": False,
                }
        return catalog

    def pair_learning_slots(self, item: Dict[str, Any]) -> Dict[str, List[str]]:
        """Return the exact UTA increments touched by one effective comparison."""
        groups: Dict[str, List[str]] = {}
        task = self._task_for_evidence(item)
        requirements = {raw.system_key: raw for raw in task.requirements}
        mapped_a = self.direct_space.mapped_values(item["params_a"])
        mapped_b = self.direct_space.mapped_values(item["params_b"])
        for spec in self.direct_space.attributes:
            requirement = requirements.get(spec.key)
            if requirement is None:
                continue
            left = self.direct_space.attribute_vector(requirement, mapped_a[spec.key])
            right = self.direct_space.attribute_vector(requirement, mapped_b[spec.key])
            part = self.direct_space.attribute_slice(spec.key)
            local_delta = left[part] - right[part]
            slot_ids = []
            for index, value in enumerate(local_delta):
                if abs(float(value)) <= 1e-9:
                    continue
                side = "left" if index < self.segments else "right"
                segment = index if side == "left" else index - self.segments
                slot_ids.append(f"direct:{spec.key}:{side}:{segment}")
            if slot_ids:
                groups[f"direct:{spec.key}"] = slot_ids
        product_delta = self._product_delta(item)
        for spec, part in self.space.slices():
            slot_ids = [
                f"product:{spec.key}:{index}"
                for index, value in enumerate(product_delta[part])
                if abs(float(value)) > 1e-9
            ]
            if slot_ids:
                groups[f"product:{spec.key}"] = slot_ids
        return groups

    def learning_coverage(self, observations: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        catalog = self._coverage_catalog()
        counts = {key: 0 for key in catalog}
        focused_counts = {key: 0 for key in catalog}
        local_pairs = 0
        for item in self.active_evidence(observations):
            groups = self.pair_learning_slots(item)
            touched = [slot for slots in groups.values() for slot in slots if slot in catalog]
            if not touched:
                continue
            if int(item.get("causal_focus_version") or 0) >= 1:
                active_drivers = {
                    str(key)
                    for key in item.get("active_driver_keys") or []
                    if str(key)
                }
                focused = bool(item.get("controlled_variable_comparison")) and 1 <= len(active_drivers) <= 2
            else:
                focused = len(groups) <= 2 and all(len(slots) <= 2 for slots in groups.values())
            if any(catalog[slot]["local"] for slot in touched):
                local_pairs += 1
            for slot in set(touched):
                counts[slot] += 1
                if focused:
                    focused_counts[slot] += 1
        slots: List[Dict[str, Any]] = []
        for slot_id, metadata in catalog.items():
            count = counts[slot_id]
            focused_count = focused_counts[slot_id]
            slots.append(
                {
                    **metadata,
                    "evidence_count": count,
                    "focused_evidence_count": focused_count,
                    "covered": count >= 1,
                    "confident": count >= 2 and focused_count >= 1,
                }
            )
        local = [item for item in slots if item["local"]]
        covered = [item for item in slots if item["covered"]]
        confident = [item for item in slots if item["confident"]]
        local_covered = [item for item in local if item["covered"]]
        local_confident = [item for item in local if item["confident"]]
        by_attribute: List[Dict[str, Any]] = []
        attribute_keys = sorted({(item["channel"], item["attribute_key"]) for item in slots})
        for channel, key in attribute_keys:
            subset = [item for item in slots if item["channel"] == channel and item["attribute_key"] == key]
            by_attribute.append(
                {
                    "channel": channel,
                    "attribute_key": key,
                    "attribute_label": subset[0]["attribute_label"],
                    "slots": len(subset),
                    "covered_slots": sum(1 for item in subset if item["covered"]),
                    "confident_slots": sum(1 for item in subset if item["confident"]),
                    "coverage_ratio": round(sum(1 for item in subset if item["covered"]) / max(len(subset), 1), 3),
                }
            )
        ordered_targets = sorted(
            slots,
            key=lambda item: (
                0 if item["local"] and not item["confident"] else 1,
                item["evidence_count"],
                item["focused_evidence_count"],
                item["channel"],
                item["attribute_key"],
                item["segment"],
            ),
        )
        return {
            "definition": "只统计进入联合UTA的有效比较；覆盖表示触及，可靠表示至少2条且含1至2个主动设计变量的因果聚焦证据",
            "total_slots": len(slots),
            "covered_slots": len(covered),
            "confident_slots": len(confident),
            "full_coverage_ratio": round(len(covered) / max(len(slots), 1), 3),
            "full_confident_ratio": round(len(confident) / max(len(slots), 1), 3),
            "local_slots": len(local),
            "local_covered_slots": len(local_covered),
            "local_confident_slots": len(local_confident),
            "local_coverage_ratio": round(len(local_covered) / max(len(local), 1), 3) if local else 1.0,
            "local_confident_ratio": round(len(local_confident) / max(len(local), 1), 3) if local else 1.0,
            "local_effective_pairs": local_pairs,
            "slots": slots,
            "by_attribute": by_attribute,
            "next_targets": ordered_targets[:8],
        }

    def fit(self, evidence: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        observations = self.active_evidence(evidence)
        evidence_by_task: Dict[str, int] = {}
        for item in observations:
            task_id = str(item.get("system_task_id") or self.task.task_id)
            evidence_by_task[task_id] = evidence_by_task.get(task_id, 0) + 1
        train = list(observations)
        test: List[Dict[str, Any]] = []
        test_accuracy: Optional[float] = None
        train_direct: Optional[np.ndarray] = None
        train_product: Optional[np.ndarray] = None
        if len(observations) >= 8:
            test = [item for item in observations if self._test_member(item)] or [observations[-1]]
            test_ids = {id(item) for item in test}
            train = [item for item in observations if id(item) not in test_ids]
            train_direct, train_product, _train_slacks, train_success = self._solve_joint(train)
            if train_success:
                test_accuracy = self._joint_accuracy(test, train_direct, train_product)

        m1 = self._strict_joint_consistency(observations)
        self.direct_increments, self.increments, slacks, success = self._solve_joint(observations)
        self.alpha = float(self.direct_increments.sum()) / self.PRODUCT_GROUP_SPAN
        if train_direct is None or train_product is None:
            train_direct, train_product = self.direct_increments, self.increments
        training_accuracy = self._joint_accuracy(train, train_direct, train_product)
        consistency = self._joint_accuracy(observations, self.direct_increments, self.increments)
        m3 = self._joint_m3(observations) if observations and not m1["consistent"] else {
            "status": "not_required",
            "success": True,
            "minimum_conflicts": 0,
            "conflicting_evidence_ids": [],
        }
        m3_ids = set(m3.get("conflicting_evidence_ids") or [])
        tolerances = [
            {
                "evidence_id": item.get("id"),
                "slack": round(slack, 5),
                "high_tolerance": slack >= self.HIGH_TOLERANCE,
                "m3_conflict": item.get("id") in m3_ids,
            }
            for item, slack in zip(observations, slacks)
        ]
        high_tolerance = sum(1 for item in tolerances if item["high_tolerance"])
        cross_validation = self._joint_cross_validate(observations)
        scheme_holdout = self._joint_whole_scheme_holdout(observations)
        validation_values = [
            float(value)
            for value in (
                test_accuracy,
                cross_validation.get("accuracy"),
                scheme_holdout.get("accuracy"),
            )
            if value is not None
        ]
        conservative_validation = min(validation_values) if validation_values else None
        coverage = self.learning_coverage(observations)
        product_total = max(float(self.increments.sum()), 1e-12)
        attribute_weights = {
            spec.key: round(float(self.increments[part].sum()) / product_total, 6)
            for spec, part in self.space.slices()
        }
        direct_total = max(float(self.direct_increments.sum()), 1e-12)
        direct_attribute_weights = {
            spec.key: round(
                float(self.direct_increments[self.direct_space.attribute_slice(spec.key)].sum()) / direct_total,
                6,
            )
            for spec in self.direct_space.attributes
        }
        direct_branch_weights = {
            spec.key: {
                side: round(
                    float(self.direct_increments[self.direct_space.branch_slice(spec.key, side)].sum())
                    / direct_total,
                    6,
                )
                for side in ("left", "right")
            }
            for spec in self.direct_space.attributes
        }
        total_attributes = len(self.space.attributes) + len(self.direct_space.attributes)
        recommended = max(30, 4 * (total_attributes + 1))
        if not observations:
            model_status = "no_data"
            generalization_status = "insufficient_evidence"
        elif not m1["consistent"]:
            model_status = "needs_review"
            generalization_status = "conflicting_evidence"
        elif conservative_validation is None or len(observations) < recommended:
            model_status = "preliminary"
            generalization_status = "insufficient_evidence"
        elif conservative_validation >= 0.80:
            model_status = "validated"
            generalization_status = "whole_scheme_supported" if scheme_holdout.get("accuracy") is not None else "pair_validation_only"
        else:
            model_status = "needs_review"
            generalization_status = "unstable_on_holdout"
        self.summary_data = {
            "model_type": "requirement_anchored_weight_separated_joint_lp_uta_v11",
            "model_name": "要求锚定、权重分离的主动学习稳健联合LP-UTA V11",
            "status": model_status if success else "fallback_prior",
            "task_id": "shared_system_preference",
            "active_scoring_task_id": self.task.task_id,
            "source_task_ids": sorted(evidence_by_task),
            "evidence_by_task": evidence_by_task,
            "segments": self.segments,
            "attribute_count": total_attributes,
            "product_attribute_count": len(self.space.attributes),
            "direct_attribute_count": len(self.direct_space.attributes),
            "utility_increment_variables": self.space.variable_count + self.direct_space.variable_count,
            "product_increment_variables": self.space.variable_count,
            "direct_increment_variables": self.direct_space.variable_count,
            "evidence_pairs": len(observations),
            "training_pairs": len(train),
            "test_pairs": len(test),
            "training_accuracy": None if training_accuracy is None else round(training_accuracy, 3),
            "test_accuracy": None if test_accuracy is None else round(test_accuracy, 3),
            "judgment_consistency": None if consistency is None else round(consistency, 3),
            "judgment_consistency_definition": "全体有效判断在M2最小容忍联合UTA模型下的拟合正确率",
            "m1_consistent": bool(m1["consistent"]),
            "m1_label": "通过" if m1["consistent"] else "未通过",
            "m1_solver_status": m1.get("solver_status"),
            "m2_total_slack": round(sum(slacks), 5),
            "m2_max_slack": round(max(slacks or [0.0]), 5),
            "high_tolerance_threshold": self.HIGH_TOLERANCE,
            "high_tolerance_pairs": high_tolerance,
            "tolerances": tolerances,
            "m3": m3,
            "cross_validation": cross_validation,
            "whole_scheme_holdout": scheme_holdout,
            "conservative_validation_accuracy": (
                None if conservative_validation is None else round(conservative_validation, 3)
            ),
            "validation_components": {
                "fixed_holdout_accuracy": None if test_accuracy is None else round(test_accuracy, 3),
                "cross_validation_accuracy": cross_validation.get("accuracy"),
                "whole_scheme_holdout_accuracy": scheme_holdout.get("accuracy"),
                "aggregation": "取全部可用独立验证正确率的最低值",
            },
            "split_method": "固定哈希约80/20留出；交叉验证按系统任务分层；另做整方案留出",
            "generalization_status": generalization_status,
            "learning_coverage": coverage,
            "alpha_prior": round(self.alpha_prior, 6),
            "alpha_min": round(self.alpha_min, 6),
            "alpha_max": round(self.alpha_max, 6),
            "learned_alpha": round(self.alpha, 6),
            "attribute_weights": attribute_weights,
            "direct_attribute_weights": direct_attribute_weights,
            "direct_branch_weights": direct_branch_weights,
            "increments": [round(float(item), 10) for item in self.increments],
            "direct_increments": [round(float(item), 10) for item in self.direct_increments],
            "direct_side_limits": self.direct_space.side_limits,
            "product_reference_params": dict(self.space.reference_params),
            "product_attribute_keys": [item.key for item in self.space.attributes],
            "direct_attribute_keys": [item.key for item in self.direct_space.attributes],
            "direct_history_stats": {
                key: {
                    "minimum": round(float(min(values)), 10),
                    "median": round(float(statistics.median(values)), 10),
                    "maximum": round(float(max(values)), 10),
                }
                for key, values in self.direct_scorer.history_by_system.items() if values
            },
            "recommended_training_pairs": recommended,
            "score_definition": "直接属性单项分固定100/80工程锚点，UTA独立学习属性权重与受约束形状；任务邻域优先后补齐全域",
            "protocol_used_for_training": False,
            "task_requirements_used_as_context": True,
            "task_requirement_values_frozen_into_model": False,
            "direct_curve_parameters_learned": True,
            "system_contract_version": self.project.contract_version,
        }
        self.summary_data["marginal_curves"] = self.marginal_curves()
        self.summary_data["direct_uta_curves"] = self.direct_uta_curves()
        return dict(self.summary_data)

    def marginal_curves(self) -> List[Dict[str, Any]]:
        curves = super().marginal_curves()
        for curve in curves:
            curve["raw_piecewise_points"] = [dict(item) for item in curve.get("points", [])]
            curve["smooth_points"] = smooth_pchip_points(
                curve.get("points", []), "raw_value", ("utility", "prior_utility")
            )
            curve["display_curve"] = "shape_preserving_pchip"
            curve["scoring_curve"] = "piecewise_linear_lp_uta_v11"
        return curves

    def _direct_branch_context(
        self,
        requirement: SystemRequirementSpec,
        value: float,
    ) -> Tuple[Optional[str], Optional[str], float]:
        """Return active side, penalty/bonus role and normalized distance."""
        kind = requirement.requirement_type
        definitions = self.direct_space.branch_definition(requirement)
        side: Optional[str] = None
        role: Optional[str] = None
        if kind == "target" and requirement.target_value is not None:
            if value < float(requirement.target_value):
                side, role = "left", "penalty"
            elif value > float(requirement.target_value):
                side, role = "right", "penalty"
        elif kind == "at_least" and requirement.minimum is not None:
            if value < float(requirement.minimum):
                side, role = "left", "penalty"
            elif value > float(requirement.minimum) and requirement.overachievement_bonus:
                side, role = "right", "bonus"
        elif kind == "at_most" and requirement.maximum is not None:
            if value > float(requirement.maximum):
                side, role = "right", "penalty"
            elif value < float(requirement.maximum) and requirement.overachievement_bonus:
                side, role = "left", "bonus"
        elif kind == "within_range" and requirement.minimum is not None and requirement.maximum is not None:
            if value < float(requirement.minimum):
                side, role = "left", "penalty"
            elif value > float(requirement.maximum):
                side, role = "right", "penalty"
        if side is None:
            return None, None, 0.0
        definition = definitions[side]
        anchor = float(definition["anchor"])
        scale = max(float(definition["scale"]), 1e-12)
        distance = (anchor - value) / scale if side == "left" else (value - anchor) / scale
        return side, role, max(0.0, float(distance))

    def _direct_shape_blend(self, system_key: str, side: str) -> float:
        """Let focused evidence influence shape without moving engineering anchors."""
        coverage = dict(self.summary_data.get("learning_coverage") or {})
        slots = [
            item for item in coverage.get("slots") or []
            if item.get("channel") == "direct_system"
            and item.get("attribute_key") == system_key
            and item.get("side") == side
        ]
        if not slots:
            return 0.0
        evidence = sum(float(item.get("evidence_count", 0)) for item in slots) / max(len(slots), 1)
        focused = sum(float(item.get("focused_evidence_count", 0)) for item in slots) / max(len(slots), 1)
        effective = evidence + 0.5 * focused
        if effective <= 0.0:
            return 0.0
        half = self.DIRECT_SHAPE_EVIDENCE_HALF_SATURATION
        return self.DIRECT_SHAPE_BLEND_CAP * effective / (effective + half)

    @staticmethod
    def _anchor_normalized_progress(
        raw_progress: float,
        raw_anchor_progress: float,
        distance: float,
        side_limit: float,
        anchor_fraction: float,
    ) -> float:
        """Normalize a learned monotone branch through a fixed distance-one anchor."""
        raw_progress = clamp(float(raw_progress), 0.0, 1.0)
        raw_anchor_progress = clamp(float(raw_anchor_progress), 0.0, 1.0)
        anchor_fraction = clamp(float(anchor_fraction), 0.0, 1.0)
        if distance <= 1.0:
            if raw_anchor_progress <= 1e-12:
                return anchor_fraction * clamp(distance, 0.0, 1.0)
            return clamp(anchor_fraction * raw_progress / raw_anchor_progress, 0.0, anchor_fraction)
        if raw_anchor_progress >= 1.0 - 1e-12:
            tail = clamp((distance - 1.0) / max(side_limit - 1.0, 1e-12), 0.0, 1.0)
            return anchor_fraction + (1.0 - anchor_fraction) * tail
        tail = (raw_progress - raw_anchor_progress) / max(1.0 - raw_anchor_progress, 1e-12)
        return clamp(anchor_fraction + (1.0 - anchor_fraction) * tail, anchor_fraction, 1.0)

    def direct_marginal_score(
        self,
        requirement: SystemRequirementSpec,
        value: float,
    ) -> Dict[str, Any]:
        """Return an unweighted task score with fixed 100/80 engineering anchors."""
        prior_score, satisfied = self.direct_scorer.score_mapped_value(float(value), requirement)
        side, role, distance = self._direct_branch_context(requirement, float(value))
        if side is None or role is None or requirement.system_key not in self.direct_space.attribute_by_key:
            return {
                "score": float(prior_score),
                "configured_prior_score": float(prior_score),
                "learned_shape_score": float(prior_score),
                "shape_learning_blend": 0.0,
                "side": side,
                "role": role,
                "normalized_distance": distance,
                "satisfied": satisfied,
            }
        part = self.direct_space.branch_slice(requirement.system_key, side)
        increments = np.asarray(self.direct_increments[part], dtype=float)
        total = float(increments.sum())
        if total <= 1e-12:
            shape_score = float(prior_score)
            blend = 0.0
        else:
            distribution = increments / total
            limit = float(self.direct_space.side_limits[requirement.system_key][side])
            raw_progress = float(self.direct_space._basis(distance, limit) @ distribution)
            raw_anchor = float(self.direct_space._basis(1.0, limit) @ distribution)
            if role == "penalty":
                progress = self._anchor_normalized_progress(raw_progress, raw_anchor, distance, limit, 0.20)
                shape_score = 100.0 * (1.0 - progress)
            else:
                bonus_anchor = 1.0 - math.exp(-0.5)
                progress = self._anchor_normalized_progress(
                    raw_progress, raw_anchor, distance, limit, bonus_anchor
                )
                shape_score = 100.0 + max(0.0, float(requirement.score_cap) - 100.0) * progress
            blend = self._direct_shape_blend(requirement.system_key, side)
        score = float(prior_score) + blend * (float(shape_score) - float(prior_score))
        if role == "penalty" and math.isclose(distance, 1.0, rel_tol=0.0, abs_tol=1e-9):
            score = 80.0
        score = clamp(score, 0.0, max(100.0, float(requirement.score_cap)))
        return {
            "score": float(score),
            "configured_prior_score": float(prior_score),
            "learned_shape_score": float(shape_score),
            "shape_learning_blend": float(blend),
            "side": side,
            "role": role,
            "normalized_distance": float(distance),
            "satisfied": satisfied,
        }

    def direct_uta_curves(self) -> List[Dict[str, Any]]:
        direct_total = max(float(self.direct_increments.sum()), 1e-12)
        curves: List[Dict[str, Any]] = []
        for requirement in self.task.requirements:
            if requirement.system_key not in self.direct_space.attribute_by_key:
                continue
            base = self.direct_scorer.requirement_curve(requirement)
            definitions = self.direct_space.branch_definition(requirement)
            raw_values = set()
            for side in ("left", "right"):
                definition = definitions[side]
                anchor = float(definition["anchor"])
                scale = float(definition["scale"])
                limit = self.direct_space.side_limits[requirement.system_key][side]
                raw_values.add(anchor - scale if side == "left" else anchor + scale)
                for index in range(self.segments + 1):
                    distance = limit * index / self.segments
                    raw_values.add(anchor - distance * scale if side == "left" else anchor + distance * scale)
            if requirement.requirement_type == "within_range":
                if requirement.minimum is not None:
                    raw_values.add(float(requirement.minimum))
                if requirement.maximum is not None:
                    raw_values.add(float(requirement.maximum))
            points: List[Dict[str, Any]] = []
            for value in sorted(raw_values):
                marginal = self.direct_marginal_score(requirement, value)
                points.append(
                    {
                        "value": round(float(value), 8),
                        "score": round(float(marginal["score"]), 6),
                        "utility": round(float(marginal["score"]), 6),
                        "prior_utility": round(float(marginal["configured_prior_score"]), 6),
                        "satisfied": marginal["satisfied"],
                    }
                )
            smooth = smooth_pchip_points(points, "value", ("utility",), 161)
            for row in smooth:
                marginal = self.direct_marginal_score(requirement, float(row["value"]))
                row["score"] = row["utility"]
                row["prior_utility"] = round(float(marginal["configured_prior_score"]), 6)
                row["satisfied"] = marginal["satisfied"]
            part = self.direct_space.attribute_slice(requirement.system_key)
            curves.append(
                {
                    **base,
                    "points": points,
                    "raw_piecewise_points": [dict(item) for item in points],
                    "smooth_points": smooth,
                    "attribute_weight": round(float(self.direct_increments[part].sum()) / direct_total, 6),
                    "left_branch_weight": round(
                        float(self.direct_increments[self.direct_space.branch_slice(requirement.system_key, "left")].sum())
                        / direct_total,
                        6,
                    ),
                    "right_branch_weight": round(
                        float(self.direct_increments[self.direct_space.branch_slice(requirement.system_key, "right")].sum())
                        / direct_total,
                        6,
                    ),
                    "configured_not_learned": False,
                    "learned_from_expert_comparisons": True,
                    "display_curve": "shape_preserving_pchip",
                    "scoring_curve": "requirement_anchored_weight_separated_lp_uta_v11",
                    "fixed_engineering_anchors": {"requirement_score": 100.0, "distance_one_score": 80.0},
                }
            )
        return curves

    def requirement_curves(self) -> List[Dict[str, Any]]:
        return self.direct_uta_curves()

    def direct_score(self, params: Dict[str, Any]) -> Dict[str, Any]:
        diagnostics = self.direct_scorer.assess(params)
        attributes: List[Dict[str, Any]] = []
        direct_total = max(float(self.direct_increments.sum()), 1e-12)
        for item, requirement in zip(diagnostics["attributes"], self.task.requirements):
            copy = dict(item)
            if requirement.system_key in self.direct_space.attribute_by_key:
                part = self.direct_space.attribute_slice(requirement.system_key)
                learned_weight = float(self.direct_increments[part].sum()) / direct_total
            else:
                learned_weight = 0.0
            marginal = self.direct_marginal_score(requirement, float(copy["value"]))
            marginal_score = float(marginal["score"])
            delta = learned_weight * (marginal_score - 100.0)
            copy["configured_prior_score"] = round(float(marginal["configured_prior_score"]), 3)
            copy["learned_shape_score"] = round(float(marginal["learned_shape_score"]), 3)
            copy["shape_learning_blend"] = round(float(marginal["shape_learning_blend"]), 6)
            copy["score"] = round(marginal_score, 3)
            copy["unweighted_task_score"] = round(marginal_score, 3)
            copy["score_curve"] = "requirement_anchored_weight_separated_lp_uta_v11"
            copy["group_weight"] = round(learned_weight, 6)
            copy["direct_point_contribution"] = round(delta, 3)
            copy["weight_source"] = "联合LP-UTA学习属性权重；单属性100/80工程锚点独立保持"
            copy["normalized_deviation"] = round(float(marginal["normalized_distance"]), 6)
            attributes.append(copy)
        score = 100.0 + sum(float(item["direct_point_contribution"]) for item in attributes)
        return {
            **diagnostics,
            "score": round(score, 3),
            "attributes": attributes,
            "weighting": "单属性任务满足分与属性权重分离；联合LP-UTA学习权重和受锚点约束的边际形状",
        }

    def assess(self, params: Dict[str, Any]) -> Dict[str, Any]:
        direct = self.direct_score(params)
        product_score, product_contributions = self.product_score(params)
        raw_total = self.alpha * float(direct["score"]) + (1.0 - self.alpha) * product_score
        hard_cap = direct.get("hard_cap")
        decision_score = raw_total if hard_cap is None else min(raw_total, float(hard_cap))
        hard_failures = [
            item
            for item in direct.get("failed_requirements", [])
            if item.get("hard_policy") in {"veto", "cap_total"}
        ]
        qualification_status = (
            "unqualified_hard_requirement"
            if hard_failures
            else "qualified" if direct.get("failed_requirement_count", 0) == 0 else "qualified_with_gaps"
        )
        direct_contributions = []
        for item in direct["attributes"]:
            copy = dict(item)
            copy["total_point_contribution"] = round(
                self.alpha * float(item.get("direct_point_contribution", 0.0)), 3
            )
            direct_contributions.append(copy)
        total_contributions = sorted(
            [
                {
                    "label": item["system_attribute"],
                    "channel": "direct_system_uta",
                    "contribution": item["total_point_contribution"],
                }
                for item in direct_contributions
            ]
            + [
                {
                    "label": item["product_label"],
                    "channel": "product_performance_uta",
                    "contribution": item["total_point_contribution"],
                }
                for item in product_contributions
            ],
            key=lambda item: abs(float(item["contribution"])),
            reverse=True,
        )
        confidence = min(0.98, 0.45 + 0.012 * int(self.summary_data.get("evidence_pairs", 0)))
        return {
            "method": "要求锚定、权重分离的主动学习稳健联合LP-UTA V11",
            "task": asdict(self.task),
            "total_score": round(decision_score, 2),
            "continuous_score": round(raw_total, 2),
            "raw_total_score": round(raw_total, 2),
            "decision_score": round(decision_score, 2),
            "direct_score": round(float(direct["score"]), 2),
            "product_score": round(product_score, 2),
            "alpha": round(self.alpha, 4),
            "alpha_prior": round(self.alpha_prior, 4),
            "alpha_bounds": [round(self.alpha_min, 4), round(self.alpha_max, 4)],
            "direct_assessment": {**direct, "attributes": direct_contributions},
            "product_reference": "历史成品样本各属性稳健中位数（经验中性参考，不是系统要求）",
            "product_contributions": product_contributions,
            "top_contributors": total_contributions[:6],
            "confidence": round(confidence, 3),
            "hard_cap_applied": hard_cap,
            "hard_requirement_failed_count": len(hard_failures),
            "hard_requirement_failures": hard_failures,
            "qualification_status": qualification_status,
            "compliance_status": direct.get("compliance_status"),
            "failed_requirement_count": direct.get("failed_requirement_count", 0),
            "formula": "U_continuous = alpha * sum(w_j * S_j) + (1-alpha) * U_product_UTA；硬性策略再形成U_decision",
        }


__all__ = [
    "HierarchicalUTAModel",
    "DirectRequirementFeatureSpace",
    "ProductFeatureSpace",
    "SystemDirectScorer",
    "smooth_pchip_points",
]
