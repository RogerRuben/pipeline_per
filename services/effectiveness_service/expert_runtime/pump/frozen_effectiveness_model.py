# -*- coding: utf-8 -*-
"""Freeze a learned effectiveness project into a deployable, data-minimized model.

The expert workstation keeps the original Workbook and complete State.  The
portable package produced here contains only the project schema and parameters
needed for inference.  It deliberately excludes source Workbook rows,
generated schemes, individual A/B observations, free-text notes and review
history.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import zipfile
from dataclasses import asdict
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union


FORMAT_VERSION = "effectiveness-frozen-runtime-package-15.0"
MODEL_FORMAT_VERSION = "effectiveness-frozen-model-15.0"
ALGORITHM_VERSION = "V15-PAR-UTA-PARTIAL-IDENTIFICATION-FRONTIER"
PROFILE_VERSION = 15

RUNTIME_SOURCE_FILES = [
    "coupling_model.py",
    "feasibility_model.py",
    "frontier_model.py",
    "frozen_effectiveness_model.py",
    "interactive_project_app.py",
    "product_ui.html",
    "preference_models.py",
    "project_excel.py",
    "requirement_model.py",
]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_like_local_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _select(source: Dict[str, Any], names: Iterable[str]) -> Dict[str, Any]:
    return {name: copy.deepcopy(source[name]) for name in names if name in source}


def _sanitized_robust_models(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for item in items or []:
        output.append(
            _select(
                item,
                (
                    "source",
                    "accuracy",
                    "increments",
                ),
            )
        )
    return output


def _sanitized_boundaries(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for item in items or []:
        # evidence_ids identify individual expert records and are intentionally
        # not part of the portable inference model.
        output.append(
            _select(
                item,
                (
                    "attribute_key",
                    "attribute_label",
                    "side",
                    "boundary",
                    "unit",
                    "negative_count",
                    "feasible_anchor_count",
                    "feasible_anchor",
                    "confidence",
                    "mature",
                    "scope",
                ),
            )
        )
    return output


def _sanitized_uta_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    result = _select(
        summary or {},
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
            "high_tolerance_pairs",
            "generalization_status",
            "recommended_training_pairs",
            "attribute_weights",
            "increments",
            "validation_increments",
            "cross_validation",
            "whole_scheme_holdout",
            "robust_model_count",
            "robust_unique_model_count",
            "learning_context",
            "protocol_used_for_training",
        ),
    )
    result["robust_models"] = _sanitized_robust_models((summary or {}).get("robust_models") or [])
    return result


def _sanitized_bt_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    return _select(
        summary or {},
        (
            "model_type",
            "training_pairs",
            "training_accuracy",
            "weighted_accuracy",
            "log_loss",
            "attribute_weights",
            "weights",
            "learning_context",
            "protocol_used_for_training",
        ),
    )


def _sanitized_feasibility_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    return _select(
        summary or {},
        (
            "model_type",
            "training_samples",
            "expert_samples",
            "weak_existing_samples",
            "positive_samples",
            "negative_samples",
            "reason_guided_samples",
            "accuracy",
            "weighted_accuracy",
            "log_loss",
            "epochs",
            "feature_labels",
            "mature_boundary_count",
        ),
    )


def _model_project_schema(app: Any) -> Dict[str, Any]:
    project = app.project
    return {
        "excel_contract_version": "product_excel_v15",
        "product_code": project.product_code,
        "project_name": project.project_name,
        "workbook_fingerprint": project.workbook_fingerprint,
        "learning_fingerprint": project.learning_fingerprint,
        "attributes": [asdict(item) for item in project.attributes],
        "couplings": [asdict(item) for item in project.couplings],
        "requirement_profiles": [asdict(item) for item in project.requirement_profiles],
        "source_scheme_count": len(project.schemes),
    }


def build_frozen_model(app: Any, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Create the JSON payload used by the portable inference runtime."""
    state = copy.deepcopy(state if state is not None else app.load_state())
    feasibility_model = app.feasibility_model_from_state(state)
    completion = app.learning_completion(state)
    uta_summary = _sanitized_uta_summary(state.get("uta_model") or {})
    bt_summary = _sanitized_bt_summary(state.get("bt_model") or {})
    feasibility_summary = _sanitized_feasibility_summary(state.get("feasibility_model") or {})
    frozen_boundaries = _sanitized_boundaries(feasibility_model.boundary_summaries())

    inference_state = {
        "profile_version": int(state.get("profile_version", PROFILE_VERSION)),
        "active_protocol_profile_id": state.get("active_protocol_profile_id"),
        "evaluation_level": state.get("evaluation_level", "configured_attribute_space"),
        "uta_segments": int(state.get("uta_segments", 3)),
        "bt_weights": copy.deepcopy(state.get("bt_weights") or []),
        "bt_model": bt_summary,
        "uta_increments": copy.deepcopy(state.get("uta_increments") or uta_summary.get("increments") or []),
        "uta_model": uta_summary,
        "feasibility_weights": copy.deepcopy(state.get("feasibility_weights") or {}),
        "feasibility_model": feasibility_summary,
        "frozen_learned_boundaries": frozen_boundaries,
        # These collections must stay empty in a portable package.  Their
        # complete originals remain in userdata/state on the expert computer.
        "interactions": [],
        "preference_evidence": [],
        "feasibility_evidence": [],
        "review_evidence": [],
        "review_history": [],
        "unknown_evaluations": [],
    }

    coupling_system = app._ensure_coupling_system(state)
    coupling_keys = sorted(
        {
            key
            for edge in app.project.couplings
            for key in (edge.source_key, edge.target_key)
        }
    )
    coupling_context_anchors = [
        {
            "id": f"ANCHOR-{index:03d}",
            "params": {
                key: copy.deepcopy(scheme.params[key])
                for key in coupling_keys
                if key in scheme.params
            },
        }
        for index, scheme in enumerate(app.project.schemes, start=1)
    ]

    core = {
        "model_format_version": MODEL_FORMAT_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "profile_version": PROFILE_VERSION,
        "project_schema": _model_project_schema(app),
        "inference_state": inference_state,
        "coupling_models": copy.deepcopy(coupling_system.summaries()),
        "coupling_context_anchors": coupling_context_anchors,
        "training_summary": {
            "interaction_count": len(state.get("interactions") or []),
            "preference_evidence_count": len(state.get("preference_evidence") or []),
            "feasibility_evidence_count": len(state.get("feasibility_evidence") or []),
            "review_count": len(state.get("review_evidence") or []),
            "learning_completion": copy.deepcopy(completion),
            "uta": _select(
                uta_summary,
                (
                    "status",
                    "evidence_pairs",
                    "training_accuracy",
                    "test_accuracy",
                    "judgment_consistency",
                    "generalization_status",
                    "cross_validation",
                    "whole_scheme_holdout",
                    "robust_model_count",
                    "robust_unique_model_count",
                ),
            ),
            "bt": _select(bt_summary, ("training_pairs", "training_accuracy", "log_loss")),
            "feasibility": _select(
                feasibility_summary,
                ("expert_samples", "positive_samples", "negative_samples", "accuracy", "log_loss"),
            ),
        },
        "privacy": {
            "contains_source_workbook": False,
            "contains_source_scheme_rows": False,
            "contains_generated_scheme_rows": False,
            "contains_individual_ab_records": False,
            "contains_expert_free_text": False,
            "contains_review_history": False,
            "contains_learned_parameters": True,
            "contains_compact_coupling_context_anchors": bool(coupling_context_anchors),
            "internal_full_archive_required": True,
            "portable_feasibility_limitations": [
                "nearest_original_scheme_distance_disabled_without_raw_scheme_rows",
                "record_level_coupling_frontier_anchors_disabled_without_expert_records",
            ],
        },
    }
    digest = _json_sha256(core)
    core["model_digest"] = digest
    core["model_version"] = "effect-v15-frozen-%s-%s" % (
        app.project.learning_fingerprint[:12],
        digest[:12],
    )
    core["frozen_at"] = _utc_like_local_timestamp()
    return core


def validate_frozen_model(model: Dict[str, Any]) -> None:
    if model.get("model_format_version") != MODEL_FORMAT_VERSION:
        raise ValueError("冻结效能模型格式无效。")
    required = ("project_schema", "inference_state", "coupling_models", "privacy", "model_digest")
    missing = [name for name in required if name not in model]
    if missing:
        raise ValueError("冻结效能模型缺少字段：%s" % "、".join(missing))
    core = copy.deepcopy(model)
    core.pop("model_digest", None)
    core.pop("model_version", None)
    core.pop("frozen_at", None)
    if _json_sha256(core) != str(model.get("model_digest")):
        raise ValueError("冻结效能模型内容摘要校验失败。")
    privacy = model.get("privacy") or {}
    forbidden_true = [
        "contains_source_workbook",
        "contains_source_scheme_rows",
        "contains_generated_scheme_rows",
        "contains_individual_ab_records",
        "contains_expert_free_text",
        "contains_review_history",
    ]
    if any(bool(privacy.get(name)) for name in forbidden_true):
        raise ValueError("冻结效能模型隐私声明不符合外带要求。")


def _readme_text(model: Dict[str, Any]) -> str:
    completion = ((model.get("training_summary") or {}).get("learning_completion") or {})
    status = completion.get("title") or completion.get("status") or "未知"
    return """效能评估冻结模型包
====================

模型版本：{model_version}
训练状态：{status}
产品：{product}

本包用于把甲方电脑上训练完成的效能评估模型导入评估服务。
本包不包含原始Excel、完整原始方案行、逐条A/B记录、专家文字备注或复核历史。
上述完整资料继续保存在甲方电脑的userdata目录及状态历史归档中。

模型包包含：UTA分段效用参数、稳健模型、BT参数、可行性权重、专家边界、
单调区间前沿参数、属性结构、基准技术协议、训练验收摘要、版本和SHA-256校验。
V15区间前沿直接保存可辨识系数集合，不需要依靠原始样本重新拟合；匿名上下文锚点
仅用于兼容非前沿型软趋势，且不含原方案编号、来源、备注、效能判断或专家记录。

注意：模型参数本身仍属于甲方数据训练成果，带出前应取得甲方授权。
""".format(
        model_version=model.get("model_version"),
        status=status,
        product=(model.get("project_schema") or {}).get("project_name"),
    )


def export_frozen_model_bundle(
    app: Any,
    state: Optional[Dict[str, Any]] = None,
    source_root: Optional[Path] = None,
) -> Tuple[bytes, str]:
    """Return a self-contained model ZIP and a safe ASCII filename."""
    source_root = Path(source_root or Path(__file__).resolve().parent).resolve()
    missing = [name for name in RUNTIME_SOURCE_FILES if not (source_root / name).is_file()]
    if missing:
        raise ValueError("冻结模型运行源码不完整：%s" % "、".join(missing))
    model = build_frozen_model(app, state)
    validate_frozen_model(model)
    model_bytes = json.dumps(model, ensure_ascii=False, indent=2).encode("utf-8")

    files: List[Dict[str, Any]] = [
        {
            "path": "model/frozen_effectiveness_model.json",
            "sha256": _bytes_sha256(model_bytes),
            "size": len(model_bytes),
        }
    ]
    source_payloads: Dict[str, bytes] = {}
    for name in RUNTIME_SOURCE_FILES:
        data = (source_root / name).read_bytes()
        relative = "source/%s" % name
        source_payloads[relative] = data
        files.append({"path": relative, "sha256": _bytes_sha256(data), "size": len(data)})

    schema = model.get("project_schema") or {}
    manifest = {
        "format_version": FORMAT_VERSION,
        "created_at": model.get("frozen_at"),
        "model_version": model.get("model_version"),
        "algorithm_version": model.get("algorithm_version"),
        "profile_version": model.get("profile_version"),
        "product_code": schema.get("product_code"),
        "product_name": schema.get("project_name"),
        "learning_fingerprint": schema.get("learning_fingerprint"),
        "workbook_fingerprint": schema.get("workbook_fingerprint"),
        "model_digest": model.get("model_digest"),
        "model": "model/frozen_effectiveness_model.json",
        "source_root": "source",
        "state_mode": "frozen_learned_model_without_training_records",
        "privacy": copy.deepcopy(model.get("privacy") or {}),
        "training_summary": copy.deepcopy(model.get("training_summary") or {}),
        "files": files,
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    readme_bytes = _readme_text(model).encode("utf-8-sig")

    package = BytesIO()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("effectiveness_runtime_manifest.json", manifest_bytes)
        archive.writestr("model/frozen_effectiveness_model.json", model_bytes)
        archive.writestr("00_模型包说明.txt", readme_bytes)
        for relative, data in source_payloads.items():
            archive.writestr(relative, data)
    filename = "effectiveness_model_%s_%s.zip" % (
        str(schema.get("learning_fingerprint") or "unknown")[:16],
        str(model.get("model_digest") or "unknown")[:12],
    )
    return package.getvalue(), filename


class FrozenEffectivenessRuntime:
    """Reconstruct the V15 inference graph without the original Workbook rows."""

    def __init__(self, model_path: Union[Path, str]):
        from coupling_model import CouplingSystem, MonotonicTargetModel, SourceEffect
        from feasibility_model import ExpertFeasibilityModel
        from frontier_model import FrontierSideModel, FrontierSourceEffect, FrontierTargetModel
        from interactive_project_app import ProjectApp
        from project_excel import (
            AttributeSpec,
            CouplingSpec,
            ProjectDataset,
            RequirementProfile,
            RequirementSpec,
            SchemeRecord,
        )

        self.model_path = Path(model_path).resolve()
        model = json.loads(self.model_path.read_text(encoding="utf-8"))
        validate_frozen_model(model)
        self.model = model
        schema = model["project_schema"]
        attributes = [AttributeSpec(**item) for item in schema.get("attributes") or []]
        couplings = [CouplingSpec(**item) for item in schema.get("couplings") or []]
        profiles: List[Any] = []
        for raw_profile in schema.get("requirement_profiles") or []:
            profile = dict(raw_profile)
            profile["requirements"] = [
                RequirementSpec(**item) for item in profile.get("requirements") or []
            ]
            profiles.append(RequirementProfile(**profile))
        project = ProjectDataset(
            product_code=str(schema.get("product_code") or "FROZEN_EFFECTIVENESS"),
            project_name=str(schema.get("project_name") or "冻结效能模型"),
            workbook_path="<frozen-effectiveness-model>",
            workbook_fingerprint=str(schema.get("workbook_fingerprint") or ""),
            learning_fingerprint=str(schema.get("learning_fingerprint") or ""),
            attributes=attributes,
            schemes=[],
            couplings=couplings,
            requirement_profiles=profiles,
            warnings=["当前为不含原始训练记录的冻结推理模型。"],
        )

        app = ProjectApp.__new__(ProjectApp)
        app.seed = 20260710
        app.state_root = Path(tempfile.mkdtemp(prefix="frozen_effectiveness_"))
        app.configure_project(project)

        fitted: Dict[str, Any] = {}
        for raw_model in model.get("coupling_models") or []:
            if raw_model.get("model_status") == "direction_only" or raw_model.get("intercept") is None:
                continue
            data = copy.deepcopy(raw_model)
            if data.get("model_type") == "interval_monotone_frontier":
                effects = [FrontierSourceEffect(**item) for item in data.get("source_effects", [])]
                lower_side = (
                    FrontierSideModel(**data["lower_side"])
                    if data.get("lower_side") is not None
                    else None
                )
                upper_side = (
                    FrontierSideModel(**data["upper_side"])
                    if data.get("upper_side") is not None
                    else None
                )
                allowed_frontier = {
                    "target_key",
                    "target_label",
                    "target_unit",
                    "source_ranges",
                    "target_min",
                    "target_max",
                    "constraint_mode",
                    "sample_count",
                    "expert_constraint_count",
                    "confidence",
                    "identifiability_score",
                    "identifiability_label",
                    "evidence_summary",
                    "model_type",
                    "model_status",
                    "r2",
                    "rmse",
                    "training_coverage",
                    "condition_number",
                    "effective_rank",
                    "intercept",
                    "lower_offset",
                    "upper_offset",
                }
                fitted[data["target_key"]] = FrontierTargetModel(
                    source_effects=effects,
                    lower_side=lower_side,
                    upper_side=upper_side,
                    **{key: value for key, value in data.items() if key in allowed_frontier},
                )
                continue
            effects = [SourceEffect(**item) for item in data.pop("source_effects", [])]
            allowed = {
                "target_key",
                "target_label",
                "target_unit",
                "intercept",
                "lower_offset",
                "upper_offset",
                "rmse",
                "r2",
                "sample_count",
                "training_coverage",
                "confidence",
                "target_min",
                "target_max",
                "source_ranges",
                "identifiability_score",
                "identifiability_label",
                "condition_number",
                "effective_rank",
                "expert_constraint_count",
                "constraint_mode",
            }
            fitted[data["target_key"]] = MonotonicTargetModel(
                source_effects=effects,
                **{key: value for key, value in data.items() if key in allowed}
            )
        by_target: Dict[str, List[Any]] = {}
        for edge in project.couplings:
            if edge.target_key not in fitted:
                by_target.setdefault(edge.target_key, []).append(edge)
        context_schemes = [
            SchemeRecord(
                id=str(item.get("id") or f"ANCHOR-{index:03d}"),
                source="frozen_coupling_context_anchor",
                known_feasibility="feasible",
                notes="冻结模型局部耦合条件锚点",
                params=dict(item.get("params") or {}),
                excel_row=0,
            )
            for index, item in enumerate(model.get("coupling_context_anchors") or [], start=1)
        ]
        app.coupling_system = CouplingSystem(
            project,
            fitted,
            direction_only=by_target,
            local_context=bool(context_schemes),
            context_schemes=context_schemes,
        )
        app._runtime_coupling_signature = tuple()
        app.prior_feasibility_model = ExpertFeasibilityModel(
            project,
            app.coupling_system,
            conditional_context=True,
        )
        app.reason_options = app._build_reason_options()

        self.app = app
        self.project = project
        self.state = copy.deepcopy(model["inference_state"])
        self.model_version = str(model.get("model_version"))
        self.algorithm_version = str(model.get("algorithm_version"))
        self.profile_version = int(model.get("profile_version", PROFILE_VERSION))
        self.product_code = project.product_code
        self.product_name = project.project_name

        # Force construction now so corrupt vector lengths or unknown model
        # features fail during import instead of on the first live request.
        app.bt_model_from_state(self.state)
        app.uta_model_from_state(self.state)
        app.feasibility_model_from_state(self.state)

    def evaluate(self, params: Dict[str, Any], requirement_profile: Any = None) -> Dict[str, Any]:
        return self.app.evaluate(params, self.state, requirement_profile=requirement_profile)
