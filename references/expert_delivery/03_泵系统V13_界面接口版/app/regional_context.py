# -*- coding: utf-8 -*-
"""Task-conditioned operating-region planning for the pump V13 workflow."""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, replace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from system_excel import (
    SystemProjectDataset,
    SystemRequirementSpec,
    SystemTask,
)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def task_from_dict(raw: Dict[str, Any]) -> SystemTask:
    data = dict(raw)
    data["requirements"] = tuple(
        SystemRequirementSpec(**dict(item)) for item in data.get("requirements") or []
    )
    return SystemTask(**data)


def requirement_anchor(requirement: SystemRequirementSpec) -> Optional[float]:
    if requirement.requirement_type == "target":
        return requirement.target_value
    if requirement.requirement_type == "at_least":
        return requirement.minimum
    if requirement.requirement_type == "at_most":
        return requirement.maximum
    if requirement.requirement_type == "within_range":
        if requirement.minimum is not None and requirement.maximum is not None:
            return 0.5 * (float(requirement.minimum) + float(requirement.maximum))
    return None


@dataclass(frozen=True)
class RegionalCalibrationContext:
    region_id: str
    label: str
    sequence_index: int
    primary_system_key: str
    primary_system_label: str
    primary_unit: str
    lower: float
    center: float
    upper: float
    selected_task_region: bool
    calibration_task: SystemTask
    weight_scaling_exponent: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "region_id": self.region_id,
            "label": self.label,
            "sequence_index": self.sequence_index,
            "primary_system_key": self.primary_system_key,
            "primary_system_label": self.primary_system_label,
            "primary_unit": self.primary_unit,
            "lower": self.lower,
            "center": self.center,
            "upper": self.upper,
            "selected_task_region": self.selected_task_region,
            "calibration_task": asdict(self.calibration_task),
            "weight_scaling_exponent": self.weight_scaling_exponent,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "RegionalCalibrationContext":
        data = dict(raw)
        data["calibration_task"] = task_from_dict(dict(data["calibration_task"]))
        return cls(**data)


class PumpRegionPlanner:
    """Build representative task contexts without product-name hardcoding.

    The primary scale is the widest positive direct system attribute whose
    requirement is a lower bound or relative-higher preference. For the pump
    workbook this naturally resolves to flow, while keeping the mechanism
    reusable for another similarly structured product family.
    """

    MAX_REGIONS = 5

    def __init__(self, project: SystemProjectDataset):
        self.project = project
        self.product_by_key = project.product.attribute_by_key()
        self.mappings_by_system: Dict[str, List[Any]] = {}
        for mapping in project.direct_mappings():
            self.mappings_by_system.setdefault(mapping.system_key, []).append(mapping)
        self.history_by_system: Dict[str, List[float]] = {
            key: [self.mapped_values(item.params)[key] for item in project.product.schemes]
            for key in self.mappings_by_system
        }
        self.range_by_system = {
            key: self._mapped_generation_range(key) for key in self.mappings_by_system
        }

    def mapped_values(self, params: Dict[str, Any]) -> Dict[str, float]:
        return {
            system_key: sum(
                float(params[item.product_key]) * float(item.coefficient) + float(item.constant)
                for item in mappings
            )
            for system_key, mappings in self.mappings_by_system.items()
        }

    def _mapped_generation_range(self, system_key: str) -> Tuple[float, float]:
        low = 0.0
        high = 0.0
        for mapping in self.mappings_by_system.get(system_key, []):
            spec = self.product_by_key[mapping.product_key]
            if not spec.is_numeric or spec.generation_min is None or spec.generation_max is None:
                continue
            values = (
                float(spec.generation_min) * float(mapping.coefficient) + float(mapping.constant),
                float(spec.generation_max) * float(mapping.coefficient) + float(mapping.constant),
            )
            low += min(values)
            high += max(values)
        history = self.history_by_system.get(system_key) or []
        if history:
            low = min(low, min(history))
            high = max(high, max(history))
        return float(low), float(high)

    @staticmethod
    def _positive_log_span(values: Iterable[float]) -> float:
        positive = [float(value) for value in values if float(value) > 0.0]
        if len(positive) < 2:
            return 0.0
        return math.log(max(positive) / max(min(positive), 1e-12))

    def primary_requirement(self, task: SystemTask) -> Optional[SystemRequirementSpec]:
        candidates: List[Tuple[float, SystemRequirementSpec]] = []
        for requirement in task.requirements:
            if requirement.system_key not in self.mappings_by_system:
                continue
            if requirement.requirement_type not in {"at_least", "relative_higher"}:
                continue
            low, high = self.range_by_system.get(requirement.system_key, (0.0, 0.0))
            span = self._positive_log_span([low, high] + self.history_by_system.get(requirement.system_key, []))
            candidates.append((span, requirement))
        return max(candidates, key=lambda item: (item[0], item[1].system_key))[1] if candidates else None

    @staticmethod
    def _region_count(low: float, high: float) -> int:
        if low <= 0.0 or high <= low:
            return 1
        ratio = high / low
        if ratio >= 20.0:
            return 4
        if ratio >= 5.0:
            return 3
        if ratio >= 2.0:
            return 2
        return 1

    @staticmethod
    def _merge_centers(values: Sequence[float], log_mode: bool) -> List[float]:
        ordered = sorted(float(value) for value in values)
        merged: List[float] = []
        for value in ordered:
            if not merged:
                merged.append(value)
                continue
            distance = (
                abs(math.log(max(value, 1e-12) / max(merged[-1], 1e-12)))
                if log_mode
                else abs(value - merged[-1]) / max(abs(value), abs(merged[-1]), 1.0)
            )
            if distance < 0.14:
                merged[-1] = math.sqrt(value * merged[-1]) if log_mode else 0.5 * (value + merged[-1])
            else:
                merged.append(value)
        return merged

    def _representative_centers(self, low: float, high: float, selected_anchor: float) -> List[float]:
        count = self._region_count(low, high)
        log_mode = low > 0.0 and high > low
        if count == 1:
            return [clamp(selected_anchor, low, high)]
        if log_mode:
            log_low = math.log(low)
            log_high = math.log(high)
            centers = [
                math.exp(log_low + index * (log_high - log_low) / (count - 1))
                for index in range(count)
            ]
        else:
            centers = [low + index * (high - low) / (count - 1) for index in range(count)]
        centers.append(clamp(selected_anchor, low, high))
        centers = self._merge_centers(centers, log_mode)
        if len(centers) > self.MAX_REGIONS:
            centers = sorted(centers, key=lambda value: abs(value - selected_anchor))[: self.MAX_REGIONS]
            centers.sort()
        return centers

    @staticmethod
    def _boundaries(centers: Sequence[float], low: float, high: float) -> List[Tuple[float, float]]:
        log_mode = low > 0.0 and all(value > 0.0 for value in centers)
        edges = [float(low)]
        for left, right in zip(centers, centers[1:]):
            edges.append(math.sqrt(left * right) if log_mode else 0.5 * (left + right))
        edges.append(float(high))
        return [(edges[index], edges[index + 1]) for index in range(len(centers))]

    @staticmethod
    def _region_label(index: int, count: int, attribute_label: str) -> str:
        if count == 1:
            level = "当前"
        elif count == 2:
            level = ("低", "高")[index]
        elif count == 3:
            level = ("低", "中", "高")[index]
        elif count == 4:
            level = ("低", "中低", "中高", "高")[index]
        else:
            level = f"第{index + 1}"
        return f"{level}{attribute_label}规格"

    def _weight_exponent(self, primary_key: str, task: SystemTask) -> float:
        budget_keys = {
            item.system_key for item in task.requirements if item.requirement_type == "at_most"
        }
        if not budget_keys:
            return 0.0
        weight_key = next(iter(sorted(budget_keys)))
        primary = self.history_by_system.get(primary_key) or []
        weights = self.history_by_system.get(weight_key) or []
        slopes: List[float] = []
        for left in range(min(len(primary), len(weights))):
            for right in range(left + 1, min(len(primary), len(weights))):
                q1, q2 = float(primary[left]), float(primary[right])
                m1, m2 = float(weights[left]), float(weights[right])
                if min(q1, q2, m1, m2) <= 0.0:
                    continue
                denominator = math.log(q2 / q1)
                if abs(denominator) < 0.20:
                    continue
                slope = math.log(m2 / m1) / denominator
                if math.isfinite(slope):
                    slopes.append(slope)
        return round(clamp(statistics.median(slopes) if slopes else 0.55, 0.0, 1.5), 6)

    @staticmethod
    def _scaled_optional(value: Optional[float], ratio: float) -> Optional[float]:
        return None if value is None else float(value) * float(ratio)

    def _calibration_task(
        self,
        selected_task: SystemTask,
        primary: SystemRequirementSpec,
        center: float,
        index: int,
        label: str,
        weight_exponent: float,
    ) -> SystemTask:
        primary_anchor = requirement_anchor(primary) or center
        primary_ratio = center / primary_anchor if abs(primary_anchor) > 1e-12 else 1.0
        requirements: List[SystemRequirementSpec] = []
        for requirement in selected_task.requirements:
            if requirement.system_key == primary.system_key:
                updates: Dict[str, Any] = {
                    "lower_80_distance": self._scaled_optional(requirement.lower_80_distance, abs(primary_ratio)),
                    "upper_80_distance": self._scaled_optional(requirement.upper_80_distance, abs(primary_ratio)),
                    "description": (requirement.description + f"；V13自动区域校准中心{center:g}").strip("；"),
                }
                if requirement.requirement_type == "at_least":
                    updates["minimum"] = center
                elif requirement.requirement_type == "target":
                    updates["target_value"] = center
                requirements.append(replace(requirement, **updates))
                continue
            if requirement.requirement_type == "at_most" and requirement.maximum is not None:
                scale = max(primary_ratio, 1e-9) ** weight_exponent
                requirements.append(
                    replace(
                        requirement,
                        maximum=float(requirement.maximum) * scale,
                        lower_80_distance=self._scaled_optional(requirement.lower_80_distance, scale),
                        upper_80_distance=self._scaled_optional(requirement.upper_80_distance, scale),
                        description=(requirement.description + f"；按主规格指数{weight_exponent:.3f}自动缩放").strip("；"),
                    )
                )
                continue
            requirements.append(requirement)
        return SystemTask(
            task_id=f"CAL-{selected_task.task_id}-{index + 1:02d}",
            task_name=f"全域校准·{label}",
            operating_condition=f"由{selected_task.task_name}自动派生的代表规格工况",
            requirements=tuple(requirements),
            description="仅用于一次训练中的全域曲线校准，不覆盖Excel真实系统任务。",
        )

    def discover(self, selected_task: SystemTask) -> List[RegionalCalibrationContext]:
        primary = self.primary_requirement(selected_task)
        if primary is None:
            return []
        selected_anchor = requirement_anchor(primary)
        low, high = self.range_by_system[primary.system_key]
        positive_history = [value for value in self.history_by_system.get(primary.system_key, []) if value > 0.0]
        if positive_history:
            low = min(low, min(positive_history))
            high = max(high, max(positive_history))
        if selected_anchor is None:
            selected_anchor = statistics.median(positive_history) if positive_history else 0.5 * (low + high)
        if low <= 0.0 and high > 0.0:
            low = min(positive_history) if positive_history else max(high * 0.01, 1e-6)
        if high <= low:
            high = low + max(abs(low) * 0.10, 1.0)
        centers = self._representative_centers(float(low), float(high), float(selected_anchor))
        boundaries = self._boundaries(centers, float(low), float(high))
        attribute = self.project.system_attribute_by_key()[primary.system_key]
        selected_index = min(range(len(centers)), key=lambda idx: abs(math.log(max(centers[idx], 1e-12) / max(float(selected_anchor), 1e-12))))
        weight_exponent = self._weight_exponent(primary.system_key, selected_task)
        raw: List[RegionalCalibrationContext] = []
        for index, (center, bounds) in enumerate(zip(centers, boundaries)):
            label = self._region_label(index, len(centers), attribute.label)
            raw.append(
                RegionalCalibrationContext(
                    region_id=f"REG-{selected_task.task_id}-{index + 1:02d}",
                    label=label,
                    sequence_index=index,
                    primary_system_key=primary.system_key,
                    primary_system_label=attribute.label,
                    primary_unit=attribute.unit,
                    lower=round(float(bounds[0]), 8),
                    center=round(float(center), 8),
                    upper=round(float(bounds[1]), 8),
                    selected_task_region=index == selected_index,
                    calibration_task=self._calibration_task(
                        selected_task,
                        primary,
                        float(center),
                        index,
                        label,
                        weight_exponent,
                    ),
                    weight_scaling_exponent=weight_exponent,
                )
            )
        selected = raw[selected_index]
        remaining = sorted(
            (item for item in raw if item.region_id != selected.region_id),
            key=lambda item: abs(math.log(max(item.center, 1e-12) / max(selected.center, 1e-12))),
        )
        return [
            replace(selected, sequence_index=0),
            *[replace(item, sequence_index=index + 1) for index, item in enumerate(remaining)],
        ]

    @staticmethod
    def context_distance(left: SystemTask, right: SystemTask) -> float:
        left_by_key = left.requirement_by_key()
        right_by_key = right.requirement_by_key()
        keys = sorted(set(left_by_key) | set(right_by_key))
        if not keys:
            return 0.0
        distances: List[float] = []
        for key in keys:
            a = left_by_key.get(key)
            b = right_by_key.get(key)
            if a is None or b is None:
                distances.append(2.5)
                continue
            if a.requirement_type != b.requirement_type:
                distances.append(1.5)
                continue
            x = requirement_anchor(a)
            y = requirement_anchor(b)
            if x is None or y is None:
                distances.append(0.0)
            elif x > 0.0 and y > 0.0:
                distances.append(abs(math.log(x / y)))
            else:
                scale = max(abs(x), abs(y), 1.0)
                distances.append(abs(x - y) / scale)
        return math.sqrt(sum(value * value for value in distances) / len(distances))
