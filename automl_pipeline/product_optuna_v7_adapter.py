# -*- coding: utf-8 -*-
import copy,hashlib,json,pickle,shutil,time,traceback
from pathlib import Path
import numpy as np
from threadpoolctl import threadpool_limits
from .product_price_optuna_v7 import load_config,baseline_config,history_references
from .product_price_optuna_v2 import train_price_optuna_v2,workbook_config,dataset,training_source_rows,expert_rating,LABELS
from .product_optuna_v4_adapter import write_csv
from tools.product_price_automl import write_json,native_field_metadata
from services.price_service.optuna_v2.typed_features import apply_types,restore_configurations
from services.price_service.optuna_v4.native_member import NativeConfigurationMember,predict_saved
from services.price_service.optuna_v5.trace import TrainingTrace,plain
from services.price_service.optuna_v5.persistence import BaselineObserver,save_state,load_state
from services.price_service.optuna_v7.engine import run
from services.price_service.optuna_v7.backend import NativeV7ConfigurationMember
from services.price_service.export_native_price_bundle import export_from_notebook
from services.price_service.native_bundle import save_bundle,load_bundle,predict_batch,predict
ROOT=Path(__file__).resolve().parents[1]

def _train(data,output,cfg_path,overrides,input_path,trace):
    started=time.monotonic();data=copy.deepcopy(data)
    cfg=workbook_config(input_path,load_config(cfg_path,overrides),{p['parameter_id'] for p in data['parameters']},loader=load_config)
    trace.heartbeat_seconds=cfg['v7']['logging']['heartbeat_seconds'];trace.emit('v7_settings',config=cfg)
    if input_path:
        from app.product_table import product_table_rows
        src=Path(input_path);restore_configurations(data,product_table_rows(src.name,src.read_bytes(),require_dual_header=True),cfg['expert']['field_types'],cfg['input']['missing_tokens'])
    apply_types(data,cfg['expert']['field_types']);frame,y,specs,mappings,groups,ids=dataset(data,cfg);rows=training_source_rows(data,ids,input_path)
    if cfg['v7']['consistency']['mode']!='off' and any(float(s)!=int(float(s)) for s in cfg['expert']['scores'].values()):raise ValueError('可选五档指标只接收1—5整数专家评分。')
    ratings={p['parameter_id']:expert_rating(p,cfg)[0] for p in specs};expert={k:(v if cfg['expert']['enabled'] else 1.) for k,v in ratings.items()}
    code={str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'services/price_service/optuna_v7').glob('*.py')}
    for name in ('automl_pipeline/product_price_optuna_v7.py','automl_pipeline/product_optuna_v7_adapter.py','automl_pipeline/product_price_optuna_v2.py',
        'services/price_service/optuna_v2/price_automl_core.py','services/price_service/optuna_v2/optimizer_v3.py','services/price_service/optuna_v2/typed_features.py',
        'services/price_service/optuna_v4/metrics.py','services/price_service/optuna_v4/native_member.py','services/price_service/native_bundle.py','services/price_service/export_native_price_bundle.py'):
        code[name]=hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
    identity_cfg=copy.deepcopy(cfg);identity_cfg['v7']['persistence']['directory']=None
    contract={'products':data['products'],'parameters':specs,'source_sha256':hashlib.sha256(Path(input_path).read_bytes()).hexdigest() if input_path else None,
        'v3_history_references':history_references(cfg)}
    identity=hashlib.sha256(json.dumps([frame.values.tolist(),list(frame.columns),y.tolist(),rows.tolist(),expert,mappings,contract,identity_cfg,code],sort_keys=True).encode('utf-8')).hexdigest()
    persist=cfg['v7']['persistence'];checkpoint=Path(persist['directory'] or ROOT/'state/v7')
    if not checkpoint.is_absolute():checkpoint=ROOT/checkpoint
    root=(checkpoint/identity[:24]) if persist['enabled'] else output/'transient_search';root.mkdir(parents=True,exist_ok=True)
    basecfg=baseline_config(cfg);basecfg['targets']['hard_gate']=False
    baseline_code={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
        'automl_pipeline/product_price_optuna_v2.py','automl_pipeline/product_price_optuna_v3.py',
        'services/price_service/optuna_v2/price_automl_core.py','services/price_service/optuna_v2/optimizer.py',
        'services/price_service/optuna_v2/optimizer_v3.py','services/price_service/optuna_v2/cached_backend.py','services/price_service/optuna_v2/typed_features.py')}
    baseline_identity=hashlib.sha256(json.dumps([frame.values.tolist(),list(frame.columns),y.tolist(),rows.tolist(),expert,mappings,contract,basecfg,baseline_code],sort_keys=True).encode('utf-8')).hexdigest()
    base_root=checkpoint/'original_v3_baselines'/baseline_identity[:24] if persist['enabled'] else root
    base_root.mkdir(parents=True,exist_ok=True);base=base_root/'baseline_source'
    marker=load_state(base_root/'baseline_done',baseline_identity) if persist['enabled'] and persist['resume'] else None
    base_files=('training_report.json','effective_config.json','price_ensemble_bundle.pkl','selected_split_bundle.pkl','price_native_bundle.pkl','study_history.json','best_config.json')
    if marker:
        for name,digest in marker['sha256'].items():
            if hashlib.sha256((base/name).read_bytes()).hexdigest()!=digest:raise ValueError('V3基线检查点产物变更，拒绝复用：'+name)
        baseline= json.loads((base/'training_report.json').read_text(encoding='utf-8'));baseline_new_fits=0
        trace.emit('v7_baseline_restored',sha256=marker['sha256'],baseline_parameters=baseline['selected_parameters'])
    else:
        observer=BaselineObserver(base_root/'baseline_state',baseline_identity,resume=persist['resume'],enabled=persist['enabled'],progress=trace.message)
        observer.trace=trace
        with trace.span('v7_original_v3_baseline',training_rows=rows,baseline_config=basecfg):
            with threadpool_limits(limits=1):
                _,baseline=train_price_optuna_v2(data,base,progress=trace.message,overrides=basecfg,input_path=input_path,
                    engine='optuna_v3',trace=trace,search_observer=observer)
        baseline_new_fits=baseline.get('cache_statistics',{}).get('model_fits',0)-observer.restored_fits
        marker={'sha256':{name:hashlib.sha256((base/name).read_bytes()).hexdigest() for name in base_files}}
        if persist['enabled']:save_state(base_root/'baseline_done',marker,baseline_identity)
    destination=output/'baseline_v3';shutil.copytree(str(base),str(destination),dirs_exist_ok=True)
    effective=json.loads((base/'effective_config.json').read_text(encoding='utf-8'));cfg['validation']=effective['validation']
    write_json(output/'effective_config.json',cfg);write_json(output/'requested_config.json',load_config(cfg_path,overrides))
    labels={p['parameter_id']:p['label'] for p in specs};categories={key:{float(code):label for label,code in value.items()} for key,value in mappings.items()}
    trace.emit('v7_input',rows=len(y),attributes=len(specs),source_rows=rows,explicit_expert_scores=cfg['expert']['scores'],baseline_sha256=marker['sha256'])
    with threadpool_limits(limits=1):result=run(frame,y,rows,expert,tuple(mappings),categories,cfg,base,baseline,root,identity,trace)
    models={};ledger=[];modelpack=result['pack']
    for i,(bundle,weight,entry) in enumerate(zip(modelpack['configurations'],modelpack['weights'],modelpack['entries'])):
        name='v3plus_%d'%(i+1);recipe=entry['recipe'];models[name]=NativeV7ConfigurationMember(bundle,list(frame.columns),labels)
        ledger.append({'name':name,'configuration':name,'label':'V3+参数方案%d（%s）'%(i+1,entry['name']),'family':'ensemble','weight':float(weight),
            'seed_instances':len(recipe.get('model_seeds',[0])),'parameters':recipe['params'],'model_seeds':recipe.get('model_seeds',[0]),'expert_strength':recipe.get('expert_strength',1.)})
    attributes=[]
    uses_expert=cfg['expert']['enabled'] and any(float(w)>0 and e['recipe'].get('expert_strength',1.)>0 for w,e in zip(modelpack['weights'],modelpack['entries']))
    for p in specs:
        k=p['parameter_id'];attributes.append({'field_id':k,'label':p['label'],'score':ratings[k],'enabled':cfg['expert']['enabled'],
            'specified':k in cfg['expert']['scores'],'score_source':expert_rating(p,cfg)[1],'default_score':cfg['expert']['default_score'],'used_in_training':uses_expert})
    met=result['targets_met'];quality='v3plus_development_targets_met' if met else 'unqualified_trial_draft'
    warning='V7/V3+：原V3获分模型完整保留；主模型全量训练。原划分及共同复核均参与选择，属于开发成绩，未提供独立测试集。'
    if not met:warning+=' 当前未达训练/开发或启用的专家门槛，只能作为试用草稿。'
    metadata=native_field_metadata(frame,specs,mappings)
    for field in metadata.values():field.update(required=True,missing_policy='reject')
    product=data['products'][0];path=output/'price_native_bundle.pkl'
    with trace.span('v7_native_export',configurations=len(models)):
        native=export_from_notebook(dict(models,X_train=frame,price_scaler=None),output=path,product_code=product['product_code'],product_name=product['product_name'],
            model_version='optuna-v7-v3plus-'+cfg['v7']['mode']+'-1.0',target_divisor_to_wan=1.,field_metadata=metadata,
            model_variables={k:k for k in models},ensemble_model_names=list(models),ensemble_weights=modelpack['weights'],strict=True,model_source='namespace',model_output_transform='direct')
        native['residual_calibration']=result['interval_calibration']
        native['automl_explanation']={'member_ledger':ledger,'expert_groups':[],'engineering':{},'field_labels':labels,'price_attribute_scores':attributes,
            'expert_scores_enabled':cfg['expert']['enabled'],'expert_records':cfg['expert']['records'],'calibration':1.,'sample_warning':warning,
            'evidence_sufficient':False,'quality_status':quality,'protocol':'v3plus_common_development','evaluation_scope':'development_not_independent_test',
            'deployment_scope':'full_data','training_metrics':result['train'],'development_metrics':result['cv'] or result['anchor'],
            'anchor_development_metrics':result['anchor'],'common_development_metrics':result['cv'],
            'reference_metrics':result['reference_anchor'],'reference_training_metrics':result['reference_train'],'reference_cv_metrics':result['reference_cv'],
            'test_metrics':{'mape_percent':None,'r2':None,'scope':'external_test_not_provided'},'weights_fitted_on_validation':True,
            'weight_policy':'v3_prior_shrunk_on_common_development','interval_note':'开发残差探索区间，覆盖率未校准。',
            'feature_importance':result['importance'],'expert_grade_consistency':result['consistency']}
        save_bundle(path,native)
    source_pack=output/'price_ensemble_bundle.pkl';source_pack.write_bytes(pickle.dumps(modelpack,protocol=4));native=load_bundle(path)
    raw=[r['params'] for r in data['agreements']];lookup={r['agreement_id']:r['params'] for r in data['agreements']}
    responses=predict_batch(native,[lookup[k] for k in ids]);expected=predict_saved(modelpack,frame)
    delta=max(abs(r['predicted_price_wan']-float(p)) for r,p in zip(responses,expected))
    if delta>1e-5:raise AssertionError('V7原生导出与训练包不一致。')
    for k,r in zip(ids[:3],responses[:3]):
        if predict(native,lookup[k])['predicted_price_wan']!=r['predicted_price_wan']:raise AssertionError('V7单条/批量预测不一致。')
    write_csv(output/'training_predictions.csv',[{'record_id':k,'source_excel_row':int(row),'actual_price':float(a),'predicted_price':float(p),'ape_percent':float(abs(a-p)/a*100.)} for k,row,a,p in zip(ids,rows,y,expected)])
    development=result['cv'] or result['anchor'];rejected=not met and (cfg['targets']['hard_gate'] or cfg['v7']['publish']['policy']=='require_targets')
    report={'engine':'optuna_v7','implementation_version':'v7-v3plus-1.0','protocol':'v3plus_common_development','search_mode':cfg['v7']['mode'],
        'selected_model':result['selected_name'],'training_rows':len(y),'feature_count':len(specs),'feature_order':list(frame.columns),
        'train_metrics':result['train'],'deployed_training_metrics':result['train'],'full_refit_training_metrics':result['train'],
        'selected_split_development_metrics':result['anchor'],'development_metrics':development,'common_development_metrics':result['cv'],
        'cv_metrics':{'mean_mape':development['mape_percent'],'mean_r2':development['r2'],'scope':'common_development' if result['cv'] else 'searched_split_development'},
        'holdout_metrics':{'mape_percent':None,'r2':None,'scope':'external_test_not_provided'},'test_metrics':{'mape_percent':None,'r2':None,'scope':'external_test_not_provided'},
        'nested_cv_metrics':{'mean_mape':None,'mean_r2':None},'targets_met':met,'quality_status':quality,'quality_gate_rejected':rejected,
        'business_accuracy_certified':False,'independent_test_provided':False,'weights_fitted_on_validation':True,'deployment_scope':'full_data','full_data_deployment':True,
        'baseline_v3':{'directory':'baseline_v3','artifact_sha256':marker['sha256'],'primary':result['reference_anchor'],'common_development':result['reference_cv'],
            'full_training':result['reference_train'],'selected_parameters':baseline['selected_parameters'],'search_config':effective,'report':baseline},
        'enhancement_applied':result['enhancement_applied'],'review_status':result['review_status'],'stop_reason':result['stop_reason'],
        'evaluation_plan':result['plan'],'candidate_reviews':result['review_candidates'],'actual_finalists':result['finalists'],
        'feature_importance':result['importance'],'expert_grade_consistency':result['consistency'],'expert_scores_enabled':cfg['expert']['enabled'],
        'member_count':len(ledger),'configuration_count':len(ledger),'member_ledger':ledger,'weights':{r['name']:r['weight'] for r in ledger},
        'baseline_new_fits':baseline_new_fits,'extra_fits_this_session':result['extra_fits_this_session'],'fit_statistics':result['fit_statistics'],
        'checkpoint_restored':bool(marker and baseline_new_fits==0),'checkpoint_directory':str(root),'search_identity':identity,
        'v3_history_references':contract['v3_history_references'],
        'native_equivalence_max_delta_wan':delta,'saved_reload_verified':True,
        'baseline_core_unchanged':code['services/price_service/optuna_v2/price_automl_core.py']=='f8f9877029e13535b97b40c429fb4afe6b5d613db635b6bee83307fca30b2899',
        'code_fingerprints':code,'baseline_code_fingerprints':baseline_code,'baseline_parameters_frozen':True,
        'targets':cfg['targets'],'min_samples':cfg['input']['min_samples'],'tiny_sample_mode':len(y)<8,'numeric_scaling':'per_training_fit_MinMax',
        'targets_scope':'deployment_training_and_searched_anchor_development_plus_optional_expert',
        'common_development_targets_met':None if result['cv'] is None else bool(result['cv']['mape_percent']<=cfg['targets']['mape_percent_max'] and result['cv']['r2'] is not None and result['cv']['r2']>=cfg['targets']['r2_min']),
        'categorical_encoding':'per_training_fit_onehot','attribute_types':{p['parameter_id']:('配置类型' if p['parameter_id'] in mappings else '数值') for p in specs},
        'elapsed_seconds':time.monotonic()-started,'warning':warning}
    write_json(output/'training_report.json',plain(report));write_json(output/'feature_importance.json',result['importance'] or {'status':'disabled'})
    write_json(output/'expert_grade_consistency.json',result['consistency'] or {'mode':'off','status':'disabled'})
    write_json(output/'price_attribute_scores.json',{'attributes':attributes,'records':cfg['expert']['records']})
    manifest_path=Path(str(path)+'.manifest.json');manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest.update(bundle_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),automl_explanation=native['automl_explanation']);write_json(manifest_path,manifest)
    trace.emit('v7_report_saved',selected=result['selected_name'],training=result['train'],development=development,targets_met=met,expert_consistency=result['consistency'])
    if rejected:raise ValueError('V7训练/开发或专家五档指标未达标；诊断已保存，旧运行版本未更新。')
    return native,report

def train(data,output_dir,config_path=None,progress=None,overrides=None,input_path=None):
    output=Path(output_dir);output.mkdir(parents=True,exist_ok=True);trace=TrainingTrace(output,progress)
    try:
        with trace.span('v7_pipeline'):return _train(data,output,config_path,overrides,input_path,trace)
    except BaseException:trace.emit('v7_pipeline_exception',traceback=traceback.format_exc());raise
