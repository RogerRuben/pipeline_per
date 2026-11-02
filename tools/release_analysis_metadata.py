"""Offline, release-bound permutation analysis. Never fits or changes a model."""
import copy
import hashlib
import json
import math
import random
import statistics
from pathlib import Path

def enrich(model, business, report):
    from services.price_service.native_bundle import load_bundle,predict_batch
    bundle=load_bundle(model)
    members=bundle.get('ensemble',{}).get('members',[])
    member_info=[{'name':m.get('name'),'weight':float(m.get('weight',1))} for m in members]
    result={'active_members':member_info,
            'search_candidates':sorted(k[7:] for k in report.get('selected_parameters',{}) if k.startswith('weight_')),
            'feature_importance':None}
    if business is None:
        return result
    business=Path(business)
    source=json.loads(business.read_text(encoding='utf-8-sig'))
    definitions={p['parameter_id']:p for p in source['data']['parameters']}
    rows=[r for r in sorted(source['data']['agreements'],key=lambda x:x['agreement_id'])
          if isinstance(r.get('historical_price_wan'),(int,float)) and r.get('params')]
    # Prefer the exact search-validation IDs; full refit may include them.
    validation_ids=set(report.get('validation_record_ids') or [])
    matched=[r for r in rows if r['agreement_id'] in validation_ids]
    scope='selected_search_validation_after_refit_not_independent_test' if len(matched)>=4 else 'released_reference_rows_not_independent_test'
    rows=(matched if len(matched)>=4 else rows)[:64]
    if len(rows)<4:return result
    keys=bundle.get('source_feature_order') or bundle['feature_order']
    data=[]
    for row in rows:
        values=dict(row['params'])
        for key in keys:
            mapping=definitions.get(key,{}).get('model_value_mapping_json') or {}
            if isinstance(mapping,str):mapping=json.loads(mapping)
            if mapping:values[key]=mapping.get(str(values.get(key)),values.get(key))
        data.append(values)
    truth=[float(r['historical_price_wan']) for r in rows]
    def mae(values):
        predicted=predict_batch(bundle,values)
        return sum(abs(item['predicted_price_wan']-y) for item,y in zip(predicted,truth))/len(truth)
    baseline=mae(data);items=[]
    for index,key in enumerate(keys):
        scores=[]
        for repeat in range(3):
            values=[r.get(key) for r in data];random.Random(20261102+index*101+repeat).shuffle(values)
            scores.append(mae([dict(row,**{key:v}) for row,v in zip(data,values)])-baseline)
        items.append({'field_id':key,'label_zh':definitions.get(key,{}).get('label',key),
                      'importance':statistics.mean(scores),'std':statistics.pstdev(scores)})
    items.sort(key=lambda r:r['importance'],reverse=True)
    total=sum(max(0,r['importance']) for r in items)
    cumulative=0
    for rank,row in enumerate(items,1):
        row.update(rank=rank,ratio=max(0,row['importance'])/total if total else 0)
        cumulative+=row['ratio'];row['cumulative_ratio']=cumulative
    published=copy.deepcopy(source['data']);published.setdefault('parameter_groups',[])
    for parameter in published.get('parameters',[]):
        if parameter.get('value_type') in ('integer','numeric'):parameter['value_type']='number'
    result['feature_importance']={
        'business_data_sha256':hashlib.sha256(json.dumps(published,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest(),
        'kind':'permutation_mae_increase','scope':scope,
        'scoring':'permutation MAE minus baseline MAE','unit':'万元','baseline_mae':baseline,
        'seed':20261102,'repeats':3,'max_sample_rows':64,'sample_selection':'sorted matching validation IDs; fallback published reference rows only if fewer than four matches','sample_ids':[r['agreement_id'] for r in rows],
        'sample_sha256':hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
        'business_package_sha256':hashlib.sha256(business.read_bytes()).hexdigest(),
        'feature_schema_sha256':hashlib.sha256(json.dumps(bundle.get('feature_schema'),sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
        'model_sha256':hashlib.sha256(Path(model).read_bytes()).hexdigest(),
        'items':items,'normalization':'positive MAE increases / sum of all positive increases; negative values retained',
        'note':'发布参考样本可能参与全数据重拟合，不是独立测试或因果贡献；正值占比仅作展示。'}
    return result

