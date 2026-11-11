# -*- coding: utf-8 -*-
"""Privacy-preserving frozen runtime for the V13 regional pump model."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
import tempfile
import zipfile
from dataclasses import asdict, replace
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


FORMAT_VERSION = "system-effectiveness-frozen-runtime-package-13.0"
MODEL_FORMAT_VERSION = "system-effectiveness-frozen-model-13.0"
ALGORITHM_VERSION = "regional-contextual-requirement-anchored-joint-lp-uta-v13-pump"
RUNTIME_SOURCE_FILES = (
    "coupling_model.py",
    "feasibility_model.py",
    "frontier_model.py",
    "interactive_project_app.py",
    "preference_models.py",
    "project_excel.py",
    "requirement_model.py",
    "frozen_effectiveness_model.py",
    "system_excel.py",
    "hierarchical_uta.py",
    "regional_context.py",
    "frozen_system_model.py",
    "formula_constraints.py",
)


def _json_sha256(payload: Any) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _select(source: Dict[str, Any], keys: Sequence[str]) -> Dict[str, Any]:
    return {key: copy.deepcopy(source[key]) for key in keys if key in source}


def _sanitized_model_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    output = copy.deepcopy(summary)
    output.pop("tolerances", None)
    return output


def _product_reference_params(app: Any) -> Dict[str, Any]:
    output: Dict[str, Any] = {}
    for spec in app.project.attributes:
        values = [item.params[spec.key] for item in app.project.schemes]
        if not values:
            continue
        if spec.is_numeric:
            output[spec.key] = float(statistics.median(float(item) for item in values))
        else:
            output[spec.key] = values[0]
    return output


def _all_task_models(app: Any, state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    shared = copy.deepcopy(state.get("system_shared_model") or state.get("system_model") or {})
    models: Dict[str, Dict[str, Any]] = {}
    for task in app.system_project.tasks:
        view = copy.deepcopy(app.contextual_summary_for_task(state, task) or shared)
        view["active_scoring_task_id"] = task.task_id
        view["transfer"] = app.task_transfer_profile(state, task.task_id, shared)
        models[task.task_id] = view
    return models


def build_frozen_system_model(app: Any, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    state = copy.deepcopy(state if state is not None else app.load_state())
    feasibility_model = app.feasibility_model_from_state(state)
    models_by_task = _all_task_models(app, state)
    project = app.system_project.product
    product_schema = {
        "product_code": project.product_code,
        "project_name": project.project_name,
        "workbook_fingerprint": project.workbook_fingerprint,
        "learning_fingerprint": project.learning_fingerprint,
        "attributes": [asdict(item) for item in project.attributes],
        "couplings": [asdict(item) for item in project.couplings],
        "source_scheme_count": len(project.schemes),
        "reference_params": _product_reference_params(app),
    }
    inference_state = {
        "profile_version": int(state.get("profile_version", 15)),
        "system_profile_version": 13,
        "uta_segments": int(state.get("uta_segments", 3)),
        "active_system_task_id": state.get("active_system_task_id"),
        "system_shared_settings": copy.deepcopy(state.get("system_shared_settings") or app.default_system_settings()),
        "system_settings_by_task": copy.deepcopy(state.get("system_settings_by_task") or {}),
        "system_shared_model": _sanitized_model_summary(state.get("system_shared_model") or state.get("system_model") or {}),
        "system_models_by_task": {key: _sanitized_model_summary(value) for key, value in models_by_task.items()},
        "regional_context_version": int(state.get("regional_context_version", 1)),
        "regional_contexts": copy.deepcopy(state.get("regional_contexts") or []),
        "regional_models": {
            key: _sanitized_model_summary(value)
            for key, value in (state.get("regional_models") or {}).items()
        },
        "bt_weights": copy.deepcopy(state.get("bt_weights") or []),
        "bt_model": _select(state.get("bt_model") or {}, ("model_type", "weights", "training_pairs", "training_accuracy", "log_loss")),
        "uta_increments": copy.deepcopy(state.get("uta_increments") or []),
        "uta_model": _sanitized_model_summary(state.get("uta_model") or {}),
        "feasibility_weights": copy.deepcopy(state.get("feasibility_weights") or {}),
        "feasibility_model": copy.deepcopy(state.get("feasibility_model") or {}),
        "frozen_learned_boundaries": copy.deepcopy(feasibility_model.boundary_summaries()),
        "interactions": [],
        "preference_evidence": [],
        "feasibility_evidence": [],
        "review_evidence": [],
        "review_history": [],
        "unknown_evaluations": [],
    }
    completion_by_task: Dict[str, Any] = {}
    active = state.get("active_system_task_id")
    for task in app.system_project.tasks:
        temporary = copy.deepcopy(state)
        temporary["active_system_task_id"] = task.task_id
        temporary["system_models_by_task"] = models_by_task
        temporary["system_model"] = models_by_task[task.task_id]
        completion_by_task[task.task_id] = app.learning_completion(temporary)
    state["active_system_task_id"] = active
    core = {
        "model_format_version": MODEL_FORMAT_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "system_profile_version": 13,
        "system_contract_version": app.system_project.contract_version,
        "product_schema": product_schema,
        "system_contract": {
            "system_fingerprint": app.system_project.system_fingerprint,
            "system_learning_fingerprint": app.system_project.system_learning_fingerprint,
            "system_attributes": [asdict(item) for item in app.system_project.system_attributes],
            "mappings": [asdict(item) for item in app.system_project.mappings],
            "tasks": [asdict(item) for item in app.system_project.tasks],
            "formulas": [asdict(item) for item in app.system_project.formulas],
            "formula_calibrations": copy.deepcopy(app.formula_engine.summaries()),
        },
        "inference_state": inference_state,
        "coupling_models": copy.deepcopy(app.coupling_system.summaries()),
        "training_summary": {
            "interaction_count": len(state.get("interactions") or []),
            "preference_evidence_count": len(state.get("preference_evidence") or []),
            "feasibility_evidence_count": len(state.get("feasibility_evidence") or []),
            "review_count": len(state.get("review_evidence") or []),
            "completion_by_task": completion_by_task,
            "models_by_task": {
                key: _select(
                    value,
                    (
                        "status", "evidence_pairs", "training_accuracy", "test_accuracy",
                        "judgment_consistency", "m1_label", "m2_total_slack", "learned_alpha", "transfer",
                    ),
                )
                for key, value in models_by_task.items()
            },
            "regional_models": {
                key: _select(
                    value,
                    (
                        "status", "regional_context_id", "regional_context_label",
                        "evidence_pairs", "training_accuracy", "test_accuracy",
                        "conservative_validation_accuracy", "judgment_consistency",
                        "learned_alpha", "contextual_transfer",
                    ),
                )
                for key, value in (state.get("regional_models") or {}).items()
            },
        },
        "privacy": {
            "contains_source_workbook": False,
            "contains_source_scheme_rows": False,
            "contains_generated_scheme_rows": False,
            "contains_individual_ab_records": False,
            "contains_expert_free_text": False,
            "contains_review_history": False,
            "contains_learned_parameters": True,
            "internal_full_archive_required": True,
            "portable_feasibility_limitations": [
                "nearest_original_scheme_distance_disabled_without_raw_scheme_rows",
                "exact_historical_identity_requires_internal_full_archive",
                "partial_identification_frontiers_are_preserved_as_learned_parameter_sets",
            ],
        },
    }
    digest = _json_sha256(core)
    core["model_digest"] = digest
    core["model_version"] = "system-v13-pump-frozen-%s-%s" % (
        app.system_project.system_learning_fingerprint[:12], digest[:12]
    )
    core["frozen_at"] = datetime.now().isoformat(timespec="seconds")
    return core


def validate_frozen_system_model(model: Dict[str, Any]) -> None:
    if model.get("model_format_version") != MODEL_FORMAT_VERSION:
        raise ValueError("系统级冻结模型格式无效。")
    required = ("product_schema", "system_contract", "inference_state", "coupling_models", "privacy", "model_digest")
    missing = [key for key in required if key not in model]
    if missing:
        raise ValueError("系统级冻结模型缺少字段：%s" % "、".join(missing))
    core = copy.deepcopy(model)
    core.pop("model_digest", None)
    core.pop("model_version", None)
    core.pop("frozen_at", None)
    if _json_sha256(core) != str(model.get("model_digest")):
        raise ValueError("系统级冻结模型内容摘要校验失败。")
    privacy = model.get("privacy") or {}
    forbidden = (
        "contains_source_workbook", "contains_source_scheme_rows", "contains_generated_scheme_rows",
        "contains_individual_ab_records", "contains_expert_free_text", "contains_review_history",
    )
    if any(bool(privacy.get(key)) for key in forbidden):
        raise ValueError("系统级冻结模型隐私声明不符合外带要求。")


def _readme_text(model: Dict[str, Any]) -> str:
    schema = model.get("product_schema") or {}
    contract = model.get("system_contract") or {}
    return """系统级效能评估V13泵特化冻结模型包
==========================

模型版本：{version}
成品项目：{product}
系统指纹：{fingerprint}

本包用于只读评价新成品方案对系统任务的相对效能贡献。
包内包含通用系统属性、成品到系统映射、任务要求、系统直接属性与成品属性的
共享联合LP-UTA、各规格区域收缩模型、任务插值规则、alpha、安全公式表达式及校准参数、可行性参数、专家边界、耦合代理和训练验收摘要。

任务要求是评分时的参数化锚点。压力目标、流量要求或重量预算变化时，
共享边际效用无需重新训练；运行时按新任务与已学习区域的距离进行平滑插值，再即时重算直接贡献和综合分。

本包不包含原始Excel、原始或生成方案行、逐条A/B记录、专家自由文字或复核历史。
完整过程继续保存在甲方电脑userdata/state及state_history中。
模型参数属于甲方训练成果，带出前必须取得授权。
""".format(
        version=model.get("model_version"),
        product=schema.get("project_name"),
        fingerprint=contract.get("system_fingerprint"),
    )


def export_frozen_system_model_bundle(
    app: Any,
    state: Optional[Dict[str, Any]] = None,
    demo_source_root: Optional[Path] = None,
    system_source_root: Optional[Path] = None,
) -> Tuple[bytes, str]:
    demo_source_root = Path(demo_source_root or Path(__file__).resolve().parent.parent / "demo").resolve()
    system_source_root = Path(system_source_root or Path(__file__).resolve().parent).resolve()
    model = build_frozen_system_model(app, state)
    validate_frozen_system_model(model)
    model_bytes = json.dumps(model, ensure_ascii=False, indent=2).encode("utf-8")
    source_payloads: Dict[str, bytes] = {}
    files = [{"path": "model/frozen_system_model.json", "sha256": _bytes_sha256(model_bytes), "size": len(model_bytes)}]
    for name in RUNTIME_SOURCE_FILES:
        source = system_source_root / name
        if not source.is_file():
            source = demo_source_root / name
        if not source.is_file():
            raise ValueError("系统级冻结模型运行源码不完整：%s" % source)
        data = source.read_bytes()
        relative = "source/%s" % name
        source_payloads[relative] = data
        files.append({"path": relative, "sha256": _bytes_sha256(data), "size": len(data)})
    contract = model["system_contract"]
    manifest = {
        "format_version": FORMAT_VERSION,
        "created_at": model.get("frozen_at"),
        "model_version": model.get("model_version"),
        "algorithm_version": model.get("algorithm_version"),
        "system_fingerprint": contract.get("system_fingerprint"),
        "model_digest": model.get("model_digest"),
        "model": "model/frozen_system_model.json",
        "state_mode": "frozen_learned_model_without_training_records",
        "privacy": copy.deepcopy(model.get("privacy") or {}),
        "training_summary": copy.deepcopy(model.get("training_summary") or {}),
        "files": files,
    }
    package = BytesIO()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("system_effectiveness_runtime_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        archive.writestr("model/frozen_system_model.json", model_bytes)
        archive.writestr("00_模型包说明.txt", _readme_text(model).encode("utf-8-sig"))
        for relative, data in source_payloads.items():
            archive.writestr(relative, data)
    filename = "system_effectiveness_model_%s_%s.zip" % (
        str(contract.get("system_fingerprint") or "unknown")[:16], str(model.get("model_digest"))[:12]
    )
    return package.getvalue(), filename


class FrozenSystemRuntime:
    """Reconstruct transferable system V13 scoring without source records."""

    def __init__(self, model_path: Union[Path, str]):
        import numpy as np
        from coupling_model import CouplingSystem, MonotonicTargetModel, SourceEffect
        from feasibility_model import ExpertFeasibilityModel
        from frontier_model import FrontierSideModel, FrontierSourceEffect, FrontierTargetModel
        from interactive_project_app import ProjectApp
        from project_excel import AttributeSpec, CouplingSpec, ProjectDataset, SchemeRecord
        from system_excel import (
            SystemAttributeSpec, SystemMappingSpec, SystemProjectDataset,
            SystemRequirementSpec, SystemTask,
        )
        from formula_constraints import FormulaConstraintEngine, FormulaConstraintSpec
        from regional_context import PumpRegionPlanner, RegionalCalibrationContext

        self.model_path = Path(model_path).resolve()
        model = json.loads(self.model_path.read_text(encoding="utf-8"))
        validate_frozen_system_model(model)
        self.model = model
        schema = model["product_schema"]
        attributes = [AttributeSpec(**item) for item in schema.get("attributes") or []]
        couplings = [CouplingSpec(**item) for item in schema.get("couplings") or []]
        reference_params = copy.deepcopy(schema.get("reference_params") or {})
        reference_scheme = SchemeRecord(
            "FROZEN-REFERENCE",
            "冻结效用参考",
            "未标注",
            "仅用于重建UTA参考尺度，不代表历史实物",
            reference_params,
            0,
        )
        product = ProjectDataset(
            str(schema.get("product_code") or "FROZEN_SYSTEM"),
            str(schema.get("project_name") or "冻结系统模型"),
            "<frozen-system-model>",
            str(schema.get("workbook_fingerprint") or ""),
            str(schema.get("learning_fingerprint") or ""),
            attributes,
            [reference_scheme],
            couplings,
            [],
            ["当前为不含原始训练记录的系统级冻结推理模型。"],
        )
        contract = model["system_contract"]
        system_attributes = [SystemAttributeSpec(**item) for item in contract.get("system_attributes") or []]
        mappings = [SystemMappingSpec(**item) for item in contract.get("mappings") or []]
        tasks: List[Any] = []
        for raw in contract.get("tasks") or []:
            data = dict(raw)
            data["requirements"] = tuple(SystemRequirementSpec(**item) for item in data.get("requirements") or [])
            tasks.append(SystemTask(**data))
        formulas: List[Any] = []
        for raw in contract.get("formulas") or []:
            data = dict(raw)
            data["source_keys"] = tuple(data.get("source_keys") or ())
            data["source_labels"] = tuple(data.get("source_labels") or ())
            formulas.append(FormulaConstraintSpec(**data))
        system_project = SystemProjectDataset(
            product=product,
            system_attributes=system_attributes,
            mappings=mappings,
            tasks=tasks,
            system_fingerprint=str(contract.get("system_fingerprint") or ""),
            system_learning_fingerprint=str(
                contract.get("system_learning_fingerprint") or contract.get("system_fingerprint") or ""
            ),
            formulas=formulas,
        )

        app = ProjectApp.__new__(ProjectApp)
        app.seed = 20260817
        app.state_root = Path(tempfile.mkdtemp(prefix="frozen_system_"))
        physics_product = ProjectDataset(
            product.product_code,
            product.project_name,
            product.workbook_path,
            product.workbook_fingerprint,
            product.learning_fingerprint,
            attributes,
            [],
            couplings,
            [],
            list(product.warnings),
        )
        app.configure_project(physics_product)
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
                    "target_key", "target_label", "target_unit", "source_ranges",
                    "target_min", "target_max", "constraint_mode", "sample_count",
                    "expert_constraint_count", "confidence", "identifiability_score",
                    "identifiability_label", "evidence_summary", "model_type", "model_status",
                    "r2", "rmse", "training_coverage", "condition_number", "effective_rank",
                    "intercept", "lower_offset", "upper_offset",
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
                "target_key", "target_label", "target_unit", "intercept", "lower_offset", "upper_offset",
                "rmse", "r2", "sample_count", "training_coverage", "confidence", "target_min", "target_max",
                "source_ranges", "identifiability_score", "identifiability_label", "condition_number",
                "effective_rank", "expert_constraint_count",
                "constraint_mode",
            }
            fitted[data["target_key"]] = MonotonicTargetModel(
                source_effects=effects, **{key: value for key, value in data.items() if key in allowed}
            )
        by_target: Dict[str, List[Any]] = {}
        for edge in physics_product.couplings:
            if edge.target_key not in fitted:
                by_target.setdefault(edge.target_key, []).append(edge)
        app.coupling_system = CouplingSystem(physics_product, fitted, direction_only=by_target)
        app.prior_feasibility_model = ExpertFeasibilityModel(physics_product, app.coupling_system)
        app.reason_options = app._build_reason_options()

        self.product_app = app
        self.system_project = system_project
        self.formula_engine = FormulaConstraintEngine(
            product,
            formulas,
            calibration_summaries=contract.get("formula_calibrations") or [],
        )
        self.state = copy.deepcopy(model["inference_state"])
        self.models: Dict[str, Any] = {}
        from hierarchical_uta import HierarchicalUTAModel
        self._np = np
        self._hierarchical_model = HierarchicalUTAModel
        self._shared_summary = self.state.get("system_shared_model") or {}
        self._shared_settings = self.state.get("system_shared_settings") or {
            "alpha_prior": 0.70, "alpha_min": 0.60, "alpha_max": 0.90,
        }
        self.tasks_by_id = {task.task_id: task for task in tasks}
        self._region_distance = PumpRegionPlanner.context_distance
        self.regional_contexts = [
            RegionalCalibrationContext.from_dict(dict(item))
            for item in self.state.get("regional_contexts") or []
        ]
        for task in tasks:
            summary = (self.state.get("system_models_by_task") or {}).get(task.task_id) or self._shared_summary
            self.models[task.task_id] = self._build_task_model(task, summary)
        self.model_version = str(model.get("model_version"))

    def _contextual_summary(self, task: Any) -> Dict[str, Any]:
        """Apply the same two-neighbour shrinkage used by the online V13 app."""
        shared = copy.deepcopy(self._shared_summary)
        shared_direct = [float(value) for value in shared.get("direct_increments") or []]
        if not shared_direct:
            return shared
        candidates: List[Tuple[float, float, Dict[str, Any], Any]] = []
        regional_models = self.state.get("regional_models") or {}
        for context in self.regional_contexts:
            summary = copy.deepcopy(regional_models.get(context.region_id) or {})
            count = int(summary.get("evidence_pairs", 0))
            direct = summary.get("direct_increments") or []
            if count < 2 or len(direct) != len(shared_direct):
                continue
            distance = float(self._region_distance(task, context.calibration_task))
            kernel = math.exp(-0.5 * (distance / 0.75) ** 2)
            support = min(1.0, count / 8.0)
            candidates.append((distance, kernel * support, summary, context))
        if not candidates:
            shared["contextual_transfer"] = {
                "mode": "shared_prior_only", "confidence": 0.35,
                "nearest_distance": None, "source_regions": [],
            }
            return shared
        candidates.sort(key=lambda item: item[0])
        selected = candidates[:2]
        total_weight = sum(item[1] for item in selected)
        if total_weight <= 1e-12:
            return shared
        regional_direct = [0.0] * len(shared_direct)
        sources: List[Dict[str, Any]] = []
        effective_support = 0.0
        for distance, weight, summary, context in selected:
            normalized = weight / total_weight
            count = int(summary.get("evidence_pairs", 0))
            effective_support += normalized * count
            for index, value in enumerate(summary.get("direct_increments") or []):
                regional_direct[index] += normalized * float(value)
            sources.append(
                {
                    "region_id": context.region_id,
                    "label": context.label,
                    "distance": round(distance, 4),
                    "weight": round(normalized, 4),
                    "evidence_pairs": count,
                }
            )
        blend = min(0.70, effective_support / (effective_support + 8.0))
        output = copy.deepcopy(shared)
        output["direct_increments"] = [
            (1.0 - blend) * shared_value + blend * regional_value
            for shared_value, regional_value in zip(shared_direct, regional_direct)
        ]
        nearest = selected[0][0]
        coverage = min(1.0, effective_support / 8.0)
        output["contextual_transfer"] = {
            "mode": "regional_interpolation" if len(selected) > 1 else "nearest_region_shrinkage",
            "confidence": round(min(0.96, (1.0 / (1.0 + nearest)) * (0.60 + 0.36 * coverage)), 3),
            "nearest_distance": round(nearest, 4),
            "regional_blend": round(blend, 4),
            "source_regions": sources,
        }
        return output

    def _build_task_model(self, task: Any, summary: Optional[Dict[str, Any]] = None) -> Any:
        summary = summary or self._shared_summary
        settings = self._shared_settings
        uta = self._hierarchical_model(
            self.system_project, task,
            settings["alpha_prior"], settings["alpha_min"], settings["alpha_max"],
            int(self.state.get("uta_segments", 3)),
        )
        increments = summary.get("increments") or []
        direct_increments = summary.get("direct_increments") or []
        if (
            len(increments) != uta.space.variable_count
            or len(direct_increments) != uta.direct_space.variable_count
        ):
            raise ValueError("冻结系统模型的联合UTA增量长度与属性结构不一致。")
        uta.increments = self._np.asarray(increments, dtype=float)
        uta.direct_increments = self._np.asarray(direct_increments, dtype=float)
        for key, sides in (summary.get("direct_side_limits") or {}).items():
            if key in uta.direct_space.side_limits:
                uta.direct_space.side_limits[key].update(
                    {side: float(value) for side, value in sides.items() if side in {"left", "right"}}
                )
        uta.alpha = float(uta.direct_increments.sum()) / uta.PRODUCT_GROUP_SPAN
        uta.summary_data = dict(summary)
        uta.space.reference_params.update(summary.get("product_reference_params") or {})
        for key, stats in (summary.get("direct_history_stats") or {}).items():
            restored_history = [
                float(stats["minimum"]), float(stats["median"]), float(stats["maximum"])
            ]
            uta.direct_scorer.history_by_system[key] = restored_history
            if key in uta.direct_space.history_by_system:
                uta.direct_space.history_by_system[key] = list(restored_history)
        return uta

    def evaluate(
        self,
        params: Dict[str, Any],
        task_id: Optional[str] = None,
        requirement_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        task_id = task_id or str(self.state.get("active_system_task_id") or "")
        if task_id not in self.models:
            raise ValueError("冻结系统模型中不存在任务：%s" % task_id)
        model = self.models[task_id]
        if requirement_overrides:
            task = self.tasks_by_id[task_id]
            requirements = []
            allowed = {
                "requirement_type", "target_value", "minimum", "maximum",
                "lower_80_distance", "upper_80_distance", "overachievement_bonus",
                "lower_compliance_tolerance", "upper_compliance_tolerance",
                "score_cap", "weight", "hard_policy", "description",
            }
            unknown = set(requirement_overrides) - {item.system_key for item in task.requirements}
            if unknown:
                raise ValueError("任务要求覆盖包含未知系统属性：%s" % "、".join(sorted(unknown)))
            for requirement in task.requirements:
                changes = {
                    key: value
                    for key, value in (requirement_overrides.get(requirement.system_key) or {}).items()
                    if key in allowed
                }
                requirements.append(replace(requirement, **changes))
            runtime_task = replace(
                task,
                task_id="%s::runtime_override" % task.task_id,
                task_name="%s（运行时要求）" % task.task_name,
                requirements=tuple(requirements),
            )
            model = self._build_task_model(runtime_task, self._contextual_summary(runtime_task))
        from formula_constraints import merge_formula_assessment

        base = self.product_app.evaluate(params, self.state)
        merge_formula_assessment(base, self.formula_engine.assess(params))
        system = model.assess(params)
        base["system_assessment"] = system
        base["effectiveness_score"] = system["total_score"]
        base["effectiveness_source"] = system["method"]
        base["frozen_system_model_version"] = self.model_version
        base["task_migration"] = {
            "source_task_id": task_id,
            "runtime_requirement_override": bool(requirement_overrides),
            "shared_model_retrained": False,
            "contextual_transfer": copy.deepcopy(model.summary_data.get("contextual_transfer") or {}),
        }
        return base


__all__ = [
    "FORMAT_VERSION",
    "MODEL_FORMAT_VERSION",
    "build_frozen_system_model",
    "validate_frozen_system_model",
    "export_frozen_system_model_bundle",
    "FrozenSystemRuntime",
]
