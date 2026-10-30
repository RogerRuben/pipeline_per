# -*- coding: utf-8 -*-
"""Online pairwise preference learning and linear-programming UTA models."""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, linprog, minimize

try:
    from scipy.optimize import milp
except ImportError:  # SciPy < 1.9: keep the app usable, but M3 is unavailable.
    milp = None

from project_excel import AttributeSpec, ProjectDataset, RequirementProfile, RequirementSpec


MIN_UTA_SEGMENTS = 2
MAX_UTA_SEGMENTS = 6
DEFAULT_UTA_SEGMENTS = 3


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def sigmoid(value: float) -> float:
    value = clamp(value, -35.0, 35.0)
    return 1.0 / (1.0 + math.exp(-value))


def validate_segment_count(value: Any) -> int:
    try:
        segments = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("UTA 分段数必须是整数。") from exc
    if not MIN_UTA_SEGMENTS <= segments <= MAX_UTA_SEGMENTS:
        raise ValueError(f"UTA 分段数必须在 {MIN_UTA_SEGMENTS} 到 {MAX_UTA_SEGMENTS} 之间。")
    return segments


def stage_attribute_prior(attributes: Sequence[AttributeSpec]) -> np.ndarray:
    """Translate design order into a soft 50/30/20 importance prior."""
    if not attributes:
        return np.zeros(0, dtype=float)
    configured = {1: 0.50, 2: 0.30, 3: 0.20}
    grouped: Dict[int, List[int]] = {}
    for index, spec in enumerate(attributes):
        grouped.setdefault(spec.design_stage, []).append(index)
    active_total = sum(configured.get(stage, 0.0) for stage in grouped)
    if active_total <= 0.0:
        return np.full(len(attributes), 1.0 / len(attributes), dtype=float)
    prior = np.zeros(len(attributes), dtype=float)
    for stage, indices in grouped.items():
        share = configured.get(stage, 0.0) / active_total
        for index in indices:
            prior[index] = share / len(indices)
    prior /= max(float(prior.sum()), 1e-12)
    return prior


@dataclass(frozen=True)
class UtilityFeatureSpace:
    """Shared benefit-oriented features for BT and UTA."""

    attributes: Tuple[AttributeSpec, ...]
    segments: int = DEFAULT_UTA_SEGMENTS
    requirements: Tuple[RequirementSpec, ...] = ()
    requirement_profile_id: Optional[str] = None

    @classmethod
    def from_project(
        cls,
        project: ProjectDataset,
        segments: int = DEFAULT_UTA_SEGMENTS,
        requirement_profile: Optional[RequirementProfile] = None,
    ) -> "UtilityFeatureSpace":
        # Kept in the signature only so old callers do not crash. Protocol values
        # are a final 100-point reference and are never preference-model features.
        _ = requirement_profile
        usable = tuple(
            item
            for item in project.attributes
            if item.is_numeric
            and item.participates_utility
            and item.preference_direction in {"higher_better", "lower_better"}
        )
        return cls(
            usable,
            validate_segment_count(segments),
            (),
            None,
        )

    @property
    def attribute_count(self) -> int:
        return len(self.attributes)

    @property
    def variable_count(self) -> int:
        return self.attribute_count * self.segments

    def benefit(self, params: Dict[str, Any], spec: AttributeSpec) -> float:
        requirement = self.requirement_by_key().get(spec.key)
        if requirement is not None:
            return self.requirement_benefit(float(params[spec.key]), spec, requirement)
        assert spec.generation_min is not None and spec.generation_max is not None
        span = max(float(spec.generation_max) - float(spec.generation_min), 1e-12)
        position = clamp((float(params[spec.key]) - float(spec.generation_min)) / span, 0.0, 1.0)
        return position if spec.preference_direction == "higher_better" else 1.0 - position

    def requirement_by_key(self) -> Dict[str, RequirementSpec]:
        return {item.attribute_key: item for item in self.requirements}

    @staticmethod
    def _linear(value: float, zero: float, one: float) -> float:
        width = one - zero
        if abs(width) <= 1e-12:
            return 1.0 if value == one else 0.0
        return clamp((value - zero) / width, 0.0, 1.0)

    def requirement_benefit(
        self,
        value: float,
        spec: AttributeSpec,
        requirement: RequirementSpec,
    ) -> float:
        assert spec.generation_min is not None and spec.generation_max is not None
        lo = float(spec.generation_min)
        hi = float(spec.generation_max)
        kind = requirement.requirement_type
        if kind == "at_least":
            assert requirement.minimum is not None
            threshold = requirement.minimum
            if value < threshold:
                base = self._linear(value, lo, threshold)
                return base * (0.8 if requirement.overachievement_bonus else 1.0)
            if not requirement.overachievement_bonus:
                return 1.0
            return 0.8 + 0.2 * self._linear(value, threshold, hi)
        if kind == "at_most":
            assert requirement.maximum is not None
            threshold = requirement.maximum
            if value > threshold:
                base = self._linear(value, hi, threshold)
                return base * (0.8 if requirement.overachievement_bonus else 1.0)
            if not requirement.overachievement_bonus:
                return 1.0
            return 0.8 + 0.2 * self._linear(value, threshold, lo)
        if kind in {"within_range", "target"}:
            if kind == "within_range":
                assert requirement.minimum is not None and requirement.maximum is not None
                accepted_lo = requirement.minimum - requirement.tolerance
                accepted_hi = requirement.maximum + requirement.tolerance
            else:
                assert requirement.target_value is not None
                accepted_lo = requirement.target_value - requirement.tolerance
                accepted_hi = requirement.target_value + requirement.tolerance
            if value < accepted_lo:
                return self._linear(value, lo, accepted_lo)
            if value > accepted_hi:
                return self._linear(value, hi, accepted_hi)
            return 1.0
        position = self._linear(value, lo, hi)
        return position if kind == "higher_better" else 1.0 - position

    def linear_vector(self, params: Dict[str, Any]) -> np.ndarray:
        return np.asarray([self.benefit(params, item) for item in self.attributes], dtype=float)

    def segment_vector(self, params: Dict[str, Any]) -> np.ndarray:
        values: List[float] = []
        for spec in self.attributes:
            scaled = self.benefit(params, spec) * self.segments
            values.extend(clamp(scaled - segment, 0.0, 1.0) for segment in range(self.segments))
        return np.asarray(values, dtype=float)

    def attribute_slices(self) -> Iterable[Tuple[AttributeSpec, slice]]:
        for index, spec in enumerate(self.attributes):
            start = index * self.segments
            yield spec, slice(start, start + self.segments)


class OnlineBTModel:
    """Constrained feature Bradley-Terry model retrained after each valid preference."""

    SCALE = 5.0

    def __init__(
        self,
        project: ProjectDataset,
        weights: Optional[Sequence[float]] = None,
        requirement_profile: Optional[RequirementProfile] = None,
    ):
        self.space = UtilityFeatureSpace.from_project(project, requirement_profile=requirement_profile)
        count = self.space.attribute_count
        self.prior_weights = stage_attribute_prior(self.space.attributes)
        if weights is not None and len(weights) == count and sum(float(item) for item in weights) > 0:
            vector = np.maximum(np.asarray(weights, dtype=float), 0.0)
            self.weights = vector / vector.sum()
        elif count:
            self.weights = self.prior_weights.copy()
        else:
            self.weights = np.zeros(0, dtype=float)

    @staticmethod
    def active_evidence(evidence: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            item
            for item in evidence
            if item.get("status", "active") not in {
                "withdrawn",
                "rejected",
                "superseded",
                "invalidated",
            }
            and item.get("relation") in {"A", "B", "tie"}
        ]

    def fit(self, evidence: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        observations = self.active_evidence(evidence)
        if not observations or not self.space.attribute_count:
            return self.summary(observations)
        differences = []
        targets = []
        sample_weights = []
        for item in observations:
            delta = self.space.linear_vector(item["params_a"]) - self.space.linear_vector(item["params_b"])
            differences.append(delta)
            targets.append(1.0 if item["relation"] == "A" else 0.0 if item["relation"] == "B" else 0.5)
            sample_weights.append(float(item.get("confidence", 1.0)))
        x = np.asarray(differences, dtype=float)
        y = np.asarray(targets, dtype=float)
        weights = np.asarray(sample_weights, dtype=float)
        prior = self.prior_weights

        def objective(vector: np.ndarray) -> float:
            logits = np.clip(self.SCALE * (x @ vector), -35.0, 35.0)
            probabilities = 1.0 / (1.0 + np.exp(-logits))
            loss = -np.sum(weights * (y * np.log(probabilities + 1e-12) + (1.0 - y) * np.log(1.0 - probabilities + 1e-12)))
            regularization = 0.035 * len(observations) * float(np.sum((vector - prior) ** 2))
            return float(loss + regularization)

        result = minimize(
            objective,
            self.weights,
            method="SLSQP",
            bounds=[(0.0, 1.0)] * self.space.attribute_count,
            constraints=[{"type": "eq", "fun": lambda vector: float(np.sum(vector) - 1.0)}],
            options={"maxiter": 600, "ftol": 1e-10},
        )
        if result.success and np.isfinite(result.x).all() and result.x.sum() > 0:
            self.weights = np.maximum(result.x, 0.0)
            self.weights /= self.weights.sum()
        return self.summary(observations)

    def utility(self, params: Dict[str, Any]) -> float:
        if not self.space.attribute_count:
            return 0.5
        return float(self.space.linear_vector(params) @ self.weights)

    def score(self, params: Dict[str, Any]) -> float:
        return 100.0 * self.utility(params)

    def probability(self, params_a: Dict[str, Any], params_b: Dict[str, Any]) -> float:
        return sigmoid(self.SCALE * (self.utility(params_a) - self.utility(params_b)))

    def uncertainty(self, params_a: Dict[str, Any], params_b: Dict[str, Any]) -> float:
        probability = self.probability(params_a, params_b)
        return 1.0 - 2.0 * abs(probability - 0.5)

    def summary(self, observations: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        strict_correct = 0
        strict_count = 0
        weighted_loss = 0.0
        weight_total = 0.0
        for item in observations:
            probability = self.probability(item["params_a"], item["params_b"])
            target = 1.0 if item["relation"] == "A" else 0.0 if item["relation"] == "B" else 0.5
            sample_weight = float(item.get("confidence", 1.0))
            weighted_loss += sample_weight * -(target * math.log(probability + 1e-12) + (1.0 - target) * math.log(1.0 - probability + 1e-12))
            weight_total += sample_weight
            if item["relation"] in {"A", "B"}:
                strict_count += 1
                strict_correct += int((probability >= 0.5) == (item["relation"] == "A"))
        attribute_weights = {
            spec.key: round(float(self.weights[index]), 6) for index, spec in enumerate(self.space.attributes)
        }
        return {
            "model_type": "constrained_feature_bradley_terry",
            "update_mode": "每条有效偏好后立即用全部有效证据重训",
            "training_pairs": len(observations),
            "strict_pairs": strict_count,
            "training_accuracy": round(strict_correct / strict_count, 3) if strict_count else None,
            "log_loss": round(weighted_loss / weight_total, 4) if weight_total else None,
            "attribute_weights": attribute_weights,
            "stage_prior_weights": {
                spec.key: round(float(self.prior_weights[index]), 6)
                for index, spec in enumerate(self.space.attributes)
            },
            "requirement_profile_id": self.space.requirement_profile_id,
            "weights": [round(float(item), 10) for item in self.weights],
            "status": "no_data" if not observations else "online_learning",
        }


class LPUTAModel:
    """Additive piecewise-linear UTA solved by linear programming."""

    STRICT_MARGIN = 0.008
    TIE_MARGIN = 0.012
    HIGH_TOLERANCE = 0.02
    M3_BIG_M = 2.1

    def __init__(
        self,
        project: ProjectDataset,
        segments: int = DEFAULT_UTA_SEGMENTS,
        increments: Optional[Sequence[float]] = None,
        requirement_profile: Optional[RequirementProfile] = None,
    ):
        self.project = project
        self.segments = validate_segment_count(segments)
        self.space = UtilityFeatureSpace.from_project(
            project,
            self.segments,
            requirement_profile=requirement_profile,
        )
        count = self.space.variable_count
        attribute_prior = stage_attribute_prior(self.space.attributes)
        self.prior_increments = np.repeat(attribute_prior / self.segments, self.segments)
        if increments is not None and len(increments) == count and sum(float(item) for item in increments) > 0:
            vector = np.maximum(np.asarray(increments, dtype=float), 0.0)
            self.increments = vector / vector.sum()
        elif count:
            self.increments = self.prior_increments.copy()
        else:
            self.increments = np.zeros(0, dtype=float)

    @staticmethod
    def active_evidence(evidence: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return OnlineBTModel.active_evidence(evidence)

    @staticmethod
    def stable_test_member(item: Dict[str, Any]) -> bool:
        identity = str(item.get("id") or item.get("interaction_id") or repr(item))
        digest = hashlib.sha256(identity.encode("utf-8")).digest()
        return int.from_bytes(digest[:2], "big") % 5 == 0

    def split_evidence(self, evidence: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        items = list(evidence)
        if len(items) < 8:
            return items, []
        test = [item for item in items if self.stable_test_member(item)]
        if not test:
            test = [items[-1]]
        if len(test) >= len(items):
            test = test[: max(1, len(items) // 5)]
        test_ids = {id(item) for item in test}
        train = [item for item in items if id(item) not in test_ids]
        return train, test

    def shape_constraints(self, total_variables: int) -> Tuple[List[np.ndarray], List[float]]:
        rows: List[np.ndarray] = []
        bounds: List[float] = []
        for spec, attr_slice in self.space.attribute_slices():
            trend = spec.marginal_trend
            diminishing = "递减" in trend or "饱和" in trend or ("负效用加速" in trend and spec.preference_direction == "lower_better")
            increasing = "递增" in trend or ("加速" in trend and not diminishing)
            linear = "线性" in trend
            indices = list(range(attr_slice.start, attr_slice.stop))
            for left, right in zip(indices, indices[1:]):
                if diminishing or linear:
                    row = np.zeros(total_variables)
                    row[right] = 1.0
                    row[left] = -1.0
                    rows.append(row)
                    bounds.append(0.0)
                if increasing or linear:
                    row = np.zeros(total_variables)
                    row[left] = 1.0
                    row[right] = -1.0
                    rows.append(row)
                    bounds.append(0.0)
        return rows, bounds

    def evidence_delta(self, item: Dict[str, Any]) -> np.ndarray:
        delta = self.space.segment_vector(item["params_a"]) - self.space.segment_vector(item["params_b"])
        return delta if item["relation"] != "B" else -delta

    def solve_m1(self, evidence: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        count = self.space.variable_count
        if not count:
            return {"consistent": False, "solver_status": "no_utility_attributes"}
        rows, bounds = self.shape_constraints(count)
        for item in evidence:
            delta = self.space.segment_vector(item["params_a"]) - self.space.segment_vector(item["params_b"])
            if item["relation"] == "A":
                rows.append(-delta)
                bounds.append(-self.STRICT_MARGIN)
            elif item["relation"] == "B":
                rows.append(delta)
                bounds.append(-self.STRICT_MARGIN)
            else:
                rows.extend([delta, -delta])
                bounds.extend([self.TIE_MARGIN, self.TIE_MARGIN])
        result = linprog(
            np.zeros(count),
            A_ub=np.asarray(rows) if rows else None,
            b_ub=np.asarray(bounds) if bounds else None,
            A_eq=np.ones((1, count)),
            b_eq=np.asarray([1.0]),
            bounds=[(0.0, 1.0)] * count,
            method="highs",
        )
        return {
            "consistent": bool(result.success),
            "solver_status": str(result.message),
        }

    def solve_m2(self, evidence: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        variable_count = self.space.variable_count
        evidence_count = len(evidence)
        if not variable_count:
            return {"success": False, "solver_status": "no_utility_attributes", "increments": [], "slacks": []}
        total = variable_count + evidence_count + variable_count
        slack_start = variable_count
        deviation_start = variable_count + evidence_count
        objective = np.zeros(total)
        for index, item in enumerate(evidence):
            objective[slack_start + index] = max(float(item.get("confidence", 1.0)), 0.1)
        objective[deviation_start:] = 0.003
        rows, limits = self.shape_constraints(total)
        for index, item in enumerate(evidence):
            delta = self.space.segment_vector(item["params_a"]) - self.space.segment_vector(item["params_b"])
            slack_index = slack_start + index
            if item["relation"] == "A":
                row = np.zeros(total)
                row[:variable_count] = -delta
                row[slack_index] = -1.0
                rows.append(row)
                limits.append(-self.STRICT_MARGIN)
            elif item["relation"] == "B":
                row = np.zeros(total)
                row[:variable_count] = delta
                row[slack_index] = -1.0
                rows.append(row)
                limits.append(-self.STRICT_MARGIN)
            else:
                for sign in (1.0, -1.0):
                    row = np.zeros(total)
                    row[:variable_count] = sign * delta
                    row[slack_index] = -1.0
                    rows.append(row)
                    limits.append(self.TIE_MARGIN)
        for index in range(variable_count):
            upper = np.zeros(total)
            upper[index] = 1.0
            upper[deviation_start + index] = -1.0
            rows.append(upper)
            limits.append(float(self.prior_increments[index]))
            lower = np.zeros(total)
            lower[index] = -1.0
            lower[deviation_start + index] = -1.0
            rows.append(lower)
            limits.append(-float(self.prior_increments[index]))
        equality = np.zeros((1, total))
        equality[0, :variable_count] = 1.0
        result = linprog(
            objective,
            A_ub=np.asarray(rows) if rows else None,
            b_ub=np.asarray(limits) if limits else None,
            A_eq=equality,
            b_eq=np.asarray([1.0]),
            bounds=[(0.0, 1.0)] * total,
            method="highs",
        )
        if not result.success:
            return {
                "success": False,
                "solver_status": str(result.message),
                "increments": self.increments.tolist(),
                "slacks": [None] * evidence_count,
            }
        increments = np.maximum(result.x[:variable_count], 0.0)
        increments /= max(float(increments.sum()), 1e-12)
        slacks = np.maximum(result.x[slack_start:deviation_start], 0.0)
        return {
            "success": True,
            "solver_status": str(result.message),
            "increments": increments.tolist(),
            "slacks": slacks.tolist(),
            "objective": float(result.fun),
        }

    def solve_m3(self, evidence: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Locate a minimum-cardinality set of comparisons to relax."""
        variable_count = self.space.variable_count
        evidence_count = len(evidence)
        if not variable_count or not evidence_count:
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
                "solver_status": "当前 SciPy 不提供 scipy.optimize.milp；请升级到 SciPy 1.9 或更高版本。",
            }
        total = variable_count + evidence_count
        objective = np.zeros(total)
        objective[variable_count:] = 1.0
        integrality = np.zeros(total, dtype=int)
        integrality[variable_count:] = 1
        lower_bounds = np.zeros(total)
        upper_bounds = np.ones(total)
        rows, limits = self.shape_constraints(total)
        for index, item in enumerate(evidence):
            delta = self.space.segment_vector(item["params_a"]) - self.space.segment_vector(item["params_b"])
            conflict_index = variable_count + index
            if item["relation"] == "A":
                row = np.zeros(total)
                row[:variable_count] = -delta
                row[conflict_index] = -self.M3_BIG_M
                rows.append(row)
                limits.append(-self.STRICT_MARGIN)
            elif item["relation"] == "B":
                row = np.zeros(total)
                row[:variable_count] = delta
                row[conflict_index] = -self.M3_BIG_M
                rows.append(row)
                limits.append(-self.STRICT_MARGIN)
            else:
                for sign in (1.0, -1.0):
                    row = np.zeros(total)
                    row[:variable_count] = sign * delta
                    row[conflict_index] = -self.M3_BIG_M
                    rows.append(row)
                    limits.append(self.TIE_MARGIN)
        equality = np.zeros(total)
        equality[:variable_count] = 1.0
        matrix = np.vstack([np.asarray(rows), equality]) if rows else np.asarray([equality])
        constraint_lower = np.concatenate([np.full(len(rows), -np.inf), np.asarray([1.0])])
        constraint_upper = np.concatenate([np.asarray(limits), np.asarray([1.0])])
        result = milp(
            objective,
            integrality=integrality,
            bounds=Bounds(lower_bounds, upper_bounds),
            constraints=LinearConstraint(matrix, constraint_lower, constraint_upper),
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
            evidence[index].get("id")
            for index, value in enumerate(result.x[variable_count:])
            if value > 0.5
        ]
        return {
            "status": "localized" if selected else "not_required",
            "success": True,
            "minimum_conflicts": len(selected),
            "conflicting_evidence_ids": selected,
            "solver_status": str(result.message),
        }

    def utility(self, params: Dict[str, Any]) -> float:
        if not self.space.variable_count:
            return 0.5
        return float(self.space.segment_vector(params) @ self.increments)

    def score(self, params: Dict[str, Any]) -> float:
        return 100.0 * self.utility(params)

    def attribute_contributions(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        vector = self.space.segment_vector(params)
        output: List[Dict[str, Any]] = []
        for spec, attr_slice in self.space.attribute_slices():
            utility = float(vector[attr_slice] @ self.increments[attr_slice])
            output.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "unit": spec.unit,
                    "raw_value": params[spec.key],
                    "utility": round(utility, 6),
                    "score_points": round(100.0 * utility, 3),
                }
            )
        return output

    def accuracy(self, evidence: Sequence[Dict[str, Any]]) -> Optional[float]:
        if not evidence:
            return None
        correct = 0
        for item in evidence:
            difference = self.utility(item["params_a"]) - self.utility(item["params_b"])
            if item["relation"] == "A":
                correct += int(difference > 0.0)
            elif item["relation"] == "B":
                correct += int(difference < 0.0)
            else:
                correct += int(abs(difference) <= self.TIE_MARGIN)
        return correct / len(evidence)

    def accuracy_with_increments(
        self, evidence: Sequence[Dict[str, Any]], increments: Sequence[float]
    ) -> Optional[float]:
        if not evidence:
            return None
        previous = self.increments
        self.increments = np.asarray(increments, dtype=float)
        try:
            return self.accuracy(evidence)
        finally:
            self.increments = previous

    @staticmethod
    def stable_bucket(identity: str, buckets: int) -> int:
        digest = hashlib.sha256(identity.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "big") % buckets

    def cross_validate(self, evidence: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        items = list(evidence)
        if len(items) < 12:
            return {
                "status": "insufficient_data",
                "folds": 0,
                "evaluated_pairs": 0,
                "accuracy": None,
                "fold_results": [],
                "increments": [],
            }
        fold_count = min(5, max(3, len(items) // 8))
        assignments = [self.stable_bucket(str(item.get("id") or index), fold_count) for index, item in enumerate(items)]
        results = []
        models = []
        weighted_correct = 0.0
        evaluated = 0
        for fold in range(fold_count):
            test = [item for index, item in enumerate(items) if assignments[index] == fold]
            train = [item for index, item in enumerate(items) if assignments[index] != fold]
            if not test or len(train) < max(5, self.space.attribute_count):
                continue
            solved = self.solve_m2(train)
            if not solved.get("success"):
                continue
            accuracy = self.accuracy_with_increments(test, solved["increments"])
            assert accuracy is not None
            results.append(
                {
                    "fold": fold + 1,
                    "training_pairs": len(train),
                    "test_pairs": len(test),
                    "accuracy": round(accuracy, 3),
                }
            )
            models.append([round(float(value), 10) for value in solved["increments"]])
            weighted_correct += accuracy * len(test)
            evaluated += len(test)
        return {
            "status": "completed" if len(results) >= 2 else "insufficient_data",
            "folds": len(results),
            "evaluated_pairs": evaluated,
            "accuracy": round(weighted_correct / evaluated, 3) if evaluated else None,
            "fold_results": results,
            "increments": models,
        }

    def whole_scheme_holdout(self, evidence: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        items = [item for item in evidence if item.get("scheme_a") and item.get("scheme_b")]
        scheme_ids = sorted({str(item[side]) for item in items for side in ("scheme_a", "scheme_b")})
        if len(items) < 12 or len(scheme_ids) < 5:
            return {
                "status": "insufficient_data",
                "held_out_schemes": [],
                "training_pairs": 0,
                "test_pairs": 0,
                "accuracy": None,
                "increments": None,
            }
        held_out = {item for item in scheme_ids if self.stable_bucket(item, 5) == 0}
        if not held_out:
            held_out = {scheme_ids[-1]}
        if len(held_out) == len(scheme_ids):
            held_out = {scheme_ids[-1]}
        train = [item for item in items if item["scheme_a"] not in held_out and item["scheme_b"] not in held_out]
        test = [item for item in items if item["scheme_a"] in held_out or item["scheme_b"] in held_out]
        if len(train) < max(5, self.space.attribute_count) or not test:
            return {
                "status": "insufficient_data",
                "held_out_schemes": sorted(held_out),
                "training_pairs": len(train),
                "test_pairs": len(test),
                "accuracy": None,
                "increments": None,
            }
        solved = self.solve_m2(train)
        accuracy = self.accuracy_with_increments(test, solved["increments"]) if solved.get("success") else None
        return {
            "status": "completed" if accuracy is not None else "solver_failed",
            "held_out_schemes": sorted(held_out),
            "training_pairs": len(train),
            "test_pairs": len(test),
            "accuracy": round(accuracy, 3) if accuracy is not None else None,
            "increments": [round(float(value), 10) for value in solved["increments"]] if solved.get("success") else None,
        }

    def ranking_stability(self, model_increments: Sequence[Sequence[float]]) -> List[Dict[str, Any]]:
        if len(model_increments) < 2:
            return []
        output = []
        for scheme in self.project.schemes:
            scores = [
                100.0 * float(self.space.segment_vector(scheme.params) @ np.asarray(increments, dtype=float))
                for increments in model_increments
            ]
            output.append(
                {
                    "scheme_id": scheme.id,
                    "mean_score": round(float(np.mean(scores)), 2),
                    "p10": round(float(np.percentile(scores, 10)), 2),
                    "p90": round(float(np.percentile(scores, 90)), 2),
                    "width": round(float(np.percentile(scores, 90) - np.percentile(scores, 10)), 2),
                }
            )
        return sorted(output, key=lambda item: item["mean_score"], reverse=True)

    def bootstrap_models(
        self,
        evidence: Sequence[Dict[str, Any]],
        maximum: int = 12,
    ) -> List[Dict[str, Any]]:
        """Build a deterministic small-sample ensemble from resampled judgments."""
        items = list(evidence)
        if len(items) < 8 or not self.space.variable_count:
            return []
        identity = "|".join(str(item.get("id") or index) for index, item in enumerate(items))
        base_seed = int.from_bytes(hashlib.sha256(identity.encode("utf-8")).digest()[:8], "big")
        reference_accuracy = self.accuracy(items)
        minimum_accuracy = max(0.50, float(reference_accuracy or 0.50) - 0.15)
        output: List[Dict[str, Any]] = []
        for replicate in range(maximum):
            rng = random.Random(base_seed + 104729 * (replicate + 1))
            sample = [items[rng.randrange(len(items))] for _ in items]
            if len({str(item.get("id") or id(item)) for item in sample}) < max(4, self.space.attribute_count // 2):
                continue
            solved = self.solve_m2(sample)
            if not solved.get("success"):
                continue
            accuracy = self.accuracy_with_increments(items, solved["increments"])
            if accuracy is None or accuracy + 1e-12 < minimum_accuracy:
                continue
            output.append(
                {
                    "source": f"bootstrap_{replicate + 1:02d}",
                    "accuracy": round(float(accuracy), 3),
                    "increments": [round(float(value), 10) for value in solved["increments"]],
                }
            )
        return output

    @staticmethod
    def robust_model_ensemble(models: Sequence[Dict[str, Any]], maximum: int = 24) -> List[Dict[str, Any]]:
        output: List[Dict[str, Any]] = []
        for model in models:
            increments = model.get("increments") or []
            if not increments:
                continue
            output.append(
                {
                    **{name: value for name, value in model.items() if name != "increments"},
                    "increments": [round(float(value), 10) for value in increments],
                }
            )
            if len(output) >= maximum:
                break
        return output

    def marginal_curves(self) -> List[Dict[str, Any]]:
        curves: List[Dict[str, Any]] = []
        for spec, attr_slice in self.space.attribute_slices():
            increments = self.increments[attr_slice]
            assert spec.generation_min is not None and spec.generation_max is not None
            raw_values = np.linspace(float(spec.generation_min), float(spec.generation_max), self.segments + 1)
            points = []
            for raw_value in raw_values:
                benefit = self.space.benefit({spec.key: float(raw_value)}, spec)
                scaled = benefit * self.segments
                basis = np.asarray(
                    [clamp(scaled - segment, 0.0, 1.0) for segment in range(self.segments)],
                    dtype=float,
                )
                utility = float(basis @ increments)
                points.append(
                    {
                        "benefit": round(benefit, 6),
                        "raw_value": round(float(raw_value), spec.precision),
                        "utility": round(utility, 6),
                    }
                )
            curves.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "unit": spec.unit,
                    "preference_direction": spec.preference_direction,
                    "marginal_trend": spec.marginal_trend,
                    "attribute_weight": round(float(np.sum(increments)), 6),
                    "requirement_type": (
                        self.space.requirement_by_key().get(spec.key).requirement_type
                        if spec.key in self.space.requirement_by_key()
                        else None
                    ),
                    "points": points,
                    "increments": [round(float(item), 8) for item in increments],
                }
            )
        return curves

    def fit(self, evidence: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        observations = self.active_evidence(evidence)
        train, test = self.split_evidence(observations)
        m1 = self.solve_m1(observations)
        diagnostic = self.solve_m2(observations)
        m3 = self.solve_m3(observations) if observations and not m1["consistent"] else {
            "status": "not_required",
            "success": True,
            "minimum_conflicts": 0,
            "conflicting_evidence_ids": [],
        }
        trained = self.solve_m2(train)
        if trained.get("success"):
            self.increments = np.asarray(trained["increments"], dtype=float)
        training_accuracy = self.accuracy(train)
        test_accuracy = self.accuracy(test)
        judgment_consistency = (
            self.accuracy_with_increments(observations, diagnostic["increments"])
            if observations and diagnostic.get("success")
            else None
        )
        cross_validation = self.cross_validate(observations)
        scheme_holdout = self.whole_scheme_holdout(observations)
        validation_models = list(cross_validation.get("increments") or [])
        if scheme_holdout.get("increments"):
            validation_models.append(scheme_holdout["increments"])
        rank_stability = self.ranking_stability(validation_models)
        robust_candidates: List[Dict[str, Any]] = []
        if self.increments.size:
            robust_candidates.append(
                {
                    "source": "main_training",
                    "accuracy": round(float(self.accuracy(observations) or 0.0), 3),
                    "increments": self.increments.tolist(),
                }
            )
        if diagnostic.get("success"):
            robust_candidates.append(
                {
                    "source": "full_evidence_minimum_tolerance",
                    "accuracy": round(float(judgment_consistency or 0.0), 3),
                    "increments": diagnostic.get("increments") or [],
                }
            )
        for index, increments in enumerate(cross_validation.get("increments") or [], start=1):
            robust_candidates.append(
                {
                    "source": f"cross_validation_{index:02d}",
                    "accuracy": round(float(self.accuracy_with_increments(observations, increments) or 0.0), 3),
                    "increments": increments,
                }
            )
        if scheme_holdout.get("increments"):
            increments = scheme_holdout["increments"]
            robust_candidates.append(
                {
                    "source": "whole_scheme_holdout",
                    "accuracy": round(float(self.accuracy_with_increments(observations, increments) or 0.0), 3),
                    "increments": increments,
                }
            )
        robust_candidates.extend(self.bootstrap_models(observations))
        robust_models = self.robust_model_ensemble(robust_candidates)
        robust_unique_model_count = len(
            {
                tuple(round(float(value), 8) for value in model.get("increments") or [])
                for model in robust_models
            }
        )
        diagnostic_slacks = diagnostic.get("slacks") or []
        tolerance = []
        m3_ids = set(m3.get("conflicting_evidence_ids") or [])
        for index, item in enumerate(observations):
            slack = diagnostic_slacks[index] if index < len(diagnostic_slacks) else None
            tolerance.append(
                {
                    "evidence_id": item.get("id"),
                    "relation": item.get("relation"),
                    "slack": round(float(slack), 6) if slack is not None else None,
                    "high_tolerance": bool(slack is not None and float(slack) >= self.HIGH_TOLERANCE),
                    "m3_conflict": item.get("id") in m3_ids,
                }
            )
        tolerance.sort(key=lambda item: float("inf") if item["slack"] is None else -item["slack"])
        free_parameters = max(self.space.variable_count - 1, 0)
        recommended = max(12, math.ceil(1.5 * free_parameters))
        cv_accuracy = cross_validation.get("accuracy")
        scheme_accuracy = scheme_holdout.get("accuracy")
        robust_validation = (
            test_accuracy is not None
            and test_accuracy >= 0.75
            and cv_accuracy is not None
            and cv_accuracy >= 0.75
            and scheme_accuracy is not None
            and scheme_accuracy >= 0.70
        )
        if not observations:
            status = "no_data"
        elif not m1["consistent"]:
            status = "needs_review"
        elif len(train) < max(5, self.space.attribute_count):
            status = "insufficient_data"
        elif len(train) < recommended or not test:
            status = "preliminary"
        elif robust_validation:
            status = "validated"
        else:
            status = "needs_review"
        if not observations or len(observations) < 12:
            generalization_status = "insufficient_evidence"
        elif scheme_accuracy is None:
            generalization_status = "pair_validation_only"
        elif robust_validation:
            generalization_status = "whole_scheme_supported"
        else:
            generalization_status = "unstable_on_holdout"
        curves = self.marginal_curves()
        return {
            "model_type": "additive_piecewise_linear_lp_uta",
            "requirement_profile_id": self.space.requirement_profile_id,
            "segments": self.segments,
            "attribute_count": self.space.attribute_count,
            "utility_increment_variables": self.space.variable_count,
            "free_parameters": free_parameters,
            "recommended_training_pairs": recommended,
            "evidence_pairs": len(observations),
            "training_pairs": len(train),
            "test_pairs": len(test),
            "split_method": "固定哈希 80/20 留出；不足 8 条时暂不留出",
            "status": status,
            "m1_consistent": m1["consistent"],
            "m1_label": "待检验" if not observations else "通过" if m1["consistent"] else "未通过",
            "m1_solver_status": m1["solver_status"],
            "m2_total_slack": round(sum(float(item or 0.0) for item in diagnostic_slacks), 6),
            "m2_max_slack": round(max([float(item or 0.0) for item in diagnostic_slacks] or [0.0]), 6),
            "high_tolerance_threshold": self.HIGH_TOLERANCE,
            "high_tolerance_pairs": sum(1 for item in tolerance if item["high_tolerance"]),
            "m3": m3,
            "training_accuracy": round(training_accuracy, 3) if training_accuracy is not None else None,
            "test_accuracy": round(test_accuracy, 3) if test_accuracy is not None else None,
            "judgment_consistency": (
                round(judgment_consistency, 3) if judgment_consistency is not None else None
            ),
            "judgment_consistency_definition": "全体有效判断在最小容忍UTA模型下的拟合正确率",
            "cross_validation": {key: value for key, value in cross_validation.items() if key != "increments"},
            "whole_scheme_holdout": {key: value for key, value in scheme_holdout.items() if key != "increments"},
            "generalization_status": generalization_status,
            "validation_increments": validation_models,
            "robust_models": robust_models,
            "robust_model_count": len(robust_models),
            "robust_unique_model_count": robust_unique_model_count,
            "robust_method": "主UTA解、全证据最小容忍解、交叉验证、整方案留出和确定性Bootstrap重训集合",
            "rank_stability": rank_stability,
            "mean_rank_interval_width": (
                round(sum(item["width"] for item in rank_stability) / len(rank_stability), 2)
                if rank_stability
                else None
            ),
            "tolerances": tolerance,
            "attribute_weights": {item["key"]: item["attribute_weight"] for item in curves},
            "stage_prior_weights": {
                spec.key: round(float(stage_attribute_prior(self.space.attributes)[index]), 6)
                for index, spec in enumerate(self.space.attributes)
            },
            "marginal_curves": curves,
            "increments": [round(float(item), 10) for item in self.increments],
        }
