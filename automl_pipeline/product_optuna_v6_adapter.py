# -*- coding: utf-8 -*-
import copy,csv,hashlib,json,pickle,sys
from pathlib import Path
import numpy as np
from threadpoolctl import threadpool_limits
from .product_price_optuna_v6 import load_config
from .product_price_optuna_v2 import workbook_config,dataset,training_source_rows,expert_rating,LABELS
from .product_optuna_v4_adapter import write_csv
from tools.product_price_automl import write_json,native_field_metadata
from services.price_service.optuna_v2.typed_features import apply_types,restore_configurations
from services.price_service.optuna_v4.native_member import NativeConfigurationMember,predict_saved
from services.price_service.optuna_v5.trace import TrainingTrace,plain
from services.price_service.optuna_v6.engine import run
from services.price_service.export_native_price_bundle import export_from_notebook
from services.price_service.native_bundle import save_bundle,load_bundle,predict_batch,predict
ROOT=Path(__file__).resolve().parents[1]


def _train(data,output,cfg_path,overrides,input_path,trace):
    data=copy.deepcopy(data);output=Path(output)
    cfg=workbook_config(input_path,load_config(cfg_path,overrides),{p['parameter_id'] for p in data['parameters']},loader=load_config)
    trace.heartbeat_seconds=cfg['v6']['logging']['heartbeat_seconds'];trace.emit('v6_settings',config=cfg)
    if input_path:
        from app.product_table import product_table_rows
        source=Path(input_path);restore_configurations(data,product_table_rows(source.name,source.read_bytes(),require_dual_header=True),cfg['expert']['field_types'],cfg['input']['missing_tokens'])
    apply_types(data,cfg['expert']['field_types']);frame,y,specs,mappings,groups,ids=dataset(data,cfg);rows=training_source_rows(data,ids,input_path)
    if cfg['v6']['evaluation']['mode']=='locked_holdout' and cfg['v6']['evaluation']['require_declared_types'] and set(frame.columns)-set(cfg['expert']['field_types']):raise ValueError('固定留出模式须声明全部属性类型。')
    ratings={p['parameter_id']:expert_rating(p,cfg)[0] for p in data['parameters']}
    expert={name:ratings[name] if cfg['expert']['enabled'] else 1. for name in frame.columns}
    categories={key:{float(code):label for label,code in value.items()} for key,value in mappings.items()}
    checkpoint=cfg['v6']['persistence']['directory'] or str(ROOT/'state/v6');checkpoint=Path(checkpoint)
    if not checkpoint.is_absolute():checkpoint=ROOT/checkpoint
    write_json(output/'effective_config.json',cfg);write_json(output/'requested_config.json',cfg)
    trace.emit('v6_input',records=len(y),features=list(frame.columns),source_rows=rows,expert_scores=expert,
               note='专家1—5分是价格属性重要性；融合份额在本轮训练资料内部另行学习。')
    with threadpool_limits(limits=1):result=run(frame,y,rows,ids,expert,mappings,categories,cfg,output,checkpoint,trace)
    pack=result['pack'];dev=result['plan']['dev'];labels={p['parameter_id']:p['label'] for p in specs}
    source_pack=output/'price_ensemble_bundle.pkl';source_pack.write_bytes(pickle.dumps(pack,protocol=4))
    write_csv(output/'test_predictions.csv',[{'round':r['round'],'record_id':ids[int(i)],'source_excel_row':int(rows[int(i)]),
        'actual_price':float(a),'predicted_price':float(p),'ape_percent':float(abs(a-p)/a*100.),
        'model_sha256':r['model_sha256_before_test']} for r in result['outer'] for i,a,p in zip(r['validation_indices'],r['actual'],r['prediction'])])
    full_prediction=predict_saved(pack,frame.iloc[dev])
    write_csv(output/'training_predictions.csv',[{'record_id':ids[int(i)],'source_excel_row':int(rows[int(i)]),'actual_price':float(y[int(i)]),'predicted_price':float(p)} for i,p in zip(dev,full_prediction)])
    met=result['targets_met'];quality='qualified_sealed_assessment' if met else 'unqualified_trial_draft'
    scope='locked_outer_holdout' if cfg['v6']['evaluation']['mode']=='locked_holdout' else 'nested_procedure_test'
    warning='V6独立搜索，未继承V3冠军、划分或份额；训练与封存验收%s。'%('达到当前目标' if met else '未达标，当前为试用草稿')
    warning+=' 内部练习成绩参与参数和份额选择；封存价格未进入本轮选择。'
    warning+=('当前导出同一套已验收模型，留出记录未用于重拟合。' if scope=='locked_outer_holdout' else '轮换验收评估的是完整训练办法；最终模型另用全部开发记录训练，不冒充同一模型的独立测试成绩。')
    warning+=' 小样本验收可能波动，不能保证真实业务精度；反复依据本表验收结果修改方法也会引入开发偏差。'
    attributes=[]
    for p in specs:
        name=p['parameter_id'];s=ratings[name];x=expert[name]
        attributes.append({'field_id':name,'label':p['label'],'score':s,'enabled':cfg['expert']['enabled'],
            'specified':name in cfg['expert']['scores'],'used_in_training':True,'score_source':expert_rating(p,cfg)[1],
            'default_score':cfg['expert']['default_score'],'model_multipliers':{'lasso':.5+(x-1)/4*2.5,'ridge':.3+(x-1)/4*4.7,'svr':.5+(x-1)/4*2.}})
    models={};ledger=[]
    for j,(bundle,weight,entry) in enumerate(zip(pack['configurations'],pack['weights'],pack['entries'])):
        name='member_%d'%(j+1);models[name]=NativeConfigurationMember(bundle,list(frame.columns),labels)
        ledger.append({'name':name,'configuration':name,'label':('价格中位数参照' if entry['label_family']=='median' else LABELS[entry['family']])+'（%d）'%(j+1),
            'family':'ensemble','weight':float(weight),'seed_instances':1,'model_seed':entry['parameters'].get('model_seed',0),'parameters':entry['parameters'],'search_spec':entry['spec']})
    metadata=native_field_metadata(frame.iloc[dev],specs,mappings)
    for field in metadata.values():field.update(required=True,missing_policy='reject')
    product=data['products'][0];path=output/'price_native_bundle.pkl'
    with trace.span('v6_native_export',path=str(path)):
        native=export_from_notebook(dict(models,X_train=frame.iloc[dev],price_scaler=None),output=path,product_code=product['product_code'],product_name=product['product_name'],
            model_version='optuna-v6-'+cfg['v6']['mode']+'-1.0',target_divisor_to_wan=1.,field_metadata=metadata,model_variables={n:n for n in models},
            ensemble_model_names=list(models),ensemble_weights=pack['weights'],strict=True,model_source='namespace',model_output_transform='direct')
        native['residual_calibration']=dict(result['selected']['interval_calibration'])
        inner=result['selected'].get('inner_score')
        native['automl_explanation']={'member_ledger':ledger,'expert_groups':[],'engineering':{},'field_labels':labels,
            'price_attribute_scores':attributes,'expert_scores_enabled':cfg['expert']['enabled'],'expert_records':cfg['expert']['records'],
            'calibration':1.,'sample_warning':warning,'evidence_sufficient':False,'quality_status':quality,
            'protocol':'sealed_nested','evaluation_scope':scope,'deployment_scope':'full_data','training_metrics':result['train'],
            'development_metrics':inner['validation'] if inner else None,'holdout_metrics':dict(result['test'],scope=scope),
            'test_metrics':dict(result['test'],scope=scope),'weights_fitted_on_validation':False,
            'weight_policy':result['selected']['weight_source'],'interval_note':'仅用训练范围内练习残差的探索区间，未校准覆盖率；不使用封存验收答案'}
        save_bundle(path,native)
        manifest_path=Path(str(path)+'.manifest.json');manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest.update(bundle_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),automl_explanation=native['automl_explanation']);write_json(manifest_path,manifest)
    restored=load_bundle(path);lookup={r['agreement_id']:r['params'] for r in data['agreements']}
    served=predict_batch(restored,[lookup[key] for key in ids]);expected=predict_saved(pack,frame)
    delta=max(abs(item['predicted_price_wan']-float(p)) for item,p in zip(served,expected))
    if delta>1e-5:raise AssertionError('V6原生导出预测不一致。')
    for key,item in zip(ids[:3],served[:3]):
        if predict(restored,lookup[key])['predicted_price_wan']!=item['predicted_price_wan']:raise AssertionError('V6单条批量不一致。')
    rejected=not met and (cfg['targets']['hard_gate'] or cfg['v6']['publish']['policy']=='require_targets')
    report={'engine':'optuna_v6','implementation_version':'optuna-v6-1.0','protocol':'sealed_nested','search_mode':cfg['v6']['mode'],
        'training_rows':len(y),'development_rows':len(dev),'feature_count':len(specs),'feature_order':list(frame.columns),
        'selected_model':'独立目标导向搜索与训练内OOF收缩融合','deployment_scope':'full_data','full_data_deployment':True,
        'train_metrics':result['train'],'deployed_training_metrics':result['train'],'full_refit_training_metrics':result['train'],
        'test_metrics':result['test'],'development_metrics':inner['validation'] if inner else None,'selection_metrics':inner,
        'cv_metrics':{'mean_mape':result['test']['mape_percent'],'mean_r2':result['test']['r2'],'std_mape':None,'std_r2':None,'scope':scope},
        'holdout_metrics':dict(result['test'],scope=scope),'nested_cv_metrics':{'mean_mape':result['test']['mape_percent'],'mean_r2':result['test']['r2'],'scope':scope},
        'targets_met':met,'quality_status':quality,'quality_gate_rejected':rejected,'business_accuracy_certified':False,
        'fit_record_ids':[ids[int(i)] for i in dev],'holdout_record_ids':[ids[int(i)] for i in range(len(y)) if i not in set(dev)],
        'outer_holdout_used_for_selection':False,'outer_assessment_complete':True,'outer_rounds':len(result['outer']),
        'outer_results':result['outer'],'outer_training_metrics':result['outer_training'],'evaluation_plan':result['plan']['public'],
        'member_count':len(ledger),'configuration_count':len(ledger),'member_ledger':ledger,'weights':{r['name']:r['weight'] for r in ledger},
        'weight_policy':result['selected']['weight_source'],'weights_fitted_on_test':False,'v3_used_for_selection':False,
        'interval_calibration':native['residual_calibration'],'interval_uses_test_labels':False,
        'expert_scores_enabled':cfg['expert']['enabled'],'train_gate':cfg['v6']['train_gate'],'targets':cfg['targets'],
        'fit_statistics':result['fit_statistics'],'model_fits_this_session':result['fit_statistics']['new_attempts_this_session'],
        'trials_requested':cfg['search']['n_trials'],'trials_completed':result['selected']['trials'],'stop_reason':result['selected']['stop_reason'],
        'final_complete_candidates':result['selected'].get('complete_candidates',0),'final_pruned_candidates':result['selected'].get('pruned_candidates',0),
        'checkpoint_restored':result['resumed'],'checkpoint_directory':result['checkpoint_directory'],'search_identity':result['identity'],
        'native_equivalence_max_delta_wan':delta,'saved_reload_verified':True,'min_samples':cfg['input']['min_samples'],'tiny_sample_mode':len(y)<8,
        'numeric_scaling':'each_fit_training_only_MinMax','categorical_encoding':'each_fit_training_only_onehot',
        'attribute_types':{p['parameter_id']:('配置类型' if p['parameter_id'] in mappings else '数值') for p in specs},
        'elapsed_seconds':result['elapsed_seconds'],'warning':warning}
    write_json(output/'training_report.json',plain(report));write_json(output/'price_attribute_scores.json',{'attributes':attributes,'records':cfg['expert']['records']})
    trace.emit('v6_report_saved',report=report)
    if rejected:raise ValueError('V6训练/封存验收未达目标，诊断与模型已保存，旧运行版本保留。')
    return restored,report


def train(data,output_dir,config_path=None,progress=None,overrides=None,input_path=None):
    output=Path(output_dir);output.mkdir(parents=True,exist_ok=True);trace=TrainingTrace(output,progress)
    try:
        with trace.span('v6_pipeline'):return _train(data,output,config_path,overrides,input_path,trace)
    except BaseException:
        import traceback
        trace.emit('v6_pipeline_exception',traceback=traceback.format_exc());raise
