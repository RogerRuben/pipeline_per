# -*- coding: utf-8 -*-
"""Audit saved predictions and two reviewed functions, without running the notebook."""
import argparse
import ast
import contextlib
import io
import json
import math
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.metrics import r2_score,mean_absolute_percentage_error
from sklearn.model_selection import KFold
from sklearn.preprocessing import MinMaxScaler
from threadpoolctl import threadpool_limits
from tools.product_r43_profiles import profile
from tools.product_price_automl_r4 import load_config


def metrics(y,p):
    return {'mape_percent':float(np.mean(np.abs(y-p)/y)*100.),'r2':float(r2_score(y,p))}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--directory',default=str(ROOT/'outputs/notebook_review_20261015'));args=parser.parse_args();directory=Path(args.directory)
    rows=json.loads((directory/'notebook_outputs.json').read_text(encoding='utf-8'));texts={r['cell']:r['text'] for r in rows}
    import re
    block=texts[40].split('原y_pred：',1)[1].split('去除离群值后的y_pred:',1)[0]
    bank=np.fromstring(block.replace('[',' ').replace(']',' '),sep=' ').reshape(6,6)
    y=np.asarray([float(value) for value in re.findall(r'^\s*\d+\s+([\d.]+)\s*$',texts[42],flags=re.M)])
    assert y.shape==(6,)
    errors=np.mean(np.abs(bank-y[:,None])/y[:,None],axis=0)
    weights=1.+errors.max()-errors;weights/=weights.sum()
    weighted=metrics(y,bank.dot(weights));equal=metrics(y,bank.mean(axis=1))
    assert abs(weighted['mape_percent']-9.586661786043738)<1e-5
    assert abs(weighted['r2']-.9387905129026108)<1e-7
    labels=['lasso','ridge','expert_tree','extra_trees','svr','gbdt']
    fusion={'scope':'recalculation from saved six-by-six prediction text; no retraining or new test data',
            'rows':6,'model_order':labels,'y_true':y.tolist(),'saved_model_predictions':bank.tolist(),
            'weights':weights.tolist(),'notebook_weighted_metrics':weighted,'equal_weight_metrics':equal,
            'mape_improvement_over_equal_pp':equal['mape_percent']-weighted['mape_percent'],
            'per_model':[dict(model=name,**metrics(y,bank[:,i])) for i,name in enumerate(labels)]}
    cfg=profile('data_only');saved={}
    for line in texts[9].splitlines():
        for label in ('gbdt','et'):
            if line.strip().startswith(label+' ='):
                saved[label]=ast.literal_eval(line.split('=',1)[1].strip())
    parameter_coverage={}
    for notebook_name,current_name in (('gbdt','gbdt'),('et','extra_trees')):
        bounds=cfg['search']['parameter_ranges'][current_name]['n_estimators'][:2]
        parameter_coverage[current_name]={'notebook_saved_best':saved[notebook_name],'current_n_estimators_bounds':bounds,
                                        'saved_tree_count_in_current_range':bounds[0]<=saved[notebook_name]['n_estimators']<=bounds[1]}
    try:
        load_config(overrides={'search':{'parameter_ranges':{'extra_trees':{'min_samples_split':[4,10]}}}})
        parameter_coverage['extra_trees']['min_samples_split_configurable']=True
    except ValueError as error:
        parameter_coverage['extra_trees']['min_samples_split_configurable']=False
        parameter_coverage['extra_trees']['validation_error']=str(error)
    parameter_coverage['expert_tree_in_default_models']='expert_tree' in cfg['models']
    parameter_coverage['default_expert_scores_enabled']=cfg['expert']['enabled']
    # Execute only the two already-reviewed, side-effect-limited function bodies.
    # Their fitting side effect is the specific behavior this controlled probe tests.
    tree=ast.parse((directory/'notebook_source.py').read_text(encoding='utf-8'))
    selected=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in ('calculate_confidence_intervals','new_3_indictors')]
    assert len(selected)==2
    namespace={'np':np,'KFold':KFold,'r2_score':r2_score,'mean_absolute_percentage_error':mean_absolute_percentage_error,'df_std':100.}
    exec(compile(ast.Module(body=selected,type_ignores=[]),'<reviewed notebook functions>','exec'),namespace)
    rng=np.random.RandomState(110);X=pd.DataFrame(rng.uniform(1.,10.,(12,3)),columns=['a','b','c']);target=pd.Series(60.+10.*X['a']+3.*X['b'])
    scaler=MinMaxScaler().fit(X.iloc[3:]);X_scaled=scaler.transform(X)
    model=Ridge(alpha=.1).fit(X_scaled[3:],np.log(target.iloc[3:]))
    initial=model.predict(X_scaled[:3]);coef=model.coef_.copy();identity=id(model)
    with threadpool_limits(limits=1),contextlib.redirect_stdout(io.StringIO()):
        namespace['new_3_indictors'](X,target,pd.DataFrame(X_scaled[:3]),target.iloc[:3],np.exp(initial),model,'controlled_probe')
    after=model.predict(X_scaled[:3])
    assert id(model)==identity and not np.allclose(initial,after)
    mutation={'scope':'synthetic 12x3 controlled probe of the actual reviewed evaluation function; not a business-accuracy experiment',
              'same_model_object':id(model)==identity,'parameters_changed':not np.allclose(coef,model.coef_),
              'predictions_before_in_log_space':initial.tolist(),'same_inputs_after_function':after.tolist(),
              'root_cause':'evaluation calls model.fit(raw_features, raw_prices) on the deployment object in every fold'}
    # Small-sample split counts are conditional examples, not a claim about
    # the unavailable business table's actual strata or training row ordering.
    counts={'records':16,'test_size':.2,'test_rows':4,'all_possible_test_sets':math.comb(16,4),
            'two_balanced_price_strata_size_8_each':math.comb(8,2)**2,
            'caching_warning':'same held-out set is insufficient as a cache key if stochastic training depends on training row order; preserve ordered IDs and estimator seed'}
    result={'status':'PASS','saved_output_fusion':fusion,'in_place_model_mutation':mutation,'split_coverage_example':counts,'current_parameter_coverage':parameter_coverage,
            'original_training_reproduced':False,'reason':'referenced workbook is not present beside the provided notebook; supplied RAR also contains only the notebook',
            'production_source_changed':False}
    (directory/'audit_experiments.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':'PASS','weighted':weighted,'equal':equal,'weights':weights.tolist(),'mutation_confirmed':mutation['parameters_changed'],'original_training_reproduced':False},ensure_ascii=False))
if __name__=='__main__':main()
