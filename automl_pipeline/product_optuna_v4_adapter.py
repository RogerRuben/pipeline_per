# -*- coding: utf-8 -*-
"""V4 product-table -> searched development model -> saved native service bundle."""
import copy
import csv
import hashlib
import json
import pickle
import time
from pathlib import Path
from app.console_compat import enable_plain_console
enable_plain_console()
import numpy as np
from threadpoolctl import threadpool_limits
from .product_price_optuna_v4 import load_config
from .product_price_optuna_v2 import workbook_config,dataset,training_source_rows,expert_rating
from tools.product_price_automl import write_json,native_field_metadata
from services.price_service.optuna_v2.typed_features import apply_types,restore_configurations
from services.price_service.optuna_v4.splits import holdout_indices,make_splits
from services.price_service.optuna_v4.backend import Backend
from services.price_service.optuna_v4.optimizer import run_search,choose_ensemble,compact,fmt
from services.price_service.optuna_v4.metrics import metrics,violation
from services.price_service.optuna_v4.native_member import NativeConfigurationMember,predict_saved
from services.price_service.export_native_price_bundle import export_from_notebook
from services.price_service.native_bundle import save_bundle,load_bundle,predict,predict_batch


def groups_for(frame,ids,data,cfg):
    """Union identical inputs and optional specimen/batch groups; never use price."""
    parent=list(range(len(frame)))
    def root(i):
        while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
        return i
    def union(keys):
        first={}
        for i,key in enumerate(keys):
            if key in first:parent[root(i)]=root(first[key])
            else:first[key]=i
    p=cfg['v4']['splits']
    if p['group_identical_inputs']:union([tuple(row) for row in frame.values])
    if p['group_field']:
        field=p['group_field'];lookup={r['agreement_id']:r['params'] for r in data['agreements']}
        keys=[lookup[key].get(field) for key in ids]
        if any(key is None or str(key).strip()=='' for key in keys):raise ValueError('分组属性不存在或有缺失：'+field)
        union([str(key) for key in keys])
    return np.asarray([root(i) for i in range(len(frame))],int)


def write_csv(path,rows):
    if not rows:return
    with Path(path).open('w',encoding='utf-8-sig',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)


def train(data,output_dir,config_path=None,progress=None,overrides=None,input_path=None):
    started=time.monotonic();progress=progress or (lambda message:print(message,flush=True))
    cfg=load_config(config_path,overrides);known={p['parameter_id'] for p in data['parameters']}
    cfg=workbook_config(input_path,cfg,known,loader=load_config);data=copy.deepcopy(data)
    if input_path:
        from app.product_table import product_table_rows
        source=Path(input_path);restore_configurations(data,product_table_rows(source.name,source.read_bytes(),require_dual_header=True),cfg['expert']['field_types'],cfg['input']['missing_tokens'])
    apply_types(data,cfg['expert']['field_types'])
    if cfg['input']['remove_outliers']:raise ValueError('V4不自动删除异常成品；请在处理好的成品表中确认记录后再训练。')
    frame,y,specs,mappings,_,ids=dataset(data,cfg);rows=training_source_rows(data,ids,input_path)
    groups=groups_for(frame,ids,data,cfg);development,holdout=holdout_indices(len(y),ids,groups,cfg)
    if len(holdout) and cfg['v4']['evaluation']['require_declared_types']:
        missing=set(frame.columns)-set(cfg['expert']['field_types'])
        if missing:raise ValueError('固定留出模式请为全部属性声明数值/配置类型，避免根据留出表值推断类型：'+','.join(sorted(missing)))
    dev=frame.iloc[development].reset_index(drop=True);target=y[development];devrows=rows[development];devids=[ids[i] for i in development]
    splits,split_report=make_splits(target,groups[development],cfg)
    if cfg['v4']['search']['mode']=='random' and len(splits)>cfg['v4']['splits']['random_splits']:
        selected=np.sort(np.random.RandomState(cfg['v4']['splits']['seed']).choice(len(splits),cfg['v4']['splits']['random_splits'],replace=False))
        splits=[splits[int(i)] for i in selected]
        split_report.update(evaluated_splits=len(splits),exhaustive=False,random_search_subset=True)
    output=Path(output_dir);output.mkdir(parents=True,exist_ok=True)
    write_json(output/'requested_config.json',cfg);write_json(output/'effective_config.json',cfg)
    write_json(output/'split_plan.json',dict(split_report,partitions=[{'split_id':f['id'],'training_ids':[devids[i] for i in f['train']],'validation_ids':[devids[i] for i in f['validation']]} for f in splits],development_ids=devids,locked_holdout_ids=[ids[i] for i in holdout]))
    ratings={p['parameter_id']:expert_rating(p,cfg)[0] for p in data['parameters']}
    applied={name:ratings[name] if cfg['expert']['enabled'] else 1. for name in frame.columns}
    labels={p['parameter_id']:p['label'] for p in specs}
    category_labels={key:{float(code):label for label,code in values.items()} for key,values in mappings.items()}
    backend=Backend(dev,target,devrows,applied,tuple(mappings),category_labels,cfg['v4']['search']['cache_entries'])
    progress('V4 %s：开发%d条、%d属性、固定复核%d个划分；外层留出%d条。训练门槛%s/%s。'%(cfg['v4']['search']['mode'],len(dev),len(specs),len(splits),len(holdout),cfg['v4']['train_gate']['mode'],cfg['v4']['train_gate']['scope']))
    from services.price_service.optuna_v4.optimizer import budget_plan
    plan=budget_plan(cfg,len(splits));write_json(output/'budget_plan.json',plan)
    progress('配置搜索总预算%d轮；局部预算合计%d轮；另有融合权重%d轮。'%(plan['total_configuration_trials'],sum(plan['per_split_trials']),plan['ensemble_weight_trials']))
    with threadpool_limits(limits=1):
        search=run_search(backend,splits,cfg,progress)
        write_json(output/'search_diagnostics.json',{k:v for k,v in search.items() if k!='reviewed'})
        records=[]
        for r in search['records']:
            m=r['summary'];records.append({'context':r['context'],'phase':r['phase'],'trial':r['trial'],'valid':r['valid'],'train_mape_percent':m['train']['mape_percent'] if m else None,'train_r2':m['train']['r2'] if m else None,'validation_mape_percent':m['validation']['mape_percent'] if m else None,'validation_r2':m['validation']['r2'] if m else None,'train_violation':m['train_violation'] if m else None,'error':r['error'],'parameters':json.dumps(r['parameters'],ensure_ascii=False)})
        write_csv(output/'trials.csv',records)
        if cfg['deployment']['model_scope']=='full_data':
            from services.price_service.optuna_v4.metrics import ranking
            eligible=[]
            for candidate in search['reviewed']:
                try:
                    model=backend.fit(dev,target,candidate['parameters'],applied)
                    prediction=predict_saved({'configurations':[model],'weights':[1.]},dev)
                    score=metrics(target,prediction);gate=cfg['v4']['train_gate']
                    candidate.update(full_fitted=model,deployment_train_prediction=prediction)
                    candidate['summary'].update(deployed_train=score,deployed_train_violation=violation(score,gate['mape_percent_max'],gate['r2_min']));eligible.append(candidate)
                except (ValueError,FloatingPointError,OverflowError) as exc:progress('V4全量重拟合淘汰不稳定候选：'+repr(exc))
            search['reviewed']=sorted(eligible,key=lambda r:ranking(r['summary'],cfg))
        selected,weights,combined=choose_ensemble(search['reviewed'],backend,cfg,progress)
        write_json(output/'cross_split_review.json',[{'parameters':r['parameters'],'source':r['source'],'summary':r['summary']} for r in search['reviewed']])
        # One fixed representative snapshot is retained for exact replay, independently
        # of the default refit on all development records.
        representative=min(combined['folds'],key=lambda f:violation(metrics(target[f['validation']],f['validation_prediction']),cfg['targets']['mape_percent_max'],cfg['targets']['r2_min']))
        def fit_set(indices):
            return {'format':'price-optuna-v4-1.0','columns':list(dev.columns),'weights':weights.tolist(),
                    'configurations':[r['full_fitted'] if len(indices)==len(dev) and 'full_fitted' in r else backend.fit(dev.iloc[indices],target[indices],r['parameters'],applied) for r in selected],
                    'fit_ids':[devids[i] for i in indices]}
        full=fit_set(np.arange(len(dev)));snapshot=fit_set(representative['train'])
        fitted=full if cfg['deployment']['model_scope']=='full_data' else snapshot
        full_train=metrics(target,predict_saved(full,dev))
        fit_indices=np.arange(len(dev)) if cfg['deployment']['model_scope']=='full_data' else representative['train']
        deployed_train=metrics(target[fit_indices],predict_saved(fitted,dev.iloc[fit_indices]))
    for name,value in (('price_ensemble_bundle.pkl',full),('selected_split_bundle.pkl',snapshot)):
        with (output/name).open('wb') as handle:pickle.dump(value,handle,protocol=4)
    with (output/('price_ensemble_bundle.pkl' if cfg['deployment']['model_scope']=='full_data' else 'selected_split_bundle.pkl')).open('rb') as handle:
        replay=pickle.load(handle)
    if not np.allclose(predict_saved(replay,dev),predict_saved(fitted,dev),rtol=1e-12,atol=1e-12):raise AssertionError('保存模型复载不一致。')
    prediction_rows=[]
    for fold in combined['folds']:
        for i,pred in zip(fold['validation'],fold['validation_prediction']):prediction_rows.append({'split_id':fold['id'],'record_id':devids[i],'source_excel_row':int(devrows[i]),'actual_price':float(target[i]),'predicted_price':float(pred)})
    write_csv(output/'cross_split_predictions.csv',prediction_rows)
    write_csv(output/'selected_split_predictions.csv',[r for r in prediction_rows if r['split_id']==representative['id']])
    s=combined['summary'];g=cfg['v4']['train_gate'];train_active=g['mode']!='off'
    actual_train_violation=violation(deployed_train,g['mape_percent_max'],g['r2_min'])
    met=s['validation_violation']==0 and (not train_active or (s['train_violation']==0 and actual_train_violation==0))
    quality='qualified_development' if met else 'unqualified_trial_draft'
    # Locked holdout is evaluated ONCE after configuration/weights/refit are frozen.
    holdout_score=metrics(y[holdout],predict_saved(fitted,frame.iloc[holdout])) if len(holdout) else None
    warning=('开发成绩参与了超参数和融合权重选择，不是独立测试成绩。'
             '当前模型%s；%s。'%(('在全部开发记录重拟合' if cfg['deployment']['model_scope']=='full_data' else '使用所选开发划分快照'),('开发目标达标' if met else '未达标试用草稿')))
    if len(holdout):warning+=' 固定外层留出未参与搜索或重拟合；查看该成绩后再调参则需新的外部数据复验。'
    product=data['products'][0];path=output/'price_native_bundle.pkl';variables={};ledger=[]
    for j,(model,weight) in enumerate(zip(fitted['configurations'],weights)):
        name='configuration_%d'%(j+1);variables[name]=NativeConfigurationMember(model,list(frame.columns),labels)
        ledger.append({'name':name,'configuration':name,'label':'参数组合%d'%(j+1),'family':'ensemble','weight':float(weight),
                       'seed_instances':1,'model_seed':model['params'].get('model_seed',0),'target_transform':'log',
                       'base_weights':{name:float(w) for name,w in zip(model['model_names'],model['weights']) if w>0},'parameters':model['params']})
    metadata=native_field_metadata(frame,specs,mappings)
    for field in metadata.values():field.update(required=True,missing_policy='reject')
    native=export_from_notebook(dict(variables,X_train=dev,price_scaler=None),output=path,product_code=product['product_code'],product_name=product['product_name'],
        model_version='optuna-v4-'+cfg['v4']['search']['mode']+'-1.0',target_divisor_to_wan=1.,field_metadata=metadata,model_variables={k:k for k in variables},
        ensemble_model_names=list(variables),ensemble_weights=weights.tolist(),strict=True,model_source='namespace',model_output_transform='direct')
    attributes=[]
    for p in data['parameters']:
        key=p['parameter_id'];effective=applied.get(key,1.)
        mult={'lasso':.5+(effective-1)/4*2.5,'ridge':.3+(effective-1)/4*4.7,'svr':.5+(effective-1)/4*2.}
        attributes.append({'field_id':key,'label':p['label'],'score':ratings[key],'specified':key in cfg['expert']['scores'],'enabled':cfg['expert']['enabled'],
                           'used_in_training':key in frame.columns,'score_source':expert_rating(p,cfg)[1],'default_score':cfg['expert']['default_score'],'model_multipliers':mult,
                           'multiplier_display':'稀疏 %.3f；岭回归 %.3f；支持向量 %.3f'%(mult['lasso'],mult['ridge'],mult['svr'])})
    native['automl_explanation']={'member_ledger':ledger,'expert_groups':[],'engineering':{},'field_labels':labels,'price_attribute_scores':attributes,
        'expert_scores_enabled':cfg['expert']['enabled'],'expert_records':cfg['expert']['records'],'calibration':1.,'sample_warning':warning,'evidence_sufficient':False,
        'evaluation_scope':'cross_partition_searched_development','deployment_scope':cfg['deployment']['model_scope'],
        'quality_status':quality,'training_metrics':deployed_train,'development_metrics':s['validation'],'holdout_metrics':holdout_score,
        'interval_note':'跨划分开发残差区间；重复预测参与选择，未经独立校准覆盖率'}
    residual=[abs(r['actual_price']-r['predicted_price']) for r in prediction_rows]
    native['residual_calibration']={'space':'native','half_width_native':float(np.quantile(residual,.95)),'coverage':None,'source':'searched_cross_partition_uncalibrated'}
    save_bundle(path,native);restored=load_bundle(path)
    lookup={r['agreement_id']:r['params'] for r in data['agreements']};served=predict_batch(restored,[lookup[i] for i in ids]);reference=predict_saved(fitted,frame)
    delta=max(abs(float(a['predicted_price_wan'])-float(b)) for a,b in zip(served,reference))
    if delta>1e-5:raise AssertionError('V4原生导出预测不一致：'+str(delta))
    for i in range(min(3,len(ids))):
        if abs(predict(restored,lookup[ids[i]])['predicted_price_wan']-served[i]['predicted_price_wan'])>1e-5:raise AssertionError('单条/批量预测不一致。')
    rejected=(cfg['v4']['publish']['policy']=='require_targets' or cfg['targets']['hard_gate']) and not met
    report={'engine':'optuna_v4','implementation_version':'optuna-v4-1.0','selected_model':'V4跨划分六模型参数组合集成',
        'search_mode':cfg['v4']['search']['mode'],'training_rows':len(frame),'development_rows':len(dev),'feature_count':len(specs),'feature_order':list(frame.columns),
        'train_metrics':s['train'],'full_refit_training_metrics':full_train,'deployed_training_metrics':deployed_train,'development_metrics':s['validation'],
        'cv_metrics':{'mean_mape':s['validation']['mape_percent'],'mean_r2':s['validation']['r2'],'std_mape':None,'std_r2':None,'scope':'pooled_cross_partition_searched_development'},
        'holdout_metrics':dict(holdout_score,scope='locked_outer_holdout') if holdout_score else {'mape_percent':None,'r2':None,'scope':'external_test_not_provided'},
        'nested_cv_metrics':{'mean_mape':None,'mean_r2':None},'validation_protocol':'fixed_unique_partitions_with_searched_parameters_and_weights',
        'split_plan':split_report,'cross_split_summary':s,'targets_met':bool(met),'quality_status':quality,'quality_gate_rejected':bool(rejected),
        'business_accuracy_certified':False,'train_gate':g,'deployed_training_gate_passed':bool(actual_train_violation==0),'targets':cfg['targets'],
        'member_count':int(sum(sum(w>0 for w in b['weights']) for b in fitted['configurations'])),'configuration_count':len(ledger),'member_ledger':ledger,'weights':{r['name']:r['weight'] for r in ledger},
        'expert_scores_enabled':cfg['expert']['enabled'],'deployment_scope':cfg['deployment']['model_scope'],'full_data_deployment':cfg['deployment']['model_scope']=='full_data',
        'fit_record_ids':fitted['fit_ids'],'development_record_ids':devids,'holdout_record_ids':[ids[i] for i in holdout],'outer_holdout_used_for_selection':False,
        'native_equivalence_max_delta_wan':float(delta),'saved_reload_verified':True,'budget_plan':search['plan'],'trials_completed':len(search['records']),
        'failed_trials':sum(not r['valid'] for r in search['records']),'phase_counts':search['phase_counts'],'cache_statistics':backend.stats,
        'timed_out':search.get('timed_out',False),'sampler_fallback_count':len(search['sampler_fallbacks']),
        'elapsed_seconds':round(time.monotonic()-started,2),'warning':warning,'min_samples':cfg['input']['min_samples'],'tiny_sample_mode':len(dev)<8,
        'numeric_scaling':'training_only_MinMax','categorical_encoding':'training_only_onehot',
        'attribute_types':{p['parameter_id']:('配置类型' if p['parameter_id'] in mappings else '数值') for p in specs}}
    write_json(output/'training_report.json',report);write_json(output/'selected_configurations.json',ledger);write_json(output/'price_attribute_scores.json',{'attributes':attributes,'records':cfg['expert']['records']})
    manifest_path=Path(str(path)+'.manifest.json');manifest=json.loads(manifest_path.read_text(encoding='utf-8'));manifest.update(bundle_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),automl_explanation=native['automl_explanation']);write_json(manifest_path,manifest)
    progress('V4完成：实际训练MAPE %.3f%%、R² %s；跨划分开发MAPE %.3f%%、R² %s；%s。'%(deployed_train['mape_percent'],fmt(deployed_train['r2']),s['validation']['mape_percent'],fmt(s['validation']['r2']),quality))
    if rejected:raise ValueError('未达训练/开发门槛，诊断已保存，旧运行模型保留。可在菜单切换为允许试用草稿。')
    return native,report
