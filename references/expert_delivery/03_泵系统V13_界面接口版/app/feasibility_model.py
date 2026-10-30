# -*- coding: utf-8 -*-
"""Explainable expert-updated feasibility model for generic projects."""

from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List, Optional, Tuple

from coupling_model import CouplingSystem
from project_excel import ProjectDataset


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class ExpertFeasibilityModel:
    def __init__(
        self,
        project: ProjectDataset,
        coupling_system: CouplingSystem,
        weights: Optional[Dict[str, float]] = None,
        frontier_evidence: Optional[List[Dict[str, Any]]] = None,
        expert_evidence: Optional[List[Dict[str, Any]]] = None,
        learned_boundaries: Optional[List[Dict[str, Any]]] = None,
        conditional_context: bool = False,
    ):
        self.project = project
        self.coupling_system = coupling_system
        self.attributes = project.attributes
        self.attribute_by_key = project.attribute_by_key()
        self.conditional_context = bool(conditional_context)
        self.frontier_evidence = list(frontier_evidence or [])
        self.expert_evidence = list(expert_evidence if expert_evidence is not None else self.frontier_evidence)
        self.learned_boundaries = (
            [dict(item) for item in learned_boundaries]
            if learned_boundaries is not None
            else self.learn_attribute_boundaries(self.expert_evidence)
        )
        self.labels = self.feature_labels()
        self.weights = dict(self.prior_weights())
        if weights:
            for name in self.labels:
                if name in weights:
                    self.weights[name] = float(weights[name])

    @staticmethod
    def sigmoid(value: float) -> float:
        if value >= 35.0:
            return 1.0
        if value <= -35.0:
            return 0.0
        return 1.0 / (1.0 + math.exp(-value))

    def feature_labels(self) -> Dict[str, str]:
        labels = {
            "bias": "基础可行先验",
            "range_violation": "超出生成范围的硬违反度",
            "range_edge": "超出已有样本经验范围",
            "experience_distance": "远离已有工程样本（只表示未知）",
        }
        for target_key, model in self.coupling_system.models.items():
            labels[f"coupling_{target_key}_below"] = f"{model.target_label}低于条件经验带"
            labels[f"coupling_{target_key}_above"] = f"{model.target_label}高于条件经验带"
            labels[f"coupling_{target_key}_boundary"] = f"{model.target_label}接近条件边界"
        for target_key in self.coupling_system.target_keys():
            target = self.attribute_by_key[target_key]
            labels[f"frontier_{target_key}_low"] = f"{target.label}低于专家标出的单调可行前沿"
            labels[f"frontier_{target_key}_high"] = f"{target.label}高于专家标出的单调可行前沿"
        for spec in self.attributes:
            if not spec.is_numeric:
                continue
            labels[f"expert_boundary_{spec.key}_low"] = f"{spec.label}低于专家反复确认的可行下边界"
            labels[f"expert_boundary_{spec.key}_high"] = f"{spec.label}高于专家反复确认的可行上边界"
        return labels

    def prior_weights(self) -> Dict[str, float]:
        weights = {
            "bias": 1.65,
            "range_violation": -3.20,
            "range_edge": -0.18,
            "experience_distance": 0.0,
        }
        for target_key in self.coupling_system.models:
            mode = self.coupling_system.constraint_mode(target_key)
            weights[f"coupling_{target_key}_below"] = (
                -1.60 if mode in {"minimum_frontier", "hard_band"} else 0.0
            )
            weights[f"coupling_{target_key}_above"] = (
                -1.60 if mode in {"maximum_frontier", "hard_band"} else 0.0
            )
            weights[f"coupling_{target_key}_boundary"] = -0.25
        for target_key in self.coupling_system.target_keys():
            mode = self.coupling_system.constraint_mode(target_key)
            weights[f"frontier_{target_key}_low"] = (
                -1.35 if mode in {"minimum_frontier", "hard_band"} else 0.0
            )
            weights[f"frontier_{target_key}_high"] = (
                -1.35 if mode in {"maximum_frontier", "hard_band"} else 0.0
            )
        for spec in self.attributes:
            if not spec.is_numeric:
                continue
            weights[f"expert_boundary_{spec.key}_low"] = -4.00
            weights[f"expert_boundary_{spec.key}_high"] = -4.00
        return weights

    def normalized_distance(self, a: Dict[str, Any], b: Dict[str, Any]) -> float:
        values = []
        for spec in self.attributes:
            if not spec.is_numeric:
                continue
            assert spec.generation_min is not None and spec.generation_max is not None
            span = max(spec.generation_max - spec.generation_min, 1e-9)
            values.append((float(a[spec.key]) - float(b[spec.key])) / span)
        return math.sqrt(sum(value * value for value in values) / max(len(values), 1))

    def features(self, params: Dict[str, Any]) -> Dict[str, float]:
        range_violation = 0.0
        experience_extrapolation = 0.0
        for spec in self.attributes:
            if not spec.is_numeric:
                continue
            assert spec.generation_min is not None and spec.generation_max is not None
            assert spec.feasible_min is not None and spec.feasible_max is not None
            value = float(params[spec.key])
            generation_span = max(spec.generation_max - spec.generation_min, 1e-9)
            if value < spec.generation_min:
                range_violation = max(range_violation, (spec.generation_min - value) / generation_span)
            elif value > spec.generation_max:
                range_violation = max(range_violation, (value - spec.generation_max) / generation_span)
            if value < spec.feasible_min:
                available = max(spec.feasible_min - spec.generation_min, 0.08 * generation_span, 1e-9)
                experience_extrapolation = max(
                    experience_extrapolation, clamp((spec.feasible_min - value) / available, 0.0, 1.5)
                )
            elif value > spec.feasible_max:
                available = max(spec.generation_max - spec.feasible_max, 0.08 * generation_span, 1e-9)
                experience_extrapolation = max(
                    experience_extrapolation, clamp((value - spec.feasible_max) / available, 0.0, 1.5)
                )

        nearest = min(
            (self.normalized_distance(params, sample.params) for sample in self.project.schemes),
            default=0.0,
        )
        output = {
            "bias": 1.0,
            "range_violation": clamp(range_violation, 0.0, 1.5),
            "range_edge": clamp(experience_extrapolation, 0.0, 1.5),
            "experience_distance": clamp(nearest / 0.45, 0.0, 1.5),
        }
        for assessment in self.coupling_system.assess(params):
            below = f"coupling_{assessment.target_key}_below"
            above = f"coupling_{assessment.target_key}_above"
            boundary = f"coupling_{assessment.target_key}_boundary"
            band_width = max(assessment.upper - assessment.lower, 1e-9)
            output[below] = (
                clamp((assessment.predicted - assessment.actual) / band_width, 0.0, 1.5)
                if assessment.constraint_mode in {"minimum_frontier", "hard_band"}
                else 0.0
            )
            output[above] = (
                clamp((assessment.actual - assessment.predicted) / band_width, 0.0, 1.5)
                if assessment.constraint_mode in {"maximum_frontier", "hard_band"}
                else 0.0
            )
            output[boundary] = (
                1.0
                if assessment.status == "near_boundary" and assessment.feasibility_relevant
                else 0.0
            )
        output.update(self.frontier_features(params))
        output.update(self.boundary_features(params))
        return output

    def infer_attribute_feedback(self, evidence: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        feedback = evidence.get("attribute_feedback")
        if isinstance(feedback, dict):
            key = feedback.get("attribute_key")
            side = feedback.get("side")
            if key in self.attribute_by_key and side in {"low", "high"}:
                return dict(feedback)

        coupling = evidence.get("coupling_feedback") or {}
        issue = coupling.get("issue")
        target_key = coupling.get("target_key")
        if target_key in self.attribute_by_key and issue in {"target_low", "target_high"}:
            return {
                "attribute_key": target_key,
                "side": "low" if issue == "target_low" else "high",
                "scope": "conditional_coupling",
                "confidence": float(coupling.get("confidence", 0.72)),
            }

        if "single_range" not in (evidence.get("reason_codes") or []):
            return None
        params = evidence.get("params") or {}
        candidates: List[Tuple[float, str, str]] = []
        for spec in self.attributes:
            if not spec.is_numeric or spec.key not in params:
                continue
            assert spec.generation_min is not None and spec.generation_max is not None
            assert spec.feasible_min is not None and spec.feasible_max is not None
            span = max(float(spec.generation_max) - float(spec.generation_min), 1e-9)
            value = float(params[spec.key])
            if value < spec.feasible_min:
                candidates.append(((spec.feasible_min - value) / span, spec.key, "low"))
            elif value > spec.feasible_max:
                candidates.append(((value - spec.feasible_max) / span, spec.key, "high"))
        if not candidates:
            return None
        _, key, side = max(candidates)
        return {
            "attribute_key": key,
            "side": side,
            "scope": "inferred_single_range",
            "confidence": 0.68,
        }

    def learn_attribute_boundaries(self, evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        feasible_values: Dict[str, List[float]] = {
            spec.key: [] for spec in self.attributes if spec.is_numeric
        }
        negative_values: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for item in evidence:
            if item.get("status", "active") != "active":
                continue
            params = item.get("params") or {}
            if item.get("label") == "feasible":
                for key in feasible_values:
                    if key in params:
                        feasible_values[key].append(float(params[key]))
                continue
            if item.get("label") != "infeasible":
                continue
            feedback = self.infer_attribute_feedback(item)
            if not feedback:
                continue
            if self.conditional_context and feedback.get("scope") == "conditional_coupling":
                continue
            key = feedback["attribute_key"]
            side = feedback["side"]
            if key not in params or key not in feasible_values:
                continue
            negative_values.setdefault((key, side), []).append(
                {
                    "value": float(params[key]),
                    "scope": feedback.get("scope", "attribute"),
                    "confidence": float(feedback.get("confidence", 0.75)),
                    "evidence_id": item.get("id"),
                }
            )

        boundaries: List[Dict[str, Any]] = []
        for (key, side), negatives in sorted(negative_values.items()):
            spec = self.attribute_by_key[key]
            values = [item["value"] for item in negatives]
            positives = feasible_values.get(key, [])
            if side == "low":
                negative_edge = max(values)
                brackets = [value for value in positives if value >= negative_edge]
                feasible_anchor = min(brackets) if brackets else None
            else:
                negative_edge = min(values)
                brackets = [value for value in positives if value <= negative_edge]
                feasible_anchor = max(brackets) if brackets else None
            boundary = (
                negative_edge + 0.65 * (feasible_anchor - negative_edge)
                if feasible_anchor is not None
                else negative_edge
            )
            explicit_count = sum(1 for item in negatives if item["scope"] != "conditional_coupling")
            mean_confidence = sum(item["confidence"] for item in negatives) / len(negatives)
            confidence = min(
                0.95,
                0.28
                + 0.11 * len(negatives)
                + (0.14 if feasible_anchor is not None else 0.0)
                + (0.05 if explicit_count else 0.0),
            )
            confidence *= clamp(mean_confidence / 0.80, 0.75, 1.05)
            mature = bool(
                (len(negatives) >= 3 and feasible_anchor is not None)
                or len(negatives) >= 5
            )
            boundaries.append(
                {
                    "attribute_key": key,
                    "attribute_label": spec.label,
                    "side": side,
                    "boundary": round(boundary, max(3, spec.precision)),
                    "unit": spec.unit,
                    "negative_count": len(negatives),
                    "feasible_anchor_count": len(brackets),
                    "feasible_anchor": feasible_anchor,
                    "confidence": round(clamp(confidence, 0.0, 0.95), 3),
                    "mature": mature,
                    "scope": "attribute" if explicit_count else "conditional_coupling",
                    "evidence_ids": [item["evidence_id"] for item in negatives if item.get("evidence_id")],
                }
            )
        return boundaries

    def boundary_features(self, params: Dict[str, Any]) -> Dict[str, float]:
        output: Dict[str, float] = {}
        for spec in self.attributes:
            if spec.is_numeric:
                output[f"expert_boundary_{spec.key}_low"] = 0.0
                output[f"expert_boundary_{spec.key}_high"] = 0.0
        for boundary in self.learned_boundaries:
            spec = self.attribute_by_key[boundary["attribute_key"]]
            value = float(params[spec.key])
            limit = float(boundary["boundary"])
            assert spec.generation_min is not None and spec.generation_max is not None
            span = max(float(spec.generation_max) - float(spec.generation_min), 1e-9)
            distance = limit - value if boundary["side"] == "low" else value - limit
            if distance <= 0.0:
                continue
            maturity_scale = 1.0 if boundary["mature"] else 0.55
            severity = clamp(0.45 + 3.0 * distance / span, 0.0, 1.5)
            output[f"expert_boundary_{spec.key}_{boundary['side']}"] = (
                severity * float(boundary["confidence"]) * maturity_scale
            )
        return output

    def boundary_violations(self, params: Dict[str, Any], mature_only: bool = False) -> List[Dict[str, Any]]:
        features = self.boundary_features(params)
        violations: List[Dict[str, Any]] = []
        for boundary in self.learned_boundaries:
            if mature_only and not boundary["mature"]:
                continue
            spec = self.attribute_by_key[boundary["attribute_key"]]
            value = float(params[spec.key])
            limit = float(boundary["boundary"])
            violated = value < limit if boundary["side"] == "low" else value > limit
            if not violated:
                continue
            direction = "低于" if boundary["side"] == "low" else "高于"
            feature_name = f"expert_boundary_{spec.key}_{boundary['side']}"
            violations.append(
                {
                    **boundary,
                    "value": round(value, spec.precision),
                    "severity": round(features.get(feature_name, 0.0), 3),
                    "message": (
                        f"{spec.label}当前值 {value:.{spec.precision}f} {spec.unit}，{direction}专家证据"
                        f"形成的可行边界 {limit:.{spec.precision}f} {spec.unit}；"
                        f"依据 {boundary['negative_count']} 次不可行判断。"
                    ),
                }
            )
        return violations

    def boundary_summaries(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self.learned_boundaries]

    def frontier_features(self, params: Dict[str, Any]) -> Dict[str, float]:
        output: Dict[str, float] = {}
        for target_key in self.coupling_system.target_keys():
            output[f"frontier_{target_key}_low"] = 0.0
            output[f"frontier_{target_key}_high"] = 0.0
        for evidence in self.frontier_evidence:
            feedback = evidence.get("coupling_feedback") or {}
            target_key = feedback.get("target_key")
            issue = feedback.get("issue")
            anchor = evidence.get("params") or {}
            if target_key not in self.coupling_system.target_keys() or target_key not in anchor:
                continue
            context_weight = (
                self.coupling_system.context_weight(target_key, params, anchor)
                if self.conditional_context
                else 1.0
            )
            if context_weight < 0.025:
                continue
            edges = self.coupling_system.edges_by_target.get(target_key, [])
            demanding = True
            relaxed = True
            source_shift = 0.0
            for edge in edges:
                spec = self.attribute_by_key[edge.source_key]
                if edge.source_key not in anchor:
                    demanding = relaxed = False
                    break
                span = max(float(spec.generation_max) - float(spec.generation_min), 1e-9)
                delta = (float(params[edge.source_key]) - float(anchor[edge.source_key])) / span
                signed = delta if edge.direction == "positive" else -delta
                demanding = demanding and signed >= -0.015
                relaxed = relaxed and signed <= 0.015
                source_shift += abs(signed)
            target = self.attribute_by_key[target_key]
            target_span = max(float(target.generation_max) - float(target.generation_min), 1e-9)
            target_delta = (float(params[target_key]) - float(anchor[target_key])) / target_span
            severity = clamp(0.55 + source_shift / max(len(edges), 1), 0.0, 1.5) * context_weight
            mode = self.coupling_system.constraint_mode(target_key)
            if (
                issue == "target_low"
                and mode in {"minimum_frontier", "hard_band"}
                and demanding
                and target_delta <= 0.015
            ):
                name = f"frontier_{target_key}_low"
                output[name] = max(output[name], severity)
            elif (
                issue == "target_high"
                and mode in {"maximum_frontier", "hard_band"}
                and relaxed
                and target_delta >= -0.015
            ):
                name = f"frontier_{target_key}_high"
                output[name] = max(output[name], severity)
            elif issue == "mismatch" and mode == "hard_band":
                distance = (
                    self.coupling_system.context_distance(target_key, params, anchor)
                    if self.conditional_context
                    else self.normalized_distance(params, anchor)
                )
                radius = 2.0 if self.conditional_context else 0.18
                if distance < radius:
                    name = f"frontier_{target_key}_low"
                    output[name] = max(
                        output[name],
                        context_weight * clamp(1.0 - distance / radius, 0.0, 1.0),
                    )
        return output

    @staticmethod
    def reason_guided_features(
        features: Dict[str, float], label: float, reason_codes: List[str]
    ) -> Dict[str, float]:
        guided = dict(features)
        if label >= 0.5:
            return guided
        for code in reason_codes:
            if code == "single_range":
                guided["range_violation"] = 1.65 * guided.get("range_violation", 0.0)
            elif code.startswith("coupling_"):
                target_key = code[len("coupling_") :]
                for suffix in ("below", "above", "boundary"):
                    name = f"coupling_{target_key}_{suffix}"
                    guided[name] = 1.65 * guided.get(name, 0.0)
                for suffix in ("low", "high"):
                    name = f"frontier_{target_key}_{suffix}"
                    guided[name] = 1.65 * guided.get(name, 0.0)
            elif code.startswith("attribute_low_"):
                name = f"expert_boundary_{code[len('attribute_low_'):]}_low"
                guided[name] = 1.85 * guided.get(name, 0.0)
            elif code.startswith("attribute_high_"):
                name = f"expert_boundary_{code[len('attribute_high_'):]}_high"
                guided[name] = 1.85 * guided.get(name, 0.0)
        return guided

    def raw(self, params: Dict[str, Any]) -> float:
        features = self.features(params)
        return sum(self.weights.get(name, 0.0) * features.get(name, 0.0) for name in self.labels)

    def local_evidence_assessment(self, params: Dict[str, Any]) -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = []
        class_counts = {
            label: sum(
                1
                for item in self.expert_evidence
                if item.get("status", "active") == "active" and item.get("label") == label
            )
            for label in ("feasible", "infeasible")
        }
        for item in self.expert_evidence:
            if item.get("status", "active") != "active" or item.get("label") not in {"feasible", "infeasible"}:
                continue
            anchor = item.get("params") or {}
            coupling = item.get("coupling_feedback") or {}
            target_key = coupling.get("target_key")
            if target_key in self.coupling_system.target_keys():
                context_distance = self.coupling_system.context_distance(target_key, params, anchor)
                context_weight = self.coupling_system.context_weight(target_key, params, anchor)
                target_spec = self.attribute_by_key[target_key]
                assert target_spec.generation_min is not None and target_spec.generation_max is not None
                target_span = max(
                    float(target_spec.generation_max) - float(target_spec.generation_min),
                    1e-9,
                )
                if target_key in self.coupling_system.models:
                    query_band = self.coupling_system.band(target_key, params)
                    anchor_band = self.coupling_system.band(target_key, anchor)

                    def residual_coordinate(values: Dict[str, Any], band: Dict[str, Any]) -> float:
                        width = max(
                            float(band["upper"]) - float(band["lower"]),
                            0.04 * target_span,
                            1e-9,
                        )
                        return (
                            float(values[target_key]) - float(band["predicted"])
                        ) / width

                    query_residual = residual_coordinate(params, query_band)
                    anchor_residual = residual_coordinate(anchor, anchor_band)
                else:
                    query_residual = (
                        float(params[target_key]) - float(target_spec.generation_min)
                    ) / target_span
                    anchor_residual = (
                        float(anchor[target_key]) - float(target_spec.generation_min)
                    ) / target_span
                residual_distance = abs(query_residual - anchor_residual)
                residual_weight = math.exp(-0.5 * (residual_distance / 0.72) ** 2)
                issue = coupling.get("issue")
                if issue == "target_low" and query_residual > anchor_residual + 0.18:
                    residual_weight *= math.exp(
                        -0.5 * ((query_residual - anchor_residual - 0.18) / 0.35) ** 2
                    )
                elif issue == "target_high" and query_residual < anchor_residual - 0.18:
                    residual_weight *= math.exp(
                        -0.5 * ((anchor_residual - query_residual - 0.18) / 0.35) ** 2
                    )
                distance = math.sqrt(context_distance**2 + residual_distance**2)
                weight = context_weight * residual_weight
                scope = f"coupling:{target_key}"
            else:
                distance = self.normalized_distance(params, anchor)
                weight = math.exp(-0.5 * (distance / 0.22) ** 2) if distance < 0.85 else 0.0
                scope = "full_product"
            confidence = float((item.get("coupling_feedback") or {}).get("confidence", 0.82))
            weight *= clamp(confidence, 0.35, 1.0)
            weight /= math.sqrt(max(class_counts.get(str(item.get("label")), 1), 1))
            if weight < 0.02:
                continue
            rows.append(
                {
                    "evidence_id": item.get("id"),
                    "label": item.get("label"),
                    "weight": weight,
                    "distance": distance,
                    "scope": scope,
                }
            )
        total = sum(item["weight"] for item in rows)
        if total <= 1e-12:
            return {
                "available": False,
                "probability": None,
                "effective_support": 0.0,
                "evidence_ids": [],
                "scope": "none",
            }
        positive = sum(item["weight"] for item in rows if item["label"] == "feasible")
        negative = sum(item["weight"] for item in rows if item["label"] == "infeasible")
        effective = total * total / max(sum(item["weight"] ** 2 for item in rows), 1e-12)
        return {
            "available": True,
            "probability": clamp(positive / total, 0.0, 1.0),
            "effective_support": round(effective, 3),
            "positive_support": round(positive, 6),
            "negative_support": round(negative, 6),
            "evidence_ids": [str(item["evidence_id"]) for item in sorted(rows, key=lambda row: row["weight"], reverse=True)[:5]],
            "scope": "conditional_local" if any(item["scope"].startswith("coupling:") for item in rows) else "local_product",
        }

    def probability(self, params: Dict[str, Any]) -> float:
        for scheme in self.project.schemes:
            if all(
                key in params
                and (
                    abs(float(params[key]) - float(value)) <= 1e-9
                    if isinstance(value, (int, float)) and isinstance(params[key], (int, float))
                    else params[key] == value
                )
                for key, value in scheme.params.items()
            ):
                return 1.0
        features = self.features(params)
        learned_raw = sum(
            self.weights.get(name, 0.0) * features.get(name, 0.0)
            for name in self.labels
        )
        prior = self.prior_weights()
        structural_raw = sum(
            prior.get(name, 0.0) * features.get(name, 0.0)
            for name in self.labels
        )
        expert_count = sum(
            item.get("status", "active") == "active"
            and item.get("label") in {"feasible", "infeasible"}
            for item in self.expert_evidence
        )
        structural_weight = clamp(0.72 - 0.006 * expert_count, 0.55, 0.72)
        learned_bias = float(self.weights.get("bias", prior.get("bias", 0.0)))
        structural_shape = structural_raw - float(prior.get("bias", 0.0))
        learned_shape = learned_raw - learned_bias
        calibrated_raw = (
            learned_bias
            + structural_weight * structural_shape
            + (1.0 - structural_weight) * learned_shape
        )
        base = self.sigmoid(calibrated_raw)
        frontier_probabilities = [
            float(getattr(item, "feasibility_probability", 1.0))
            for item in self.coupling_system.assess(params)
            if getattr(item, "constraint_mode", "expected_trend")
            in {"minimum_frontier", "maximum_frontier", "hard_band"}
        ]
        # Physical constraints are conjunctive: one clearly violated frontier
        # cannot be averaged away by many ordinary feasible examples.
        frontier_cap = min(frontier_probabilities, default=1.0)
        base = min(base, frontier_cap)
        if not self.conditional_context:
            return base
        local = self.local_evidence_assessment(params)
        if not local["available"]:
            return base
        support = float(local["effective_support"])
        positive = float(local.get("positive_support", 0.0))
        negative = float(local.get("negative_support", 0.0))
        prior_strength = 1.25
        posterior = (prior_strength * base + positive) / max(
            prior_strength + positive + negative,
            1e-12,
        )
        blend = clamp(0.12 + 0.07 * support, 0.12, 0.58)
        return min(
            frontier_cap,
            clamp((1.0 - blend) * base + blend * posterior, 0.0, 1.0),
        )

    def fit(
        self,
        samples: List[Dict[str, Any]],
        epochs: int = 240,
        learning_rate: float = 0.040,
        l2: float = 0.045,
    ) -> Dict[str, Any]:
        prior = self.prior_weights()
        self.weights = dict(prior)
        class_totals = {
            label: sum(
                max(0.05, float(sample.get("weight", 1.0)))
                for sample in samples
                if int(float(sample["label"]) >= 0.5) == label
            )
            for label in (0, 1)
        }
        present_totals = [value for value in class_totals.values() if value > 1e-12]
        balanced_mass = statistics.fmean(present_totals) if present_totals else 1.0
        if samples:
            for _ in range(epochs):
                for sample in samples:
                    params = sample["params"]
                    label = float(sample["label"])
                    raw_weight = max(0.05, float(sample.get("weight", 1.0)))
                    class_label = int(label >= 0.5)
                    class_factor = (
                        balanced_mass / max(class_totals[class_label], 1e-12)
                        if len(present_totals) == 2
                        else 1.0
                    )
                    weight = raw_weight * clamp(class_factor, 0.25, 4.0)
                    features = self.reason_guided_features(
                        self.features(params), label, list(sample.get("reason_codes") or [])
                    )
                    guided_raw = sum(
                        self.weights.get(name, 0.0) * features.get(name, 0.0)
                        for name in self.labels
                    )
                    error = label - self.sigmoid(guided_raw)
                    step = learning_rate * weight
                    for name in self.labels:
                        regularizer = l2 * (self.weights[name] - prior[name])
                        self.weights[name] = clamp(
                            self.weights[name] + step * (error * features.get(name, 0.0) - regularizer),
                            -5.0,
                            5.0,
                        )

        correct = 0
        weighted_correct = 0.0
        total_weight = 0.0
        losses: List[float] = []
        expert_count = 0
        positive_count = 0
        negative_count = 0
        reason_guided_count = 0
        for sample in samples:
            label = float(sample["label"])
            weight = float(sample.get("weight", 1.0))
            probability = clamp(self.probability(sample["params"]), 1e-6, 1.0 - 1e-6)
            predicted = 1.0 if probability >= 0.5 else 0.0
            if predicted == label:
                correct += 1
                weighted_correct += weight
            total_weight += weight
            losses.append(-(label * math.log(probability) + (1.0 - label) * math.log(1.0 - probability)))
            expert_count += 1 if str(sample.get("source") or "").startswith("expert") else 0
            positive_count += 1 if label >= 0.5 else 0
            negative_count += 1 if label < 0.5 else 0
            reason_guided_count += 1 if sample.get("reason_codes") else 0
        has_both_classes = positive_count > 0 and negative_count > 0
        return {
            "model_type": (
                "verified_anchor_conjunctive_frontier_feasibility_v15"
                if self.conditional_context
                else "explainable_logistic_feasibility_v2_boundary"
            ),
            "training_samples": len(samples),
            "expert_samples": expert_count,
            "weak_existing_samples": len(samples) - expert_count,
            "positive_samples": positive_count,
            "negative_samples": negative_count,
            "reason_guided_samples": reason_guided_count,
            "class_balance": {
                "negative_weight": round(class_totals[0], 4),
                "positive_weight": round(class_totals[1], 4),
                "balanced_target_mass": round(balanced_mass, 4),
            },
            "accuracy": round(correct / len(samples), 3) if samples and has_both_classes else None,
            "weighted_accuracy": (
                round(weighted_correct / max(total_weight, 1e-9), 3) if samples and has_both_classes else None
            ),
            "log_loss": round(sum(losses) / len(losses), 4) if losses else None,
            "epochs": epochs if samples else 0,
            "feature_labels": self.labels,
            "learned_boundaries": self.boundary_summaries(),
            "mature_boundary_count": sum(1 for item in self.learned_boundaries if item["mature"]),
        }

    def risk_contributors(self, params: Dict[str, Any], limit: int = 5) -> List[Dict[str, Any]]:
        features = self.features(params)
        items = []
        for name, label in self.labels.items():
            contribution = self.weights.get(name, 0.0) * features.get(name, 0.0)
            if contribution < -0.015:
                items.append(
                    {
                        "feature": name,
                        "label": label,
                        "value": round(features.get(name, 0.0), 3),
                        "contribution": round(contribution, 3),
                    }
                )
        return sorted(items, key=lambda item: item["contribution"])[:limit]
