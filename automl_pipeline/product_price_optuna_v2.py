# -*- coding: utf-8 -*-
"""Adapt the supplied joint NSGA-II six-model engine to the product pipeline."""
import copy
import hashlib
import json
import time
from pathlib import Path
from app.console_compat import enable_plain_console
enable_plain_console()
import numpy as np
from threadpoolctl import threadpool_limits
from services.price_service.optuna_v2 import price_automl_core as core
from services.price_service.optuna_v2.optimizer import run_training
from services.price_service.optuna_v2.native_member import NativeOptunaMember
from services.price_service.export_native_price_bundle import export_from_notebook
from services.price_service.native_bundle import save_bundle,load_bundle,predict,predict_batch
from tools.product_price_automl import price_dataset,native_field_metadata,write_json
from tools.product_r4_input import apply_field_types,ratings_from_workbook
from services.price_service.optuna_v2.typed_features import apply_types,canonical_type,TypedBackend,native_preprocessor

DEFAULT={'schema_version':'price-optuna-v2-pipeline-1.0',
 'search':{'n_trials':150,'optimizer_seed':42,'seed_low':1,'seed_high':10000,'timeout_seconds':None},
 'validation':{'test_size':.2,'price_bins':3,'auto_adjust_test_size':True},
 'targets':{'r2_min':.9,'mape_percent_max':10.,'hard_gate':False},
 'expert':{'enabled':True,'default_score':1.,'use_supplied_defaults':True,'scores':{},'records':[],'field_types':{}},
 'input':{'missing_tokens':[],'remove_outliers':False,'min_samples':4},
 'deployment':{'model_scope':'full_data'}}
LABELS={'lasso':'稀疏回归','ridge':'岭回归','expert_tree':'专家分裂树','extratrees':'极端随机树','svr':'支持向量回归','gbdt':'梯度提升树'}


def load_config(path=None,overrides=None):
    result=copy.deepcopy(DEFAULT)
    def merge(target,source,prefix=''):
        if not isinstance(source,dict):raise ValueError('配置必须是对象：'+prefix)
        for key,value in source.items():
            if key not in target:raise ValueError('未知Optuna V2配置：'+prefix+key)
            if isinstance(target[key],dict) and key not in ('scores','field_types'):merge(target[key],value,prefix+key+'.')
            else:target[key]=copy.deepcopy(value)
    if path:merge(result,json.loads(Path(path).read_text(encoding='utf-8-sig')))
    if overrides:merge(result,overrides)
    if result['schema_version']!=DEFAULT['schema_version']:raise ValueError('Optuna V2配置版本无效。')
    s=result['search'];v=result['validation'];q=result['targets'];e=result['expert']
    for value in (s['n_trials'],v['price_bins']):
        if not isinstance(value,int) or isinstance(value,bool) or value<1:raise ValueError('试验次数和价格分层数须为正整数。')
    for key in ('optimizer_seed','seed_low','seed_high'):
        if not isinstance(s[key],int) or isinstance(s[key],bool) or not 0<=s[key]<=2**32-1:raise ValueError('随机种子须为非负32位整数。')
    if s['seed_high']<s['seed_low'] or s['n_trials']>100000:raise ValueError('种子范围或试验次数无效。')
    if not isinstance(v['test_size'],(int,float)) or not 0<v['test_size']<.8:raise ValueError('验证比例须在0—0.8之间。')
    if s['timeout_seconds'] is not None and (not isinstance(s['timeout_seconds'],(int,float)) or not np.isfinite(s['timeout_seconds']) or s['timeout_seconds']<=0):raise ValueError('时间预算须为正数或null。')
    if not np.isfinite(q['r2_min']) or q['r2_min']>1 or not np.isfinite(q['mape_percent_max']) or q['mape_percent_max']<=0:raise ValueError('精度目标无效。')
    for flag in (q['hard_gate'],e['enabled'],e['use_supplied_defaults'],result['input']['remove_outliers'],v['auto_adjust_test_size']):
        if not isinstance(flag,bool):raise ValueError('开关必须填写true/false。')
    minimum=result['input']['min_samples']
    if not isinstance(minimum,int) or isinstance(minimum,bool) or minimum<4:raise ValueError('input.min_samples必须是至少4的整数，训练与R方验证各需至少2条。')
    for key,value in dict(e['scores'],__default__=e['default_score']).items():
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not np.isfinite(value) or not 1<=value<=5:raise ValueError('价格属性%s评分必须为1—5分。'%key)
    if result['deployment']['model_scope'] not in ('full_data','selected_split'):raise ValueError('部署范围须为full_data或selected_split。')
    if not isinstance(result['input']['missing_tokens'],list):raise ValueError('缺失标识须为列表。')
    result['expert']['field_types']={key:canonical_type(value) for key,value in e['field_types'].items()}
    return result


def workbook_config(source,cfg,known,loader=load_config):
    cfg=copy.deepcopy(cfg)
    if source:
        scores,records,types,unused=ratings_from_workbook(source,known)
        cfg['expert']['scores'].update(scores)
        if records:cfg['expert']['records']=records
        cfg['expert']['field_types'].update(types)
    unknown=(set(cfg['expert']['scores'])|set(cfg['expert']['field_types']))-set(known)
    if unknown:raise ValueError('价格专家配置引用未知英文代号：'+','.join(sorted(unknown)))
    return loader(overrides=cfg)


def expert_rating(parameter,cfg):
    key=parameter['parameter_id'];e=cfg['expert']
    if key in e['scores']:return float(e['scores'][key]),'workbook_or_configuration'
    if e['use_supplied_defaults']:
        label=parameter['label'];unit=parameter.get('unit') or ''
        for candidate in (key,label,label+'_'+unit if unit else label):
            if candidate in core.EXPERT_MAP:return float(core.EXPERT_MAP[candidate]),'supplied_default'
    return float(e['default_score']),'default'


def training_split(records,cfg):
    minimum=cfg['input']['min_samples'];requested=float(cfg['validation']['test_size'])
    if records<minimum:raise ValueError('有效价格记录%d条，少于input.min_samples配置的%d条。'%(records,minimum))
    proposed=int(np.ceil(records*requested));actual=max(2,min(records-2,proposed));adjusted=actual!=proposed
    if adjusted and not cfg['validation']['auto_adjust_test_size']:
        raise ValueError('验证划分至少需要2条成品、训练集至少2条才能计算R方；请调整validation.test_size或启用auto_adjust_test_size。')
    effective=actual/float(records) if adjusted else requested
    # Avoid a rounded-up extra record when k/n is not exactly representable.
    if adjusted and int(np.ceil(records*effective))!=actual:effective=float(np.nextafter(effective,0.))
    return effective,{'min_samples':minimum,'records':records,'requested_test_size':requested,'effective_test_size':effective,
        'requested_test_rows':proposed,'validation_rows':actual,'training_rows':records-actual,'adjusted':adjusted,
        'auto_adjust_test_size':cfg['validation']['auto_adjust_test_size'],'tiny_sample_mode':records<8}


def dataset(data,cfg):
    data=copy.deepcopy(data);apply_types(data,cfg['expert']['field_types'])
    frame,y,specs,mappings,groups,ids=price_dataset(data,1)
    # Preserve the supplied loader's exclusion of exploratory diagnostic fields.
    frame=frame.loc[:,[name for name in frame.columns if name not in core.EXCLUDE_COLS]]
    specs=[p for p in specs if p['parameter_id'] in frame.columns]
    mappings={name:value for name,value in mappings.items() if name in frame.columns}
    if not len(specs):raise ValueError('去除诊断列后没有可用于价格训练的属性。')
    groups=np.unique(frame.values,axis=0,return_inverse=True)[1]
    training_split(len(y),cfg)
    if np.ptp(y)==0:raise ValueError('有效成品价格完全相同，无法进行R方目标搜索。')
    missing=np.argwhere(~np.isfinite(frame.values))
    if len(missing):
        i,j=missing[0];raise ValueError('成品%s的价格输入属性“%s”缺失；此版本要求处理完整的数值特征，不自动填充或删除记录。'%(ids[i],specs[j]['label']))
    return frame,y,specs,mappings,groups,ids


def training_source_rows(data,ids,source):
    # Physical workbook rows are retained even when blank rows precede headers.
    numbers=None
    if source and Path(source).suffix.lower()=='.xlsx':
        from app.product_table import product_table_rows,clean_field_id
        from app.xlsx_utils import read_workbook_details
        raw=Path(source).read_bytes();table=product_table_rows(Path(source).name,raw,require_dual_header=True)
        sheet=next(s for s in read_workbook_details(raw) if s['name']==table['sheet_name'])
        numbers=[n for row,n in zip(sheet['rows'],sheet['row_numbers']) if any(clean_field_id(x) for x in row)][2:]
    if numbers is None:numbers=list(range(3,len(data['agreements'])+3))
    row_by_id={r['agreement_id']:n for r,n in zip(data['agreements'],numbers)}
    return np.asarray([row_by_id[i] for i in ids],int)


def train_price_optuna_v2(data,output_dir,config_path=None,progress=None,overrides=None,input_path=None,engine='optuna_v2',trace=None,search_observer=None):
    loader=load_config
    if engine=='optuna_v3':
        from .product_price_optuna_v3 import load_config as loader
    elif engine!='optuna_v2':raise ValueError('未知Optuna引擎。')
    cfg=loader(config_path,overrides);known={p['parameter_id'] for p in data['parameters']}
    cfg=workbook_config(input_path,cfg,known,loader=loader)
    data=copy.deepcopy(data)
    if input_path:
        from app.product_table import product_table_rows
        from services.price_service.optuna_v2.typed_features import restore_configurations
        source_path=Path(input_path)
        restore_configurations(data,product_table_rows(source_path.name,source_path.read_bytes(),require_dual_header=True),cfg['expert']['field_types'],cfg['input']['missing_tokens'])
    apply_types(data,cfg['expert']['field_types'])
    frame,y,specs,mappings,groups,ids=dataset(data,cfg);rows=training_source_rows(data,ids,input_path)
    output=Path(output_dir);output.mkdir(parents=True,exist_ok=True);progress=progress or (lambda s:print(s,flush=True));start=time.monotonic()
    ratings={p['parameter_id']:expert_rating(p,cfg)[0] for p in data['parameters']}
    applied={name:(ratings[name] if cfg['expert']['enabled'] else 1.) for name in frame.columns}
    filter_report={'policy':'none','removed_record_ids':[],'input_priced_rows':len(y)}
    if cfg['input']['remove_outliers']:
        from sklearn.preprocessing import StandardScaler
        from sklearn.ensemble import IsolationForest
        with threadpool_limits(limits=1):keep=IsolationForest(contamination=.05,random_state=42).fit_predict(StandardScaler().fit_transform(frame))!=-1
        filter_report.update(policy='source_optional_feature_IsolationForest_0.05_seed42',removed_record_ids=[i for i,k in zip(ids,keep) if not k])
        frame=frame.iloc[np.flatnonzero(keep)].reset_index(drop=True);y=y[keep];rows=rows[keep];groups=groups[keep];ids=[i for i,k in zip(ids,keep) if k]
    effective_test_size,split_report=training_split(len(y),cfg)
    write_json(output/'requested_config.json',cfg)
    cfg=copy.deepcopy(cfg);cfg['validation']['test_size']=effective_test_size
    write_json(output/'effective_config.json',cfg);write_json(output/'input_filter_report.json',filter_report);write_json(output/'validation_split.json',split_report)
    version='V3' if engine=='optuna_v3' else 'V2'
    progress('Optuna %s：%d条、%d属性，联合搜索划分种子、六模型参数及权重。'%(version,len(y),len(specs)))
    if split_report['adjusted']:progress('验证比例已从%.4f调整为%.4f：%d条训练、%d条验证。'%(split_report['requested_test_size'],effective_test_size,split_report['training_rows'],split_report['validation_rows']))
    s=cfg['search'];v=cfg['validation'];q=cfg['targets']
    category_fields=tuple(mappings);category_labels={key:{float(code):label for label,code in values.items()} for key,values in mappings.items()}
    backend=TypedBackend(category_fields,category_labels)
    search_identity=hashlib.sha256(json.dumps({'format':'price-typed-core-1.0','core_sha256':hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest(),
        'typed_preprocessing_sha256':hashlib.sha256(Path(core.__file__).with_name('typed_features.py').read_bytes()).hexdigest(),
        'X':frame.values.tolist(),'columns':list(frame.columns),'y':y.tolist(),'rows':rows.tolist(),'experts':applied,
        'mappings':mappings,'validation':v,'encoding':'training_only_onehot_then_MinMax_1.0'},sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()
    runner=run_training;extra={'model_backend':backend,'history_identity':search_identity}
    if engine=='optuna_v3':
        from services.price_service.optuna_v2.cached_backend import CachedBackend
        from services.price_service.optuna_v2.optimizer_v3 import run_training as runner
        from .product_price_optuna_v3 import search_settings
        backend=CachedBackend(category_fields,category_labels,cfg['v3']['cache_entries'])
        extra={'model_backend':backend,'history_identity':search_identity,'v3_settings':search_settings(cfg)}
        if search_observer is not None:extra['observer']=search_observer
    if trace is not None:backend.trace=trace
    with threadpool_limits(limits=1):
        result=runner(data_path=str(input_path or 'pipeline_prepared_data'),output_dir=str(output),n_trials=s['n_trials'],target='price_wan',
            test_size=v['test_size'],price_bins=v['price_bins'],r2_target=q['r2_min'],mape_target=q['mape_percent_max']/100.,
            seed_low=s['seed_low'],seed_high=s['seed_high'],optimizer_seed=s['optimizer_seed'],drop_first=False,remove_outliers=False,verbose=False,
            prepared_data=(frame,y,rows),expert_scores=applied,progress_callback=progress,timeout_seconds=s['timeout_seconds'],min_samples=cfg['input']['min_samples'],**extra)
    deployment_scope=cfg['deployment']['model_scope']
    fitted=core.load_bundle(result['saved_model'] if deployment_scope=='full_data' else result['saved_validation_model'])
    labels={p['parameter_id']:p['label'] for p in specs};variables={};ledger=[]
    encoder=fitted.get('input_encoder');owners=encoder.owners if encoder else {key:key for key in frame.columns}
    model_labels={key:labels[owner]+('＝'+encoder.labels[key] if encoder and key in encoder.labels else '') for key,owner in owners.items()}
    for index,name in enumerate(core.MODEL_NAMES):
        weight=float(fitted['weights'][index])
        if weight<=0:continue
        variables[name]=NativeOptunaMember(name,fitted['models'][name],fitted['feature_names'],fitted['expert_map'],model_labels,fitted['scaler'],owners,encoder.labels if encoder else {})
        ledger.append({'name':name,'configuration':name,'label':LABELS[name],'family':'tree' if name in ('expert_tree','extratrees','gbdt') else 'kernel' if name=='svr' else 'linear',
                       'weight':weight,'seed_instances':1,'model_seed':0,'split_seed':result['selected_split_seed'],'target_transform':'log'})
    metadata=native_field_metadata(frame,specs,mappings)
    for field in metadata.values():field.update(required=True,missing_policy='reject')
    product=data['products'][0];path=output/'price_native_bundle.pkl'
    native=export_from_notebook(dict(variables,X_train=frame,price_scaler=native_preprocessor(fitted)),output=path,
        product_code=product['product_code'],product_name=product['product_name'],model_version='user-optuna-'+version.lower()+'-joint-pareto-2.0',
        target_divisor_to_wan=1.,field_metadata=metadata,model_variables={k:k for k in variables},ensemble_model_names=list(variables),
        ensemble_weights=[r['weight'] for r in ledger],strict=True,model_source='namespace',model_output_transform='direct')
    attributes=[]
    for p in data['parameters']:
        key=p['parameter_id'];score=ratings[key];used=key in frame.columns;effective=applied.get(key,1.)
        multipliers={'lasso':.5+(effective-1)/4*2.5,'ridge':.3+(effective-1)/4*4.7,'svr':.5+(effective-1)/4*2.}
        attributes.append({'field_id':key,'label':p['label'],'score':score,'specified':key in cfg['expert']['scores'],'enabled':cfg['expert']['enabled'],
            'used_in_training':used,'exclusion_reason':'' if used else '原版本排除的诊断列' if key in core.EXCLUDE_COLS else '未编码文本或整列无有效观测，未用于价格回归',
            'score_source':expert_rating(p,cfg)[1],'default_score':cfg['expert']['default_score'],
            'model_multipliers':multipliers,'multiplier_display':'稀疏 %.3f；岭回归 %.3f；支持向量 %.3f'%(multipliers['lasso'],multipliers['ridge'],multipliers['svr'])})
    warning='指标为Optuna搜索过的划分开发成绩；当前服务使用%s模型。真实业务精度尚需验证。'%('全量重拟合' if deployment_scope=='full_data' else '所选划分快照')
    if split_report['tiny_sample_mode']:warning+=' 当前仅%d条记录，验证分数易随划分变化。'%len(y)
    native['automl_explanation']={'member_ledger':ledger,'expert_groups':[],'engineering':{},'field_labels':labels,
        'price_attribute_scores':attributes,'expert_scores_enabled':cfg['expert']['enabled'],'expert_records':cfg['expert']['records'],
        'calibration':1.,'sample_warning':warning,'evidence_sufficient':False,
        'evaluation_scope':'searched_split_development','deployment_scope':deployment_scope,
        'interval_note':'所选开发划分残差的探索区间，未经独立数据校准覆盖率'}
    import pandas as pd
    validation=pd.read_csv(output/'selected_split_predictions.csv')
    residual=np.abs(validation['actual_price'].values-validation['predicted_price'].values)
    native['residual_calibration']={'space':'native','half_width_native':float(np.quantile(residual,.95)),'coverage':None,'source':'searched_split_uncalibrated'}
    save_bundle(path,native);restored=load_bundle(path)
    params_by_id={r['agreement_id']:r['params'] for r in data['agreements']}
    native_rows=[params_by_id[i] for i in ids]
    reference,components=backend.predict(fitted,frame)
    served=predict_batch(restored,native_rows);delta=max(abs(float(r['predicted_price_wan'])-float(p)) for r,p in zip(served,reference))
    if delta>1e-5:raise AssertionError('原Optuna模型与流水线原生包预测不一致：'+str(delta))
    for i in range(min(3,len(frame))):
        if abs(predict(restored,native_rows[i])['predicted_price_wan']-served[i]['predicted_price_wan'])>1e-5:raise AssertionError('批量与单条预测不一致。')
    validation_fit=core.load_bundle(result['saved_validation_model']);lookup={int(n):i for i,n in enumerate(rows)}
    validation_frame=frame.iloc[[lookup[int(n)] for n in validation['source_excel_row']]]
    replay,_=backend.predict(validation_fit,validation_frame)
    if not np.allclose(replay,validation['predicted_price'].values,rtol=1e-10,atol=1e-10):raise AssertionError('所选划分模型重新加载后预测不一致。')
    metrics=result['validation_metrics'];met=metrics['r2']>=q['r2_min'] and metrics['mape']*100<=q['mape_percent_max']
    report={'engine':engine,'implementation_version':'supplied-optuna-v2-typed-2.0' if engine=='optuna_v2' else 'project-optuna-v3-1.0','selected_model':'Optuna双目标六模型集成',
        'training_rows':len(frame),'feature_count':len(specs),'feature_order':list(frame.columns),'representative_seed':result['selected_split_seed'],
        'seed_selection':'joint_split_seed_hyperparameters_and_six_weights_'+('NSGAII_MOTPE' if engine=='optuna_v3' else 'NSGAII'),'selected_trial':result['selected_trial'],
        'selected_parameters':result['selected_parameters'],'selection_mode':result['selection_mode'],'trials_completed':result['n_trials'],'trials_requested':s['n_trials'],
        'cv_metrics':{'mean_mape':metrics['mape']*100,'mean_r2':metrics['r2'],'std_mape':None,'std_r2':None,'scope':'searched_split_development_not_cv'},
        'holdout_metrics':{'mape_percent':None,'r2':None,'scope':'external_test_not_provided'},
        'nested_cv_metrics':{'mean_mape':None,'mean_r2':None},'validation_metrics':metrics,'validation_protocol':'joint_searched_split',
        'validation_train_rows':result['validation_train_rows'],'validation_test_rows':result['validation_test_rows'],
        'validation_record_ids':[ids[lookup[int(n)]] for n in validation['source_excel_row']],
        'targets_met':bool(met),'cv_targets_met':None,'holdout_targets_met':None,'business_accuracy_certified':False,
        'member_count':len(ledger),'configuration_count':1,'member_ledger':ledger,'weights':{r['name']:r['weight'] for r in ledger},
        'expert_scores_enabled':cfg['expert']['enabled'],'deployment_scope':deployment_scope,'full_data_deployment':deployment_scope=='full_data',
        'native_equivalence_max_delta_wan':float(delta),'selected_split_reload_verified':True,'quality_gate_rejected':bool(q['hard_gate'] and not met),
        'elapsed_seconds':round(time.monotonic()-start,2),'warning':warning,'targets':q,'feasible_pareto_count':result['feasible_pareto_count'],
        'encoded_feature_count':len(fitted['feature_names']),'attribute_types':{p['parameter_id']:('配置类型' if p['parameter_id'] in mappings else '数值') for p in specs},
        'search_identity':search_identity,'categorical_encoding':'training_only_onehot','numeric_scaling':'training_only_MinMax',
        'min_samples':cfg['input']['min_samples'],'tiny_sample_mode':split_report['tiny_sample_mode'],'validation_split':split_report}
    for key in ('history_trials_imported','history_policy','phase_counts','selected_phase','tpe_inherited_completed_trials','candidate_pool_count','cache_statistics','failed_trials','sampler_fallback_count','implementation_provenance'):
        if key in result:report[key]=result[key]
    report.update(selected_training_metrics=result.get('selected_training_metrics'),full_refit_training_metrics=result.get('full_refit_training_metrics'),
                  deployed_training_metrics=result.get('full_refit_training_metrics') if deployment_scope=='full_data' else result.get('selected_training_metrics'))
    write_json(output/'training_report.json',report);write_json(output/'price_attribute_scores.json',{'attributes':attributes,'records':cfg['expert']['records']})
    manifest_path=Path(str(path)+'.manifest.json');manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest.update(bundle_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),automl_explanation=native['automl_explanation']);write_json(manifest_path,manifest)
    progress('Optuna %s完成：开发MAPE %.3f%%，R² %.6f；已导出%s原生价格模型。'%(version,metrics['mape']*100,metrics['r2'],deployment_scope))
    if report['quality_gate_rejected']:raise ValueError('开发指标未达目标，已保存诊断；旧运行版本保留。')
    return native,report
