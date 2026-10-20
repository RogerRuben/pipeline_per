# -*- coding: utf-8 -*-
import json
import hashlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
import numpy as np
from threadpoolctl import threadpool_limits
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from services.price_service.r4_estimators import R4PriceRegressor,PricePreparation,DualRidge
from tools.product_price_automl_r4 import InnerSearch,load_config,score,blend,input_diagnostics
from tools.upgrade_product_r41 import upgrade


class R41Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)
        self.X=np.random.RandomState(613).normal(size=(16,36))
        self.y=100.+10*self.X[:,0]
        self.specs=[{"parameter_id":"x%d"%j,"label":"属性%d"%j,"value_type":"number"} for j in range(36)]
    def tearDown(self): self.tmp.cleanup()
    def test_kernel_preserves_constant_price_far_from_training_points(self):
        for target in ("identity","log"):
            m=R4PriceRegressor(self.specs,[3]*36,{"model":"kernel_ridge","target_transform":target,"parameters":{"alpha":10.,"gamma":1.}}).fit(self.X,np.full(16,800.))
            np.testing.assert_allclose(m.predict(np.full((2,36),100.)),[800.,800.],rtol=1e-10)
    def test_screening_can_recover_a_three_point_attribute(self):
        p=PricePreparation(self.specs,[3]+[5]*35,strength=0.,view="screened",select_k=1).fit(self.X,self.y)
        self.assertEqual(p.selection_details_["selected_fields"],["x0"])
    def test_validation_values_cannot_change_screening_or_imputation(self):
        p=PricePreparation(self.specs,[3]*36,strength=0.,view="screened",select_k=3).fit(self.X[:12],self.y[:12])
        before=(dict(p.defaults),list(p.kept),dict(p.selection_details_))
        p.transform(np.full((4,36),1e8))
        self.assertEqual(before,(p.defaults,list(p.kept),p.selection_details_))
    def test_relative_ridge_is_invariant_to_price_units(self):
        c={"model":"relative_ridge","parameters":{"alpha":2.,"relative_power":2.}}
        a=R4PriceRegressor(self.specs,[3]*36,c).fit(self.X,self.y)
        b=R4PriceRegressor(self.specs,[3]*36,c).fit(self.X,1000*self.y)
        np.testing.assert_allclose(a.predict(self.X),b.predict(self.X)/1000,rtol=1e-10)
    def test_auto_inner_folds_are_disjoint_and_use_five_folds_at_fifteen_groups(self):
        s=InnerSearch(self.X[:15],self.y[:15],np.arange(15),self.specs,[3]*36,load_config(),time.monotonic()+30,self.path,lambda text:None)
        self.assertEqual(len(s.folds),5)
        for train,test in s.folds:
            self.assertFalse(set(train)&set(test)); self.assertEqual(len(train),12)
    def test_loo_can_be_explicitly_restored(self):
        c=load_config(overrides={"validation":{"inner_strategy":"loo"}})
        s=InnerSearch(self.X[:15],self.y[:15],np.arange(15),self.specs,[3]*36,c,time.monotonic()+30,self.path,lambda text:None)
        self.assertEqual(len(s.folds),15)
    def test_unmet_stagnation_restarts_parameters_and_reports_failure(self):
        c=load_config(overrides={"models":["ridge"],"search":{"warm_start":False,"anchor_trials":2,"restart_trials":2},
             "pipeline":{"feature_views":["all"]},"quality":{"mape_percent_max":1e-8,"r2_min":.999999999},
             "seed_search":{"max_trials":5000,"minimum_trials":4,"milestones":[4,8,16],"patience":1,"minimum_gain":1000.}})
        with threadpool_limits(limits=1):
            r=InnerSearch(self.X[:15],self.y[:15],np.arange(15),self.specs,[3]*36,c,time.monotonic()+30,self.path,lambda text:None).run()
        self.assertEqual(r["diagnostics"]["parameter_restarts"],1)
        self.assertEqual(r["seed_report"]["stop_reason"],"targets_unmet_after_parameter_restart")
        self.assertLess(r["seed_report"]["attempted"],5000)
    def test_disabling_early_stop_really_runs_requested_seed_count(self):
        c=load_config(overrides={"models":["ridge"],"search":{"warm_start":False,"anchor_trials":1},"pipeline":{"feature_views":["all"]},
                               "seed_search":{"max_trials":17,"early_stop":False}})
        with threadpool_limits(limits=1):
            r=InnerSearch(self.X[:15],self.y[:15],np.arange(15),self.specs,[3]*36,c,time.monotonic()+30,self.path,lambda text:None).run()
        self.assertEqual(r["seed_report"]["completed"],17)
    def test_old_r4_config_remains_accepted(self):
        old={"schema_version":"price-automl-r4-1.0","search":{"ridge_alphas":[.01,.1,1.,10.]},"expert":{"scores":{"x0":5}},"quality":{"mape_percent_max":7.}}
        c=load_config(overrides=old)
        self.assertEqual(c["validation"]["inner_strategy"],"auto")
        self.assertEqual(c["expert"]["scores"],old["expert"]["scores"])
    def test_config_upgrade_preserves_experts_targets_and_saved_files(self):
        config=self.path/"config"; config.mkdir()
        old=load_config(overrides={"expert":{"scores":{"x0":4.5}},"quality":{"mape_percent_max":7.}})
        old["search"]["ridge_alphas"]=[.01,.1,1.,10.]; old["pipeline"]["feature_views"]=["all","expert_core"]
        old["models"]=[m for m in old["models"] if m!="relative_ridge"]
        (config/"price_automl_r4.json").write_text(json.dumps(old),encoding="utf-8")
        (config/"portable.json").write_text('{"startup_policy":"saved_only"}',encoding="utf-8")
        saved=self.path/"latest.json"; saved.write_bytes(b"saved model pointer")
        backup=upgrade(self.path); new=load_config(config/"price_automl_r4.json")
        self.assertEqual(new["expert"],old["expert"]); self.assertEqual(new["quality"],old["quality"])
        self.assertEqual(saved.read_bytes(),b"saved model pointer"); self.assertTrue(backup.is_file())
        self.assertIn(1000.,new["search"]["ridge_alphas"]); self.assertIn("relative_ridge",new["models"])
    def test_prepared_prediction_equals_raw_prediction(self):
        for name in ("ridge","relative_ridge","kernel_ridge","baseline"):
            m=R4PriceRegressor(self.specs,[3]*36,{"model":name,"parameters":{}}).fit(self.X,self.y)
            np.testing.assert_allclose(m.predict(self.X),m.predict_prepared(m.preparation_.transform(self.X)))
    def test_joint_targets_survive_a_mape_heavy_scalar_objective(self):
        y=np.linspace(80.,120.,16); bad=y.copy(); bad[-1]+=30.; good=y+2.
        c=load_config(overrides={"ensemble":{"alpha":.99,"max_members":1}})
        indices,weights,p=blend(y,np.column_stack([bad,good]),c)
        self.assertEqual(indices,[1]); self.assertGreater(score(y,p)["r2"],.9)
    def test_identical_inputs_price_conflicts_have_irreducible_error(self):
        r=input_diagnostics(np.zeros((2,1)),np.asarray([10.,30.]),[self.specs[0]],[3],np.asarray([0,0]))
        self.assertEqual(len(r["conflicting_groups"]),1)
        self.assertAlmostEqual(r["identical_inputs_mape_lower_bound_percent"],100/3)
        self.assertEqual(r["identical_inputs_r2_upper_bound"],0.)
    def test_upgrade_refreshes_only_patch_checksums(self):
        cfg=self.path/"config"; cfg.mkdir()
        (cfg/"price_automl_r4.json").write_text('{}',encoding="utf-8")
        (cfg/"portable.json").write_text('{}',encoding="utf-8")
        source=self.path/"tools/product_price_automl_r4.py"; source.parent.mkdir(); source.write_bytes(b"new price source")
        original={"immutable_files":[{"path":"runtime/python38/python.exe","sha256":"keep-runtime-check","size":123},
                                      {"path":"tools/product_price_automl_r4.py","sha256":"old-price-check","size":1}]}
        manifest=self.path/"PACKAGE_MANIFEST.json"; manifest.write_text(json.dumps(original),encoding="utf-8")
        upgrade(self.path); entries={r["path"]:r for r in json.loads(manifest.read_text(encoding="utf-8"))["immutable_files"]}
        self.assertEqual(entries["runtime/python38/python.exe"],original["immutable_files"][0])
        self.assertEqual(entries["tools/product_price_automl_r4.py"]["sha256"],hashlib.sha256(source.read_bytes()).hexdigest())


if __name__=="__main__": unittest.main(verbosity=2)
