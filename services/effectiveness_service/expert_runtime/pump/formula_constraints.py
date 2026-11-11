# -*- coding: utf-8 -*-
"""Versioned and safely evaluated physical formula constraints."""

from __future__ import annotations

import ast
import math
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple


SUPPORTED_FORMULA_TYPES = {"expression", "product_divide_constant"}
SUPPORTED_MODES = {"exact", "scale_calibrated", "affine_calibrated", "efficiency_calibrated", "diagnostic"}
SUPPORTED_RELATIONS = {"equal", "less_equal", "greater_equal"}
SUPPORTED_ENFORCEMENT = {"hard", "soft", "diagnostic"}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(float(item) for item in values)
    if not ordered:
        raise ValueError("无法对空序列计算分位数。")
    if len(ordered) == 1:
        return ordered[0]
    position = clamp(float(probability), 0.0, 1.0) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def _clip(value: float, low: float, high: float) -> float:
    return clamp(float(value), float(low), float(high))


SAFE_FUNCTIONS: Dict[str, Callable[..., float]] = {
    "abs": lambda value: abs(float(value)),
    "sqrt": lambda value: math.sqrt(float(value)),
    "log": lambda value: math.log(float(value)),
    "log10": lambda value: math.log10(float(value)),
    "exp": lambda value: math.exp(float(value)),
    "min": lambda *values: min(float(item) for item in values),
    "max": lambda *values: max(float(item) for item in values),
    "pow": lambda base, exponent: math.pow(float(base), float(exponent)),
    "clip": _clip,
}
SAFE_CONSTANTS = {"pi": math.pi, "e": math.e}


class SafeFormulaExpression:
    """Parse a small engineering expression language without executing code."""

    MAX_NODES = 120
    MAX_POWER = 12.0

    def __init__(self, expression: str):
        self.expression = str(expression or "").strip()
        if not self.expression:
            raise ValueError("公式表达式不能为空。")
        try:
            self.tree = ast.parse(self.expression, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"公式表达式语法错误：{exc.msg}") from exc
        if len(list(ast.walk(self.tree))) > self.MAX_NODES:
            raise ValueError("公式表达式过于复杂，请拆成多个中间派生属性。")
        self.variable_names: Set[str] = set()
        self._validate(self.tree)

    def _validate(self, node: ast.AST) -> None:
        if isinstance(node, ast.Expression):
            self._validate(node.body)
            return
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("公式中只允许数值常数。")
            if not math.isfinite(float(node.value)):
                raise ValueError("公式中不允许无穷大或非数值常数。")
            return
        if hasattr(ast, "Num") and isinstance(node, ast.Num):
            return
        if isinstance(node, ast.Name):
            if node.id not in SAFE_CONSTANTS:
                self.variable_names.add(node.id)
            return
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            self._validate(node.operand)
            return
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
            self._validate(node.left)
            self._validate(node.right)
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in SAFE_FUNCTIONS:
                raise ValueError(f"公式函数“{node.func.id}”不在安全白名单中。")
            if node.keywords:
                raise ValueError("公式函数不支持命名参数。")
            if not node.args:
                raise ValueError(f"公式函数“{node.func.id}”缺少参数。")
            for argument in node.args:
                self._validate(argument)
            return
        raise ValueError(f"公式中包含不允许的语法：{type(node).__name__}")

    def evaluate(self, values: Dict[str, Any]) -> float:
        result = float(self._evaluate_node(self.tree.body, values))
        if not math.isfinite(result):
            raise ValueError("公式计算结果不是有限数值。")
        return result

    def _evaluate_node(self, node: ast.AST, values: Dict[str, Any]) -> float:
        if isinstance(node, ast.Constant):
            return float(node.value)
        if hasattr(ast, "Num") and isinstance(node, ast.Num):
            return float(node.n)
        if isinstance(node, ast.Name):
            if node.id in SAFE_CONSTANTS:
                return SAFE_CONSTANTS[node.id]
            if node.id not in values:
                raise ValueError(f"公式缺少属性“{node.id}”的数值。")
            value = float(values[node.id])
            if not math.isfinite(value):
                raise ValueError(f"属性“{node.id}”不是有限数值。")
            return value
        if isinstance(node, ast.UnaryOp):
            value = self._evaluate_node(node.operand, values)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left = self._evaluate_node(node.left, values)
            right = self._evaluate_node(node.right, values)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                if abs(right) <= 1e-15:
                    raise ValueError("公式计算发生除零。")
                return left / right
            if abs(right) > self.MAX_POWER:
                raise ValueError(f"公式幂指数绝对值不能超过{self.MAX_POWER:g}。")
            return math.pow(left, right)
        if isinstance(node, ast.Call):
            name = node.func.id
            arguments = [self._evaluate_node(item, values) for item in node.args]
            if name == "exp" and abs(arguments[0]) > 100:
                raise ValueError("exp函数参数绝对值不能超过100。")
            return float(SAFE_FUNCTIONS[name](*arguments))
        raise ValueError("公式计算遇到不允许的语法。")


@dataclass(frozen=True)
class FormulaConstraintSpec:
    formula_id: str
    target_key: str
    target_label: str
    target_unit: str
    formula_type: str = "expression"
    source_keys: Tuple[str, ...] = ()
    source_labels: Tuple[str, ...] = ()
    constant: float = 1.0
    mode: str = "exact"
    relative_tolerance: float = 0.05
    absolute_tolerance: float = 0.0
    enforcement: str = "hard"
    auto_complete: bool = True
    description: str = ""
    expression: str = ""
    relation: str = "equal"
    operating_condition: str = ""

    def normalized_expression(self) -> str:
        if self.expression.strip():
            return self.expression.strip()
        if self.formula_type == "product_divide_constant":
            return f"({' * '.join(self.source_keys)}) / {self.constant:g}"
        return ""

    def expression_text(self) -> str:
        relation = {"equal": "=", "less_equal": "<=", "greater_equal": ">="}.get(self.relation, "=")
        return f"{self.target_label} {relation} {self.normalized_expression()}"


@dataclass(frozen=True)
class FormulaCalibration:
    formula_id: str
    sample_count: int
    valid_sample_count: int
    median_factor: float
    lower_factor: float
    upper_factor: float
    median_efficiency: Optional[float]
    lower_efficiency: Optional[float]
    upper_efficiency: Optional[float]
    median_relative_residual: float
    confidence: str
    warnings: Tuple[str, ...] = ()
    affine_slope: float = 1.0
    affine_intercept: float = 0.0


class FormulaConstraintEngine:
    """Evaluate safe expressions, calibrated relationships and generation dependencies."""

    def __init__(
        self,
        product: Any,
        specs: Iterable[FormulaConstraintSpec],
        calibration_summaries: Optional[Iterable[Dict[str, Any]]] = None,
    ):
        self.product = product
        self.specs = list(specs)
        self.by_target = {item.target_key: item for item in self.specs}
        self.attribute_by_key = product.attribute_by_key()
        self.compiled: Dict[str, SafeFormulaExpression] = {}
        for spec in self.specs:
            self._validate_spec(spec)
        self.ordered_specs = self._dependency_order()
        supplied = {
            str(item.get("formula_id")): self._calibration_from_dict(item)
            for item in (calibration_summaries or [])
        }
        self.calibrations: Dict[str, FormulaCalibration] = {}
        for spec in self.specs:
            self.calibrations[spec.formula_id] = supplied.get(spec.formula_id) or self._calibrate(spec)

    @staticmethod
    def _calibration_from_dict(raw: Dict[str, Any]) -> FormulaCalibration:
        allowed = {
            "formula_id", "sample_count", "valid_sample_count", "median_factor",
            "lower_factor", "upper_factor", "median_efficiency", "lower_efficiency",
            "upper_efficiency", "median_relative_residual", "confidence", "warnings",
            "affine_slope", "affine_intercept",
        }
        data = {key: value for key, value in raw.items() if key in allowed}
        data["warnings"] = tuple(data.get("warnings") or ())
        data.setdefault("affine_slope", 1.0)
        data.setdefault("affine_intercept", 0.0)
        return FormulaCalibration(**data)

    def _validate_spec(self, spec: FormulaConstraintSpec) -> None:
        if spec.formula_type not in SUPPORTED_FORMULA_TYPES:
            raise ValueError(f"不支持的公式类型：{spec.formula_type}")
        if spec.mode not in SUPPORTED_MODES:
            raise ValueError(f"不支持的公式处理模式：{spec.mode}")
        if spec.relation not in SUPPORTED_RELATIONS:
            raise ValueError(f"不支持的公式关系：{spec.relation}")
        if spec.enforcement not in SUPPORTED_ENFORCEMENT:
            raise ValueError(f"不支持的公式约束级别：{spec.enforcement}")
        if spec.target_key not in self.attribute_by_key:
            raise ValueError(f"公式目标属性不存在：{spec.target_key}")
        expression = SafeFormulaExpression(spec.normalized_expression())
        unknown = sorted(expression.variable_names - set(self.attribute_by_key))
        if unknown:
            raise ValueError(f"公式“{spec.formula_id}”包含未知属性：{'、'.join(unknown)}")
        if spec.target_key in expression.variable_names:
            raise ValueError(f"公式“{spec.formula_id}”不能直接引用目标属性自身。")
        if spec.relative_tolerance < 0 or spec.absolute_tolerance < 0:
            raise ValueError(f"公式“{spec.formula_id}”的容差不能为负数。")
        if spec.auto_complete and spec.relation != "equal":
            raise ValueError(f"公式“{spec.formula_id}”是非等式关系，不能设置生成时自动回算。")
        self.compiled[spec.formula_id] = expression

    def _dependency_order(self) -> List[FormulaConstraintSpec]:
        by_target = {item.target_key: item for item in self.specs}
        visiting: Set[str] = set()
        visited: Set[str] = set()
        output: List[FormulaConstraintSpec] = []

        def visit(spec: FormulaConstraintSpec) -> None:
            if spec.target_key in visited:
                return
            if spec.target_key in visiting:
                raise ValueError(f"派生属性公式存在循环依赖，涉及“{spec.target_label}”。")
            visiting.add(spec.target_key)
            for source in self.compiled[spec.formula_id].variable_names:
                dependency = by_target.get(source)
                if dependency is not None:
                    visit(dependency)
            visiting.remove(spec.target_key)
            visited.add(spec.target_key)
            output.append(spec)

        for item in self.specs:
            visit(item)
        return output

    def base_value(self, spec: FormulaConstraintSpec, params: Dict[str, Any]) -> float:
        return self.compiled[spec.formula_id].evaluate(params)

    def _calibrate(self, spec: FormulaConstraintSpec) -> FormulaCalibration:
        pairs: List[Tuple[float, float]] = []
        factors: List[float] = []
        warnings: List[str] = []
        for scheme in self.product.schemes:
            try:
                base = self.base_value(spec, scheme.params)
                actual = float(scheme.params[spec.target_key])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(base) or not math.isfinite(actual):
                continue
            pairs.append((base, actual))
            if abs(base) > 1e-12:
                factors.append(actual / base)
        if not pairs:
            warnings.append("历史样本无法形成有效公式校准，暂按原公式处理。")
            return FormulaCalibration(
                spec.formula_id, len(self.product.schemes), 0,
                1.0, 1.0, 1.0, None, None, None, 0.0, "insufficient", tuple(warnings),
            )

        slope = 1.0
        intercept = 0.0
        if spec.mode == "affine_calibrated" and len(pairs) >= 2:
            mean_x = statistics.mean(item[0] for item in pairs)
            mean_y = statistics.mean(item[1] for item in pairs)
            denominator = sum((item[0] - mean_x) ** 2 for item in pairs)
            if denominator > 1e-12:
                slope = sum((x - mean_x) * (y - mean_y) for x, y in pairs) / denominator
                intercept = mean_y - slope * mean_x
            else:
                warnings.append("公式基值缺少变化，无法稳定拟合仿射系数，暂按斜率1处理。")

        median_factor = statistics.median(factors) if factors else 1.0
        lower_factor = quantile(factors, 0.10) if factors else median_factor
        upper_factor = quantile(factors, 0.90) if factors else median_factor
        predicted = []
        for base, _actual in pairs:
            if spec.mode in {"scale_calibrated", "efficiency_calibrated"}:
                predicted.append(base * median_factor)
            elif spec.mode == "affine_calibrated":
                predicted.append(slope * base + intercept)
            else:
                predicted.append(base)
        residuals = [
            abs(actual - estimate) / max(abs(estimate), 1e-9)
            for (_base, actual), estimate in zip(pairs, predicted)
        ]
        if len(pairs) < 5:
            confidence = "low"
            warnings.append("有效样本少于5个，校准参数仅作初步参考。")
        elif len(pairs) < 12:
            confidence = "medium"
        else:
            confidence = "high"
        efficiencies = [1.0 / item for item in factors if item > 0] if spec.mode == "efficiency_calibrated" else []
        if efficiencies and sum(item > 1.0 + spec.relative_tolerance for item in efficiencies) > len(efficiencies) / 3:
            warnings.append("超过三分之一的样本反算效率大于1，请检查属性定义、单位或工况。")
        return FormulaCalibration(
            formula_id=spec.formula_id,
            sample_count=len(self.product.schemes),
            valid_sample_count=len(pairs),
            median_factor=round(float(median_factor), 10),
            lower_factor=round(float(lower_factor), 10),
            upper_factor=round(float(upper_factor), 10),
            median_efficiency=statistics.median(efficiencies) if efficiencies else None,
            lower_efficiency=quantile(efficiencies, 0.10) if efficiencies else None,
            upper_efficiency=quantile(efficiencies, 0.90) if efficiencies else None,
            median_relative_residual=round(float(statistics.median(residuals)), 10),
            confidence=confidence,
            warnings=tuple(warnings),
            affine_slope=round(float(slope), 10),
            affine_intercept=round(float(intercept), 10),
        )

    def predicted_value(self, spec: FormulaConstraintSpec, params: Dict[str, Any]) -> float:
        base = self.base_value(spec, params)
        calibration = self.calibrations[spec.formula_id]
        if spec.mode in {"scale_calibrated", "efficiency_calibrated"}:
            return base * calibration.median_factor
        if spec.mode == "affine_calibrated":
            return calibration.affine_slope * base + calibration.affine_intercept
        return base

    def complete_generated(self, params: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        output = dict(params)
        completions: List[Dict[str, Any]] = []
        for spec in self.ordered_specs:
            if not spec.auto_complete or spec.mode == "diagnostic" or spec.relation != "equal":
                continue
            value = self.predicted_value(spec, output)
            target = self.attribute_by_key[spec.target_key]
            rounded = int(round(value)) if target.data_type == "integer" else round(value, target.precision)
            previous = output.get(spec.target_key)
            output[spec.target_key] = rounded
            completions.append(
                {
                    "formula_id": spec.formula_id,
                    "target_key": spec.target_key,
                    "target_label": spec.target_label,
                    "previous": previous,
                    "calculated": rounded,
                    "unit": spec.target_unit,
                    "mode": spec.mode,
                    "expression": spec.expression_text(),
                }
            )
        return output, completions

    def assess_one(self, spec: FormulaConstraintSpec, params: Dict[str, Any]) -> Dict[str, Any]:
        calibration = self.calibrations[spec.formula_id]
        base = self.base_value(spec, params)
        actual = float(params[spec.target_key])
        expected = self.predicted_value(spec, params)
        tolerance = max(spec.absolute_tolerance, spec.relative_tolerance * max(abs(expected), 1e-9))
        residual = actual - expected
        relative_error = abs(residual) / max(abs(expected), 1e-9)
        lower = expected - tolerance
        upper = expected + tolerance
        if spec.mode in {"scale_calibrated", "efficiency_calibrated"} and calibration.valid_sample_count:
            lower = min(lower, base * calibration.lower_factor)
            upper = max(upper, base * calibration.upper_factor)

        below_theoretical = (
            spec.mode == "efficiency_calibrated"
            and spec.relation == "equal"
            and actual + tolerance < base
        )
        if spec.relation == "less_equal":
            consistent = actual <= upper
        elif spec.relation == "greater_equal":
            consistent = actual >= lower
        else:
            consistent = lower <= actual <= upper
        if below_theoretical:
            consistent = False
            status = "below_theoretical_minimum"
            severity = clamp((base - actual) / max(abs(base), 1e-9), 0.25, 1.0)
            message = (
                f"{spec.target_label}为 {actual:.4g} {spec.target_unit}，低于未计损耗的公式基值 "
                f"{base:.4g} {spec.target_unit}；请核对属性定义、单位或工况。"
            )
        elif consistent:
            status = "consistent"
            message = f"{spec.target_label}满足公式关系，计算基准为 {expected:.4g} {spec.target_unit}。"
            severity = 0.0
        else:
            status = "diagnostic_difference" if spec.mode == "diagnostic" else "formula_mismatch"
            severity = clamp(relative_error / max(spec.relative_tolerance, 0.01), 0.10, 1.0)
            message = (
                f"{spec.target_label}为 {actual:.4g} {spec.target_unit}，公式基准为 "
                f"{expected:.4g} {spec.target_unit}，当前不满足配置的公式关系。"
            )
        implied_factor = actual / base if abs(base) > 1e-12 else None
        implied_efficiency = (
            base / actual
            if spec.mode == "efficiency_calibrated" and abs(actual) > 1e-12
            else None
        )
        hard_violation = not consistent and spec.enforcement == "hard" and spec.mode != "diagnostic"
        return {
            "formula_id": spec.formula_id,
            "target_key": spec.target_key,
            "target_label": spec.target_label,
            "target_unit": spec.target_unit,
            "formula_type": spec.formula_type,
            "mode": spec.mode,
            "relation": spec.relation,
            "enforcement": spec.enforcement,
            "expression": spec.expression_text(),
            "expression_source": spec.normalized_expression(),
            "source_keys": sorted(self.compiled[spec.formula_id].variable_names),
            "base_formula_value": round(base, 8),
            "predicted_value": round(expected, 8),
            "actual_value": round(actual, 8),
            "lower": round(lower, 8),
            "upper": round(upper, 8),
            "residual": round(residual, 8),
            "relative_error": round(relative_error, 6),
            "implied_factor": None if implied_factor is None else round(implied_factor, 8),
            "implied_efficiency": None if implied_efficiency is None else round(implied_efficiency, 6),
            "status": status,
            "severity": round(severity, 3),
            "hard_violation": hard_violation,
            "message": message,
            "calibration_confidence": calibration.confidence,
            "calibration_sample_count": calibration.valid_sample_count,
        }

    def assess(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        working = dict(params)
        output: List[Dict[str, Any]] = []
        for spec in self.ordered_specs:
            result = self.assess_one(spec, working)
            output.append(result)
            if spec.target_key not in working and spec.relation == "equal":
                working[spec.target_key] = result["predicted_value"]
        return output

    def summaries(self) -> List[Dict[str, Any]]:
        output: List[Dict[str, Any]] = []
        for spec in self.ordered_specs:
            row = asdict(self.calibrations[spec.formula_id])
            row["warnings"] = list(row.get("warnings") or [])
            row["spec"] = asdict(spec)
            row["spec"]["source_keys"] = list(spec.source_keys)
            row["spec"]["source_labels"] = list(spec.source_labels)
            row["normalized_expression"] = spec.normalized_expression()
            row["expression"] = spec.expression_text()
            output.append(row)
        return output


def merge_formula_assessment(base: Dict[str, Any], assessments: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Attach formula diagnostics and conservatively update feasibility fields."""
    base["formula_assessments"] = [dict(item) for item in assessments]
    if not assessments:
        return base
    base.setdefault("assessment_basis", []).append(f"{len(assessments)} 条已知物理公式或折算关系")
    hard = [item for item in assessments if item.get("hard_violation")]
    soft = [
        item for item in assessments
        if item.get("status") not in {"consistent"} and not item.get("hard_violation")
    ]
    for item in hard:
        base.setdefault("hard_violations", []).append(
            {
                "feature": item.get("target_key"),
                "title": f"{item.get('target_label')}不满足物理公式",
                "value": item.get("actual_value"),
                "severity": max(0.75, float(item.get("severity") or 0.0)),
                "message": item.get("message"),
                "source": "formula_constraint",
            }
        )
    max_severity = max([float(item.get("severity") or 0.0) for item in assessments] or [0.0])
    if hard:
        base["status"] = "infeasible_by_formula"
        base["physics_status"] = "infeasible_by_formula"
        base["learned_feasibility_probability"] = min(
            float(base.get("learned_feasibility_probability") or 0.0), 0.02
        )
        base["physics_risk"] = max(float(base.get("physics_risk") or 0.0), 0.95)
        base["risk"] = max(float(base.get("risk") or 0.0), 0.95)
    elif soft:
        probability = float(base.get("learned_feasibility_probability") or 0.0)
        adjusted = probability * (1.0 - 0.45 * max_severity)
        base["learned_feasibility_probability"] = round(clamp(adjusted, 0.03, 0.99), 3)
        base["physics_risk"] = round(max(float(base.get("physics_risk") or 0.0), 0.30 + 0.45 * max_severity), 3)
        base["risk"] = round(max(float(base.get("risk") or 0.0), 1.0 - adjusted), 3)
        base.setdefault("feasibility_risk_contributors", []).insert(
            0,
            {
                "code": "formula_consistency",
                "label": soft[0].get("message"),
                "contribution": round(0.45 * max_severity, 3),
            },
        )
    return base


__all__ = [
    "FormulaConstraintSpec",
    "FormulaCalibration",
    "FormulaConstraintEngine",
    "SafeFormulaExpression",
    "SUPPORTED_FORMULA_TYPES",
    "SUPPORTED_MODES",
    "SUPPORTED_RELATIONS",
    "SUPPORTED_ENFORCEMENT",
    "merge_formula_assessment",
]
