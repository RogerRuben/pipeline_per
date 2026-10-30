# -*- coding: utf-8 -*-
"""Interactive system-level effectiveness application."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import webbrowser
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from openpyxl import load_workbook as load_export_workbook
from openpyxl.styles import Font, PatternFill


HERE = Path(__file__).resolve().parent
DEMO_DIR = HERE.parent / "demo"
# V11 owns a private copy of the V15 product-learning core. Put this
# directory first even when Python already added it for the entry script;
# otherwise the legacy shared demo directory can silently win the import.
if str(HERE) in sys.path:
    sys.path.remove(str(HERE))
sys.path.insert(0, str(HERE))

from interactive_project_app import (  # noqa: E402
    DEFAULT_UTA_SEGMENTS,
    FEASIBILITY_LABELS,
    MAX_UTA_SEGMENTS,
    MIN_UTA_SEGMENTS,
    OnlineBTModel,
    PREFERENCE_LABELS,
    ProjectApp,
    ProjectDataError,
    binary_response,
    json_response,
    read_body,
)
from preference_models import LPUTAModel, validate_segment_count  # noqa: E402
from coupling_model import CouplingSystem  # noqa: E402
from feasibility_model import ExpertFeasibilityModel  # noqa: E402
from hierarchical_uta import HierarchicalUTAModel  # noqa: E402
from formula_constraints import FormulaConstraintEngine, merge_formula_assessment  # noqa: E402
from regional_context import (  # noqa: E402
    PumpRegionPlanner,
    RegionalCalibrationContext,
    clamp,
    task_from_dict,
)
from system_excel import (  # noqa: E402
    SystemProjectDataset,
    load_system_workbook,
    upgrade_product_workbook,
)


DEFAULT_PORT = 8799
SYSTEM_PROFILE_VERSION = 13
COMPATIBLE_SYSTEM_PROFILE_VERSIONS = {12, SYSTEM_PROFILE_VERSION}
STATE_NAMESPACE_PREFIX = "system_v12_pump_"
REGION_LOCAL_COVERAGE_TARGET = 0.65
REGION_LOCAL_CONFIDENT_TARGET = 0.40


class SystemProjectApp(ProjectApp):
    """V15 product physics plus V13 regional multi-context system UTA."""

    def __init__(self, workbook_path: Path, state_dir: Optional[Path] = None, seed: int = 20260820):
        self.system_project: SystemProjectDataset = load_system_workbook(workbook_path)
        self.product_learning_fingerprint = self.system_project.product.learning_fingerprint
        root = state_dir or HERE / "state"
        self._system_state_root = Path(root).expanduser().resolve()
        super().__init__(workbook_path, root, seed)

    def configure_project(self, project: Any) -> None:
        """Refresh both product and system contracts after an Excel range writeback."""
        if hasattr(self, "system_project"):
            self.system_project = load_system_workbook(project.workbook_path)
            project = self.system_project.product
            self.product_learning_fingerprint = project.learning_fingerprint
            project.learning_fingerprint = f"{STATE_NAMESPACE_PREFIX}{self.system_project.system_learning_fingerprint}"
        super().configure_project(project)
        # The embedded V15 core protects product states with its own prefix.
        # System V13 has an independent contract and must not inherit that
        # namespace, otherwise range writeback can move the active state file.
        system_fingerprint = f"{STATE_NAMESPACE_PREFIX}{self.system_project.system_learning_fingerprint}"
        self.project.learning_fingerprint = system_fingerprint
        self.state_path = self.state_root / f"state_{system_fingerprint}.json"
        self.coupling_system = CouplingSystem.fit(self.project, local_context=True)
        self._runtime_coupling_signature: Optional[Tuple[Any, ...]] = tuple()
        self.prior_feasibility_model = ExpertFeasibilityModel(
            self.project,
            self.coupling_system,
            conditional_context=True,
        )
        self.formula_engine = FormulaConstraintEngine(project, self.system_project.formulas)
        self.region_planner = PumpRegionPlanner(self.system_project)
        for spec in self.system_project.formulas:
            self.reason_options.setdefault(
                f"formula_{spec.target_key}",
                {
                    "title": f"{spec.target_label}与已知公式不一致",
                    "description": f"请检查{spec.target_label}、输入属性、单位或工作工况。",
                },
            )

    @staticmethod
    def _active_feasibility_evidence(state: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            item
            for item in (state or {}).get("feasibility_evidence", [])
            if item.get("status", "active") == "active"
            and item.get("label") in {"feasible", "infeasible"}
        ]

    @classmethod
    def _coupling_evidence_signature(cls, state: Optional[Dict[str, Any]]) -> Tuple[Any, ...]:
        rows: List[Tuple[Any, ...]] = []
        for item in cls._active_feasibility_evidence(state):
            feedback = item.get("coupling_feedback") or {}
            if not feedback.get("target_key"):
                continue
            rows.append(
                (
                    item.get("id"),
                    item.get("status", "active"),
                    item.get("label"),
                    feedback.get("target_key"),
                    tuple(sorted(str(key) for key in feedback.get("source_keys") or [])),
                    feedback.get("issue"),
                    item.get("probe_source"),
                    item.get("probe_target"),
                    json.dumps(item.get("params") or {}, ensure_ascii=False, sort_keys=True),
                )
            )
        return tuple(rows)

    def _ensure_coupling_system(
        self,
        state: Optional[Dict[str, Any]],
        *,
        force: bool = False,
    ) -> CouplingSystem:
        signature = self._coupling_evidence_signature(state)
        if force or signature != getattr(self, "_runtime_coupling_signature", None):
            evidence = self._active_feasibility_evidence(state)
            self.coupling_system = CouplingSystem.fit(
                self.project,
                local_context=True,
                expert_evidence=evidence,
            )
            self._runtime_coupling_signature = signature
        return self.coupling_system

    def save_state(self, state: Dict[str, Any]) -> None:
        """Avoid archiving the transient product-only state built during initialization."""
        if "system_profile_version" in state:
            super().save_state(state)
            return
        previous = os.environ.get("EFFECTIVENESS_EXPERT_PACKAGE")
        os.environ["EFFECTIVENESS_EXPERT_PACKAGE"] = "0"
        try:
            super().save_state(state)
        finally:
            if previous is None:
                os.environ.pop("EFFECTIVENESS_EXPERT_PACKAGE", None)
            else:
                os.environ["EFFECTIVENESS_EXPERT_PACKAGE"] = previous

    def historical_match(self, params: Dict[str, Any]) -> Optional[Any]:
        """Return the manufactured historical product matching this exact parameter vector."""
        for scheme in self.project.schemes:
            matched = True
            for spec in self.attributes:
                if spec.key not in params or spec.key not in scheme.params:
                    matched = False
                    break
                if spec.is_numeric:
                    tolerance = max(10.0 ** (-(int(spec.precision) + 2)), 1e-10)
                    if abs(float(params[spec.key]) - float(scheme.params[spec.key])) > tolerance:
                        matched = False
                        break
                elif params[spec.key] != scheme.params[spec.key]:
                    matched = False
                    break
            if matched:
                return scheme
        return None

    def feasibility_training_samples(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        samples = super().feasibility_training_samples(state)
        existing_count = len(state.get("existing_samples", []))
        for sample in samples[:existing_count]:
            sample["weight"] = 0.30
            sample["source"] = "verified_historical"
        active = [
            item
            for item in state.get("feasibility_evidence", [])
            if item.get("status", "active") == "active"
            and item.get("label") in {"feasible", "infeasible"}
        ]
        for sample, observation in zip(samples[existing_count:], active):
            conditional = bool(observation.get("coupling_feedback")) or any(
                str(code).startswith("coupling_") for code in observation.get("reason_codes", [])
            )
            if conditional:
                sample["weight"] = 0.85 if observation.get("label") == "feasible" else 1.25
                sample["source"] = "expert_conditional"
            elif "single_range" not in (observation.get("reason_codes") or []):
                sample["weight"] = 0.90 if observation.get("label") == "feasible" else 1.20
                sample["source"] = "expert_local"
        return samples

    def feasibility_model_from_state(self, state: Optional[Dict[str, Any]]) -> ExpertFeasibilityModel:
        coupling_system = self._ensure_coupling_system(state)
        weights = (state or {}).get("feasibility_weights") or self.prior_feasibility_model.prior_weights()
        active_evidence = [
            item
            for item in (state or {}).get("feasibility_evidence", [])
            if item.get("status", "active") == "active"
        ]
        frontier = [item for item in active_evidence if item.get("coupling_feedback")]
        return ExpertFeasibilityModel(
            self.project,
            coupling_system,
            weights=weights,
            frontier_evidence=frontier,
            expert_evidence=active_evidence,
            conditional_context=True,
        )

    def retrain_feasibility_model(self, state: Dict[str, Any]) -> ExpertFeasibilityModel:
        active_evidence = [
            item
            for item in state.get("feasibility_evidence", [])
            if item.get("status", "active") == "active"
        ]
        frontier = [item for item in active_evidence if item.get("coupling_feedback")]
        coupling_system = self._ensure_coupling_system(state, force=True)
        model = ExpertFeasibilityModel(
            self.project,
            coupling_system,
            frontier_evidence=frontier,
            expert_evidence=active_evidence,
            conditional_context=True,
        )
        summary = model.fit(self.feasibility_training_samples(state))
        summary["frontier_evidence"] = len(frontier)
        summary["verified_historical_samples"] = len(state.get("existing_samples", []))
        summary["conditional_evidence"] = sum(
            1 for item in active_evidence if item.get("coupling_feedback")
        )
        summary["context_model"] = "partial_identification_frontier_feasibility_v11"
        coupling_summaries = coupling_system.summaries()
        summary["coupling_identifiability"] = [
            {
                "target_key": item.get("target_key"),
                "target_label": item.get("target_label"),
                "score": item.get("identifiability_score"),
                "label": item.get("identifiability_label"),
                "expert_constraints": item.get("expert_constraint_count", 0),
            }
            for item in coupling_summaries
        ]
        state["coupling_model_state"] = coupling_summaries
        state["feasibility_weights"] = dict(model.weights)
        state["feasibility_model"] = summary
        return model

    def record_feasibility_evidence(
        self,
        state: Dict[str, Any],
        interaction_id: str,
        scheme: Dict[str, Any],
        side: str,
        label_value: str,
        reason_codes: List[str],
        reason_text: str,
        coupling_feedback: Optional[Dict[str, Any]] = None,
        engineering_feedback: str = "",
    ) -> Optional[Dict[str, Any]]:
        if self.historical_match(scheme.get("params") or {}) is not None:
            return None
        return super().record_feasibility_evidence(
            state,
            interaction_id,
            scheme,
            side,
            label_value,
            reason_codes,
            reason_text,
            coupling_feedback,
            engineering_feedback,
        )

    def open_review_pair(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        state = self.load_state()
        review_id = str(payload.get("reviewId") or "").strip()
        review = self._review_by_unique_id(state, review_id)
        if review is None or review.get("status") != "open":
            raise ProjectDataError("该复核项不存在或已经处理。")
        task_id = str(review.get("system_task_id") or "")
        if task_id in self.system_project.task_by_id():
            state["active_system_task_id"] = task_id

        evidence_ids = [str(item) for item in review.get("evidence_ids") or []]
        target_id = str(review.get("target_evidence_id") or (evidence_ids[-1] if evidence_ids else ""))
        preference = self.find_evidence(state.get("preference_evidence", []), target_id)
        feasibility = self.find_evidence(state.get("feasibility_evidence", []), target_id)
        interaction_id = str((preference or feasibility or {}).get("interaction_id") or "")
        interaction = next(
            (item for item in state.get("interactions", []) if str(item.get("id")) == interaction_id),
            None,
        )

        if preference:
            scheme_a_id = str(preference.get("scheme_a"))
            scheme_b_id = str(preference.get("scheme_b"))
            params_a = dict(preference.get("params_a") or {})
            params_b = dict(preference.get("params_b") or {})
        elif interaction:
            scheme_a_id = str(interaction.get("scheme_a"))
            scheme_b_id = str(interaction.get("scheme_b"))
            scheme_a = self.find_scheme(state, scheme_a_id)
            scheme_b = self.find_scheme(state, scheme_b_id)
            params_a = dict((scheme_a or {}).get("params") or {})
            params_b = dict((scheme_b or {}).get("params") or {})
        else:
            raise ProjectDataError("该复核项缺少原始A/B参数，无法重新载入。")

        def ensure_scheme(scheme_id: str, params: Dict[str, Any], side: str) -> str:
            if self.find_scheme(state, scheme_id):
                return scheme_id
            restored_id = f"REVIEW-{review_id}-{side}"
            if not self.find_scheme(state, restored_id):
                state.setdefault("known_schemes", []).append(
                    {
                        "id": restored_id,
                        "source": "review_replay",
                        "source_detail": f"从复核项 {review_id} 恢复的原始方案",
                        "params": dict(params),
                    }
                )
            return restored_id

        scheme_a_id = ensure_scheme(scheme_a_id, params_a, "A")
        scheme_b_id = ensure_scheme(scheme_b_id, params_b, "B")
        state["active_pair"] = {"a": scheme_a_id, "b": scheme_b_id}
        state["active_review_id"] = review_id
        state.setdefault("learning_events", []).append(
            {
                "id": f"E-{len(state.get('learning_events', [])) + 1:03d}",
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "type": "review_pair_opened",
                "title": "已载入待复核原始方案",
                "detail": f"复核项 {review_id} 的原A/B已恢复到主比较区；重新提交后只替代目标证据 {target_id}。",
            }
        )
        self.save_state(state)
        return self.summarize(state)

    def _resolve_active_review_after_submit(self, review_id: str) -> Dict[str, Any]:
        state = self.load_state()
        review = next(
            (item for item in state.get("review_evidence", []) if str(item.get("id")) == review_id),
            None,
        )
        if review is None:
            state["active_review_id"] = None
            self.save_state(state)
            return self.summarize(state)
        target_id = str(review.get("target_evidence_id") or "")
        old_preference = self.find_evidence(state.get("preference_evidence", []), target_id)
        old_feasibility = self.find_evidence(state.get("feasibility_evidence", []), target_id)
        interaction = state.get("interactions", [])[-1] if state.get("interactions") else {}
        replacement_id: Optional[str] = None
        if old_preference:
            replacement_id = interaction.get("preference_evidence_id")
        elif old_feasibility:
            for evidence_id in interaction.get("feasibility_evidence_ids") or []:
                candidate = self.find_evidence(state.get("feasibility_evidence", []), str(evidence_id))
                if candidate and candidate.get("scheme_id") == old_feasibility.get("scheme_id"):
                    replacement_id = str(evidence_id)
                    break
        old = old_preference or old_feasibility
        if old is not None:
            old.setdefault("revision_history", []).append(
                {
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "action": "superseded_by_review",
                    "review_id": review_id,
                    "replacement_evidence_id": replacement_id,
                }
            )
            old["status"] = "superseded"
        if replacement_id:
            replacement = self.find_evidence(state.get("preference_evidence", []), replacement_id)
            if replacement is None:
                replacement = self.find_evidence(state.get("feasibility_evidence", []), replacement_id)
            if replacement is not None:
                replacement["supersedes_evidence_id"] = target_id
                replacement["review_id"] = review_id
        review["status"] = "resolved"
        review["resolved_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        review["resolution"] = {
            "action": "rejudged",
            "target_evidence_id": target_id,
            "replacement_evidence_id": replacement_id,
        }
        state["active_review_id"] = None
        if old_preference:
            self.retrain_preference_models(state)
        if old_feasibility:
            self.retrain_feasibility_model(state)
        state.setdefault("learning_events", []).append(
            {
                "id": f"E-{len(state.get('learning_events', [])) + 1:03d}",
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "type": "review_rejudged",
                "title": "复核重新判断已保存",
                "detail": f"复核项 {review_id} 已用新证据 {replacement_id or '无有效新证据'} 替代目标证据 {target_id}；原记录保留。",
            }
        )
        self.save_state(state)
        return self.summarize(state)

    def submit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        state = self.load_state()
        pair = state.get("active_pair") or {}
        scheme_a = self.find_scheme(state, pair.get("a"))
        scheme_b = self.find_scheme(state, pair.get("b"))
        adjusted = dict(payload)
        for side, scheme in (("A", scheme_a), ("B", scheme_b)):
            if scheme and self.historical_match(scheme.get("params") or {}) is not None:
                adjusted[f"feasibility{side}"] = "feasible"
                adjusted[f"reasonCodes{side}"] = []
                adjusted[f"reason{side}"] = ""
                adjusted[f"couplingFeedback{side}"] = None
                adjusted[f"engineeringFeedback{side}"] = ""
        review_id = str(adjusted.get("reviewId") or state.get("active_review_id") or "").strip()
        result = super().submit(adjusted)
        if review_id:
            return self._resolve_active_review_after_submit(review_id)
        return result

    def stability_retest_pair(
        self,
        state: Dict[str, Any],
    ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """Repeat only judgments from the active regional context."""
        temporary = dict(state)
        temporary["preference_evidence"] = list(self.regional_evidence(state))
        result = super().stability_retest_pair(temporary)
        if "active_stability_retest" in temporary:
            state["active_stability_retest"] = temporary["active_stability_retest"]
        if "comparison_quality" in temporary:
            state["comparison_quality"] = temporary["comparison_quality"]
        if result:
            for scheme in result:
                scheme.setdefault("generation_explanation", {}).update(
                    {
                        "regional_context_id": self.training_context_id(state),
                        "regional_context_label": (
                            self.active_region(state).label if self.active_region(state) is not None else "当前任务"
                        ),
                    }
                )
        return result

    def default_system_settings(self) -> Dict[str, float]:
        return {"alpha_prior": 0.70, "alpha_min": 0.60, "alpha_max": 0.90}

    def active_task_id(self, state: Optional[Dict[str, Any]]) -> str:
        task_id = str((state or {}).get("active_system_task_id") or "")
        if task_id in self.system_project.task_by_id():
            return task_id
        return self.system_project.default_task().task_id

    def active_task(self, state: Optional[Dict[str, Any]]) -> Any:
        return self.system_project.task_by_id()[self.active_task_id(state)]

    def regional_contexts(self, state: Optional[Dict[str, Any]]) -> List[RegionalCalibrationContext]:
        return [
            RegionalCalibrationContext.from_dict(dict(item))
            for item in (state or {}).get("regional_contexts", [])
        ]

    def _initialize_regional_contexts(self, state: Dict[str, Any]) -> None:
        """Build one task-priority sequence plus representative full-domain contexts."""
        selected_task = self.active_task(state)
        contexts = list(self.region_planner.discover(selected_task))
        for task in self.system_project.tasks:
            if task.task_id == selected_task.task_id:
                continue
            candidates = self.region_planner.discover(task)
            exact = next((item for item in candidates if item.selected_task_region), None)
            if exact is None:
                continue
            nearest = min(
                (self.region_planner.context_distance(exact.calibration_task, item.calibration_task) for item in contexts),
                default=float("inf"),
            )
            if nearest <= 0.35:
                continue
            exact = RegionalCalibrationContext(
                region_id=f"TASK-{task.task_id}",
                label=f"真实任务校准·{task.task_name}",
                sequence_index=len(contexts),
                primary_system_key=exact.primary_system_key,
                primary_system_label=exact.primary_system_label,
                primary_unit=exact.primary_unit,
                lower=exact.lower,
                center=exact.center,
                upper=exact.upper,
                selected_task_region=False,
                calibration_task=task,
                weight_scaling_exponent=exact.weight_scaling_exponent,
            )
            contexts.append(exact)
        state["regional_context_version"] = 1
        state["regional_origin_task_id"] = selected_task.task_id
        state["regional_contexts"] = [item.to_dict() for item in contexts]
        state["active_region_id"] = contexts[0].region_id if contexts else ""
        state["regional_models"] = {}
        state["region_best_ids"] = {}
        state["region_transition_history"] = []
        state["pending_region_notice"] = None

    def active_region(self, state: Optional[Dict[str, Any]]) -> Optional[RegionalCalibrationContext]:
        contexts = self.regional_contexts(state)
        if not contexts:
            return None
        region_id = str((state or {}).get("active_region_id") or "")
        return next((item for item in contexts if item.region_id == region_id), contexts[0])

    def training_context_id(self, state: Optional[Dict[str, Any]]) -> str:
        region = self.active_region(state)
        return region.region_id if region is not None else self.active_task_id(state)

    def training_task(self, state: Optional[Dict[str, Any]]) -> Any:
        region = self.active_region(state)
        return region.calibration_task if region is not None else self.active_task(state)

    def regional_evidence(
        self,
        state: Dict[str, Any],
        region_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        region_id = region_id or self.training_context_id(state)
        return [
            item
            for item in self.shared_preference_evidence(state)
            if str(item.get("regional_context_id") or item.get("system_task_id") or "") == region_id
        ]

    def contextual_summary_for_task(
        self,
        state: Optional[Dict[str, Any]],
        task: Any,
    ) -> Dict[str, Any]:
        """Shrink nearby regional direct-attribute parameters toward the shared model."""
        state = state or {}
        shared = dict(state.get("system_shared_model") or state.get("system_model") or {})
        shared_direct = [float(value) for value in shared.get("direct_increments") or []]
        if not shared_direct:
            return shared
        candidates: List[Tuple[float, float, Dict[str, Any], RegionalCalibrationContext]] = []
        regional_models = state.get("regional_models") or {}
        for context in self.regional_contexts(state):
            summary = dict(regional_models.get(context.region_id) or {})
            count = int(summary.get("evidence_pairs", 0))
            direct = summary.get("direct_increments") or []
            if count < 2 or len(direct) != len(shared_direct):
                continue
            distance = self.region_planner.context_distance(task, context.calibration_task)
            kernel = math.exp(-0.5 * (distance / 0.75) ** 2)
            support = min(1.0, count / 8.0)
            candidates.append((distance, kernel * support, summary, context))
        if not candidates:
            shared["contextual_transfer"] = {
                "mode": "shared_prior_only",
                "confidence": 0.35,
                "nearest_distance": None,
                "source_regions": [],
            }
            return shared
        candidates.sort(key=lambda item: item[0])
        selected = candidates[:2]
        total_weight = sum(item[1] for item in selected)
        if total_weight <= 1e-12:
            return shared
        regional_direct = [0.0] * len(shared_direct)
        source_regions: List[Dict[str, Any]] = []
        effective_support = 0.0
        for distance, weight, summary, context in selected:
            normalized = weight / total_weight
            effective_support += normalized * int(summary.get("evidence_pairs", 0))
            for index, value in enumerate(summary.get("direct_increments") or []):
                regional_direct[index] += normalized * float(value)
            source_regions.append(
                {
                    "region_id": context.region_id,
                    "label": context.label,
                    "distance": round(distance, 4),
                    "weight": round(normalized, 4),
                    "evidence_pairs": int(summary.get("evidence_pairs", 0)),
                }
            )
        blend = min(0.70, effective_support / (effective_support + 8.0))
        output = dict(shared)
        output["direct_increments"] = [
            (1.0 - blend) * shared_value + blend * regional_value
            for shared_value, regional_value in zip(shared_direct, regional_direct)
        ]
        nearest_distance = selected[0][0]
        coverage_factor = min(1.0, effective_support / 8.0)
        confidence = min(0.96, (1.0 / (1.0 + nearest_distance)) * (0.60 + 0.36 * coverage_factor))
        output["contextual_transfer"] = {
            "mode": "regional_interpolation" if len(selected) > 1 else "nearest_region_shrinkage",
            "confidence": round(confidence, 3),
            "nearest_distance": round(nearest_distance, 4),
            "regional_blend": round(blend, 4),
            "source_regions": source_regions,
        }
        return output

    def regional_progress(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        output: List[Dict[str, Any]] = []
        must_reviews = [
            item
            for item in state.get("review_evidence", [])
            if item.get("status") == "open" and item.get("tier", "must_review") == "must_review"
        ]
        for context in self.regional_contexts(state):
            evidence = self.regional_evidence(state, context.region_id)
            model = self.system_model_from_state(state, task=context.calibration_task)
            coverage = model.learning_coverage(evidence)
            minimum_pairs = max(6, 2 * max(len(model.direct_space.attributes), 1))
            region_review_count = sum(
                1
                for item in must_reviews
                if str(item.get("regional_context_id") or item.get("system_task_id") or "") == context.region_id
            )
            reasons: List[str] = []
            if len(evidence) < minimum_pairs:
                reasons.append(f"有效比较{len(evidence)}/{minimum_pairs}")
            if float(coverage.get("local_coverage_ratio", 0.0)) < REGION_LOCAL_COVERAGE_TARGET:
                reasons.append(
                    f"要求附近覆盖{float(coverage.get('local_coverage_ratio', 0.0)):.0%}/"
                    f"{REGION_LOCAL_COVERAGE_TARGET:.0%}"
                )
            if float(coverage.get("local_confident_ratio", 0.0)) < REGION_LOCAL_CONFIDENT_TARGET:
                reasons.append(
                    f"可靠覆盖{float(coverage.get('local_confident_ratio', 0.0)):.0%}/"
                    f"{REGION_LOCAL_CONFIDENT_TARGET:.0%}"
                )
            if region_review_count:
                reasons.append(f"必须复核{region_review_count}项")
            output.append(
                {
                    **context.to_dict(),
                    "evidence_pairs": len(evidence),
                    "minimum_pairs": minimum_pairs,
                    "local_coverage_ratio": float(coverage.get("local_coverage_ratio", 0.0)),
                    "local_confident_ratio": float(coverage.get("local_confident_ratio", 0.0)),
                    "complete": not reasons,
                    "reasons": reasons,
                    "active": context.region_id == self.training_context_id(state),
                }
            )
        return output

    def advance_training_region_if_ready(self, state: Dict[str, Any]) -> bool:
        progress = self.regional_progress(state)
        active = next((item for item in progress if item["active"]), None)
        if active is None or not active["complete"]:
            return False
        next_region = next((item for item in progress if not item["complete"]), None)
        if next_region is None or next_region["region_id"] == active["region_id"]:
            return False
        state["active_region_id"] = next_region["region_id"]
        notice = {
            "from_region_id": active["region_id"],
            "from_label": active["label"],
            "to_region_id": next_region["region_id"],
            "to_label": next_region["label"],
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "message": (
                f"{active['label']}已经学习完成。接下来补充{next_region['label']}，"
                "A/B两侧都会切换到该规格区域，不再保留上一规格的优胜方案。"
            ),
        }
        state["pending_region_notice"] = notice
        state.setdefault("region_transition_history", []).append(dict(notice))
        state.setdefault("learning_events", []).append(
            {
                "id": f"E-{len(state.get('learning_events', [])) + 1:03d}",
                "created_at": notice["created_at"],
                "type": "regional_learning_context_changed",
                "title": "全域学习已切换规格区域",
                "detail": notice["message"],
            }
        )
        return True

    def settings_for_task(self, state: Optional[Dict[str, Any]], task_id: Optional[str] = None) -> Dict[str, float]:
        state = state or {}
        task_id = task_id or self.active_task_id(state)
        stored = state.get("system_shared_settings") or (
            (state.get("system_settings_by_task") or {}).get(task_id) or {}
        )
        defaults = self.default_system_settings()
        return {key: float(stored.get(key, value)) for key, value in defaults.items()}

    def task_requirement_distance(
        self,
        params: Dict[str, Any],
        state: Optional[Dict[str, Any]] = None,
        model: Optional[HierarchicalUTAModel] = None,
        task: Optional[Any] = None,
    ) -> float:
        """Robust dimensionless distance from one coherent scheme to the active task anchor."""
        task = task or self.training_task(state)
        model = model or self.system_model_from_state(state, task=task)
        mapped = model.direct_space.mapped_values(params)
        distances: List[Tuple[float, float]] = []
        for requirement in task.requirements:
            if requirement.system_key not in mapped:
                continue
            value = float(mapped[requirement.system_key])
            definitions = model.direct_space.branch_definition(requirement)
            kind = requirement.requirement_type
            distance: Optional[float] = None
            if kind == "target" and requirement.target_value is not None:
                side = "left" if value < requirement.target_value else "right"
                definition = definitions[side]
                distance = abs(value - float(requirement.target_value)) / max(float(definition["scale"]), 1e-12)
            elif kind == "at_least" and requirement.minimum is not None:
                side = "left" if value < requirement.minimum else "right"
                distance = abs(value - float(requirement.minimum)) / max(float(definitions[side]["scale"]), 1e-12)
            elif kind == "at_most" and requirement.maximum is not None:
                side = "left" if value < requirement.maximum else "right"
                distance = abs(value - float(requirement.maximum)) / max(float(definitions[side]["scale"]), 1e-12)
            elif (
                kind == "within_range"
                and requirement.minimum is not None
                and requirement.maximum is not None
            ):
                if value < requirement.minimum:
                    distance = (float(requirement.minimum) - value) / max(float(definitions["left"]["scale"]), 1e-12)
                elif value > requirement.maximum:
                    distance = (value - float(requirement.maximum)) / max(float(definitions["right"]["scale"]), 1e-12)
                else:
                    distance = 0.0
            if distance is not None:
                distances.append((min(float(distance), 8.0), max(0.05, float(requirement.weight))))
        if not distances:
            return 0.0
        total_weight = sum(weight for _distance, weight in distances)
        return sum(distance * weight for distance, weight in distances) / max(total_weight, 1e-12)

    def _trusted_anchor_schemes(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        by_id = {item["id"]: item for item in state.get("existing_samples", [])}
        feasible_ids = {
            str(item.get("scheme_id"))
            for item in state.get("feasibility_evidence", [])
            if item.get("status", "active") == "active" and item.get("label") == "feasible"
        }
        for item in state.get("known_schemes", []):
            if item.get("id") in feasible_ids:
                by_id[item["id"]] = item
        return list(by_id.values())

    def initial_task_anchor(
        self,
        state: Dict[str, Any],
        model: Optional[HierarchicalUTAModel] = None,
    ) -> Dict[str, Any]:
        """Select one real historical scheme near the task without breaking its couplings."""
        model = model or self.system_model_from_state(state, task=self.training_task(state))
        candidates = list(state.get("existing_samples", []))
        if not candidates:
            raise ProjectDataError("Excel中没有可作为任务启动锚点的历史方案。")
        ranked: List[Tuple[float, Dict[str, Any]]] = []
        for item in candidates:
            evaluation = self.evaluate(item["params"], state)
            hard_penalty = 100.0 if evaluation.get("hard_violations") else 0.0
            feasibility_penalty = 1.5 * (1.0 - float(evaluation.get("learned_feasibility_probability", 0.5)))
            distance = self.task_requirement_distance(item["params"], state, model)
            ranked.append((hard_penalty + distance + feasibility_penalty, item))
        return min(ranked, key=lambda row: (row[0], str(row[1].get("id"))))[1]

    def scheme_in_region(
        self,
        state: Dict[str, Any],
        scheme: Optional[Dict[str, Any]],
        region: Optional[RegionalCalibrationContext] = None,
    ) -> bool:
        """Return whether a complete product scheme belongs to one calibration band."""
        if not scheme or not scheme.get("params"):
            return False
        region = region or self.active_region(state)
        if region is None:
            return True
        value = self.region_planner.mapped_values(scheme["params"]).get(region.primary_system_key)
        return bool(
            value is not None
            and float(region.lower) - 1e-9 <= float(value) <= float(region.upper) + 1e-9
        )

    def _generated_region_anchor(
        self,
        state: Dict[str, Any],
        seed: Dict[str, Any],
        region: RegionalCalibrationContext,
    ) -> Dict[str, Any]:
        """Create a coupled, formula-completed anchor when a band has no real sample."""
        candidates: List[Dict[str, Any]] = []
        for offset in range(10):
            candidate = self.generate_candidate(
                state,
                seed,
                mode="global_space_fill",
                seed_offset=730 + offset,
                commit=False,
                normalized_vector={},
            )
            if self.scheme_in_region(state, candidate, region):
                candidates.append(candidate)
        if not candidates:
            raise ProjectDataError(
                f"无法在{region.label}内建立完整方案，请检查主规格映射和生成范围。"
            )
        selected = min(
            candidates,
            key=lambda item: (
                abs(
                    float(
                        self.region_planner.mapped_values(item["params"])[region.primary_system_key]
                    )
                    - float(region.center)
                ),
                len(self.evaluate(item["params"], state).get("hard_violations") or []),
            ),
        )
        anchor_id = f"REGION-ANCHOR-{region.region_id}"
        selected["id"] = anchor_id
        selected["source"] = "regional_calibration_anchor"
        selected["source_detail"] = (
            f"{region.label}没有历史实物落点，由最近历史方案按耦合与公式生成的待专家确认区域锚点"
        )
        selected.setdefault("generation_explanation", {}).update(
            {
                "purpose": "建立新规格区域的A方案，使A/B两侧同时进入该区域",
                "detail": "该方案不是历史实物，专家本轮仍需判断其物理可行性。",
                "regional_anchor": True,
                "regional_context_id": region.region_id,
                "regional_context_label": region.label,
            }
        )
        state["generated_index"] = int(state.get("generated_index", 1)) + 1
        if not self.find_scheme(state, anchor_id):
            state.setdefault("known_schemes", []).append(selected)
        return self.find_scheme(state, anchor_id) or selected

    def regional_anchor(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Choose a local winner or a trustworthy scheme inside the active band."""
        region = self.active_region(state)
        if region is None:
            default = self.find_scheme(state, state.get("current_best_id"))
            return default or self.initial_task_anchor(state)
        local_best_id = str((state.get("region_best_ids") or {}).get(region.region_id) or "")
        local_best = self.find_scheme(state, local_best_id)
        if self.scheme_in_region(state, local_best, region):
            return local_best
        trusted = [
            item for item in self._trusted_anchor_schemes(state)
            if self.scheme_in_region(state, item, region)
        ]
        if trusted:
            model = self.system_model_from_state(state, task=region.calibration_task)
            selected = min(
                trusted,
                key=lambda item: (
                    self.task_requirement_distance(
                        item["params"], state, model, region.calibration_task
                    ),
                    str(item["id"]),
                ),
            )
            state.setdefault("region_best_ids", {})[region.region_id] = selected["id"]
            return selected
        trusted_all = self._trusted_anchor_schemes(state)
        if not trusted_all:
            raise ProjectDataError("Excel中没有可用于建立区域锚点的历史方案。")
        seed = min(
            trusted_all,
            key=lambda item: abs(
                float(
                    self.region_planner.mapped_values(item["params"])[region.primary_system_key]
                )
                - float(region.center)
            ),
        )
        return self._generated_region_anchor(state, seed, region)

    def active_learning_coverage(
        self,
        state: Dict[str, Any],
        model: Optional[HierarchicalUTAModel] = None,
    ) -> Dict[str, Any]:
        model = model or self.system_model_from_state(state, task=self.training_task(state))
        return model.learning_coverage(self.regional_evidence(state))

    @staticmethod
    def active_learning_phase(coverage: Dict[str, Any], direct_attribute_count: int) -> str:
        local_pairs_needed = max(6, 2 * max(direct_attribute_count, 1))
        local_ready = (
            float(coverage.get("local_coverage_ratio", 0.0)) >= REGION_LOCAL_COVERAGE_TARGET
            and float(coverage.get("local_confident_ratio", 0.0)) >= 0.50
            and int(coverage.get("local_effective_pairs", 0)) >= local_pairs_needed
        )
        full_ready = (
            float(coverage.get("full_coverage_ratio", 0.0)) >= 0.85
            and float(coverage.get("full_confident_ratio", 0.0)) >= 0.65
        )
        if not local_ready:
            return "requirement_neighborhood"
        if not full_ready:
            return "global_completion"
        return "stability_confirmation"

    def _slot_activation(
        self,
        model: HierarchicalUTAModel,
        params: Dict[str, Any],
        slot_id: str,
        state: Optional[Dict[str, Any]] = None,
    ) -> float:
        parts = slot_id.split(":")
        if parts[0] == "direct" and len(parts) == 4:
            _channel, key, side, raw_segment = parts
            requirement = next(
                (item for item in self.training_task(state).requirements if item.system_key == key),
                None,
            )
            if requirement is None:
                return 0.0
            mapped = model.direct_space.mapped_values(params)
            vector = model.direct_space.attribute_vector(requirement, mapped[key])
            part = model.direct_space.branch_slice(key, side)
            return float(vector[part][int(raw_segment)])
        if parts[0] == "product" and len(parts) == 3:
            _channel, key, raw_segment = parts
            vector = model.space.vector(params)
            part = next(part for spec, part in model.space.slices() if spec.key == key)
            return float(vector[part][int(raw_segment)])
        return 0.0

    def comparison_base(self, state: Dict[str, Any], default: Dict[str, Any]) -> Dict[str, Any]:
        switched = self.advance_training_region_if_ready(state)
        task = self.training_task(state)
        model = self.system_model_from_state(state, task=task)
        coverage = self.active_learning_coverage(state, model)
        phase = self.active_learning_phase(coverage, len(model.direct_space.attributes))
        anchor = self.regional_anchor(state)
        if switched:
            self._last_base_reason = {
                "mode": "regional_context_reset",
                "detail": "区域已经切换，A与B同时迁入新的规格区域。",
                "regional_context_id": self.training_context_id(state),
                "task_distance": round(
                    self.task_requirement_distance(anchor["params"], state, model, task), 3
                ),
            }
            return anchor
        elif phase == "requirement_neighborhood":
            default_in_region = self.scheme_in_region(state, default)
            default_distance = self.task_requirement_distance(default["params"], state, model, task)
            anchor_distance = self.task_requirement_distance(anchor["params"], state, model, task)
            selected = (
                default
                if default_in_region and default_distance <= anchor_distance + 0.15
                else anchor
            )
            self._last_base_reason = {
                "mode": "regional_requirement_anchor",
                "detail": (
                    "区域已切换，A与B同时迁入新的规格区域。"
                    if switched
                    else "选择接近当前规格校准要求的完整可信方案作为A，保留历史方案内部耦合。"
                ),
                "regional_context_id": self.training_context_id(state),
                "task_distance": round(self.task_requirement_distance(selected["params"], state, model, task), 3),
            }
            return selected
        if phase == "global_completion":
            targets = list(coverage.get("next_targets") or [])
            target = next((item for item in targets if not item.get("confident")), None)
            if target:
                trusted = [
                    item for item in self._trusted_anchor_schemes(state)
                    if self.scheme_in_region(state, item)
                ] or [anchor]
                selected = min(
                    trusted,
                    key=lambda item: (
                        abs(self._slot_activation(model, item["params"], target["slot_id"], state) - 0.5),
                        str(item["id"]),
                    ),
                )
                self._last_base_reason = {
                    "mode": "undercovered_segment_anchor",
                    "detail": f"选择靠近{target['attribute_label']}第{target['segment']}段的可信方案作为A，用于补齐全域曲线。",
                    "target_slot": target["slot_id"],
                }
                return selected
        self._last_base_reason = {
            "mode": "regional_best_anchor",
            "detail": "当前进入稳定性确认阶段，继续围绕本规格区域内已确认较优方案进行少量复核探索。",
        }
        return default if self.scheme_in_region(state, default) else anchor

    def preference_evidence_for_context(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        task_id = self.training_context_id(state)
        return [
            item
            for item in OnlineBTModel.active_evidence(state.get("preference_evidence", []))
            if item.get("learning_context") == "system_overall_effectiveness"
            and str(item.get("regional_context_id") or item.get("system_task_id") or "") == task_id
        ]

    def shared_preference_evidence(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            item
            for item in OnlineBTModel.active_evidence(state.get("preference_evidence", []))
            if item.get("learning_context") == "system_overall_effectiveness"
        ]

    @staticmethod
    def _task_signature(task: Any) -> Tuple[Tuple[str, str], ...]:
        return tuple(sorted((item.system_key, item.requirement_type) for item in task.requirements))

    @staticmethod
    def _requirement_value_signature(task: Any) -> str:
        requirements = task.get("requirements") if isinstance(task, dict) else [
            asdict(item) for item in task.requirements
        ]
        return json.dumps(requirements or [], ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def task_transfer_profile(
        self,
        state: Dict[str, Any],
        task_id: Optional[str] = None,
        model_summary: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        task_id = task_id or self.active_task_id(state)
        task = self.system_project.task_by_id()[task_id]
        evidence = self.shared_preference_evidence(state)
        counts: Dict[str, int] = {}
        for item in evidence:
            source = str(item.get("regional_context_id") or item.get("system_task_id") or "legacy_unknown")
            counts[source] = counts.get(source, 0) + 1
        current_requirement_signature = self._requirement_value_signature(task)
        exact_pairs = 0
        for item in evidence:
            snapshot = item.get("system_task_snapshot")
            if snapshot and self._requirement_value_signature(snapshot) == current_requirement_signature:
                exact_pairs += 1
        summary = model_summary or state.get("system_shared_model") or state.get("system_model") or {}
        accuracy = float(
            summary.get("conservative_validation_accuracy")
            or summary.get("test_accuracy")
            or summary.get("training_accuracy")
            or 0.0
        )
        contextual = self.contextual_summary_for_task(state, task).get("contextual_transfer") or {}
        nearest_distance = contextual.get("nearest_distance")
        regional_confidence = float(contextual.get("confidence", 0.35))
        if exact_pairs:
            mode = "native_validated"
            label = "本任务已有直接判断验证"
            confidence = max(
                regional_confidence,
                0.72 + 0.18 * accuracy + 0.10 * min(1.0, exact_pairs / 12.0),
            )
        elif not evidence:
            mode = "prior_only"
            label = "仅使用工程先验，尚无可迁移判断"
            confidence = 0.35
        elif nearest_distance is not None and float(nearest_distance) <= 0.22:
            mode = "nearby_region_interpolation"
            label = "位于已学习规格区域，可直接插值评价"
            confidence = regional_confidence
        elif nearest_distance is not None and float(nearest_distance) <= 0.65:
            mode = "regional_interpolation"
            label = "位于相邻规格区域，采用分层插值"
            confidence = min(regional_confidence, 0.82)
        else:
            mode = "regional_extrapolation"
            label = "超出主要证据区域，当前属于低可信外推"
            confidence = min(regional_confidence, 0.58)
        return {
            "mode": mode,
            "label": label,
            "confidence": round(min(0.98, confidence), 3),
            "source_task_ids": sorted(counts),
            "shared_evidence_pairs": len(evidence),
            "task_specific_pairs": exact_pairs,
            "nearest_region_distance": nearest_distance,
            "source_regions": contextual.get("source_regions") or [],
            "requires_retraining": False,
            "recommended_action": (
                "无需重新训练；可直接评分。"
                if mode in {"native_validated", "nearby_region_interpolation", "regional_interpolation"}
                else "无需清空原模型；当前结果应按外推可信度谨慎使用。"
            ),
        }

    def sync_preference_reviews(self, state: Dict[str, Any]) -> None:
        """Run conflict diagnostics inside one system task, never across tasks."""
        task_id = self.training_context_id(state)
        all_reviews = state.setdefault("review_evidence", [])
        task_reviews = [
            item
            for item in all_reviews
            if str(item.get("regional_context_id") or item.get("system_task_id") or "") == task_id
        ]
        other_reviews = [
            item
            for item in all_reviews
            if str(item.get("regional_context_id") or item.get("system_task_id") or "") != task_id
        ]
        temporary = dict(state)
        task_evidence = self.preference_evidence_for_context(state)
        task_evidence_ids = {str(item.get("id")) for item in task_evidence}
        temporary["preference_evidence"] = task_evidence
        temporary["review_evidence"] = task_reviews
        uta_summary = dict(state.get("uta_model") or {})
        uta_summary["tolerances"] = [
            item
            for item in uta_summary.get("tolerances", [])
            if str(item.get("evidence_id")) in task_evidence_ids
        ]
        m3 = dict(uta_summary.get("m3") or {})
        m3["conflicting_evidence_ids"] = [
            evidence_id
            for evidence_id in m3.get("conflicting_evidence_ids", [])
            if str(evidence_id) in task_evidence_ids
        ]
        uta_summary["m3"] = m3
        temporary["uta_model"] = uta_summary
        super().sync_preference_reviews(temporary)
        for item in temporary["review_evidence"]:
            item["system_task_id"] = task_id
            item["regional_context_id"] = task_id
        state["review_evidence"] = other_reviews + temporary["review_evidence"]
        self._normalize_review_ids(state)

    @staticmethod
    def _review_by_unique_id(state: Dict[str, Any], review_id: str) -> Optional[Dict[str, Any]]:
        matches = [
            item
            for item in state.get("review_evidence", [])
            if str(item.get("id") or "") == str(review_id or "")
        ]
        if len(matches) > 1:
            open_matches = [item for item in matches if item.get("status") == "open"]
            if len(open_matches) == 1:
                return open_matches[0]
            raise ProjectDataError("复核编号存在歧义，请刷新页面后再操作。")
        return matches[0] if matches else None

    @staticmethod
    def _normalize_review_ids(state: Dict[str, Any]) -> bool:
        """Make review IDs globally unique across all tasks and regions."""
        reviews = state.setdefault("review_evidence", [])
        active_id = str(state.get("active_review_id") or "")
        active_target: Optional[Dict[str, Any]] = None
        if active_id:
            active_matches = [item for item in reviews if str(item.get("id") or "") == active_id]
            open_matches = [item for item in active_matches if item.get("status") == "open"]
            if len(open_matches) == 1:
                active_target = open_matches[0]
            elif len(active_matches) == 1:
                active_target = active_matches[0]

        used = set()
        maximum = 0
        for item in reviews:
            review_id = str(item.get("id") or "")
            if review_id.startswith("R-") and review_id[2:].isdigit():
                maximum = max(maximum, int(review_id[2:]))
        next_index = maximum + 1
        changed = False
        for item in reviews:
            review_id = str(item.get("id") or "")
            if review_id and review_id not in used:
                used.add(review_id)
                continue
            while f"R-{next_index:03d}" in used:
                next_index += 1
            replacement = f"R-{next_index:03d}"
            next_index += 1
            if review_id:
                item.setdefault("original_review_id", review_id)
            item["id"] = replacement
            item["id_migrated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            used.add(replacement)
            changed = True
        if active_target is not None and state.get("active_review_id") != active_target.get("id"):
            state["active_review_id"] = active_target.get("id")
            changed = True
        return changed

    def _causal_focus_metadata(
        self,
        scheme_a: Dict[str, Any],
        scheme_b: Dict[str, Any],
    ) -> Dict[str, Any]:
        explanation = dict(scheme_b.get("generation_explanation") or {})
        independent = {spec.key: spec for spec in self.independent_query_specs()}
        explicit_keys = explanation.get("independent_changed_keys")
        if isinstance(explicit_keys, list):
            active_keys = [str(key) for key in explicit_keys if str(key) in independent]
        else:
            active_keys = []
            params_a = scheme_a.get("params") or {}
            params_b = scheme_b.get("params") or {}
            for key, spec in independent.items():
                if key not in params_a or key not in params_b:
                    continue
                before = self.normalize_coordinate(params_a[key], spec)
                after = self.normalize_coordinate(params_b[key], spec)
                if abs(after - before) >= 0.018:
                    active_keys.append(key)
        active_keys = list(dict.fromkeys(active_keys))

        params_a = scheme_a.get("params") or {}
        params_b = scheme_b.get("params") or {}
        changed_keys: List[str] = []
        for spec in self.attributes:
            if not spec.is_numeric or spec.key not in params_a or spec.key not in params_b:
                continue
            before = self.normalize_coordinate(params_a[spec.key], spec)
            after = self.normalize_coordinate(params_b[spec.key], spec)
            if abs(after - before) >= 1e-9:
                changed_keys.append(spec.key)
        formula_targets = {str(item.target_key) for item in self.system_project.formulas}
        coupling_targets = {str(key) for key in self.coupling_system.target_keys()}
        passive_keys = [key for key in changed_keys if key not in active_keys]
        return {
            "causal_focus_version": 1,
            "active_driver_keys": active_keys,
            "passive_changed_keys": passive_keys,
            "coupling_derived_keys": [key for key in passive_keys if key in coupling_targets],
            "formula_derived_keys": [key for key in passive_keys if key in formula_targets],
            "controlled_variable_comparison": 1 <= len(active_keys) <= 2,
        }

    def _enrich_causal_focus_evidence(self, state: Dict[str, Any]) -> bool:
        changed = False
        for evidence in state.get("preference_evidence", []):
            if int(evidence.get("causal_focus_version") or 0) >= 1:
                continue
            scheme_a = self.find_scheme(state, str(evidence.get("scheme_a") or "")) or {
                "params": dict(evidence.get("params_a") or {})
            }
            scheme_b = self.find_scheme(state, str(evidence.get("scheme_b") or "")) or {
                "params": dict(evidence.get("params_b") or {})
            }
            evidence.update(self._causal_focus_metadata(scheme_a, scheme_b))
            changed = True
        return changed

    def system_model_from_state(
        self,
        state: Optional[Dict[str, Any]],
        task: Optional[Any] = None,
    ) -> HierarchicalUTAModel:
        state = state or {}
        settings = self.settings_for_task(state)
        task = task or self.active_task(state)
        model = HierarchicalUTAModel(
            self.system_project,
            task,
            alpha_prior=settings["alpha_prior"],
            alpha_min=settings["alpha_min"],
            alpha_max=settings["alpha_max"],
            segments=int(state.get("uta_segments", DEFAULT_UTA_SEGMENTS)),
        )
        summary = self.contextual_summary_for_task(state, task)
        increments = summary.get("increments") or []
        direct_increments = summary.get("direct_increments") or []
        if (
            len(increments) == model.space.variable_count
            and len(direct_increments) == model.direct_space.variable_count
        ):
            np = __import__("numpy")
            model.increments = np.asarray(increments, dtype=float)
            model.direct_increments = np.asarray(direct_increments, dtype=float)
            stored_limits = summary.get("direct_side_limits") or {}
            for key, sides in stored_limits.items():
                if key in model.direct_space.side_limits:
                    model.direct_space.side_limits[key].update(
                        {side: float(value) for side, value in sides.items() if side in {"left", "right"}}
                    )
            model.alpha = float(model.direct_increments.sum()) / model.PRODUCT_GROUP_SPAN
            model.summary_data = dict(summary)
            references = summary.get("product_reference_params") or {}
            for key in model.space.reference_params:
                if key in references:
                    model.space.reference_params[key] = float(references[key])
            for key, stats in (summary.get("direct_history_stats") or {}).items():
                if key in model.direct_scorer.history_by_system:
                    restored_history = [
                        float(stats["minimum"]), float(stats["median"]), float(stats["maximum"])
                    ]
                    model.direct_scorer.history_by_system[key] = restored_history
                    if key in model.direct_space.history_by_system:
                        model.direct_space.history_by_system[key] = list(restored_history)
        return model

    def refresh_task_model_views(self, state: Dict[str, Any], summary: Dict[str, Any]) -> None:
        state["system_models_by_task"] = {}
        for task in self.system_project.tasks:
            view = self.contextual_summary_for_task(state, task)
            view["active_scoring_task_id"] = task.task_id
            view["transfer"] = self.task_transfer_profile(state, task.task_id, summary)
            state["system_models_by_task"][task.task_id] = view

    def retrain_preference_models(self, state: Dict[str, Any]) -> Tuple[OnlineBTModel, HierarchicalUTAModel]:
        active_evidence = self.preference_evidence_for_context(state)
        shared_evidence = self.shared_preference_evidence(state)

        # The proxy BT/UTA remains available only to help V11 candidate search.
        proxy_bt = OnlineBTModel(self.project, requirement_profile=None)
        bt_summary = proxy_bt.fit(active_evidence)
        proxy_uta = LPUTAModel(
            self.project,
            segments=state.get("uta_segments", DEFAULT_UTA_SEGMENTS),
            requirement_profile=None,
        )
        proxy_summary = proxy_uta.fit(active_evidence)
        bt_summary["learning_context"] = "system_candidate_search_proxy"
        bt_summary["system_task_id"] = self.training_context_id(state)
        state["bt_weights"] = list(bt_summary.get("weights") or [])
        state["bt_model"] = bt_summary
        state["uta_increments"] = list(proxy_summary.get("increments") or [])

        settings = self.settings_for_task(state)
        model = HierarchicalUTAModel(
            self.system_project,
            self.training_task(state),
            alpha_prior=settings["alpha_prior"],
            alpha_min=settings["alpha_min"],
            alpha_max=settings["alpha_max"],
            segments=int(state.get("uta_segments", DEFAULT_UTA_SEGMENTS)),
        )
        summary = model.fit(shared_evidence)
        summary["base_solver"] = summary.get("model_type")
        summary["model_type"] = "regional_contextual_requirement_anchored_joint_lp_uta_v13"
        summary["model_name"] = "全域多工况分层收缩主动学习稳健联合LP-UTA V13"
        summary["learning_context"] = "system_overall_effectiveness"
        summary["system_contract_version"] = self.system_project.contract_version
        state["system_shared_model"] = summary
        state["system_model"] = dict(summary)
        regional_models: Dict[str, Dict[str, Any]] = {}
        for context in self.regional_contexts(state):
            regional_model = HierarchicalUTAModel(
                self.system_project,
                context.calibration_task,
                alpha_prior=settings["alpha_prior"],
                alpha_min=settings["alpha_min"],
                alpha_max=settings["alpha_max"],
                segments=int(state.get("uta_segments", DEFAULT_UTA_SEGMENTS)),
            )
            regional_summary = regional_model.fit(self.regional_evidence(state, context.region_id))
            regional_summary["base_solver"] = regional_summary.get("model_type")
            regional_summary["model_type"] = "regional_calibration_joint_lp_uta_v13"
            regional_summary["model_name"] = "规格区域校准联合LP-UTA V13"
            regional_summary["learning_context"] = "regional_system_effectiveness"
            regional_summary["regional_context_id"] = context.region_id
            regional_summary["regional_context_label"] = context.label
            regional_summary["calibration_task"] = asdict(context.calibration_task)
            regional_models[context.region_id] = regional_summary
        state["regional_models"] = regional_models
        self.refresh_task_model_views(state, summary)
        active_summary = self.contextual_summary_for_task(state, self.training_task(state))
        state["system_model"] = dict(active_summary)
        state["uta_model"] = dict(active_summary)
        for item in state.get("preference_evidence", []):
            if item in shared_evidence and item.get("status") == "pending_bt_uta":
                item["status"] = "used_bt_uta"
        self.sync_preference_reviews(state)
        self.record_learning_snapshot(state, summary)
        return proxy_bt, model

    @staticmethod
    def record_learning_snapshot(state: Dict[str, Any], summary: Dict[str, Any]) -> None:
        history = state.setdefault("system_learning_history", [])
        snapshot = {
            "sequence": len(history) + 1,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "interaction_count": len(state.get("interactions", [])),
            "effective_pairs": int(summary.get("evidence_pairs", 0)),
            "training_accuracy": summary.get("training_accuracy"),
            "test_accuracy": summary.get("test_accuracy"),
            "conservative_validation_accuracy": summary.get("conservative_validation_accuracy"),
            "judgment_consistency": summary.get("judgment_consistency"),
            "learned_alpha": summary.get("learned_alpha"),
            "attribute_weights": dict(summary.get("attribute_weights") or {}),
            "direct_attribute_weights": dict(summary.get("direct_attribute_weights") or {}),
            "local_coverage_ratio": (summary.get("learning_coverage") or {}).get("local_coverage_ratio"),
            "full_coverage_ratio": (summary.get("learning_coverage") or {}).get("full_coverage_ratio"),
            "open_reviews": sum(
                1
                for item in state.get("review_evidence", [])
                if item.get("status") == "open" and item.get("tier", "must_review") == "must_review"
            ),
        }
        signature_keys = (
            "interaction_count",
            "effective_pairs",
            "training_accuracy",
            "test_accuracy",
            "conservative_validation_accuracy",
            "judgment_consistency",
            "learned_alpha",
            "attribute_weights",
            "direct_attribute_weights",
            "local_coverage_ratio",
            "full_coverage_ratio",
            "open_reviews",
        )
        if history and all(history[-1].get(key) == snapshot.get(key) for key in signature_keys):
            return
        history.append(snapshot)

    def _initial_state(self) -> Dict[str, Any]:
        self._building_product_initial_state = True
        try:
            state = super()._initial_state()
        finally:
            self._building_product_initial_state = False
        state["system_profile_version"] = SYSTEM_PROFILE_VERSION
        state["active_system_task_id"] = self.system_project.default_task().task_id
        state["system_shared_settings"] = self.default_system_settings()
        state["system_settings_by_task"] = {
            item.task_id: dict(state["system_shared_settings"]) for item in self.system_project.tasks
        }
        state["preference_evidence"] = []
        state["bt_weights"] = []
        state["bt_model"] = {}
        state["uta_increments"] = []
        state["uta_model"] = {}
        state["system_model"] = {}
        state["system_shared_model"] = {}
        state["system_models_by_task"] = {}
        state["system_learning_history"] = []
        self._initialize_regional_contexts(state)
        state["known_schemes"] = list(state.get("existing_samples", []))
        state["generated_index"] = 1
        state["active_pair"] = None
        state["active_review_id"] = None
        state["active_learning_version"] = "regional_contextual_requirement_anchored_uta_v13"
        state["system_contract_version"] = self.system_project.contract_version
        state["system_source_contract_version"] = self.system_project.source_contract_version
        state["learning_events"].append(
            {
                "id": f"E-{len(state['learning_events']) + 1:03d}",
                "created_at": state["created_at"],
                "type": "system_level_initialized",
                "title": "系统级V13泵特化全域多工况UTA已初始化",
                "detail": "先学习当前任务附近，再让A/B同时迁入其他规格区域；单属性任务分固定100/80工程锚点，UTA学习共享形状、区域修正和权重，同时继续学习物理前沿。",
            }
        )
        self.retrain_preference_models(state)
        model = self.system_model_from_state(state, task=self.training_task(state))
        anchor = self.initial_task_anchor(state, model)
        state["current_best_id"] = anchor["id"]
        self._last_base_reason = {
            "mode": "initial_requirement_anchor",
            "detail": "从历史完整方案中选择与当前系统要求最接近且物理风险较低的方案作为启动A。",
            "task_distance": round(self.task_requirement_distance(anchor["params"], state, model), 3),
        }
        candidate = self.select_candidate(state, anchor)
        state["known_schemes"].append(candidate)
        state["active_pair"] = {"a": anchor["id"], "b": candidate["id"]}
        self.save_state(state)
        return state

    def load_state(self, reset: bool = False) -> Dict[str, Any]:
        state = super().load_state(reset=reset)
        changed = False
        version = state.get("system_profile_version")
        if version not in COMPATIBLE_SYSTEM_PROFILE_VERSIONS:
            return self._initial_state()
        migrated_v13 = version == 12
        if migrated_v13:
            state["system_profile_version"] = SYSTEM_PROFILE_VERSION
            state["active_learning_version"] = "regional_contextual_requirement_anchored_uta_v13"
            state.setdefault("learning_events", []).append(
                {
                    "id": f"E-{len(state.get('learning_events', [])) + 1:03d}",
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "type": "system_v13_state_migrated",
                    "title": "V13判断已迁移到系统级V13",
                    "detail": "保留全部判断与方案；已按主动设计变量重建因果聚焦证据，并修复跨任务复核编号。",
                }
            )
            changed = True
        review_ids_changed = self._normalize_review_ids(state)
        causal_evidence_changed = self._enrich_causal_focus_evidence(state)
        changed = changed or review_ids_changed or causal_evidence_changed
        valid_tasks = self.system_project.task_by_id()
        if state.get("active_system_task_id") not in valid_tasks:
            state["active_system_task_id"] = self.system_project.default_task().task_id
            changed = True
        if not state.get("regional_contexts"):
            self._initialize_regional_contexts(state)
            changed = True
        shared_settings = state.setdefault("system_shared_settings", self.default_system_settings())
        settings_by_task = state.setdefault("system_settings_by_task", {})
        state.setdefault("system_models_by_task", {})
        learning_history = state.setdefault("system_learning_history", [])
        state["system_contract_version"] = self.system_project.contract_version
        state["system_source_contract_version"] = self.system_project.source_contract_version
        for task in self.system_project.tasks:
            if settings_by_task.get(task.task_id) != shared_settings:
                settings_by_task[task.task_id] = dict(shared_settings)
                changed = True
        shared_model = state.get("system_shared_model") or {}
        if migrated_v13 or causal_evidence_changed or not shared_model or not state.get("regional_models"):
            self.retrain_preference_models(state)
            changed = True
        else:
            active_summary = self.contextual_summary_for_task(state, self.training_task(state))
            state["system_model"] = dict(active_summary)
            state["uta_model"] = dict(active_summary)
            self.refresh_task_model_views(state, shared_model)
            if not learning_history:
                self.record_learning_snapshot(state, shared_model)
                changed = True
        if changed:
            self.save_state(state)
        self._ensure_coupling_system(state)
        return state

    def record_preference_evidence(
        self,
        state: Dict[str, Any],
        interaction_id: str,
        scheme_a: Dict[str, Any],
        scheme_b: Dict[str, Any],
        feasibility_a: str,
        feasibility_b: str,
        preference: str,
        preference_confidence: float = 1.0,
    ) -> Optional[Dict[str, Any]]:
        evidence = super().record_preference_evidence(
            state,
            interaction_id,
            scheme_a,
            scheme_b,
            feasibility_a,
            feasibility_b,
            preference,
            preference_confidence,
        )
        if evidence:
            evidence["learning_context"] = "system_overall_effectiveness"
            evidence["system_task_id"] = self.training_context_id(state)
            evidence["regional_context_id"] = self.training_context_id(state)
            evidence["selected_system_task_id"] = self.active_task_id(state)
            evidence["alpha_settings"] = dict(self.settings_for_task(state))
            evidence["system_task_snapshot"] = asdict(self.training_task(state))
            evidence.update(self._causal_focus_metadata(scheme_a, scheme_b))
            if preference in {"A", "B"}:
                winner = scheme_a if preference == "A" else scheme_b
                if self.scheme_in_region(state, winner):
                    state.setdefault("region_best_ids", {})[
                        self.training_context_id(state)
                    ] = winner["id"]
            notice = state.get("pending_region_notice") or {}
            if notice.get("to_region_id") == self.training_context_id(state):
                state["pending_region_notice"] = None
        return evidence

    def _regional_vector(
        self,
        state: Dict[str, Any],
        base: Dict[str, Any],
        normalized_vector: Optional[Dict[str, float]],
        seed_offset: int,
    ) -> Optional[Dict[str, float]]:
        """Restrict a global sampler to the active primary-specification band."""
        region = self.active_region(state)
        if region is None:
            return normalized_vector
        mappings = list(self.region_planner.mappings_by_system.get(region.primary_system_key) or [])
        usable = [
            item
            for item in mappings
            if abs(float(item.coefficient)) > 1e-12
            and item.product_key in self.attribute_by_key
            and self.attribute_by_key[item.product_key].is_numeric
        ]
        if not usable:
            return normalized_vector
        mapping = max(usable, key=lambda item: abs(float(item.coefficient)))
        spec = self.attribute_by_key[mapping.product_key]
        other = 0.0
        for item in mappings:
            if item is mapping:
                other += float(item.constant)
            else:
                other += float(base["params"][item.product_key]) * float(item.coefficient) + float(item.constant)
        coefficient = float(mapping.coefficient)
        raw_bounds = sorted(
            [
                (float(region.lower) - other) / coefficient,
                (float(region.upper) - other) / coefficient,
            ]
        )
        low = max(float(spec.generation_min), raw_bounds[0])
        high = min(float(spec.generation_max), raw_bounds[1])
        if high <= low:
            return normalized_vector
        low_z = self.normalize_coordinate(low, spec)
        high_z = self.normalize_coordinate(high, spec)
        # A global regional draw starts from the current coherent product and
        # moves the primary scale deliberately. This prevents Latin-hypercube
        # coordinates of unrelated attributes from crowding out the scale
        # change that is required to enter the new band.
        vector = {
            item.key: self.normalize_coordinate(base["params"][item.key], item)
            for item in self.attributes
            if item.is_numeric and item.participates_generation and item.key in base["params"]
        }
        vector.update(normalized_vector or {})
        raw_z = vector.get(mapping.product_key)
        if raw_z is None:
            raw_z = ((seed_offset * 37 + int(state.get("generated_index", 1)) * 11) % 101) / 100.0
        vector[mapping.product_key] = low_z + clamp(float(raw_z), 0.0, 1.0) * (high_z - low_z)
        return vector

    def generate_candidate(
        self,
        state: Dict[str, Any],
        base: Dict[str, Any],
        mode: Optional[str] = None,
        seed_offset: int = 0,
        commit: bool = True,
        normalized_vector: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        self._ensure_coupling_system(state)
        vector = self._regional_vector(state, base, normalized_vector, seed_offset)
        candidate = super().generate_candidate(
            state,
            base,
            mode=mode,
            seed_offset=seed_offset,
            commit=commit,
            normalized_vector=vector,
        )
        completed, formula_steps = self.formula_engine.complete_generated(candidate["params"])
        candidate["params"] = completed
        if formula_steps:
            explanation = candidate.setdefault("generation_explanation", {})
            explanation["formula_completion"] = formula_steps
            labels = "、".join(item["target_label"] for item in formula_steps)
            explanation["detail"] = (
                str(explanation.get("detail") or "")
                + f" 已按已知物理公式自动回算{labels}，没有把派生属性作为独立随机变量。"
            ).strip()
        region = self.active_region(state)
        if region is not None:
            mapped_value = self.region_planner.mapped_values(candidate["params"]).get(region.primary_system_key)
            candidate.setdefault("generation_explanation", {}).update(
                {
                    "regional_context_id": region.region_id,
                    "regional_context_label": region.label,
                    "regional_primary_key": region.primary_system_key,
                    "regional_primary_value": None if mapped_value is None else round(float(mapped_value), 6),
                    "regional_bounds": [region.lower, region.upper],
                    "regional_same_band": bool(
                        mapped_value is not None
                        and float(region.lower) - 1e-9 <= float(mapped_value) <= float(region.upper) + 1e-9
                    ),
                }
            )
        return candidate

    def evaluate(self, params: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._ensure_coupling_system(state)
        base = super().evaluate(params, state)
        formula_assessments = self.formula_engine.assess(params)
        merge_formula_assessment(base, formula_assessments)
        feasibility_model = self.feasibility_model_from_state(state)
        base["local_feasibility_evidence"] = feasibility_model.local_evidence_assessment(params)
        historical = self.historical_match(params)
        if historical is not None:
            base["historical_verified"] = True
            base["historical_scheme_id"] = historical.id
            base["status"] = "verified_historical_feasible"
            base["physics_status"] = "verified_historical_product"
            base["risk"] = 0.0
            base["physics_risk"] = 0.0
            base["learned_feasibility_probability"] = 1.0
            base["feasibility_risk_contributors"] = []
            base["learned_boundary_violations"] = []
            base["feasibility_confidence"] = "高：该参数组合对应已经制造出的历史实物，物理可行性按100%硬锚点处理"
            base.setdefault("assessment_basis", []).insert(
                0,
                f"历史实物 {historical.id} 的完整参数精确匹配，物理可行性已验证",
            )
        else:
            base["historical_verified"] = False
            base["historical_scheme_id"] = None
        # Product-level protocol sheets remain in the workbook only for legacy
        # compatibility. They must not leak into system-level scoring or advice.
        # Keep an empty mapping for export compatibility while ensuring the
        # product-level baseline protocol contributes no system-level score.
        base["requirement_assessment"] = {}
        base["protocol_score"] = None
        base["protocol_score_interval"] = None
        base["baseline_comparison"] = None
        model = self.system_model_from_state(state)
        system = model.assess(params)
        system["base_solver_method"] = system.get("method")
        system["method"] = "全域多工况分层收缩、要求锚定的主动学习稳健联合LP-UTA V13"
        transfer = self.task_transfer_profile(state or {}, model_summary=model.summary_data)
        system["task_transfer"] = transfer
        system["confidence"] = round(min(float(system.get("confidence", 0.0)), float(transfer["confidence"])), 3)
        base["system_assessment"] = system
        base["effectiveness_score"] = system["total_score"]
        base["effectiveness_source"] = system["method"]
        base["bt_score"] = system["total_score"]
        base["top_contributors"] = [
            {
                "key": item["label"],
                "label": item["label"],
                "value": item["contribution"],
                "channel": item["channel"],
            }
            for item in system["top_contributors"]
        ]
        base.setdefault("assessment_basis", []).extend(
            [
                f"系统任务：{system['task']['task_name']}",
                f"当前任务适配权重 alpha={system['alpha']:.1%}",
                "Excel提供系统要求锚点与方向；系统直接属性和其他成品属性的边际效用均由专家比较联合学习。",
                f"派生属性公式：{len(formula_assessments)}条；公式先用于物理诊断，再进入系统效能评价。",
                f"任务迁移：{transfer['label']}（把握{transfer['confidence']:.0%}），共享模型未因切换任务而重训。",
            ]
        )
        return base

    @staticmethod
    def _percentile(values: List[float], probability: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(float(value) for value in values)
        position = clamp(float(probability), 0.0, 1.0) * (len(ordered) - 1)
        left = int(math.floor(position))
        right = int(math.ceil(position))
        if left == right:
            return ordered[left]
        ratio = position - left
        return ordered[left] + ratio * (ordered[right] - ordered[left])

    def _model_from_explicit_summary(
        self,
        state: Dict[str, Any],
        task: Any,
        summary: Dict[str, Any],
    ) -> HierarchicalUTAModel:
        temporary = dict(state)
        temporary["system_shared_model"] = dict(summary)
        temporary["system_model"] = dict(summary)
        temporary["regional_models"] = {}
        return self.system_model_from_state(temporary, task=task)

    def unknown_visualization_payload(
        self,
        params: Dict[str, Any],
        state: Dict[str, Any],
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        active_task = self.active_task(state)
        active_model = self.system_model_from_state(state, task=active_task)
        current_system = result.get("system_assessment") or active_model.assess(params)
        direct_values = {
            item.get("system_key"): item
            for item in ((current_system.get("direct_assessment") or {}).get("attributes") or [])
        }
        direct_curves = []
        requirements = active_task.requirement_by_key()
        for curve in active_model.direct_uta_curves():
            copy = dict(curve)
            current = direct_values.get(curve.get("system_key")) or {}
            requirement = requirements.get(str(curve.get("system_key")))
            display_range = self.region_planner.range_by_system.get(str(curve.get("system_key")))
            if requirement is not None and display_range is not None:
                low, high = (float(display_range[0]), float(display_range[1]))
                if high > low:
                    smooth_points: List[Dict[str, Any]] = []
                    for index in range(161):
                        value = low + (high - low) * index / 160.0
                        marginal = active_model.direct_marginal_score(requirement, value)
                        smooth_points.append(
                            {
                                "value": round(value, 8),
                                "utility": round(float(marginal["score"]), 6),
                                "score": round(float(marginal["score"]), 6),
                                "prior_utility": round(
                                    float(marginal.get("configured_prior_score", marginal["score"])), 6
                                ),
                                "satisfied": bool(marginal.get("satisfied", False)),
                            }
                        )
                    copy["minimum"] = low
                    copy["maximum"] = high
                    copy["smooth_points"] = smooth_points
                    copy["display_domain_source"] = "excel_mapped_generation_range"
            copy["current_value"] = current.get("value")
            copy["current_score"] = current.get("score")
            copy["current_satisfied"] = current.get("satisfied")
            direct_curves.append(copy)
        product_curves = []
        for curve in active_model.marginal_curves():
            copy = dict(curve)
            copy["current_value"] = params.get(curve.get("key"))
            product_curves.append(copy)

        scenarios: List[Dict[str, Any]] = []
        seen_tasks: List[Any] = []
        task_entries: List[Tuple[str, str, Any]] = [
            (f"TASK-{active_task.task_id}", f"当前任务·{active_task.task_name}", active_task)
        ]
        task_entries.extend(
            (context.region_id, context.label, context.calibration_task)
            for context in self.regional_contexts(state)
        )
        for context_id, label, task in task_entries:
            if any(self.region_planner.context_distance(task, seen) < 0.08 for seen in seen_tasks):
                continue
            seen_tasks.append(task)
            assessment = self.system_model_from_state(state, task=task).assess(params)
            transfer = self.contextual_summary_for_task(state, task).get("contextual_transfer") or {}
            scenarios.append(
                {
                    "context_id": context_id,
                    "label": label,
                    "task": asdict(task),
                    "total_score": assessment.get("total_score"),
                    "continuous_score": assessment.get("continuous_score", assessment.get("raw_total_score")),
                    "direct_score": assessment.get("direct_score"),
                    "product_score": assessment.get("product_score"),
                    "failed_requirement_count": assessment.get("failed_requirement_count"),
                    "transfer_confidence": transfer.get("confidence", 0.35),
                }
            )

        sensitivity_scores: List[float] = [float(current_system.get("continuous_score", current_system.get("total_score", 0.0)))]
        shared = state.get("system_shared_model") or {}
        if shared:
            sensitivity_scores.append(
                float(self._model_from_explicit_summary(state, active_task, shared).assess(params).get("continuous_score", 0.0))
            )
        for summary in (state.get("regional_models") or {}).values():
            if int(summary.get("evidence_pairs", 0)) < 2:
                continue
            sensitivity_scores.append(
                float(self._model_from_explicit_summary(state, active_task, summary).assess(params).get("continuous_score", 0.0))
            )

        flow_weight: Dict[str, Any] = {}
        contexts = self.regional_contexts(state)
        if contexts:
            primary_key = contexts[0].primary_system_key
            weight_key = None
            for requirement in active_task.requirements:
                if requirement.system_key != primary_key and requirement.requirement_type == "at_most":
                    weight_key = requirement.system_key
                    break
            if weight_key is None:
                for mapping in self.system_project.direct_mappings():
                    spec = self.attribute_by_key.get(mapping.product_key)
                    if mapping.system_key != primary_key and spec and spec.preference_direction == "lower_better":
                        weight_key = mapping.system_key
                        break
            if weight_key:
                history = []
                for scheme in self.project.schemes:
                    mapped = self.region_planner.mapped_values(scheme.params)
                    if primary_key in mapped and weight_key in mapped:
                        history.append(
                            {
                                "id": scheme.id,
                                "x": round(float(mapped[primary_key]), 6),
                                "y": round(float(mapped[weight_key]), 6),
                            }
                        )
                current_mapped = self.region_planner.mapped_values(params)
                budget_points = []
                for context in contexts:
                    requirement = context.calibration_task.requirement_by_key().get(weight_key)
                    if requirement and requirement.maximum is not None:
                        budget_points.append(
                            {
                                "region_id": context.region_id,
                                "label": context.label,
                                "x": context.center,
                                "y": float(requirement.maximum),
                            }
                        )
                flow_weight = {
                    "x_key": primary_key,
                    "x_label": contexts[0].primary_system_label,
                    "x_unit": contexts[0].primary_unit,
                    "y_key": weight_key,
                    "y_label": self.system_project.system_attribute_by_key()[weight_key].label,
                    "y_unit": self.system_project.system_attribute_by_key()[weight_key].unit,
                    "history": history,
                    "current": {
                        "x": current_mapped.get(primary_key),
                        "y": current_mapped.get(weight_key),
                    },
                    "regional_budget_points": budget_points,
                }
        return {
            "version": "system_v13_unknown_visualization_1",
            "direct_curves": direct_curves,
            "product_curves": product_curves,
            "task_scenarios": scenarios,
            "flow_weight": flow_weight,
            "regional_sensitivity": {
                "model_count": len(sensitivity_scores),
                "minimum": round(min(sensitivity_scores), 3),
                "p10": round(float(self._percentile(sensitivity_scores, 0.10)), 3),
                "median": round(float(self._percentile(sensitivity_scores, 0.50)), 3),
                "p90": round(float(self._percentile(sensitivity_scores, 0.90)), 3),
                "maximum": round(max(sensitivity_scores), 3),
                "definition": "共享模型、区域模型与上下文插值模型对同一任务的敏感性范围，不是试验统计置信区间。",
            },
            "regional_progress": self.regional_progress(state),
        }

    def evaluate_unknown(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        record = super().evaluate_unknown(payload)
        state = self.load_state()
        stored = next(
            (item for item in state.get("unknown_evaluations", []) if item.get("id") == record.get("id")),
            None,
        )
        target = stored or record
        target.setdefault("result", {})["system_v13_visualization"] = self.unknown_visualization_payload(
            target["params"], state, target["result"]
        )
        if stored is not None:
            self.save_state(state)
        return target

    def generation_mode(self, state: Dict[str, Any]) -> str:
        self._ensure_coupling_system(state)
        if getattr(self, "_building_product_initial_state", False):
            return super().generation_mode(state)
        latest_interaction = (state.get("interactions") or [None])[-1]
        latest_has_infeasible = bool(
            latest_interaction
            and (
                latest_interaction.get("feasibility_a") == "infeasible"
                or latest_interaction.get("feasibility_b") == "infeasible"
            )
        )
        recent_coupling = self.recent_coupling_infeasible(state)
        if latest_has_infeasible and recent_coupling:
            latest = recent_coupling["scheme"]
            latest_mode = (latest.get("generation_explanation") or {}).get("mode")
            by_id = {str(item.get("id")): item for item in state.get("known_schemes", [])}
            bracket_in_cooldown = False
            for interaction in state.get("interactions", [])[-4:]:
                for side in ("a", "b"):
                    scheme = by_id.get(str(interaction.get(f"scheme_{side}") or ""))
                    if (scheme or {}).get("generation_explanation", {}).get("mode") == "feasibility_bracket":
                        bracket_in_cooldown = True
                        break
                if bracket_in_cooldown:
                    break
            if latest_mode != "feasibility_bracket" and not bracket_in_cooldown:
                return "feasibility_bracket"
        context = getattr(self, "_candidate_context", None) or {}
        phase = context.get("phase")
        if not phase:
            model = self.system_model_from_state(state, task=self.training_task(state))
            coverage = self.active_learning_coverage(state, model)
            phase = self.active_learning_phase(coverage, len(model.direct_space.attributes))
        index = int(state.get("generated_index", 1))
        weak_coupling_strength = any(
            float(model.identifiability_score) < 0.58
            for model in self.coupling_system.models.values()
        )
        probe = (
            "directional_coupling_probe"
            if self.coupling_system.target_keys()
            and (not self.coupling_system.models or weak_coupling_strength)
            else "coupling_boundary_probe"
            if self.coupling_system.models
            else "global_space_fill"
        )
        if phase == "requirement_neighborhood":
            schedule = [
                "multiscale_local",
                "multiscale_local",
                "multiscale_local",
                "global_space_fill",
                "multiscale_local",
                probe,
            ]
        elif phase == "global_completion":
            schedule = [
                "global_space_fill",
                "maximin_exploration",
                "multiscale_local",
                "global_space_fill",
                probe,
                "multiscale_local",
            ]
        else:
            schedule = ["multiscale_local", "global_space_fill", "multiscale_local", probe]
        return schedule[(index - 1) % len(schedule)]

    def _candidate_learning_context(
        self,
        state: Dict[str, Any],
        base: Dict[str, Any],
    ) -> Dict[str, Any]:
        model = self.system_model_from_state(state, task=self.training_task(state))
        coverage = self.active_learning_coverage(state, model)
        return {
            "model": model,
            "coverage": coverage,
            "phase": self.active_learning_phase(coverage, len(model.direct_space.attributes)),
            "base_assessment": model.assess(base["params"]),
            "slot_by_id": {item["slot_id"]: item for item in coverage.get("slots", [])},
        }

    def candidate_selection_adjustment(
        self,
        state: Dict[str, Any],
        base: Dict[str, Any],
        candidate: Dict[str, Any],
        evaluation: Dict[str, Any],
        metrics: Dict[str, float],
        mode: str,
    ) -> Tuple[float, Dict[str, Any]]:
        if getattr(self, "_building_product_initial_state", False):
            return 0.0, {}
        context = getattr(self, "_candidate_context", None) or self._candidate_learning_context(state, base)
        model: HierarchicalUTAModel = context["model"]
        coverage = context["coverage"]
        phase = context["phase"]
        pseudo_evidence = {
            "params_a": base["params"],
            "params_b": candidate["params"],
            "relation": "A",
            "system_task_id": self.training_context_id(state),
            "regional_context_id": self.training_context_id(state),
            "system_task_snapshot": asdict(self.training_task(state)),
        }
        groups = model.pair_learning_slots(pseudo_evidence)
        slot_by_id = context["slot_by_id"]
        touched = [slot for slots in groups.values() for slot in slots if slot in slot_by_id]
        global_turn = mode in {"global_space_fill", "maximin_exploration"}
        eligible = [
            slot
            for slot in touched
            if not slot_by_id[slot].get("confident")
            and (phase != "requirement_neighborhood" or global_turn or slot_by_id[slot].get("local"))
        ]
        scarcity_values = [
            1.0 / (1.0 + float(slot_by_id[slot].get("evidence_count", 0)))
            for slot in set(eligible)
        ]
        coverage_gain = sum(sorted(scarcity_values, reverse=True)[:4]) / max(min(len(scarcity_values), 4), 1)
        zero_coverage_bonus = 0.18 if any(not slot_by_id[slot].get("covered") for slot in eligible) else 0.0
        task_distance = self.task_requirement_distance(candidate["params"], state, model)
        requirement_proximity = 1.0 / (1.0 + task_distance)
        changed_groups = len(groups)
        independent_changes: List[str] = []
        for spec in self.independent_query_specs():
            before = self.normalize_coordinate(base["params"][spec.key], spec)
            after = self.normalize_coordinate(candidate["params"][spec.key], spec)
            if abs(after - before) >= 0.018:
                independent_changes.append(spec.key)
        focused = 1 <= len(independent_changes) <= 2
        interpretability = (
            1.0
            if focused
            else max(0.0, 1.0 - 0.28 * max(0, len(independent_changes) - 2))
        )
        right_assessment = evaluation.get("system_assessment") or model.assess(candidate["params"])
        score_gap = abs(
            float(right_assessment.get("total_score", 0.0))
            - float(context["base_assessment"].get("total_score", 0.0))
        )
        target_gap = 1.5 if phase == "stability_confirmation" else 4.5 if global_turn else 3.0
        gap_width = 2.4 if global_turn else 1.8
        comparison_informativeness = math.exp(
            -0.5 * ((score_gap - target_gap) / max(gap_width, 1e-9)) ** 2
        )
        tiny_gap_penalty = (
            0.0
            if phase == "stability_confirmation" or score_gap >= 0.8
            else 0.75 * (0.8 - score_gap) / 0.8
        )
        if phase == "requirement_neighborhood":
            adjustment = (
                1.10 * coverage_gain
                + zero_coverage_bonus
                + (0.65 if not global_turn else 0.18) * requirement_proximity
                + 0.42 * comparison_informativeness
                + 0.28 * interpretability
                - 0.18 * float(metrics.get("base_distance", 0.0))
                - tiny_gap_penalty
            )
        elif phase == "global_completion":
            adjustment = (
                1.30 * coverage_gain
                + zero_coverage_bonus
                + 0.40 * comparison_informativeness
                + 0.24 * interpretability
                + 0.12 * float(metrics.get("novelty", 0.0))
                - tiny_gap_penalty
            )
        else:
            adjustment = (
                0.58 * comparison_informativeness
                + 0.48 * coverage_gain
                + 0.28 * interpretability
            )
        if not eligible and phase != "stability_confirmation":
            adjustment -= 0.28
        region = self.active_region(state)
        regional_value = None
        regional_penalty = 0.0
        trivial_gap_penalty = 0.0
        if region is not None:
            regional_value = self.region_planner.mapped_values(candidate["params"]).get(
                region.primary_system_key
            )
            if regional_value is None:
                regional_penalty = 3.0
            elif float(regional_value) < float(region.lower):
                regional_penalty = 4.0 + 2.0 * (
                    (float(region.lower) - float(regional_value))
                    / max(abs(float(region.center)), 1e-9)
                )
            elif float(regional_value) > float(region.upper):
                regional_penalty = 4.0 + 2.0 * (
                    (float(regional_value) - float(region.upper))
                    / max(abs(float(region.center)), 1e-9)
                )
            adjustment -= regional_penalty
        if score_gap > 14.0:
            trivial_gap_penalty = min(3.5, 0.22 * (score_gap - 14.0))
            adjustment -= trivial_gap_penalty
        target_details = sorted(
            (slot_by_id[slot] for slot in set(eligible)),
            key=lambda item: (item.get("evidence_count", 0), item.get("attribute_label", ""), item.get("segment", 0)),
        )[:4]
        phase_label = {
            "requirement_neighborhood": "当前系统要求附近优先",
            "global_completion": "全域UTA曲线补齐",
            "stability_confirmation": "模型稳定性确认",
        }[phase]
        return adjustment, {
            "active_learning_phase": phase,
            "active_learning_phase_label": phase_label,
            "requirement_distance": round(task_distance, 3),
            "requirement_proximity": round(requirement_proximity, 3),
            "uta_coverage_gain": round(coverage_gain, 3),
            "comparison_uncertainty": round(comparison_informativeness, 3),
            "comparison_informativeness": round(comparison_informativeness, 3),
            "predicted_score_gap": round(score_gap, 3),
            "target_score_gap": target_gap,
            "trivial_score_gap_penalty": round(trivial_gap_penalty, 3),
            "regional_band_penalty": round(regional_penalty, 3),
            "regional_primary_value": None if regional_value is None else round(float(regional_value), 6),
            "regional_context_id": None if region is None else region.region_id,
            "regional_context_label": None if region is None else region.label,
            "controlled_variable_comparison": focused,
            "model_attribute_groups_changed": changed_groups,
            "independent_attribute_groups_changed": len(independent_changes),
            "independent_changed_keys": independent_changes,
            "target_uta_slots": [
                {
                    "slot_id": item["slot_id"],
                    "attribute_label": item["attribute_label"],
                    "side_label": item["side_label"],
                    "segment": item["segment"],
                    "evidence_count": item["evidence_count"],
                }
                for item in target_details
            ],
        }

    def select_candidate(self, state: Dict[str, Any], base: Dict[str, Any], pool_size: int = 192) -> Dict[str, Any]:
        if getattr(self, "_building_product_initial_state", False):
            return super().select_candidate(state, base, min(pool_size, 24))
        context = self._candidate_learning_context(state, base)
        self._candidate_context = context
        try:
            candidate = super().select_candidate(state, base, pool_size)
        finally:
            self._candidate_context = None
        region = self.active_region(state)
        if region is not None and not self.scheme_in_region(state, candidate, region):
            fallback = [
                self.generate_candidate(
                    state,
                    base,
                    mode="global_space_fill",
                    seed_offset=910 + offset,
                    commit=False,
                    normalized_vector={},
                )
                for offset in range(12)
            ]
            fallback = [item for item in fallback if self.scheme_in_region(state, item, region)]
            if not fallback:
                raise ProjectDataError(f"候选生成器无法把方案B限制在{region.label}。")
            candidate = min(
                fallback,
                key=lambda item: abs(
                    float(
                        self.region_planner.mapped_values(item["params"])[region.primary_system_key]
                    )
                    - float(region.center)
                ),
            )
            candidate["id"] = f"G-{int(state.get('generated_index', 1)):03d}"
            state["generated_index"] = int(state.get("generated_index", 1)) + 1
            candidate.setdefault("generation_explanation", {}).update(
                {
                    "regional_fallback": True,
                    "detail": "原候选越过当前规格边界，已自动替换为同区域候选。",
                }
            )
        model = context["model"]
        left = model.assess(base["params"])
        right = model.assess(candidate["params"])
        direct_change = float(right["direct_score"]) - float(left["direct_score"])
        product_change = float(right["product_score"]) - float(left["product_score"])
        if abs(direct_change) >= 1.0 and abs(product_change) >= 1.0 and direct_change * product_change < 0:
            query_type = "跨组权衡"
            purpose = "学习当前任务适配与成品固有性能之间的权衡"
        elif abs(direct_change) >= abs(product_change):
            query_type = "当前任务适配探索"
            purpose = "比较当前Excel定义的直接系统属性变化"
        else:
            query_type = "成品性能贡献探索"
            purpose = "学习其他成品属性对系统总体效能的经验贡献"
        candidate.setdefault("generation_explanation", {}).update(
            {
                "system_query_type": query_type,
                "system_purpose": purpose,
                "direct_score_change": round(direct_change, 2),
                "product_score_change": round(product_change, 2),
                "predicted_system_score": right["total_score"],
                "base_selection": dict(getattr(self, "_last_base_reason", {}) or {}),
                "regional_context_id": self.training_context_id(state),
                "regional_context_label": (
                    self.active_region(state).label if self.active_region(state) is not None else "当前任务"
                ),
            }
        )
        return candidate

    def learning_completion(self, state: Dict[str, Any]) -> Dict[str, Any]:
        model = state.get("system_shared_model") or state.get("system_model") or {}
        pairs = int(model.get("evidence_pairs", 0))
        minimum = max(30, 4 * (int(model.get("attribute_count", 0)) + 1))
        accuracy = model.get("conservative_validation_accuracy")
        consistency = model.get("judgment_consistency")
        model_instance = self.system_model_from_state(state, task=self.training_task(state))
        active_coverage = self.active_learning_coverage(state, model_instance)
        shared_coverage = model_instance.learning_coverage(self.shared_preference_evidence(state))
        phase = self.active_learning_phase(active_coverage, len(model_instance.direct_space.attributes))
        open_reviews = sum(
            1
            for item in state.get("review_evidence", [])
            if item.get("status") == "open"
            and item.get("tier", "must_review") == "must_review"
            and item.get("learning_context", "system_overall_effectiveness") == "system_overall_effectiveness"
        )
        transfer = self.task_transfer_profile(state, model_summary=model)
        region_progress = self.regional_progress(state)
        completed_regions = sum(1 for item in region_progress if item["complete"])
        current_task_region = min(
            region_progress,
            key=lambda item: self.region_planner.context_distance(
                self.active_task(state), task_from_dict(item["calibration_task"])
            ),
            default=None,
        )
        local_complete = bool(current_task_region and current_task_region["complete"])
        local_reasons = list((current_task_region or {}).get("reasons") or [])
        global_reasons: List[str] = []
        if completed_regions < len(region_progress):
            global_reasons.append(f"规格区域完成{completed_regions}/{len(region_progress)}")
        if pairs < minimum:
            global_reasons.append(f"共享模型有效比较{pairs}对，建议至少{minimum}对")
        if accuracy is None:
            global_reasons.append("尚未形成稳健独立验证正确率")
        elif float(accuracy) < 0.80:
            global_reasons.append(f"保守验证正确率{float(accuracy):.0%}，未达到80%")
        if consistency is None:
            global_reasons.append("尚不能检查判断一致性")
        elif float(consistency) < 0.80:
            global_reasons.append(f"判断一致性{float(consistency):.0%}，未达到80%")
        if float(shared_coverage.get("full_coverage_ratio", 0.0)) < 0.60:
            global_reasons.append(
                f"共享UTA区段覆盖{float(shared_coverage.get('full_coverage_ratio', 0.0)):.0%}，目标60%"
            )
        if float(shared_coverage.get("full_confident_ratio", 0.0)) < 0.45:
            global_reasons.append(
                f"共享可靠覆盖{float(shared_coverage.get('full_confident_ratio', 0.0)):.0%}，目标45%"
            )
        if open_reviews:
            global_reasons.append(f"还有{open_reviews}项判断待复核")
        complete = not global_reasons
        if complete:
            title = "全域多工况效能模型学习完成"
            message = f"{len(region_progress)}个代表规格区域均已完成；{transfer['label']}。"
        elif local_complete:
            title = "当前任务附近已就绪，正在补齐其他规格区域"
            message = "；".join(global_reasons) + "。下一组A/B会在尚未完成的规格区域内共同生成。"
        else:
            title = "当前系统要求附近仍需继续学习"
            message = "；".join(local_reasons) + "。"
        return {
            "status": "standard_complete" if complete else "continue_learning",
            "complete": complete,
            "requirement_neighborhood_complete": local_complete,
            "global_curve_complete": complete,
            "high_confidence": bool(
                complete
                and float(accuracy or 0.0) >= 0.90
                and float(consistency or 0.0) >= 0.90
            ),
            "title": title,
            "message": message,
            "validation_accuracy": accuracy,
            "validation_components": dict(model.get("validation_components") or {}),
            "test_pairs": int(model.get("test_pairs", 0)),
            "accuracy_target": 0.80,
            "consistency": consistency,
            "consistency_target": 0.80,
            "m1_consistent": bool(model.get("m1_consistent", False)),
            "m1_role": "严格零冲突诊断，不作为独立完成门槛",
            "m3_minimum_conflicts": int((model.get("m3") or {}).get("minimum_conflicts", 0)),
            "shared_evidence_pairs": pairs,
            "exact_context_pairs": transfer["task_specific_pairs"],
            "minimum_pairs": minimum,
            "local_minimum_pairs": (current_task_region or {}).get("minimum_pairs", 0),
            "open_reviews": open_reviews,
            "reasons": global_reasons,
            "local_reasons": local_reasons,
            "active_learning_phase": phase,
            "active_learning_coverage": active_coverage,
            "shared_learning_coverage": shared_coverage,
            "regional_progress": region_progress,
            "completed_regions": completed_regions,
            "total_regions": len(region_progress),
            "active_region_id": self.training_context_id(state),
            "active_region_label": (
                self.active_region(state).label if self.active_region(state) is not None else "当前任务"
            ),
            "transfer": transfer,
        }

    @staticmethod
    def task_payload(task: Any, model: HierarchicalUTAModel) -> Dict[str, Any]:
        """Serialize task requirements with the effective V13 scoring scales."""
        payload = asdict(task)
        for raw, requirement in zip(payload.get("requirements", []), task.requirements):
            if requirement.system_key not in model.direct_space.attribute_by_key:
                continue
            definitions = model.direct_space.branch_definition(requirement)
            kind = requirement.requirement_type
            lower_relevant = kind in {"target", "at_least", "within_range"} or (
                kind == "at_most" and requirement.overachievement_bonus
            )
            upper_relevant = kind in {"target", "at_most", "within_range"} or (
                kind == "at_least" and requirement.overachievement_bonus
            )
            raw["effective_lower_80_distance"] = (
                round(float(definitions["left"]["scale"]), 8) if lower_relevant else None
            )
            raw["effective_upper_80_distance"] = (
                round(float(definitions["right"]["scale"]), 8) if upper_relevant else None
            )
            raw["lower_80_distance_source"] = (
                "configured" if requirement.lower_80_distance is not None else "automatic"
            ) if lower_relevant else None
            raw["upper_80_distance_source"] = (
                "configured" if requirement.upper_80_distance is not None else "automatic"
            ) if upper_relevant else None
        return payload

    def summarize(self, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        state = state or self.load_state()
        output = super().summarize(state)
        output["project"]["stage"] = "系统级V13泵特化"
        output["project"]["model_note"] = "当前任务附近优先，完成后A/B同时迁入其他规格区域；直接属性采用任务条件100/80锚定UTA，区域模型以收缩方式修正共享曲线和权重。"
        visualization_model = self.system_model_from_state(state, task=self.active_task(state))
        training_model = self.system_model_from_state(state, task=self.training_task(state))
        coverage = self.active_learning_coverage(state, training_model)
        region_progress = self.regional_progress(state)
        active_model = dict(
            (state.get("system_models_by_task") or {}).get(self.active_task_id(state))
            or state.get("system_model")
            or {}
        )
        active_model["learning_coverage"] = coverage
        output["system"] = {
            "profile_version": SYSTEM_PROFILE_VERSION,
            "contract_version": self.system_project.contract_version,
            "source_contract_version": self.system_project.source_contract_version,
            "fingerprint": self.system_project.system_fingerprint,
            "learning_fingerprint": self.system_project.system_learning_fingerprint,
            "active_task_id": self.active_task_id(state),
            "active_task": self.task_payload(self.active_task(state), visualization_model),
            "system_attributes": [asdict(item) for item in self.system_project.system_attributes],
            "tasks": [self.task_payload(item, visualization_model) for item in self.system_project.tasks],
            "mappings": [asdict(item) for item in self.system_project.mappings],
            "formulas": [asdict(item) for item in self.system_project.formulas],
            "formula_calibrations": self.formula_engine.summaries(),
            "settings": self.settings_for_task(state),
            "model": active_model,
            "shared_model": state.get("system_shared_model") or {},
            "transfer": self.task_transfer_profile(state, model_summary=state.get("system_shared_model") or {}),
            "models_by_task": state.get("system_models_by_task") or {},
            "training_context": {
                "region_id": self.training_context_id(state),
                "region": None if self.active_region(state) is None else self.active_region(state).to_dict(),
                "task": self.task_payload(self.training_task(state), training_model),
                "notice": state.get("pending_region_notice"),
            },
            "regional_progress": region_progress,
            "regional_models": state.get("regional_models") or {},
            "warnings": self.system_project.warnings,
            "active_learning": {
                "phase": self.active_learning_phase(coverage, len(visualization_model.direct_space.attributes)),
                "coverage": coverage,
            },
        }
        output["utaModel"] = active_model
        for side in ("a", "b"):
            scheme = (output.get("pair") or {}).get(side)
            if scheme and scheme.get("params"):
                scheme.setdefault("prediction", {})["system_assessment"] = training_model.assess(
                    scheme["params"]
                )
        output.setdefault("visualization", {}).update(
            {
                "utaMarginalCurves": visualization_model.marginal_curves(),
                "systemDirectUTACurves": visualization_model.direct_uta_curves(),
                "systemRequirementCurves": visualization_model.requirement_curves(),
                "trainingContextDirectCurves": training_model.direct_uta_curves(),
                "learningHistory": list(state.get("system_learning_history", [])),
                "learningCoverage": coverage,
                "regionalProgress": region_progress,
                "visualizationVersion": "system_v13_regional_multicontext_charts_1",
            }
        )
        output["learningCompletion"] = self.learning_completion(state)
        output["stats"]["openReviews"] = sum(
            1
            for item in state.get("review_evidence", [])
            if item.get("status") == "open" and item.get("tier", "must_review") == "must_review"
        )
        output["stats"]["suggestedReviews"] = sum(
            1
            for item in state.get("review_evidence", [])
            if item.get("status") == "open" and item.get("tier") == "suggested_review"
        )
        output["requirementProfiles"] = []
        output["protocolProfiles"] = []
        output["activeRequirement"] = None
        output["activeProtocol"] = None
        active_review_id = str(state.get("active_review_id") or "")
        active_review = next(
            (
                item
                for item in output.get("reviewEvidence", [])
                if str(item.get("id")) == active_review_id and item.get("status") == "open"
            ),
            None,
        )
        output["activeReview"] = active_review
        return output

    def export_datasets(self, state: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
        datasets = super().export_datasets(state)
        datasets["system_attributes"] = [asdict(item) for item in self.system_project.system_attributes]
        datasets["system_mappings"] = [asdict(item) for item in self.system_project.mappings]
        datasets["system_task_requirements"] = [
            {
                "任务编号": task.task_id,
                "任务名称": task.task_name,
                "系统型号/工况": task.operating_condition,
                **asdict(requirement),
            }
            for task in self.system_project.tasks
            for requirement in task.requirements
        ]
        datasets["formula_constraints"] = self.formula_engine.summaries()
        datasets["formula_diagnostics"] = [
            {
                "评估编号": record.get("id"),
                "时间": record.get("created_at"),
                **item,
            }
            for record in state.get("unknown_evaluations", [])
            for item in ((record.get("result") or {}).get("formula_assessments") or [])
        ]
        datasets["system_models"] = [
            {"任务编号": task_id, **summary}
            for task_id, summary in (state.get("system_models_by_task") or {}).items()
        ]
        datasets["regional_contexts"] = self.regional_progress(state)
        datasets["regional_models"] = [
            {"区域编号": region_id, **summary}
            for region_id, summary in (state.get("regional_models") or {}).items()
        ]
        datasets["system_learning_history"] = [
            {
                "更新序号": item.get("sequence"),
                "时间": item.get("created_at"),
                "全部判断数": item.get("interaction_count"),
                "有效比较数": item.get("effective_pairs"),
                "训练正确率": item.get("training_accuracy"),
                "测试正确率": item.get("test_accuracy"),
                "保守验证正确率": item.get("conservative_validation_accuracy"),
                "判断一致性": item.get("judgment_consistency"),
                "要求附近覆盖": item.get("local_coverage_ratio"),
                "全域覆盖": item.get("full_coverage_ratio"),
                "任务适配权重alpha": item.get("learned_alpha"),
                "属性权重": item.get("attribute_weights"),
                "待复核数": item.get("open_reviews"),
            }
            for item in state.get("system_learning_history", [])
        ]
        coverage = self.active_learning_coverage(state)
        datasets["uta_learning_coverage"] = [dict(item) for item in coverage.get("slots", [])]
        datasets["coupling_online_models"] = [
            {
                "目标属性": model.get("target_label"),
                "目标属性ID": model.get("target_key"),
                "模型状态": model.get("model_status"),
                "可辨识性": model.get("identifiability_label"),
                "可辨识性得分": model.get("identifiability_score"),
                "有效秩": model.get("effective_rank"),
                "条件数": model.get("condition_number"),
                "专家约束数": model.get("expert_constraint_count", 0),
                "历史样本数": model.get("sample_count", 0),
                "源属性效应": model.get("source_effects", []),
                "局部带宽": {
                    "lower_offset": model.get("lower_offset"),
                    "upper_offset": model.get("upper_offset"),
                },
            }
            for model in self.coupling_system.summaries()
        ]
        datasets["system_score_ledger"] = [
            {
                "评估编号": record.get("id"),
                "时间": record.get("created_at"),
                "任务编号": (result.get("system_assessment") or {}).get("task", {}).get("task_id"),
                "系统属性": item.get("system_attribute"),
                "系统属性ID": item.get("system_key"),
                "映射值": item.get("value"),
                "单位": item.get("unit"),
                "要求类型": item.get("requirement_type"),
                "要求说明": item.get("requirement_text"),
                "单项得分": item.get("score"),
                "是否满足": item.get("satisfied"),
                "组内权重": item.get("group_weight"),
                "综合分贡献": item.get("total_point_contribution"),
                "映射来源": item.get("mapping_sources"),
            }
            for record in state.get("unknown_evaluations", [])
            for result in [record.get("result") or {}]
            for item in ((result.get("system_assessment") or {}).get("direct_assessment") or {}).get("attributes", [])
        ]
        datasets["learning_events"] = [dict(item) for item in state.get("learning_events", [])]
        for row, evidence in zip(datasets.get("preference_evidence", []), state.get("preference_evidence", [])):
            row["系统任务编号"] = evidence.get("selected_system_task_id")
            row["区域校准上下文"] = evidence.get("regional_context_id")
            row["区域校准任务快照"] = evidence.get("system_task_snapshot")
            row["学习上下文"] = evidence.get("learning_context")
        return datasets

    def report_markdown(self, state: Dict[str, Any]) -> str:
        base = super().report_markdown(state)
        lines = [
            base.rstrip(),
            "",
            "## 系统级V13泵特化全域多工况分层UTA模型",
            "",
            f"- 系统配置指纹：`{self.system_project.system_fingerprint}`",
            f"- 共享学习指纹：`{self.system_project.system_learning_fingerprint}`",
            f"- Excel契约：`{self.system_project.contract_version}`",
            f"- 系统属性数：{len(self.system_project.system_attributes)}",
            f"- 直接映射数：{len(self.system_project.direct_mappings())}",
            f"- 系统任务数：{len(self.system_project.tasks)}",
            f"- 派生属性公式数：{len(self.system_project.formulas)}",
            "- 综合算法：确定性公式/效率折算 + 系统要求锚定直接属性UTA + 跨任务共享成品性能UTA + 规格区域收缩修正 + 邻近区域平滑插值。",
            "",
            "### 各任务迁移状态",
            "",
            "| 任务 | 本任务判断 | 共享判断 | 迁移方式 | 迁移置信度 | alpha |",
            "|---|---:|---:|---|---:|---:|",
        ]
        models = state.get("system_models_by_task") or {}
        for task in self.system_project.tasks:
            model = models.get(task.task_id) or {}
            lines.append(
                f"| {task.task_name} | {(model.get('transfer') or {}).get('task_specific_pairs', 0)} "
                f"| {model.get('evidence_pairs', 0)} | {(model.get('transfer') or {}).get('label')} "
                f"| {(model.get('transfer') or {}).get('confidence')} | {model.get('learned_alpha')} |"
            )
        lines.extend(
            [
                "",
                "系统要求、映射、共享模型和各任务迁移视图均随本报告导出；完整逐条判断和State历史仅保存在甲方内部电脑。",
                "",
            ]
        )
        return "\n".join(lines)

    def export_workbook_bytes(self, state: Dict[str, Any], datasets: Dict[str, List[Dict[str, Any]]]) -> bytes:
        raw = super().export_workbook_bytes(state, datasets)
        workbook = load_export_workbook(BytesIO(raw))
        titles = {
            "system_attributes": "系统属性",
            "system_mappings": "系统映射",
            "system_task_requirements": "系统任务要求",
            "formula_constraints": "派生公式模型",
            "formula_diagnostics": "公式诊断账本",
            "system_models": "系统模型",
            "regional_contexts": "规格区域进度",
            "regional_models": "规格区域模型",
            "system_learning_history": "学习收敛趋势",
            "uta_learning_coverage": "UTA学习覆盖",
            "coupling_online_models": "在线耦合辨识",
            "system_score_ledger": "系统评分账本",
            "learning_events": "学习过程",
        }
        for key, title in titles.items():
            rows = datasets.get(key) or []
            sheet = workbook.create_sheet(title)
            if not rows:
                sheet.append(["状态"])
                sheet.append(["暂无数据"])
                continue
            headers: List[str] = []
            for row in rows:
                for name in row:
                    if name not in headers:
                        headers.append(name)
            sheet.append(headers)
            for row in rows:
                sheet.append([self.serializable_cell(row.get(name)) for name in headers])
            for cell in sheet[1]:
                cell.font = Font(color="FFFFFF", bold=True)
                cell.fill = PatternFill("solid", fgColor="0F766E")
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()

    def export_frozen_model_bundle(self) -> Tuple[bytes, str]:
        from frozen_system_model import export_frozen_system_model_bundle

        return export_frozen_system_model_bundle(self, self.load_state(), HERE, HERE)

    def export(self) -> Dict[str, Any]:
        output = super().export()
        state = self.load_state()
        output["system_level"] = {
            "profile_version": SYSTEM_PROFILE_VERSION,
            "contract_version": self.system_project.contract_version,
            "source_contract_version": self.system_project.source_contract_version,
            "system_fingerprint": self.system_project.system_fingerprint,
            "system_learning_fingerprint": self.system_project.system_learning_fingerprint,
            "system_attributes": [asdict(item) for item in self.system_project.system_attributes],
            "tasks": [asdict(item) for item in self.system_project.tasks],
            "active_task": asdict(self.active_task(state)),
            "settings": self.settings_for_task(state),
            "model": ((state.get("system_models_by_task") or {}).get(self.active_task_id(state)) or state.get("system_model") or {}),
            "models_by_task": state.get("system_models_by_task") or {},
            "mappings": [asdict(item) for item in self.system_project.mappings],
            "formulas": [asdict(item) for item in self.system_project.formulas],
            "formula_calibrations": self.formula_engine.summaries(),
            "shared_model": state.get("system_shared_model") or {},
            "regional_contexts": [item.to_dict() for item in self.regional_contexts(state)],
            "regional_models": state.get("regional_models") or {},
            "transfer": self.task_transfer_profile(state, model_summary=state.get("system_shared_model") or {}),
            "method": "regional_contextual_requirement_anchored_joint_lp_uta_v13_pump",
        }
        output["method_status"]["completed_stage"] = "system_level_v13_pump"
        output["method_status"]["current_scoring"] = "regional_contextual_requirement_anchored_joint_lp_uta_v13_pump"
        return output

    def update_system_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        state = self.load_state()
        try:
            alpha_prior = float(payload.get("alphaPrior"))
            alpha_min = float(payload.get("alphaMin"))
            alpha_max = float(payload.get("alphaMax"))
        except (TypeError, ValueError) as exc:
            raise ProjectDataError("alpha默认值和范围必须是数值。") from exc
        if not 0.0 <= alpha_min <= alpha_prior <= alpha_max <= 1.0:
            raise ProjectDataError("alpha必须满足：0 <= 下限 <= 默认值 <= 上限 <= 1。")
        previous = dict(self.settings_for_task(state))
        current = {"alpha_prior": alpha_prior, "alpha_min": alpha_min, "alpha_max": alpha_max}
        state["system_shared_settings"] = current
        state["system_settings_by_task"] = {
            task.task_id: dict(current) for task in self.system_project.tasks
        }
        if current != previous:
            self.retrain_preference_models(state)
            state["learning_events"].append(
                {
                    "id": f"E-{len(state['learning_events']) + 1:03d}",
                    "created_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                    "type": "system_alpha_settings_changed",
                    "title": "系统组权重设置已更新",
                    "detail": f"共享alpha先验由{previous['alpha_prior']:.2f}改为{alpha_prior:.2f}，范围为[{alpha_min:.2f}, {alpha_max:.2f}]；全部任务的原始判断均保留并统一重训。",
                }
            )
            self.save_state(state)
        return self.summarize(state)

    def update_active_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        state = self.load_state()
        task_id = str(payload.get("taskId") or "").strip()
        if task_id not in self.system_project.task_by_id():
            raise ProjectDataError(f"找不到系统任务“{task_id}”。")
        previous = self.active_task_id(state)
        if task_id != previous:
            state["active_system_task_id"] = task_id
            state["active_review_id"] = None
            if not self.shared_preference_evidence(state):
                self._initialize_regional_contexts(state)
            else:
                target_task = self.active_task(state)
                progress = self.regional_progress(state)
                candidates = [item for item in progress if not item["complete"]] or progress
                if candidates:
                    nearest = min(
                        candidates,
                        key=lambda item: self.region_planner.context_distance(
                            target_task, task_from_dict(item["calibration_task"])
                        ),
                    )
                    state["active_region_id"] = nearest["region_id"]
            shared = state.get("system_shared_model") or state.get("system_model") or {}
            active_summary = self.contextual_summary_for_task(state, self.training_task(state))
            state["system_model"] = dict(active_summary)
            state["uta_model"] = dict(active_summary)
            self.refresh_task_model_views(state, shared)
            default_base = self.find_scheme(state, state.get("current_best_id")) or state["existing_samples"][0]
            base = self.comparison_base(state, default_base)
            candidate = self.select_candidate(state, base)
            state["known_schemes"].append(candidate)
            state["active_pair"] = {"a": base["id"], "b": candidate["id"]}
            state["learning_events"].append(
                {
                    "id": f"E-{len(state['learning_events']) + 1:03d}",
                    "created_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                    "type": "system_task_changed",
                    "title": "系统评价任务已切换",
                    "detail": f"任务由{previous}切换为{task_id}；共享UTA、区域模型、成品可行性和全部历史判断继续沿用，并已选择离新任务最近的未完成规格区域，无需清空重训。",
                }
            )
            self.save_state(state)
        return self.summarize(state)


LEGACY_SYSTEM_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>液压系统效能评价</title>
<style>
:root{--ink:#10233f;--muted:#56708f;--line:#cbd8e8;--soft:#f4f7fb;--teal:#0f7c75;--blue:#2563dc;--green:#e0f6e9;--warn:#fff2cf;--bad:#fde6e3}*{box-sizing:border-box}body{margin:0;background:var(--soft);color:var(--ink);font:17px/1.55 "Microsoft YaHei",Arial,sans-serif}button,input,select,textarea{font:inherit}button{border:0;border-radius:6px;background:var(--teal);color:white;padding:10px 18px;cursor:pointer;font-weight:700}button.secondary{background:white;color:var(--ink);border:1px solid var(--line)}button.danger{background:#aa3c35}.top{background:white;border-bottom:1px solid var(--line);padding:18px 28px;display:flex;justify-content:space-between;gap:18px;align-items:center}.top h1{font-size:25px;margin:0}.meta{color:var(--muted);font-size:15px}.layout{display:grid;grid-template-columns:minmax(760px,1fr) 370px;gap:16px;padding:16px;max-width:1760px;margin:auto}.panel{background:white;border:1px solid var(--line);border-radius:8px;padding:18px}.main{min-width:0}.sidebar{display:flex;flex-direction:column;gap:16px}.status{padding:12px 14px;border-radius:6px;background:var(--warn);margin-bottom:14px}.status.ok{background:var(--green)}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}.stat{border:1px solid var(--line);border-radius:6px;padding:10px;text-align:center;background:white}.stat b{display:block;font-size:24px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:16px}.scheme{border:1px solid var(--line);border-radius:8px;overflow:hidden;background:white}.scheme-head{padding:16px 18px;border-bottom:1px solid var(--line)}.scheme-head h2{font-size:22px;margin:0 0 4px}.tag{display:inline-block;background:var(--green);border-radius:20px;padding:2px 10px;font-size:14px}.sysbox{padding:14px 18px;background:#eef7f6;border-bottom:1px solid var(--line)}.scoreline{display:flex;gap:20px;align-items:baseline;flex-wrap:wrap}.scoreline strong{font-size:25px;color:var(--teal)}.sysrow,.paramrow{display:grid;grid-template-columns:minmax(160px,1fr) auto;gap:12px;padding:10px 18px;border-bottom:1px solid #e5ecf4;min-height:66px;align-items:center}.sysrow{padding:7px 0;min-height:48px}.value{text-align:right;font-variant-numeric:tabular-nums}.unit{font-size:14px;color:var(--muted)}.why{padding:12px 18px;background:#f8fafc;min-height:82px}.feedback{margin-top:16px}.feedback-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.choice{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}.choice label{border:1px solid var(--line);border-radius:6px;padding:8px 12px;cursor:pointer}.choice input{margin-right:6px}.preference{border-top:1px solid var(--line);margin-top:14px;padding-top:14px}.reason{width:100%;min-height:72px;border:1px solid var(--line);border-radius:6px;padding:9px}.actions{display:flex;gap:10px;margin-top:14px;align-items:center}.message{color:var(--teal);font-weight:700}.panel h2,.panel h3{margin:0 0 12px}.setting{margin-bottom:12px}.setting label{display:block;font-weight:700;margin-bottom:4px}.setting input,.setting select{width:100%;border:1px solid var(--line);border-radius:6px;padding:8px;background:white}.alpha-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}.alpha-grid label{font-size:14px}.model-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.metric{padding:8px;background:#f6f8fb;border-radius:5px}.metric b{display:block;font-size:20px}.eval-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.eval-grid label{display:block;font-size:14px}.eval-grid input{width:100%;border:1px solid var(--line);border-radius:6px;padding:7px}.result{margin-top:12px;border-top:1px solid var(--line);padding-top:12px}.contrib{display:grid;grid-template-columns:1fr auto;gap:8px;padding:5px 0}.positive{color:#08715f}.negative{color:#a33b35}details{border-top:1px solid var(--line);padding-top:10px;margin-top:10px}summary{cursor:pointer;font-weight:700}.reviews .review{border-top:1px solid var(--line);padding:10px 0}.small-actions button{padding:5px 9px;font-size:14px}.warning{background:var(--warn);padding:8px;border-radius:5px;margin-top:8px}@media(max-width:1150px){.layout{grid-template-columns:1fr}.sidebar{display:grid;grid-template-columns:1fr 1fr}}@media(max-width:760px){body{font-size:16px}.layout{padding:8px}.pair,.feedback-grid,.sidebar{grid-template-columns:1fr}.stats{grid-template-columns:1fr 1fr}.top{padding:14px}.sysrow,.paramrow{grid-template-columns:1fr auto}}
</style></head><body>
<header class="top"><div><h1>液压系统效能评价</h1><div class="meta" id="projectMeta">正在载入...</div></div><div><button class="secondary" id="resetBtn">清空本项目判断</button></div></header>
<div class="layout"><main class="main"><div id="completion" class="status"></div><div id="stats" class="stats"></div><section class="pair"><article class="scheme" id="schemeA"></article><article class="scheme" id="schemeB"></article></section>
<section class="panel feedback"><h2>本轮专家判断</h2><div class="meta">先判断泵方案本身能否实现；两边都可行时，再判断哪套方案对当前液压系统总体效能更有利。</div><form id="feedbackForm"><div class="feedback-grid"><div><h3>方案 A 可行性</h3><div class="choice" id="feasA"></div><textarea class="reason" id="reasonA" placeholder="不可行时可补充参数或耦合原因"></textarea></div><div><h3>方案 B 可行性</h3><div class="choice" id="feasB"></div><textarea class="reason" id="reasonB" placeholder="不可行时可补充参数或耦合原因"></textarea></div></div><div class="preference"><h3>哪套方案对液压系统总体效能更有利？</h3><div class="choice" id="pref"></div></div><div class="actions"><button type="submit">保存并生成下一组</button><span class="message" id="message"></span></div></form></section></main>
<aside class="sidebar"><section class="panel"><h2>系统任务</h2><div class="setting"><label for="taskSelect">当前液压系统任务</label><select id="taskSelect"></select></div><div id="taskInfo" class="meta"></div></section>
<section class="panel"><h2>组权重设置</h2><div class="meta">这是先验和允许学习的范围。已有判断不会删除，修改后会自动重新训练。</div><div class="alpha-grid"><label>下限<input id="alphaMin" type="number" min="0" max="1" step="0.05"></label><label>默认值<input id="alphaPrior" type="number" min="0" max="1" step="0.05"></label><label>上限<input id="alphaMax" type="number" min="0" max="1" step="0.05"></label></div><button id="alphaSave" style="margin-top:10px">应用并重训</button><div id="alphaResult" class="meta" style="margin-top:8px"></div></section>
<section class="panel"><h2>模型状态</h2><div id="modelMetrics" class="model-grid"></div><details><summary>查看算法口径</summary><div id="modelDetail" class="meta"></div></details></section>
<section class="panel"><h2>未知方案评估</h2><div id="evalGrid" class="eval-grid"></div><button id="evalBtn" style="margin-top:10px">评估该方案</button><div id="evalResult"></div></section>
<section class="panel reviews"><h2>需要复核 <span id="reviewCount"></span></h2><div id="reviews"></div></section>
<section class="panel"><details><summary>后台学习记录</summary><div id="events"></div></details></section></aside></div>
<script>
let state=null;const esc=x=>String(x??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
async function api(path,options={}){const r=await fetch(path,options);let data=null;try{data=await r.json()}catch(_){throw new Error('服务器返回格式错误')}if(!r.ok)throw new Error(data.error||'请求失败');return data}
function choices(target,name,items){document.getElementById(target).innerHTML=items.map(([v,t])=>`<label><input type="radio" name="${name}" value="${v}">${t}</label>`).join('')}
function assessment(s){return s.prediction?.system_assessment||{direct_assessment:{attributes:[]},top_contributors:[]}}
function renderScheme(s,letter){const sys=assessment(s),g=s.generation_explanation||{},direct=sys.direct_assessment?.attributes||[];const sysrows=direct.map(x=>`<div class="sysrow"><div>${esc(x.system_attribute)}<div class="unit">${esc(x.requirement_text)}</div></div><div class="value">${esc(x.value)} ${esc(x.unit)}<div class="unit">单项 ${esc(x.score)} 分</div></div></div>`).join('');const rows=Object.entries(state.params).map(([key,p])=>`<div class="paramrow"><div>${esc(p.label)}<div class="unit">第 ${esc(p.design_stage)} 批 · ${esc(p.unit)}</div></div><div class="value">${esc(s.params[key])}</div></div>`).join('');const purpose=g.system_purpose||g.purpose||'已有历史方案';return `<div class="scheme-head"><h2>方案 ${letter} · ${esc(s.id)}</h2><span class="tag">${esc(s.prediction?.status?state.labels.status[s.prediction.status]||s.prediction.status:'待评估')}</span></div><div class="sysbox"><div class="scoreline"><strong>${esc(sys.total_score)} 分</strong><span>任务适配 ${esc(sys.direct_score)} · 固有性能 ${esc(sys.product_score)}</span></div>${sysrows}</div><div class="why"><b>本轮作用：</b>${esc(purpose)}<br><span class="meta">${esc(g.detail||s.source_detail||'历史样本')}</span></div>${rows}`}
function renderEval(result){const r=result.result||result,sys=r.system_assessment||{},direct=sys.direct_assessment?.attributes||[],contrib=sys.top_contributors||[];return `<div class="result"><div class="scoreline"><strong>${esc(sys.total_score)} 分</strong><span>可信度 ${Math.round(Number(sys.confidence||0)*100)}%</span></div><div class="meta">当前任务适配 ${esc(sys.direct_score)} 分；成品固有性能 ${esc(sys.product_score)} 分；学习后 alpha=${esc(sys.alpha)}</div><h3>系统要求</h3>${direct.map(x=>`<div class="contrib"><span>${esc(x.system_attribute)}：${esc(x.value)} ${esc(x.unit)}<br><span class="meta">${esc(x.requirement_text)}</span></span><b class="${x.satisfied===false?'negative':'positive'}">${esc(x.score)}分</b></div>`).join('')}<h3>主要加减分</h3>${contrib.map(x=>`<div class="contrib"><span>${esc(x.label)} · ${x.channel==='direct_system'?'任务适配':'固有性能'}</span><b class="${Number(x.contribution)>=0?'positive':'negative'}">${Number(x.contribution)>=0?'+':''}${esc(x.contribution)}</b></div>`).join('')||'<div class="meta">暂无可解释贡献。</div>'}</div>`}
function renderReview(x){return `<div class="review"><b>${esc(x.id)}</b> ${esc(x.message)}<div class="small-actions"><button onclick="review('${esc(x.id)}','keep')">保留</button> <button class="secondary" onclick="review('${esc(x.id)}','tie')">改为相当</button> <button class="secondary" onclick="review('${esc(x.id)}','reverse')">反向</button> <button class="danger" onclick="review('${esc(x.id)}','withdraw')">撤回</button></div></div>`}
function render(s){state=s;const c=s.learningCompletion||{},sys=s.system||{},m=sys.model||{},set=sys.settings||{},task=sys.active_task||{};document.getElementById('projectMeta').textContent=`${s.project.name} · ${task.task_name||''}`;const box=document.getElementById('completion');box.className=`status ${c.complete?'ok':''}`;box.innerHTML=`<b>${esc(c.title)}</b><br>${esc(c.message)}`;document.getElementById('stats').innerHTML=[['历史方案',s.stats.existingSchemes],['全部判断',s.stats.interactions],['当前任务有效对比',m.evidence_pairs||0],['需要复核',s.stats.openReviews]].map(x=>`<div class="stat"><b>${esc(x[1])}</b>${esc(x[0])}</div>`).join('');document.getElementById('schemeA').innerHTML=renderScheme(s.pair.a,'A');document.getElementById('schemeB').innerHTML=renderScheme(s.pair.b,'B');choices('feasA','feasibilityA',[['feasible','可以实现'],['infeasible','不能实现'],['uncertain','暂时看不准']]);choices('feasB','feasibilityB',[['feasible','可以实现'],['infeasible','不能实现'],['uncertain','暂时看不准']]);choices('pref','preference',[['A','方案 A 更好'],['B','方案 B 更好'],['tie','两个方案差不多'],['unknown','暂时无法比较']]);document.getElementById('taskSelect').innerHTML=sys.tasks.map(x=>`<option value="${esc(x.task_id)}" ${x.task_id===sys.active_task_id?'selected':''}>${esc(x.task_name)}</option>`).join('');document.getElementById('taskInfo').innerHTML=`工况：${esc(task.operating_condition||'未填写')}<br>压力目标：${esc(task.pressure_target??'未填写')}；流量下限：${esc(task.flow_minimum??'未填写')}；重量预算：${esc(task.weight_budget??'未填写（相对模式）')}`;document.getElementById('alphaMin').value=set.alpha_min;document.getElementById('alphaPrior').value=set.alpha_prior;document.getElementById('alphaMax').value=set.alpha_max;document.getElementById('alphaResult').textContent=`当前学习结果 alpha=${Number(m.learned_alpha??set.alpha_prior).toFixed(3)}`;document.getElementById('modelMetrics').innerHTML=[['学习后 alpha',m.learned_alpha??set.alpha_prior],['有效对比',m.evidence_pairs||0],['训练正确率',m.training_accuracy==null?'暂无':Math.round(m.training_accuracy*100)+'%'],['测试正确率',m.test_accuracy==null?'暂无':Math.round(m.test_accuracy*100)+'%'],['一致性',m.judgment_consistency==null?'暂无':Math.round(m.judgment_consistency*100)+'%'],['UTA分段',m.segments||3]].map(x=>`<div class="metric"><b>${esc(x[1])}</b>${esc(x[0])}</div>`).join('');document.getElementById('modelDetail').innerHTML=`方法：${esc(m.model_name||'需求锚定双通道层次化LP-UTA')}<br>alpha先验/范围：${esc(m.alpha_prior)} / [${esc(m.alpha_min)}, ${esc(m.alpha_max)}]<br>其他性能属性数：${esc(m.attribute_count||0)}；增量变量：${esc(m.utility_increment_variables||0)}<br>说明：压力、流量、重量使用任务要求锚定；其他属性使用历史样本中位数作经验中性参考。`;const base=s.pair.a.params;document.getElementById('evalGrid').innerHTML=Object.entries(s.params).map(([key,p])=>`<label>${esc(p.label)} (${esc(p.unit)})<input id="eval_${esc(key)}" type="${p.data_type==='categorical'?'text':'number'}" step="${p.precision===0?'1':'any'}" value="${esc(base[key])}"></label>`).join('');const reviews=s.reviewEvidence||[];document.getElementById('reviewCount').textContent=`(${reviews.filter(x=>x.status==='open').length})`;document.getElementById('reviews').innerHTML=reviews.filter(x=>x.status==='open').map(renderReview).join('')||'<div class="meta">当前没有待复核判断。</div>';document.getElementById('events').innerHTML=(s.learningEvents||[]).map(x=>`<p><b>${esc(x.title)}</b><br><span class="meta">${esc(x.detail)}</span></p>`).join('')||'<div class="meta">暂无记录。</div>'}
async function load(){try{render(await api('/api/state'))}catch(e){document.getElementById('projectMeta').textContent=e.message}}
document.getElementById('feedbackForm').onsubmit=async e=>{e.preventDefault();const a=document.querySelector('input[name="feasibilityA"]:checked'),b=document.querySelector('input[name="feasibilityB"]:checked'),p=document.querySelector('input[name="preference"]:checked');if(!a||!b){document.getElementById('message').textContent='请先判断两套方案能否实现。';return}const preference=a.value==='feasible'&&b.value==='feasible'?(p?.value||'unknown'):'unknown';try{render(await api('/api/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({feasibilityA:a.value,feasibilityB:b.value,preference,reasonA:document.getElementById('reasonA').value,reasonB:document.getElementById('reasonB').value,reasonCodesA:[],reasonCodesB:[]})}));document.getElementById('message').textContent='已保存，下一组已生成。'}catch(err){document.getElementById('message').textContent=err.message}}
document.getElementById('alphaSave').onclick=async()=>{try{render(await api('/api/system-settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({alphaMin:Number(document.getElementById('alphaMin').value),alphaPrior:Number(document.getElementById('alphaPrior').value),alphaMax:Number(document.getElementById('alphaMax').value)})}))}catch(e){alert(e.message)}};
document.getElementById('taskSelect').onchange=async e=>{try{render(await api('/api/active-task',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({taskId:e.target.value})}))}catch(err){alert(err.message)}};
document.getElementById('evalBtn').onclick=async()=>{const p={};Object.keys(state.params).forEach(k=>p[k]=document.getElementById(`eval_${k}`).value);try{document.getElementById('evalResult').innerHTML=renderEval(await api('/api/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)}))}catch(e){document.getElementById('evalResult').innerHTML=`<div class="warning">${esc(e.message)}</div>`}};
async function review(id,action){try{render(await api('/api/review-action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reviewId:id,action})}))}catch(e){alert(e.message)}}
document.getElementById('resetBtn').onclick=async()=>{if(confirm('确定清空当前系统级判断并重新开始吗？成品Excel不会改变。'))render(await api('/api/reset',{method:'POST'}))};load();
</script></body></html>"""

HTML = (HERE / "system_ui.html").read_text(encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    app: SystemProjectApp

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _html(self) -> None:
        data = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/":
                self._html()
            elif path == "/api/state":
                json_response(self, self.app.summarize())
            elif path == "/api/export":
                json_response(self, self.app.export())
            elif path == "/api/export-bundle":
                data, filename = self.app.export_bundle()
                binary_response(self, data, "application/zip", filename)
            elif path == "/api/export-model":
                data, filename = self.app.export_frozen_model_bundle()
                binary_response(self, data, "application/zip", filename)
            elif path == "/api/health":
                json_response(self, {"status": "ok", "mode": "system_level", "version": SYSTEM_PROFILE_VERSION})
            else:
                json_response(self, {"error": "页面不存在。"}, 404)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 500)

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            body = read_body(self)
            if path == "/api/submit":
                result = self.app.submit(body)
            elif path == "/api/evaluate":
                result = self.app.evaluate_unknown(body)
            elif path == "/api/system-settings":
                result = self.app.update_system_settings(body)
            elif path == "/api/preference-settings":
                result = self.app.update_preference_settings(body)
            elif path == "/api/active-task":
                result = self.app.update_active_task(body)
            elif path == "/api/generation-ranges":
                result = self.app.update_generation_ranges(body)
            elif path == "/api/feasibility-slice":
                result = self.app.feasibility_slice(body)
            elif path == "/api/review-action":
                result = self.app.review_action(body)
            elif path == "/api/review-open":
                result = self.app.open_review_pair(body)
            elif path == "/api/reset":
                result = self.app.summarize(self.app.load_state(reset=True))
            else:
                json_response(self, {"error": "接口不存在。"}, 404)
                return
            json_response(self, result)
        except (ProjectDataError, ValueError) as exc:
            json_response(self, {"error": str(exc)}, 400)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 500)


def choose_workbook() -> Optional[Path]:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        selected = filedialog.askopenfilename(
            title="选择已经升级的系统级Excel",
            filetypes=[("Excel工作簿", "*.xlsx"), ("所有文件", "*.*")],
        )
        root.destroy()
        return Path(selected).resolve() if selected else None
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="系统级效能评价V13泵特化全域多工况应用")
    parser.add_argument("workbook", nargs="?", help="含系统映射配置和系统任务要求的Excel")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--state-dir", default=str(HERE / "state"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--upgrade", metavar="PRODUCT_XLSX", help="仅把成品级Excel复制升级为系统级Excel")
    parser.add_argument("--output", help="--upgrade的输出文件")
    args = parser.parse_args()
    if args.upgrade:
        if not args.output:
            raise SystemExit("使用--upgrade时必须同时提供--output新文件路径。")
        output = upgrade_product_workbook(args.upgrade, args.output)
        print(f"系统级Excel已生成：{output}")
        return
    workbook = Path(args.workbook).resolve() if args.workbook else choose_workbook()
    if workbook is None:
        raise SystemExit("未选择系统级Excel，程序已退出。")
    app = SystemProjectApp(workbook, Path(args.state_dir))
    Handler.app = app
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"系统级效能评价V13泵特化版已启动：{url}")
    print(f"项目：{app.project.project_name}")
    print(f"任务：{app.system_project.default_task().task_name}")
    if not args.no_browser and os.environ.get("SYSTEM_APP_NO_BROWSER") != "1":
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
