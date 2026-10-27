# -*- coding: utf-8 -*-
"""Single source of truth for the V21.2 optimization-scenario strategy layer."""
from __future__ import print_function

import copy
import json
from pathlib import Path


VALID_SORT_KEYS = {
    "comprehensive", "price", "capability", "cost_effectiveness",
    "tag_match", "feasibility",
}
VALID_DIRECTIONS = {"asc", "desc"}
VALID_INTENSITIES = {"balanced", "target", "extreme"}


class ScenarioPolicyService(object):
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_file():
            fallback = Path(__file__).resolve().parents[1] / "config" / "scenario_config.json"
            if fallback.is_file():
                self.path = fallback
            else:
                self.path = Path(__file__).resolve().parent / 'defaults' / 'scenario_config.json'
        self.config = self._load()

    def _load(self):
        raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        scenarios = raw.get("scenarios") or {}
        if not scenarios:
            raise ValueError("scenario_config.json缺少scenarios")
        default = str(raw.get("default_scenario") or "balanced")
        if default not in scenarios:
            raise ValueError("默认优化场景不存在: %s" % default)
        for code, item in scenarios.items():
            default_sort = item.get("default_sort") or {}
            if default_sort.get("key") not in VALID_SORT_KEYS:
                raise ValueError("场景%s默认排序字段无效" % code)
            if default_sort.get("direction") not in VALID_DIRECTIONS:
                raise ValueError("场景%s默认排序方向无效" % code)
            for intensity, weights in (item.get("intensity_weights") or {}).items():
                self._validate_weights(code, intensity, weights)
            self._validate_weights(code, "default", item.get("ranking_weights") or {})
        return raw

    @staticmethod
    def _validate_weights(code, intensity, weights):
        keys = {"technical", "price", "capability"}
        if set(weights) != keys:
            raise ValueError("场景%s/%s权重字段必须为technical/price/capability" % (code, intensity))
        total = sum(float(weights[key]) for key in keys)
        if abs(total - 1.0) > 1e-8 or any(float(weights[key]) < 0 for key in keys):
            raise ValueError("场景%s/%s权重必须非负且合计为1" % (code, intensity))

    def catalog(self):
        scenarios = []
        for code, source in (self.config.get("scenarios") or {}).items():
            item = copy.deepcopy(source)
            item["scenario"] = code
            scenarios.append(item)
        return {
            "version": self.config.get("version"),
            "default_scenario": self.config.get("default_scenario"),
            "system_default_sort": copy.deepcopy(self.config.get("system_default_sort") or {}),
            "intensities": [
                {"key": "balanced", "label": "平衡"},
                {"key": "target", "label": "偏向目标"},
                {"key": "extreme", "label": "极致优化"},
            ],
            "scenarios": scenarios,
        }

    def resolve(self, request):
        request = dict(request or {})
        has_scenario = not bool(request.get("_scenario_legacy")) and request.get("scenario") not in (None, "")
        default_code = str(self.config.get("default_scenario") or "balanced")
        code = str(request.get("scenario") or default_code)
        if code not in self.config["scenarios"]:
            code = default_code
        source = copy.deepcopy(self.config["scenarios"][code])
        intensity = str(request.get("optimization_intensity") or "target")
        if intensity not in VALID_INTENSITIES:
            intensity = "target"
        weights = copy.deepcopy((source.get("intensity_weights") or {}).get(intensity) or source["ranking_weights"])
        default_sort = copy.deepcopy(source["default_sort"])
        ranking = request.get("ranking_policy") or {}
        requested_key = str(ranking.get("sort_key") or request.get("sort_by") or "")
        requested_direction = str(ranking.get("sort_direction") or request.get("sort_order") or "").lower()
        declared_source = str(request.get("sort_source") or ranking.get("source") or "")
        if not has_scenario:
            system = copy.deepcopy(self.config.get("system_default_sort") or {"key": "comprehensive", "direction": "desc", "display_name": "综合推荐"})
            key = requested_key if requested_key in VALID_SORT_KEYS else system["key"]
            direction = requested_direction if requested_direction in VALID_DIRECTIONS else ("asc" if key == "price" else "desc")
            applied_source = "user_override" if requested_key in VALID_SORT_KEYS else "system_default"
        elif declared_source == "user_override" and requested_key in VALID_SORT_KEYS:
            key = requested_key
            direction = requested_direction if requested_direction in VALID_DIRECTIONS else ("asc" if key == "price" else "desc")
            applied_source = "user_override"
        else:
            key, direction, applied_source = default_sort["key"], default_sort["direction"], "scenario_default"
        display_names = {
            "comprehensive": "需求匹配优先 · 同等匹配下综合评分最高",
            "price": "需求匹配优先 · 同等匹配下价格最低" if direction == "asc" else "需求匹配优先 · 同等匹配下价格最高",
            "capability": "需求匹配优先 · 同等匹配下效能最低" if direction == "asc" else "需求匹配优先 · 同等匹配下效能最高",
            "cost_effectiveness": "需求匹配优先 · 同等匹配下效费比最低" if direction == "asc" else "需求匹配优先 · 同等匹配下效费比最高",
            "tag_match": "需求匹配优先 · 同等匹配下标签匹配率最低" if direction == "asc" else "需求匹配优先 · 同等匹配下标签匹配率最高",
            "feasibility": "需求匹配优先 · 同等匹配下可行概率最低" if direction == "asc" else "需求匹配优先 · 同等匹配下可行概率最高",
        }
        if applied_source == "user_override":
            labels = {
                "comprehensive": "综合评分", "price": "价格", "capability": "效能",
                "cost_effectiveness": "效费比", "tag_match": "标签匹配率", "feasibility": "可行概率",
            }
            display_names[key] = "需求匹配优先 · 同等匹配下%s%s" % (
                labels.get(key, key), "升序" if direction == "asc" else "降序"
            )
        options = dict(request.get("scenario_options") or {})
        policy = {
            "scenario": code,
            "scenario_name": source.get("scenario_name") or code,
            "optimization_intensity": intensity,
            "default_sort": default_sort,
            "ranking_weights": weights,
            "description": source.get("description") or "",
            "short_description": source.get("short_description") or "",
            "result_explanation": source.get("result_explanation") or "",
            "options": copy.deepcopy(source.get("options") or []),
            "scenario_options": options,
            "strategy_active": has_scenario,
            "applied_ranking": {
                "sort_key": key,
                "sort_direction": direction,
                "display_name": display_names.get(key, key),
                "source": applied_source,
                "source_display_name": "用户调整" if applied_source == "user_override" else (source.get("scenario_name") + "场景推荐" if applied_source == "scenario_default" else "系统默认"),
            },
        }
        return policy

    def apply(self, request):
        effective = dict(request or {})
        policy = self.resolve(effective)
        effective["scenario"] = policy["scenario"]
        effective["_scenario_legacy"] = not policy.get("strategy_active")
        effective["optimization_intensity"] = policy["optimization_intensity"]
        effective["sort_by"] = policy["applied_ranking"]["sort_key"]
        effective["sort_order"] = policy["applied_ranking"]["sort_direction"]
        effective["sort_source"] = policy["applied_ranking"]["source"]
        effective["_scenario_policy"] = policy if policy.get("strategy_active") else {}
        options = policy.get("scenario_options") or {}
        constraint_sources = {}
        for option in policy.get("options") or []:
            key = str(option.get("key") or "")
            if not key:
                continue
            business_value = effective.get(key)
            scenario_value = options.get(key)
            if business_value not in (None, ""):
                effective[key] = float(business_value) if option.get("type") == "number" else business_value
                options[key] = effective[key]
                constraint_sources[key] = (
                    "business_target_overrode_scenario_alias"
                    if scenario_value not in (None, "") and str(scenario_value) != str(business_value)
                    else "business_target"
                )
            elif scenario_value not in (None, ""):
                effective[key] = float(scenario_value) if option.get("type") == "number" else scenario_value
                options[key] = effective[key]
                constraint_sources[key] = "scenario_alias"
            else:
                options[key] = None
                constraint_sources[key] = "unrestricted"
        policy["scenario_options"] = options
        policy["applied_constraints"] = {
            "min_capability": effective.get("min_capability"),
            "max_price": effective.get("max_price"),
            "sources": constraint_sources,
        }
        return effective, policy
