# -*- coding: utf-8 -*-
"""Ten virtual products with exactly fifty attributes and preset expert priors."""
import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.product_pipeline import fixture_csv
from tools.product_price_automl import write_json


def tiny_fixture(seed=20261003):
    functional = [("emergency_unlock", "是否应急解锁"), ("self_test", "是否自检"), ("overload_protect", "是否过载保护"),
                  ("position_feedback", "是否位置反馈"), ("manual_drive", "是否手动驱动"), ("brake_hold", "是否制动保持"),
                  ("remote_control", "是否远程控制"), ("fault_alarm", "是否故障报警")]
    performance = [("rated_thrust_n", "额定推力(N)"), ("peak_thrust_n", "峰值推力(N)"), ("speed_mm_s", "运行速度(mm/s)"),
                   ("mass_kg", "质量(kg)"), ("response_time_ms", "响应时间(ms)"), ("stroke_mm", "行程(mm)"),
                   ("position_error_mm", "定位误差(mm)"), ("lifetime_cycles", "寿命(次)"), ("duty_ratio", "工作制(%)"),
                   ("power_w", "功耗(W)"), ("current_a", "额定电流(A)"), ("voltage_v", "额定电压(V)"),
                   ("noise_db", "噪声(dB)"), ("length_mm", "长度(mm)"), ("width_mm", "宽度(mm)"), ("height_mm", "高度(mm)"),
                   ("efficiency_percent", "传动效率(%)"), ("gear_ratio", "减速比"), ("backlash_mm", "回程间隙(mm)"),
                   ("rigidity_n_mm", "刚度(N/mm)"), ("repeatability_mm", "重复精度(mm)"), ("holding_force_n", "保持力(N)"),
                   ("acceleration_mm_s2", "加速度(mm/s²)"), ("deceleration_mm_s2", "减速度(mm/s²)"),
                   ("comm_latency_ms", "通信延迟(ms)"), ("sample_rate_hz", "采样频率(Hz)"),
                   ("rated_torque_nm", "额定转矩(Nm)"), ("peak_torque_nm", "峰值转矩(Nm)")]
    environment = [("temp_low_c", "工作温度下限(℃)"), ("temp_high_c", "工作温度上限(℃)"), ("humidity_percent", "耐湿度(%)"),
                   ("vibration_g", "振动耐受(g)"), ("shock_g", "冲击耐受(g)"), ("salt_spray_h", "盐雾试验时间(h)"),
                   ("protection_grade", "防护等级(IP)"), ("altitude_m", "适用海拔(m)"), ("corrosion_h", "腐蚀耐受(h)"),
                   ("emc_level", "电磁兼容等级"), ("water_pressure_kpa", "耐水压(kPa)"), ("storage_high_c", "存储温度上限(℃)")]
    categorical = [("power_type", "电源类型"), ("material_type", "壳体材料")]
    attributes = functional + performance + environment + categorical
    assert len(attributes) == 50
    # Presets are constructed before any target prices. They are demonstration
    # assumptions, not expert opinions estimated from the ten target labels.
    coefficients = {"rated_thrust_n": .00042, "speed_mm_s": .016, "salt_spray_h": .003,
                    "protection_grade": .03, "emergency_unlock": .65, "self_test": .45, "temp_high_c": .025}
    price_config = {"schema_version": "price-automl-v2-1.0", "experts_from_workbook": True,
                    "models": ["engineering_cost", "engineering_residual", "ridge", "lasso", "elasticnet", "bayesian_ridge", "huber",
                               "svr", "kernel_ridge", "knn", "random_forest", "extra_trees", "gbdt"],
                    "search": {"trials": 20, "ensemble_trials": 4, "promote": 6},
                    "resources": {"total_seconds": 300, "fit_timeout_seconds": 30},
                    "validation": {"folds": 2, "repeats": 2, "outer_folds": 2, "outer_repeats": 2},
                    "pipeline": {"select_k": ["all", 3, 6], "interaction_pairs": [["rated_thrust_n", "speed_mm_s"]]},
                    "seed_bagging": {"milestones": [4, 8, 16]},
                    "ensemble": {"mode": "performance_budget", "max_mape_delta_pp": .1, "max_r2_drop": .005,
                                 "max_members": 6, "family_priors": [
                                     {"family": "engineering", "label": "工程知识模型", "weight": .35, "confidence": .85},
                                     {"family": "linear", "label": "正则化线性模型", "weight": .35, "confidence": .8},
                                     {"family": "tree", "label": "树模型", "weight": .2, "confidence": .7},
                                     {"family": "kernel", "label": "核与近邻模型", "weight": .1, "confidence": .65}]},
                    "engineering": {"intercept_wan": 3.0, "coefficients": coefficients}}
    lower = {"mass_kg", "response_time_ms", "position_error_mm", "power_w", "current_a", "noise_db", "length_mm", "width_mm", "height_mm",
             "backlash_mm", "repeatability_mm", "comm_latency_ms", "temp_low_c"}
    effect = {"attributes": {}, "couplings": [
        {"source": "rated_thrust_n", "target": "peak_thrust_n", "coefficient": 1.45, "offset": 0, "tolerance": 200},
        {"source": "speed_mm_s", "target": "response_time_ms", "coefficient": -2.6, "offset": 320, "tolerance": 12},
        {"source": "rated_thrust_n", "target": "mass_kg", "coefficient": .00038, "offset": 3, "tolerance": .8}]}
    for group, items, total in (("功能", functional, .3), ("性能", performance, .5), ("环境", environment, .2)):
        for key, label in items:
            effect["attributes"][key] = {"preference": "lower" if key in lower else "higher", "weight": total / len(items)}
    for key, label in categorical:
        effect["attributes"][key] = {"preference": "neutral", "weight": 0}
    rng = random.Random(seed)
    ranges = {"stroke_mm": (50, 450), "position_error_mm": (.01, .15), "lifetime_cycles": (50000, 200000),
              "duty_ratio": (25, 100), "power_w": (80, 250), "current_a": (1, 9), "noise_db": (35, 65),
              "length_mm": (180, 450), "width_mm": (50, 120), "height_mm": (40, 100), "efficiency_percent": (75, 98),
              "gear_ratio": (20, 120), "backlash_mm": (.02, .4), "rigidity_n_mm": (1000, 4000), "repeatability_mm": (.005, .08),
              "holding_force_n": (5000, 18000), "acceleration_mm_s2": (50, 400), "deceleration_mm_s2": (100, 400),
              "comm_latency_ms": (5, 30), "sample_rate_hz": (100, 1500), "rated_torque_nm": (15, 100), "peak_torque_nm": (30, 160),
              "humidity_percent": (80, 98), "vibration_g": (2, 15), "shock_g": (10, 50), "altitude_m": (1000, 5000),
              "corrosion_h": (48, 240), "emc_level": (2, 4), "water_pressure_kpa": (30, 100), "storage_high_c": (70, 100)}
    rows = []
    for i in range(10):
        record = dict((key, float((i + index // 2) % (2 + index % 2) > 0)) for index, (key, label) in enumerate(functional))
        thrust, speed = rng.uniform(4500, 15500), rng.uniform(15, 75)
        for index, (key, label) in enumerate(performance + environment):
            lo, hi = ranges.get(key, (10 + index, 100 + index * 5))
            record[key] = round(rng.uniform(lo, hi), 3)
        record.update(rated_thrust_n=round(thrust, 1), peak_thrust_n=round(thrust * 1.45 + rng.uniform(-140, 140), 1),
                      speed_mm_s=round(speed, 2), mass_kg=round(2.2 + thrust * .00038 + speed * .018, 3),
                      response_time_ms=round(320 - speed * 2.6 + rng.uniform(-8, 8), 2), temp_low_c=rng.choice([-40, -30, -20]),
                      temp_high_c=rng.choice([60, 70, 85]), salt_spray_h=rng.choice([24, 48, 96, 168]),
                      protection_grade=rng.choice([54, 55, 65, 66, 67]), power_type="24V" if i % 2 else "48V",
                      material_type="铝合金" if i % 3 else "钢制")
        record["voltage_v"] = 24 if i % 2 else 48
        # A separate, nearby virtual generating equation; no inference claims.
        price = (3.12 + .000415 * record["rated_thrust_n"] + .0168 * record["speed_mm_s"] + .0031 * record["salt_spray_h"] +
                 .029 * record["protection_grade"] + .63 * record["emergency_unlock"] + .47 * record["self_test"] +
                 .024 * record["temp_high_c"] + rng.uniform(-.07, .07))
        values = ["有" if record[key] else "无" for key, _ in functional] + [record[key] for key, _ in performance + environment + categorical]
        if i == 2:
            values[[key for key, _ in attributes].index("comm_latency_ms")] = ""
        if i == 5:
            values[[key for key, _ in attributes].index("material_type")] = ""
        rows.append(["TINY%03d" % (i + 1), "虚拟成品%02d" % (i + 1), round(price, 5)] + values)
    attribute_groups = {}
    for group, items in (("功能", functional), ("性能", performance), ("环境", environment), ("类别", categorical)):
        attribute_groups.update(dict((key, group) for key, _ in items))
    return {"labels": ["成品编号", "成品名称", "价格(万元)"] + [label for key, label in attributes],
            "field_ids": ["product_id", "product_name", "price_wan"] + [key for key, label in attributes], "rows": rows,
            "effectiveness_config": effect, "price_config": price_config,
            "attribute_groups": attribute_groups,
            "source_note": "虚拟数据与演示专家先验，训练前固定；用于10行50属性的流程测试。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    fixture = tiny_fixture()
    write_json(args.output / "fixture.json", fixture)
    write_json(args.output / "price_config.json", fixture["price_config"])
    write_json(args.output / "effectiveness_config.json", fixture["effectiveness_config"])
    (args.output / "virtual_10x50.csv").write_bytes(fixture_csv(fixture))
    print("已生成10条记录、50个属性，双行表头及预设专家配置。")


if __name__ == "__main__":
    main()
