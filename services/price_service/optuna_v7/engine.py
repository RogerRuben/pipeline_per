# -*- coding: utf-8 -*-
"""Bounded post-V3 comparison; all reused scores are explicitly DEVELOPMENT."""
import copy,hashlib,itertools,json,time
from pathlib import Path
import numpy as np
from ..optuna_v2 import price_automl_core as core
from ..optuna_v2.typed_features import TypedBackend
from ..optuna_v4.metrics import metrics,violation
from ..optuna_v4.native_member import predict_saved
from ..optuna_v5.persistence import save_state,load_state
from ..optuna_v5.trace import plain,score_text
from .backend import Backend,FitLedger,ExtraBudgetStop
from .review import make_folds,candidate_pool,shrink_weights,summarize,guard,score_key,mix_results
from .importance import response_importance,grade_consistency

def pack(items,weights):
    return {'format':'price-v7-v3plus-1.0','configurations':[r['bundle'] for r in items],
        'weights':list(map(float,weights)),'entries':[{'name':r['recipe']['id'],'recipe':r['recipe']} for r in items]}

def run(frame,y,rows,expert,category_fields,category_labels,cfg,baseline_dir,baseline_report,root,identity,trace):
    v=cfg['v7'];root=Path(root);start=time.monotonic();persist=v['persistence']['enabled']
    restored=load_state(root/'enhancement_result',identity) if persist and v['persistence']['resume'] else None
    if restored is not None:
        restored=copy.deepcopy(restored);restored['extra_fits_this_session']=0;restored['restored']=True
        trace.emit('v7_enhancement_restored',statistics=restored['fit_statistics']);return restored
    baseline_dir=Path(baseline_dir);full=core.load_bundle(baseline_dir/'price_ensemble_bundle.pkl')
    p=baseline_report['selected_parameters'];base_recipe={'id':'baseline','params':p,'expert_strength':1.,'model_seeds':[0]}
    base_item={'recipe':base_recipe,'bundle':full};base_pack=pack([base_item],[1.])
    baseline_fits=baseline_report.get('cache_statistics',{}).get('model_fits',6*baseline_report['trials_completed'])
    limit=min(v['budget']['max_extra_fits'],max(v['budget']['minimum_extra_fits'],int(np.ceil(baseline_fits*v['budget']['extra_fit_ratio']))))
    ledger=FitLedger(root,limit,v['budget']['extra_seconds'],trace)
    backend=Backend(frame,y,rows,expert,category_fields,category_labels,root,identity,ledger,trace)
    split=core.split_data(frame,y,rows,int(p['split_seed']),cfg['validation']['test_size'],cfg['validation']['price_bins'])
    lookup={int(n):i for i,n in enumerate(rows)};anchor={'id':'v3_anchor','train':np.array([lookup[int(n)] for n in split[-2]],int),
        'validation':np.array([lookup[int(n)] for n in split[-1]],int)}
    selected_snapshot=core.load_bundle(baseline_dir/'selected_split_bundle.pkl')
    base_anchor=metrics(y[anchor['validation']],TypedBackend.predict(selected_snapshot,frame.iloc[anchor['validation']])[0])
    base_train=metrics(y,TypedBackend.predict(full,frame)[0])
    planned_seeds=[0];s=v['model_seeds'];rng=np.random.RandomState(v['review']['seed'])
    if s['enabled']:
        planned_seeds=[0] if s['low']==0 else []
        while len(planned_seeds)<s['count']:
            value=int(rng.randint(s['low'],s['high']+1,dtype=np.int64))
            if value not in planned_seeds:planned_seeds.append(value)
    trace.emit('v7_enhancement_plan',baseline_fits=baseline_fits,extra_fit_limit=limit,model_seeds=planned_seeds,
               note='划分种子仅属于原V3开发搜索；新增训练种子事先声明，平均全部计划种子，不挑最高分。')
    history=json.loads((baseline_dir/'study_history.json').read_text(encoding='utf-8'))
    common=[];plan=None;reviewed=[];review_status='disabled';stop_reason=None;base_review=None
    # Reserve full-data refits. Original baseline is already available for fallback.
    active=core.normalized_weights(p)>0
    worst_per_recipe=sum((len(planned_seeds) if name in ('extratrees','gbdt') else 1) for name,on in zip(core.MODEL_NAMES,active) if on)
    reserve=worst_per_recipe*min(3,v['review']['max_candidates'])
    def evaluate(recipe):
        parts=[]
        for f in common+[anchor]:
            bundle=backend.fit(f['train'],recipe,'anchor' if f['id']=='v3_anchor' else 'common_cv_%s'%f['id'])
            tr,tc=backend.predict(bundle,frame.iloc[f['train']]);va,vc=backend.predict(bundle,frame.iloc[f['validation']])
            item=dict(f,train_components=np.asarray(tc,float),validation_components=np.asarray(vc,float))
            if f['id']=='v3_anchor':an=item
            else:parts.append(item)
            trace.emit('v7_review_fold',candidate=recipe['id'],fold=f['id'],train_rows=rows[f['train']],validation_rows=rows[f['validation']],
                train=metrics(y[f['train']],tr),validation=metrics(y[f['validation']],va))
        weights=np.asarray(recipe.get('family_weights',core.normalized_weights(recipe['params'])),float)
        return {'recipe':copy.deepcopy(recipe),'parts':parts,'anchor':an,'weights':weights,'score':summarize(parts,an,y,weights)}
    if v['review']['enabled'] and v['mode']!='baseline_only':
        try:
            common,plan=make_folds(frame,rows,cfg);trace.emit('v7_common_plan',plan=plan)
            proposals=candidate_pool(baseline_report,frame,y,rows,cfg,history)
            for recipe in proposals:
                if len({q['recipe']['id'].split('__')[0] for q in reviewed})>=v['review']['max_candidates']:break
                required=(len(common)+1)*sum(core.normalized_weights(recipe['params'])>0)
                if not ledger.room(required+reserve):stop_reason='reserve_final_refits';break
                try:
                    trace.emit('v7_review_candidate_start',recipe=recipe)
                    result=evaluate(recipe);reviewed.append(result)
                    if recipe['id']=='baseline':base_review=result
                    if v['fusion']['enabled']:
                        P=np.vstack([f['validation_components'] for f in result['parts']]);target=np.concatenate([y[f['validation']] for f in result['parts']])
                        updated=shrink_weights(P,target,result['weights'],v['fusion'])
                        if not np.allclose(updated,result['weights'],atol=1e-9):
                            extra=copy.deepcopy(result);extra['weights']=updated;extra['recipe']['id']+='__shrunk'
                            extra['recipe']['family_weights']=updated.tolist();extra['score']=summarize(extra['parts'],extra['anchor'],y,updated);reviewed.append(extra)
                        if v['fusion']['drop_weak_members']:
                            # A finite one-member ablation uses existing predictions, no
                            # additional fits. Bound total movement from the V3 prior.
                            for family_index,family in enumerate(core.MODEL_NAMES):
                                prior=result['weights'];removed=float(prior[family_index])
                                if removed<=0 or removed>=1. or 2.*removed>v['fusion']['max_prior_movement']+1e-12:continue
                                w=prior.copy();w[family_index]=0.;w/=w.sum()
                                extra=copy.deepcopy(result);extra['weights']=w;extra['recipe']['id']+='__drop_'+family
                                extra['recipe']['family_weights']=w.tolist();extra['score']=summarize(extra['parts'],extra['anchor'],y,w)
                                reviewed.append(extra);trace.emit('v7_member_ablation',recipe=extra['recipe'],score=extra['score'],additional_fits=0)
                    trace.emit('v7_review_candidate_complete',recipe=recipe,score=result['score'],partial_score_eligible=False)
                except ExtraBudgetStop:stop_reason='extra_budget_during_candidate';break
                except (ValueError,FloatingPointError,OverflowError,RuntimeError) as exc:
                    trace.emit('v7_review_candidate_rejected',recipe=recipe,error=repr(exc),reason='incomplete_or_numerically_invalid')
                    if recipe['id']=='baseline':raise
            if base_review:
                leaders=sorted([q for q in reviewed if guard(q['score'],base_review['score'],cfg)],key=lambda q:score_key(q['score'],cfg))
                for j,leader in enumerate(leaders[:s['max_candidates']]):
                    if len(planned_seeds)<2:break
                    required=(len(common)+1)*sum(w>0 and name in ('extratrees','gbdt') for name,w in zip(core.MODEL_NAMES,leader['weights']))*(len(planned_seeds)-1)
                    if not ledger.room(required+reserve):break
                    recipe=copy.deepcopy(leader['recipe']);recipe['id']+='__seed_mean';recipe['model_seeds']=planned_seeds
                    try:
                        seeded=evaluate(recipe);reviewed.append(seeded)
                        trace.emit('v7_seed_average_complete',recipe=recipe,score=seeded['score'],seeds_cherry_picked=False)
                    except ExtraBudgetStop:break
                    except (ValueError,FloatingPointError,OverflowError,RuntimeError) as exc:trace.emit('v7_seed_average_rejected',recipe=recipe,error=repr(exc),all_seeds_required=True)
                review_status='complete'
            else:review_status='baseline_only_budget'
        except (ValueError,FloatingPointError,OverflowError,RuntimeError) as exc:
            stop_reason=repr(exc);review_status='unavailable';trace.emit('v7_review_unavailable',error=repr(exc),baseline_preserved=True)
    # Only common-fold-complete records may be compared. The original snapshot stays untouched.
    eligible=[r for r in reviewed if base_review and guard(r['score'],base_review['score'],cfg)]
    eligible.sort(key=lambda r:score_key(r['score'],cfg))
    choices=[{'items':[r],'weights':np.ones(1),'score':r['score'],'name':r['recipe']['id']} for r in eligible]
    if v['fusion']['enabled'] and len(eligible)>1:
        counter=0
        for size in range(2,min(v['fusion']['max_configurations'],len(eligible))+1):
            for members in itertools.combinations(eligible[:5],size):
                if counter>=v['fusion']['max_combinations']:break
                counter+=1
                # A small, shrunk mixture of already competent recipes; no nested global search.
                P=np.column_stack([np.concatenate([f['validation_components'].dot(r['weights']) for f in r['parts']]) for r in members])
                target=np.concatenate([y[f['validation']] for f in members[0]['parts']]);prior=np.ones(size)/size
                w=shrink_weights(P,target,prior,v['fusion']);score=mix_results(members,w,y)
                if guard(score,base_review['score'],cfg):choices.append({'items':members,'weights':w,'score':score,'name':'mixture_'+str(counter)})
    baseline_choice={'items':[base_review] if base_review else [],'weights':np.ones(1),'score':base_review['score'] if base_review else None,'name':'baseline','pack':base_pack}
    choices.sort(key=lambda q:score_key(q['score'],cfg));choices=choices[:3] if v['consistency']['mode'] not in ('soft','strict') else choices[:v['review']['max_candidates']]
    choices.append(baseline_choice);finals=[];full_cache={'baseline':full}
    def consistency_for(model):
        imp=response_importance(lambda X:predict_saved(model,X),frame,v['consistency'])
        return imp,grade_consistency(imp,cfg['expert']['scores'],list(frame.columns),v['consistency'])
    for choice in choices:
        try:
            if 'pack' not in choice:
                members=[]
                for r in choice['items']:
                    recipe=r['recipe'];key=json.dumps(recipe,sort_keys=True)
                    if key not in full_cache:full_cache[key]=backend.fit(np.arange(len(frame)),recipe,'full_data_finalist')
                    members.append({'recipe':recipe,'bundle':full_cache[key]})
                choice['pack']=pack(members,choice['weights'])
            predicted=predict_saved(choice['pack'],frame);choice['train']=metrics(y,predicted)
            if choice['score'] is not None:choice['score']=copy.deepcopy(choice['score']);choice['score']['deployed_train']=choice['train']
            choice['importance']=choice['consistency']=None
            if v['consistency']['mode'] in ('soft','strict'):choice['importance'],choice['consistency']=consistency_for(choice['pack'])
            finals.append(choice);trace.emit('v7_actual_model_check',name=choice['name'],training=choice['train'],development=choice['score'],consistency=choice['consistency'])
        except ExtraBudgetStop:trace.emit('v7_finalist_budget_stop',name=choice['name']);continue
        except (ValueError,FloatingPointError,OverflowError,RuntimeError) as exc:
            trace.emit('v7_actual_model_rejected',name=choice['name'],error=repr(exc))
            if choice is baseline_choice:raise
    if not finals:raise ValueError('没有可导出的有效价格模型；原V3产物仍保留。')
    mode=v['consistency']['mode']
    def final_key(choice):
        con=choice['consistency'];score=choice['score']
        if score is None:key=(False,0.,base_anchor['mape_percent'],base_anchor['mape_percent'])
        else:key=score_key(score,cfg)
        if mode=='strict':return (not (not key[0] and con['passed']),bool(key[0]),not con['passed'])+tuple(key)
        if mode=='soft':
            error=con['conservative_error_percent'];penalty=v['consistency']['soft_penalty_pp']*(100. if error is None else error)/100.
            return tuple(key[:2])+(key[2]+penalty,)+tuple(key[3:])
        return tuple(key)
    selected=min(finals,key=final_key)
    if mode=='report':
        # Reporting must not consume candidate-fit time or change selection.
        selected['importance'],selected['consistency']=consistency_for(selected['pack'])
        trace.emit('v7_expert_report_only',selected=selected['name'],consistency=selected['consistency'],selection_already_frozen=True)
    anchor_metric=selected['score']['anchor_validation'] if selected['score'] else base_anchor
    q=cfg['targets'];met=violation(selected['train'],q['mape_percent_max'],q['r2_min'])==0 and violation(anchor_metric,q['mape_percent_max'],q['r2_min'])==0
    if mode=='strict':met=met and selected['consistency']['passed']
    cv=selected['score']['validation'] if selected['score'] else None
    if cv:
        residual=[]
        for f_index,f in enumerate(selected['items'][0]['parts']):
            pred=sum(w*r['parts'][f_index]['validation_components'].dot(r['weights']) for w,r in zip(selected['weights'],selected['items']))
            residual.extend(np.abs(y[f['validation']]-pred))
        interval_source='common_development_oof_uncalibrated'
    else:residual=np.abs(y-predict_saved(selected['pack'],frame));interval_source='training_reference_uncalibrated'
    result={'pack':selected['pack'],'selected_name':selected['name'],'train':selected['train'],'anchor':anchor_metric,'cv':cv,
        'selected_score':selected['score'],'importance':selected['importance'],'consistency':selected['consistency'],
        'reference_anchor':base_anchor,'reference_train':base_train,'reference_cv':base_review['score']['validation'] if base_review else None,
        'plan':plan,'review_status':review_status,'stop_reason':stop_reason or 'bounded_candidates_completed',
        'targets_met':bool(met),'enhancement_applied':selected['name']!='baseline','restored':False,
        'interval_calibration':{'space':'native','half_width_native':float(np.quantile(residual,.95)),'coverage':None,'source':interval_source},
        'baseline_fits':baseline_fits,'extra_fits_this_session':ledger.used-ledger.before,
        'fit_statistics':{'extra_fit_limit':limit,'extra_attempts_total':ledger.used,'extra_new_attempts':ledger.used-ledger.before,'cache_hits':ledger.cache_hits},
        'review_candidates':[{'recipe':r['recipe'],'score':r['score'],'guard_passed':bool(base_review and guard(r['score'],base_review['score'],cfg))} for r in reviewed],
        'finalists':[{'name':r['name'],'train':r['train'],'score':r['score'],'consistency':r['consistency']} for r in finals],
        'elapsed_enhancement_seconds':time.monotonic()-start}
    trace.emit('v7_selection_complete',selected=selected['name'],training=result['train'],common_development=cv,
        anchor_development=anchor_metric,targets_met=met,expert_consistency=result['consistency'],statistics=result['fit_statistics'])
    if persist:save_state(root/'enhancement_result',result,identity)
    if not v['persistence']['keep_fit_cache']:
        folder=(root/'fit_cache').resolve()
        if folder.parent!=root.resolve():raise ValueError('拒绝清理越界缓存。')
        for file in folder.iterdir():
            if file.is_file() and not file.is_symlink():file.unlink()
    return result
