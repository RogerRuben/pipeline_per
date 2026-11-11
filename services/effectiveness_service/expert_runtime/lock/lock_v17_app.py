# -*- coding: utf-8 -*-
"""Lock-product V17: baseline degradation, UTA learning and reuse decisions."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import random
import socket
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import numpy as np

from lock_utility_model import (
    BaselineDegradationSpace,
    LockPreferenceLearner,
    clamp,
    evaluate_migrated_reuse_model,
    evaluate_reuse_model,
    fit_combined_reuse_threshold,
    reanchored_effectiveness_score,
)
from preference_models import (
    DEFAULT_UTA_SEGMENTS,
    MAX_UTA_SEGMENTS,
    MIN_UTA_SEGMENTS,
    OnlineBTModel,
    validate_segment_count,
)
from project_excel import ProjectDataError, ProjectDataset, load_project_workbook


HERE = Path(__file__).resolve().parent
DEFAULT_WORKBOOK = HERE / "data" / "product_v17_lock_demo.xlsx"
DEFAULT_STATE_DIR = HERE / "state_v17_lock"
DEFAULT_PORT = 8798
PROFILE_VERSION = 17
STATE_CONTRACT = "lock_product_degradation_v17"
MODEL_FORMAT = "lock-product-reuse-model-17.0"
PROBE_LEVELS = (0.10, 0.22, 0.38, 0.58, 0.78, 1.0)
BOUNDARY_TOLERANCE = 0.035
MULTI_REUSABLE_FACTORS = (0.24, 0.42, 0.60, 0.78, 0.94)
MULTI_MIN_REUSABLE_D = 1e-8

REUSE_LABELS = {
    "reusable": "仍可复用",
    "not_reusable": "已不可复用",
    "uncertain": "暂时难判断",
}
PREFERENCE_OPTIONS = {
    "A_strong": ("A", 1.0, "A明显更值得复用"),
    "A_slight": ("A", 0.62, "A略好"),
    "tie": ("tie", 0.80, "两者差不多"),
    "B_slight": ("B", 0.62, "B略好"),
    "B_strong": ("B", 1.0, "B明显更值得复用"),
    "unknown": ("unknown", 0.0, "暂时无法比较"),
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def json_response(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def html_response(handler: BaseHTTPRequestHandler, text: str) -> None:
    data = text.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def binary_response(handler: BaseHTTPRequestHandler, data: bytes, filename: str) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "application/octet-stream")
    handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def read_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def stable_signature(params: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class LockV17App:
    def __init__(
        self,
        workbook_path: Path | str,
        state_dir: Path | str = DEFAULT_STATE_DIR,
        seed: int = 20260827,
    ) -> None:
        self.seed = int(seed)
        self.state_root = Path(state_dir).expanduser().resolve()
        self.configure_project(load_project_workbook(workbook_path, ignore_couplings=True))

    def configure_project(self, project: ProjectDataset) -> None:
        if not project.requirement_profiles:
            raise ProjectDataError("V17 锁特化版必须在 Excel 中至少填写一个“基准技术协议”。")
        fingerprint_payload = {
            "product_code": project.product_code,
            "attributes": [
                {
                    "key": item.key,
                    "label": item.label,
                    "unit": item.unit,
                    "data_type": item.data_type,
                    "design_stage": item.design_stage,
                    "precision": item.precision,
                    "generation_min": item.generation_min,
                    "generation_max": item.generation_max,
                    "preference_direction": item.preference_direction,
                    "marginal_trend": item.marginal_trend,
                    "participates_utility": item.participates_utility,
                    "participates_generation": item.participates_generation,
                }
                for item in project.attributes
            ],
            # Only the first profile defines the learned coordinate system.
            # Additional profiles are evaluation anchors and must not move the
            # expert to a fresh State when they are added or edited.
            "training_master_profile": [
                {
                    "id": profile.id,
                    "name": profile.name,
                    "requirements": [
                        {
                            "attribute_key": requirement.attribute_key,
                            "target_value": requirement.target_value,
                            "minimum": requirement.minimum,
                            "maximum": requirement.maximum,
                        }
                        for requirement in profile.requirements
                    ],
                }
                for profile in project.requirement_profiles[:1]
            ],
        }
        normalized = json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        project.learning_fingerprint = "lock_v17_%s" % hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest()[:24]
        self.project = project
        self.attribute_by_key = project.attribute_by_key()
        self.profile_by_id = project.requirement_profile_by_id()
        self.state_path = self.state_root / f"state_{project.learning_fingerprint}.json"
        self._spaces: Dict[str, BaselineDegradationSpace] = {}

    def space(self, profile_id: str) -> BaselineDegradationSpace:
        if profile_id not in self.profile_by_id:
            raise ProjectDataError(f"未找到基准技术协议“{profile_id}”。")
        if profile_id not in self._spaces:
            self._spaces[profile_id] = BaselineDegradationSpace(
                self.project,
                self.profile_by_id[profile_id],
            )
        return self._spaces[profile_id]

    def _new_boundary(self, dimension: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "dimension_id": dimension["id"],
            "attribute_key": dimension["attribute_key"],
            "attribute_label": dimension["attribute_label"],
            "side": dimension["side"],
            "side_label": dimension["side_label"],
            "unit": dimension["unit"],
            "baseline_value": dimension["baseline_value"],
            "bad_edge": dimension["bad_edge"],
            "accepted_d": 0.0,
            "rejected_d": None,
            "limit_d": None,
            "status": "pending",
            "observations": [],
            "uncertain_count": 0,
        }

    def _new_session(self, profile_id: str, uta_segments: int = DEFAULT_UTA_SEGMENTS) -> Dict[str, Any]:
        space = self.space(profile_id)
        session: Dict[str, Any] = {
            "profile_id": profile_id,
            "created_at": now_iso(),
            "phase": "single",
            "single_cursor": 0,
            "single_boundaries": {
                item["id"]: self._new_boundary(item) for item in space.dimensions
            },
            "single_order": [item["id"] for item in space.dimensions],
            "multi_round": 0,
            "multi_attribute_counts": {item.key: 0 for item in space.attributes},
            "multi_bin_counts": {
                item["id"]: [0 for _ in MULTI_REUSABLE_FACTORS]
                for item in space.dimensions
            },
            "multi_signatures": [],
            "interactions": [],
            "reuse_evidence": [],
            "preference_evidence": [],
            "review_items": [],
            "review_history": [],
            "review_acknowledgements": [],
            "comparison_quality": {"stability_checks": []},
            "uta_segments": int(uta_segments),
            "bt_weights": [],
            "bt_model": {},
            "uta_increments": [],
            "uta_model": {},
            "combined_reuse_threshold": {
                "status": "insufficient_classes",
                "threshold": None,
                "evidence_count": 0,
            },
            "active_pair": None,
            "unknown_evaluations": [],
            "learning_events": [
                {
                    "id": "E-001",
                    "created_at": now_iso(),
                    "title": "锁特化训练已开始",
                    "detail": (
                        f"基准“{space.profile.name}”固定为100分；先逐项学习"
                        f"{len(space.dimensions)} 个单向退化边界，不采集物理可行性或耦合证据。"
                    ),
                }
            ],
        }
        self._prepare_next_pair(session)
        return session

    def _initial_state(self) -> Dict[str, Any]:
        profile = self.project.default_requirement_profile() or self.project.requirement_profiles[0]
        state = {
            "profile_version": PROFILE_VERSION,
            "state_contract": STATE_CONTRACT,
            "project_name": self.project.project_name,
            "workbook_path": self.project.workbook_path,
            "workbook_fingerprint": self.project.workbook_fingerprint,
            "learning_fingerprint": self.project.learning_fingerprint,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "master_profile_id": profile.id,
            "active_profile_id": profile.id,
            "sessions": {},
        }
        state["sessions"][profile.id] = self._new_session(profile.id)
        self.save_state(state)
        return state

    def load_state(self, reset: bool = False) -> Dict[str, Any]:
        if reset or not self.state_path.exists():
            return self._initial_state()
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._initial_state()
        if (
            state.get("profile_version") != PROFILE_VERSION
            or state.get("state_contract") != STATE_CONTRACT
            or state.get("learning_fingerprint") != self.project.learning_fingerprint
        ):
            return self._initial_state()
        default_profile_id = self.project.requirement_profiles[0].id
        if state.get("master_profile_id") not in self.profile_by_id:
            state["master_profile_id"] = default_profile_id
        if state.get("active_profile_id") not in self.profile_by_id:
            state["active_profile_id"] = state["master_profile_id"]
        master_profile_id = state["master_profile_id"]
        if master_profile_id not in state.setdefault("sessions", {}):
            state["sessions"][master_profile_id] = self._new_session(master_profile_id)
        return state

    def save_state(self, state: Dict[str, Any]) -> None:
        state["updated_at"] = now_iso()
        state["workbook_path"] = self.project.workbook_path
        state["workbook_fingerprint"] = self.project.workbook_fingerprint
        state["learning_fingerprint"] = self.project.learning_fingerprint
        self.state_root.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(state, ensure_ascii=False, indent=2)
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(serialized, encoding="utf-8")
        temporary.replace(self.state_path)
        if str(os.environ.get("EFFECTIVENESS_EXPERT_PACKAGE", "0")).lower() in {"1", "true", "yes", "on"}:
            digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            session = self.active_session(state)
            root = self.state_root / "state_history" / self.project.learning_fingerprint
            root.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            count = len(session.get("interactions") or [])
            (root / f"state_{timestamp}_n{count:04d}_{digest[:12]}.json").write_text(
                serialized,
                encoding="utf-8",
            )

    @staticmethod
    def active_session(state: Dict[str, Any]) -> Dict[str, Any]:
        return state["sessions"][state["master_profile_id"]]

    def _dimension_for_boundary(
        self,
        session: Dict[str, Any],
        boundary: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self.space(session["profile_id"]).dimension_by_id[boundary["dimension_id"]]

    def _boundary_complete(
        self,
        boundary: Dict[str, Any],
        dimension: Dict[str, Any],
        space: BaselineDegradationSpace,
    ) -> bool:
        if boundary["rejected_d"] is not None:
            gap = float(boundary["rejected_d"]) - float(boundary["accepted_d"])
            if gap <= BOUNDARY_TOLERANCE:
                return True
            accepted = float(space.value_at(dimension["id"], boundary["accepted_d"]))
            rejected = float(space.value_at(dimension["id"], boundary["rejected_d"]))
            spec = self.attribute_by_key[dimension["attribute_key"]]
            raw_resolution = 1.0 if spec.data_type == "integer" else 10.0 ** (-int(spec.precision))
            return abs(rejected - accepted) <= 1.5 * raw_resolution
        return float(boundary["accepted_d"]) >= 1.0 - 1e-9

    @staticmethod
    def _finish_boundary(boundary: Dict[str, Any]) -> None:
        if boundary["rejected_d"] is None:
            boundary["status"] = "full_range_reusable"
            boundary["limit_d"] = 1.0
        else:
            boundary["status"] = "learned"
            boundary["limit_d"] = round(
                0.5 * (float(boundary["accepted_d"]) + float(boundary["rejected_d"])),
                6,
            )

    def _next_probe_d(self, boundary: Dict[str, Any]) -> float:
        accepted = float(boundary["accepted_d"])
        rejected = boundary["rejected_d"]
        observations = boundary.get("observations") or []
        previous = float(observations[-1]["deterioration"]) if observations else None
        if rejected is not None:
            candidate = 0.5 * (accepted + float(rejected))
            if previous is not None and abs(candidate - previous) < 1e-6:
                candidate = accepted + 0.35 * (float(rejected) - accepted)
            return clamp(candidate, 0.0, 1.0)
        # Rounded engineering values can turn a nominal 0.22 probe into an
        # actual 0.21875 deterioration.  Require a visible forward step so the
        # staircase cannot keep regenerating the same raw value.
        for level in PROBE_LEVELS:
            if level > accepted + 0.005 and (previous is None or abs(level - previous) > 0.005):
                return float(level)
        return 1.0

    def _single_pair(self, session: Dict[str, Any]) -> Dict[str, Any]:
        order = session["single_order"]
        while session["single_cursor"] < len(order):
            boundary = session["single_boundaries"][order[session["single_cursor"]]]
            if boundary["status"] in {"learned", "full_range_reusable", "unresolved"}:
                session["single_cursor"] += 1
                continue
            break
        if session["single_cursor"] >= len(order):
            session["phase"] = "multi"
            return self._multi_pair(session)
        boundary = session["single_boundaries"][order[session["single_cursor"]]]
        boundary["status"] = "probing"
        space = self.space(session["profile_id"])
        probe_d = self._next_probe_d(boundary)
        params_a = copy.deepcopy(space.baseline_params)
        params_b = space.scheme_from_changes({boundary["dimension_id"]: probe_d})
        actual_value = float(params_b[boundary["attribute_key"]])
        dimension = space.dimension_by_id[boundary["dimension_id"]]
        actual_d = space.deterioration_for_dimension(actual_value, dimension)
        if boundary.get("rejected_d") is None and actual_d <= float(boundary["accepted_d"]) + 1e-8:
            # Coarse engineering precision can map two adjacent normalized
            # probes to the same displayed value.  Skip forward until the
            # expert is shown a genuinely different方案 instead of exhausting
            # the retry budget on a duplicate.
            forward_levels = [
                level for level in (*PROBE_LEVELS, 1.0) if level > probe_d + 1e-8
            ]
            for forward_d in forward_levels:
                forward_params = space.scheme_from_changes(
                    {boundary["dimension_id"]: float(forward_d)}
                )
                forward_value = float(forward_params[boundary["attribute_key"]])
                forward_actual_d = space.deterioration_for_dimension(
                    forward_value,
                    dimension,
                )
                if forward_actual_d > float(boundary["accepted_d"]) + 1e-8:
                    probe_d = float(forward_d)
                    params_b = forward_params
                    actual_value = forward_value
                    actual_d = forward_actual_d
                    break
        index = len(session["interactions"]) + 1
        return {
            "stage": "single",
            "question_id": f"SINGLE-{index:04d}",
            "dimension_id": boundary["dimension_id"],
            "probe_d": round(actual_d, 8),
            "a": self._scheme_payload(
                f"BASE-{session['profile_id']}",
                "基准100分",
                params_a,
                {},
                space,
            ),
            "b": self._scheme_payload(
                f"S-{index:04d}",
                f"仅降低“{boundary['attribute_label']}”",
                params_b,
                {boundary["dimension_id"]: actual_d},
                space,
            ),
            "instruction": (
                f"本轮只改变“{boundary['attribute_label']}”，请判断退化后的指标是否仍能接受并复用。"
            ),
        }

    def _confirmed_reusable_limit(
        self,
        session: Dict[str, Any],
        dimension_id: str,
    ) -> float:
        boundary = session["single_boundaries"][dimension_id]
        if boundary.get("status") not in {"learned", "full_range_reusable"}:
            return 0.0
        return clamp(float(boundary.get("accepted_d") or 0.0), 0.0, 1.0)

    def _dimension_candidates_by_attribute(self, session: Dict[str, Any]) -> Dict[str, List[str]]:
        output: Dict[str, List[str]] = {}
        for dimension_id in session["single_order"]:
            boundary = session["single_boundaries"][dimension_id]
            if self._confirmed_reusable_limit(session, dimension_id) <= MULTI_MIN_REUSABLE_D:
                continue
            output.setdefault(boundary["attribute_key"], []).append(dimension_id)
        return output

    def _adaptive_sampling_shares(
        self,
        session: Dict[str, Any],
        by_attribute: Dict[str, List[str]],
    ) -> Dict[str, float]:
        keys = list(by_attribute)
        if not keys:
            return {}
        widths = {
            key: max(
                0.01,
                sum(
                    self._confirmed_reusable_limit(session, dimension_id)
                    for dimension_id in by_attribute[key]
                )
                / max(len(by_attribute[key]), 1),
            )
            for key in keys
        }
        breadth = {key: math.sqrt(value) for key, value in widths.items()}
        breadth_total = sum(breadth.values())
        uniform = 0.55 / len(keys)
        return {
            key: uniform + 0.45 * breadth[key] / max(breadth_total, 1e-12)
            for key in keys
        }

    def _attribute_sampling_priority(
        self,
        session: Dict[str, Any],
        key: str,
        dimensions: Sequence[str],
        share: float,
        jitter: float,
    ) -> float:
        counts = session["multi_attribute_counts"]
        total = sum(int(value) for value in counts.values())
        current = int(counts.get(key, 0))
        target = (total + max(len(counts), 1)) * float(share)
        minimum_deficit = max(0, 6 - current)
        bins = session.get("multi_bin_counts") or {}
        bin_scarcity = sum(
            sum(1.0 / (1.0 + int(value)) for value in bins.get(dimension_id, []))
            / max(len(MULTI_REUSABLE_FACTORS), 1)
            for dimension_id in dimensions
        ) / max(len(dimensions), 1)
        return 2.5 * minimum_deficit + (target - current) + 0.45 * bin_scarcity + 0.9 * jitter

    def _reusable_multi_deterioration(
        self,
        session: Dict[str, Any],
        space: BaselineDegradationSpace,
        dimension_id: str,
        factor: float,
    ) -> float:
        confirmed_limit = self._confirmed_reusable_limit(session, dimension_id)
        requested = clamp(confirmed_limit * float(factor), 0.0, confirmed_limit)
        dimension = space.dimension_by_id[dimension_id]
        actual = space.deterioration_for_dimension(
            float(space.value_at(dimension_id, requested)),
            dimension,
        )
        if actual <= MULTI_MIN_REUSABLE_D:
            actual = space.deterioration_for_dimension(
                float(space.value_at(dimension_id, confirmed_limit)),
                dimension,
            )
        if actual > confirmed_limit + 1e-8:
            raise ProjectDataError(
                f"“{dimension['attribute_label']}”多项退化值越过已确认的单项可复用边界。"
            )
        return round(actual, 8)

    def _generate_multi_changes(
        self,
        session: Dict[str, Any],
        rng: random.Random,
        offset: int,
    ) -> Dict[str, float]:
        by_attribute = self._dimension_candidates_by_attribute(session)
        space = self.space(session["profile_id"])
        keys = list(by_attribute)
        if not keys:
            return {}
        shares = self._adaptive_sampling_shares(session, by_attribute)
        jitter = {key: rng.random() for key in keys}
        ordered = sorted(
            keys,
            key=lambda key: self._attribute_sampling_priority(
                session,
                key,
                by_attribute[key],
                shares[key],
                jitter[key],
            ),
            reverse=True,
        )
        size = min(len(ordered), 2 + ((session["multi_round"] + offset) % 2))
        selected = ordered[:size]
        changes: Dict[str, float] = {}
        for index, key in enumerate(selected):
            dimensions = by_attribute[key]
            dimension_id = dimensions[(session["multi_round"] + offset + index) % len(dimensions)]
            bin_counts = (session.get("multi_bin_counts") or {}).get(
                dimension_id,
                [0 for _ in MULTI_REUSABLE_FACTORS],
            )
            bin_jitter = [rng.random() for _ in MULTI_REUSABLE_FACTORS]
            bin_index = min(
                range(len(MULTI_REUSABLE_FACTORS)),
                key=lambda value: (int(bin_counts[value]), bin_jitter[value]),
            )
            factor = MULTI_REUSABLE_FACTORS[bin_index]
            deterioration = self._reusable_multi_deterioration(
                session,
                space,
                dimension_id,
                factor,
            )
            if deterioration > MULTI_MIN_REUSABLE_D:
                changes[dimension_id] = deterioration
        return changes

    def _multi_pair(self, session: Dict[str, Any]) -> Dict[str, Any]:
        space = self.space(session["profile_id"])
        round_index = int(session["multi_round"])
        if round_index >= 12 and round_index % 10 == 9:
            prior = [
                item
                for item in session["interactions"]
                if item.get("stage") == "multi"
                and item.get("preference") in {"A", "B", "tie"}
                and not item.get("stability_repeat_of")
            ]
            if prior:
                original = prior[(round_index // 10) % len(prior)]
                session["multi_round"] = round_index + 1
                index = len(session["interactions"]) + 1
                return {
                    "stage": "multi",
                    "question_id": f"STABILITY-{index:04d}",
                    "a": copy.deepcopy(original["scheme_a"]),
                    "b": copy.deepcopy(original["scheme_b"]),
                    "stability_context": {
                        "interaction_id": original["id"],
                        "reuse_a": original.get("reuse_a"),
                        "reuse_b": original.get("reuse_b"),
                        "preference": original.get("preference"),
                    },
                    "instruction": (
                        "这是少量稳定性复测题，请按当前判断重新作答。"
                        "系统不会显示旧答案，复测用于检查前后口径是否稳定。"
                    ),
                }
        rng = random.Random(self.seed + 10007 * (round_index + 1) + len(session["interactions"]) * 101)
        seen = set(session.get("multi_signatures") or [])
        learner = self._learner(session)
        candidates: List[Tuple[float, Dict[str, float], Dict[str, float], Dict[str, Any], Dict[str, Any]]] = []
        for attempt in range(32):
            changes_a = self._generate_multi_changes(session, rng, attempt * 2)
            changes_b = self._generate_multi_changes(session, rng, attempt * 2 + 1)
            if not changes_a or not changes_b or changes_a == changes_b:
                continue
            params_a = space.scheme_from_changes(changes_a)
            params_b = space.scheme_from_changes(changes_b)
            signature = stable_signature({"a": params_a, "b": params_b})
            inverse = stable_signature({"a": params_b, "b": params_a})
            if signature in seen or inverse in seen:
                continue
            probability = learner.pair_probability(params_a, params_b)
            used_keys = [
                space.dimension_by_id[item]["attribute_key"]
                for item in [*changes_a, *changes_b]
            ]
            scarcity = sum(1.0 / (1.0 + session["multi_attribute_counts"].get(key, 0)) for key in used_keys)
            informativeness = 1.0 - 2.0 * abs(probability - 0.5)
            objective = 0.62 * informativeness + 0.38 * scarcity / max(len(used_keys), 1)
            candidates.append((objective, changes_a, changes_b, params_a, params_b))
        if not candidates:
            changes_a = self._generate_multi_changes(session, rng, 0)
            changes_b = self._generate_multi_changes(session, rng, 5)
            params_a = space.scheme_from_changes(changes_a)
            params_b = space.scheme_from_changes(changes_b)
        else:
            _, changes_a, changes_b, params_a, params_b = max(candidates, key=lambda item: item[0])
        session["multi_round"] = round_index + 1
        pair_signature = stable_signature({"a": params_a, "b": params_b})
        session.setdefault("multi_signatures", []).append(pair_signature)
        session["multi_signatures"] = session["multi_signatures"][-120:]
        for changes in (changes_a, changes_b):
            for dimension_id, deterioration in changes.items():
                key = space.dimension_by_id[dimension_id]["attribute_key"]
                session["multi_attribute_counts"][key] = int(
                    session["multi_attribute_counts"].get(key, 0)
                ) + 1
                accepted = self._confirmed_reusable_limit(session, dimension_id)
                ratio = float(deterioration) / max(accepted, 1e-12)
                bin_index = min(
                    range(len(MULTI_REUSABLE_FACTORS)),
                    key=lambda value: abs(MULTI_REUSABLE_FACTORS[value] - ratio),
                )
                session.setdefault("multi_bin_counts", {}).setdefault(
                    dimension_id,
                    [0 for _ in MULTI_REUSABLE_FACTORS],
                )[bin_index] += 1
        index = len(session["interactions"]) + 1
        return {
            "stage": "multi",
            "question_id": f"MULTI-{index:04d}",
            "a": self._scheme_payload(f"A-{index:04d}", "稀疏退化组合A", params_a, changes_a, space),
            "b": self._scheme_payload(f"B-{index:04d}", "稀疏退化组合B", params_b, changes_b, space),
            "instruction": (
                "请分别判断A、B整体是否还值得复用，再比较哪一个更值得复用。"
                "每个方案最多退化三个属性，且各单项均未超过此前确认的可复用边界。"
            ),
        }

    def _scheme_payload(
        self,
        scheme_id: str,
        source: str,
        params: Dict[str, Any],
        changes: Dict[str, float],
        space: BaselineDegradationSpace,
    ) -> Dict[str, Any]:
        changed = []
        for dimension_id, deterioration in changes.items():
            dimension = space.dimension_by_id[dimension_id]
            key = dimension["attribute_key"]
            changed.append(
                {
                    "dimension_id": dimension_id,
                    "attribute_key": key,
                    "attribute_label": dimension["attribute_label"],
                    "baseline_value": space.baseline_params[key],
                    "value": params[key],
                    "unit": dimension["unit"],
                    "deterioration": round(float(deterioration), 4),
                    "deterioration_percent_of_range": round(100.0 * float(deterioration), 1),
                }
            )
        return {
            "id": scheme_id,
            "source": source,
            "params": params,
            "changed": changed,
            "changed_count": len(changed),
        }

    def _prepare_next_pair(self, session: Dict[str, Any]) -> None:
        session["active_pair"] = (
            self._single_pair(session) if session["phase"] == "single" else self._multi_pair(session)
        )

    def _learner(self, session: Dict[str, Any]) -> LockPreferenceLearner:
        return LockPreferenceLearner(
            self.space(session["profile_id"]),
            segments=session.get("uta_segments", DEFAULT_UTA_SEGMENTS),
            bt_weights=session.get("bt_weights") or None,
            uta_increments=session.get("uta_increments") or None,
            boundary_scales=self._boundary_scales(session),
        )

    @staticmethod
    def _boundary_scales(session: Dict[str, Any]) -> Dict[str, float]:
        output: Dict[str, float] = {}
        for dimension_id, boundary in session["single_boundaries"].items():
            value = boundary.get("limit_d")
            if value is None:
                value = boundary.get("accepted_d")
            if value is None or float(value) <= 1e-8:
                rejected = boundary.get("rejected_d")
                value = 0.5 * float(rejected) if rejected is not None else 1.0
            output[dimension_id] = clamp(float(value), 0.01, 1.0)
        return output

    def _retrain(self, session: Dict[str, Any]) -> None:
        learner = self._learner(session)
        bt_summary, uta_summary = learner.fit(session.get("preference_evidence") or [])
        session["bt_model"] = bt_summary
        session["bt_weights"] = bt_summary.get("weights") or []
        session["uta_model"] = uta_summary
        session["uta_increments"] = uta_summary.get("increments") or []
        learner = self._learner(session)
        session["combined_reuse_threshold"] = fit_combined_reuse_threshold(
            session.get("reuse_evidence") or [],
            learner,
        )
        self._sync_preference_reviews(session)

    def _sync_preference_reviews(self, session: Dict[str, Any]) -> None:
        uta = session.get("uta_model") or {}
        active_ids = {
            item["id"]
            for item in session.get("preference_evidence") or []
            if item.get("status", "active") == "active"
        }
        acknowledgements = set(session.get("review_acknowledgements") or [])
        m3_ids = set((uta.get("m3") or {}).get("conflicting_evidence_ids") or []) & active_ids
        tolerance = {
            item.get("evidence_id"): item
            for item in uta.get("tolerances") or []
            if item.get("evidence_id") in active_ids
        }
        desired: List[Tuple[str, str, str]] = []
        for evidence_id in sorted(m3_ids):
            desired.append((evidence_id, "must_review", "最小冲突诊断认为这次A/B判断需要优先复核。"))
        for evidence_id, item in tolerance.items():
            if evidence_id in m3_ids or not item.get("high_tolerance"):
                continue
            desired.append((evidence_id, "suggested_review", "这次A/B判断需要较大容忍量才能被当前UTA解释，建议复核。"))
        for evidence_id, tier, message in desired:
            key = f"preference:{evidence_id}:{tier}"
            if key in acknowledgements:
                continue
            existing = next(
                (
                    item
                    for item in session["review_items"]
                    if item.get("review_key") == key and item.get("status") == "open"
                ),
                None,
            )
            if existing is not None:
                continue
            evidence = next(
                item for item in session["preference_evidence"] if item["id"] == evidence_id
            )
            session["review_items"].append(
                {
                    "id": f"REV-{len(session['review_items']) + 1:03d}",
                    "created_at": now_iso(),
                    "status": "open",
                    "type": "preference_model_conflict",
                    "tier": tier,
                    "review_key": key,
                    "message": message,
                    "evidence_ids": [evidence_id],
                    "interaction_id": evidence.get("interaction_id"),
                }
            )

    def uta_effective_coverage(self, session: Dict[str, Any]) -> Dict[str, Any]:
        learner = self._learner(session)
        segments = int(session.get("uta_segments", DEFAULT_UTA_SEGMENTS))
        relevant = max(1, min(segments, int(math.ceil(0.75 * segments))))
        counts = {
            spec.key: [0 for _ in range(relevant)]
            for spec in learner.space.attributes
        }
        active = OnlineBTModel.active_evidence(session.get("preference_evidence") or [])
        for evidence in active:
            for params in (evidence["params_a"], evidence["params_b"]):
                for spec in learner.space.attributes:
                    raw, side = learner.space.deterioration(params, spec)
                    if raw <= 1e-9:
                        continue
                    effective = learner.effective_deterioration(
                        raw,
                        learner.scale_for(spec, side),
                    )
                    index = min(relevant - 1, int(effective * segments))
                    counts[spec.key][index] += 1
        total_bins = max(len(counts) * relevant, 1)
        covered = sum(1 for values in counts.values() for value in values if value >= 1)
        reliable = sum(1 for values in counts.values() for value in values if value >= 3)
        return {
            "coordinate_system": "single_boundary_adaptive",
            "relevant_bins_per_attribute": relevant,
            "covered_bins": covered,
            "reliable_bins": reliable,
            "total_bins": total_bins,
            "covered_ratio": round(covered / total_bins, 3),
            "reliable_ratio": round(reliable / total_bins, 3),
            "counts": counts,
        }

    def submit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        state = self.load_state()
        session = self.active_session(state)
        pair = session["active_pair"]
        if pair["stage"] == "single":
            self._submit_single(session, pair, payload)
        else:
            self._submit_multi(session, pair, payload)
        self._prepare_next_pair(session)
        self.save_state(state)
        return self.summarize(state)

    def _submit_single(
        self,
        session: Dict[str, Any],
        pair: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> None:
        label = str(payload.get("reuseB") or "")
        if label not in REUSE_LABELS:
            raise ProjectDataError("请选择退化指标是否仍可复用。")
        boundary = session["single_boundaries"][pair["dimension_id"]]
        deterioration = float(pair["probe_d"])
        observation = {
            "id": f"R-{len(session['interactions']) + 1:04d}",
            "created_at": now_iso(),
            "deterioration": round(deterioration, 8),
            "value": pair["b"]["params"][boundary["attribute_key"]],
            "label": label,
        }
        boundary["observations"].append(observation)
        if label == "reusable":
            boundary["accepted_d"] = max(float(boundary["accepted_d"]), deterioration)
        elif label == "not_reusable":
            boundary["rejected_d"] = (
                deterioration
                if boundary["rejected_d"] is None
                else min(float(boundary["rejected_d"]), deterioration)
            )
        else:
            boundary["uncertain_count"] = int(boundary.get("uncertain_count", 0)) + 1
        space = self.space(session["profile_id"])
        dimension = space.dimension_by_id[pair["dimension_id"]]
        if self._boundary_complete(boundary, dimension, space):
            self._finish_boundary(boundary)
        elif len(boundary["observations"]) >= 12:
            boundary["status"] = "unresolved"
            boundary["limit_d"] = None
        interaction = {
            "id": f"H-{len(session['interactions']) + 1:04d}",
            "created_at": now_iso(),
            "stage": "single",
            "question_id": pair["question_id"],
            "scheme_a": pair["a"],
            "scheme_b": pair["b"],
            "reuse_b": label,
            "preference": "baseline_structurally_better",
        }
        session["interactions"].append(interaction)
        session["learning_events"].append(
            {
                "id": f"E-{len(session['learning_events']) + 1:03d}",
                "created_at": now_iso(),
                "title": f"{boundary['attribute_label']}边界证据已保存",
                "detail": (
                    f"退化度 {deterioration:.3f} 判断为“{REUSE_LABELS[label]}”。"
                    + (
                        f"当前极限区间 [{boundary['accepted_d']:.3f}, {boundary['rejected_d']:.3f}]。"
                        if boundary["rejected_d"] is not None
                        else f"目前确认可接受至 {boundary['accepted_d']:.3f}。"
                    )
                ),
            }
        )

    def _add_reuse_evidence(
        self,
        session: Dict[str, Any],
        interaction_id: str,
        scheme: Dict[str, Any],
        side: str,
        label: str,
    ) -> Optional[Dict[str, Any]]:
        if label not in REUSE_LABELS:
            raise ProjectDataError("整体复用选项无效。")
        if label == "uncertain":
            return None
        evidence = {
            "id": f"R-{len(session['reuse_evidence']) + 1:04d}",
            "created_at": now_iso(),
            "interaction_id": interaction_id,
            "scheme_id": scheme["id"],
            "side": side,
            "params": copy.deepcopy(scheme["params"]),
            "changed": copy.deepcopy(scheme["changed"]),
            "label": label,
            "confidence": 1.0,
            "status": "active",
        }
        signature = stable_signature(evidence["params"])
        for previous in session["reuse_evidence"]:
            if (
                previous.get("status", "active") == "active"
                and stable_signature(previous["params"]) == signature
                and previous.get("label") != label
            ):
                session["review_items"].append(
                    {
                        "id": f"REV-{len(session['review_items']) + 1:03d}",
                        "created_at": now_iso(),
                        "status": "open",
                        "type": "reuse_label_conflict",
                        "message": (
                            f"方案 {previous['scheme_id']} 与 {scheme['id']} 参数相同，"
                            "但整体复用判断相反，请复核其中一次。"
                        ),
                        "evidence_ids": [previous["id"], evidence["id"]],
                    }
                )
        session["reuse_evidence"].append(evidence)
        return evidence

    def _submit_multi(
        self,
        session: Dict[str, Any],
        pair: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> None:
        reuse_a = str(payload.get("reuseA") or "")
        reuse_b = str(payload.get("reuseB") or "")
        raw_preference = str(payload.get("preference") or "")
        if reuse_a not in REUSE_LABELS or reuse_b not in REUSE_LABELS:
            raise ProjectDataError("请分别判断方案 A 和 B 是否还能整体复用。")
        if raw_preference not in PREFERENCE_OPTIONS:
            raise ProjectDataError("请选择 A/B 哪个更值得复用。")
        relation, confidence, preference_label = PREFERENCE_OPTIONS[raw_preference]
        review_context = pair.get("review_context") or {}
        if review_context:
            replaced_interaction_id = review_context.get("interaction_id")
            replaced_ids = set()
            for collection_name in ("reuse_evidence", "preference_evidence"):
                for item in session.get(collection_name) or []:
                    if (
                        item.get("interaction_id") == replaced_interaction_id
                        and item.get("status", "active") == "active"
                    ):
                        item["status"] = "superseded"
                        replaced_ids.add(item["id"])
            for review in session.get("review_items") or []:
                if review.get("status") != "open":
                    continue
                if review.get("interaction_id") == replaced_interaction_id or replaced_ids.intersection(
                    review.get("evidence_ids") or []
                ):
                    review["status"] = "resolved"
                    review["resolved_at"] = now_iso()
                    review["action"] = "rejudged"
                    session["review_history"].append(copy.deepcopy(review))
        interaction_id = f"H-{len(session['interactions']) + 1:04d}"
        evidence_a = self._add_reuse_evidence(session, interaction_id, pair["a"], "A", reuse_a)
        evidence_b = self._add_reuse_evidence(session, interaction_id, pair["b"], "B", reuse_b)
        preference_evidence = None
        if relation in {"A", "B", "tie"}:
            preference_evidence = {
                "id": f"P-{len(session['preference_evidence']) + 1:04d}",
                "created_at": now_iso(),
                "interaction_id": interaction_id,
                "scheme_a": pair["a"]["id"],
                "scheme_b": pair["b"]["id"],
                "params_a": copy.deepcopy(pair["a"]["params"]),
                "params_b": copy.deepcopy(pair["b"]["params"]),
                "relation": relation,
                "confidence": confidence,
                "status": "active",
                "learning_context": "lock_v17_baseline_degradation_tradeoff",
            }
            session["preference_evidence"].append(preference_evidence)
        interaction = {
                "id": interaction_id,
                "created_at": now_iso(),
                "stage": "multi",
                "question_id": pair["question_id"],
                "scheme_a": pair["a"],
                "scheme_b": pair["b"],
                "reuse_a": reuse_a,
                "reuse_b": reuse_b,
                "preference": relation,
                "preference_strength": raw_preference,
                "preference_label": preference_label,
                "reuse_evidence_ids": [
                    item["id"] for item in (evidence_a, evidence_b) if item is not None
                ],
                "preference_evidence_id": (
                    preference_evidence["id"] if preference_evidence is not None else None
                ),
                "replaces_interaction_id": review_context.get("interaction_id"),
                "stability_repeat_of": (pair.get("stability_context") or {}).get(
                    "interaction_id"
                ),
            }
        session["interactions"].append(interaction)
        stability = pair.get("stability_context") or {}
        if stability:
            consistent = (
                reuse_a == stability.get("reuse_a")
                and reuse_b == stability.get("reuse_b")
                and relation == stability.get("preference")
            )
            session.setdefault("comparison_quality", {}).setdefault(
                "stability_checks", []
            ).append(
                {
                    "id": f"STAB-{len(session['comparison_quality']['stability_checks']) + 1:03d}",
                    "created_at": now_iso(),
                    "original_interaction_id": stability.get("interaction_id"),
                    "repeat_interaction_id": interaction_id,
                    "consistent": consistent,
                    "original": {
                        "reuse_a": stability.get("reuse_a"),
                        "reuse_b": stability.get("reuse_b"),
                        "preference": stability.get("preference"),
                    },
                    "repeat": {
                        "reuse_a": reuse_a,
                        "reuse_b": reuse_b,
                        "preference": relation,
                    },
                }
            )
            session["comparison_quality"]["stability_checks"] = session[
                "comparison_quality"
            ]["stability_checks"][-30:]
        self._retrain(session)
        uta = session["uta_model"]
        threshold = session["combined_reuse_threshold"]
        session["learning_events"].append(
            {
                "id": f"E-{len(session['learning_events']) + 1:03d}",
                "created_at": now_iso(),
                "title": "稀疏组合证据已更新 UTA",
                "detail": (
                    f"有效偏好 {uta.get('evidence_pairs', 0)} 对，判断一致度 "
                    f"{uta.get('judgment_consistency') if uta.get('judgment_consistency') is not None else '待形成'}；"
                    f"综合复用分界 {threshold.get('threshold') if threshold.get('threshold') is not None else '仍在夹逼'}。"
                ),
            }
        )

    def update_profile(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        profile_id = str(payload.get("profileId") or "")
        if profile_id not in self.profile_by_id:
            raise ProjectDataError("基准技术协议无效。")
        state = self.load_state()
        state["active_profile_id"] = profile_id
        self.save_state(state)
        return self.summarize(state)

    def update_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        segments = validate_segment_count(payload.get("utaSegments"))
        state = self.load_state()
        session = self.active_session(state)
        if int(session["uta_segments"]) != segments:
            session["uta_segments"] = segments
            self._retrain(session)
            session["learning_events"].append(
                {
                    "id": f"E-{len(session['learning_events']) + 1:03d}",
                    "created_at": now_iso(),
                    "title": "UTA 分段数已调整",
                    "detail": f"全部属性统一改为 {segments} 段，并使用现有多属性偏好重新训练。",
                }
            )
            self.save_state(state)
        return self.summarize(state)

    def _boundary_payload(
        self,
        space: BaselineDegradationSpace,
        boundary: Dict[str, Any],
    ) -> Dict[str, Any]:
        result = copy.deepcopy(boundary)
        dimension = space.dimension_by_id[boundary["dimension_id"]]
        result["accepted_value"] = space.value_at(dimension["id"], boundary["accepted_d"])
        result["rejected_value"] = (
            None
            if boundary["rejected_d"] is None
            else space.value_at(dimension["id"], boundary["rejected_d"])
        )
        result["limit_value"] = (
            None
            if boundary["limit_d"] is None
            else space.value_at(dimension["id"], boundary["limit_d"])
        )
        result["progress"] = (
            1.0
            if boundary["status"] in {"learned", "full_range_reusable", "unresolved"}
            else min(0.95, len(boundary["observations"]) / 6.0)
        )
        return result

    def _parse_unknown_params(self, payload: Dict[str, Any], space: BaselineDegradationSpace) -> Dict[str, Any]:
        raw = payload.get("params") if isinstance(payload.get("params"), dict) else payload
        params = copy.deepcopy(space.baseline_params)
        for spec in self.project.attributes:
            if spec.key not in raw:
                continue
            value = raw[spec.key]
            if spec.is_numeric:
                try:
                    params[spec.key] = float(value)
                except (TypeError, ValueError) as exc:
                    raise ProjectDataError(f"“{spec.label}”必须填写数字。") from exc
            else:
                params[spec.key] = value
        return params

    def evaluate_params(
        self,
        params: Dict[str, Any],
        session: Dict[str, Any],
        evaluation_profile_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        master_space = self.space(session["profile_id"])
        target_profile_id = evaluation_profile_id or session["profile_id"]
        evaluation_space = self.space(target_profile_id)
        learner = self._learner(session)
        uta_ready = int((session.get("uta_model") or {}).get("evidence_pairs", 0)) > 0
        result = evaluate_migrated_reuse_model(
            master_space=master_space,
            evaluation_space=evaluation_space,
            learner=learner,
            params=params,
            boundaries=session["single_boundaries"],
            threshold_model=session.get("combined_reuse_threshold") or {},
            master_profile_id=session["profile_id"],
            evaluation_profile_id=target_profile_id,
            uta_ready=uta_ready,
            support_by_dimension=self._curve_support(session),
        )
        robust_scores = []
        for model in (session.get("uta_model") or {}).get("robust_models") or []:
            increments = model.get("increments") or []
            if len(increments) != len(session.get("uta_increments") or []):
                continue
            robust_learner = LockPreferenceLearner(
                master_space,
                segments=session.get("uta_segments", DEFAULT_UTA_SEGMENTS),
                uta_increments=increments,
                boundary_scales=self._boundary_scales(session),
            )
            robust_scores.append(
                reanchored_effectiveness_score(
                    robust_learner,
                    evaluation_space,
                    params,
                )
            )
        if robust_scores:
            values = np.asarray(robust_scores, dtype=float)
            result["effectiveness_interval"] = [
                round(float(np.percentile(values, 10)), 2),
                round(float(np.percentile(values, 90)), 2),
            ]
            threshold = result.get("combined_reuse_threshold")
            result["robust_model_count"] = len(robust_scores)
            result["reuse_support_rate"] = (
                None
                if threshold is None
                else round(float(np.mean(values >= float(threshold))), 3)
            )
        return result

    def _curve_support(self, session: Dict[str, Any]) -> Dict[str, float]:
        space = self.space(session["profile_id"])
        output = {dimension["id"]: 0.0 for dimension in space.dimensions}
        for dimension_id, boundary in session["single_boundaries"].items():
            observations = boundary.get("observations") or []
            output[dimension_id] = max(
                [float(item.get("deterioration", 0.0)) for item in observations] or [0.0]
            )
        for evidence in OnlineBTModel.active_evidence(session.get("preference_evidence") or []):
            for params in (evidence["params_a"], evidence["params_b"]):
                for spec in space.attributes:
                    deterioration, side = space.deterioration(params, spec)
                    dimension_id = f"{spec.key}:{side}"
                    if dimension_id in output:
                        output[dimension_id] = max(output[dimension_id], float(deterioration))
        return {key: round(value, 8) for key, value in output.items()}

    def evaluate_unknown(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        state = self.load_state()
        session = self.active_session(state)
        evaluation_profile_id = state["active_profile_id"]
        params = self._parse_unknown_params(payload, self.space(evaluation_profile_id))
        result = self.evaluate_params(params, session, evaluation_profile_id=evaluation_profile_id)
        record = {
            "id": f"U-{len(session['unknown_evaluations']) + 1:04d}",
            "created_at": now_iso(),
            "params": params,
            "result": result,
        }
        session["unknown_evaluations"].append(record)
        session["unknown_evaluations"] = session["unknown_evaluations"][-100:]
        self.save_state(state)
        return record

    def review_action(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        state = self.load_state()
        session = self.active_session(state)
        review_id = str(payload.get("reviewId") or "")
        evidence_id = str(payload.get("evidenceId") or "")
        action = str(payload.get("action") or "")
        review = next((item for item in session["review_items"] if item["id"] == review_id), None)
        if review is None or review.get("status") != "open":
            raise ProjectDataError("复核项不存在或已经处理。")
        if evidence_id not in review.get("evidence_ids", []):
            raise ProjectDataError("请选择该复核项关联的一条判断。")
        evidence = next(
            (
                item
                for collection in (
                    session.get("reuse_evidence") or [],
                    session.get("preference_evidence") or [],
                )
                for item in collection
                if item["id"] == evidence_id
            ),
            None,
        )
        if evidence is None:
            raise ProjectDataError("关联判断不存在。")
        if action == "rejudge":
            interaction = next(
                (
                    item
                    for item in session["interactions"]
                    if item["id"] == evidence.get("interaction_id")
                ),
                None,
            )
            if interaction is None or interaction.get("stage") != "multi":
                raise ProjectDataError("原A/B方案已不可用，无法重新判断。")
            session["active_pair"] = {
                "stage": "multi",
                "question_id": f"REVIEW-{review_id}",
                "a": copy.deepcopy(interaction["scheme_a"]),
                "b": copy.deepcopy(interaction["scheme_b"]),
                "review_context": {
                    "review_id": review_id,
                    "interaction_id": interaction["id"],
                },
                "instruction": (
                    "正在复核一组旧判断。请重新判断A、B整体能否复用以及哪个更值得复用；"
                    "提交后只替换这一组证据。"
                ),
            }
            self.save_state(state)
            return self.summarize(state)
        if action == "withdraw":
            evidence["status"] = "withdrawn"
        elif action != "keep":
            raise ProjectDataError("复核操作无效。")
        review["status"] = "resolved"
        review["resolved_at"] = now_iso()
        review["action"] = action
        review["selected_evidence_id"] = evidence_id
        review_key = review.get("review_key")
        if review_key:
            session.setdefault("review_acknowledgements", []).append(review_key)
        session["review_history"].append(copy.deepcopy(review))
        self._retrain(session)
        self.save_state(state)
        return self.summarize(state)

    def _review_payload(
        self,
        session: Dict[str, Any],
        review: Dict[str, Any],
    ) -> Dict[str, Any]:
        output = copy.deepcopy(review)
        interaction_id = review.get("interaction_id")
        if interaction_id is None:
            evidence_ids = set(review.get("evidence_ids") or [])
            evidence = next(
                (
                    item
                    for collection in (
                        session.get("reuse_evidence") or [],
                        session.get("preference_evidence") or [],
                    )
                    for item in collection
                    if item.get("id") in evidence_ids
                ),
                None,
            )
            interaction_id = None if evidence is None else evidence.get("interaction_id")
        interaction = next(
            (
                item
                for item in session.get("interactions") or []
                if item.get("id") == interaction_id
            ),
            None,
        )
        if interaction is not None:
            output["interaction_id"] = interaction["id"]
            output["original_judgment"] = {
                "reuse_a": interaction.get("reuse_a"),
                "reuse_b": interaction.get("reuse_b"),
                "preference": interaction.get("preference"),
                "preference_label": interaction.get("preference_label"),
            }
            output["scheme_a"] = copy.deepcopy(interaction.get("scheme_a"))
            output["scheme_b"] = copy.deepcopy(interaction.get("scheme_b"))
        return output

    def reset(self) -> Dict[str, Any]:
        return self.summarize(self.load_state(reset=True))

    def export_archive(self) -> bytes:
        state = self.load_state()
        payload = {
            "format": "lock-product-v17-complete-archive",
            "exported_at": now_iso(),
            "project": {
                "product_code": self.project.product_code,
                "project_name": self.project.project_name,
                "workbook_fingerprint": self.project.workbook_fingerprint,
                "learning_fingerprint": self.project.learning_fingerprint,
            },
            "state": state,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    def export_frozen_model(self) -> bytes:
        from frozen_lock_model import build_frozen_model

        model = build_frozen_model(self)
        return json.dumps(model, ensure_ascii=False, indent=2).encode("utf-8")

    def export_frozen_model_bundle(self) -> Tuple[bytes, str]:
        from frozen_lock_model import export_frozen_model_bundle

        return export_frozen_model_bundle(self)

    def learning_completion(self, session: Dict[str, Any]) -> Dict[str, Any]:
        boundaries = list(session["single_boundaries"].values())
        complete = [
            item
            for item in boundaries
            if item["status"] in {"learned", "full_range_reusable", "unresolved"}
        ]
        resolved = [item for item in complete if item["status"] != "unresolved"]
        uta = session.get("uta_model") or {}
        threshold = session.get("combined_reuse_threshold") or {}
        preference_count = int(uta.get("evidence_pairs", 0))
        minimum_pairs = max(20, 2 * len(self.space(session["profile_id"]).attributes))
        recommended = max(
            minimum_pairs,
            int(uta.get("recommended_training_pairs", minimum_pairs)),
        )
        single_done = len(complete) == len(boundaries)
        validation_values = []
        for value in (
            uta.get("test_accuracy"),
            (uta.get("cross_validation") or {}).get("accuracy"),
            (uta.get("whole_scheme_holdout") or {}).get("accuracy"),
        ):
            if value is not None:
                validation_values.append(float(value))
        validation_accuracy = min(validation_values) if validation_values else None
        consistency = uta.get("judgment_consistency")
        consistency = None if consistency is None else float(consistency)
        coverage = self.uta_effective_coverage(session)
        must_reviews = [
            item
            for item in session.get("review_items") or []
            if item.get("status") == "open" and item.get("tier", "must_review") == "must_review"
        ]
        suggested_reviews = [
            item
            for item in session.get("review_items") or []
            if item.get("status") == "open" and item.get("tier") == "suggested_review"
        ]
        stability_checks = list(
            (session.get("comparison_quality") or {}).get("stability_checks") or []
        )
        stability_consistency = (
            sum(1 for item in stability_checks if item.get("consistent"))
            / len(stability_checks)
            if stability_checks
            else None
        )
        reasons: List[str] = []
        if not single_done:
            reasons.append(f"单项复用极限完成 {len(complete)}/{len(boundaries)}")
        if len(resolved) < len(boundaries):
            reasons.append(f"仍有 {len(boundaries) - len(resolved)} 个单项边界没有形成明确结论")
        if preference_count < minimum_pairs:
            reasons.append(f"有效多属性比较 {preference_count} 对，至少需要 {minimum_pairs} 对")
        if validation_accuracy is None:
            reasons.append("尚未形成完整的独立验证正确率")
        elif validation_accuracy < 0.80:
            reasons.append(f"保守验证正确率 {validation_accuracy:.0%}，目标为80%")
        if consistency is None:
            reasons.append("尚未形成判断一致度")
        elif consistency < 0.80:
            reasons.append(f"判断一致度 {consistency:.0%}，目标为80%")
        if float(coverage["covered_ratio"]) < 0.80:
            reasons.append(f"有效UTA区段覆盖 {float(coverage['covered_ratio']):.0%}，目标为80%")
        if float(coverage["reliable_ratio"]) < 0.60:
            reasons.append(f"可靠UTA区段覆盖 {float(coverage['reliable_ratio']):.0%}，目标为60%")
        threshold_accuracy = threshold.get("accuracy")
        if threshold.get("status") != "learned" or threshold_accuracy is None:
            reasons.append("综合复用分界尚未稳定形成")
        elif float(threshold_accuracy) < 0.80:
            reasons.append(f"综合复用分界正确率 {float(threshold_accuracy):.0%}，目标为80%")
        if int(threshold.get("reusable_count", 0)) < 3 or int(
            threshold.get("not_reusable_count", 0)
        ) < 3:
            reasons.append("综合复用分界两侧的证据不足，各至少需要3个")
        if len(stability_checks) >= 2 and float(stability_consistency or 0.0) < 0.75:
            reasons.append(f"稳定性复测一致率 {float(stability_consistency or 0.0):.0%}，目标为75%")
        if must_reviews:
            reasons.append(f"还有 {len(must_reviews)} 项必须复核的判断")
        standard_complete = not reasons
        high_confidence = bool(
            standard_complete
            and preference_count >= recommended
            and validation_accuracy is not None
            and validation_accuracy >= 0.90
            and consistency is not None
            and consistency >= 0.90
            and float(threshold_accuracy or 0.0) >= 0.90
            and float(coverage["covered_ratio"]) >= 0.90
            and float(coverage["reliable_ratio"]) >= 0.75
        )
        if not single_done:
            status = "single_learning"
            title = "正在逐属性学习复用极限"
        elif high_confidence:
            status = "high_confidence_complete"
            title = "模型已高置信完成，可以结束对比"
        elif standard_complete:
            status = "standard_complete"
            title = "模型已达到可用标准，可以结束对比"
        else:
            status = "continue_learning"
            title = "模型仍需继续学习"
        suggested_additional = 0 if standard_complete else max(5, minimum_pairs - preference_count)
        if validation_accuracy is not None and validation_accuracy < 0.80:
            suggested_additional = max(suggested_additional, 10)
        return {
            "status": status,
            "title": title,
            "complete": standard_complete,
            "high_confidence": high_confidence,
            "message": "已满足全部停止条件。" if standard_complete else "；".join(reasons) + "。",
            "reasons": reasons,
            "single_total": len(boundaries),
            "single_completed": len(complete),
            "single_resolved": len(resolved),
            "single_progress": round(len(complete) / max(len(boundaries), 1), 3),
            "preference_pairs": preference_count,
            "minimum_preference_pairs": minimum_pairs,
            "recommended_preference_pairs": recommended,
            "suggested_additional_effective_pairs": suggested_additional,
            "threshold_status": threshold.get("status"),
            "threshold": threshold.get("threshold"),
            "threshold_accuracy": threshold.get("accuracy"),
            "uta_status": uta.get("status", "no_data"),
            "uta_test_accuracy": uta.get("test_accuracy"),
            "judgment_consistency": uta.get("judgment_consistency"),
            "validation_accuracy": None if validation_accuracy is None else round(validation_accuracy, 3),
            "validation_components": {
                "test_accuracy": uta.get("test_accuracy"),
                "cross_validation_accuracy": (uta.get("cross_validation") or {}).get("accuracy"),
                "whole_scheme_holdout_accuracy": (uta.get("whole_scheme_holdout") or {}).get("accuracy"),
            },
            "m1_consistent": bool(uta.get("m1_consistent", False)),
            "m1_role": "严格零冲突研究诊断，不作为独立完成门槛",
            "m3_minimum_conflicts": int((uta.get("m3") or {}).get("minimum_conflicts", 0) or 0),
            "open_must_reviews": len(must_reviews),
            "open_suggested_reviews": len(suggested_reviews),
            "coverage": coverage,
            "stability_check_count": len(stability_checks),
            "stability_consistency": (
                None if stability_consistency is None else round(stability_consistency, 3)
            ),
        }

    def summarize(self, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        state = state or self.load_state()
        session = self.active_session(state)
        space = self.space(session["profile_id"])
        evaluation_space = self.space(state["active_profile_id"])
        boundaries = [
            self._boundary_payload(space, session["single_boundaries"][dimension_id])
            for dimension_id in session["single_order"]
        ]
        return {
            "app": {
                "name": "成品级 V17 锁特化效能与复用评估",
                "profile_version": PROFILE_VERSION,
                "state_contract": STATE_CONTRACT,
                "physical_feasibility_enabled": False,
                "coupling_learning_enabled": False,
                "master_profile_id": state["master_profile_id"],
            },
            "project": {
                "name": self.project.project_name,
                "product_code": self.project.product_code,
                "warnings": [*self.project.warnings, *space.warnings],
                "attributes": [
                    {
                        "key": item.key,
                        "label": item.label,
                        "unit": item.unit,
                        "data_type": item.data_type,
                        "precision": item.precision,
                        "design_stage": item.design_stage,
                        "preference_direction": item.preference_direction,
                        "participates_utility": item.participates_utility,
                    }
                    for item in self.project.attributes
                ],
                "profiles": [
                    {"id": item.id, "name": item.name} for item in self.project.requirement_profiles
                ],
                "master_profile_id": state["master_profile_id"],
                "active_profile_id": state["active_profile_id"],
            },
            "baseline": space.baseline_payload(),
            "evaluationBaseline": evaluation_space.baseline_payload(),
            "phase": session["phase"],
            "pair": copy.deepcopy(session["active_pair"]),
            "singleBoundaries": boundaries,
            "multiAttributeCounts": copy.deepcopy(session["multi_attribute_counts"]),
            "learningCompletion": self.learning_completion(session),
            "btModel": copy.deepcopy(session.get("bt_model") or {}),
            "utaModel": copy.deepcopy(session.get("uta_model") or {}),
            "combinedReuseThreshold": copy.deepcopy(
                session.get("combined_reuse_threshold") or {}
            ),
            "stats": {
                "interactions": len(session["interactions"]),
                "singleInteractions": sum(
                    1 for item in session["interactions"] if item["stage"] == "single"
                ),
                "multiInteractions": sum(
                    1 for item in session["interactions"] if item["stage"] == "multi"
                ),
                "preferenceEvidence": len(
                    OnlineBTModel.active_evidence(session["preference_evidence"])
                ),
                "reuseEvidence": sum(
                    1
                    for item in session["reuse_evidence"]
                    if item.get("status", "active") == "active"
                ),
                "openReviews": sum(
                    1 for item in session["review_items"] if item.get("status") == "open"
                ),
            },
            "reviews": [
                self._review_payload(session, item)
                for item in session["review_items"]
                if item.get("status") == "open"
            ],
            "utaCoverage": self.uta_effective_coverage(session),
            "comparisonQuality": copy.deepcopy(session.get("comparison_quality") or {}),
            "recentInteractions": copy.deepcopy(session["interactions"][-8:]),
            "learningEvents": copy.deepcopy(session["learning_events"][-60:]),
            "unknownEvaluations": copy.deepcopy(session["unknown_evaluations"][-20:]),
            "developerSettings": {
                "utaSegments": session["uta_segments"],
                "minimumSegments": MIN_UTA_SEGMENTS,
                "maximumSegments": MAX_UTA_SEGMENTS,
            },
        }


APP_INSTANCE: Optional[LockV17App] = None


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_EXCLUSIVEADDRUSE,
                1,
            )
        super().server_bind()


class RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: Any) -> None:
        return

    @property
    def app(self) -> LockV17App:
        assert APP_INSTANCE is not None
        return APP_INSTANCE

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
            if path == "/":
                html_response(self, (HERE / "lock_ui.html").read_text(encoding="utf-8"))
            elif path == "/api/state":
                json_response(self, self.app.summarize())
            elif path == "/api/health":
                json_response(self, {"status": "ok", "profile_version": PROFILE_VERSION})
            elif path == "/api/export-archive":
                binary_response(self, self.app.export_archive(), "lock_v17_complete_archive.json")
            elif path == "/api/export-model":
                data, filename = self.app.export_frozen_model_bundle()
                binary_response(self, data, filename)
            elif path == "/api/export-model-json":
                binary_response(self, self.app.export_frozen_model(), "lock_v17_frozen_model.json")
            else:
                json_response(self, {"error": "未找到该接口。"}, 404)
        except ProjectDataError as exc:
            json_response(self, {"error": str(exc)}, 400)
        except Exception as exc:  # pragma: no cover - surfaced to offline UI
            json_response(self, {"error": f"服务器错误：{exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
            payload = read_body(self)
            if path == "/api/submit":
                result = self.app.submit(payload)
            elif path == "/api/evaluate":
                result = self.app.evaluate_unknown(payload)
            elif path == "/api/profile":
                result = self.app.update_profile(payload)
            elif path == "/api/settings":
                result = self.app.update_settings(payload)
            elif path == "/api/review":
                result = self.app.review_action(payload)
            elif path == "/api/reset":
                result = self.app.reset()
            else:
                json_response(self, {"error": "未找到该接口。"}, 404)
                return
            json_response(self, result)
        except ProjectDataError as exc:
            json_response(self, {"error": str(exc)}, 400)
        except Exception as exc:  # pragma: no cover - surfaced to offline UI
            json_response(self, {"error": f"服务器错误：{exc}"}, 500)


def serve(app: LockV17App, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    global APP_INSTANCE
    APP_INSTANCE = app
    server = ExclusiveThreadingHTTPServer((host, int(port)), RequestHandler)
    print(f"成品级 V17 锁特化版：http://{host}:{port}/")
    print(f"Excel：{app.project.workbook_path}")
    print(f"State：{app.state_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="成品级 V17 锁特化效能与复用评估")
    parser.add_argument("workbook", nargs="?", default=str(DEFAULT_WORKBOOK))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args(argv)
    app = LockV17App(args.workbook, state_dir=args.state_dir)
    if args.reset:
        app.load_state(reset=True)
    serve(app, args.host, args.port)


if __name__ == "__main__":
    main()
