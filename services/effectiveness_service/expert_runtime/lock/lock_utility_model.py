# -*- coding: utf-8 -*-
"""Baseline-relative degradation utilities for the lock-specialized V17 flow."""

from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from preference_models import DEFAULT_UTA_SEGMENTS, LPUTAModel, OnlineBTModel
from project_excel import AttributeSpec, ProjectDataError, ProjectDataset, RequirementProfile


REUSE_STATUS_LABELS = {
    "reusable": "可以复用",
    "not_reusable": "不建议直接复用",
    "uncertain": "仍需专家判断",
}
ADAPTIVE_BOUNDARY_COORDINATE = 0.75


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _round_value(value: float, spec: AttributeSpec) -> Any:
    if spec.data_type == "integer":
        return int(round(value))
    return round(float(value), int(spec.precision))


class BaselineDegradationSpace:
    """Map raw lock attributes to dimensionless goodness capped at the baseline."""

    def __init__(self, project: ProjectDataset, profile: RequirementProfile):
        self.project = project
        self.profile = profile
        self.attribute_by_key = project.attribute_by_key()
        self.requirement_by_key = profile.requirement_by_key()
        self.warnings: List[str] = []
        self.baseline_params: Dict[str, Any] = self._build_baseline_params()
        self.attributes: List[AttributeSpec] = []
        self.dimensions: List[Dict[str, Any]] = []
        self._configure_dimensions()
        if not self.attributes:
            raise ProjectDataError("当前基准没有可用于锁特化效能训练的数值属性。")
        self.virtual_project = self._build_virtual_project()

    def _reference_value(self, spec: AttributeSpec) -> Optional[float]:
        requirement = self.requirement_by_key.get(spec.key)
        if requirement is None:
            return None
        if requirement.target_value is not None:
            return float(requirement.target_value)
        if requirement.requirement_type in {"at_least", "higher_better"}:
            return None if requirement.minimum is None else float(requirement.minimum)
        if requirement.requirement_type in {"at_most", "lower_better"}:
            return None if requirement.maximum is None else float(requirement.maximum)
        if requirement.minimum is not None and requirement.maximum is not None:
            return 0.5 * (float(requirement.minimum) + float(requirement.maximum))
        return None

    def _fallback_value(self, spec: AttributeSpec) -> Any:
        values = [
            float(scheme.params[spec.key])
            for scheme in self.project.schemes
            if scheme.params.get(spec.key) is not None
        ]
        if values:
            return _round_value(float(np.median(np.asarray(values, dtype=float))), spec)
        if spec.is_numeric:
            assert spec.generation_min is not None and spec.generation_max is not None
            return _round_value(0.5 * (spec.generation_min + spec.generation_max), spec)
        return ""

    def _build_baseline_params(self) -> Dict[str, Any]:
        output: Dict[str, Any] = {}
        for spec in self.project.attributes:
            if not spec.is_numeric:
                output[spec.key] = self._fallback_value(spec)
                continue
            reference = self._reference_value(spec)
            if reference is None:
                output[spec.key] = self._fallback_value(spec)
                self.warnings.append(
                    f"基准技术协议未填写“{spec.label}”，当前仅用历史中位数补齐展示；该属性不进入训练。"
                )
                continue
            assert spec.generation_min is not None and spec.generation_max is not None
            if not float(spec.generation_min) <= reference <= float(spec.generation_max):
                raise ProjectDataError(
                    f"“{spec.label}”的100分基准值 {reference:g} 不在生成范围 "
                    f"[{float(spec.generation_min):g}, {float(spec.generation_max):g}] 内。"
                )
            output[spec.key] = _round_value(reference, spec)
        return output

    def _configure_dimensions(self) -> None:
        for spec in self.project.attributes:
            if not (
                spec.is_numeric
                and spec.participates_utility
                and spec.participates_generation
                and spec.key in self.requirement_by_key
            ):
                continue
            assert spec.generation_min is not None and spec.generation_max is not None
            baseline = float(self.baseline_params[spec.key])
            preference = spec.preference_direction
            if preference == "higher_better" and baseline > float(spec.generation_min) + 1e-12:
                self.attributes.append(spec)
                self.dimensions.append(self._dimension(spec, "lower", float(spec.generation_min)))
            elif preference == "lower_better" and baseline < float(spec.generation_max) - 1e-12:
                self.attributes.append(spec)
                self.dimensions.append(self._dimension(spec, "upper", float(spec.generation_max)))
            elif preference == "interval":
                added = False
                self.attributes.append(spec)
                if baseline > float(spec.generation_min) + 1e-12:
                    self.dimensions.append(self._dimension(spec, "lower", float(spec.generation_min)))
                    added = True
                if baseline < float(spec.generation_max) - 1e-12:
                    self.dimensions.append(self._dimension(spec, "upper", float(spec.generation_max)))
                    added = True
                if not added:
                    self.attributes.pop()
            else:
                self.warnings.append(
                    f"“{spec.label}”没有可用的单向退化方向，已从 V17 训练序列中跳过。"
                )
        unique: Dict[str, AttributeSpec] = {}
        for spec in self.attributes:
            unique[spec.key] = spec
        self.attributes = list(unique.values())

    def _dimension(self, spec: AttributeSpec, side: str, bad_edge: float) -> Dict[str, Any]:
        baseline = float(self.baseline_params[spec.key])
        return {
            "id": f"{spec.key}:{side}",
            "attribute_key": spec.key,
            "attribute_label": spec.label,
            "unit": spec.unit,
            "side": side,
            "side_label": "向低值退化" if side == "lower" else "向高值退化",
            "baseline_value": baseline,
            "bad_edge": bad_edge,
            "span": abs(bad_edge - baseline),
            "precision": spec.precision,
            "design_stage": spec.design_stage,
            "preference_direction": spec.preference_direction,
        }

    def _build_virtual_project(self) -> ProjectDataset:
        virtual_attributes = [
            AttributeSpec(
                key=spec.key,
                label=spec.label,
                unit="相对基准",
                data_type="continuous",
                design_stage=spec.design_stage,
                precision=4,
                generation_min=0.0,
                generation_max=1.0,
                feasible_min=0.0,
                feasible_max=1.0,
                preference_direction="higher_better",
                marginal_trend="待确认",
                participates_utility=True,
                participates_generation=True,
                description=f"{spec.label}相对基准的无量纲保有效用",
            )
            for spec in self.attributes
        ]
        return ProjectDataset(
            product_code=f"{self.project.product_code}_LOCK_V17",
            project_name=f"{self.project.project_name} - 锁特化退化效能",
            workbook_path=self.project.workbook_path,
            workbook_fingerprint=self.project.workbook_fingerprint,
            learning_fingerprint=f"lock_v17_{self.project.learning_fingerprint}",
            attributes=virtual_attributes,
            schemes=[],
            couplings=[],
            requirement_profiles=[],
            warnings=list(self.warnings),
        )

    @property
    def dimension_by_id(self) -> Dict[str, Dict[str, Any]]:
        return {item["id"]: item for item in self.dimensions}

    def deterioration_for_dimension(self, value: float, dimension: Dict[str, Any]) -> float:
        baseline = float(dimension["baseline_value"])
        span = max(float(dimension["span"]), 1e-12)
        if dimension["side"] == "lower":
            return max(0.0, (baseline - float(value)) / span)
        return max(0.0, (float(value) - baseline) / span)

    def deterioration(self, params: Dict[str, Any], spec: AttributeSpec) -> Tuple[float, str]:
        value = float(params[spec.key])
        baseline = float(self.baseline_params[spec.key])
        if spec.preference_direction == "higher_better":
            dimension = self.dimension_by_id.get(f"{spec.key}:lower")
            return (0.0, "better_or_equal") if value >= baseline or dimension is None else (
                self.deterioration_for_dimension(value, dimension),
                "lower",
            )
        if spec.preference_direction == "lower_better":
            dimension = self.dimension_by_id.get(f"{spec.key}:upper")
            return (0.0, "better_or_equal") if value <= baseline or dimension is None else (
                self.deterioration_for_dimension(value, dimension),
                "upper",
            )
        if value < baseline:
            dimension = self.dimension_by_id.get(f"{spec.key}:lower")
            return (0.0, "better_or_equal") if dimension is None else (
                self.deterioration_for_dimension(value, dimension),
                "lower",
            )
        if value > baseline:
            dimension = self.dimension_by_id.get(f"{spec.key}:upper")
            return (0.0, "better_or_equal") if dimension is None else (
                self.deterioration_for_dimension(value, dimension),
                "upper",
            )
        return 0.0, "better_or_equal"

    def goodness_params(self, params: Dict[str, Any]) -> Dict[str, float]:
        output: Dict[str, float] = {}
        for spec in self.attributes:
            deterioration, _side = self.deterioration(params, spec)
            output[spec.key] = 1.0 - clamp(deterioration, 0.0, 1.0)
        return output

    def value_at(self, dimension_id: str, deterioration: float) -> Any:
        dimension = self.dimension_by_id[dimension_id]
        raw = float(dimension["baseline_value"]) + clamp(deterioration, 0.0, 1.0) * (
            float(dimension["bad_edge"]) - float(dimension["baseline_value"])
        )
        return _round_value(raw, self.attribute_by_key[dimension["attribute_key"]])

    def scheme_from_changes(self, changes: Dict[str, float]) -> Dict[str, Any]:
        params = copy.deepcopy(self.baseline_params)
        for dimension_id, deterioration in changes.items():
            params[self.dimension_by_id[dimension_id]["attribute_key"]] = self.value_at(
                dimension_id,
                deterioration,
            )
        return params

    def baseline_payload(self) -> Dict[str, Any]:
        return {
            "profile_id": self.profile.id,
            "profile_name": self.profile.name,
            "score": 100.0,
            "params": copy.deepcopy(self.baseline_params),
            "warnings": list(self.warnings),
        }

    def schema_payload(self) -> Dict[str, Any]:
        return {
            "attributes": [asdict(item) for item in self.project.attributes],
            "trainable_attribute_keys": [item.key for item in self.attributes],
            "dimensions": copy.deepcopy(self.dimensions),
            "baseline": self.baseline_payload(),
        }


class LockPreferenceLearner:
    """Reuse V15 BT/LP-UTA on baseline-capped goodness coordinates."""

    def __init__(
        self,
        space: BaselineDegradationSpace,
        segments: int = DEFAULT_UTA_SEGMENTS,
        bt_weights: Optional[Sequence[float]] = None,
        uta_increments: Optional[Sequence[float]] = None,
        boundary_scales: Optional[Dict[str, float]] = None,
    ):
        self.space = space
        self.segments = int(segments)
        self.boundary_scales = {
            str(key): clamp(float(value), 1e-6, 1.0)
            for key, value in (boundary_scales or {}).items()
        }
        self.bt = OnlineBTModel(space.virtual_project, weights=bt_weights)
        self.uta = LPUTAModel(
            space.virtual_project,
            segments=self.segments,
            increments=uta_increments,
        )

    def scale_for(self, spec: AttributeSpec, side: str) -> float:
        dimension_id = f"{spec.key}:{side}"
        return float(self.boundary_scales.get(dimension_id, 1.0))

    def effective_deterioration(
        self,
        raw_deterioration: float,
        scale: float,
    ) -> float:
        raw = clamp(raw_deterioration, 0.0, 1.0)
        pivot = clamp(scale, 1e-6, 1.0)
        if pivot >= 1.0 - 1e-9:
            return raw
        if raw <= pivot:
            return ADAPTIVE_BOUNDARY_COORDINATE * raw / pivot
        return ADAPTIVE_BOUNDARY_COORDINATE + (
            (1.0 - ADAPTIVE_BOUNDARY_COORDINATE)
            * (raw - pivot)
            / max(1.0 - pivot, 1e-12)
        )

    def transformed_params(self, params: Dict[str, Any]) -> Dict[str, float]:
        output: Dict[str, float] = {}
        for spec in self.space.attributes:
            deterioration, side = self.space.deterioration(params, spec)
            scale = self.scale_for(spec, side)
            effective = self.effective_deterioration(deterioration, scale)
            output[spec.key] = 1.0 - clamp(effective, 0.0, 1.0)
        return output

    def transformed_evidence(self, evidence: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        output = []
        for item in evidence:
            transformed = copy.deepcopy(item)
            transformed["params_a"] = self.transformed_params(item["params_a"])
            transformed["params_b"] = self.transformed_params(item["params_b"])
            output.append(transformed)
        return output


    def fit(self, evidence: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        transformed = self.transformed_evidence(evidence)
        bt_summary = self.bt.fit(transformed)
        uta_summary = self.uta.fit(transformed)
        uta_summary["coordinate_system"] = "single_boundary_adaptive_dimensionless_goodness"
        uta_summary["adaptive_boundary_coordinate"] = ADAPTIVE_BOUNDARY_COORDINATE
        uta_summary["boundary_scales"] = copy.deepcopy(self.boundary_scales)
        uta_summary["score_cap"] = 100.0
        uta_summary["baseline_score"] = 100.0
        uta_summary["marginal_loss_curves"] = self.loss_curves()
        return bt_summary, uta_summary

    def score(self, params: Dict[str, Any], prefer_uta: bool = True) -> float:
        transformed = self.transformed_params(params)
        value = self.uta.score(transformed) if prefer_uta else self.bt.score(transformed)
        return round(clamp(value, 0.0, 100.0), 2)

    def pair_probability(self, params_a: Dict[str, Any], params_b: Dict[str, Any]) -> float:
        return self.bt.probability(
            self.transformed_params(params_a),
            self.transformed_params(params_b),
        )

    def loss_contributions(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        transformed = self.transformed_params(params)
        vector = self.uta.space.segment_vector(transformed)
        output: List[Dict[str, Any]] = []
        for spec, attr_slice in self.uta.space.attribute_slices():
            increments = self.uta.increments[attr_slice]
            current = float(vector[attr_slice] @ increments)
            weight = float(np.sum(increments))
            original = self.space.attribute_by_key[spec.key]
            deterioration, side = self.space.deterioration(params, original)
            effective = self.effective_deterioration(
                deterioration,
                self.scale_for(original, side),
            )
            output.append(
                {
                    "attribute_key": spec.key,
                    "attribute_label": original.label,
                    "unit": original.unit,
                    "attribute_weight": round(weight, 6),
                    "current_utility_points": round(100.0 * current, 3),
                    "baseline_utility_points": round(100.0 * weight, 3),
                    "loss_points": round(100.0 * max(0.0, weight - current), 3),
                    "deterioration": round(deterioration, 6),
                    "effective_deterioration": round(effective, 6),
                    "deterioration_side": side,
                }
            )
        return sorted(output, key=lambda item: item["loss_points"], reverse=True)

    def loss_map(self, params: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {
            item["attribute_key"]: item
            for item in self.loss_contributions(params)
        }

    def loss_curves(self, point_count: int = 41) -> List[Dict[str, Any]]:
        output = []
        for spec, attr_slice in self.uta.space.attribute_slices():
            increments = self.uta.increments[attr_slice]
            weight = float(np.sum(increments))
            points = []
            original = self.space.attribute_by_key[spec.key]
            dimension = next(
                item for item in self.space.dimensions if item["attribute_key"] == spec.key
            )
            scale = self.scale_for(original, dimension["side"])
            for deterioration in np.linspace(0.0, 1.0, point_count):
                effective = self.effective_deterioration(float(deterioration), scale)
                goodness = 1.0 - effective
                scaled = goodness * self.segments
                basis = np.asarray(
                    [clamp(scaled - segment, 0.0, 1.0) for segment in range(self.segments)],
                    dtype=float,
                )
                current = float(basis @ increments)
                points.append(
                    {
                        "deterioration": round(float(deterioration), 4),
                        "effective_deterioration": round(float(effective), 4),
                        "raw_value": self.space.value_at(
                            dimension["id"],
                            float(deterioration),
                        ),
                        "support": "core" if float(deterioration) <= scale + 1e-9 else "tail",
                        "retained_points": round(100.0 * current, 4),
                        "loss_points": round(100.0 * max(0.0, weight - current), 4),
                    }
                )
            output.append(
                {
                    "attribute_key": spec.key,
                    "attribute_label": original.label,
                    "unit": original.unit,
                    "attribute_weight": round(weight, 6),
                    "maximum_points": round(100.0 * weight, 3),
                    "dimension_id": dimension["id"],
                    "side_label": dimension["side_label"],
                    "adaptive_scale": round(scale, 6),
                    "adaptive_boundary_coordinate": ADAPTIVE_BOUNDARY_COORDINATE,
                    "points": points,
                }
            )
        return output


def _migrated_limit_value(
    learner: LockPreferenceLearner,
    evaluation_space: BaselineDegradationSpace,
    attribute_key: str,
    side: str,
    baseline_loss: float,
    loss_budget: float,
) -> Optional[Any]:
    dimension_id = f"{attribute_key}:{side}"
    if dimension_id not in evaluation_space.dimension_by_id:
        return None
    params = copy.deepcopy(evaluation_space.baseline_params)

    def incremental_loss(deterioration: float) -> float:
        params[attribute_key] = evaluation_space.value_at(dimension_id, deterioration)
        loss = learner.loss_map(params).get(attribute_key, {}).get("loss_points", 0.0)
        return max(0.0, float(loss) - float(baseline_loss))

    if incremental_loss(1.0) < float(loss_budget) - 1e-8:
        return evaluation_space.value_at(dimension_id, 1.0)
    low, high = 0.0, 1.0
    for _ in range(42):
        middle = 0.5 * (low + high)
        if incremental_loss(middle) >= float(loss_budget):
            high = middle
        else:
            low = middle
    return evaluation_space.value_at(dimension_id, 0.5 * (low + high))


def reanchored_effectiveness_score(
    learner: LockPreferenceLearner,
    evaluation_space: BaselineDegradationSpace,
    params: Dict[str, Any],
) -> float:
    """Score by transferring each learned curve shape to the active baseline."""
    master_space = learner.space
    transferred = copy.deepcopy(master_space.baseline_params)
    for spec in master_space.attributes:
        deterioration, side = evaluation_space.deterioration(params, spec)
        dimension_id = f"{spec.key}:{side}"
        if deterioration > 1e-12 and dimension_id in master_space.dimension_by_id:
            transferred[spec.key] = master_space.value_at(
                dimension_id,
                deterioration,
            )
    baseline_losses = learner.loss_map(master_space.baseline_params)
    candidate_losses = learner.loss_map(transferred)
    total_loss = sum(
        max(
            0.0,
            float(candidate_losses[key]["loss_points"])
            - float(baseline_losses[key]["loss_points"]),
        )
        for key in candidate_losses
    )
    return round(clamp(100.0 - total_loss, 0.0, 100.0), 2)


def evaluate_migrated_reuse_model(
    master_space: BaselineDegradationSpace,
    evaluation_space: BaselineDegradationSpace,
    learner: LockPreferenceLearner,
    params: Dict[str, Any],
    boundaries: Dict[str, Dict[str, Any]],
    threshold_model: Dict[str, Any],
    master_profile_id: str,
    evaluation_profile_id: str,
    uta_ready: bool,
    support_by_dimension: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Transfer learned marginal curve shapes to another, usually lower, baseline."""
    support = support_by_dimension or {}
    master_baseline_losses = learner.loss_map(master_space.baseline_params)
    evaluation_baseline_losses = learner.loss_map(evaluation_space.baseline_params)
    raw_candidate_losses = learner.loss_map(params)
    transferred_params = copy.deepcopy(master_space.baseline_params)
    evaluation_coordinates: Dict[str, Tuple[float, str]] = {}
    for spec in master_space.attributes:
        deterioration, side = evaluation_space.deterioration(params, spec)
        evaluation_coordinates[spec.key] = (deterioration, side)
        dimension_id = f"{spec.key}:{side}"
        if deterioration > 1e-12 and dimension_id in master_space.dimension_by_id:
            transferred_params[spec.key] = master_space.value_at(
                dimension_id,
                deterioration,
            )
    candidate_losses = learner.loss_map(transferred_params)
    attribute_results: List[Dict[str, Any]] = []
    contributions: List[Dict[str, Any]] = []
    clear_violations: List[str] = []
    uncertain_attributes: List[str] = []
    migration_flags: List[str] = []

    for spec in master_space.attributes:
        key = spec.key
        current = float(params[key])
        baseline = float(evaluation_space.baseline_params[key])
        deterioration, side = evaluation_coordinates[key]
        master_baseline_d, master_baseline_side = master_space.deterioration(
            evaluation_space.baseline_params,
            spec,
        )
        master_candidate_d, master_candidate_side = master_space.deterioration(params, spec)
        if (
            abs(baseline - float(master_space.baseline_params[key])) > 1e-9
            and master_baseline_d <= 1e-9
        ):
            migration_flags.append(f"{key}:better_than_master")
        baseline_loss = float(master_baseline_losses[key]["loss_points"])
        candidate_loss = float(candidate_losses.get(key, {}).get("loss_points", 0.0))
        incremental_loss = max(0.0, candidate_loss - baseline_loss)
        dimension_id = f"{key}:{side}"
        boundary = boundaries.get(dimension_id)
        accepted_budget = None
        rejected_budget = None
        limit_budget = None
        limit_value = None
        if boundary is not None and boundary.get("status") not in {
            "pending",
            "probing",
            "unresolved",
        }:
            master_dimension = master_space.dimension_by_id.get(dimension_id)
            if master_dimension is not None:
                def boundary_loss(deterioration_value: float) -> float:
                    probe = copy.deepcopy(master_space.baseline_params)
                    probe[key] = master_space.value_at(dimension_id, deterioration_value)
                    return float(learner.loss_map(probe)[key]["loss_points"])

                accepted_budget = max(
                    0.0,
                    boundary_loss(float(boundary.get("accepted_d") or 0.0))
                    - float(master_baseline_losses[key]["loss_points"]),
                )
                rejected_d = boundary.get("rejected_d")
                rejected_budget = (
                    None
                    if rejected_d is None
                    else max(
                        accepted_budget,
                        boundary_loss(float(rejected_d))
                        - float(master_baseline_losses[key]["loss_points"]),
                    )
                )
                limit_d = boundary.get("limit_d")
                if limit_d is None:
                    limit_d = boundary.get("accepted_d") or 0.0
                limit_budget = max(
                    accepted_budget,
                    boundary_loss(float(limit_d))
                    - float(master_baseline_losses[key]["loss_points"]),
                )
                limit_value = evaluation_space.value_at(
                    dimension_id,
                    float(limit_d),
                )

        if deterioration <= 1e-9:
            status = "better_or_equal"
            status_label = "达到或优于当前基准，可复用"
        elif boundary is None or accepted_budget is None:
            status = "not_learned"
            status_label = "单项复用损失预算尚未学完"
            uncertain_attributes.append(spec.label)
        elif incremental_loss <= accepted_budget + 1e-6:
            status = "single_reusable"
            status_label = "在迁移后的单项可复用范围内"
        elif rejected_budget is not None and incremental_loss >= rejected_budget - 1e-6:
            status = "beyond_single_limit"
            status_label = "超过迁移后的单项复用极限"
            clear_violations.append(spec.label)
        else:
            status = "near_single_boundary"
            status_label = "位于迁移后的单项极限夹逼区间"
            uncertain_attributes.append(spec.label)

        master_side = master_candidate_side if master_candidate_d > 1e-9 else master_baseline_side
        support_limit = float(support.get(dimension_id, 0.0))
        anchor_support = float(support.get(f"{key}:{master_baseline_side}", 0.0))
        if master_baseline_d > anchor_support + 1e-8:
            migration_flags.append(f"{key}:anchor_outside_support")
        if deterioration > support_limit + 1e-8:
            migration_flags.append(f"{key}:outside_support")
        contributions.append(
            {
                "attribute_key": key,
                "attribute_label": spec.label,
                "unit": spec.unit,
                "attribute_weight": candidate_losses[key]["attribute_weight"],
                "master_baseline_loss_points": master_baseline_losses[key]["loss_points"],
                "evaluation_baseline_loss_points": round(
                    float(evaluation_baseline_losses.get(key, {}).get("loss_points", 0.0)),
                    3,
                ),
                "candidate_master_loss_points": round(
                    float(raw_candidate_losses.get(key, {}).get("loss_points", 0.0)),
                    3,
                ),
                "shape_transferred_loss_points": round(candidate_loss, 3),
                "loss_points": round(incremental_loss, 3),
                "deterioration": round(deterioration, 6),
                "deterioration_side": side,
            }
        )
        attribute_results.append(
            {
                "attribute_key": key,
                "attribute_label": spec.label,
                "unit": spec.unit,
                "baseline_value": baseline,
                "current_value": current,
                "difference": round(current - baseline, spec.precision),
                "deterioration": round(deterioration, 6),
                "deterioration_percent_of_range": round(100.0 * deterioration, 2),
                "side": side,
                "status": status,
                "status_label": status_label,
                "accepted_loss_budget": None if accepted_budget is None else round(accepted_budget, 3),
                "rejected_loss_budget": None if rejected_budget is None else round(rejected_budget, 3),
                "incremental_loss_points": round(incremental_loss, 3),
                "limit_value": limit_value,
            }
        )

    contributions.sort(key=lambda item: item["loss_points"], reverse=True)
    total_loss = sum(float(item["loss_points"]) for item in contributions)
    score = round(clamp(100.0 - total_loss, 0.0, 100.0), 2)
    threshold = threshold_model.get("threshold")
    upward = any(flag.endswith(":better_than_master") for flag in migration_flags)
    outside_support = any(
        flag.endswith(":outside_support") or flag.endswith(":anchor_outside_support")
        for flag in migration_flags
    )
    if evaluation_profile_id == master_profile_id:
        migration_status = "master_direct"
        migration_label = "主基准直接评价"
    elif upward:
        migration_status = "unsupported_upward"
        migration_label = "新基准优于主基准，超出可迁移方向"
    elif outside_support:
        migration_status = "shape_preserving_extrapolation"
        migration_label = "曲线形状保持迁移，部分区域为低置信外推"
    else:
        migration_status = "supported_shape_transfer"
        migration_label = "主曲线证据范围内的逐属性形状迁移"

    if upward:
        overall = "uncertain"
        conclusion = "当前基准包含优于主训练基准的属性，不能直接迁移，请校准或重新训练。"
    elif clear_violations:
        overall = "not_reusable"
        conclusion = "存在属性超过迁移后的单项复用极限，整个成品不建议直接复用。"
    elif threshold is not None and score < float(threshold) - 1e-9:
        overall = "not_reusable"
        conclusion = "单项可能尚可，但多项增量损失已超过整机复用预算。"
    elif uncertain_attributes or threshold is None:
        overall = "uncertain"
        conclusion = "当前单项边界或综合复用分界尚未学完整，仍需专家判断。"
    else:
        overall = "reusable"
        conclusion = "各属性未越过迁移后的单项极限，综合效能也达到复用分界。"

    master_candidate_score = learner.score(params, prefer_uta=uta_ready)
    master_evaluation_baseline_score = learner.score(
        evaluation_space.baseline_params,
        prefer_uta=uta_ready,
    )
    global_shift_score = round(
        clamp(
            master_candidate_score + (100.0 - master_evaluation_baseline_score),
            0.0,
            100.0,
        ),
        2,
    )
    vertical_reanchoring_loss = sum(
        max(
            0.0,
            float(raw_candidate_losses[key]["loss_points"])
            - float(evaluation_baseline_losses[key]["loss_points"]),
        )
        for key in raw_candidate_losses
    )
    vertical_reanchoring_score = round(
        clamp(100.0 - vertical_reanchoring_loss, 0.0, 100.0),
        2,
    )
    curves = []
    contribution_by_key = {item["attribute_key"]: item for item in contributions}
    for curve in learner.loss_curves():
        key = curve["attribute_key"]
        spec = master_space.attribute_by_key[key]
        migrated_points = []
        support_limit = float(support.get(curve["dimension_id"], 0.0))
        for point in curve["points"]:
            deterioration_value = float(point["deterioration"])
            migrated_points.append(
                {
                    **point,
                    "original_loss_points": point["loss_points"],
                    "migrated_loss_points": point["loss_points"],
                    "migrated_raw_value": evaluation_space.value_at(
                        curve["dimension_id"],
                        deterioration_value,
                    ),
                    "evidence_support": (
                        "supported"
                        if deterioration_value <= support_limit + 1e-9
                        else "extrapolated"
                    ),
                }
            )
        evaluation_baseline_master_d, _ = master_space.deterioration(
            evaluation_space.baseline_params,
            spec,
        )
        current_master_d, _ = master_space.deterioration(params, spec)
        migrated_limit_value = next(
            item["limit_value"]
            for item in attribute_results
            if item["attribute_key"] == key
        )
        limit_master_d = None
        if migrated_limit_value is not None:
            limit_params = copy.deepcopy(evaluation_space.baseline_params)
            limit_params[key] = migrated_limit_value
            limit_master_d, _ = master_space.deterioration(limit_params, spec)
        curves.append(
            {
                **{key2: value for key2, value in curve.items() if key2 != "points"},
                "evaluation_baseline_value": evaluation_space.baseline_params[key],
                "current_value": params[key],
                "current_evaluation_deterioration": round(
                    evaluation_coordinates[key][0],
                    6,
                ),
                "evaluation_baseline_master_deterioration": round(
                    evaluation_baseline_master_d,
                    6,
                ),
                "current_master_deterioration": round(current_master_d, 6),
                "migrated_limit_value": migrated_limit_value,
                "migrated_limit_master_deterioration": (
                    None if limit_master_d is None else round(limit_master_d, 6)
                ),
                "support_max_deterioration": round(support_limit, 6),
                "points": migrated_points,
            }
        )

    return {
        "model_scope": "lock_product_v17_reanchored_degradation_effectiveness",
        "physical_feasibility_evaluated": False,
        "master_profile_id": master_profile_id,
        "baseline_profile_id": evaluation_profile_id,
        "baseline_profile_name": evaluation_space.profile.name,
        "baseline_score": 100.0,
        "effectiveness_score": score,
        "score_source": "boundary-adaptive reanchored LP-UTA" if uta_ready else "reanchored design-stage prior",
        "score_cap": 100.0,
        "effectiveness_interval": None,
        "combined_reuse_threshold": threshold,
        "combined_threshold_status": threshold_model.get("status"),
        "combined_threshold_accuracy": threshold_model.get("accuracy"),
        "overall_reuse_status": overall,
        "overall_reuse_label": REUSE_STATUS_LABELS.get(overall, "仍需确认"),
        "conclusion": conclusion,
        "single_attribute_violations": clear_violations,
        "uncertain_attributes": uncertain_attributes,
        "attributes": attribute_results,
        "loss_contributions": contributions,
        "total_loss_points": round(total_loss, 3),
        "migration": {
            "status": migration_status,
            "label": migration_label,
            "method": "per_attribute_normalized_curve_shape_transfer",
            "master_candidate_score": master_candidate_score,
            "master_evaluation_baseline_score": master_evaluation_baseline_score,
            "global_shift_comparison_score": global_shift_score,
            "vertical_reanchoring_comparison_score": vertical_reanchoring_score,
            "reanchored_score": score,
            "flags": sorted(set(migration_flags)),
        },
        "visualization": {
            "marginal_curves": curves,
            "reuse_threshold": threshold,
        },
        "params": copy.deepcopy(params),
    }


def evaluate_reuse_model(
    space: BaselineDegradationSpace,
    learner: LockPreferenceLearner,
    params: Dict[str, Any],
    boundaries: Dict[str, Dict[str, Any]],
    threshold_model: Dict[str, Any],
    profile_id: str,
    uta_ready: bool,
) -> Dict[str, Any]:
    """Evaluate effectiveness and reuse with the shared online/frozen contract."""
    score = learner.score(params, prefer_uta=uta_ready)
    attribute_results: List[Dict[str, Any]] = []
    clear_violations: List[str] = []
    uncertain_attributes: List[str] = []

    for spec in space.attributes:
        current = float(params[spec.key])
        baseline = float(space.baseline_params[spec.key])
        deterioration, side = space.deterioration(params, spec)
        if deterioration <= 1e-9:
            status = "better_or_equal"
            status_label = "达到或优于基准，可复用"
            limit_value = None
            accepted_d = 0.0
            rejected_d = None
        else:
            dimension_id = f"{spec.key}:{side}"
            boundary = boundaries.get(dimension_id)
            if boundary is None or boundary.get("status") in {
                "pending",
                "probing",
                "unresolved",
            }:
                status = "not_learned"
                status_label = "单项极限尚未学完"
                limit_value = None
                accepted_d = None
                rejected_d = None
                uncertain_attributes.append(spec.label)
            else:
                accepted_d = float(boundary.get("accepted_d", 0.0))
                rejected_d = boundary.get("rejected_d")
                limit_d = boundary.get("limit_d")
                if limit_d is None:
                    limit_d = (
                        accepted_d
                        if rejected_d is None
                        else 0.5 * (accepted_d + float(rejected_d))
                    )
                limit_value = space.value_at(dimension_id, float(limit_d))
                if deterioration <= accepted_d + 1e-9:
                    status = "single_reusable"
                    status_label = "在已确认可复用范围内"
                elif rejected_d is not None and deterioration >= float(rejected_d) - 1e-9:
                    status = "beyond_single_limit"
                    status_label = "超过单项复用极限"
                    clear_violations.append(spec.label)
                elif boundary.get("status") == "full_range_reusable" and deterioration <= 1.0 + 1e-9:
                    status = "single_reusable"
                    status_label = "整个生成范围均可复用"
                else:
                    status = "near_single_boundary"
                    status_label = "位于单项极限夹逼区间"
                    uncertain_attributes.append(spec.label)

        attribute_results.append(
            {
                "attribute_key": spec.key,
                "attribute_label": spec.label,
                "unit": spec.unit,
                "baseline_value": baseline,
                "current_value": current,
                "difference": round(current - baseline, spec.precision),
                "deterioration": round(deterioration, 6),
                "deterioration_percent_of_range": round(100.0 * deterioration, 2),
                "side": side,
                "status": status,
                "status_label": status_label,
                "accepted_d": accepted_d,
                "rejected_d": rejected_d,
                "limit_value": limit_value,
            }
        )

    threshold = threshold_model.get("threshold")
    if clear_violations:
        overall = "not_reusable"
        conclusion = "存在属性超过单项复用极限，整个成品不建议直接复用。"
    elif threshold is not None and score < float(threshold) - 1e-9:
        overall = "not_reusable"
        conclusion = "单项可能尚可，但多项退化叠加后已低于综合复用极限。"
    elif uncertain_attributes or threshold is None:
        overall = "uncertain"
        conclusion = "当前单项边界或综合复用分界尚未学完整，仍需专家判断。"
    else:
        overall = "reusable"
        conclusion = "各属性未越过单项极限，综合效能也达到当前复用分界。"

    contributions = learner.loss_contributions(params)
    return {
        "model_scope": "lock_product_baseline_degradation_effectiveness",
        "physical_feasibility_evaluated": False,
        "baseline_profile_id": profile_id,
        "baseline_profile_name": space.profile.name,
        "baseline_score": 100.0,
        "effectiveness_score": score,
        "score_source": "baseline-capped LP-UTA" if uta_ready else "design-stage prior",
        "score_cap": 100.0,
        "combined_reuse_threshold": threshold,
        "combined_threshold_status": threshold_model.get("status"),
        "combined_threshold_accuracy": threshold_model.get("accuracy"),
        "overall_reuse_status": overall,
        "overall_reuse_label": REUSE_STATUS_LABELS.get(overall, "仍需确认"),
        "conclusion": conclusion,
        "single_attribute_violations": clear_violations,
        "uncertain_attributes": uncertain_attributes,
        "attributes": attribute_results,
        "loss_contributions": contributions,
        "total_loss_points": round(
            sum(float(item["loss_points"]) for item in contributions),
            3,
        ),
        "params": copy.deepcopy(params),
    }


def fit_combined_reuse_threshold(
    evidence: Sequence[Dict[str, Any]],
    learner: LockPreferenceLearner,
) -> Dict[str, Any]:
    rows = []
    for item in evidence:
        if item.get("status", "active") != "active":
            continue
        label = item.get("label")
        if label not in {"reusable", "not_reusable"}:
            continue
        rows.append(
            {
                "id": item.get("id"),
                "label": label,
                "score": learner.score(item["params"]),
                "confidence": float(item.get("confidence", 1.0)),
            }
        )
    reusable = [item for item in rows if item["label"] == "reusable"]
    rejected = [item for item in rows if item["label"] == "not_reusable"]
    if not reusable or not rejected:
        return {
            "status": "insufficient_classes",
            "threshold": None,
            "evidence_count": len(rows),
            "reusable_count": len(reusable),
            "not_reusable_count": len(rejected),
            "accuracy": None,
            "separable": False,
            "rows": rows,
        }
    scores = sorted({item["score"] for item in rows})
    candidates = [0.0, 100.0]
    candidates.extend(scores)
    candidates.extend(0.5 * (left + right) for left, right in zip(scores, scores[1:]))
    best: Optional[Tuple[float, float, float]] = None
    for threshold in candidates:
        total_weight = sum(item["confidence"] for item in rows)
        correct_weight = sum(
            item["confidence"]
            for item in rows
            if (item["score"] >= threshold) == (item["label"] == "reusable")
        )
        accuracy = correct_weight / max(total_weight, 1e-12)
        distance_to_center = abs(threshold - 80.0)
        candidate = (accuracy, -distance_to_center, threshold)
        if best is None or candidate > best:
            best = candidate
    assert best is not None
    threshold = float(best[2])
    lowest_reusable = min(item["score"] for item in reusable)
    highest_rejected = max(item["score"] for item in rejected)
    separable = highest_rejected < lowest_reusable
    if separable:
        threshold = 0.5 * (highest_rejected + lowest_reusable)
    status = "learned" if len(rows) >= 8 and best[0] >= 0.80 else "preliminary"
    return {
        "status": status,
        "threshold": round(threshold, 2),
        "evidence_count": len(rows),
        "reusable_count": len(reusable),
        "not_reusable_count": len(rejected),
        "accuracy": round(float(best[0]), 3),
        "separable": separable,
        "highest_not_reusable_score": round(highest_rejected, 2),
        "lowest_reusable_score": round(lowest_reusable, 2),
        "rows": rows,
    }
