# -*- coding: utf-8 -*-
from __future__ import print_function
import json
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

BASES = {"price": "http://127.0.0.1:18101", "effectiveness": "http://127.0.0.1:18102"}
opener = build_opener(ProxyHandler({}))


def request_json(url, payload=None):
    raw = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=raw, headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with opener.open(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            message = detail.get("message") or detail.get("error") or str(exc)
        except Exception:
            message = str(exc)
        raise RuntimeError(message)


def example(schema):
    values = {}
    for field in schema.get("fields") or []:
        key = field.get("field_name") or field.get("key")
        if not key or field.get("source", "product_parameter") != "product_parameter":
            continue
        allowed = list(field.get("allowed_values") or field.get("categories") or [])
        lower = field.get("generation_min")
        upper = field.get("generation_max")
        if lower is None:
            lower = field.get("training_min")
        if upper is None:
            upper = field.get("training_max")
        dtype = str(field.get("dtype") or field.get("type") or "").lower()
        if dtype in ("enum", "categorical", "category") and allowed:
            value = allowed[0]
        elif field.get("default_value") is not None:
            value = field.get("default_value")
        elif field.get("training_mean") is not None:
            value = field.get("training_mean")
        elif allowed:
            value = allowed[0]
        elif lower is not None and upper is not None:
            value = (float(lower) + float(upper)) / 2.0
        elif lower is not None:
            value = lower
        elif dtype in ("boolean", "bool"):
            value = 0
        else:
            value = 0
        values[key] = value
    return values


ok = True
schemas = {}
for kind, label in (("price", "价格"), ("effectiveness", "效能")):
    try:
        base = BASES[kind]
        data = request_json(base + "/health")
        schemas[kind] = request_json(base + "/api/v1/schema")
        print("[OK] %s服务: %s / backend=%s / model=%s" % (label, data.get("status"), data.get("backend"), data.get("model_version")))
    except Exception as exc:
        ok = False
        print("[ERROR] %s服务不可用: %s" % (label, exc))

if ok:
    codes = [str(schemas[kind].get("product_code") or "") for kind in ("price", "effectiveness")]
    if not codes[0] or codes[0] != codes[1]:
        print("[WARN] 双服务product_code声明不同: %s / %s；继续以实算JSON判断可用性" % tuple(codes))
    else:
        print("[OK] 双服务product_code一致: %s" % codes[0])

if ok:
    try:
        price = request_json(BASES["price"] + "/api/v1/predict", {
            "request_id": "DEPLOYMENT-CHECK-PRICE", "product_code": codes[0] or None,
            "parameters": example(schemas["price"]),
        })
        predicted = float((price.get("prediction") or {}).get("predicted_price_wan"))
        print("[OK] 价格API实算返回数值: %s" % predicted)
    except Exception as exc:
        ok = False
        print("[ERROR] 价格API实算失败: %s" % exc)
    try:
        effect = request_json(BASES["effectiveness"] + "/api/v1/evaluate", {
            "request_id": "DEPLOYMENT-CHECK-EFFECT", "product_code": codes[1] or codes[0] or None,
            "parameters": example(schemas["effectiveness"]),
        })
        evaluated = effect.get("evaluation") or {}
        score = float(evaluated.get("effectiveness_score", evaluated.get("capability_score")))
        print("[OK] 效能API实算返回分数: %s" % score)
    except Exception as exc:
        ok = False
        print("[ERROR] 效能API实算失败: %s" % exc)
sys.exit(0 if ok else 1)
