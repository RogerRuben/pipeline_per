# -*- coding: utf-8 -*-
"""Protocol-anchored robust UTA scoring with the protocol fixed at 100 points."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from project_excel import AttributeSpec, ProjectDataset, RequirementProfile, RequirementSpec
from preference_models import DEFAULT_UTA_SEGMENTS, LPUTAModel


DECISION_LABELS = {
    "above_baseline": "相对基准效能高于100分",
    "equal_to_baseline": "与基准技术协议同为100分",
    "below_baseline": "相对基准效能低于100分",
    "baseline_unavailable": "未设置基准技术协议",
}
ROBUST_LABELS = {
    "all_models_at_or_above_baseline": "全部稳健模型均支持达到或超过基准100分",
    "models_span_baseline": "稳健结果跨越基准100分，需要结合P10-P90解释",
    "all_models_below_baseline": "全部稳健模型均低于基准100分",
    "single_model": "当前只有一个中心模型，尚不能形成稳健区间",
    "baseline_unavailable": "未设置基准技术协议",
}
_DEFAULT_PROFILE = object()

REQUIREMENT_TYPE_LABELS = {
    "at_least": "越大越好",
    "at_most": "越小越好",
    "within_range": "区间型",
    "target": "目标值型",
    "higher_better": "越大越好",
    "lower_better": "越小越好",
}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


@dataclass
class AttributeRequirementAssessment:
    attribute_key: str
    attribute_label: str
    unit: str
    value: Optional[float]
    requirement_type: str
    requirement_label: str
    requirement_text: str
    satisfaction: float
    weight: float
    weighted_score: float
    met: bool
    hard_requirement: bool
    hard_gap: bool
    gap: Optional[float]
    explanation: str
    design_stage: int
    reference_value: Optional[float] = None
    relative_score_percent: float = 0.0
    better_than_reference: bool = False
    protocol_baseline_points: float = 0.0
    scheme_points: float = 0.0
    score_delta: float = 0.0
    protocol_utility: float = 0.0
    scheme_utility: float = 0.0
    scoring_basis: str = "linear_protocol_distance"

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        difference = None if self.value is None or self.reference_value is None else self.value - self.reference_value
        ratio = None if self.value is None or self.reference_value in (None, 0.0) else self.value / self.reference_value
        if difference is None:
            value_comparison = "无法比较"
        elif difference > 1e-9:
            value_comparison = "高于基准"
        elif difference < -1e-9:
            value_comparison = "低于基准"
        else:
            value_comparison = "与基准相同"
        if self.score_delta > 0.005:
            performance_comparison = "优于基准"
        elif self.score_delta < -0.005:
            performance_comparison = "劣于基准"
        else:
            performance_comparison = "与基准相当"
        result.update({
            "candidate_value": self.value,
            "baseline_value": self.reference_value,
            "difference": round(difference, 9) if difference is not None else None,
            "ratio_to_baseline": round(ratio, 9) if ratio is not None else None,
            "direction": self.requirement_type,
            "direction_label": self.requirement_label,
            "value_comparison": value_comparison,
            "performance_comparison": performance_comparison,
            "comparison_label": f"{value_comparison}，{performance_comparison}",
            "baseline_points": self.protocol_baseline_points,
            "candidate_points": self.scheme_points,
            "contribution_delta": self.score_delta,
            "contribution_reason": self.explanation,
        })
        return result


@dataclass
class RequirementAssessment:
    profile_id: Optional[str]
    profile_name: str
    coverage: float
    coverage_percent: float
    decision: str
    decision_label: str
    hard_gap_count: int
    hard_gaps: List[str]
    attributes: List[AttributeRequirementAssessment] = field(default_factory=list)
    stage_weight_share: Dict[int, float] = field(default_factory=dict)
    thresholds: Dict[str, float] = field(default_factory=dict)
    reference_score: float = 100.0
    scoring_mode: str = "protocol_reference_100"
    weight_source: str = "design_stage_prior_50_30_20"
    primary_score: float = 0.0
    robust_p10: Optional[float] = None
    robust_median: Optional[float] = None
    robust_p90: Optional[float] = None
    robust_min: Optional[float] = None
    robust_max: Optional[float] = None
    robust_model_count: int = 0
    robust_unique_model_count: int = 0
    support_at_80: Optional[float] = None
    support_at_100: Optional[float] = None
    robust_conclusion: str = "single_model"
    robust_conclusion_label: str = ROBUST_LABELS["single_model"]

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["attributes"] = [item.to_dict() for item in self.attributes]
        result["baseline_comparison"] = result.pop("decision")
        result["baseline_comparison_label"] = result.pop("decision_label")
        result.pop("thresholds", None)
        result["baseline_profile_id"] = self.profile_id
        result["baseline_profile_name"] = self.profile_name
        result["business_scope"] = "product_level_relative_effectiveness"
        result["reuse_classification_enabled"] = False
        return result


class RequirementEvaluator:
    """Anchor learned generic UTA value differences at a protocol score of 100."""

    DEFAULT_STAGE_SHARES = {1: 0.50, 2: 0.30, 3: 0.20}

    def __init__(
        self,
        project: ProjectDataset,
        profile: Any = _DEFAULT_PROFILE,
        attribute_weights: Optional[Dict[str, float]] = None,
        weight_source: Optional[str] = None,
        uta_segments: int = DEFAULT_UTA_SEGMENTS,
        uta_increments: Optional[Sequence[float]] = None,
        robust_models: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> None:
        self.project = project
        self.profile = project.default_requirement_profile() if profile is _DEFAULT_PROFILE else profile
        self.attributes = project.attribute_by_key()
        self.attribute_weights = dict(attribute_weights or {})
        self.uta_segments = int(uta_segments)
        self.uta_increments = [float(value) for value in uta_increments] if uta_increments is not None else []
        self.robust_models = list(robust_models or [])
        self.weight_source = weight_source or (
            "learned_generic_preference_weights"
            if self.attribute_weights
            else "design_stage_prior_50_30_20"
        )

    def evaluate(self, params: Dict[str, Any]) -> RequirementAssessment:
        if self.profile is None:
            return RequirementAssessment(
                profile_id=None,
                profile_name="未设置基准技术协议",
                coverage=0.0,
                coverage_percent=0.0,
                decision="baseline_unavailable",
                decision_label=DECISION_LABELS["baseline_unavailable"],
                hard_gap_count=0,
                hard_gaps=[],
            )

        stage_prior, stage_shares = self._stage_prior(
            [item.attribute_key for item in self.profile.requirements]
        )
        if self.uta_increments:
            assessments = self._uta_assessments(params, self.uta_increments, stage_prior)
            scoring_mode = "protocol_anchored_robust_uta"
        else:
            weights, _ = self._attribute_weights()
            assessments = [
                self._evaluate_attribute(
                    self.attributes[requirement.attribute_key],
                    requirement,
                    self._numeric_value(params.get(requirement.attribute_key)),
                    weights[requirement.attribute_key],
                )
                for requirement in self.profile.requirements
            ]
            scoring_mode = (
                "protocol_anchored_bt_linear" if self.attribute_weights else "protocol_anchored_stage_prior"
            )

        relative_utility = clamp(sum(item.weighted_score for item in assessments), 0.0, 2.0)
        robust_scores = self._robust_scores(params, stage_prior, relative_utility * 100.0)
        robust = self._robust_summary(robust_scores)
        hard_gaps = [item.attribute_label for item in assessments if item.hard_gap]
        decision = self._decision(relative_utility, bool(hard_gaps))
        return RequirementAssessment(
            profile_id=self.profile.id,
            profile_name=self.profile.name,
            coverage=round(relative_utility, 6),
            coverage_percent=round(relative_utility * 100.0, 2),
            decision=decision,
            decision_label=DECISION_LABELS[decision],
            hard_gap_count=len(hard_gaps),
            hard_gaps=hard_gaps,
            attributes=assessments,
            stage_weight_share=stage_shares,
            thresholds={},
            weight_source=self.weight_source,
            scoring_mode=scoring_mode,
            primary_score=round(relative_utility * 100.0, 2),
            **robust,
        )

    def _uta_assessments(
        self,
        params: Dict[str, Any],
        increments: Sequence[float],
        stage_prior: Dict[str, float],
    ) -> List[AttributeRequirementAssessment]:
        assert self.profile is not None
        model = LPUTAModel(
            self.project,
            segments=self.uta_segments,
            increments=increments,
            requirement_profile=None,
        )
        protocol_params = {
            requirement.attribute_key: self._reference_value(requirement)
            for requirement in self.profile.requirements
        }
        scheme_vector = model.space.segment_vector(params)
        protocol_vector = model.space.segment_vector(protocol_params)
        slices = {spec.key: (spec, attr_slice) for spec, attr_slice in model.space.attribute_slices()}
        monotonic_keys = set(slices)
        target_keys = [item.attribute_key for item in self.profile.requirements if item.attribute_key not in monotonic_keys]
        target_share = clamp(sum(stage_prior.get(key, 0.0) for key in target_keys), 0.0, 1.0)
        if not monotonic_keys:
            target_share = 1.0
        monotonic_share = 1.0 - target_share
        raw_monotonic_total = sum(
            float(model.increments[attr_slice].sum()) for _, attr_slice in slices.values()
        )
        if raw_monotonic_total <= 1e-12:
            monotonic_share = 0.0
            target_share = 1.0
        target_prior_total = sum(stage_prior.get(key, 0.0) for key in target_keys)

        assessments: List[AttributeRequirementAssessment] = []
        for requirement in self.profile.requirements:
            spec = self.attributes[requirement.attribute_key]
            value = self._numeric_value(params.get(spec.key))
            reference = self._reference_value(requirement)
            if spec.key in slices and value is not None and reference is not None:
                _, attr_slice = slices[spec.key]
                raw_weight = float(model.increments[attr_slice].sum())
                scale = monotonic_share / max(raw_monotonic_total, 1e-12)
                weight = raw_weight * scale
                scheme_utility = float(scheme_vector[attr_slice] @ model.increments[attr_slice]) * scale
                protocol_utility = float(protocol_vector[attr_slice] @ model.increments[attr_slice]) * scale
                relative = (
                    clamp(1.0 + (scheme_utility - protocol_utility) / weight, 0.0, 2.0)
                    if weight > 1e-12
                    else 1.0
                )
                _, met, gap, better, _ = self._score_value(spec, requirement, value, reference)
                explanation = (
                    f"{spec.label}{'达到' if met else '未达到'}100分参考值"
                    f"{'，优于基准参考' if better else ''}；完整UTA边际曲线换算后，"
                    f"该属性相对得分 {relative * 100.0:.1f} 分，对总分影响 "
                    f"{100.0 * weight * (relative - 1.0):+.2f} 分。"
                )
                assessments.append(
                    self._assessment_record(
                        spec,
                        requirement,
                        value,
                        reference,
                        weight,
                        relative,
                        met,
                        gap,
                        better,
                        explanation,
                        protocol_utility,
                        scheme_utility,
                        "learned_piecewise_uta",
                    )
                )
                continue

            weight = (
                stage_prior.get(spec.key, 0.0) / max(target_prior_total, 1e-12) * target_share
                if target_keys
                else 0.0
            )
            assessments.append(self._evaluate_attribute(spec, requirement, value, weight))
        return assessments

    def _robust_scores(
        self,
        params: Dict[str, Any],
        stage_prior: Dict[str, float],
        primary_score: float,
    ) -> List[float]:
        if not self.uta_increments:
            return [round(primary_score, 6)]
        candidates: List[Sequence[float]] = [
            model.get("increments") or [] for model in self.robust_models
        ]
        primary_key = tuple(round(float(value), 8) for value in self.uta_increments)
        if not any(
            tuple(round(float(value), 8) for value in candidate) == primary_key
            for candidate in candidates
        ):
            candidates.insert(0, self.uta_increments)
        scores: List[float] = []
        for increments in candidates:
            key = tuple(round(float(value), 8) for value in increments)
            if not key:
                continue
            assessments = self._uta_assessments(params, increments, stage_prior)
            scores.append(clamp(sum(item.weighted_score for item in assessments), 0.0, 2.0) * 100.0)
        return scores or [round(primary_score, 6)]

    @staticmethod
    def _percentile(values: Sequence[float], probability: float) -> float:
        ordered = sorted(float(value) for value in values)
        if len(ordered) == 1:
            return ordered[0]
        position = clamp(probability, 0.0, 1.0) * (len(ordered) - 1)
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = position - lower
        return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction

    def _robust_summary(self, scores: Sequence[float]) -> Dict[str, Any]:
        values = list(scores)
        count = len(values)
        unique_count = len({round(float(value), 8) for value in values})
        minimum = min(values)
        maximum = max(values)
        support_80 = sum(value >= 80.0 - 1e-9 for value in values) / count
        support_100 = sum(value >= 100.0 - 1e-9 for value in values) / count
        if count < 2:
            conclusion = "single_model"
        elif minimum >= 100.0 - 1e-9:
            conclusion = "all_models_at_or_above_baseline"
        elif maximum >= 100.0 - 1e-9:
            conclusion = "models_span_baseline"
        else:
            conclusion = "all_models_below_baseline"
        return {
            "robust_p10": round(self._percentile(values, 0.10), 2),
            "robust_median": round(self._percentile(values, 0.50), 2),
            "robust_p90": round(self._percentile(values, 0.90), 2),
            "robust_min": round(minimum, 2),
            "robust_max": round(maximum, 2),
            "robust_model_count": count,
            "robust_unique_model_count": unique_count,
            "support_at_80": round(support_80, 3),
            "support_at_100": round(support_100, 3),
            "robust_conclusion": conclusion,
            "robust_conclusion_label": ROBUST_LABELS[conclusion],
        }

    def _attribute_weights(self) -> Tuple[Dict[str, float], Dict[int, float]]:
        assert self.profile is not None
        requirement_keys = [item.attribute_key for item in self.profile.requirements]
        prior, stage_shares = self._stage_prior(requirement_keys)
        if not self.attribute_weights:
            return prior, stage_shares
        combined = {
            key: max(0.0, float(self.attribute_weights.get(key, prior.get(key, 0.0))))
            for key in requirement_keys
        }
        total = sum(combined.values())
        if total <= 1e-12:
            return prior, stage_shares
        return {key: value / total for key, value in combined.items()}, stage_shares

    def _stage_prior(self, keys: List[str]) -> Tuple[Dict[str, float], Dict[int, float]]:
        stage_members: Dict[int, List[str]] = {}
        for key in keys:
            stage_members.setdefault(self.attributes[key].design_stage, []).append(key)
        active_total = sum(self.DEFAULT_STAGE_SHARES.get(stage, 0.0) for stage in stage_members)
        if active_total <= 0.0:
            equal = 1.0 / max(1, len(keys))
            return {key: equal for key in keys}, {}
        stage_shares = {
            stage: self.DEFAULT_STAGE_SHARES.get(stage, 0.0) / active_total
            for stage in stage_members
        }
        weights: Dict[str, float] = {}
        for stage, members in stage_members.items():
            for key in members:
                weights[key] = stage_shares[stage] / len(members)
        return weights, stage_shares

    @staticmethod
    def _numeric_value(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _reference_value(requirement: RequirementSpec) -> Optional[float]:
        if requirement.target_value is not None:
            return float(requirement.target_value)
        if requirement.requirement_type == "at_least" and requirement.minimum is not None:
            return float(requirement.minimum)
        if requirement.requirement_type == "at_most" and requirement.maximum is not None:
            return float(requirement.maximum)
        if requirement.minimum is not None and requirement.maximum is not None:
            return 0.5 * (float(requirement.minimum) + float(requirement.maximum))
        return None

    def _evaluate_attribute(
        self,
        spec: AttributeSpec,
        requirement: RequirementSpec,
        value: Optional[float],
        weight: float,
    ) -> AttributeRequirementAssessment:
        reference = self._reference_value(requirement)
        requirement_text = (
            f"基准协议100分参考值 {reference:g} {spec.unit}"
            if reference is not None
            else "基准值缺失"
        )
        if value is None or reference is None:
            relative = 0.0
            met = False
            gap = None
            better = False
            explanation = f"{spec.label}没有可比较的数值，暂按0分处理。"
        else:
            relative, met, gap, better, explanation = self._score_value(
                spec, requirement, value, reference
            )
        return self._assessment_record(
            spec,
            requirement,
            value,
            reference,
            weight,
            relative,
            met,
            gap,
            better,
            explanation,
            weight,
            relative * weight,
            "protocol_centered_target" if requirement.requirement_type in {"within_range", "target"} else "linear_fallback",
        )

    def _assessment_record(
        self,
        spec: AttributeSpec,
        requirement: RequirementSpec,
        value: Optional[float],
        reference: Optional[float],
        weight: float,
        relative: float,
        met: bool,
        gap: Optional[float],
        better: bool,
        explanation: str,
        protocol_utility: float,
        scheme_utility: float,
        scoring_basis: str,
    ) -> AttributeRequirementAssessment:
        requirement_text = (
            f"基准协议100分参考值 {reference:g} {spec.unit}"
            if reference is not None
            else "基准值缺失"
        )
        hard_gap = bool(requirement.hard_requirement and not met)
        return AttributeRequirementAssessment(
            attribute_key=spec.key,
            attribute_label=spec.label,
            unit=spec.unit,
            value=value,
            requirement_type=requirement.requirement_type,
            requirement_label=REQUIREMENT_TYPE_LABELS.get(requirement.requirement_type, "基准参考"),
            requirement_text=requirement_text,
            satisfaction=round(relative, 6),
            weight=round(weight, 6),
            weighted_score=round(relative * weight, 6),
            met=met,
            hard_requirement=requirement.hard_requirement,
            hard_gap=hard_gap,
            gap=None if gap is None else round(gap, max(3, spec.precision)),
            explanation=explanation,
            design_stage=spec.design_stage,
            reference_value=reference,
            relative_score_percent=round(relative * 100.0, 2),
            better_than_reference=better,
            protocol_baseline_points=round(weight * 100.0, 4),
            scheme_points=round(relative * weight * 100.0, 4),
            score_delta=round((relative - 1.0) * weight * 100.0, 4),
            protocol_utility=round(protocol_utility, 8),
            scheme_utility=round(scheme_utility, 8),
            scoring_basis=scoring_basis,
        )

    def _score_value(
        self,
        spec: AttributeSpec,
        requirement: RequirementSpec,
        value: float,
        reference: float,
    ) -> Tuple[float, bool, float, bool, str]:
        assert spec.generation_min is not None and spec.generation_max is not None
        lo = float(spec.generation_min)
        hi = float(spec.generation_max)
        span = max(hi - lo, 1e-12)
        kind = requirement.requirement_type
        if kind in {"higher_better", "at_least"}:
            relative = clamp(1.0 + (value - reference) / span, 0.0, 2.0)
            met = value >= reference
            gap = max(0.0, reference - value)
            better = value > reference
        elif kind in {"lower_better", "at_most"}:
            relative = clamp(1.0 + (reference - value) / span, 0.0, 2.0)
            met = value <= reference
            gap = max(0.0, value - reference)
            better = value < reference
        else:
            if value <= reference:
                relative = self._linear(value, lo, reference)
            else:
                relative = self._linear(value, hi, reference)
            tolerance = max(float(requirement.tolerance), 0.5 * (10.0 ** (-spec.precision)))
            gap = max(0.0, abs(value - reference) - tolerance)
            met = gap <= 0.0
            better = False
        direction = "达到" if met else "未达到"
        comparison = "，优于基准参考" if better else ""
        explanation = (
            f"{spec.label}{direction}100分参考值{comparison}；"
            f"该属性相对得分 {relative * 100.0:.1f} 分。"
        )
        return relative, met, gap, better, explanation

    @staticmethod
    def _linear(value: float, zero: float, one: float) -> float:
        width = one - zero
        if abs(width) <= 1e-12:
            return 1.0 if value == one else 0.0
        return clamp((value - zero) / width, 0.0, 1.0)

    def _decision(self, score_ratio: float, has_hard_gap: bool) -> str:
        del has_hard_gap  # Physical feasibility remains a separate gate.
        if score_ratio > 1.0 + 5e-5:
            return "above_baseline"
        if score_ratio < 1.0 - 5e-5:
            return "below_baseline"
        return "equal_to_baseline"
