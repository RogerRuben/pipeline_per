# -*- coding: utf-8 -*-
"""Actual ensemble response importance; never rewrite values using expert labels."""
import numpy as np
import pandas as pd

def response_importance(predict,frame,settings):
    baseline=np.asarray(predict(frame),float);n=len(frame);rng=np.random.RandomState(settings['seed'])
    if n<2 or baseline.shape!=(n,) or not np.isfinite(baseline).all():raise ValueError('重要性参考表或预测无效。')
    schedules=[rng.permutation(n) for _ in range(settings['permutations'])]
    values=[];den=max(float(np.mean(np.abs(baseline))),np.finfo(float).tiny)
    changes_by_field={key:[] for key in frame.columns};pending=[];owners=[]
    def flush():
        if not pending:return
        combined=pd.concat(pending,ignore_index=True);p=np.asarray(predict(combined),float)
        if p.shape!=(len(combined),) or not np.isfinite(p).all():raise ValueError('批量置换重要性预测无效。')
        for i,key in enumerate(owners):changes_by_field[key].append(float(np.mean(np.abs(p[i*n:(i+1)*n]-baseline)))/den*100.)
        pending[:]=[];owners[:]=[]
    for key in frame.columns:
        for schedule in schedules:
            perturbed=frame.copy();perturbed[key]=frame[key].values[schedule]
            pending.append(perturbed);owners.append(key)
            if len(pending)*n>=1024:flush()
    flush()
    for key in frame.columns:
        changes=changes_by_field[key]
        values.append({'field_id':key,'response_change_percent':float(np.mean(changes)),
            'repeat_std_percent':float(np.std(changes)),'constant_in_reference':bool(frame[key].nunique()<2)})
    total=sum(r['response_change_percent'] for r in values)
    for row in values:row['importance_percent']=100.*row['response_change_percent']/total if total>1e-12 else 0.
    return {'method':'ensemble_price_response_permutation_v1','reference_rows':n,'uses_price_labels':False,
        'normalization_available':total>1e-12,'permutations':settings['permutations'],'seed':settings['seed'],'attributes':values,
        'interpretation':'参考输入范围内，替换一个原始属性后实际集成价格的变化；不是因果贡献或独立验证精度。'}

def grade_consistency(importance,scores,all_fields,settings):
    known=set(all_fields);rated={k:float(v) for k,v in scores.items() if k in known}
    if any(not np.isfinite(v) or v<1 or v>5 or v!=int(v) for v in rated.values()):raise ValueError('五档一致性要求明确提供1—5整数评分。')
    counts={g:sum(int(v)==g for v in rated.values()) for g in range(1,6)}
    values={r['field_id']:r for r in importance['attributes']};ordered=sorted(rated,key=lambda k:(-values[k]['importance_percent'],k))
    allocation=[]
    for g in (5,4,3,2,1):allocation += [g]*counts[g]
    possible={k:{allocation[i]} for i,k in enumerate(ordered)};tol=settings['tie_tolerance']
    start=0
    while start<len(ordered):
        end=start+1;first=values[ordered[start]]['importance_percent']
        while end<len(ordered) and abs(values[ordered[end]]['importance_percent']-first)<=tol:end+=1
        bins=set(allocation[start:end])
        for k in ordered[start:end]:possible[k]=bins
        start=end
    records=[];wrong=ambiguous=0
    for k in ordered:
        choices=sorted(possible[k]);pred=choices[0] if len(choices)==1 else None
        undefined=not importance['normalization_available'] or len(set(rated.values()))<2
        if undefined:pred=None
        uncertain=pred is None;error=not uncertain and pred!=int(rated[k])
        ambiguous+=int(uncertain);wrong+=int(error)
        records.append(dict(values[k],expert_grade=int(rated[k]),model_grade=pred,possible_grades=choices,
                            ambiguous=uncertain,wrong_grade=error))
    coverage=len(rated)/max(1,len(known));rate=100.*wrong/len(rated) if rated else None
    conservative=100.*(wrong+ambiguous)/len(rated) if rated else None
    available=bool(rated) and importance['normalization_available'] and len(set(rated.values()))>=2
    passed=available and coverage>=settings['min_coverage'] and ambiguous==0 and rate<=settings['max_error_percent']+1e-12
    return {'mode':settings['mode'],'mapping':settings['mapping'],'status':'available' if available else 'not_identifiable',
        'passed':bool(passed),'rated_attributes':len(rated),'total_attributes':len(known),'coverage':coverage,
        'max_error_percent':settings['max_error_percent'],'wrong_attributes':wrong,'ambiguous_attributes':ambiguous,
        'error_percent':rate,'conservative_error_percent':conservative,'grade_counts':counts,'attributes':records,
        'unrated_attributes':sorted(known-set(rated)),
        'mapping_uses_individual_expert_labels':False,'mapping_uses_expert_grade_counts':True,
        'note':'档位配额在训练前由明确专家评分确定；同档不排序。持平跨档不按专家标签破同分，严格模式不算通过。'}
