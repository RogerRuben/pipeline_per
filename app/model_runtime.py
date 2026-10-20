# -*- coding: utf-8 -*-
"""In-process model runtime for V19.

The runtime deliberately uses only the Python standard library so the deployed
recommendation system can run on Windows 7 / Python 3.8 without NumPy, SciPy or
pickle dependencies.
"""
from __future__ import print_function

import ast
import hashlib
import json
import math
import random
from pathlib import Path

try:
    from .model_contract_v4 import (
        CONTRACT_VERSION as CONTRACT_V4_VERSION,
        evaluate_effectiveness as evaluate_effectiveness_v4,
        evaluate_price as evaluate_price_v4,
        validate_bundle as validate_bundle_v4,
    )
except ImportError:
    from model_contract_v4 import (
        CONTRACT_VERSION as CONTRACT_V4_VERSION,
        evaluate_effectiveness as evaluate_effectiveness_v4,
        evaluate_price as evaluate_price_v4,
        validate_bundle as validate_bundle_v4,
    )


MODEL_CONTRACT_VERSION = "4.0-compatible"
# The effectiveness schema is the complete product/editor schema. Price models
# declare their own raw-input contract and may use a subset or additional fields
# with an explicit missing-value policy.
_ALLOWED_FUNCS = {
    "abs": abs,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "log": math.log,
    "log1p": math.log1p,
    "exp": math.exp,
    "pow": pow,
}


def _expression_names(expression):
    try:
        tree = ast.parse(str(expression), mode="eval")
    except SyntaxError as exc:
        raise ModelContractError("模型表达式语法错误: %s" % exc)
    return set(node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id not in _ALLOWED_FUNCS)


def safe_expression(expression, variables):
    """Evaluate a numeric model expression with a small, auditable AST subset."""
    tree = ast.parse(str(expression), mode="eval")

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if hasattr(ast, "Num") and isinstance(node, ast.Num):
            return float(node.n)
        if isinstance(node, ast.Name):
            if node.id not in variables:
                raise ModelInputError("模型表达式缺少原始属性%s" % node.id, [node.id], "model")
            return float(variables[node.id])
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add): return left + right
            if isinstance(node.op, ast.Sub): return left - right
            if isinstance(node.op, ast.Mult): return left * right
            if isinstance(node.op, ast.Div): return left / right
            if isinstance(node.op, ast.Pow): return left ** right
            if isinstance(node.op, ast.Mod): return left % right
            raise ModelContractError("模型表达式包含不支持的二元运算")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _ALLOWED_FUNCS:
            return float(_ALLOWED_FUNCS[node.func.id](*[visit(arg) for arg in node.args]))
        if isinstance(node, ast.BoolOp):
            values = [bool(visit(item)) for item in node.values]
            if isinstance(node.op, ast.And): return 1.0 if all(values) else 0.0
            if isinstance(node.op, ast.Or): return 1.0 if any(values) else 0.0
            raise ModelContractError("模型表达式包含不支持的布尔运算")
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
            left, right = visit(node.left), visit(node.comparators[0])
            op = node.ops[0]
            if isinstance(op, ast.Gt): return 1.0 if left > right else 0.0
            if isinstance(op, ast.GtE): return 1.0 if left >= right else 0.0
            if isinstance(op, ast.Lt): return 1.0 if left < right else 0.0
            if isinstance(op, ast.LtE): return 1.0 if left <= right else 0.0
            if isinstance(op, ast.Eq): return 1.0 if left == right else 0.0
            if isinstance(op, ast.NotEq): return 1.0 if left != right else 0.0
        raise ModelContractError("模型表达式包含不允许的语法: %s" % type(node).__name__)

    value = visit(tree)
    if not math.isfinite(value):
        raise ModelInputError("模型表达式产生非有限值", [], "model")
    return value



def clamp(value, lo, hi):
    return max(float(lo), min(float(hi), float(value)))


def parse_bool(value):
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if float(value) != 0.0 else 0
    text = str(value or "").strip().lower()
    if text in ("1", "true", "yes", "y", "on", "有", "是", "具备", "支持"):
        return 1
    if text in ("0", "false", "no", "n", "off", "无", "否", "不具备", "不支持", ""):
        return 0
    raise ValueError("无法识别布尔值: %s" % value)


def _sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _safe_float(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError("指标%s不是有效数值: %s" % (name, value))
    if not math.isfinite(result):
        raise ValueError("指标%s不是有限数值" % name)
    return result


class ModelContractError(ValueError):
    pass


class ModelInputError(ValueError):
    def __init__(self, message, missing_features=None, model_kind=None):
        ValueError.__init__(self, message)
        self.missing_features = list(missing_features or [])
        self.model_kind = model_kind


class EffectivenessBundle(object):
    def __init__(self, bundle, artifact_path=None):
        self.bundle = bundle
        self.artifact_path = str(artifact_path or "")
        self.schema = bundle["schema"]
        self.features = self.schema["features"]
        self.by_key = dict((item["key"], item) for item in self.features)
        self.couplings = bundle.get("coupling_models", [])
        self.feasibility = bundle["feasibility_model"]
        self.capability = bundle["capability_model"]
        self.training_ranges = bundle.get("training_ranges", {})

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), path)

    def validate(self):
        errors, warnings = [], []
        manifest = self.bundle.get("manifest", {})
        schema = self.bundle.get("schema", {})
        if not manifest.get("model_version"):
            errors.append("效能模型缺少manifest.model_version")
        if not schema.get("product_code"):
            errors.append("效能模型缺少schema.product_code")
        keys = [item.get("key") for item in schema.get("features", [])]
        if len(keys) != len(set(keys)):
            errors.append("效能模型指标编号重复")
        if not keys:
            errors.append("效能模型schema.features为空")
        order = schema.get("generator_order", [])
        if set(order) != set(keys):
            errors.append("generator_order必须与schema.features完全一致")
        for item in schema.get("features", []):
            key = item.get("key")
            if item.get("required", True) is not True:
                warnings.append("效能属性%s被标记为非必填；当前项目约定效能属性即完整可编辑成品属性，建议设为必填" % key)
            if item.get("type") not in ("number", "boolean"):
                errors.append("指标%s的模型类型仅支持number/boolean" % key)
            if item.get("min") is None or item.get("max") is None:
                errors.append("指标%s缺少min/max" % key)
            elif float(item["min"]) > float(item["max"]):
                errors.append("指标%s的min大于max" % key)
            if key not in self.training_ranges:
                warnings.append("指标%s没有训练分布范围，异常值检测将退化为模型绝对范围" % key)
        coupling_targets = set()
        for model in self.couplings:
            target = model.get("target")
            if target not in self.by_key:
                errors.append("耦合模型目标指标不存在: %s" % target)
            if target in coupling_targets:
                warnings.append("耦合模型目标指标重复: %s" % target)
            coupling_targets.add(target)
            for source in model.get("sources", []):
                if source.get("key") not in self.by_key:
                    errors.append("耦合模型源指标不存在: %s" % source.get("key"))
        for key in self.capability.get("weights", {}):
            if key not in self.by_key:
                errors.append("效能模型权重引用不存在的指标: %s" % key)
        for rule in self.bundle.get("hard_rules", []):
            try:
                expressions = [rule.get("expression")] if rule.get("expression") else [rule.get("left", "0"), rule.get("right", "0")]
                dependencies = set()
                for expression in expressions:
                    dependencies.update(_expression_names(expression))
                unknown = sorted(dependencies - set(self.by_key))
                if unknown:
                    errors.append("硬规则%s引用未知属性: %s" % (rule.get("rule_id") or rule.get("name") or "未命名", "、".join(unknown)))
            except Exception as exc:
                errors.append("硬规则%s表达式无效: %s" % (rule.get("rule_id") or rule.get("name") or "未命名", exc))
        return errors, warnings

    def normalize(self, key, value, clip=True):
        spec = self.by_key[key]
        z = (_safe_float(value, key) - float(spec["min"])) / max(float(spec["max"]) - float(spec["min"]), 1e-12)
        return clamp(z, 0.0, 1.0) if clip else z

    def canonicalize(self, params, strict_unknown=False):
        params = params or {}
        if strict_unknown:
            unknown = sorted(set(params) - set(self.by_key))
            if unknown:
                raise ValueError("模型未定义这些指标: %s" % ", ".join(unknown))
        canonical = {}
        missing = [spec["key"] for spec in self.features if spec.get("required", True) and params.get(spec["key"]) in (None, "")]
        if missing:
            labels = [self.by_key[key].get("label", key) for key in missing]
            raise ModelInputError("效能计算缺少必填成品属性: %s" % "、".join(labels), missing, "effectiveness")
        for spec in self.features:
            key = spec["key"]
            if params.get(key) in (None, ""):
                policy = spec.get("missing_policy", "reject")
                if policy == "default" and "default_value" in spec:
                    value = spec["default_value"]
                else:
                    raise ModelInputError("效能属性%s缺失且没有可用缺失策略" % spec.get("label", key), [key], "effectiveness")
            else:
                value = params.get(key)
            if spec.get("parser") == "ip_grade":
                text = str(value).strip().upper()
                if text.startswith("IP"):
                    text = text[2:]
                value = _safe_float(text or spec["min"], spec.get("label", key))
            if spec["type"] == "boolean":
                canonical[key] = parse_bool(value)
            else:
                canonical[key] = _safe_float(value, spec.get("label", key))
        return canonical

    def anomaly_assessment(self, params):
        """Detect range/OOD anomalies without requiring a third-party library.

        The saved 1%--99% training ranges are treated as the trusted operating
        domain. A normalized distance outside that range is accumulated to form
        an interpretable multivariate OOD score.
        """
        items = []
        distance_sum = 0.0
        severe_count = 0
        for spec in self.features:
            if spec["type"] == "boolean":
                continue
            key = spec["key"]
            actual = float(params[key])
            absolute_lo, absolute_hi = float(spec["min"]), float(spec["max"])
            training = self.training_ranges.get(key, [absolute_lo, absolute_hi])
            train_lo, train_hi = float(training[0]), float(training[1])
            width = max(train_hi - train_lo, (absolute_hi - absolute_lo) * 0.05, 1e-9)
            if actual < train_lo:
                d = (train_lo - actual) / width
                state = "below_training"
            elif actual > train_hi:
                d = (actual - train_hi) / width
                state = "above_training"
            else:
                d = 0.0
                state = "inside_training"
            if d > 0:
                severity = "error" if d >= 0.75 else "warning"
                severe_count += 1 if severity == "error" else 0
                items.append({
                    "parameter_id": key,
                    "label": spec.get("label", key),
                    "actual": actual,
                    "training_lower": train_lo,
                    "training_upper": train_hi,
                    "distance": round(d, 4),
                    "severity": severity,
                    "state": state,
                    "message": "%s超出模型主要训练范围[%s, %s]" % (spec.get("label", key), train_lo, train_hi),
                })
                distance_sum += min(d, 3.0)
        numeric_count = max(sum(1 for item in self.features if item["type"] != "boolean"), 1)
        score = distance_sum / numeric_count
        if severe_count >= 2 or score >= 0.18:
            status = "out_of_domain"
        elif items:
            status = "caution"
        else:
            status = "in_domain"
        return {
            "status": status,
            "score": round(score, 6),
            "is_anomaly": status != "in_domain",
            "items": items,
            "message": {
                "in_domain": "输入位于模型主要训练范围内。",
                "caution": "部分指标接近或超出训练数据主要范围，预测可信度下降。",
                "out_of_domain": "输入明显偏离训练数据，预测仅供参考，建议补充样本并重新训练。",
            }[status],
        }

    def coupling_band(self, model, params):
        value = float(model["intercept"])
        contributions = []
        for src in model.get("sources", []):
            z = self.normalize(src["key"], params[src["key"]])
            part = float(src["coefficient"]) * z
            value += part
            contributions.append({"key": src["key"], "normalized": z, "contribution": part})
        target_spec = self.by_key[model["target"]]
        span = float(target_spec["max"]) - float(target_spec["min"])
        predicted = float(target_spec["min"]) + value * span
        lower = float(target_spec["min"]) + (value + float(model["lower_offset"])) * span
        upper = float(target_spec["min"]) + (value + float(model["upper_offset"])) * span
        return {
            "predicted": clamp(predicted, target_spec["min"], target_spec["max"]),
            "lower": clamp(min(lower, upper), target_spec["min"], target_spec["max"]),
            "upper": clamp(max(lower, upper), target_spec["min"], target_spec["max"]),
            "contributions": contributions,
        }

    @staticmethod
    def _rule_holds(left, operator, right):
        if operator == "gte": return left >= right
        if operator == "gt": return left > right
        if operator == "lte": return left <= right
        if operator == "lt": return left < right
        if operator == "eq": return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
        if operator == "neq": return not math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
        raise ModelContractError("效能模型硬规则包含未知比较关系: %s" % operator)

    def engineered_risk_features(self, params):
        """Build deployable risk features entirely from the model artifact.

        Product-specific hard checks are stored in ``hard_rules`` in the
        effectiveness bundle. The runtime only provides generic expression,
        comparison, coupling and range operators, so a new product never
        requires editing this Python module.
        """
        hard = 0.0
        reasons = []
        variables = dict((key, float(value)) for key, value in params.items())
        for rule in self.bundle.get("hard_rules", []):
            try:
                if rule.get("expression"):
                    ok = bool(safe_expression(rule["expression"], variables))
                else:
                    left = safe_expression(rule.get("left", "0"), variables)
                    right = safe_expression(rule.get("right", "0"), variables)
                    ok = self._rule_holds(left, rule.get("operator", "gte"), right)
            except Exception as exc:
                raise ModelContractError("硬规则%s无法计算: %s" % (rule.get("rule_id") or rule.get("name") or "未命名", exc))
            if not ok:
                hard += float(rule.get("weight", 1.0))
                reasons.append(rule.get("message") or rule.get("name") or "违反模型硬规则")

        coupling_violation = 0.0
        coupling_near = 0.0
        coupling_details = []
        for model in self.couplings:
            band = self.coupling_band(model, params)
            actual = float(params[model["target"]])
            width = max(band["upper"] - band["lower"], 1e-9)
            if actual < band["lower"]:
                distance = (band["lower"] - actual) / width
                state = "below"
                coupling_violation += min(distance, 3.0)
            elif actual > band["upper"]:
                distance = (actual - band["upper"]) / width
                state = "above"
                coupling_violation += min(distance, 3.0)
            else:
                distance = min(actual - band["lower"], band["upper"] - actual) / width
                state = "inside"
                if distance < 0.12:
                    coupling_near += (0.12 - distance) / 0.12
            coupling_details.append({
                "target": model["target"],
                "actual": round(actual, 5),
                "lower": round(band["lower"], 5),
                "upper": round(band["upper"], 5),
                "predicted": round(band["predicted"], 5),
                "state": state,
            })

        center_distance = 0.0
        numeric_count = 0
        for spec in self.features:
            if spec["type"] == "boolean":
                continue
            numeric_count += 1
            center_distance += abs(self.normalize(spec["key"], params[spec["key"]]) - 0.5)
        center_distance /= max(numeric_count, 1)
        anomaly = self.anomaly_assessment(params)
        values = {
            "hard_violation_count": hard,
            "coupling_violation": coupling_violation,
            "coupling_violation_severity": coupling_violation,
            "coupling_near_boundary": coupling_near,
            "parameter_center_distance": center_distance,
            "center_distance": center_distance,
            "outside_training_range": float(anomaly.get("score", 0.0)),
        }
        return values, reasons, coupling_details

    def feasibility_probability(self, params):
        features, reasons, coupling_details = self.engineered_risk_features(params)
        linear = float(self.feasibility["intercept"])
        for key, weight in self.feasibility["weights"].items():
            linear += float(weight) * float(features.get(key, 0.0))
        linear = max(-40.0, min(40.0, linear))
        probability = 1.0 / (1.0 + math.exp(-linear))
        contributors = []
        for key, weight in self.feasibility["weights"].items():
            contribution = float(weight) * float(features.get(key, 0.0))
            if contribution < -0.02:
                contributors.append({"feature": key, "contribution": contribution})
        contributors.sort(key=lambda x: x["contribution"])
        return probability, reasons, coupling_details, contributors[:5]

    def capability_score(self, params):
        value = float(self.capability["intercept"])
        contributions = []
        for key, weight in self.capability["weights"].items():
            spec = self.by_key[key]
            z = self.normalize(key, params[key])
            if spec.get("preference") == "lower":
                z = 1.0 - z
            elif spec.get("preference") == "neutral":
                z = 0.5
            contribution = float(weight) * z
            value += contribution
            contributions.append({"key": key, "contribution": contribution})
        score = clamp(value, 0.0, 100.0)
        contributions.sort(key=lambda x: abs(x["contribution"]), reverse=True)
        return score, contributions[:8]

    def evaluate(self, params):
        canonical = self.canonicalize(params)
        anomaly = self.anomaly_assessment(canonical)
        probability, reasons, coupling_details, risk_contributors = self.feasibility_probability(canonical)
        capability, capability_contributors = self.capability_score(canonical)
        status = "likely_feasible" if probability >= 0.75 else "borderline" if probability >= 0.45 else "high_risk"
        return {
            "canonical_parameters": canonical,
            "capability_score": round(capability, 4),
            "feasibility_probability": round(probability, 6),
            "feasibility_status": status,
            "hard_risk_reasons": reasons,
            "coupling_assessments": coupling_details,
            "risk_contributors": risk_contributors,
            "capability_contributors": capability_contributors,
            "anomaly_assessment": anomaly,
            "model_version": self.bundle["manifest"]["model_version"],
            "capability_source": self.bundle.get("capability_source", "snapshot"),
        }

    def generate(self, count=10, seed=20260724, constraints=None, min_feasibility=0.75, pool_size=2200):
        constraints = constraints or {}
        rng = random.Random(seed)
        target_models = dict((item["target"], item) for item in self.couplings)
        generated, seen = [], set()
        for _ in range(max(pool_size, count * 120)):
            params = {}
            infeasible = False
            for key in self.schema["generator_order"]:
                spec = self.by_key[key]
                rule = constraints.get(key, {})
                lo = max(float(spec["min"]), float(rule.get("min", spec["min"])))
                hi = min(float(spec["max"]), float(rule.get("max", spec["max"])))
                if lo > hi:
                    raise ValueError("指标%s的筛选条件与模型范围没有交集" % spec.get("label", key))
                if "allowed" in rule:
                    choices = [float(v) for v in rule["allowed"] if lo <= float(v) <= hi]
                    if not choices:
                        infeasible = True
                        break
                    value = rng.choice(choices)
                elif key in target_models:
                    band = self.coupling_band(target_models[key], params)
                    margin = 0.06 * max(band["upper"] - band["lower"], 0.0)
                    inner_lo = max(lo, band["lower"] + margin)
                    inner_hi = min(hi, band["upper"] - margin)
                    if inner_lo > inner_hi:
                        inner_lo, inner_hi = max(lo, band["lower"]), min(hi, band["upper"])
                    if inner_lo > inner_hi:
                        value = clamp(band["predicted"], lo, hi)
                    else:
                        value = rng.uniform(inner_lo, inner_hi)
                elif spec["type"] == "boolean":
                    value = int(lo) if lo == hi else (1 if rng.random() < 0.72 else 0)
                else:
                    # Triangular sampling favours the learned domain centre while
                    # preserving enough diversity for recommendation.
                    mode = (lo + hi) / 2.0
                    value = rng.triangular(lo, hi, mode)
                if spec.get("allowed_values"):
                    choices = [float(item) for item in spec.get("allowed_values") if lo <= float(item) <= hi]
                    value = min(choices, key=lambda item: abs(item - value)) if choices else round(value)
                params[key] = int(value) if spec["type"] == "boolean" else round(value, 5)
            if infeasible:
                continue
            result = self.evaluate(params)
            if result["feasibility_probability"] < min_feasibility or result["hard_risk_reasons"]:
                continue
            if result["anomaly_assessment"]["status"] == "out_of_domain":
                continue
            signature = tuple(round(float(params[k]), 2) for k in sorted(params))
            if signature in seen:
                continue
            seen.add(signature)
            generated.append({"params": params, "evaluation": result})
        generated.sort(key=lambda x: (x["evaluation"]["capability_score"], x["evaluation"]["feasibility_probability"]), reverse=True)
        selected = []
        while generated and len(selected) < count:
            if not selected:
                selected.append(generated.pop(0))
                continue
            best_index, best_value = 0, -1e9
            for index, item in enumerate(generated[:500]):
                novelty = min(self._distance(item["params"], chosen["params"]) for chosen in selected)
                value = (0.58 * item["evaluation"]["capability_score"] / 100.0 +
                         0.22 * item["evaluation"]["feasibility_probability"] + 0.20 * novelty)
                if value > best_value:
                    best_value, best_index = value, index
            selected.append(generated.pop(best_index))
        for index, item in enumerate(selected):
            item["scheme_id"] = "GEN-FC-%03d" % (index + 1)
            item["source"] = "learned_feasible_contour"
        return selected

    def _distance(self, left, right):
        total, count = 0.0, 0
        for spec in self.features:
            key = spec["key"]
            if spec["type"] == "boolean":
                total += abs(int(left[key]) - int(right[key]))
            else:
                total += abs(self.normalize(key, left[key]) - self.normalize(key, right[key]))
            count += 1
        return total / max(count, 1)


class PriceBundle(object):
    """Deployment-safe JSON price model with an independent input contract.

    The original notebook/runtime builds raw and engineered features, standardizes
    them with saved mean/scale, predicts log-price with an ensemble, and applies
    a saved residual interval. V19 preserves that format while making raw feature
    dependencies and missing-value policies explicit.
    """
    def __init__(self, bundle, artifact_path=None):
        self.bundle = bundle
        self.artifact_path = str(artifact_path or "")
        self.names = list(bundle["feature_names"])
        self.raw_contract = list((bundle.get("input_contract") or {}).get("raw_features") or [])
        if not self.raw_contract:
            inferred = sorted(self.inferred_raw_dependencies())
            self.raw_contract = [
                {"key": key, "source": "product_parameter", "required": True, "missing_policy": "reject"}
                for key in inferred
            ]
        self.raw_by_key = dict((item["key"], item) for item in self.raw_contract)

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), path)

    def feature_dependencies(self, feature_name):
        expressions = self.bundle.get("feature_engineering") or {}
        expression = expressions.get(feature_name)
        if expression:
            return _expression_names(expression)
        return {feature_name}

    def inferred_raw_dependencies(self):
        result = set()
        for name in self.names:
            result.update(self.feature_dependencies(name))
        return result

    def validate(self, effectiveness_schema=None):
        errors, warnings = [], []
        manifest = self.bundle.get("manifest", {})
        if not manifest.get("model_version"):
            errors.append("价格模型缺少manifest.model_version")
        n = len(self.names)
        if not n:
            errors.append("价格模型feature_names为空")
        if len(self.bundle.get("mean", [])) != n or len(self.bundle.get("scale", [])) != n:
            errors.append("价格模型feature_names、mean、scale长度不一致")
        models = self.bundle.get("models", [])
        weights = self.bundle.get("ensemble_weights", [])
        if not models:
            errors.append("价格模型没有任何子模型")
        if len(models) != len(weights):
            errors.append("价格模型子模型数量与集成权重数量不一致")
        for index, model in enumerate(models):
            if len(model.get("coefficients", [])) != n:
                errors.append("价格子模型%d系数长度错误" % (index + 1))
        if weights and abs(sum(float(v) for v in weights) - 1.0) > 0.02:
            warnings.append("价格模型集成权重之和不是1")
        if len(self.bundle.get("residual_interval_log", [])) != 2:
            errors.append("价格模型缺少有效的残差区间")

        raw_keys = [item.get("key") for item in self.raw_contract]
        if any(not key for key in raw_keys):
            errors.append("价格模型input_contract存在空指标编号")
        if len(raw_keys) != len(set(raw_keys)):
            errors.append("价格模型input_contract指标编号重复")
        dependencies = self.inferred_raw_dependencies()
        undeclared = sorted(dependencies - set(raw_keys))
        if undeclared:
            errors.append("价格模型未声明这些原始输入依赖: %s" % ", ".join(undeclared))
        expressions = self.bundle.get("feature_engineering") or {}
        for name in self.names:
            if name in raw_keys:
                continue
            if not expressions.get(name):
                errors.append("价格派生特征%s缺少feature_engineering表达式" % name)
                continue
            try:
                _expression_names(expressions[name])
            except Exception as exc:
                errors.append("价格派生特征%s表达式无效: %s" % (name, exc))
        allowed_policies = {"reject", "default", "training_mean", "zero", "constant"}
        for item in self.raw_contract:
            key = item.get("key")
            policy = item.get("missing_policy", "reject")
            if policy not in allowed_policies:
                errors.append("价格输入%s的missing_policy无效: %s" % (key, policy))
            if policy in ("default", "constant") and item.get("default_value") is None:
                errors.append("价格输入%s使用%s策略但缺少default_value" % (key, policy))
            if policy == "training_mean" and item.get("training_mean") is None:
                errors.append("价格输入%s使用training_mean策略但缺少training_mean" % key)

        if effectiveness_schema:
            effect_by_key = dict((item.get("key"), item) for item in effectiveness_schema.get("features", []))
            for item in self.raw_contract:
                key = item.get("key")
                if key in effect_by_key:
                    e = effect_by_key[key]
                    if item.get("type") and item.get("type") != e.get("type"):
                        errors.append("共享指标%s的数据类型不一致" % key)
                    if item.get("unit") not in (None, "") and e.get("unit") not in (None, "") and item.get("unit") != e.get("unit"):
                        errors.append("共享指标%s的单位不一致: %s / %s" % (key, e.get("unit"), item.get("unit")))
                else:
                    policy = item.get("missing_policy", "reject")
                    if policy == "reject":
                        errors.append("价格模型必填原始输入%s不属于完整效能/成品属性，且没有缺失处理策略" % key)
                    else:
                        warnings.append("价格模型输入%s不在效能属性中，将按%s策略处理" % (key, policy))
        return errors, warnings

    @staticmethod
    def _coerce(value, item):
        kind = item.get("type", "number")
        if kind == "boolean":
            return float(parse_bool(value))
        if item.get("parser") == "ip_grade":
            text = str(value).strip().upper()
            if text.startswith("IP"):
                text = text[2:]
            value = text
        return _safe_float(value, item.get("label") or item.get("key"))

    def resolve_raw_inputs(self, params):
        resolved, imputed, missing = {}, [], []
        params = params or {}
        for item in self.raw_contract:
            key = item["key"]
            value = params.get(key)
            if value not in (None, ""):
                resolved[key] = self._coerce(value, item)
                continue
            policy = item.get("missing_policy", "reject")
            if policy == "reject":
                missing.append(key)
                continue
            if policy in ("default", "constant"):
                value = item.get("default_value")
            elif policy == "training_mean":
                value = item.get("training_mean")
            elif policy == "zero":
                value = 0.0
            else:
                missing.append(key)
                continue
            resolved[key] = self._coerce(value, item)
            imputed.append({"parameter_id": key, "policy": policy, "value": resolved[key]})
        if missing:
            raise ModelInputError("价格计算缺少必填属性: %s" % "、".join(missing), missing, "price")
        return resolved, imputed

    def feature_vector(self, params):
        resolved, imputed = self.resolve_raw_inputs(params)
        computed = dict(resolved)
        expressions = self.bundle.get("feature_engineering") or {}
        unresolved = [name for name in self.names if name not in computed]
        while unresolved:
            progressed = False
            for name in list(unresolved):
                expression = expressions.get(name)
                if not expression:
                    raise ModelContractError("价格模型包含运行时无法构造的特征: %s；请在模型包feature_engineering中提供表达式" % name)
                dependencies = _expression_names(expression)
                if dependencies.issubset(set(computed)):
                    computed[name] = safe_expression(expression, computed)
                    unresolved.remove(name)
                    progressed = True
            if not progressed:
                details = []
                for name in unresolved:
                    missing = sorted(_expression_names(expressions.get(name, "0")) - set(computed))
                    details.append("%s缺少%s" % (name, "、".join(missing)))
                raise ModelContractError("价格特征工程存在循环依赖或未声明依赖：%s" % "；".join(details))
        return computed, imputed, resolved

    def predict(self, params):
        raw, imputed, resolved = self.feature_vector(params)
        vector, extreme = [], []
        for i, name in enumerate(self.names):
            mean = float(self.bundle["mean"][i])
            scale = max(float(self.bundle["scale"][i]), 1e-12)
            z = (float(raw[name]) - mean) / scale
            vector.append(z)
            if abs(z) > 3.5:
                extreme.append({"feature": name, "z_score": round(z, 3), "severity": "error" if abs(z) > 5 else "warning"})
        logs = []
        for model in self.bundle["models"]:
            value = float(model["intercept"])
            for coef, x in zip(model["coefficients"], vector):
                value += float(coef) * x
            logs.append(value)
        weights = self.bundle["ensemble_weights"]
        log_price = sum(float(w) * value for w, value in zip(weights, logs))
        price = math.exp(log_price)
        low = math.exp(log_price + float(self.bundle["residual_interval_log"][0]))
        high = math.exp(log_price + float(self.bundle["residual_interval_log"][1]))
        domain_status = "out_of_domain" if sum(1 for x in extreme if x["severity"] == "error") >= 2 else "caution" if extreme else "in_domain"
        return {
            "predicted_price_wan": round(max(price, 0.01), 4),
            "price_interval_wan": [round(max(low, 0.01), 4), round(max(high, 0.01), 4)],
            "model_version": self.bundle["manifest"]["model_version"],
            "model_source": "json_ridge_ensemble",
            "feature_anomalies": extreme,
            "domain_assessment": {"status": domain_status, "is_anomaly": domain_status != "in_domain", "items": extreme},
            "imputed_features": imputed,
            "resolved_raw_inputs": resolved,
        }



class EffectivenessBundleV4(object):
    """Contract-4 effectiveness snapshot exposed through the legacy app API."""
    contract_version = "4.0"

    def __init__(self, bundle, artifact_path=None):
        self.bundle = bundle
        self.artifact_path = str(artifact_path or "")
        features = []
        training_ranges = {}
        for item in bundle.get("feature_schema") or []:
            dtype = str(item.get("dtype") or "number")
            value_type = "boolean" if dtype == "boolean" else "number" if dtype in ("number", "integer", "ip_grade") else dtype
            search_type = {
                "number": "continuous", "integer": "integer", "ip_grade": "integer",
                "boolean": "boolean", "enum": "unordered_enum", "text": "text",
            }.get(dtype, "auto")
            spec = {
                "key": item["field_name"], "label": item.get("field_label") or item["field_name"],
                "unit": item.get("unit") or "", "type": value_type,
                "min": item.get("generation_min", item.get("training_min", 0.0)),
                "max": item.get("generation_max", item.get("training_max", 1.0)),
                "preference": {"higher_better":"higher", "lower_better":"lower"}.get(item.get("preference_direction"), "neutral"),
                "required": bool(item.get("required", True)), "missing_policy": item.get("missing_policy", "reject"),
                "search_type": search_type, "allowed_values": item.get("allowed_values") or item.get("categories"),
                "decimal_places": int(item.get("precision", 3)), "description": item.get("description") or "",
                "adjustment_hint": item.get("adjustment_hint") or "", "auto_adjustable": bool(item.get("participates_generation", True)),
                "editable": bool(item.get("editable", True)), "source": "product_parameter", "dtype": dtype,
                "training_mean": item.get("training_mean"), "default_value": item.get("default_value"),
            }
            if dtype == "ip_grade": spec["parser"] = "ip_grade"
            features.append(spec)
            training_ranges[spec["key"]] = [item.get("feasible_min", spec["min"]), item.get("feasible_max", spec["max"])]
        self.schema = {
            "product_code": bundle.get("product_code"),
            "product_name": bundle.get("product_name") or (bundle.get("training_report") or {}).get("project_name") or bundle.get("product_code"),
            "features": features, "generator_order": [x["key"] for x in features],
            "schema_role": "effectiveness_attributes",
        }
        self.features = features
        self.by_key = {x["key"]: x for x in features}
        self.training_ranges = training_ranges
        self.couplings = []
        for model in bundle.get("coupling_models") or []:
            self.couplings.append({
                "target": model.get("target_key"), "intercept": model.get("intercept"),
                "lower_offset": model.get("lower_offset"), "upper_offset": model.get("upper_offset"),
                "target_min": model.get("target_min"), "target_max": model.get("target_max"),
                "sources": [
                    {"key": x.get("key"), "direction": x.get("direction"), "coefficient": x.get("normalized_coefficient", 0.0)}
                    for x in model.get("source_effects") or []
                ],
                "source_ranges": model.get("source_ranges") or {}, "sample_count": model.get("sample_count"), "r2": model.get("r2"),
            })
        self.feasibility = bundle.get("feasibility_model") or {}
        self.capability = bundle.get("preference_uta") or bundle.get("preference_bt") or {}
        # Reuse stable canonicalization and generic sampling implementation.
        synthetic_couplings=[]
        for model in self.couplings:
            synthetic_couplings.append({
                "target": model["target"], "intercept": model["intercept"],
                "lower_offset": model["lower_offset"], "upper_offset": model["upper_offset"],
                "sources": model["sources"],
            })
        self._legacy_helper = EffectivenessBundle({
            "manifest": {"model_version": bundle.get("model_version"), "product_code": bundle.get("product_code")},
            "schema": self.schema, "training_ranges": training_ranges,
            "coupling_models": synthetic_couplings,
            "feasibility_model": {"intercept": 0.0, "weights": {}},
            "capability_model": {"intercept": 0.0, "weights": {}}, "hard_rules": [],
        }, artifact_path)

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), path)

    def validate(self):
        return validate_bundle_v4(self.bundle, "effectiveness"), []

    def canonicalize(self, params, strict_unknown=False):
        return self._legacy_helper.canonicalize(params, strict_unknown=strict_unknown)

    def normalize(self, key, value, clip=True):
        return self._legacy_helper.normalize(key, value, clip=clip)

    def coupling_band(self, model, params):
        # Contract-4 coefficients use normalized source values and normalized target.
        target = model["target"]
        pred_n = float(model.get("intercept", 0.0))
        for source in model.get("sources") or []:
            key = source["key"]
            lo, hi = (model.get("source_ranges") or {}).get(key, [self.by_key[key]["min"], self.by_key[key]["max"]])
            z = clamp((float(params[key])-float(lo))/max(float(hi)-float(lo),1e-12),0.0,1.0)
            pred_n += float(source.get("coefficient",0.0))*z
        spec=self.by_key[target]; tmin=float(model.get("target_min",spec["min"])); tmax=float(model.get("target_max",spec["max"]))
        den=lambda z:tmin+float(z)*(tmax-tmin)
        predicted=den(pred_n); lower=clamp(den(pred_n+float(model.get("lower_offset",0.0))),tmin,tmax); upper=clamp(den(pred_n+float(model.get("upper_offset",0.0))),tmin,tmax)
        return {"predicted":predicted,"lower":min(lower,upper),"upper":max(lower,upper)}

    def evaluate(self, params):
        canonical = self.canonicalize(params)
        raw = evaluate_effectiveness_v4(self.bundle, canonical)
        contour=[]
        for item in raw.get("feasible_contours") or []:
            actual=float(item["actual"]); lower=float(item["lower"]); upper=float(item["upper"])
            state="inside" if lower<=actual<=upper else "below" if actual<lower else "above"
            contour.append({"target":item.get("parameter_id"),"actual":actual,"predicted":item.get("predicted"),"lower":lower,"upper":upper,"state":state,"outside":state!="inside"})
        domain=[]
        for key in raw.get("effectiveness_domain_warnings") or []:
            spec=self.by_key.get(key,{})
            domain.append({"parameter_id":key,"label":spec.get("label",key),"severity":"warning","message":"%s超出效能模型范围"%spec.get("label",key)})
        probability=float(raw.get("feasibility_probability",0.0))
        status="likely_feasible" if probability>=0.75 else "borderline" if probability>=0.45 else "high_risk"
        return {
            "canonical_parameters":canonical,"capability_score":float(raw.get("capability_score",0.0)),
            "feasibility_probability":probability,"feasibility_status":status,
            "hard_risk_reasons":raw.get("hard_risk_reasons") or [],"coupling_assessments":contour,
            "risk_contributors":raw.get("risk_contributors") or [],"capability_contributors":raw.get("capability_contributors") or [],
            "anomaly_assessment":{"status":"out_of_domain" if domain else "in_domain","is_anomaly":bool(domain),"score":len(domain)/max(len(self.features),1),"items":domain,"message":"部分效能输入超出模型范围。" if domain else "效能输入位于模型范围内。"},
            "model_version":self.bundle.get("model_version"),"effectiveness_confidence":raw.get("effectiveness_confidence"),
            "effectiveness_source":raw.get("effectiveness_source"),"requirement_assessment":raw.get("requirement_assessment"),
            "generic_preference_score":raw.get("generic_preference_score"),
        }

    def generate(self, count=10, seed=20260724, constraints=None, min_feasibility=0.75, pool_size=2200):
        # Mixed-app generator performs the real neighborhood search. This fallback
        # only supplies valid starting points for direct runtime callers.
        rng=random.Random(seed); constraints=constraints or {}; output=[]
        for _ in range(max(pool_size,count*50)):
            params={}
            for spec in self.features:
                rule=constraints.get(spec["key"],{}); lo=max(float(spec["min"]),float(rule.get("min",spec["min"]))); hi=min(float(spec["max"]),float(rule.get("max",spec["max"])))
                if lo>hi: raise ValueError("指标%s的筛选条件与模型范围没有交集"%spec.get("label",spec["key"]))
                if spec["type"]=="boolean": value=rng.randint(int(lo),int(hi))
                elif spec.get("search_type")=="integer": value=int(round(rng.uniform(lo,hi)))
                else: value=rng.uniform(lo,hi)
                params[spec["key"]]=value
            ev=self.evaluate(params)
            if ev["feasibility_probability"]>=min_feasibility:
                output.append({"params":ev["canonical_parameters"],"evaluation":ev})
                if len(output)>=count: break
        return output


class PriceBundleV4(object):
    """Contract-4 price ensemble exposed through the legacy app API."""
    contract_version = "4.0"

    def __init__(self, bundle, artifact_path=None):
        self.bundle=bundle; self.artifact_path=str(artifact_path or "")
        schema_map={x.get("field_name"):x for x in bundle.get("feature_schema") or []}
        self.raw_contract=[]
        for binding in bundle.get("model_input_bindings") or []:
            if not binding.get("enabled",True): continue
            schema=schema_map.get(binding.get("field_name"),{}); dtype=binding.get("dtype") or schema.get("dtype") or "number"
            item={
                "key":binding.get("field_name"),"label":binding.get("field_label") or schema.get("field_label") or binding.get("field_name"),
                "source":binding.get("source_type","product_parameter"),"type":"boolean" if dtype=="boolean" else "number" if dtype in ("number","integer","ip_grade") else dtype,
                "dtype":dtype,"unit":binding.get("unit") or schema.get("unit") or "","required":bool(binding.get("required",True)),
                "missing_policy":binding.get("missing_policy","reject"),"training_mean":binding.get("training_mean",schema.get("training_mean")),
                "default_value":binding.get("configured_value"),"min":schema.get("generation_min",schema.get("training_min")),
                "max":schema.get("generation_max",schema.get("training_max")),"training_min":schema.get("training_min"),"training_max":schema.get("training_max"),
                "search_type":schema.get("search_type") or {"integer":"integer","ip_grade":"integer","boolean":"boolean","enum":"unordered_enum"}.get(dtype,"continuous"),
                "allowed_values":schema.get("allowed_values") or schema.get("categories"),"decimal_places":int(schema.get("precision",3)),
                "description":schema.get("description") or "","adjustment_hint":schema.get("adjustment_hint") or "",
                "editable":bool(schema.get("editable",True)),"auto_adjustable":bool(schema.get("auto_adjustable",True)),
            }
            if dtype=="ip_grade": item["parser"]="ip_grade"
            self.raw_contract.append(item)
        self.raw_by_key={x["key"]:x for x in self.raw_contract}; self.names=list((bundle.get("preprocessing") or {}).get("feature_order") or [])

    @classmethod
    def load(cls,path): return cls(json.loads(Path(path).read_text(encoding="utf-8")),path)

    def validate(self,effectiveness_schema=None):
        errors=validate_bundle_v4(self.bundle,"price"); warnings=[]
        if effectiveness_schema:
            e_map={x["key"]:x for x in effectiveness_schema.get("features",[])}
            for item in self.raw_contract:
                if item["key"] in e_map:
                    e=e_map[item["key"]]
                    if item.get("type")!=e.get("type"): errors.append("共享指标%s的数据类型不一致"%item["key"])
                    if item.get("unit") and e.get("unit") and item["unit"]!=e["unit"]: errors.append("共享指标%s的单位不一致"%item["key"])
                elif item.get("source")=="product_parameter" and item.get("missing_policy","reject")=="reject":
                    errors.append("价格专用字段%s必须配置training_mean/default等缺失策略，以便旧方案可计算"%item["key"])
        return errors,warnings

    @staticmethod
    def _coerce(value,item):
        if item.get("type")=="boolean": return float(parse_bool(value))
        if item.get("parser")=="ip_grade":
            text=str(value).strip().upper(); value=text[2:] if text.startswith("IP") else text
        if item.get("type") in ("enum","text"): return value
        return _safe_float(value,item.get("label") or item.get("key"))

    def resolve_raw_inputs(self,params):
        params=params or {}; resolved={}; imputed=[]; missing=[]
        for item in self.raw_contract:
            key=item["key"]; value=params.get(key)
            if value not in (None,""): resolved[key]=self._coerce(value,item); continue
            policy=item.get("missing_policy","reject")
            if policy=="reject": missing.append(key); continue
            if policy=="training_mean": value=item.get("training_mean")
            elif policy in ("default","constant"): value=item.get("default_value")
            elif policy=="zero": value=0
            if value is None: missing.append(key); continue
            resolved[key]=self._coerce(value,item); imputed.append({"parameter_id":key,"policy":policy,"value":resolved[key]})
        if missing: raise ModelInputError("价格计算缺少必填属性: %s"%"、".join(missing),missing,"price")
        return resolved,imputed

    def predict(self,params):
        resolved,imputed=self.resolve_raw_inputs(params); raw=evaluate_price_v4(self.bundle,resolved)
        domain=[{"feature":x,"severity":"warning"} for x in raw.get("price_domain_warnings") or []]
        return {"predicted_price_wan":raw["predicted_price_wan"],"price_interval_wan":raw["price_interval_wan"],
            "model_version":self.bundle.get("model_version"),"model_source":"contract4_json_ensemble","feature_anomalies":domain,
            "domain_assessment":{"status":"caution" if domain else "in_domain","is_anomaly":bool(domain),"items":domain},
            "imputed_features":imputed,"resolved_raw_inputs":resolved,"price_confidence":raw.get("price_confidence")}


class IntegratedModelRuntime(object):
    def __init__(self,model_dir):
        self.model_dir=Path(model_dir); self.effectiveness_path=self.model_dir/"effectiveness_bundle.json"; self.price_path=self.model_dir/"price_bundle.json"
        self.effectiveness=None; self.price=None; self.contract_report=None; self.contract_version=None; self.reload()

    @staticmethod
    def _kind(path):
        raw=json.loads(Path(path).read_text(encoding="utf-8"))
        return "4.0" if str(raw.get("recommendation_contract_version") or "")=="4.0" else "3.0"

    @staticmethod
    def validate_pair(effectiveness_path,price_path):
        errors=[]; warnings=[]; effectiveness=None; price=None
        try: ekind=IntegratedModelRuntime._kind(effectiveness_path)
        except Exception as exc: errors.append("效能模型无法读取: %s"%exc); ekind=None
        try: pkind=IntegratedModelRuntime._kind(price_path)
        except Exception as exc: errors.append("价格模型无法读取: %s"%exc); pkind=None
        if ekind and pkind and ekind!=pkind: errors.append("价格与效能模型契约版本不一致: %s / %s"%(ekind,pkind))
        if not errors:
            try:
                effectiveness=(EffectivenessBundleV4 if ekind=="4.0" else EffectivenessBundle).load(effectiveness_path)
                e,w=effectiveness.validate(); errors.extend(e); warnings.extend(w)
            except Exception as exc: errors.append("效能模型无法加载: %s"%exc); effectiveness=None
            try:
                price=(PriceBundleV4 if pkind=="4.0" else PriceBundle).load(price_path)
                e,w=price.validate(effectiveness.schema if effectiveness else None); errors.extend(e); warnings.extend(w)
            except Exception as exc: errors.append("价格模型无法加载: %s"%exc); price=None
        shared=[]; price_only=[]; effect_only=[]
        if effectiveness and price:
            ep=str(effectiveness.bundle.get("product_code") or effectiveness.bundle.get("manifest",{}).get("product_code") or effectiveness.schema.get("product_code"))
            pp=str(price.bundle.get("product_code") or price.bundle.get("manifest",{}).get("product_code"))
            if ep!=pp: errors.append("两个模型的product_code不一致: %s / %s"%(ep,pp))
            effect_keys=set(effectiveness.by_key); price_keys={x["key"] for x in price.raw_contract if x.get("source","product_parameter")=="product_parameter"}
            shared=sorted(effect_keys & price_keys); price_only=sorted(price_keys-effect_keys); effect_only=sorted(effect_keys-price_keys)
            if not shared: warnings.append("价格与效能模型没有共享产品属性，请确认字段映射")
        return {"valid":not errors,"errors":errors,"warnings":warnings,"contract_version":ekind if ekind==pkind else None,
            "effectiveness":effectiveness,"price":price,"feature_compatibility":{"effectiveness_features":sorted(effectiveness.by_key) if effectiveness else [],
            "price_raw_features":sorted(x["key"] for x in price.raw_contract) if price else [],"shared_features":shared,
            "effectiveness_only_features":effect_only,"price_only_features":price_only,"policy":"union_product_schema_with_shared_fields"}}

    def reload(self):
        report=self.validate_pair(self.effectiveness_path,self.price_path); self.contract_report={k:v for k,v in report.items() if k not in ("effectiveness","price")}
        if not report["valid"]: raise ModelContractError("；".join(report["errors"]))
        self.effectiveness=report["effectiveness"]; self.price=report["price"]; self.contract_version=report.get("contract_version") or "3.0"
        return self.contract_report

    @property
    def schema(self): return self.effectiveness.schema

    def all_feature_specs(self):
        result=[]; seen=set()
        for spec in self.effectiveness.features:
            item=dict(spec); item["model_role"]="shared" if spec["key"] in self.feature_roles()["shared_features"] else "effectiveness_only"; item["default_visible"]=True
            result.append(item); seen.add(spec["key"])
        for spec in self.price.raw_contract:
            if spec.get("source","product_parameter")!="product_parameter" or spec["key"] in seen: continue
            item=dict(spec); item.setdefault("min",item.get("training_min")); item.setdefault("max",item.get("training_max")); item.setdefault("preference","neutral")
            item["model_role"]="price_only"; item["default_visible"]=False; result.append(item); seen.add(spec["key"])
        return result

    def feature_roles(self):
        comp=(self.contract_report or {}).get("feature_compatibility",{})
        return {"shared_features":list(comp.get("shared_features") or []),"effectiveness_only_features":list(comp.get("effectiveness_only_features") or []),"price_only_features":list(comp.get("price_only_features") or [])}

    def evaluate(self,params):
        eff=self.effectiveness.evaluate(params); canonical=dict(eff["canonical_parameters"]); price_inputs=dict(params or {}); price_inputs.update(canonical)
        price=self.price.predict(price_inputs); merged=dict(canonical); merged.update(price.get("resolved_raw_inputs") or {})
        ce=eff["capability_score"]/max(price["predicted_price_wan"],1e-9); effect_anomaly=dict(eff["anomaly_assessment"]); price_anomaly=dict(price.get("domain_assessment") or {"status":"in_domain","is_anomaly":False,"items":[]})
        order={"in_domain":0,"caution":1,"out_of_domain":2}; combined_status=max((effect_anomaly.get("status","in_domain"),price_anomaly.get("status","in_domain")),key=lambda x:order.get(x,0))
        combined={"status":combined_status,"is_anomaly":combined_status!="in_domain","score":effect_anomaly.get("score",0.0),"items":effect_anomaly.get("items",[]),"price_feature_anomalies":price.get("feature_anomalies",[]),"message":"效能与价格模型均位于主要训练域内。" if combined_status=="in_domain" else "至少一个模型存在边界或域外输入。"}
        confidence="high"
        if combined_status=="caution" or eff["feasibility_status"]=="borderline" or price.get("imputed_features"): confidence="medium"
        if combined_status=="out_of_domain" or eff["feasibility_status"]=="high_risk": confidence="low"
        return {"predicted_price_wan":price["predicted_price_wan"],"price_interval_wan":price["price_interval_wan"],"capability_score":eff["capability_score"],
            "cost_effectiveness":round(ce,4),"feasibility_probability":eff["feasibility_probability"],"feasibility_status":eff["feasibility_status"],
            "prediction_confidence":confidence,"anomaly_assessment":combined,"effectiveness_anomaly_assessment":effect_anomaly,"price_anomaly_assessment":price_anomaly,
            "price_imputed_features":price.get("imputed_features",[]),"risk_contributors":eff.get("risk_contributors",[]),"hard_risk_reasons":eff.get("hard_risk_reasons",[]),
            "coupling_assessments":eff.get("coupling_assessments",[]),"capability_contributors":eff.get("capability_contributors",[]),
            "requirement_assessment":eff.get("requirement_assessment"),"effectiveness_source":eff.get("effectiveness_source"),
            "model_versions":{"effectiveness":eff["model_version"],"price":price["model_version"]},"model_source":"in_process_json_models","parameters":merged}

    def generate(self,count=10,seed=20260724,constraints=None,min_feasibility=0.75,pool_size=2200):
        raw=self.effectiveness.generate(count=count,seed=seed,constraints=constraints,min_feasibility=min_feasibility,pool_size=pool_size)
        for item in raw: item["evaluation"]=self.evaluate(item["params"]); item["params"]=dict(item["evaluation"]["parameters"])
        return raw

    def manifest(self):
        compatibility=(self.contract_report or {}).get("feature_compatibility",{})
        def info(bundle,path):
            if bundle.get("manifest"):
                result=dict(bundle["manifest"])
            else:
                result={"model_version":bundle.get("model_version"),"product_code":bundle.get("product_code"),"model_kind":bundle.get("model_kind")}
            result["artifact_sha256"]=_sha256(path); return result
        return {"contract_version":self.contract_version,"supported_contract_versions":["3.0","4.0"],"contract_valid":bool(self.contract_report and self.contract_report.get("valid")),
            "contract_warnings":list((self.contract_report or {}).get("warnings",[])),"product_code":self.schema["product_code"],
            "schema_keys":[x["key"] for x in self.all_feature_specs()],"feature_compatibility":compatibility,
            "effectiveness":info(self.effectiveness.bundle,self.effectiveness_path),"price":info(self.price.bundle,self.price_path)}
