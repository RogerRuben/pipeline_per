# -*- coding: utf-8 -*-
"""Bilingual table -> preserved V3 -> V5 fusion/search -> two saved native scopes."""
import copy
import hashlib
import json
import pickle
import time
from pathlib import Path
import numpy as np
from threadpoolctl import threadpool_limits
from .product_price_optuna_v5 import load_config
from .product_price_optuna_v2 import workbook_config,dataset,training_source_rows,training_split,expert_rating,LABELS
from .product_optuna_v4_adapter import write_csv
from tools.product_price_automl import write_json,native_field_metadata
from services.price_service.optuna_v2 import price_automl_core as core
from services.price_service.optuna_v2.typed_features import apply_types,restore_configurations
from services.price_service.optuna_v4.splits import holdout_indices
from services.price_service.optuna_v4.metrics import metrics,violation
from services.price_service.optuna_v4.native_member import NativeConfigurationMember,predict_saved
from services.price_service.optuna_v5.engine import run,pack
from services.price_service.optuna_v5.fusion import dominates
from services.price_service.optuna_v5.review import review
from services.price_service.optuna_v5.persistence import save_state
from services.price_service.optuna_v5.trace import TrainingTrace,fit_scope
from services.price_service.export_native_price_bundle import export_from_notebook
from services.price_service.native_bundle import load_bundle,save_bundle,predict,predict_batch

ROOT=Path(__file__).resolve().parents[1]


def _train(data,output_dir,config_path=None,progress=None,overrides=None,input_path=None,trace=None):
    started=time.monotonic();progress=progress or (lambda text:print(text,flush=True));output=Path(output_dir);output.mkdir(parents=True,exist_ok=True)
    cfg=workbook_config(input_path,load_config(config_path,overrides),{p['parameter_id'] for p in data['parameters']},loader=load_config);data=copy.deepcopy(data)
    fixed=cfg['v5']['protocol']=='fixed_cv'
    if trace:
        trace.heartbeat_seconds=cfg['v5']['logging']['heartbeat_seconds']
        trace.emit('effective_settings',config=cfg,protocol=cfg['v5']['protocol'])
    if input_path:
        from app.product_table import product_table_rows
        source=Path(input_path);restore_configurations(data,product_table_rows(source.name,source.read_bytes(),require_dual_header=True),cfg['expert']['field_types'],cfg['input']['missing_tokens'])
    apply_types(data,cfg['expert']['field_types']);frame,y,specs,mappings,groups,ids=dataset(data,cfg);rows=training_source_rows(data,ids,input_path)
    from .product_price_optuna_v4 import load_config as v4_config
    hold_cfg=v4_config(overrides={'input':cfg['input'],'v4':{'evaluation':cfg['v5']['evaluation']}})
    dev_indices,outer=holdout_indices(len(y),ids,groups,hold_cfg)
    if len(outer) and cfg['v5']['evaluation']['require_declared_types'] and set(frame.columns)-set(cfg['expert']['field_types']):raise ValueError('V5固定外层留出必须声明全部属性类型。')
    dev=frame.iloc[dev_indices].reset_index(drop=True);target=y[dev_indices];dev_rows=rows[dev_indices];dev_ids=[ids[i] for i in dev_indices]
    ratio,split_report=training_split(len(dev),cfg);write_json(output/'requested_config.json',cfg);cfg=copy.deepcopy(cfg);cfg['validation']['test_size']=ratio
    write_json(output/'effective_config.json',cfg);write_json(output/'validation_split.json',split_report)
    ratings={p['parameter_id']:expert_rating(p,cfg)[0] for p in data['parameters']};expert={name:ratings[name] if cfg['expert']['enabled'] else 1. for name in dev.columns}
    category_labels={key:{float(code):label for label,code in values.items()} for key,values in mappings.items()}
    checkpoint=cfg['v5']['persistence']['directory'];checkpoint=Path(checkpoint) if checkpoint else ROOT/'state/v5'
    if not checkpoint.is_absolute():checkpoint=ROOT/checkpoint
    source_sha=hashlib.sha256(Path(input_path).read_bytes()).hexdigest() if input_path else None
    if trace:trace.emit('input_prepared',development_rows=dev_rows,holdout_rows=rows[outer],features=list(frame.columns),expert_scores=expert)
    progress('V5 %s：%d条开发记录、%d属性、%d条固定外层留出。先保留完整V3基线。'%(cfg['v5']['mode'],len(dev),len(specs),len(outer)))
    write_json(output/'budget_plan.json',{'baseline_configuration_trials':cfg['search']['n_trials'],'baseline_fit_upper_bound':6*cfg['search']['n_trials']+12,
        'additional_member_fit_limit':cfg['v5']['enhancement']['max_new_fits'],'audit_fit_upper_bound':cfg['v5']['audit']['max_members']*cfg['v5']['audit']['max_splits'] if cfg['v5']['audit']['enabled'] else 0,
        'total_member_fit_limit':cfg['v5']['budget']['max_member_fits'],'fusion_uses_existing_predictions':True})
    with threadpool_limits(limits=1):
        if fixed:
            from services.price_service.optuna_v5.fixed_cv import run as fixed_run
            result=fixed_run(dev,target,dev_rows,expert,mappings,category_labels,cfg,output,checkpoint,progress,source_sha,trace)
            selected=result['selected'];audit={k:x for k,x in result['fixed_cv'].items() if k not in ('validation_indices','validation_prediction')}
        else:
            progress('旧版单划分协议：开发标签参与融合权重拟合，高分不代表跨划分能力。')
            result=run(dev,target,dev_rows,expert,mappings,category_labels,cfg,output,checkpoint,progress,source_sha,trace)
            selected,audit=review(result,dev,target,dev_rows,expert,mappings,category_labels,cfg,progress)
        reference=result['reference'];state=result['state'];reference_pack=pack(reference);snapshot=pack(selected)
        if not fixed and cfg['v5']['mode']=='goal_first' and reference['summary']['qualified'] and (not selected['summary']['qualified'] or not dominates(selected['summary'],reference['summary'])):
            selected=copy.deepcopy(reference);snapshot=reference_pack;progress('V5导出前检查：使用保留V3基线，拒绝开发指标回退。')
        base_full=state['baseline']['full'];full_models=[];active=np.flatnonzero(selected['weights']>0)
        cache=state.setdefault('full_models',{})
        for j in active:
            entry=selected['entries'][j];family=entry['family'];key=json.dumps(entry['parameters'],sort_keys=True)
            if entry.get('baseline_family') and selected['seed']==reference['seed'] and family in base_full['models']:
                one=np.zeros(len(core.MODEL_NAMES));one[core.MODEL_NAMES.index(family)]=1.
                model=dict(base_full,models={family:base_full['models'][family]},weights=one,params=entry['parameters'])
            elif key in cache:model=cache[key]
            else:
                with fit_scope(trace,'full_refit',family=family,parameters=entry['parameters'],train_rows=dev_rows.tolist()):
                    model=result['fitter'].fit(dev,target,entry['parameters'],expert)
                state['full_fits']+=1;cache[key]=model
            full_models.append(model)
        full=dict(snapshot,configurations=full_models)
        snapshot_prediction=predict_saved(snapshot,dev);full_prediction=predict_saved(full,dev)
        snapshot_train=metrics(target[selected['train']],snapshot_prediction[selected['train']]);snapshot_dev=metrics(target[selected['validation']],snapshot_prediction[selected['validation']])
        full_train=metrics(target,full_prediction)
        fitted=full if cfg['deployment']['model_scope']=='full_data' else snapshot
        fitted_train=full_train if cfg['deployment']['model_scope']=='full_data' else snapshot_train
        holdout_score=metrics(y[outer],predict_saved(fitted,frame.iloc[outer])) if len(outer) else None
        if trace:trace.emit('deployment_scope_scores',snapshot_train=snapshot_train,snapshot_split_development=snapshot_dev,full_refit_training=full_train,holdout=holdout_score,scope=cfg['deployment']['model_scope'])
    for name,value in [('selected_split_bundle.pkl',snapshot),('price_ensemble_bundle.pkl',full),('v3_reference_bundle.pkl',reference_pack)]:
        with (output/name).open('wb') as handle:pickle.dump(value,handle,protocol=4)
    reloaded=pickle.loads((output/'selected_split_bundle.pkl').read_bytes())
    if not np.allclose(predict_saved(reloaded,dev),snapshot_prediction,rtol=1e-12,atol=1e-12):raise AssertionError('V5获分快照复载预测发生变化。')
    write_csv(output/'selected_split_predictions.csv',[{'record_id':dev_ids[i],'source_excel_row':int(dev_rows[i]),'actual_price':float(target[i]),'predicted_price':float(snapshot_prediction[i])} for i in selected['validation']])
    write_csv(output/'selected_train_predictions.csv',[{'record_id':dev_ids[i],'source_excel_row':int(dev_rows[i]),'actual_price':float(target[i]),'predicted_price':float(snapshot_prediction[i])} for i in selected['train']])
    v=cfg['v5'];g=v['train_gate'];main_dev=audit['summary']['validation'] if (fixed or v['mode']=='robust') and audit.get('status')=='completed' else snapshot_dev
    if fixed:
        held=result['fixed_cv'];ii=held['validation_indices'];pp=held['validation_prediction']
        write_csv(output/'fixed_cv_predictions.csv',[{'record_id':dev_ids[int(i)],'source_excel_row':int(dev_rows[int(i)]),'actual_price':float(target[int(i)]),'predicted_price':float(p),'ape_percent':float(abs(target[int(i)]-p)/target[int(i)]*100.)} for i,p in zip(ii,pp)])
    met=violation(main_dev,cfg['targets']['mape_percent_max'],cfg['targets']['r2_min'])==0
    if v['mode']=='robust' and audit.get('status')!='completed':met=False
    if g['mode']!='off':met=met and violation(fitted_train,g['mape_percent_max'],g['r2_min'])==0
    if fixed:met=met and audit['summary']['qualified']
    quality='qualified_development' if met else 'unqualified_trial_draft'
    warning='V5已保留V3基线；所选开发成绩参与了搜索与融合，不是独立测试成绩。当前%s，%s。'%('全部开发记录重拟合' if cfg['deployment']['model_scope']=='full_data' else '运行实际获分开发快照','开发目标达标' if met else '未达标试用草稿')
    if v['mode']=='goal_first':warning+=' 跨划分复核单独报告，不用于替换高分冠军。'
    else:warning+=' 当前为%s模式，复核参与了选择。'%v['mode']
    if fixed:warning='V5.1固定CV：每个候选在同一组覆盖全部开发记录的划分上比较，融合权重固定，单划分成绩仅供对照。当前%s，%s。跨折成绩参与参数选择，不是独立测试成绩。V3起点也使用过开发标签，需固定外层留出或新数据评估泛化。'%('全量开发记录重拟合' if cfg['deployment']['model_scope']=='full_data' else '原划分参数快照','固定CV及训练门槛达标' if met else '未达标试用草稿')
    if len(outer):warning+=' 固定外层留出未进入调参、融合或重拟合。'
    if g['mode']!='off' and reference['summary']['train_violation']>0:warning+=' 原V3冠军未通过当前训练门槛，参考原样保留；当前候选按新门槛筛选。'
    dev_groups=groups[dev_indices];overlap=set(dev_groups[selected['train']])&set(dev_groups[selected['validation']])
    if overlap:warning+=' V3兼容划分中存在相同输入记录跨训练/开发侧，不能据此证明新成品泛化。'
    if state['baseline']['trials_completed']<cfg['search']['n_trials']:warning+=' 基线提前结束：实际%d/%d轮，不代表完整预算复现。'%(state['baseline']['trials_completed'],cfg['search']['n_trials'])
    labels={p['parameter_id']:p['label'] for p in specs};attributes=[]
    for p in data['parameters']:
        name=p['parameter_id'];x=expert.get(name,1.);mult={'lasso':.5+(x-1)/4*2.5,'ridge':.3+(x-1)/4*4.7,'svr':.5+(x-1)/4*2.}
        attributes.append({'field_id':name,'label':p['label'],'score':ratings[name],'specified':name in cfg['expert']['scores'],'enabled':cfg['expert']['enabled'],
            'used_in_training':name in frame.columns,'score_source':expert_rating(p,cfg)[1],'default_score':cfg['expert']['default_score'],'model_multipliers':mult,
            'multiplier_display':'稀疏 %.3f；岭回归 %.3f；支持向量 %.3f'%(mult['lasso'],mult['ridge'],mult['svr'])})
    metadata=native_field_metadata(frame,specs,mappings)
    for field in metadata.values():field.update(required=True,missing_policy='reject')
    product=data['products'][0]
    def export(value,path,scope):
        models={};ledger=[]
        scope_train=full_train if scope=='full_data' else snapshot_train
        scope_met=violation(main_dev,cfg['targets']['mape_percent_max'],cfg['targets']['r2_min'])==0 and (g['mode']=='off' or violation(scope_train,g['mape_percent_max'],g['r2_min'])==0)
        if v['mode']=='robust' and audit.get('status')!='completed':scope_met=False
        if fixed:scope_met=scope_met and audit['summary']['qualified']
        scope_warning=warning.replace('当前全部开发记录重拟合','当前运行实际获分开发快照' if scope=='selected_split' else '当前全部开发记录重拟合').replace('当前运行实际获分开发快照','当前全部开发记录重拟合' if scope=='full_data' else '当前运行实际获分开发快照')
        scope_warning=scope_warning.replace('开发目标达标' if met else '未达标试用草稿','开发目标达标' if scope_met else '未达标试用草稿')
        for j,(bundle,weight,entry) in enumerate(zip(value['configurations'],value['weights'],value['entries'])):
            name='member_%d'%(j+1);models[name]=NativeConfigurationMember(bundle,list(frame.columns),labels)
            ledger.append({'name':name,'configuration':name,'label':LABELS[entry['family']]+'（%d）'%(j+1),'family':'ensemble','weight':float(weight),
                           'seed_instances':1,'model_seed':int(entry['parameters'].get('model_seed',0)),'parameters':entry['parameters']})
        native=export_from_notebook(dict(models,X_train=dev,price_scaler=None),output=path,product_code=product['product_code'],product_name=product['product_name'],
            model_version='optuna-v5-'+v['mode']+'-1.0',target_divisor_to_wan=1.,field_metadata=metadata,model_variables={k:k for k in models},
            ensemble_model_names=list(models),ensemble_weights=value['weights'],strict=True,model_source='namespace',model_output_transform='direct')
        residual=np.abs(target[result['fixed_cv']['validation_indices']]-result['fixed_cv']['validation_prediction']) if fixed else np.abs(target[selected['validation']]-snapshot_prediction[selected['validation']])
        native['residual_calibration']={'space':'native','half_width_native':float(np.quantile(residual,.95)),'coverage':None,'source':'fixed_cv_development_uncalibrated' if fixed else 'searched_split_uncalibrated'}
        native['automl_explanation']={'member_ledger':ledger,'expert_groups':[],'engineering':{},'field_labels':labels,'price_attribute_scores':attributes,
            'protocol':v['protocol'],'weight_policy':v['weight_policy'] if fixed else 'validation_adaptive_legacy','weights_fitted_on_validation':not fixed,
            'expert_scores_enabled':cfg['expert']['enabled'],'expert_records':cfg['expert']['records'],'calibration':1.,'sample_warning':scope_warning,'evidence_sufficient':False,
            'evaluation_scope':'fixed_cross_validation_development' if fixed else 'searched_cross_partition_development' if v['mode']=='robust' else 'selected_searched_split_development','deployment_scope':scope,
            'quality_status':'qualified_development' if scope_met else 'unqualified_trial_draft','training_metrics':scope_train,'development_metrics':main_dev,
            'holdout_metrics':holdout_score if scope==cfg['deployment']['model_scope'] else None,'reference_metrics':reference['summary']['validation'],
            'reference_training_metrics':reference['summary']['train'],
            'reference_cv_metrics':audit.get('reference_summary',{}).get('validation'),'reference_evaluation_scope':'original_v3_searched_split',
            'interval_note':'所选开发划分残差区间，未通过独立覆盖率校准'}
        with fit_scope(trace,'native_export',path=str(path),scope=scope):save_bundle(path,native)
        manifest_path=Path(str(path)+'.manifest.json');manifest=json.loads(manifest_path.read_text(encoding='utf-8'));manifest.update(bundle_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),automl_explanation=native['automl_explanation']);write_json(manifest_path,manifest)
        return native,ledger
    saved_snapshot,snapshot_ledger=export(snapshot,output/'selected_snapshot_native.pkl','selected_split')
    saved_full,full_ledger=export(full,output/'full_refit_native.pkl','full_data')
    chosen_native=saved_full if cfg['deployment']['model_scope']=='full_data' else saved_snapshot
    import shutil
    selected_native=output/('full_refit_native.pkl' if cfg['deployment']['model_scope']=='full_data' else 'selected_snapshot_native.pkl')
    path=output/'price_native_bundle.pkl';shutil.copyfile(str(selected_native),str(path));shutil.copyfile(str(selected_native)+'.manifest.json',str(path)+'.manifest.json')
    restored=load_bundle(path);lookup={r['agreement_id']:r['params'] for r in data['agreements']};served=predict_batch(restored,[lookup[i] for i in ids]);expected=predict_saved(fitted,frame)
    delta=max(abs(r['predicted_price_wan']-float(p)) for r,p in zip(served,expected))
    if delta>1e-5:raise AssertionError('V5原生导出与获分模型不一致：'+str(delta))
    for i in range(min(3,len(ids))):
        if predict(restored,lookup[ids[i]])['predicted_price_wan']!=served[i]['predicted_price_wan']:raise AssertionError('V5单条与批量预测不一致。')
    rejected=not met and (v['publish']['policy']=='require_targets' or cfg['targets']['hard_gate'])
    total=state['baseline']['search_fits']+state['baseline']['export_fits']+state['new_fits']+state['audit_fits']+state['full_fits']+state.get('snapshot_fits',0)
    no_regression=dominates(audit['summary'],audit['reference_summary']) if fixed else dominates(selected['summary'],reference['summary'])
    if not fixed and v['mode']=='goal_first' and reference['summary']['qualified'] and (not selected['summary']['qualified'] or not no_regression):raise AssertionError('V5不应替换合格基线为更差模型。')
    baseline=state['baseline'];ledger=full_ledger if cfg['deployment']['model_scope']=='full_data' else snapshot_ledger
    report={'engine':'optuna_v5','implementation_version':'optuna-v5.1-1.0','protocol':v['protocol'],'weight_policy':v['weight_policy'] if fixed else 'validation_adaptive_legacy','weights_fitted_on_validation':not fixed,'search_mode':v['mode'],'selected_model':'固定CV与冻结权重的模型族替换' if fixed else 'V3保留基线与V5预测库融合','training_rows':len(frame),'development_rows':len(dev),
        'feature_count':len(specs),'feature_order':list(frame.columns),'deployment_scope':cfg['deployment']['model_scope'],'full_data_deployment':cfg['deployment']['model_scope']=='full_data',
        'train_metrics':audit['summary']['train'] if fixed else selected['summary']['train'],'deployed_training_metrics':fitted_train,'full_refit_training_metrics':full_train,'selected_snapshot_development_metrics':snapshot_dev,
        'development_metrics':main_dev,'reference_metrics':reference['summary'],'baseline_source':baseline['source'],'baseline_trials_completed':baseline['trials_completed'],
        'baseline_complete':bool(baseline['trials_completed']>=cfg['search']['n_trials']),
        'baseline_selected_parameters':baseline['snapshot']['params'],'baseline_ensemble_weights':np.asarray(baseline['snapshot']['weights']).tolist(),
        'baseline_non_regression':bool(no_regression),'baseline_training_feasible':bool(reference['summary']['qualified'] or reference['summary']['train_violation']==0),
        'baseline_preserved':True,'audit_used_for_selection':audit.get('used_for_selection',False),'cross_split_audit':audit,
        'reference_cv_metrics':audit.get('reference_summary'),'candidate_trials':state.get('cv_trials'),'fold_plan_sha256':audit.get('plan',{}).get('fold_plan_sha256'),
        'cv_metrics':{'mean_mape':main_dev['mape_percent'],'mean_r2':main_dev['r2'],'std_mape':None,'std_r2':None,'scope':'fixed_cross_validation_development' if fixed else 'searched_cross_partition_development' if v['mode']=='robust' else 'searched_split_development'},
        'holdout_metrics':dict(holdout_score,scope='locked_outer_holdout') if holdout_score else {'mape_percent':None,'r2':None,'scope':'external_test_not_provided'},
        'nested_cv_metrics':{'mean_mape':None,'mean_r2':None},'targets_met':bool(met),'quality_status':quality,'quality_gate_rejected':bool(rejected),'business_accuracy_certified':False,
        'stop_reason':result['reason'],'trials_requested':cfg['search']['n_trials'],'trials_completed':baseline['trials_completed'],'new_member_fits':state['new_fits'],
        'fit_statistics':{'baseline_search':baseline['search_fits'],'baseline_export':baseline['export_fits'],'enhancement':state['new_fits'],'audit':state['audit_fits'],'selected_snapshot':state.get('snapshot_fits',0),'full_refit':state['full_fits'],'total':total},
        'model_fits_this_session':total-result['fits_before_session'],
        'fit_budget':v['budget'],'conditional_family_search':True,'fusion_solver_calls':state['solver_calls'],'checkpoint_restored':state['resumed'],
        'checkpoint_directory':result['checkpoint_directory'],'selected_split_seed':selected['seed'],'validation_train_rows':len(selected['train']),'validation_test_rows':len(selected['validation']),
        'fit_record_ids':dev_ids if cfg['deployment']['model_scope']=='full_data' else [dev_ids[i] for i in selected['train']],
        'identical_input_groups_crossing_selected_split':len(overlap),
        'validation_record_ids':[dev_ids[i] for i in selected['validation']],'development_record_ids':dev_ids,'holdout_record_ids':[ids[i] for i in outer],'outer_holdout_used_for_selection':False,
        'member_count':len(ledger),'configuration_count':len(ledger),'member_ledger':ledger,'weights':{e['name']:e['weight'] for e in ledger},
        'expert_scores_enabled':cfg['expert']['enabled'],'train_gate':g,'targets':cfg['targets'],'native_equivalence_max_delta_wan':float(delta),'saved_reload_verified':True,
        'min_samples':cfg['input']['min_samples'],'tiny_sample_mode':len(dev)<8,'validation_split':split_report,'elapsed_seconds':round(time.monotonic()-started,2),
        'numeric_scaling':'training_only_MinMax','categorical_encoding':'training_only_onehot','attribute_types':{p['parameter_id']:('配置类型' if p['parameter_id'] in mappings else '数值') for p in specs},'warning':warning}
    write_json(output/'training_report.json',report);write_json(output/'cross_partition_audit.json',audit);write_json(output/'price_attribute_scores.json',{'attributes':attributes,'records':cfg['expert']['records']})
    write_json(output/'v3_reference_report.json',{'source':baseline['source'],'summary':reference['summary'],'parameters':baseline['snapshot']['params'],'weights':np.asarray(baseline['snapshot']['weights']).tolist()})
    state['elapsed']=result['elapsed_seconds']
    if v['persistence']['enabled']:save_state(result['checkpoint'],state,result['identity'])
    progress('V5完成：V3原划分对照开发 %.10g%%/%s；当前%s开发 %.10g%%/%s；实际fit %d，%s。'%(reference['summary']['validation']['mape_percent'],str(reference['summary']['validation']['r2']),'固定CV' if fixed else '原划分',main_dev['mape_percent'],str(main_dev['r2']),total,quality))
    if trace:trace.emit('training_report_saved',protocol=v['protocol'],report=report)
    if rejected:raise ValueError('V5未达当前模式门槛，获分快照及诊断已保存，旧运行版本保留。')
    return chosen_native,report


def train(data,output_dir,config_path=None,progress=None,overrides=None,input_path=None):
    trace=TrainingTrace(output_dir,progress)
    try:
        with trace.span('pipeline'):
            return _train(data,output_dir,config_path,trace.message,overrides,input_path,trace)
    except BaseException:
        import traceback
        trace.emit('pipeline_exception',traceback=traceback.format_exc())
        raise
