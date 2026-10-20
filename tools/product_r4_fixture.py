# -*- coding: utf-8 -*-
"""Fixed synthetic inputs and price importance ratings, not fitted opinions."""
import argparse
import copy
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from tools.product_v2_fixture import tiny_fixture
from tools.product_price_automl_r4 import DEFAULT
from tools.product_pipeline import fixture_csv


def fixture(attributes=50):
    base=tiny_fixture(); result=copy.deepcopy(base)
    result["labels"]=base["labels"][:attributes+3]; result["field_ids"]=base["field_ids"][:attributes+3]
    result["rows"]=[row[:attributes+3] for row in base["rows"]]
    # A real negative value must not be mistaken for a missing sentinel.
    if "temp_low_c" in result["field_ids"]: result["rows"][0][result["field_ids"].index("temp_low_c")]=-1
    important={"rated_thrust_n":5,"peak_thrust_n":4,"speed_mm_s":5,"salt_spray_h":4,"protection_grade":4,"emergency_unlock":4,"self_test":4,"temp_high_c":4}
    result["price_scores"]={key:important.get(key,1) for key in result["field_ids"][3:]}
    result["field_types"]={key:("boolean" if i<8 else "enum" if key in ("power_type","material_type") else "ip_grade" if key=="protection_grade" else "number") for i,key in enumerate(result["field_ids"][3:])}
    result["effectiveness_config"]["couplings"]=[]; result["effectiveness_config"]["auto_discover_couplings"]=False
    result["effectiveness_config"]["attributes"]={k:v for k,v in base["effectiveness_config"]["attributes"].items() if k in result["field_ids"]}
    result["price_config"]=copy.deepcopy(DEFAULT)
    result["price_config"]["expert"].update(scores=result["price_scores"],field_types=result["field_types"],attribute_groups={k:base["attribute_groups"][k] for k in result["price_scores"]})
    result["source_note"]="虚拟成品与预先设定的价格重要性评分，用于软件和算法对照。"
    return result


def main():
    p=argparse.ArgumentParser(); p.add_argument("--output",required=True); args=p.parse_args(); directory=Path(args.output); directory.mkdir(parents=True,exist_ok=True)
    for count in (50,40):
        f=fixture(count)
        (directory/("fixture_10x%d.json"%count)).write_text(json.dumps(f,ensure_ascii=False,indent=2),encoding="utf-8")
        (directory/("virtual_10x%d.csv"%count)).write_bytes(fixture_csv(f))
    print("生成10×50和10×40样本与原始1—5分价格评分。")
if __name__=="__main__": main()
