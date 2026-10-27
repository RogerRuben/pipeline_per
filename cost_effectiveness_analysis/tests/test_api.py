# -*- coding: utf-8 -*-
from cost_effectiveness_analysis.app import CostEffectivenessApplication


class Repo(object):
    def __init__(self):
        self.items = dict(("S%d" % i, {"scheme_id":"S%d" % i,"scheme_name":"方案%d" % i,
            "source":"historical","parameters":{"x":i}}) for i in range(10))
    def get_scheme(self, key): return dict(self.items[key]) if key in self.items else None
    def get_scheme_parameters(self, key, model_values=False): return dict(self.items[key]["parameters"])
    def list_schemes(self, source=None, search=None): return list(self.items.values())
    def get_sources(self): return {"historical":10}


class Client(object):
    def health(self): return {"price":{"status":"ok"},"effectiveness":{"status":"ok"}}
    def evaluate_batch(self, schemes, target_protocol=None):
        rows=[]
        for i,item in enumerate(schemes):
            rows.append({"scheme_id":item["scheme_id"],"predicted_price_wan":None if i==0 else 10+i,
                         "capability_score":80+i,"price_error":"价格失败" if i==0 else None,
                         "effectiveness_error":None})
        return {"items":rows,"models":{"price_model_version":"p1","effectiveness_model_version":"e1"},
                "target_protocol":target_protocol,"analysis_time":"2026-01-01 00:00:00","service_errors":{}}


def test_partial_failure_does_not_fail_analysis(tmp_path):
    config={"host":"127.0.0.1","port":17000,"database":{"path":"missing"},
            "services":{"price":{"url":"p"},"effectiveness":{"url":"e"}},"timeout_seconds":1}
    app=CostEffectivenessApplication(tmp_path,Repo(),Client(),config)
    result=app.analyze({"scheme_ids":["S%d" % i for i in range(10)]})
    assert result["success"] and len(result["schemes"]) == 10
    assert result["summary"]["valid_scheme_count"] == 9
    assert result["schemes"][0]["pareto"] is None
    assert result["schemes"][0]["price_error"] == "价格失败"


def test_selection_limits(tmp_path):
    config={"host":"127.0.0.1","port":17000,"database":{"path":"missing"},
            "services":{"price":{"url":"p"},"effectiveness":{"url":"e"}},"timeout_seconds":1}
    app=CostEffectivenessApplication(tmp_path,Repo(),Client(),config)
    try:
        app.analyze({"scheme_ids":["S0"]})
        assert False
    except ValueError as exc:
        assert "至少两个" in str(exc)
