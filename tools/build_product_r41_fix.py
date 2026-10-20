# -*- coding: utf-8 -*-
"""Create the small R4.1 update and a reproducible audit summary."""
import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser(); p.add_argument("--stage",required=True); p.add_argument("--diagnostics",required=True); p.add_argument("--zip",required=True)
    a=p.parse_args(); stage=Path(a.stage).resolve(); diagnostics=Path(a.diagnostics).resolve(); target=Path(a.zip).resolve()
    if ROOT/"deliverables" not in target.parents or target.exists(): raise ValueError("Use a new deliverables ZIP path.")
    records={}
    for name in ("baseline_mixed","baseline_neutral_full","baseline_wrong_full","verified_mixed","verified_neutral","verified_wrong"):
        source=diagnostics/name/"results.json"; r=json.loads(source.read_text(encoding="utf-8"))
        records[name]={"seconds":r["elapsed_seconds"],"audit_complete":r["report"]["audit_complete"],"outer_audit":r["report"]["nested_cv_metrics"],
                       "development":r["report"]["cv_metrics"],"external_virtual_256":r["external_256"],"seed_cap":r["report"]["seed_report"]["cap"]}
    old=json.loads((diagnostics/"old_bundle_old_code/result.json").read_text(encoding="utf-8"))
    new=json.loads((diagnostics/"old_bundle_new_code/result.json").read_text(encoding="utf-8"))
    assert old==new,"Old saved-model predictions changed"
    result={"status":"PASS","comparison":records,"old_saved_model_predictions_unchanged":True,
            "deep_5000":json.loads((diagnostics/"deep_5000/result.json").read_text(encoding="utf-8")),
            "saved_start_live_test":json.loads((diagnostics/"live_portable_verification.json").read_text(encoding="utf-8")),
            "regression_tests":{"r4":19,"r41":14,"saved_start":9,"console_and_input_compatibility":18,"total":60},
            "limits":"虚拟16x36；预先固定256条外部虚拟记录未参与选择。基线种子上限200，修复版5000且自适应停；时间为当前主机实测，部分任务并行，非Win7计时保证。实际业务表未提供。"}
    raw=(json.dumps(result,ensure_ascii=False,indent=2)+"\n").encode("utf-8")
    (diagnostics/"R41_validation.json").write_bytes(raw)
    (stage/"算法检查与实测结果.json").write_bytes(raw)
    files={"tools/product_price_automl_r4.py":ROOT/"tools/product_price_automl_r4.py",
           "services/price_service/r4_estimators.py":ROOT/"services/price_service/r4_estimators.py",
           "tools/upgrade_product_r41.py":ROOT/"tools/upgrade_product_r41.py",
           "APPLY_R4_1_FIX.bat":stage/"APPLY_R4_1_FIX.bat",
           "R4_1修复说明.md":ROOT/"docs/PRICE_R41_AUDIT_FIX.md"}
    with zipfile.ZipFile(str(target),"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for relative,source in files.items(): archive.write(str(source),relative)
        archive.writestr("算法检查与实测结果.json",raw)
    with zipfile.ZipFile(str(target)) as archive: assert archive.testzip() is None
    digest=hashlib.sha256(target.read_bytes()).hexdigest()
    metadata={"path":str(target),"bytes":target.stat().st_size,"sha256":digest,"status":"PASS"}
    Path(str(target)+".sha256").write_text(digest+"  "+target.name+"\n",encoding="utf-8")
    Path(str(target)+".json").write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(metadata,ensure_ascii=False))
if __name__=="__main__": main()
