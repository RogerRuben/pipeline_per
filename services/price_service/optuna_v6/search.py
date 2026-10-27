# -*- coding: utf-8 -*-
"""Independent inner search. Only complete, common-fold candidates enter mixtures."""
import copy,hashlib,json,time,traceback
from pathlib import Path
import numpy as np
import optuna
from ..optuna_v4.optimizer import ModelSeedTPE
from ..optuna_v4.metrics import metrics,violation
from ..optuna_v4.native_member import predict_saved
from ..optuna_v5.persistence import save_state,load_state
from ..optuna_v5.trace import score_text
from .backend import native_family,BudgetStop
from .blend import score_parts,choose_combinations
from .protocol import rank,loss,assessment

BASE={'split_seed':0,'lasso_alpha':.001,'ridge_alpha':.01,'svr_kernel':'linear','svr_C':1.,'svr_epsilon':.001,'svr_gamma':'scale',
      'gbdt_n_estimators':100,'gbdt_max_depth':2,'gbdt_min_samples_split':2,'gbdt_learning_rate':.05,'gbdt_subsample':1.,
      'et_n_estimators':100,'et_max_depth':4,'et_min_samples_split':2,'et_max_features':1.,'model_seed':0,
      'tree_max_depth':2,'tree_min_samples_leaf':2,'tree_lambda_expert':.2,'tree_min_gain_split':0.}


def warm_specs(cfg):
    candidates=[{'family':'median'}]
    for level in range(2):
        for family in cfg['v6']['families']:
            p={'family':family,'feature_k':0 if level==0 else 4}
            values={'ridge':{'ridge_alpha':.01 if level==0 else 1.},'lasso':{'lasso_alpha':.001 if level==0 else .01},
                    'svr':{'svr_kernel':'linear','svr_C':1. if level==0 else 10.,'svr_epsilon':.001},
                    'expert_tree':{'tree_max_depth':2 if level==0 else 4,'tree_min_samples_leaf':2 if level==0 else 1,'tree_lambda_expert':.2,'tree_min_gain_split':0.},
                    'extratrees':{'et_n_estimators':100,'et_max_depth':4 if level==0 else 8,'et_min_samples_split':2,'et_max_features':1.,'model_seed':cfg['search']['seed_low']},
                    'gbdt':{'gbdt_n_estimators':100,'gbdt_max_depth':2 if level==0 else 1,'gbdt_min_samples_split':2,'gbdt_learning_rate':.05,'gbdt_subsample':1.,'model_seed':cfg['search']['seed_low']}}
            p.update(values[family]);candidates.append(p)
    return candidates


def suggest(trial,cfg):
    family=trial.suggest_categorical('family',['median']+cfg['v6']['families']);p=dict(BASE)
    feature_k=0 if family=='median' else trial.suggest_categorical('feature_k',cfg['v6']['features']['subset_sizes'])
    if family=='ridge':p['ridge_alpha']=trial.suggest_float('ridge_alpha',1e-7,1e4,log=True)
    elif family=='lasso':p['lasso_alpha']=trial.suggest_float('lasso_alpha',1e-7,1.,log=True)
    elif family=='svr':
        p['svr_kernel']=trial.suggest_categorical('svr_kernel',['linear','rbf']);p['svr_C']=trial.suggest_float('svr_C',.01,1e4,log=True)
        p['svr_epsilon']=trial.suggest_float('svr_epsilon',1e-5,.3,log=True)
        if p['svr_kernel']=='rbf':p['svr_gamma']=trial.suggest_categorical('svr_gamma',['scale','auto',.001,.01,.1,1.])
    elif family=='expert_tree':
        p['tree_max_depth']=trial.suggest_int('tree_max_depth',1,8);p['tree_min_samples_leaf']=trial.suggest_int('tree_min_samples_leaf',1,3)
        p['tree_lambda_expert']=trial.suggest_float('tree_lambda_expert',0.,2.);p['tree_min_gain_split']=trial.suggest_float('tree_min_gain_split',0.,.15)
    elif family in ('extratrees','gbdt'):
        prefix='et' if family=='extratrees' else 'gbdt';p[prefix+'_n_estimators']=trial.suggest_categorical(prefix+'_n_estimators',[50,100,200])
        p[prefix+'_max_depth']=trial.suggest_int(prefix+'_max_depth',1,10 if prefix=='et' else 4)
        p[prefix+'_min_samples_split']=trial.suggest_categorical(prefix+'_min_samples_split',[2,4,6])
        if prefix=='et':p['et_max_features']=trial.suggest_categorical('et_max_features',[.3,.5,.8,1.])
        else:p['gbdt_learning_rate']=trial.suggest_float('gbdt_learning_rate',.005,.3,log=True);p['gbdt_subsample']=trial.suggest_float('gbdt_subsample',.6,1.)
        p['model_seed']=trial.suggest_int('model_seed',cfg['search']['seed_low'],cfg['search']['seed_high'])
    return {'family':family,'params':p,'feature_k':int(feature_k)}


def spec_key(spec):
    family=spec['family'];prefix={'ridge':'ridge_','lasso':'lasso_','svr':'svr_','expert_tree':'tree_','extratrees':'et_','gbdt':'gbdt_'}.get(family,'unused')
    params={k:v for k,v in spec['params'].items() if k.startswith(prefix)}
    if family in ('extratrees','gbdt'):params['model_seed']=spec['params']['model_seed']
    return json.dumps([family,spec['feature_k'],params],sort_keys=True)


def pack_choice(combo,bundles):
    return {'format':'price-optuna-v6-1.0','configurations':bundles,'weights':combo['weights'].tolist(),
            'entries':[{'name':'member_%d'%(i+1),'family':native_family(p['spec']),'label_family':p['spec']['family'],'parameters':b['params'],'spec':p['spec']} for i,(p,b) in enumerate(zip(combo['members'],bundles))]}


def select_and_refit(pool,backend,pool_rows,stage,cap,cfg,trace):
    choices=choose_combinations(pool,backend.y,cfg,pool_rows);fitted=[]
    for combo in choices[:cfg['v6']['selection']['finalists']]:
        bundles=[backend.fit_bundle(pool_rows,p['spec'],stage,cap,pool_rows) for p in combo['members']]
        pack=pack_choice(combo,bundles);prediction=predict_saved(pack,backend.frame.iloc[pool_rows]);train=metrics(backend.y[pool_rows],prediction)
        score=copy.deepcopy(combo['score']);g=cfg['v6']['train_gate']
        score['train']=train;score['train_violation']=0. if g['mode']=='off' else violation(train,g['mape_percent_max'],g['r2_min'])
        score['qualified']=score['train_violation']==0 and score['validation_violation']==0
        item={'pack':pack,'score':score,'inner_score':combo['score'],'weight_source':combo['weight_source'],
              'selected_specs':[p['spec'] for p in combo['members']]}
        # Intervals also belong to the trained model. Never calibrate them with
        # the sealed outer answers, even when labelled exploratory.
        residual=np.concatenate([np.abs(backend.y[f['validation']]-sum(
            w*p['parts'][i]['validation_prediction'] for w,p in zip(combo['weights'],combo['members'])))
            for i,f in enumerate(combo['members'][0]['parts'])])
        item['interval_calibration']={'space':'native','half_width_native':float(np.quantile(residual,.95)),
            'coverage':None,'source':'training_inner_oof_uncalibrated_residuals'}
        fitted.append(item)
        trace.emit('v6_full_pool_check',stage=stage,train=train,inner_development=score['validation'],weights=pack['weights'],specs=item['selected_specs'],qualified=score['qualified'])
    return min(fitted,key=lambda c:rank(c['score'],cfg))


def train_stage(backend,pool_rows,folds,stage,cap,cfg,checkpoint,identity,trace):
    v=cfg['v6'];persist=v['persistence']['enabled'];p=Path(checkpoint)/stage
    state=load_state(p,identity) if persist and v['persistence']['resume'] else None
    if state and state.get('completed'):
        trace.emit('v6_stage_restored',stage=stage,completed=True,train_rows=backend.rows[pool_rows])
        return state['result']
    if not folds:
        spec={'family':v['families'][0],'params':dict(BASE),'feature_k':0}
        bundle=backend.fit_bundle(pool_rows,spec,stage,cap,pool_rows)
        pack=pack_choice({'weights':np.ones(1),'members':[{'spec':spec}]},[bundle])
        result={'pack':pack,'score':None,'inner_score':None,'weight_source':'predeclared_no_inner_groups',
                'selected_specs':[spec],'trials':0,'stop_reason':'insufficient_inner_groups_fixed_recipe'}
        residual=np.abs(backend.y[pool_rows]-predict_saved(pack,backend.frame.iloc[pool_rows]))
        result['interval_calibration']={'space':'native','half_width_native':float(np.quantile(residual,.95)),
            'coverage':None,'source':'training_residuals_no_inner_groups_uncalibrated'}
        if persist:save_state(p,{'completed':True,'result':result},identity)
        return result
    if state is None:
        study=optuna.create_study(direction='minimize',sampler=ModelSeedTPE(cfg['search']['optimizer_seed'],8,24))
        for proposal in warm_specs(cfg):
            if proposal.get('feature_k',0) not in v['features']['subset_sizes']:proposal['feature_k']=v['features']['subset_sizes'][0]
            study.enqueue_trial(proposal)
        state={'study':study,'pool':[],'records':[],'seen':set(),'screen':[],'full_families':set(),'trials':0,'attempts':0}
    study=state['study'];maximum=min(cfg['search']['n_trials'],v['budget']['tiny_trials']) if len(pool_rows)<v['evaluation']['tiny_threshold'] else cfg['search']['n_trials']
    reserve=v['selection']['finalists']*min(v['ensemble']['max_members'],3)
    stop='trial_budget_exhausted';best=None
    def checkpoint_now():
        if persist:save_state(p,state,identity)
    while state['trials']<maximum and state['attempts']<maximum*4:
        if backend.ledger.expired():checkpoint_now();raise BudgetStop('时间预算到达，保留本轮断点；尚未完成的验收不作为完整成绩。')
        if not backend.ledger.room(stage,cap,len(folds)+reserve-1):stop='stage_fit_budget_reserved_for_refit';break
        trial=study.ask();state['attempts']+=1;spec=suggest(trial,cfg);key=spec_key(spec)
        if key in state['seen']:
            study.tell(trial,state=optuna.trial.TrialState.FAIL);trace.emit('v6_candidate_skipped',stage=stage,trial=trial.number,reason='duplicate_effective_parameters');checkpoint_now();continue
        state['seen'].add(key);state['trials']+=1;started=time.monotonic();before=backend.ledger.used();told=False
        record={'stage':stage,'trial':trial.number,'spec':spec,'status':'running','train_pool_rows':backend.rows[pool_rows].tolist()}
        trace.emit('v6_candidate_start',**record)
        try:
            prefix_count=min(v['screening']['folds'],len(folds));parts=backend.evaluate(spec,folds[:prefix_count],stage,cap,pool_rows)
            prefix=score_parts([parts],[1.],backend.y,cfg);prefix_loss=loss(prefix,cfg)
            can_prune=v['screening']['enabled'] and prefix_count<len(folds) and spec['family'] in state['full_families'] and len(state['screen'])>=v['screening']['startup_candidates']
            explore=(trial.number%max(1,int(round(1./max(v['screening']['exploration_fraction'],1e-9)))))==0
            if can_prune and not explore and prefix_loss>float(np.quantile(state['screen'],v['screening']['keep_quantile'])):
                trial.report(prefix_loss,prefix_count);study.tell(trial,state=optuna.trial.TrialState.PRUNED);told=True
                record.update(status='pruned',screen_score=prefix,reason='common_prefix_screen_only_not_final_score')
            else:
                if prefix_count<len(folds):parts+=backend.evaluate(spec,folds[prefix_count:],stage,cap,pool_rows)
                score=score_parts([parts],[1.],backend.y,cfg);study.tell(trial,loss(score,cfg));told=True
                state['pool'].append({'spec':spec,'parts':parts,'score':score,'trial':trial.number});state['full_families'].add(spec['family'])
                # Bound stored predictions; comparison is per family on all folds.
                kept=[]
                for family in ['median']+v['families']:
                    kept.extend(sorted([q for q in state['pool'] if q['spec']['family']==family],key=lambda q:rank(q['score'],cfg))[:v['ensemble']['pool_per_family']])
                state['pool']=kept
                record.update(status='complete',single_model=score,reason='all_inner_folds_complete')
            state['screen'].append(prefix_loss)
        except BudgetStop:
            if not told:study.tell(trial,state=optuna.trial.TrialState.FAIL)
            checkpoint_now();raise
        except (ValueError,FloatingPointError,OverflowError,RuntimeError) as exc:
            if told:raise
            study.tell(trial,state=optuna.trial.TrialState.FAIL)
            record.update(status='failed',reason=repr(exc),traceback=traceback.format_exc())
        record.update(fits=backend.ledger.used()-before,seconds=time.monotonic()-started)
        if state['pool']:
            leader=choose_combinations(state['pool'],backend.y,cfg,pool_rows)[0]
            record.update(best_inner_ensemble=leader['score'],best_inner_weights=leader['weights'].tolist(),
                          best_inner_specs=[p['spec'] for p in leader['members']],weight_source=leader['weight_source'])
        state['records'].append(record);trace.emit('v6_candidate_end',**record);checkpoint_now()
        if state['pool'] and state['trials']>=min(v['selection']['min_trials'],maximum) and state['full_families']>=set(['median']+v['families']):
            candidates=choose_combinations(state['pool'],backend.y,cfg,pool_rows)
            if candidates[0]['score']['qualified'] and not v['selection']['continue_after_targets']:
                best=select_and_refit(state['pool'],backend,pool_rows,stage,cap,cfg,trace)
                if best['score']['qualified']:stop='training_and_inner_targets_met';break
    if not state['pool']:raise ValueError('本轮没有完整有效候选；详见候选与拟合日志。')
    if best is None or stop!='training_and_inner_targets_met':best=select_and_refit(state['pool'],backend,pool_rows,stage,cap,cfg,trace)
    best.update(trials=state['trials'],attempts=state['attempts'],stop_reason=stop,
                complete_candidates=sum(r['status']=='complete' for r in state['records']),pruned_candidates=sum(r['status']=='pruned' for r in state['records']),
                pool_specs=[p['spec'] for p in state['pool']])
    trace.emit('v6_stage_choice','%s：训练%s；内部开发%s；%s。'%(stage,score_text(best['score']['train']),score_text(best['score']['validation']),stop),
               stage=stage,score=best['score'],weights=best['pack']['weights'],weight_source=best['weight_source'],selected_specs=best['selected_specs'],train_rows=backend.rows[pool_rows])
    if persist:save_state(p,dict(state,completed=True,result=best),identity)
    return best
