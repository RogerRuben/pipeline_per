# -*- coding: utf-8 -*-
"""Project implementation of the supplied V3 notebook's NSGA-II -> MOTPE design.

The uploaded notebook did not include its imported V3 Python module. This module
implements its documented route on the byte-preserved V2 model core.
"""
import math
import json
import time
from pathlib import Path
import numpy as np
import optuna
from .optimizer import run_training as v2_training,_select_pareto,_original_baseline
from .search_history import read_history
from .training_observation import ProgressBest


class SeedIndependentTPE(optuna.samplers.TPESampler):
    def __init__(self,seed,n_startup_trials,n_ei_candidates,constraints_func=None):
        kwargs={} if constraints_func is None else {'constraints_func':constraints_func}
        super().__init__(seed=seed,n_startup_trials=n_startup_trials,n_ei_candidates=n_ei_candidates,multivariate=False,**kwargs)
        self.seed_rng=np.random.RandomState(seed)
        self.fixed={}
        self.backup_sampler=optuna.samplers.RandomSampler(seed=seed)
        self.fallbacks=[]
        self.fallbacks_by_trial={}
    def sample_independent(self,study,trial,param_name,param_distribution):
        if param_name in self.fixed:return self.fixed[param_name]
        if param_name=='split_seed':
            return int(self.seed_rng.randint(param_distribution.low,param_distribution.high+1,dtype=np.int64))
        try:
            value=super().sample_independent(study,trial,param_name,param_distribution)
            internal=param_distribution.to_internal_repr(value)
            if not np.isfinite(internal) or not param_distribution._contains(internal):raise ValueError('TPE returned a nonfinite or out-of-range parameter')
            return value
        except (KeyError,ValueError,FloatingPointError,OverflowError) as exc:
            # Only recover errors from the TPE parameter sampler. Model fitting
            # and data errors are handled separately and are never disguised.
            item={'parameter':param_name,'error':repr(exc),'recovery':'seeded_independent_sample_within_same_distribution'}
            self.fallbacks.append(item)
            previous=self.fallbacks_by_trial.setdefault(trial.number,[]);previous.append(item)
            return self.backup_sampler.sample_independent(study,trial,param_name,param_distribution)


class LocalTrial(object):
    """Anchor-local numeric model parameters; categorical choices retain their space."""
    def __init__(self,trial,anchor,fraction):self.trial,self.anchor,self.fraction=trial,anchor,float(fraction)
    def __getattr__(self,key):return getattr(self.trial,key)
    def suggest_float(self,name,low,high,log=False,step=None):
        if name in self.anchor:
            if log:
                center=math.log(float(self.anchor[name]));width=(math.log(high)-math.log(low))*self.fraction
                low,high=max(low,math.exp(center-width)),min(high,math.exp(center+width))
            else:
                center=float(self.anchor[name]);width=(high-low)*self.fraction
                low,high=max(low,center-width),min(high,center+width)
        return self.trial.suggest_float(name,low,high,log=log,step=step)
    def suggest_int(self,name,low,high,step=1,log=False):
        if name=='split_seed':low=high=int(self.anchor[name])
        elif name in self.anchor:
            width=max(1,int(math.ceil((high-low)*self.fraction)));center=int(self.anchor[name])
            low,high=max(low,center-width),min(high,center+width)
        return self.trial.suggest_int(name,low,high,step=step,log=log)


def select_anchors(study,count,r2_target,mape_target):
    pool=[t for t in study.best_trials if t.user_attrs.get('valid') and np.isfinite(t.values).all()]
    if not pool:return []
    chosen,_,_=_select_pareto(study,r2_target,mape_target)
    priority=[chosen,min(pool,key=lambda t:t.values[1]),max(pool,key=lambda t:t.values[0])]
    priority.extend(sorted(pool,key=lambda t:t.values[1]))
    result=[]
    for trial in priority:
        if all(trial.number!=old.number for old in result):result.append(trial)
        if len(result)>=count:break
    return result


class SearchDriver(object):
    def __init__(self,settings,backend,identity,progress,seed_low,seed_high,optimizer_seed,r2_target,mape_target,observer=None):
        self.settings,self.backend,self.identity,self.progress=settings,backend,identity,progress
        self.seed_low,self.seed_high,self.seed=seed_low,seed_high,optimizer_seed
        self.r2_target,self.mape_target=r2_target,mape_target;self.counts={'nsga':0,'tpe':0};self.history={};self.anchors=[]
        self.monitor=ProgressBest(r2_target,mape_target)
        self.observer=observer
    def __call__(self,objective,initial,timeout_seconds):
        start=time.monotonic();cfg=self.settings
        study=optuna.create_study(directions=['maximize','minimize'],sampler=initial.sampler)
        warm=cfg['warm_start']
        imported,proposals,self.history=read_history(warm['history_file'],self.identity,warm['reuse_metrics'],warm['max_history_trials'],self.seed_low,self.seed_high)
        if imported:study.add_trials(imported)
        if warm['config_file']:
            import json
            from pathlib import Path
            old=json.loads(Path(warm['config_file']).read_text(encoding='utf-8-sig'))
            params=old.get('selected_parameters',old)
            # Config proposals are evaluated from scratch, even on identical data.
            from .price_automl_core import model_params_from_trial
            model_params_from_trial(optuna.trial.FixedTrial(params),self.seed_low,self.seed_high)
            if self.seed_low<=int(params['split_seed'])<=self.seed_high:proposals.insert(0,params)
        import json
        queued={json.dumps(t.params,sort_keys=True) for t in imported}
        def enqueue(params):
            import json
            key=json.dumps(params,sort_keys=True)
            if key not in queued:study.enqueue_trial(params);queued.add(key)
        for params in proposals[:warm['max_warm_proposals']]:enqueue(params)
        for seed in (7527,6192,9300,9431,3308):
            if self.seed_low<=seed<=self.seed_high:enqueue(_original_baseline(seed))
        restored=self.observer.restore(self) if self.observer is not None else None
        resumed_phase=None
        if restored is not None:
            study=restored['study'];resumed_phase=restored['phase']
            self.counts=restored['counts'];self.history=restored['history'];self.anchors=restored['anchors']
            for previous in study.trials:self.monitor.update(previous)
        def remaining():return None if timeout_seconds is None else max(0.,timeout_seconds-getattr(self.observer,'elapsed_before',0.)-(time.monotonic()-start))
        def report(phase):
            def callback(current,trial):
                self.counts[phase]+=1;done=sum(self.counts.values());total=cfg['nsga_trials']+cfg['tpe_trials']
                if trial.user_attrs.get('valid'):
                    self.progress('Optuna V3 %s %d/%d：划分种子%s，%s 模型拟合%d，缓存命中%d。'%(phase,done,total,trial.params.get('split_seed'),self.monitor.describe(trial),self.backend.stats['model_fits'],self.backend.stats['prediction_cache_hits']))
                else:self.progress('Optuna V3 %s %d/%d：跳过不稳定候选，继续搜索。%s'%(phase,done,total,trial.user_attrs.get('error','未知原因')))
                if trial.user_attrs.get('sampler_fallbacks'):self.progress('该轮TPE采样异常已恢复，%d个参数在原搜索范围内重新独立采样；详情已记录。'%len(trial.user_attrs['sampler_fallbacks']))
            return callback
        def broad(trial):trial.set_user_attr('phase','nsga');return objective(trial)
        def run_trials(current,func,count,phase):
            for _ in range(count):
                if remaining()==0:break
                if self.observer is not None and self.observer.should_stop(self):break
                trial=current.ask()
                values=func(trial)
                sampling_notes=getattr(current.sampler,'fallbacks_by_trial',{}).get(trial.number,[])
                if sampling_notes:trial.set_user_attr('sampler_fallbacks',list(sampling_notes))
                diagnostic=dict(getattr(self.backend,'last_diagnostic',{}) or {})
                if diagnostic:trial.set_user_attr('evaluation_diagnostic',diagnostic)
                # Invalid candidates are FAIL with no objective values. Giving
                # TPE synthetic (-1e6, 1e6) observations polluted its density.
                if trial.user_attrs.get('valid'):
                    finished=current.tell(trial,values=values)
                else:finished=current.tell(trial,state=optuna.trial.TrialState.FAIL)
                report(phase)(current,finished)
                if self.observer is not None:self.observer.after_trial(self,current,finished,phase)
        self.backend.searching=True
        try:
            if cfg['nsga_trials'] and resumed_phase!='tpe' and remaining()!=0:
                run_trials(study,broad,max(0,cfg['nsga_trials']-self.counts['nsga']),'nsga')
            # Transfer evaluated observations, not just enqueued parameter guesses.
            # WAITING baseline proposals are intentionally excluded from the TPE study.
            if resumed_phase=='tpe':
                tpe=study;self.tpe_history_count=restored['tpe_history_count']
            else:
                tpe=optuna.create_study(directions=['maximize','minimize'],sampler=SeedIndependentTPE(self.seed,cfg['startup_trials'],cfg['ei_candidates']))
                finished=[t for t in study.trials if t.state in (optuna.trial.TrialState.COMPLETE,optuna.trial.TrialState.FAIL)]
                for trial in finished:
                    if trial.state==optuna.trial.TrialState.COMPLETE and not trial.user_attrs.get('valid'):
                        trial=optuna.trial.create_trial(state=optuna.trial.TrialState.FAIL,params=trial.params,distributions=trial.distributions,user_attrs=trial.user_attrs)
                    tpe.add_trial(trial)
                self.tpe_history_count=sum(t.state==optuna.trial.TrialState.COMPLETE for t in tpe.trials)
            def refine(trial):
                index=self.counts['tpe'];trial.set_user_attr('phase','tpe')
                if index%cfg['anchor_refresh_interval']==0 or not self.anchors:
                    self.anchors=select_anchors(tpe,cfg['num_anchors'],self.r2_target,self.mape_target)
                # Scheduled global escapes explore independent seeds, never anchor +/- N.
                every=max(1,int(round(1./cfg['global_fraction']))) if cfg['global_fraction'] else None
                if self.anchors and not (every and (index+1)%every==0):
                    anchor=self.anchors[index%len(self.anchors)]
                    trial.set_user_attr('anchor',anchor.number);trial.set_user_attr('anchor_split_seed',anchor.params['split_seed'])
                    weight_every=max(1,int(round(1./cfg['weight_only_fraction']))) if cfg['weight_only_fraction'] else None
                    weights_only=weight_every is not None and index%weight_every==0
                    trial.set_user_attr('refinement','weights_only' if weights_only else 'hyperparameters_and_weights')
                    if weights_only:tpe.sampler.fixed={k:v for k,v in anchor.params.items() if not k.startswith('weight_')}
                    try:return objective(LocalTrial(trial,anchor.params,cfg['local_fraction']))
                    finally:tpe.sampler.fixed={}
                trial.set_user_attr('global_escape',True);return objective(trial)
            if cfg['tpe_trials'] and remaining()!=0:
                run_trials(tpe,refine,max(0,cfg['tpe_trials']-self.counts['tpe']),'tpe')
            if self.observer is not None:self.observer.flush(self,tpe,'tpe')
            self.search_diagnostics={'valid_trials':sum(t.state==optuna.trial.TrialState.COMPLETE and bool(t.user_attrs.get('valid')) for t in tpe.trials),
                'failed_trials':sum(t.state==optuna.trial.TrialState.FAIL for t in tpe.trials),'cache_statistics':dict(self.backend.stats),
                'sampler_fallback_count':len(tpe.sampler.fallbacks),'sampler_fallbacks':tpe.sampler.fallbacks,
                'failures':[{'trial':t.number,'phase':t.user_attrs.get('phase'),'split_seed':t.params.get('split_seed'),'error':t.user_attrs.get('error'),
                             'diagnostic':t.user_attrs.get('evaluation_diagnostic')} for t in tpe.trials if t.state==optuna.trial.TrialState.FAIL]}
            return tpe
        finally:
            self.backend.searching=False
            if not hasattr(self,'search_diagnostics'):
                import traceback
                self.search_diagnostics={'status':'aborted','phase_counts':dict(self.counts),'cache_statistics':dict(self.backend.stats),
                                         'traceback':traceback.format_exc(),'note':'原始异常继续向外抛出；未伪造评分或导出失败模型。'}
    def summary(self,study,chosen):
        return {'n_trials':sum(self.counts.values()),'history_trials_imported':self.history.get('imported_trials',0),
                'history_policy':self.history,'phase_counts':self.counts,'selected_phase':chosen.user_attrs.get('phase'),
                'tpe_inherited_completed_trials':self.tpe_history_count,'candidate_pool_count':len(study.trials),
                'cache_statistics':dict(self.backend.stats),'failed_trials':self.search_diagnostics['failed_trials'],
                'sampler_fallback_count':self.search_diagnostics['sampler_fallback_count'],
                'implementation_provenance':'project_implementation_of_uploaded_v3_notebook_design'}


def run_training(v3_settings,history_identity,model_backend,observer=None,**kwargs):
    driver=SearchDriver(v3_settings,model_backend,history_identity,kwargs.get('progress_callback') or print,
                        kwargs['seed_low'],kwargs['seed_high'],kwargs['optimizer_seed'],kwargs['r2_target'],kwargs['mape_target'],observer)
    try:return v2_training(model_backend=model_backend,search_driver=driver,history_identity=history_identity,**kwargs)
    finally:
        diagnostic=getattr(driver,'search_diagnostics',None)
        if diagnostic is not None:
            Path(kwargs.get('output_dir','optuna_results'),'v3_diagnostics.json').write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
