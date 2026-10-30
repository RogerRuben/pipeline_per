# -*- coding: utf-8 -*-
"""Data-minimized frozen runtime for the lock-specialized V17 model."""

from __future__ import annotations

import copy
import hashlib
import json
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from lock_utility_model import (
    BaselineDegradationSpace,
    LockPreferenceLearner,
    evaluate_migrated_reuse_model,
    evaluate_reuse_model,
    reanchored_effectiveness_score,
)
from project_excel import (
    AttributeSpec,
    ProjectDataError,
    ProjectDataset,
    RequirementProfile,
    RequirementSpec,
)


MODEL_FORMAT_VERSION = "lock-product-reuse-model-17.0"
PACKAGE_FORMAT_VERSION = "lock-product-reuse-runtime-package-17.0"
ALGORITHM_VERSION = "V17-ADAPTIVE-UTA-REANCHORED-REUSE-BOUNDARY"
RUNTIME_SOURCE_FILES = (
    "frozen_lock_model.py",
    "lock_utility_model.py",
    "preference_models.py",
    "project_excel.py",
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _select(source: Dict[str, Any], names: Sequence[str]) -> Dict[str, Any]:
    return {name: copy.deepcopy(source[name]) for name in names if name in source}


def _clean_boundaries(items: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    fields = (
        "dimension_id",
        "attribute_key",
        "attribute_label",
        "side",
        "side_label",
        "unit",
        "baseline_value",
        "bad_edge",
        "accepted_d",
        "rejected_d",
        "limit_d",
        "status",
    )
    return {
        dimension_id: _select(item, fields)
        for dimension_id, item in (items or {}).items()
    }


def _clean_threshold(item: Dict[str, Any]) -> Dict[str, Any]:
    return _select(
        item or {},
        (
            "status",
            "threshold",
            "evidence_count",
            "reusable_count",
            "not_reusable_count",
            "accuracy",
            "separable",
            "highest_not_reusable_score",
            "lowest_reusable_score",
        ),
    )


def _clean_uta_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    return _select(
        item or {},
        (
            "model_type",
            "status",
            "segments",
            "attribute_count",
            "variable_count",
            "evidence_pairs",
            "training_pairs",
            "test_pairs",
            "training_accuracy",
            "test_accuracy",
            "judgment_consistency",
            "m1_consistent",
            "m1_label",
            "m2_total_slack",
            "generalization_status",
            "recommended_training_pairs",
            "attribute_weights",
            "coordinate_system",
            "score_cap",
            "baseline_score",
        ),
    )


def _clean_bt_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    return _select(
        item or {},
        (
            "model_type",
            "training_pairs",
            "training_accuracy",
            "weighted_accuracy",
            "log_loss",
            "attribute_weights",
        ),
    )


def build_frozen_model(app: Any, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    state = copy.deepcopy(state if state is not None else app.load_state())
    session = app.active_session(state)
    space = app.space(session["profile_id"])
    completion = app.learning_completion(session)
    uta_summary = _clean_uta_summary(session.get("uta_model") or {})
    threshold = _clean_threshold(session.get("combined_reuse_threshold") or {})
    boundaries = _clean_boundaries(session.get("single_boundaries") or {})

    core = {
        "model_format": MODEL_FORMAT_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "profile_version": 17,
        "project": {
            "product_code": app.project.product_code,
            "project_name": app.project.project_name,
            "learning_fingerprint": app.project.learning_fingerprint,
        },
        "schema": space.schema_payload(),
        "baseline": space.baseline_payload(),
        "evaluation_baselines": [
            app.space(profile.id).baseline_payload()
            for profile in app.project.requirement_profiles
        ],
        "inference": {
            "profile_id": session["profile_id"],
            "uta_segments": int(session["uta_segments"]),
            "bt_weights": copy.deepcopy(session.get("bt_weights") or []),
            "uta_increments": copy.deepcopy(session.get("uta_increments") or []),
            "boundary_scales": app._boundary_scales(session),
            "uta_ready": int((session.get("uta_model") or {}).get("evidence_pairs", 0)) > 0,
            "single_boundaries": boundaries,
            "combined_reuse_threshold": threshold,
            "curve_support": app._curve_support(session),
            "robust_uta_increments": [
                copy.deepcopy(item.get("increments") or [])
                for item in (session.get("uta_model") or {}).get("robust_models") or []
                if item.get("increments")
            ],
        },
        "training_summary": {
            "completion": _select(
                completion,
                (
                    "status",
                    "title",
                    "single_total",
                    "single_completed",
                    "single_resolved",
                    "preference_pairs",
                    "recommended_preference_pairs",
                    "threshold_status",
                    "threshold",
                    "threshold_accuracy",
                    "uta_status",
                    "uta_test_accuracy",
                    "judgment_consistency",
                ),
            ),
            "uta": uta_summary,
            "bt": _clean_bt_summary(session.get("bt_model") or {}),
            "combined_reuse_threshold": threshold,
        },
        "semantics": {
            "score_range": [0, 100],
            "baseline_is_100": True,
            "better_than_baseline_bonus": False,
            "better_attributes_do_not_compensate": True,
            "physical_feasibility_included": False,
            "coupling_model_included": False,
            "not_reusable_is_not_physical_infeasibility": True,
        },
        "privacy": {
            "contains_source_workbook": False,
            "contains_source_scheme_rows": False,
            "contains_interaction_records": False,
            "contains_individual_reuse_labels": False,
            "contains_expert_free_text": False,
            "contains_review_history": False,
            "contains_learned_effectiveness_parameters": True,
        },
    }
    model = copy.deepcopy(core)
    model["model_digest"] = _digest(core)
    model["model_version"] = "lock-v17-frozen-%s-%s" % (
        str(app.project.learning_fingerprint)[:12],
        model["model_digest"][:12],
    )
    model["frozen_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return model


def validate_frozen_model(model: Dict[str, Any]) -> None:
    if model.get("model_format") != MODEL_FORMAT_VERSION:
        raise ValueError("冻结锁成品效能模型格式无效。")
    required = (
        "project",
        "schema",
        "baseline",
        "evaluation_baselines",
        "inference",
        "semantics",
        "privacy",
    )
    missing = [name for name in required if name not in model]
    if missing:
        raise ValueError("冻结模型缺少字段：%s" % "、".join(missing))
    core = copy.deepcopy(model)
    core.pop("model_digest", None)
    core.pop("model_version", None)
    core.pop("frozen_at", None)
    if _digest(core) != str(model.get("model_digest") or ""):
        raise ValueError("冻结模型内容摘要校验失败。")
    privacy = model.get("privacy") or {}
    forbidden = (
        "contains_source_workbook",
        "contains_source_scheme_rows",
        "contains_interaction_records",
        "contains_individual_reuse_labels",
        "contains_expert_free_text",
        "contains_review_history",
    )
    if any(bool(privacy.get(name)) for name in forbidden):
        raise ValueError("冻结模型隐私声明不符合外带要求。")
    inference = model.get("inference") or {}
    if not isinstance(inference.get("single_boundaries"), dict):
        raise ValueError("冻结模型缺少单项复用边界。")
    profiles = model.get("evaluation_baselines")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("冻结模型缺少可用评价基准。")
    baseline_id = str((model.get("baseline") or {}).get("profile_id") or "")
    if baseline_id not in {str(item.get("profile_id") or "") for item in profiles}:
        raise ValueError("冻结模型的训练主基准未包含在评价基准列表中。")


def _readme(model: Dict[str, Any]) -> str:
    completion = (model.get("training_summary") or {}).get("completion") or {}
    return """锁成品V17效能与复用冻结模型包
================================

模型版本：{version}
产品：{product}
100分基准：{baseline}
训练状态：{status}

用途：在不携带原始Excel和专家逐条点击记录的情况下，对未知锁成品方案进行
相对基准效能评分，并给出单项复用和整机复用结论。

Python调用：
  from frozen_lock_model import FrozenLockReuseRuntime
  runtime = FrozenLockReuseRuntime("model/frozen_lock_reuse_model.json")
  result = runtime.evaluate(params)
  result_other = runtime.evaluate(params, profile_id="评价基准ID")

本模型不评价物理可行性，也不含耦合模型。“不建议复用”只表示相对基准的
效能退化超过专家接受边界，不能解释成产品无法制造。
""".format(
        version=model.get("model_version"),
        product=(model.get("project") or {}).get("project_name"),
        baseline=(model.get("baseline") or {}).get("profile_name"),
        status=completion.get("title") or completion.get("status") or "未知",
    )


def export_frozen_model_bundle(
    app: Any,
    state: Optional[Dict[str, Any]] = None,
    source_root: Optional[Path] = None,
) -> Tuple[bytes, str]:
    source_root = Path(source_root or Path(__file__).resolve().parent).resolve()
    missing = [name for name in RUNTIME_SOURCE_FILES if not (source_root / name).is_file()]
    if missing:
        raise ValueError("冻结模型运行源码不完整：%s" % "、".join(missing))
    model = build_frozen_model(app, state)
    validate_frozen_model(model)
    model_bytes = json.dumps(model, ensure_ascii=False, indent=2).encode("utf-8")

    files: List[Dict[str, Any]] = []
    payloads: Dict[str, bytes] = {
        "model/frozen_lock_reuse_model.json": model_bytes,
    }
    for name in RUNTIME_SOURCE_FILES:
        payloads["source/%s" % name] = (source_root / name).read_bytes()
    for relative, data in payloads.items():
        files.append(
            {
                "path": relative,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest = {
        "format_version": PACKAGE_FORMAT_VERSION,
        "model_version": model.get("model_version"),
        "model_digest": model.get("model_digest"),
        "model": "model/frozen_lock_reuse_model.json",
        "source_root": "source",
        "privacy": copy.deepcopy(model.get("privacy") or {}),
        "training_summary": copy.deepcopy(model.get("training_summary") or {}),
        "files": files,
    }
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "lock_v17_runtime_manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        archive.writestr("00_冻结模型说明.txt", _readme(model).encode("utf-8-sig"))
        for relative, data in payloads.items():
            archive.writestr(relative, data)
    filename = "lock_v17_reuse_model_%s.zip" % str(model.get("model_digest"))[:12]
    return output.getvalue(), filename


class FrozenLockReuseRuntime:
    """Evaluate V17 scores and reuse decisions without Excel or training State."""

    def __init__(self, model_path: Union[Path, str]):
        self.model_path = Path(model_path).expanduser().resolve()
        self.model = json.loads(self.model_path.read_text(encoding="utf-8"))
        validate_frozen_model(self.model)
        schema = self.model["schema"]
        baseline = self.model["baseline"]
        trainable = set(schema.get("trainable_attribute_keys") or [])
        attributes: List[AttributeSpec] = []
        for raw in schema.get("attributes") or []:
            item = dict(raw)
            if item.get("key") not in trainable:
                item["participates_utility"] = False
                item["participates_generation"] = False
            attributes.append(AttributeSpec(**item))
        baseline_params = dict(baseline.get("params") or {})
        baseline_payloads = self.model.get("evaluation_baselines") or [baseline]
        profiles: List[RequirementProfile] = []
        for baseline_item in baseline_payloads:
            item_params = dict(baseline_item.get("params") or {})
            requirements = [
                RequirementSpec(
                    attribute_key=item.key,
                    attribute_label=item.label,
                    requirement_type="target",
                    target_value=float(item_params[item.key]),
                )
                for item in attributes
                if item.is_numeric and item.key in item_params
            ]
            profiles.append(
                RequirementProfile(
                    id=str(baseline_item.get("profile_id") or "FROZEN_BASELINE"),
                    name=str(baseline_item.get("profile_name") or "冻结100分基准"),
                    requirements=requirements,
                )
            )
        profile = next(
            (
                item
                for item in profiles
                if item.id == str(baseline.get("profile_id") or "")
            ),
            profiles[0],
        )
        project_data = self.model.get("project") or {}
        project = ProjectDataset(
            product_code=str(project_data.get("product_code") or "LOCK_V17_FROZEN"),
            project_name=str(project_data.get("project_name") or "锁成品V17冻结模型"),
            workbook_path="<frozen-lock-v17-model>",
            workbook_fingerprint="",
            learning_fingerprint=str(project_data.get("learning_fingerprint") or ""),
            attributes=attributes,
            schemes=[],
            couplings=[],
            requirement_profiles=profiles,
            warnings=["当前为不含原始Excel和专家点击记录的冻结推理模型。"],
        )
        self.space = BaselineDegradationSpace(project, profile)
        self.spaces = {
            item.id: BaselineDegradationSpace(project, item)
            for item in profiles
        }
        expected_ids = {item["id"] for item in schema.get("dimensions") or []}
        actual_ids = {item["id"] for item in self.space.dimensions}
        if expected_ids != actual_ids:
            raise ValueError("冻结模型的属性退化维度与运行时不一致。")
        inference = self.model["inference"]
        self.learner = LockPreferenceLearner(
            self.space,
            segments=int(inference.get("uta_segments") or 3),
            bt_weights=inference.get("bt_weights") or None,
            uta_increments=inference.get("uta_increments") or None,
            boundary_scales=inference.get("boundary_scales") or None,
        )
        self.boundaries = copy.deepcopy(inference.get("single_boundaries") or {})
        self.threshold = copy.deepcopy(inference.get("combined_reuse_threshold") or {})
        self.curve_support = copy.deepcopy(inference.get("curve_support") or {})
        self.robust_uta_increments = copy.deepcopy(
            inference.get("robust_uta_increments") or []
        )
        self.uta_ready = bool(inference.get("uta_ready"))

    def _parse_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(params, dict):
            raise ProjectDataError("待评价方案必须是属性字典。")
        output: Dict[str, Any] = {}
        for spec in self.space.project.attributes:
            if spec.key not in params or params[spec.key] in (None, ""):
                raise ProjectDataError("待评价方案缺少属性“%s”。" % spec.label)
            if spec.is_numeric:
                try:
                    output[spec.key] = float(params[spec.key])
                except (TypeError, ValueError) as exc:
                    raise ProjectDataError("“%s”必须填写数字。" % spec.label) from exc
            else:
                output[spec.key] = params[spec.key]
        return output

    def evaluate(
        self,
        params: Dict[str, Any],
        profile_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        parsed = self._parse_params(params)
        target_profile_id = str(profile_id or self.space.profile.id)
        if target_profile_id not in self.spaces:
            raise ProjectDataError("冻结模型中不存在评价基准“%s”。" % target_profile_id)
        result = evaluate_migrated_reuse_model(
            master_space=self.space,
            evaluation_space=self.spaces[target_profile_id],
            learner=self.learner,
            params=parsed,
            boundaries=self.boundaries,
            threshold_model=self.threshold,
            master_profile_id=self.space.profile.id,
            evaluation_profile_id=target_profile_id,
            uta_ready=self.uta_ready,
            support_by_dimension=self.curve_support,
        )
        robust_scores = []
        for increments in self.robust_uta_increments:
            robust_learner = LockPreferenceLearner(
                self.space,
                segments=self.learner.segments,
                uta_increments=increments,
                boundary_scales=self.learner.boundary_scales,
            )
            robust_scores.append(
                reanchored_effectiveness_score(
                    robust_learner,
                    self.spaces[target_profile_id],
                    parsed,
                )
            )
        if robust_scores:
            values = sorted(float(item) for item in robust_scores)
            def percentile(ratio: float) -> float:
                position = ratio * (len(values) - 1)
                low = int(position)
                high = min(low + 1, len(values) - 1)
                fraction = position - low
                return values[low] * (1.0 - fraction) + values[high] * fraction
            result["effectiveness_interval"] = [
                round(percentile(0.10), 2),
                round(percentile(0.90), 2),
            ]
            threshold = result.get("combined_reuse_threshold")
            result["robust_model_count"] = len(values)
            result["reuse_support_rate"] = (
                None
                if threshold is None
                else round(
                    sum(1 for value in values if value >= float(threshold)) / len(values),
                    3,
                )
            )
        return result


__all__ = [
    "FrozenLockReuseRuntime",
    "build_frozen_model",
    "export_frozen_model_bundle",
    "validate_frozen_model",
]
