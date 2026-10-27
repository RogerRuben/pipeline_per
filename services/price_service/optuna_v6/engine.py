# -*- coding: utf-8 -*-
"""Sealed outer evaluation of the entire inner training/ensemble procedure."""
import copy,hashlib,json,time
from pathlib import Path
import numpy as np
from ..optuna_v4.metrics import metrics,violation
from ..optuna_v4.native_member import predict_saved
from ..optuna_v5.persistence import save_state,load_state,atomic_json
from ..optuna_v5.trace import plain,score_text
from .backend import Backend,FitLedger,BudgetStop
from .protocol import make_plan,serialize
from .search import train_stage


def run(frame,y,rows,ids,expert,mappings,labels,cfg,output,checkpoint_root,trace):
    output=Path(output);v=cfg['v6'];plan=make_plan(frame,rows,ids,cfg)
    atomic_json(output/'evaluation_plan.json',plan['public'])
    trace.emit('v6_plan_sealed',plan=plan['public'],note='计划在任何模型拟合前确定，不搜索验收划分，不继承V3冠军。')
    identity_cfg=copy.deepcopy(cfg);identity_cfg['search'].pop('timeout_seconds',None)
    identity_cfg['v6']['persistence'].pop('directory',None);identity_cfg['v6']['persistence'].pop('resume',None)
    code={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')}
    code.update({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__).parent.parent/'optuna_v2/price_automl_core.py',Path(__file__).parent.parent/'optuna_v2/typed_features.py']})
    identity=hashlib.sha256(frame.values.tobytes()+np.asarray(y,float).tobytes()+np.asarray(rows,int).tobytes()+
        json.dumps([list(frame.columns),expert,mappings,identity_cfg,code,plan['public']],sort_keys=True).encode('utf-8')).hexdigest()
    root=Path(checkpoint_root)/identity[:24] if v['persistence']['enabled'] else output/'transient_search'
    root.mkdir(parents=True,exist_ok=True)
    state=load_state(root/'job',identity) if v['persistence']['enabled'] and v['persistence']['resume'] else None
    state=state or {'outer':[],'elapsed_seconds':0.,'completed':False}
    before_lines=(root/'fit_ledger.jsonl').read_text(encoding='utf-8').splitlines() if (root/'fit_ledger.jsonl').is_file() else []
    fits_before=sum(json.loads(line)['event']=='fit_start' for line in before_lines if line.strip())
    ledger=FitLedger(root/'fit_ledger.jsonl',v['budget']['max_member_fits'],trace,cfg['search']['timeout_seconds'],state['elapsed_seconds'])
    backend=Backend(frame,y,rows,expert,mappings,labels,ledger,v['budget']['cache_entries'],trace)
    locked=v['evaluation']['mode']=='locked_holdout'
    stages=len(plan['outer'])+(0 if locked else 1);cap=v['budget']['max_member_fits']//stages
    reserve=v['selection']['finalists']*min(v['ensemble']['max_members'],3)
    minimum=max([2*len(f)+reserve for f in plan['inner']+([] if locked else [plan['final']])])
    if cap<minimum:raise ValueError('总fit预算至少需要%d，才能为每轮的基础候选、验收及最终重拟合保留额度。'%(minimum*stages))
    trace.emit('v6_resource_plan',total_fit_limit=ledger.limit,stage_count=stages,per_stage_fit_cap=cap,
               configurations_per_stage=cfg['search']['n_trials'],tiny_configurations=v['budget']['tiny_trials'],
               resumed=bool(state['outer']),fits_before_session=fits_before)
    def persist():
        state['elapsed_seconds']=ledger.elapsed()
        if v['persistence']['enabled']:save_state(root/'job',state,identity)
    locked_selected=None
    try:
        for index,fold in enumerate(plan['outer']):
            stage='outer_%03d'%index
            if index<len(state['outer']):
                trace.emit('v6_outer_restored',stage=stage,record=state['outer'][index]);continue
            if ledger.expired():raise BudgetStop('验收尚未全部完成，时间预算到达，保留断点和旧运行模型。')
            trace.emit('v6_outer_sealed',stage=stage,allowed_train_rows=rows[fold['train']],sealed_test_rows=rows[fold['validation']],
                       note='本轮内部搜索只使用allowed_train_rows；外层价格在模型和份额确定后用于评分。')
            selected=train_stage(backend,fold['train'],plan['inner'][index],stage,cap,cfg,root,identity,trace)
            if locked:locked_selected=selected
            pack=selected['pack'];frozen=hashlib.sha256(__import__('pickle').dumps(pack,protocol=4)).hexdigest()
            pred=predict_saved(pack,frame.iloc[fold['validation']]);train_pred=predict_saved(pack,frame.iloc[fold['train']])
            score=metrics(y[fold['validation']],pred);train_score=metrics(y[fold['train']],train_pred)
            if frozen!=hashlib.sha256(__import__('pickle').dumps(pack,protocol=4)).hexdigest():raise AssertionError('验收预测不得修改模型或份额。')
            (output/'assessment_models').mkdir(exist_ok=True)
            (output/'assessment_models'/('%s.pkl'%stage)).write_bytes(__import__('pickle').dumps(pack,protocol=4))
            record={'round':index,'repeat':fold.get('repeat',0),'train_rows':rows[fold['train']].tolist(),'validation_rows':rows[fold['validation']].tolist(),
                    'train_indices':fold['train'].tolist(),'validation_indices':fold['validation'].tolist(),
                    'actual':y[fold['validation']].tolist(),'prediction':pred.tolist(),'train_actual':y[fold['train']].tolist(),'train_prediction':train_pred.tolist(),
                    'train_metrics':train_score,'test_metrics':score,'selected_specs':selected['selected_specs'],'weights':pack['weights'],
                    'weight_source':selected['weight_source'],'inner_selection_metrics':selected['score'],'trials':selected['trials'],
                    'complete_candidates':selected.get('complete_candidates',0),'pruned_candidates':selected.get('pruned_candidates',0),
                    'stop_reason':selected['stop_reason'],'model_sha256_before_test':frozen,'test_labels_used_for_training':False}
            state['outer'].append(record);persist()
            atomic_json(output/('outer_%03d_report.json'%index),plain(record))
            trace.emit('v6_outer_result','%s：训练%s；封存验收%s。'%(stage,score_text(train_score),score_text(score)),**record)
            if score['mape_percent']<1e-6:trace.emit('v6_near_perfect_audit',stage=stage,actual=record['actual'],prediction=record['prediction'],
                note='保留真实数值，不设置虚假误差下限；核对封存边界与目标代理属性。',train_rows=record['train_rows'],test_rows=record['validation_rows'])
        # No outer metric is read to choose or seed final candidates.
        trace.emit('v6_final_training_start',train_rows=rows[plan['dev']],note='按事先相同规则全量开发训练，外层成绩不回传选参或权重。')
        if locked:
            f=plan['outer'][0]
            final=locked_selected if locked_selected is not None else train_stage(backend,f['train'],plan['inner'][0],'outer_000',cap,cfg,root,identity,trace)
            trace.emit('v6_locked_export_same_model',note='直接导出本轮已验收的同一模型，不再重新选参。')
        else:final=train_stage(backend,plan['dev'],plan['final'],'final',cap,cfg,root,identity,trace)
        prediction=predict_saved(final['pack'],frame.iloc[plan['dev']]);training=metrics(y[plan['dev']],prediction)
        test=metrics(np.concatenate([r['actual'] for r in state['outer']]),np.concatenate([r['prediction'] for r in state['outer']]))
        g=v['train_gate'];met=violation(test,cfg['targets']['mape_percent_max'],cfg['targets']['r2_min'])==0
        if g['mode']!='off':met=met and violation(training,g['mape_percent_max'],g['r2_min'])==0
        if v['mode']=='robust':
            met=met and all(r['test_metrics']['mape_percent']<=cfg['targets']['mape_percent_max'] and
                (r['test_metrics']['r2'] is None or r['test_metrics']['r2']>=cfg['targets']['r2_min']) for r in state['outer'])
        outer_training=metrics(np.concatenate([r['train_actual'] for r in state['outer']]),np.concatenate([r['train_prediction'] for r in state['outer']]))
        state.update(completed=True,targets_met=bool(met));persist()
        trace.emit('v6_evaluation_complete',train=training,test=test,targets_met=bool(met),outer_rounds=len(state['outer']),
                   unique_test_records=len(set(i for r in state['outer'] for i in r['validation_indices'])),fit_attempts=ledger.used(),successful_fits=ledger.completed)
        return {'pack':final['pack'],'selected':final,'train':training,'test':test,'outer_training':outer_training,
                'targets_met':bool(met),'outer':state['outer'],'plan':plan,'checkpoint_directory':str(root),
                'fit_statistics':{'total_attempts':ledger.used(),'successful_fits':ledger.completed,'cache_hits_this_session':ledger.cache_hits,
                                  'new_attempts_this_session':ledger.used()-fits_before,'per_stage_fit_cap':cap},
                'resumed':fits_before>0,'elapsed_seconds':ledger.elapsed(),'identity':identity}
    except BaseException:
        persist();atomic_json(output/'incomplete_assessment.json',{'status':'incomplete','completed_outer_rounds':len(state['outer']),
            'expected_outer_rounds':len(plan['outer']),'fit_attempts':ledger.used(),'checkpoint_directory':str(root),
            'targets_met':False,'reason':'详见training_events.jsonl；完整验收前不覆盖运行版本。'})
        raise
