# -*- coding: utf-8 -*-
import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
import numpy as np
from threadpoolctl import threadpool_limits
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from tools.product_price_automl_r4 import load_config,InnerSearch,blend,score,train_price_r4
from tools.product_r42_search import folds
from tools.product_r42_profiles import profile
from services.price_service.r4_estimators import R4PriceRegressor,PricePreparation


class R42Tests(unittest.TestCase):
    def setUp(self):
        self.holder=tempfile.TemporaryDirectory(); self.path=Path(self.holder.name)
        self.X=np.random.RandomState(608).uniform(-1,1,(16,36)); self.X[:,0]=np.linspace(-1,1,16)
        self.y=80+30*self.X[:,0]**2
        self.specs=[{"parameter_id":"x%d"%i,"label":"属性%d"%i,"value_type":"number","unit":"","required":False,"inference":{"observed_count":16}} for i in range(36)]
    def tearDown(self):self.holder.cleanup()
    def test_repeated_cv_covers_every_group_without_leakage(self):
        groups=np.repeat(np.arange(8),2)
        c=load_config(overrides={"validation":{"inner_strategy":"price_balanced","inner_folds":4,"inner_repeats":3}})
        counts=np.zeros(16)
        for tr,te in folds(self.X,self.y,groups,c["validation"],"inner"):
            self.assertFalse(set(groups[tr])&set(groups[te])); counts[te]+=1
        np.testing.assert_array_equal(counts,3)
    def test_repeated_oof_is_mean_of_every_repetition_not_last_one(self):
        c=load_config(overrides={"validation":{"inner_strategy":"group_kfold","inner_folds":4,"inner_repeats":3}})
        s=InnerSearch(self.X,self.y,np.arange(16),self.specs,[3]*36,c,time.monotonic()+30,self.path,lambda x:None)
        candidate={"model":"baseline","parameters":{},"view":"all","target_transform":"identity"}
        total=np.zeros(16); counts=np.zeros(16)
        for tr,te in s.folds:
            m=R4PriceRegressor(self.specs,[3]*36,candidate).fit(self.X[tr],self.y[tr]);total[te]+=m.predict(self.X[te]);counts[te]+=1
        np.testing.assert_allclose(s.oof(candidate,0),total/counts)
    def test_data_only_does_not_depend_on_provided_expert_scores(self):
        c=load_config(overrides={"expert":{"enabled":False}})
        candidate={"model":"ridge","parameters":{"alpha":1.},"view":"expert_core","expert_strength":1.}
        s1=InnerSearch(self.X,self.y,np.arange(16),self.specs,[5]+[1]*35,c,time.monotonic()+30,self.path/"a",lambda x:None)
        s2=InnerSearch(self.X,self.y,np.arange(16),self.specs,[1]+[5]*35,c,time.monotonic()+30,self.path/"b",lambda x:None)
        np.testing.assert_allclose(s1.oof(candidate,0),s2.oof(candidate,0),rtol=0,atol=0)
    def test_nonlinear_screen_finds_a_quadratic_price_signal(self):
        p=PricePreparation(self.specs,[3]*36,strength=0.,view="screened",select_k=1,screening="nonlinear").fit(self.X,self.y)
        self.assertEqual(p.selection_details_["selected_fields"],["x0"])
    def test_polynomial_and_pca_models_round_trip(self):
        for name,parameters in (("poly_ridge",{"alpha":1e-8,"degree":2}),("pca_ridge",{"alpha":1.,"n_components":3})):
            config={"model":name,"parameters":parameters,"view":"screened","select_k":2,"screening":"nonlinear","expert_strength":0.}
            m=R4PriceRegressor(self.specs,[3]*36,config).fit(self.X,self.y)
            import pickle
            old=m.predict(self.X);new=pickle.loads(pickle.dumps(m,protocol=4)).predict(self.X)
            np.testing.assert_allclose(old,new)
            if name=="poly_ridge":self.assertLess(score(self.y,new)["mape_percent"],.001)
    def test_required_fusion_has_multiple_positive_weights(self):
        bank=np.column_stack([self.y*.97,self.y*1.04,np.full(16,100.)])
        c=load_config(overrides={"ensemble":{"min_members":2,"minimum_weight":.05,"maximum_weight":.9}})
        indices,w,p=blend(self.y,bank,c)
        self.assertGreaterEqual(len(indices),2);self.assertTrue(np.all(w>=.05-1e-8));self.assertTrue(np.all(w<=.9+1e-8));self.assertAlmostEqual(w.sum(),1.)
        self.assertLess(score(self.y,p)["mape_percent"],.1)
    def test_unmet_search_can_continue_after_restart_limit(self):
        c=load_config(overrides={"models":["ridge"],"search":{"warm_start":False,"anchor_trials":1,"max_restarts":0},"pipeline":{"feature_views":["all"]},
              "seed_search":{"max_trials":21,"minimum_trials":4,"milestones":[4,8,16],"check_interval":4,"patience":1,"minimum_gain":1000.,"stop_when_unmet_stagnant":False},
              "quality":{"mape_percent_max":1e-9,"r2_min":.9999999}})
        with threadpool_limits(limits=1):r=InnerSearch(self.X,self.y,np.arange(16),self.specs,[3]*36,c,time.monotonic()+30,self.path,lambda x:None).run()
        self.assertEqual(r["seed_report"]["completed"],21);self.assertEqual(r["seed_report"]["stop_reason"],"configured_cap")
    def test_all_profiles_keep_quality_targets(self):
        for name in ("data_only","expert_compare","loo_audit","quick"):
            c=profile(name);self.assertEqual(c["quality"]["mape_percent_max"],10.);self.assertEqual(c["quality"]["r2_min"],.9)
            self.assertEqual(c["expert"]["enabled"],name=="expert_compare")
    def test_exported_report_counts_all_outer_repetitions_and_training_gap(self):
        data={"products":[{"product_code":"R42_TEST","product_name":"微样本测试"}],"parameters":copy.deepcopy(self.specs),
              "agreements":[{"agreement_id":"T%d"%i,"product_code":"R42_TEST","historical_price_wan":float(self.y[i]),"params":{p["parameter_id"]:float(self.X[i,j]) for j,p in enumerate(self.specs)}} for i in range(16)]}
        cfg={"models":["ridge","poly_ridge"],"expert":{"enabled":False},"search":{"anchor_trials":2,"warm_start":False,"wide_kernels":True},
             "pipeline":{"feature_views":["all","screened"],"screening":"nonlinear"},
             "seed_search":{"max_trials":2},"ensemble":{"min_members":2,"jackknife":False},
             "validation":{"outer_strategy":"group_kfold","outer_folds":4,"outer_repeats":2,"inner_strategy":"group_kfold","inner_folds":4,"inner_repeats":2},
             "resources":{"total_seconds":60.,"per_search_seconds":5.}}
        bundle,r=train_price_r4(data,self.path/"integration",overrides=cfg,progress=lambda x:None)
        self.assertTrue(r["audit_complete"]);self.assertIn("training_fit_metrics",r);self.assertIsNotNone(r["baseline_outer_metrics"])
        audit=json.loads((self.path/"integration/nested_audit.json").read_text(encoding="utf-8"))
        self.assertTrue(all(row["validation_count"]==2 for row in audit["records"]));self.assertGreaterEqual(r["configuration_count"],2)
    def test_profile_installer_preserves_business_targets_and_raw_scores(self):
        from tools.upgrade_product_r42 import upgrade
        config=self.path/"config";config.mkdir()
        old=load_config(overrides={"expert":{"scores":{"x0":5}},"quality":{"mape_percent_max":5.,"hard_gate":True}})
        (config/"price_automl_r4.json").write_text(json.dumps(old),encoding="utf-8");(config/"portable.json").write_text('{}',encoding="utf-8")
        new=upgrade(self.path,"data_only")
        self.assertEqual(new["quality"],old["quality"]);self.assertEqual(new["expert"]["scores"],old["expert"]["scores"])
        self.assertFalse(new["expert"]["enabled"]);self.assertFalse(new["seed_search"]["stop_when_unmet_stagnant"])
    def test_quality_rejection_keeps_a_full_diagnostic_report(self):
        data={"products":[{"product_code":"REJECT","product_name":"拒绝对照"}],"parameters":copy.deepcopy(self.specs),
              "agreements":[{"agreement_id":"T%d"%i,"historical_price_wan":float(self.y[i]),"params":{p["parameter_id"]:float(self.X[i,j]) for j,p in enumerate(self.specs)}} for i in range(16)]}
        cfg={"models":["ridge"],"search":{"anchor_trials":1,"warm_start":False},"seed_search":{"max_trials":1},
             "validation":{"outer_strategy":"group_kfold","outer_folds":4,"inner_strategy":"group_kfold","inner_folds":4},
             "quality":{"mape_percent_max":1e-9,"r2_min":.99999999,"hard_gate":True},"resources":{"total_seconds":30.}}
        with self.assertRaises(ValueError):train_price_r4(data,self.path/"reject",overrides=cfg,progress=lambda x:None)
        report=json.loads((self.path/"reject/training_report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["quality_gate_rejected"]);self.assertFalse(report["targets_met"])


if __name__=="__main__":unittest.main(verbosity=2)
