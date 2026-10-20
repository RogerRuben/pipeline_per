# -*- coding: utf-8 -*-
from __future__ import print_function

import argparse
import hashlib
import inspect
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.common.http_service import ServiceApplication, JsonServiceError, run_service


def _sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _json_sha(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _activate_runtime_source(source_root, names):
    """Make bare imports resolve to one packaged expert-runtime source tree."""
    source_root = Path(source_root).resolve()
    source_text = str(source_root).lower()
    if str(source_root) in sys.path:
        sys.path.remove(str(source_root))
    sys.path.insert(0, str(source_root))
    for name in names:
        loaded = sys.modules.get(name)
        loaded_path = str(getattr(loaded, "__file__", "") or "").lower()
        if loaded is not None and not loaded_path.startswith(source_text):
            sys.modules.pop(name, None)


def _number_or_none(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _capability_contributors(requirement_assessment):
    result = []
    for item in (requirement_assessment or {}).get("attributes") or []:
        result.append({
            "parameter_id": item.get("attribute_key"),
            "parameter_label": item.get("attribute_label"),
            "unit": item.get("unit"),
            "current_value": item.get("value"),
            "reference_value": item.get("reference_value"),
            "weight": item.get("weight"),
            "relative_score_percent": item.get("relative_score_percent"),
            "protocol_baseline_points": item.get("protocol_baseline_points"),
            "scheme_points": item.get("scheme_points"),
            "score_delta": item.get("score_delta"),
            "scoring_basis": item.get("scoring_basis"),
            "explanation": item.get("explanation"),
        })
    result.sort(key=lambda item: abs(_number_or_none(item.get("score_delta")) or 0.0), reverse=True)
    return result


def _physical_gate(probability, status, hard_violations, boundary_violations, coupling_assessments, threshold=0.65):
    probability = _number_or_none(probability)
    probability = probability if probability is not None else 0.0
    hard = list(hard_violations or [])
    mature = [item for item in (boundary_violations or []) if bool(item.get("mature"))]
    severe_coupling = []
    for item in coupling_assessments or []:
        state = item.get("status", item.get("state"))
        severity = _number_or_none(item.get("severity")) or 0.0
        if state in ("below_band", "above_band", "outside") and severity >= 0.80:
            severe_coupling.append(item)
    if hard:
        decision = "reject_hard_violation"
    elif mature:
        decision = "reject_mature_expert_boundary"
    elif severe_coupling:
        decision = "reject_severe_coupling"
    elif probability < float(threshold):
        decision = "reject_low_feasibility_probability"
    elif status == "likely_feasible_extrapolation":
        decision = "pass_with_extrapolation"
    else:
        decision = "pass"
    return {
        "passed": decision in ("pass", "pass_with_extrapolation"),
        "decision": decision,
        "probability": round(probability, 6),
        "probability_threshold": float(threshold),
        "feasibility_status": status,
        "hard_violations": hard,
        "mature_boundary_violations": mature,
        "severe_coupling_mismatches": severe_coupling,
        "is_extrapolation": status == "likely_feasible_extrapolation",
        "gate_policy": "hard_range_then_mature_boundary_then_severe_coupling_then_probability",
    }


class OriginalRuntimeBackend(object):
    name = "original_effectiveness_runtime"

    def __init__(self, source_root, workbook, state_path=None, state_dir=None):
        self.source_root = Path(source_root).resolve()
        self.workbook = Path(workbook).resolve()
        self.state_path = Path(state_path).resolve() if state_path else None
        required = ["interactive_project_app.py", "project_excel.py", "coupling_model.py", "feasibility_model.py", "preference_models.py", "requirement_model.py"]
        missing = [name for name in required if not (self.source_root / name).is_file()]
        if missing:
            raise RuntimeError("效能源码不完整: %s" % ",".join(missing))
        _activate_runtime_source(self.source_root, [Path(name).stem for name in required])
        from interactive_project_app import PROFILE_VERSION, ProjectApp
        from project_excel import RequirementProfile, RequirementSpec
        self.profile_version = int(PROFILE_VERSION)
        self.RequirementProfile = RequirementProfile
        self.RequirementSpec = RequirementSpec
        root = Path(state_dir).resolve() if state_dir else Path(tempfile.mkdtemp(prefix="effect_service_state_"))
        self.app = ProjectApp(self.workbook, state_dir=root)
        self.supports_dynamic_protocol = (
            "requirement_profile" in inspect.signature(self.app.evaluate).parameters
        )
        self.supports_counterfactual_improvement = hasattr(self.app, "recommend_improvement")
        if self.state_path:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            fingerprint = state.get("learning_fingerprint")
            if fingerprint and fingerprint != self.app.project.learning_fingerprint:
                raise RuntimeError("State与Workbook学习指纹不一致")
            # Load the explicit state through the original app's migration path.
            # The package state is never modified; only this private runtime copy is upgraded.
            self.app.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.app.state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.state = self.app.load_state()
        else:
            self.state = self.app.load_state(reset=True)
        self.product_code = str(getattr(self.app.project, "project_code", None) or getattr(self.app.project, "product_code", None) or self.app.project.learning_fingerprint[:16])
        self.product_name = str(getattr(self.app.project, "project_name", None) or getattr(self.app.project, "product_name", None) or "效能项目")
        self.state_sha256 = _sha(self.app.state_path) if self.app.state_path.is_file() else _json_sha(self.state)
        self.algorithm_version = "V%d-PAR-UTA" % self.profile_version if self.profile_version >= 11 else "V%d-effectiveness-runtime" % self.profile_version
        self.model_version = "effect-v%d-%s-%s" % (
            self.profile_version,
            self.app.project.learning_fingerprint[:12],
            self.state_sha256[:12],
        )
        self.protocol = self._protocol_metadata(self.app.active_requirement_profile(self.state), "fixed_packaged_protocol")

    def _protocol_metadata(self, profile, mode):
        if profile is None:
            return None
        requirements = []
        reference_values = {}
        for item in profile.requirements:
            requirements.append({
                "attribute_key": item.attribute_key,
                "requirement_type": item.requirement_type,
                "target_value": item.target_value,
                "minimum": item.minimum,
                "maximum": item.maximum,
            })
            reference = item.target_value
            if reference is None:
                reference = item.minimum if item.minimum is not None else item.maximum
            reference_values[item.attribute_key] = reference
        payload = {"profile_id": profile.id, "profile_name": profile.name, "requirements": requirements}
        return {
            "profile_id": profile.id,
            "profile_name": profile.name,
            "reference_score": 100.0,
            "reference_digest": _json_sha(payload),
            "reference_values": reference_values,
            "mode": mode,
        }

    def _resolve_protocol(self, target_protocol):
        profiles = self.app.project.requirement_profile_by_id()
        if target_protocol in (None, ""):
            profile = self.app.active_requirement_profile(self.state)
            return profile, self._protocol_metadata(profile, "fixed_packaged_protocol")
        if not self.supports_dynamic_protocol:
            raise ValueError("当前原运行时包不支持逐请求动态目标协议，请升级为V11运行包")
        if isinstance(target_protocol, str):
            profile = profiles.get(target_protocol)
            if profile is None:
                raise ValueError("效能模型中不存在协议编号: %s" % target_protocol)
            return profile, self._protocol_metadata(profile, "packaged_protocol_selected_per_request")
        if not isinstance(target_protocol, dict):
            raise ValueError("target_protocol必须是协议编号或完整协议对象")
        profile_id = str(target_protocol.get("profile_id") or target_protocol.get("id") or "").strip()
        values = target_protocol.get("reference_values")
        if values is None:
            values = target_protocol.get("values")
        if values is None and target_protocol.get("requirements"):
            values = {}
            for item in target_protocol.get("requirements") or []:
                key = item.get("attribute_key") or item.get("parameter_id")
                reference = item.get("target_value")
                if reference is None:
                    reference = item.get("reference_value")
                if reference is None:
                    reference = item.get("minimum") if item.get("minimum") is not None else item.get("maximum")
                if key:
                    values[str(key)] = reference
        if values is None:
            profile = profiles.get(profile_id)
            if profile is None:
                raise ValueError("动态协议必须提供reference_values，或引用运行包中已有的profile_id")
            return profile, self._protocol_metadata(profile, "packaged_protocol_selected_per_request")
        if not isinstance(values, dict):
            raise ValueError("target_protocol.reference_values必须是属性编号到要求值的对象")
        if not profile_id:
            raise ValueError("动态协议缺少profile_id")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", profile_id):
            raise ValueError("动态协议profile_id只能包含英文字母、数字、下划线和连字符")
        by_key = self.app.project.attribute_by_key()
        unknown = sorted(set(str(key) for key in values) - set(by_key))
        if unknown:
            raise ValueError("动态协议包含模型未定义属性: %s" % "、".join(unknown))
        required_specs = [
            spec for spec in self.app.project.attributes
            if spec.is_numeric and spec.participates_utility
        ]
        missing = [spec.key for spec in required_specs if values.get(spec.key) in (None, "")]
        if missing:
            raise ValueError("动态协议缺少参与效能属性: %s" % "、".join(missing))
        requirements = []
        for spec in required_specs:
            try:
                reference = float(values[spec.key])
            except (TypeError, ValueError):
                raise ValueError("动态协议属性%s不是有效数值" % spec.key)
            if not math.isfinite(reference):
                raise ValueError("动态协议属性%s必须是有限数值" % spec.key)
            if spec.preference_direction == "higher_better":
                requirement_type, minimum, maximum = "higher_better", reference, None
            elif spec.preference_direction == "lower_better":
                requirement_type, minimum, maximum = "lower_better", None, reference
            elif spec.preference_direction == "interval":
                requirement_type, minimum, maximum = "target", None, None
            else:
                raise ValueError("属性%s参与效能但偏好方向未明确" % spec.key)
            requirements.append(self.RequirementSpec(
                attribute_key=spec.key,
                attribute_label=spec.label,
                requirement_type=requirement_type,
                target_value=reference,
                minimum=minimum,
                maximum=maximum,
                description="逐请求动态目标技术协议",
            ))
        profile = self.RequirementProfile(
            id=profile_id,
            name=str(target_protocol.get("profile_name") or target_protocol.get("name") or profile_id),
            requirements=requirements,
            direct_reuse_threshold=float(target_protocol.get("direct_reuse_threshold", 1.0)),
            improvement_threshold=float(target_protocol.get("improvement_threshold", 0.80)),
            redesign_threshold=float(target_protocol.get("redesign_threshold", 0.60)),
        )
        return profile, self._protocol_metadata(profile, "dynamic_request_protocol")

    def _coupling_models(self):
        result = []
        for model in self.app.coupling_system.models.values():
            result.append({
                "target": model.target_key,
                "target_label": model.target_label,
                "target_unit": model.target_unit,
                "intercept": model.intercept,
                "lower_offset": model.lower_offset,
                "upper_offset": model.upper_offset,
                "target_min": model.target_min,
                "target_max": model.target_max,
                "source_ranges": model.source_ranges,
                "sample_count": model.sample_count,
                "r2": model.r2,
                "confidence": model.confidence,
                "sources": [
                    {
                        "key": item.key,
                        "label": item.label,
                        "direction": item.direction,
                        "coefficient": item.normalized_coefficient,
                        "physical_coefficient": item.physical_coefficient,
                        "unit": item.source_unit,
                    }
                    for item in model.source_effects
                ],
            })
        return result

    def schema(self):
        fields = []
        for spec in self.app.project.attributes:
            dtype = {"continuous": "number", "integer": "integer", "categorical": "enum"}.get(spec.data_type, "number")
            parser = None
            if spec.data_type == "integer" and str(spec.unit or "").strip().upper() == "IP":
                dtype = "ip_grade"
                parser = "ip_grade"
            elif (
                spec.data_type == "integer"
                and float(spec.generation_min) == 0.0
                and float(spec.generation_max) == 1.0
            ):
                dtype = "boolean"
            allowed_values = None
            if spec.data_type == "categorical":
                allowed_values = sorted(set(
                    item.params.get(spec.key)
                    for item in self.app.project.schemes
                    if item.params.get(spec.key) not in (None, "")
                ), key=lambda value: str(value))
            elif dtype == "boolean":
                allowed_values = [0, 1]
            fields.append({
                "field_name": spec.key, "field_label": spec.label, "dtype": dtype, "unit": spec.unit,
                "required": True, "generation_min": spec.generation_min, "generation_max": spec.generation_max,
                "feasible_min": spec.feasible_min, "feasible_max": spec.feasible_max, "precision": spec.precision,
                "preference_direction": spec.preference_direction, "participates_generation": spec.participates_generation,
                "allowed_values": allowed_values, "parser": parser, "default_visible": True,
            })
        feasibility_model = self.app.feasibility_model_from_state(self.state)
        protocol_profiles = []
        for profile in self.app.project.requirement_profiles:
            protocol_profiles.append(self._protocol_metadata(profile, "packaged_protocol"))
        coupling_edges = [
            {
                "source": item.source_key,
                "source_label": item.source_label,
                "target": item.target_key,
                "target_label": item.target_label,
                "direction": item.direction,
                "coefficient_prior": item.coefficient_prior,
                "status": item.status,
            }
            for item in self.app.project.couplings
        ]
        return {"product_code": self.product_code, "product_name": self.product_name, "model_version": self.model_version, "backend": self.name, "fields": fields,
                "workbook_fingerprint": self.app.project.workbook_fingerprint, "learning_fingerprint": self.app.project.learning_fingerprint,
                "state_sha256": self.state_sha256, "profile_version": self.profile_version,
                "algorithm_version": self.algorithm_version, "active_protocol": self.protocol,
                "protocol_profiles": protocol_profiles,
                "target_protocol_contract": {
                    "supported": self.supports_dynamic_protocol,
                    "accepted": ["profile_id", "complete_reference_values"],
                    "directions_owned_by_model_schema": True,
                    "changes_learning_state": False,
                },
                "capabilities": {
                    "dynamic_target_protocol": self.supports_dynamic_protocol,
                    "counterfactual_improvement": self.supports_counterfactual_improvement,
                },
                "coupling_models": self._coupling_models(),
                "coupling_edges": coupling_edges,
                "learned_boundaries": feasibility_model.boundary_summaries(),
                "evaluation_level": self.state.get("evaluation_level", "configured_attribute_space")}

    def evaluate(self, params, target_protocol=None):
        missing = [spec.key for spec in self.app.project.attributes if params.get(spec.key) in (None, "")]
        if missing:
            raise ValueError("效能模型缺少必填字段: %s" % "、".join(missing))
        canonical = {}
        for spec in self.app.project.attributes:
            value = params[spec.key]
            if spec.data_type == "integer":
                value = int(round(float(value)))
            elif spec.is_numeric:
                value = float(value)
            canonical[spec.key] = value
        profile, protocol = self._resolve_protocol(target_protocol)
        if self.supports_dynamic_protocol:
            raw = self.app.evaluate(canonical, self.state, requirement_profile=profile)
        else:
            raw = self.app.evaluate(canonical, self.state)
        requirement = raw.get("requirement_assessment") or {}
        center_score = _number_or_none(raw.get("effectiveness_score"))
        p10 = _number_or_none(requirement.get("robust_p10"))
        p90 = _number_or_none(requirement.get("robust_p90"))
        conservative_score = p10 if p10 is not None else center_score
        protocol_interval = raw.get("protocol_score_interval")
        if not protocol_interval and center_score is not None:
            protocol_interval = [conservative_score, p90 if p90 is not None else center_score]
        interval_numbers = [_number_or_none(value) for value in (protocol_interval or [])]
        uncertainty_width = None
        if len(interval_numbers) >= 2 and None not in interval_numbers[:2]:
            uncertainty_width = round(abs(interval_numbers[1] - interval_numbers[0]), 6)
        contours = {}
        for item in raw.get("coupling_assessments", []):
            key = item.get("target_key") or item.get("target")
            if not key:
                continue
            contours[key] = {
                "current": item.get("actual"), "expected_lower": item.get("lower"),
                "expected_center": item.get("predicted"), "expected_upper": item.get("upper"),
                "outside": item.get("status") in ("below_band", "above_band"), "status": item.get("status"),
            }
        learned_boundaries = raw.get("learned_boundary_violations") or []
        physical_gate = _physical_gate(
            raw.get("learned_feasibility_probability"), raw.get("status"),
            raw.get("hard_violations") or [], learned_boundaries,
            raw.get("coupling_assessments") or [],
        )
        return {
            "parameters": canonical,
            "effectiveness_score": center_score,
            "capability_score": center_score,
            "conservative_capability_score": conservative_score,
            "protocol_score_interval": protocol_interval,
            "support_at_80": requirement.get("support_at_80", raw.get("protocol_support_at_80")),
            "support_at_100": requirement.get("support_at_100", raw.get("protocol_support_at_100")),
            "robust_model_count": requirement.get("robust_model_count", 0),
            "robust_unique_model_count": requirement.get("robust_unique_model_count", 0),
            "robust_conclusion": requirement.get("robust_conclusion"),
            "robust_conclusion_label": requirement.get("robust_conclusion_label", raw.get("protocol_robust_conclusion")),
            "score_uncertainty_width": uncertainty_width,
            "feasibility_probability": raw.get("learned_feasibility_probability"),
            "feasibility_status": raw.get("status"),
            "physical_gate": physical_gate,
            "effectiveness_source": raw.get("effectiveness_source"),
            "effectiveness_confidence": raw.get("effectiveness_confidence"),
            "feasibility_confidence": raw.get("feasibility_confidence"),
            "requirement_assessment": raw.get("requirement_assessment"),
            "uta_score": raw.get("uta_score"), "bt_score": raw.get("bt_score"),
            "contours": contours,
            "coupling_assessments": raw.get("coupling_assessments") or [],
            "risk_contributors": raw.get("feasibility_risk_contributors") or [],
            "hard_violations": raw.get("hard_violations") or [],
            "learned_boundary_violations": learned_boundaries,
            "experience_extrapolations": raw.get("experience_extrapolations") or [],
            "capability_contributors": _capability_contributors(requirement),
            "protocol": protocol,
            "raw_evaluation": raw,
        }

    def improve(self, params, target_protocol=None):
        if not self.supports_counterfactual_improvement:
            raise ValueError("当前原运行时包不支持反事实改进处方，请升级为V11运行包")
        evaluated = self.evaluate(params, target_protocol=target_protocol)
        profile, protocol = self._resolve_protocol(target_protocol)
        plan = self.app.recommend_improvement(
            evaluated["parameters"],
            self.state,
            evaluated["raw_evaluation"],
            requirement_profile=profile,
        )
        return {
            "parameters": evaluated["parameters"],
            "protocol": protocol,
            "current_evaluation": evaluated,
            "improvement_plan": plan,
        }


class FrozenRuntimeBackend(OriginalRuntimeBackend):
    """Read-only adapter for the V11 expert application's frozen export."""

    name = "frozen_effectiveness_runtime"

    def __init__(self, source_root, model_path, package_manifest=None):
        self.source_root = Path(source_root).resolve()
        self.model_path = Path(model_path).resolve()
        self.package_manifest = dict(package_manifest or {})
        required = [
            "interactive_project_app.py", "project_excel.py", "coupling_model.py",
            "feasibility_model.py", "preference_models.py", "requirement_model.py",
            "frozen_effectiveness_model.py",
        ]
        missing = [name for name in required if not (self.source_root / name).is_file()]
        if missing:
            raise RuntimeError("冻结效能运行源码不完整: %s" % ",".join(missing))
        if not self.model_path.is_file():
            raise RuntimeError("冻结效能模型不存在: %s" % self.model_path)
        _activate_runtime_source(self.source_root, [Path(name).stem for name in required])
        from frozen_effectiveness_model import FrozenEffectivenessRuntime
        from project_excel import RequirementProfile, RequirementSpec
        runtime = FrozenEffectivenessRuntime(self.model_path)
        self.frozen_runtime = runtime
        self.app = runtime.app
        self.state = runtime.state
        self.state_path = None
        self.workbook = None
        self.RequirementProfile = RequirementProfile
        self.RequirementSpec = RequirementSpec
        self.profile_version = int(runtime.profile_version)
        self.product_code = str(runtime.product_code)
        self.product_name = str(runtime.product_name)
        self.model_version = str(runtime.model_version)
        self.algorithm_version = str(runtime.algorithm_version)
        self.state_sha256 = str((runtime.model or {}).get("model_digest") or _sha(self.model_path))
        self.supports_dynamic_protocol = (
            "requirement_profile" in inspect.signature(self.app.evaluate).parameters
        )
        self.supports_counterfactual_improvement = hasattr(self.app, "recommend_improvement")
        self.protocol = self._protocol_metadata(
            self.app.active_requirement_profile(self.state), "fixed_packaged_protocol"
        )

    def schema(self):
        result = super(FrozenRuntimeBackend, self).schema()
        result["frozen_model_digest"] = self.state_sha256
        result["state_mode"] = "frozen_learned_model_without_training_records"
        result["privacy"] = dict((self.frozen_runtime.model or {}).get("privacy") or {})
        result["training_summary"] = dict(
            (self.frozen_runtime.model or {}).get("training_summary") or {}
        )
        return result


def backend_from_package(manifest_path):
    manifest_path = Path(manifest_path).resolve()
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    format_version = raw.get("format_version")
    if format_version not in (
        "effectiveness-original-runtime-package-1.0",
        "effectiveness-frozen-runtime-package-1.0",
    ):
        raise RuntimeError("效能运行包格式无效")
    root = manifest_path.parent
    source = root / raw.get("source_root", "source")
    # Verify packaged files before importing executable source.
    for item in raw.get("files") or []:
        path = root / item.get("path")
        if not path.is_file() or _sha(path) != item.get("sha256"):
            raise RuntimeError("效能运行包文件校验失败: %s" % item.get("path"))
    if format_version == "effectiveness-frozen-runtime-package-1.0":
        model_rel = raw.get("model")
        if not model_rel:
            raise RuntimeError("冻结效能运行包未声明模型文件")
        backend = FrozenRuntimeBackend(source, root / model_rel, package_manifest=raw)
        checks = {
            "product_code": backend.product_code,
            "model_version": backend.model_version,
            "algorithm_version": backend.algorithm_version,
            "learning_fingerprint": backend.app.project.learning_fingerprint,
            "model_digest": backend.state_sha256,
        }
        for key, actual in checks.items():
            declared = raw.get(key)
            if declared not in (None, "") and str(declared) != str(actual):
                raise RuntimeError("冻结效能运行包%s校验失败" % key)
        if raw.get("profile_version") is not None and int(raw["profile_version"]) != backend.profile_version:
            raise RuntimeError("冻结效能运行包算法Profile校验失败")
        return backend
    workbook = root / raw.get("workbook")
    state = root / raw.get("state") if raw.get("state") else None
    # Use a private temporary State copy so the installed artifact directory can
    # be mounted read-only. The packaged state file itself is never modified.
    backend = OriginalRuntimeBackend(source, workbook, state)
    if str(backend.app.project.learning_fingerprint) != str(raw.get("learning_fingerprint")):
        raise RuntimeError("效能运行包学习指纹校验失败")
    if raw.get("profile_version") is not None and int(raw.get("profile_version")) != backend.profile_version:
        raise RuntimeError("效能运行包算法版本校验失败")
    if raw.get("state_sha256") and str(raw.get("state_sha256")) != backend.state_sha256:
        raise RuntimeError("效能运行包迁移后State摘要校验失败")
    declared_model_version = str(raw.get("model_version") or "")
    legacy_model_version = "original-runtime-%s" % str(
        backend.app.project.learning_fingerprint
    )[:12]
    is_legacy_manifest = (
        declared_model_version == legacy_model_version
        and raw.get("profile_version") is None
        and not raw.get("algorithm_version")
        and not raw.get("state_sha256")
    )
    if declared_model_version and declared_model_version != backend.model_version and not is_legacy_manifest:
        raise RuntimeError("效能运行包模型版本校验失败")
    if is_legacy_manifest:
        # Preserve the identifier already referenced by existing product/model
        # records.  File hashes and the learning fingerprint were still checked.
        backend.model_version = declared_model_version
    return backend


class SnapshotBackend(object):
    name = "snapshot_json"

    def __init__(self, path):
        from app.model_runtime import EffectivenessBundleV4, EffectivenessBundle
        self.path = Path(path).resolve()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.bundle = EffectivenessBundleV4(raw, self.path) if str(raw.get("recommendation_contract_version") or "") == "4.0" else EffectivenessBundle(raw, self.path)
        self.raw = raw
        self.product_code = raw.get("product_code") or raw.get("manifest", {}).get("product_code") or self.bundle.schema.get("product_code")
        self.product_name = raw.get("product_name") or raw.get("schema", {}).get("product_name") or self.product_code
        self.model_version = raw.get("model_version") or raw.get("manifest", {}).get("model_version")
        self.profile_version = int(raw.get("profile_version") or raw.get("manifest", {}).get("profile_version") or 0)
        self.algorithm_version = raw.get("algorithm_version") or raw.get("manifest", {}).get("algorithm_version") or "portable-snapshot"
        self.state_sha256 = _sha(self.path)

    def schema(self):
        fields = []
        for spec in self.bundle.features:
            dtype = "ip_grade" if spec.get("parser") == "ip_grade" else "boolean" if spec.get("type") == "boolean" else spec.get("type", "number")
            fields.append({
                "field_name": spec.get("key"), "field_label": spec.get("label", spec.get("key")),
                "dtype": dtype, "unit": spec.get("unit", ""),
                "required": bool(spec.get("required", True)), "generation_min": spec.get("min"), "generation_max": spec.get("max"),
                "parser": spec.get("parser"), "allowed_values": spec.get("allowed_values"),
                "preference_direction": spec.get("preference", "neutral"),
                "participates_generation": spec.get("auto_adjustable", True),
                "default_visible": True,
            })
        return {"product_code": self.product_code, "product_name": self.product_name, "model_version": self.model_version, "backend": self.name, "fields": fields,
                "profile_version": self.profile_version, "algorithm_version": self.algorithm_version,
                "state_sha256": self.state_sha256, "active_protocol": None,
                "capabilities": {"dynamic_target_protocol": False, "counterfactual_improvement": False},
                "coupling_models": list(self.bundle.couplings),
                "evaluation_level": "configured_attribute_space"}

    def evaluate(self, params, target_protocol=None):
        if target_protocol not in (None, ""):
            raise ValueError("快照兼容后端不支持逐请求动态目标协议，请使用V11原运行时包")
        raw = self.bundle.evaluate(params)
        score = _number_or_none(raw.get("capability_score"))
        feasibility = raw.get("feasibility_probability")
        hard = raw.get("hard_risk_reasons") or []
        coupling = raw.get("coupling_assessments") or []
        physical_gate = _physical_gate(
            feasibility, raw.get("feasibility_status"), hard, [], coupling,
        )
        return {
            "parameters": raw.get("canonical_parameters") or params,
            "effectiveness_score": score, "capability_score": score,
            "conservative_capability_score": score,
            "protocol_score_interval": [score, score] if score is not None else None,
            "support_at_80": None, "support_at_100": None,
            "robust_model_count": 1, "robust_unique_model_count": 1,
            "robust_conclusion": "snapshot_single_model", "robust_conclusion_label": "快照单模型结果",
            "score_uncertainty_width": 0.0 if score is not None else None,
            "feasibility_probability": feasibility, "feasibility_status": raw.get("feasibility_status"),
            "physical_gate": physical_gate,
            "effectiveness_source": raw.get("capability_source", "snapshot"),
            "contours": dict((x.get("target"), {"current": x.get("actual"), "expected_lower": x.get("lower"), "expected_center": x.get("predicted"), "expected_upper": x.get("upper"), "outside": x.get("state") != "inside", "status": x.get("state")}) for x in raw.get("coupling_assessments", [])),
            "coupling_assessments": coupling,
            "risk_contributors": raw.get("risk_contributors") or [],
            "hard_violations": hard,
            "learned_boundary_violations": [],
            "experience_extrapolations": [],
            "capability_contributors": raw.get("capability_contributors") or [],
            "requirement_assessment": raw.get("requirement_assessment"),
            "protocol": None,
            "raw_evaluation": raw,
        }


class EffectivenessService(ServiceApplication):
    service_name = "effectiveness-prediction-service"
    service_version = "1.2.0"

    def __init__(self, backend):
        self.backend = backend

    def schema(self):
        data = self.backend.schema()
        data["service"] = self.service_name
        return data

    def health(self):
        data = ServiceApplication.health(self)
        data.update({"backend": self.backend.name, "product_code": self.schema().get("product_code"), "model_version": self.schema().get("model_version")})
        if isinstance(self.backend, FrozenRuntimeBackend):
            data.update({"model": str(self.backend.model_path), "state_mode": "frozen_learned_model_without_training_records", "read_only": True})
        elif isinstance(self.backend, OriginalRuntimeBackend):
            data.update({"workbook": str(self.backend.workbook), "workbook_sha256": _sha(self.backend.workbook), "state": str(self.backend.state_path or "baseline")})
        return data

    def _one(self, request):
        result = self.backend.evaluate(
            request.get("parameters") or request.get("params") or {},
            target_protocol=request.get("target_protocol"),
        )
        return {
            "request_id": request.get("request_id"), "candidate_id": request.get("candidate_id"), "success": True,
            "evaluation": {
                "effectiveness_score": result.get("effectiveness_score"), "capability_score": result.get("capability_score"),
                "conservative_capability_score": result.get("conservative_capability_score"),
                "protocol_score_interval": result.get("protocol_score_interval"),
                "support_at_80": result.get("support_at_80"), "support_at_100": result.get("support_at_100"),
                "robust_model_count": result.get("robust_model_count"),
                "robust_unique_model_count": result.get("robust_unique_model_count"),
                "robust_conclusion": result.get("robust_conclusion"),
                "robust_conclusion_label": result.get("robust_conclusion_label"),
                "score_uncertainty_width": result.get("score_uncertainty_width"),
                "feasibility_probability": result.get("feasibility_probability"), "feasibility_status": result.get("feasibility_status"),
                "effectiveness_source": result.get("effectiveness_source"), "effectiveness_confidence": result.get("effectiveness_confidence"),
                "feasibility_confidence": result.get("feasibility_confidence"), "uta_score": result.get("uta_score"), "bt_score": result.get("bt_score"),
            },
            "parameters": result.get("parameters"), "contours": result.get("contours") or {},
            "physical_gate": result.get("physical_gate") or {},
            "risk_contributors": result.get("risk_contributors") or [], "hard_violations": result.get("hard_violations") or [],
            "learned_boundary_violations": result.get("learned_boundary_violations") or [],
            "experience_extrapolations": result.get("experience_extrapolations") or [], "coupling_assessments": result.get("coupling_assessments") or [],
            "requirement_assessment": result.get("requirement_assessment"),
            "capability_contributors": result.get("capability_contributors") or [],
            "protocol": result.get("protocol"),
            "model": {"service_version": self.service_version, "model_version": self.schema().get("model_version"), "product_code": self.schema().get("product_code"), "backend": self.backend.name,
                      "algorithm_version": self.schema().get("algorithm_version"), "profile_version": self.schema().get("profile_version"),
                      "learning_fingerprint": self.schema().get("learning_fingerprint"), "state_sha256": self.schema().get("state_sha256")},
        }

    def _improve_one(self, request):
        if not hasattr(self.backend, "improve"):
            raise JsonServiceError("当前效能后端不支持反事实改进处方", 400, "improvement_unsupported")
        result = self.backend.improve(
            request.get("parameters") or request.get("params") or {},
            target_protocol=request.get("target_protocol"),
        )
        current = result.get("current_evaluation") or {}
        return {
            "request_id": request.get("request_id"),
            "success": True,
            "parameters": result.get("parameters") or {},
            "protocol": result.get("protocol"),
            "improvement_plan": result.get("improvement_plan") or {},
            "current": {
                "capability_score": current.get("capability_score"),
                "conservative_capability_score": current.get("conservative_capability_score"),
                "feasibility_probability": current.get("feasibility_probability"),
                "physical_gate": current.get("physical_gate") or {},
            },
            "model": {
                "service_version": self.service_version,
                "model_version": self.schema().get("model_version"),
                "product_code": self.schema().get("product_code"),
                "backend": self.backend.name,
                "algorithm_version": self.schema().get("algorithm_version"),
                "state_sha256": self.schema().get("state_sha256"),
            },
        }

    def handle_post(self, path, payload):
        if path == "/api/v1/evaluate":
            return self._one(payload)
        if path == "/api/v1/improve":
            return self._improve_one(payload)
        if path == "/api/v1/evaluate/batch":
            items = payload.get("items") or []
            if len(items) > 1000:
                raise JsonServiceError("单批最多1000条", 400, "batch_too_large")
            results = []
            for item in items:
                req = dict(item); req.setdefault("request_id", payload.get("request_id")); req.setdefault("product_code", payload.get("product_code"))
                req.setdefault("target_protocol", payload.get("target_protocol"))
                results.append(self._one(req))
            return {"request_id": payload.get("request_id"), "success": True, "count": len(results), "items": results, "model": self.health()}
        raise JsonServiceError("接口不存在", 404, "not_found")

    def example_request(self):
        # Keep the online test page executable even when the model declares
        # more than a dozen required fields.  A complete scheme is sent.
        values = {}
        for field in self.schema().get("fields", []):
            lo, hi = field.get("generation_min"), field.get("generation_max")
            dtype = str(field.get("dtype") or "").lower()
            allowed = field.get("allowed_values") or field.get("categories") or []
            if dtype in ("enum", "categorical", "category") and allowed:
                value = allowed[0]
            elif lo is not None and hi is not None:
                value = (float(lo) + float(hi)) / 2.0
            elif lo is not None:
                value = lo
            elif dtype in ("boolean", "bool"):
                value = False
            else:
                value = 0.0
            values[field.get("field_name")] = value
        return {"request_id": "EFFECT-DEMO-001", "product_code": self.schema().get("product_code"), "parameters": values}

    def openapi(self):
        return {"openapi": "3.0.3", "info": {"title": "效能与可行性预测服务 API", "version": self.service_version, "description": "优先运行V11专家软件冻结模型包，并兼容原效能工程Workbook+State与快照模式。"},
                "paths": {"/health": {"get": {"summary": "健康检查与效能模型状态"}}, "/api/v1/schema": {"get": {"summary": "效能字段契约"}},
                          "/api/v1/evaluate": {"post": {"summary": "单方案效能、可行性和轮廓评价"}}, "/api/v1/evaluate/batch": {"post": {"summary": "批量评价，最多1000条"}},
                          "/api/v1/improve": {"post": {"summary": "按需生成V11反事实改进处方"}},
                          "/openapi.json": {"get": {"summary": "OpenAPI 3.0文档"}}, "/docs": {"get": {"summary": "简易接口前端"}}}}


def build_backend(args):
    if args.source_root and args.workbook:
        return OriginalRuntimeBackend(args.source_root, args.workbook, args.state or None, args.state_dir or None)
    if args.package and Path(args.package).is_file():
        return backend_from_package(args.package)
    if args.snapshot:
        return SnapshotBackend(args.snapshot)
    raise RuntimeError("必须配置--package，或配置--source-root与--workbook，或配置--snapshot")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("EFFECT_SERVICE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("EFFECT_SERVICE_PORT", "18102")))
    parser.add_argument("--source-root", default=os.environ.get("EFFECT_SOURCE_ROOT", ""))
    parser.add_argument("--workbook", default=os.environ.get("EFFECT_WORKBOOK", ""))
    parser.add_argument("--state", default=os.environ.get("EFFECT_STATE", ""))
    parser.add_argument("--state-dir", default=os.environ.get("EFFECT_STATE_DIR", ""))
    parser.add_argument("--package", default=os.environ.get("EFFECT_RUNTIME_PACKAGE", str(ROOT / "services" / "effectiveness_service" / "model" / "current" / "effectiveness_runtime_manifest.json")))
    parser.add_argument("--snapshot", default=os.environ.get("EFFECT_SNAPSHOT", str(ROOT / "models" / "effectiveness_bundle.json")))
    args = parser.parse_args()
    run_service(EffectivenessService(build_backend(args)), args.host, args.port)


if __name__ == "__main__":
    main()
