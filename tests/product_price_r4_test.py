# -*- coding: utf-8 -*-
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
import numpy as np
from sklearn.linear_model import Ridge
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from app.xlsx_utils import write_workbook_bytes
from app.product_table import TableFormatError
from tools.product_r4_input import ratings_from_workbook,apply_field_types
from tools.product_price_automl_r4 import load_config,score,blend,InnerSearch
from services.price_service.r4_estimators import DualRidge,R4PriceRegressor,RelativeRegression,PricePreparation
from services.price_service.ensemble_estimators import StableFoldImputer
from tools.product_effectiveness_bootstrap import load_config as effect_config


class R4Tests(unittest.TestCase):
    def setUp(self):
        self.holder=tempfile.TemporaryDirectory(); self.root=Path(self.holder.name)
        self.X=np.random.RandomState(42).normal(size=(10,4)); self.y=10+self.X[:,0]
        self.specs=[{"parameter_id":"x%d"%i,"label":"属性%d"%i,"value_type":"number"} for i in range(4)]
    def tearDown(self): self.holder.cleanup()
    def book(self,rows,name="价格属性评分"):
        path=self.root/"input.xlsx"; path.write_bytes(write_workbook_bytes([(name,rows)])); return path
    def test_mean_and_median_really_differ(self):
        self.assertAlmostEqual(StableFoldImputer("mean").fit([[0],[1],[10]]).statistics_[0],11/3)
        self.assertEqual(StableFoldImputer("median").fit([[0],[1],[10]]).statistics_[0],1)
    def test_scores_are_not_percentages_and_keep_original_records(self):
        path=self.book([["英文代号","中文名称","价格重要性评分","属性类型","专家编号"],["x0","属性0",5,"number","甲"],["x0","属性0",3,"number","乙"]])
        scores,records,types,groups=ratings_from_workbook(path,{"x0"})
        self.assertEqual(scores,{"x0":4}); self.assertEqual([r["score"] for r in records],[5,3])
    def test_out_of_range_score_reports_cell(self):
        path=self.book([["英文代号","中文名称","价格重要性评分"],["x0","属性0",.5]])
        with self.assertRaises(TableFormatError) as caught: ratings_from_workbook(path,{"x0"})
        self.assertEqual(caught.exception.details["error_cell"],"C2")
    def test_unknown_score_reports_correct_sheet(self):
        path=self.book([["英文代号","中文名称","价格重要性评分"],["unknown","未知",5]])
        with self.assertRaises(TableFormatError): ratings_from_workbook(path,{"x0"})
    def test_blank_optional_score_table_is_neutral(self):
        path=self.book([["英文代号","中文名称","价格重要性评分"]])
        self.assertEqual(ratings_from_workbook(path,{"x0"})[0],{})
    def test_price_score_is_not_used_by_effectiveness(self):
        path=self.book([["英文代号","中文名称","价格重要性评分"],["x0","属性0",5]])
        configuration=effect_config(path,allow_legacy=False)
        self.assertEqual(configuration["attributes"],{}); self.assertFalse(configuration["auto_discover_couplings"])
    def test_explicit_no_coupling_is_accepted(self):
        path=self.book([["源属性","目标属性"],["无耦合",""]],"耦合关系")
        self.assertEqual(effect_config(path,allow_legacy=False)["couplings"],[])
    def test_unknown_configuration_key_is_rejected(self):
        with self.assertRaises(ValueError): load_config(overrides={"seed_search":{"typo":5000}})
    def test_deep_budget_and_final_member_budget_are_separate(self):
        config=load_config(); self.assertEqual(config["seed_search"]["limits"]["deep"],5000)
        self.assertLess(config["seed_search"]["members_per_configuration"],5000)
    def test_preparation_uses_only_training_observations(self):
        prep=PricePreparation(self.specs,[5,1,1,1]).fit(self.X[:8])
        before=copy.deepcopy(prep.defaults); prep.transform(np.full((2,4),1e8))
        self.assertEqual(before,prep.defaults)
    def test_weighted_dual_is_equivalent_to_ridge(self):
        w=np.arange(1,11)/5.5
        np.testing.assert_allclose(DualRidge(.1).fit(self.X,self.y,w).predict(self.X),Ridge(.1).fit(self.X,self.y,sample_weight=w).predict(self.X),atol=1e-10)
    def test_price_below_fifty_is_not_guessed_to_be_log(self):
        model=R4PriceRegressor(self.specs,[5,1,1,1],{"model":"ridge","parameters":{"alpha":.01}},0).fit(self.X,self.y)
        self.assertLess(np.max(model.predict(self.X)),50)
    def test_weights_produce_different_fixed_parameter_models(self):
        config={"model":"ridge","parameters":{"alpha":1.},"sample_weight_strength":.3}
        a=R4PriceRegressor(self.specs,[3]*4,config,1).fit(self.X,self.y)
        b=R4PriceRegressor(self.specs,[3]*4,config,2).fit(self.X,self.y)
        self.assertGreater(np.max(np.abs(a.predict(self.X)-b.predict(self.X))),1e-5)
    def test_relative_loss_fits_positive_price(self):
        model=RelativeRegression(alpha=.001).fit(self.X,self.y)
        self.assertLess(score(self.y,model.predict(self.X))["mape_percent"],1.)
    def test_single_record_r2_is_not_invented(self):
        self.assertIsNone(score([10.],[9.])["r2"])
    def test_fusion_uses_zero_or_nonnegative_weights(self):
        bank=np.column_stack([self.y*.98,self.y*1.02,np.full(10,50.)])
        indices,w,p=blend(self.y,bank,load_config())
        self.assertAlmostEqual(sum(w),1); self.assertTrue(np.all(w>=0)); self.assertLess(score(self.y,p)["mape_percent"],1.)
    def test_enum_override_does_not_treat_codes_as_distances(self):
        data={"parameters":[{"parameter_id":"x0","value_type":"number"}],"agreements":[{"params":{"x0":1.}},{"params":{"x0":3.}}]}
        apply_field_types(data,{"x0":"enum"})
        self.assertEqual(data["parameters"][0]["value_type"],"enum"); self.assertEqual(data["agreements"][0]["params"]["x0"],"1")
        self.assertEqual(data["parameters"][0]["search_type"],"unordered_enum")
    def test_numeric_override_retains_valid_recommendation_search_type(self):
        data={"parameters":[{"parameter_id":"x0","value_type":"enum"}],"agreements":[{"params":{"x0":"1.5"}},{"params":{"x0":"3.0"}}]}
        apply_field_types(data,{"x0":"number"})
        self.assertEqual(data["parameters"][0]["search_type"],"continuous")
    def test_all_r4_models_predict_finite_values(self):
        for name in ("ridge","lasso","mape_lasso","relative_huber","pls","svr","kernel_ridge","gpr","expert_tree","extra_trees","gbdt"):
            model=R4PriceRegressor(self.specs,[5,1,1,1],{"model":name,"parameters":{},"sample_weight_strength":.2},2).fit(self.X,self.y)
            self.assertTrue(np.isfinite(model.predict(self.X)).all(),name)


if __name__=="__main__": unittest.main(verbosity=2)
