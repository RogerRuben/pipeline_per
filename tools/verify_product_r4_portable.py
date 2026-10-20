# -*- coding: utf-8 -*-
"""Actual saved startup, prediction, recommendation and version invariants."""
import argparse
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def exercise(package,log_path,generation=True,expected_rows=10,expected_features=50,change_field='rated_thrust_n',change_delta=250):
    package=Path(package).resolve(); latest=package/"state/latest.json"
    state=latest.read_bytes(); output=package/json.loads(state)["output"]
    manifest=json.loads((output/"prototype_manifest.json").read_text(encoding="utf-8"))
    files=[output/manifest[key] for key in ("price_model","effectiveness_model","business_package")]
    before=[(sha(path),path.stat().st_mtime_ns) for path in files]
    runs_before=sorted(path.name for path in (package/"outputs").glob("run_*"))
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def get(url):
        with opener.open(url,timeout=15) as response: return json.loads(response.read().decode("utf-8"))
    def post(url,payload):
        request=urllib.request.Request(url,data=json.dumps(payload).encode("utf-8"),headers={"Content-Type":"application/json"})
        with opener.open(request,timeout=30) as response: return json.loads(response.read().decode("utf-8"))
    env=dict(os.environ,PRODUCT_NO_PAUSE="1",PYTHONIOENCODING="utf-8",PYTHONPATH="")
    started=time.time(); runtime=None
    with Path(log_path).open("w",encoding="utf-8") as handle:
        process=subprocess.Popen(["cmd.exe","/d","/c","START.bat","--no-browser","--price-port","0","--effect-port","0","--app-port","0"],
                                 cwd=str(package),env=env,stdout=handle,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            deadline=time.monotonic()+60
            while time.monotonic()<deadline:
                if process.poll() is not None: raise AssertionError(Path(log_path).read_text(encoding="utf-8"))
                try:
                    info=json.loads((output/"running.json").read_text(encoding="utf-8"))
                    if (output/"running.json").stat().st_mtime>=started:
                        health=get(info["price_url"]+"/health")
                        if health.get("status")=="ok": runtime=info; break
                except Exception: pass
                time.sleep(.2)
            if runtime is None: raise AssertionError("保存版本启动超时。")
            base=runtime["url"].rstrip("/"); bootstrap=get(base+"/api/bootstrap")
            assert bootstrap["counts"]["historical"]==expected_rows
            assert len(bootstrap["parameters"])==expected_features
            assert bootstrap["integration"]["product_ready"]
            schema=get(base+"/api/price-workbench/schema")
            assert all(any("\u3400"<=c<="\u9fff" for c in field["field_label"]) for field in schema["fields"])
            query={"session_id":"r4-acceptance","source_mode":"historical","selected_tags":[],"indicator_filters":[],"sort_by":"comprehensive",
                   "count":2,"generation_count":2,"page":1,"page_size":10,"start_generation":False}
            ranked=post(base+"/api/recommend",query); assert ranked["total"]==expected_rows
            parameters=ranked["items"][0]["params"]
            response=post(base+"/api/price-workbench/predict",{"parameters":parameters})
            prediction=response["prediction"]["predicted_price_wan"]; explanation=response["explanation"]
            assert math.isfinite(prediction) and prediction>0
            assert len(explanation["price_attribute_scores"])==expected_features
            assert all(1<=r["score"]<=5 for r in explanation["price_attribute_scores"])
            assert abs(sum(x["weighted_contribution_wan"] for x in explanation["model_contributions"])-prediction)<1e-5
            assert explanation["configuration_details"]
            changed=dict(parameters); changed[change_field]=float(changed[change_field])+change_delta
            evaluation=post(base+"/api/evaluate",{"parameters":changed})
            assert math.isfinite(evaluation["capability_score"])
            assert abs(evaluation["predicted_price_wan"]-prediction)>1e-5
            if generation:
                task=post(base+"/api/generation/request",query); deadline=time.monotonic()+90
                while task["status"] not in ("completed","failed") and time.monotonic()<deadline:
                    time.sleep(.2); task=get(base+"/api/generation-tasks/"+task["task_id"])
                assert task["status"]=="completed",task
                generated=post(base+"/api/recommend",dict(query,source_mode="generated",generation_batch_id=task["batch_id"]))
                assert generated["total"]>0
            result={"status":"PASS","rows":expected_rows,"features":expected_features,"price_effect_recommendation":True,"price_scores_1_to_5":True,
                    "chinese_labels":True,"weighted_price_matches":True,"generation":generation,"saved_prediction":prediction,
                    "startup_seconds":round(time.time()-started,2)}
        finally:
            if process.poll() is None:
                process.send_signal(signal.CTRL_BREAK_EVENT)
                try: process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    subprocess.run(["taskkill.exe","/PID",str(process.pid),"/T","/F"],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
                    process.wait(timeout=10)
            if runtime:
                for key in ("price_url","effectiveness_url"):
                    try: get(runtime[key]+"/health")
                    except Exception: continue
                    raise AssertionError("Owned service did not stop: "+key)
    assert state==latest.read_bytes()
    assert before==[(sha(path),path.stat().st_mtime_ns) for path in files]
    assert runs_before==sorted(path.name for path in (package/"outputs").glob("run_*"))
    result.update(no_retraining=True,model_hashes_and_mtime_unchanged=True,version_pointer_unchanged=True)
    return result


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--package",required=True); parser.add_argument("--report",required=True)
    parser.add_argument("--no-generation",action="store_true"); args=parser.parse_args()
    report=Path(args.report).resolve(); report.parent.mkdir(parents=True,exist_ok=True)
    value=exercise(Path(args.package),report.with_suffix(".log"),not args.no_generation)
    report.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(value,ensure_ascii=False))
if __name__=="__main__": main()
