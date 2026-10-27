# -*- coding: utf-8 -*-
"""Paired, fixed-fold family replacement. Evaluation labels never fit weights."""
import copy
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import optuna

from ..optuna_v2 import price_automl_core as core
from ..optuna_v2.typed_features import TypedBackend
from ..optuna_v4.backend import Backend
from ..optuna_v4.metrics import metrics, violation
from ..optuna_v4.optimizer import ModelSeedTPE
from .engine import identity, new_parameters, pack, run as legacy_run
from .fusion import summary
from .persistence import atomic_json, load_state, save_state
from .trace import score_text, fit_scope


def make_folds(frame, rows, settings):
    """X-only group assignment: every row is held out once per repeat."""
    n = len(frame)
    groups = np.unique(np.asarray(frame.values), axis=0, return_inverse=True)[1]
    members = [np.flatnonzero(groups == g) for g in np.unique(groups)]
    maximum = min(settings['n_splits'], n // 2, len(members))
    folds = []
    for repeat in range(settings['repeats']):
        rng = np.random.RandomState(settings['seed'] + repeat)
        chosen = None
        for count in range(maximum, 1, -1):
            for _ in range(100):
                # Largest groups first; shuffled ties preserve repeat diversity.
                order = rng.permutation(len(members)).tolist()
                order.sort(key=lambda j: -len(members[j]))
                bins = [[] for _ in range(count)]
                for j in order:
                    sizes = [sum(len(members[k]) for k in b) for b in bins]
                    destination = int(rng.choice(np.flatnonzero(np.asarray(sizes) == min(sizes))))
                    bins[destination].append(j)
                candidate = [np.sort(np.concatenate([members[j] for j in b])) for b in bins if b]
                if len(candidate) == count and all(2 <= len(v) <= n - 2 for v in candidate):
                    chosen = candidate
                    break
            if chosen is not None:
                break
        if chosen is None:
            raise ValueError('固定CV无法在隔离相同输入的同时保证训练/验证各至少2条，请增加独立成品记录。')
        for validation in chosen:
            train = np.setdiff1d(np.arange(n), validation)
            assert not set(groups[train]) & set(groups[validation])
            folds.append({'id':len(folds), 'repeat':repeat, 'train':train, 'validation':validation})
    coverage = np.bincount(np.concatenate([f['validation'] for f in folds]), minlength=n)
    if not np.all(coverage == settings['repeats']):
        raise AssertionError('固定CV须覆盖全部记录，每次重复恰好留出一次。')
    serial = [{'id':f['id'], 'repeat':f['repeat'], 'train_rows':rows[f['train']].tolist(),
               'validation_rows':rows[f['validation']].tolist()} for f in folds]
    digest = hashlib.sha256(json.dumps(serial, sort_keys=True).encode('utf-8')).hexdigest()
    return folds, {'folds':serial, 'fold_count':len(folds), 'distinct_validation_rows':n,
                   'coverage_per_row':coverage.tolist(), 'group_isolation':True,
                   'uses_prices_to_choose_folds':False, 'fold_plan_sha256':digest}


def family_parameters(params, family):
    prefix = {'lasso':'lasso_', 'ridge':'ridge_', 'svr':'svr_', 'expert_tree':'tree_',
              'extratrees':'et_', 'gbdt':'gbdt_'}[family]
    value = {k:v for k,v in params.items() if k.startswith(prefix)}
    if family in ('gbdt', 'extratrees'):
        value['model_seed'] = params.get('model_seed', 0)
    return value


def member_key(params, family):
    return json.dumps([family, family_parameters(params, family)], sort_keys=True)


def one_family(params, family):
    value = dict(params)
    value.update({'weight_' + name:float(name == family) for name in core.MODEL_NAMES})
    return value


def conservative_proposal(family,strong=False):
    """Data-independent small-sample starting points before broad TPE exploration."""
    values={
        'ridge':{'ridge_alpha':10. if strong else 1.},
        'lasso':{'lasso_alpha':.1 if strong else .01},
        'svr':{'svr_kernel':'linear','svr_C':.1 if strong else 1.,'svr_epsilon':.01},
        'expert_tree':{'tree_max_depth':1 if strong else 2,'tree_min_samples_leaf':2,
                       'tree_lambda_expert':.5 if strong else .2,'tree_min_gain_split':0.},
        'extratrees':{'et_n_estimators':100,'et_max_depth':2 if strong else 3,
                      'et_min_samples_split':4,'et_max_features':1.,'model_seed':0},
        'gbdt':{'gbdt_n_estimators':100,'gbdt_max_depth':1,'gbdt_min_samples_split':4,
                'gbdt_learning_rate':.01 if strong else .03,'gbdt_subsample':1.,'model_seed':0}}
    return dict(values[family],family=family)


def combine(evaluations, weights, y, cfg):
    first = evaluations[0]['folds']
    train_y, train_p, valid_y, valid_p, details = [], [], [], [], []
    for i, fold in enumerate(first):
        for part in evaluations[1:]:
            other = part['folds'][i]
            if not np.array_equal(fold['train'], other['train']) or not np.array_equal(fold['validation'], other['validation']):
                raise ValueError('固定CV成员划分错位，禁止组合。')
        a = sum(float(w) * part['folds'][i]['train_prediction'] for w,part in zip(weights,evaluations))
        b = sum(float(w) * part['folds'][i]['validation_prediction'] for w,part in zip(weights,evaluations))
        train_y.extend(y[fold['train']]); train_p.extend(a)
        valid_y.extend(y[fold['validation']]); valid_p.extend(b)
        details.append({'split_id':fold['id'], 'train':metrics(y[fold['train']],a),
                        'validation':metrics(y[fold['validation']],b)})
    result = summary(np.asarray(train_y),np.asarray(valid_y),np.asarray(train_p),np.asarray(valid_p),cfg)
    result.update(fold_metrics=details, worst_fold_mape=max(f['validation']['mape_percent'] for f in details),
                  validation_pass_fraction=float(np.mean([violation(f['validation'],cfg['targets']['mape_percent_max'],cfg['targets']['r2_min']) == 0 for f in details])),
                  aggregation='pooled held-out predictions; every row once per repeat; no mean fold R2',
                  scope='fixed_cross_validation_development')
    if cfg['v5']['mode'] == 'robust':
        result['qualified'] = result['qualified'] and result['validation_pass_fraction'] == 1.
    result['validation_prediction'] = np.asarray(valid_p)
    return result


def public_score(score):
    return {k:v for k,v in score.items() if k != 'validation_prediction'}


def accept(candidate, incumbent, reference, cfg):
    """Paired improvement with guards anchored to the original CV reference."""
    s = cfg['v5']['cv']; a = candidate['validation']; b = incumbent['validation']
    if cfg['v5']['train_gate']['mode'] == 'hard' and candidate['train_violation'] > incumbent['train_violation'] + 1e-10:
        return False, 'training_gate_regression'
    if a['r2'] is None or b['r2'] is None:
        return False, 'undefined_pooled_r2'
    if a['mape_percent'] > b['mape_percent'] + 1e-10 or a['r2'] < b['r2'] - s['r2_tolerance']:
        return False, 'pooled_cv_regression'
    if candidate['worst_fold_mape'] > reference['worst_fold_mape'] + s['max_fold_regression_pp']:
        return False, 'worst_fold_regression'
    if a['p90_ape_percent'] > reference['validation']['p90_ape_percent'] + s['max_tail_regression_pp']:
        return False, 'tail_error_regression'
    gate=cfg['v5']['train_gate']
    if gate['mode']=='soft':
        old_loss=np.log1p(b['mape_percent'])+.1*np.log1p(max(0.,1.-b['r2']))+gate['soft_penalty']*incumbent['train_violation']
        new_loss=np.log1p(a['mape_percent'])+.1*np.log1p(max(0.,1.-a['r2']))+gate['soft_penalty']*candidate['train_violation']
        if new_loss>=old_loss:return False,'soft_training_penalty_not_improved'
    if cfg['v5']['mode']=='balanced' and a['mape_percent']+.25*candidate['worst_fold_mape'] >= b['mape_percent']+.25*incumbent['worst_fold_mape']:
        return False, 'balanced_risk_objective_not_improved'
    if cfg['v5']['mode']=='robust' and (candidate['worst_fold_mape']>incumbent['worst_fold_mape']+1e-10 or candidate['validation_pass_fraction']<incumbent['validation_pass_fraction']):
        return False, 'robust_fold_stability_regression'
    if b['mape_percent'] - a['mape_percent'] < s['min_mape_improvement_pp'] and a['r2'] - b['r2'] <= s['r2_tolerance']:
        return False, 'improvement_below_tolerance'
    return True, 'paired_fixed_cv_improvement'


def run(frame,y,rows,expert,mappings,labels,cfg,output,checkpoint_dir,progress,source_sha=None,trace=None):
    from automl_pipeline.product_price_optuna_v4 import load_config as v4_config
    output = Path(output); started = time.monotonic(); v = cfg['v5']
    folds, plan = make_folds(frame,rows,v['cv'])
    atomic_json(output/'fixed_cv_plan.json',plan)
    if trace:
        trace.emit('fixed_cv_plan', '固定%d折；覆盖%d条记录；不搜索划分种子；冻结融合权重。' % (len(folds),len(y)), **plan)
    settings = {k:copy.deepcopy(x) for k,x in cfg.items() if k != 'v5'}
    settings['v5'] = copy.deepcopy(v)
    settings['v5']['enhancement'].pop('max_new_fits',None)
    settings['v5']['enhancement'].pop('continue_after_targets',None)
    settings['search'].pop('timeout_seconds',None)
    settings['v5']['budget'].pop('max_member_fits',None)
    settings['implementation']={name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
        for name in ('engine.py','fusion.py','../optuna_v4/backend.py','../optuna_v4/metrics.py',
                     '../optuna_v2/optimizer_v3.py','../optuna_v2/optimizer.py','../optuna_v2/cached_backend.py')}
    fingerprint = hashlib.sha256((identity(frame,y,rows,expert,mappings,cfg) + json.dumps(settings,sort_keys=True) +
                                  Path(__file__).read_text(encoding='utf-8')).encode('utf-8')).hexdigest()
    directory = Path(checkpoint_dir)/('fixed_cv_'+fingerprint[:20]); checkpoint = directory/'engine'
    saved = load_state(checkpoint,fingerprint) if v['persistence']['enabled'] and v['persistence']['resume'] else None
    backend = Backend(frame,y,rows,expert,tuple(mappings),labels,512)
    backend.trace = trace
    eval_cfg = v4_config(overrides={'targets':cfg['targets'],'v4':{'train_gate':v['train_gate']}})
    if saved is None:
        legacy = copy.deepcopy(cfg)
        legacy['v5']['fusion']['enabled'] = False
        legacy['v5']['enhancement']['max_new_fits'] = 0
        # A partial warmup must not be silently treated as the requested V3 baseline.
        raw = legacy_run(frame,y,rows,expert,mappings,labels,legacy,output,checkpoint_dir,progress,source_sha,trace)
        state = raw['state']; reference = raw['reference']; baseline = state['baseline']
        families = [e['family'] for e in reference['entries']]
        if v['weight_policy'] == 'uniform_fixed':
            families = list(core.MODEL_NAMES)
            weights = np.full(len(families),1./len(families))
        else:
            weights = np.asarray(reference['weights'],float).copy()
        parameters = [one_family(baseline['snapshot']['params'],name) for name in families]
        state.update(families=families, frozen_weights=weights, cv_parameters=parameters,
                     cv_evaluations=[], cv_trials=0, cv_records=[], cv_seen=set(),
                     new_fits=0, audit_fits=0, full_fits=0, snapshot_fits=0, solver_calls=0,
                     resumed=False, cv_started_elapsed=0., selected_snapshot=None)
        before = raw['fits_before_session']
    else:
        state = saved; reference = state['reference']; baseline = state['baseline']
        state['resumed'] = True
        before = sum(state[k] for k in ('new_fits','audit_fits','full_fits','snapshot_fits')) + baseline['search_fits'] + baseline['export_fits']
        progress('V5.1恢复固定CV：%d个候选，累计新增%d次成员fit。' % (state['cv_trials'],state['new_fits']))
    families = state['families']; weights = state['frozen_weights']
    prior_elapsed=float(state.get('cv_started_elapsed',0.))
    if trace:
        trace.emit('weights_frozen', weights=dict(zip(families,weights)), policy=v['weight_policy'],
                   baseline_split=reference['summary'], scope='reference_only_not_cv_score')
    def used():
        return baseline['search_fits'] + baseline['export_fits'] + sum(state[k] for k in ('new_fits','audit_fits','full_fits','snapshot_fits'))
    def elapsed():
        return prior_elapsed + time.monotonic()-started
    def persist():
        if v['persistence']['enabled']:
            value = dict(state,cv_started_elapsed=elapsed())
            save_state(checkpoint,value,fingerprint)
    limit = v['budget']['max_member_fits']; reserve = 2*len(families)
    if not state['cv_evaluations']:
        needed = len(families)*len(folds)
        if limit is not None and used()+needed+reserve > limit:
            raise ValueError('总fit预算不足以完整评估固定CV基线并导出；需至少%d。' % (used()+needed+reserve))
        # This mandatory reference evaluation is completed even if warmup used the
        # soft time budget. An incomplete fold set is never reported as full CV.
        for family,params in zip(families,state['cv_parameters']):
            count = backend.stats['model_fits']
            try:
                part = backend.evaluate(params,folds,eval_cfg)
            finally:
                state['audit_fits'] += backend.stats['model_fits']-count
            state['cv_evaluations'].append(part)
            state['cv_seen'].add(member_key(params,family))
        state['cv_reference'] = public_score(combine(state['cv_evaluations'],weights,y,cfg))
        state['cv_current'] = copy.deepcopy(state['cv_reference'])
        state['study'] = optuna.create_study(direction='minimize',sampler=ModelSeedTPE(cfg['search']['optimizer_seed'],5,24))
        for strong in (False,True):
            for family in families:
                if family in v['enhancement']['families']:
                    proposal=conservative_proposal(family,strong)
                    if 'model_seed' in proposal:proposal['model_seed']=v['enhancement']['model_seed_low']
                    state['study'].enqueue_trial(proposal)
        persist()
    current = state['cv_current']; original = state['cv_reference']
    if trace:
        trace.emit('cv_baseline', '固定CV基线：训练%s；开发%s。' % (score_text(original['train']),score_text(original['validation'])),
                   reference=original, weights=dict(zip(families,weights)), raw_v3_split=reference['summary'])
    allowed = [f for f in families if f in v['enhancement']['families']]
    reason = 'new_fit_budget_exhausted'; duplicates = 0
    while allowed and state['new_fits'] < v['enhancement']['max_new_fits']:
        if state['cv_trials']>=max(100,2*v['enhancement']['max_new_fits']):
            reason='candidate_attempt_budget_exhausted';break
        if current['qualified'] and not v['enhancement']['continue_after_targets']:
            reason = 'fixed_cv_targets_met'; break
        if cfg['search']['timeout_seconds'] is not None and elapsed() >= cfg['search']['timeout_seconds']:
            reason = 'time_budget_exhausted'; break
        if state['new_fits']+len(folds) > v['enhancement']['max_new_fits']:
            reason = 'insufficient_budget_for_complete_candidate'; break
        if limit is not None and used()+len(folds)+reserve > limit:
            reason = 'total_fit_budget_reserved_for_export'; break
        study = state['study']
        if state.get('stagnation',0)>=v['enhancement']['patience']:
            state['family_switches']=state.get('family_switches',0)+1
            study.sampler.fixed={'family':allowed[state['family_switches']%len(allowed)]}
            if trace:trace.emit('family_switch',family=study.sampler.fixed['family'],reason='fixed_cv_stagnation',fold_plan_sha256=plan['fold_plan_sha256'])
            state['stagnation']=0
        trial = study.ask()
        family = trial.suggest_categorical('family',allowed)
        study.sampler.fixed={}
        params = new_parameters(trial,family,baseline['snapshot']['params'],cfg)
        # split_seed is an unused reference annotation: no trial may change folds.
        params['split_seed'] = reference['seed']
        key = member_key(params,family)
        if key in state['cv_seen']:
            study.tell(trial,state=optuna.trial.TrialState.FAIL); duplicates += 1
            if trace:trace.emit('candidate_skipped', candidate=trial.number,family=family,reason='duplicate_effective_parameters')
            if duplicates >= 100:reason='duplicate_space_exhausted'; break
            continue
        state['cv_seen'].add(key); duplicates = 0; state['cv_trials'] += 1
        record = {'number':trial.number,'candidate':state['cv_trials'],'family':family,
                  'parameters':family_parameters(params,family),'fold_plan_sha256':plan['fold_plan_sha256'],
                  'weights_before':dict(zip(families,weights.tolist())), 'valid':False}
        if trace:trace.emit('candidate_start', **record)
        count = backend.stats['model_fits']; began = time.monotonic(); told=False
        try:
            part = backend.evaluate(params,folds,eval_cfg)
            position = families.index(family)
            proposal = list(state['cv_evaluations']); proposal[position] = part
            candidate = combine(proposal,weights,y,cfg)
            if trace:
                for details in candidate['fold_metrics']:
                    trace.emit('candidate_fold_ensemble',candidate=state['cv_trials'],family=family,fold=details['split_id'],
                               train=details['train'],validation=details['validation'],weights=dict(zip(families,weights)))
            accepted, why = accept(candidate,current,original,cfg)
            single = combine([part],[1.],y,cfg)
            study.tell(trial,float(np.log1p(candidate['validation']['mape_percent']) +
                                   .1*np.log1p(max(0.,1.-candidate['validation']['r2'])) +
                                   (candidate['train_violation']*(v['train_gate']['soft_penalty'] if v['train_gate']['mode']=='soft' else 1.) if v['train_gate']['mode']!='off' else 0.)))
            told=True
            record.update(valid=True, accepted=accepted,reason=why, single_model=public_score(single),
                          candidate_ensemble=public_score(candidate), incumbent_before=public_score(current))
            if accepted:
                state['cv_parameters'][position]=params; state['cv_evaluations']=proposal
                current=public_score(candidate); state['cv_current']=current; state['selected_snapshot']=None
                state['stagnation']=0
            else:state['stagnation']=state.get('stagnation',0)+1
            record['best_ensemble']=public_score(current)
            if candidate['validation']['mape_percent'] < 1e-6 and trace:
                trace.emit('near_perfect_diagnostic', candidate=trial.number,scope='full_fixed_cv_development',
                           note='仅记录真实数值，不伪造误差下限；仍非独立测试成绩',
                           max_ape_percent=candidate['validation']['max_ape_percent'],
                           individual_mape=single['validation']['mape_percent'], weights_fitted_on_validation=False)
        except (ValueError,FloatingPointError,OverflowError,RuntimeError) as exc:
            if told:raise
            import traceback
            study.tell(trial,state=optuna.trial.TrialState.FAIL)
            state['stagnation']=state.get('stagnation',0)+1
            record.update(accepted=False,reason='candidate_failed',error=repr(exc),traceback=traceback.format_exc())
        finally:
            state['new_fits'] += backend.stats['model_fits']-count
        record.update(new_fits=backend.stats['model_fits']-count,cumulative_new_fits=state['new_fits'],
                      seconds=time.monotonic()-began,weights_after=dict(zip(families,weights.tolist())))
        if record['weights_before'] != record['weights_after']:
            raise AssertionError('固定权重协议禁止使用开发标签修改融合权重。')
        with (output/'enhancement_trials.jsonl').open('a',encoding='utf-8') as handle:
            handle.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
        state['cv_records'].append(record)
        if trace:
            trace.emit('candidate_end', '候选%d %s：单模型训练%s、开发%s；集成训练%s、开发%s；优胜训练%s、开发%s；%s。' % (
                state['cv_trials'],family,score_text(record.get('single_model',{}).get('train')),score_text(record.get('single_model',{}).get('validation')),
                score_text(record.get('candidate_ensemble',{}).get('train')),
                score_text(record.get('candidate_ensemble',{}).get('validation')),
                score_text(current['train']),score_text(current['validation']),record['reason']), **record)
        persist()
    if not allowed:reason='no_active_family_in_requested_search'
    if v['enhancement']['max_new_fits']==0:reason='fixed_cv_reference_only'
    # Only the selected configuration is fitted on the reference split for a
    # separately labelled snapshot. No split score participates in acceptance.
    fitter = Backend(frame,y,rows,expert,tuple(mappings),labels,0); fitter.trace=trace
    if state['selected_snapshot'] is None:
        entries = []
        for family,params in zip(families,state['cv_parameters']):
            old = next((e for e in reference['entries'] if e['family']==family),None)
            if old is not None and member_key(params,family)==member_key(old['parameters'],family):
                entry = copy.deepcopy(old)
            else:
                with fit_scope(trace,'selected_snapshot_fit',family=family,train_rows=rows[reference['train']].tolist()):
                    bundle=fitter.fit(frame.iloc[reference['train']],y[reference['train']],params,expert)
                state['snapshot_fits'] += 1
                a,_=TypedBackend.predict(bundle,frame.iloc[reference['train']]); b,_=TypedBackend.predict(bundle,frame.iloc[reference['validation']])
                entry={'name':'fixed_cv_'+family,'family':family,'parameters':params,'bundle':bundle,
                       'train':a,'validation':b,'pinned':True}
            entries.append(entry)
        selected=dict(reference,entries=entries,weights=weights.copy())
        T=np.column_stack([e['train'] for e in entries]); V=np.column_stack([e['validation'] for e in entries])
        selected['summary']=summary(y[selected['train']],y[selected['validation']],T.dot(weights),V.dot(weights),cfg)
        state['selected_snapshot']=selected
    selected=state['selected_snapshot']; state['elapsed']=elapsed(); state['cv_started_elapsed']=elapsed(); persist()
    current_prediction=combine(state['cv_evaluations'],weights,y,cfg)['validation_prediction']
    validation_indices=np.concatenate([f['validation'] for f in folds])
    atomic_json(output/'fixed_cv_selection.json',dict(reference=original,selected=current,
                weights=dict(zip(families,weights)),weights_fitted_on_validation=False,fold_plan=plan,
                candidate_trials=state['cv_trials'],stop_reason=reason))
    if trace:trace.emit('search_stop',reason=reason,summary=current,new_fits=state['new_fits'],total_fits=used())
    return {'state':state,'selected':selected,'reference':reference,'reason':reason,
            'elapsed_seconds':elapsed(),'identity':fingerprint,'checkpoint':checkpoint,
            'checkpoint_directory':str(directory),'used_fits':used(),'fits_before_session':before,
            'fitter':fitter,'fixed_cv':{'status':'completed','summary':current,'reference_summary':original,
                'used_for_selection':True,'weights_changed_by_audit':False,'weights_fitted_on_validation':False,
                'scope':'fixed_cross_validation_development','plan':plan,
                'validation_indices':validation_indices,'validation_prediction':current_prediction}}
