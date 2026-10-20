# -*- coding: utf-8 -*-
"""Prepare the final encoded workbook and matching V10/V11 expert states."""
from __future__ import print_function

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.xlsx_utils import read_workbook_bytes
from tools.basic_aircraft_door_lock_models import FIELDS, PRICE_KEYS, read_source


def build_payload(source):
    records = read_source(source)
    headers = ["scheme_id", "historical_price_wan"] + PRICE_KEYS
    rows = []
    for record in records:
        values = [record.get("scheme_id"), record.get("price_wan")]
        for key in PRICE_KEYS:
            value = record.get(key)
            spec = next(item for item in FIELDS if item["key"] == key)
            if value is not None and spec.get("mapping"):
                value = spec["mapping"][str(value)]
            values.append(value)
        rows.append(values)
    dictionary = [["field_id", "meaning", "dtype", "unit", "encoding", "price_model", "effectiveness_model"]]
    for item in FIELDS:
        encoding = json.dumps(item.get("mapping") or {}, ensure_ascii=False, sort_keys=True)
        dictionary.append([
            item["key"], item["label"], item["dtype"], item.get("unit") or "", encoding,
            "yes" if item["key"] in PRICE_KEYS else "no",
            "yes" if item["key"] in ("attr_001", "attr_005", "attr_007") else "no",
        ])
    notes = [
        ["item", "value"],
        ["product_code", "AIRCRAFT_DOOR_LOCK_BASIC_DEMO"],
        ["purpose", "final encoded prediction fixture; virtual data only"],
        ["headers", "English field IDs directly match HTTP model parameters"],
        ["missing", "blank cells remain blank; price service applies its packaged missing policy"],
        ["boolean", "attr_001: 0=no, 1=yes"],
        ["category", "attr_002/003/004 are already encoded numeric model values"],
        ["warning", "not for engineering, airworthiness, quotation, or procurement decisions"],
    ]
    return {"sheets": {"encoded_history": [headers] + rows, "field_dictionary": dictionary, "readme": notes}}


def build_states(source_state, output, workbook):
    base = json.loads(Path(source_state).read_text(encoding="utf-8"))
    state10 = json.loads(json.dumps(base, ensure_ascii=False))
    state10["profile_version"] = 10
    state10["data_mode"] = "final_encoded_prediction_virtual_expert_v10"
    state10.setdefault("virtual_fixture", {})["product_code"] = "AIRCRAFT_DOOR_LOCK_BASIC_DEMO"
    state10["virtual_fixture"]["disclaimer"] = "虚拟专家状态，仅用于最终接口兼容性测试"
    state10["compatibility_note"] = "V10 fixed packaged protocol"
    v10_path = output / "expert_state_v10.json"
    v10_path.write_text(json.dumps(state10, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Generate V11 through the same migration/retraining path used by the
    # current effectiveness service, instead of relabeling a V10 JSON file.
    source_root = ROOT / "services" / "effectiveness_service" / "model" / "current" / "source"
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    from interactive_project_app import ProjectApp
    state_dir = output / "v11_runtime_state"
    app = ProjectApp(Path(workbook).resolve(), state_dir=state_dir)
    app.state_path.parent.mkdir(parents=True, exist_ok=True)
    app.state_path.write_text(json.dumps(state10, ensure_ascii=False, indent=2), encoding="utf-8")
    state11 = app.load_state()
    state11["data_mode"] = "final_encoded_prediction_virtual_expert_v11"
    state11.setdefault("virtual_fixture", {})["product_code"] = "AIRCRAFT_DOOR_LOCK_BASIC_DEMO"
    state11["virtual_fixture"]["disclaimer"] = "虚拟专家状态，仅用于最终接口兼容性测试"
    state11["compatibility_note"] = "V11 state produced by official V10-to-V11 migration and retraining path"
    (output / "expert_state_v11.json").write_text(
        json.dumps(state11, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # The migration runtime directory is only an intermediate workspace.  The
    # portable acceptance fixture needs the exported JSON, not mutable state.
    if state_dir.exists():
        shutil.rmtree(str(state_dir))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--source-state", required=True)
    parser.add_argument("--effectiveness-workbook", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    output = Path(args.output_dir).resolve(); output.mkdir(parents=True, exist_ok=True)
    payload = build_payload(args.source)
    payload_path = output / "encoded_prediction_workbook_payload.json"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    build_states(args.source_state, output, args.effectiveness_workbook)
    shutil.copy2(str(args.source), str(output / "source_business_history_reference.xlsx"))
    print(str(payload_path))


if __name__ == "__main__":
    main()
