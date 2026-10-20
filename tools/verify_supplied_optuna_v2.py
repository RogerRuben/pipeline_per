# -*- coding: utf-8 -*-
"""Compare the adapted engine with the exact supplied scripts on fixed data."""
import ast
import importlib
import json
import sys
import tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from product_optuna_v2_test import fixture
from tools.product_price_optuna_v2 import train_price_optuna_v2
from services.price_service.optuna_v2 import price_automl_core as adapted_core
from threadpoolctl import threadpool_limits
import numpy as np
import pandas as pd


def main():
    output=ROOT/'outputs/optuna_v2_pipeline_20261015';source=output/'supplied_source/price_automl_optuna_v2';sys.path.insert(0,str(source))
    original=importlib.import_module('price_automl_optuna_v2');original_core=importlib.import_module('price_automl_core')
    smoke=importlib.import_module('smoke_test')
    with threadpool_limits(limits=1):smoke.main()
    X,y,specs,data=fixture();frame=pd.DataFrame(X,columns=[s['parameter_id'] for s in specs]);csv=frame.copy();csv.insert(0,'sample_id',np.arange(16));csv[original_core.TARGET_DEFAULT]=y
    with tempfile.TemporaryDirectory(dir=str(output),prefix='source_parity_') as folder:
        directory=Path(folder);path=directory/'same_data.csv';csv.to_csv(path,index=False,encoding='utf-8-sig')
        with threadpool_limits(limits=1):
            old=original.run_training(str(path),str(directory/'original'),n_trials=2,verbose=False)
            native,new=train_price_optuna_v2(data,directory/'adapted',overrides={'search':{'n_trials':2}},progress=lambda s:None)
        assert old['selected_parameters']==new['selected_parameters']
        assert abs(old['validation_metrics']['r2']-new['validation_metrics']['r2'])<1e-9
        assert abs(old['validation_metrics']['mape']-new['validation_metrics']['mape'])<1e-9
        original_bundle=original_core.load_bundle(old['saved_model']);adapted_bundle=adapted_core.load_bundle(str(directory/'adapted/price_ensemble_bundle.pkl'))
        reference,_=original_core.predict_bundle(original_bundle,frame);actual,_=adapted_core.predict_bundle(adapted_bundle,frame)
        delta=float(np.max(np.abs(reference-actual)));assert delta<1e-7
    before=ast.parse((source/'price_automl_optuna_v2.py').read_text(encoding='utf-8'));after=ast.parse((ROOT/'services/price_service/optuna_v2/optimizer.py').read_text(encoding='utf-8'))
    checked=[]
    for name in ('_select_pareto','_original_baseline','_evaluate'):
        a=next(n for n in ast.walk(before) if isinstance(n,ast.FunctionDef) and n.name==name)
        b=next(n for n in ast.walk(after) if isinstance(n,ast.FunctionDef) and n.name==name)
        assert ast.dump(a)==ast.dump(b);checked.append(name)
    a=next(n for n in ast.walk(before) if isinstance(n,ast.FunctionDef) and n.name=='objective')
    b=next(n for n in ast.walk(after) if isinstance(n,ast.FunctionDef) and n.name=='objective')
    assert ast.dump(a)==ast.dump(b);checked.append('objective')
    result={'status':'PASS','supplied_smoke_test_7_trials':True,'same_16x36_data_2_trials_parameters_equal':True,'validation_metrics_equal':True,
            'full_data_model_max_delta':delta,'selection_and_objective_AST_identical':checked,'original_core_byte_identical':(source/'price_automl_core.py').read_bytes()==(ROOT/'services/price_service/optuna_v2/price_automl_core.py').read_bytes(),
            'environment':'existing Python3.8.10 sklearn0.24.2 Optuna3.6.0','business_accuracy_certified':False}
    (output/'supplied_version_parity.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
