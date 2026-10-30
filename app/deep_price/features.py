"""Finite, bounded analyses routed exclusively through the existing price gateway."""
import math
import uuid

from .analysis import DeepPriceAnalysis, Deadline
from .errors import DeepPriceError
from .provider import BusinessSnapshot

IDENTITY = ('product_code','release_id','model_version','price_model_sha256','model_instance_id',
            'business_snapshot_sha256','price_output_signature')


class AnalysisFeatures:
    def __init__(self, core):
        self.core = core

    def handle(self, method, path, payload=None, query=None):
        request_id='DEEP-'+uuid.uuid4().hex
        if method == 'GET' and path in ('/model-summary','/feature-importance','/explanation/status'):
            status, schema = self.core.handle('GET','/schema')
            if status != 200:
                return status, schema
            result = {key:schema[key] for key in IDENTITY + ('request_id','price_unit')}
            meta = schema.get('analysis_metadata') or {}
            if path == '/explanation/status':
                result.update(status='not_configured', metric_kind=None, data={})
            elif path == '/feature-importance':
                actual = meta.get('feature_importance')
                result.update(status='available' if actual else 'unavailable', metric_kind='model_feature_importance' if actual else None,
                              data={'feature_importance':actual, 'expert_scores':meta.get('expert_scores'), 'expert_metric_kind':'expert_attribute_score'})
            else:
                result.update(status='available', metric_kind='model_summary', data={'backend':schema.get('backend'),
                              'published_at':schema.get('published_at'), 'fixture':schema.get('fixture'), **meta})
            return 200, result
        if method != 'POST' or path not in ('/sensitivity/single','/sensitivity/top10','/scenario'):
            return self.core.handle(method,path,payload,query)
        if path == '/scenario':
            if not isinstance(payload,dict) or not isinstance(payload.get('items'),list):
                return 400,DeepPriceError('invalid_input','情景items必须为数组').response(request_id)
            items,names=[],[]
            for item in payload['items']:
                if not isinstance(item,dict) or set(item)-{'item_id','parameters','name'}:
                    return 400,DeepPriceError('invalid_input','情景字段仅允许item_id、parameters、name').response(request_id)
                name=item.get('name',item.get('item_id'))
                if not isinstance(name,str) or not name.strip() or len(name)>100:
                    return 400,DeepPriceError('invalid_input','情景名称必须为1至100字符').response(request_id)
                items.append({k:v for k,v in item.items() if k!='name'})
                names.append(name)
            status,result=self.core.handle('POST','/predict/batch',dict(payload,items=items))
            if status==200:
                for item,name in zip(result['items'],names):
                    item['label']=name
            return status,result
        deadline = Deadline(self.core.budget_seconds)
        try:
            if not isinstance(payload, dict) or not isinstance(payload.get('parameters'), dict):
                raise DeepPriceError('invalid_input','parameters必须为对象')
            allowed = set(IDENTITY) | {'parameters','field_id','values'} if path.endswith('/single') else set(IDENTITY) | {'parameters','field_ids'}
            if set(payload)-allowed:
                raise DeepPriceError('invalid_input','包含未知分析参数')
            core = DeepPriceAnalysis(self.core.store,self.core.gateway,deadline.remaining())
            status, schema = core.handle('GET','/schema')
            if status != 200:
                return status, schema
            for key in IDENTITY:
                if key not in payload:
                    raise DeepPriceError('invalid_input','缺少身份字段：'+key)
                if payload[key] != schema[key]:
                    raise DeepPriceError('version_conflict','分析版本已变化，请刷新',409)
            fields = {f['field_id']:f for f in schema['fields'] if f['price_model_input']}
            selected = [payload.get('field_id')] if path.endswith('/single') else payload.get('field_ids')
            if selected is None:
                selected = [k for k,f in fields.items() if f['type'] in ('number','integer','enum','boolean')
                            and (f.get('allowed_values') or f['type']=='boolean' or isinstance(payload['parameters'].get(k),(int,float)))]
            if not isinstance(selected,list) or not selected or any(not isinstance(k,str) for k in selected) or len(set(selected)) != len(selected) or any(k not in fields for k in selected):
                raise DeepPriceError('invalid_input','请选择不同的可计算价格模型字段')
            plan = []
            for key in selected:
                values = payload.get('values') if path.endswith('/single') else None
                if values is None:
                    values = sample_values(fields[key],payload['parameters'].get(key),9 if len(selected)>1 else 21)
                if not isinstance(values,list) or not 1 <= len(values) <= 99:
                    raise DeepPriceError('invalid_input','采样点必须为1至99个合法标量')
                unique = []
                for value in values:
                    if isinstance(value,(list,dict)) or isinstance(value,float) and not math.isfinite(value):
                        raise DeepPriceError('invalid_input','采样值必须为有限标量')
                    if value not in unique:
                        unique.append(value)
                plan.append((fields[key],unique))
            sample_count=sum(len(values) for _,values in plan)
            if sample_count>10000:
                raise DeepPriceError('batch_too_large','分析总采样上限10000，请缩小字段范围',413)
            request = {key:payload[key] for key in IDENTITY}
            series = [{'field_id':field['field_id'],'label_zh':field['label_zh'],'unit':field['unit'],'points':[]}
                      for field,_ in plan]
            # Keep only scalar sampling results across chunks. Full parameter
            # dictionaries exist for at most 99 sample points at a time.
            jobs=((index,value) for index,(_,values) in enumerate(plan) for value in values)
            base=None
            batches=0
            lease=BusinessSnapshot(self.core.store,deadline.remaining())
            lease.release_read_lock()
            try:
                while True:
                    chunk=[]
                    for _ in range(99):
                        job=next(jobs,None)
                        if job is None:
                            break
                        chunk.append(job)
                    if not chunk:
                        break
                    lease.assert_unchanged()
                    items=[{'item_id':'base','parameters':payload['parameters']}]
                    items.extend({'item_id':'point-%d'%i,'parameters':dict(payload['parameters'],**{plan[index][0]['field_id']:value})}
                                 for i,(index,value) in enumerate(chunk))
                    core=DeepPriceAnalysis(self.core.store,self.core.gateway,deadline.remaining())
                    status,batch=core.handle('POST','/predict/batch',dict(request,items=items))
                    if status!=200:
                        return status,batch
                    batches+=1
                    current_base=batch['items'][0]['result']['predicted_price_wan']
                    if base is not None and not math.isclose(base,current_base,rel_tol=1e-12,abs_tol=1e-8):
                        raise DeepPriceError('version_conflict','同一模型身份下的基准价格发生变化，整次分析作废',409)
                    base=current_base
                    for (index,value),item in zip(chunk,batch['items'][1:]):
                        price=item['result']['predicted_price_wan']
                        series[index]['points'].append({'value':value,'price_wan':price,
                                                       'change_rate_pct':None if base==0 else (price-base)/base*100})
                    lease.assert_unchanged()
                # Check once more after the last chunk; never return partial
                # curves after service/version/config changes between batches.
                status,final=DeepPriceAnalysis(self.core.store,self.core.gateway,deadline.remaining()).handle('GET','/schema')
                if status!=200:
                    return status,final
                if any(final[key]!=request[key] for key in IDENTITY):
                    raise DeepPriceError('version_conflict','跨批次分析版本发生变化，整次分析作废',409)
                lease.assert_unchanged()
            finally:
                lease.close()
            if path.endswith('/top10'):
                series.sort(key=lambda s:max((abs(p['change_rate_pct']) for p in s['points'] if p['change_rate_pct'] is not None),default=0),reverse=True)
                series=series[:10]
            result = dict(request,request_id=request_id,price_unit='万元')
            result.update(success=True,pricing_mode='ml',base_price_wan=base,metric_kind='price_change_rate',series=series,compared_field_count=len(selected),
                          sample_count=sample_count,batch_count=batches,
                          note='在所选字段及合法采样范围内按最大绝对价格变化率排序；跨批次使用同一模型身份，不是全局重要度或因果效应。')
            deadline.remaining()
            return 200,result
        except DeepPriceError as exc:
            return exc.status,exc.response(request_id)


def sample_values(field,base,count):
    allowed = field.get('allowed_values') or ([True,False] if field['type']=='boolean' else [])
    if allowed:
        if len(allowed)<=count:
            return allowed
        return [allowed[round(i*(len(allowed)-1)/(count-1))] for i in range(count)]
    if field['type'] not in ('number','integer') or isinstance(base,bool) or not isinstance(base,(int,float)) or not math.isfinite(base):
        raise DeepPriceError('invalid_input','数值敏感性需要当前数值，枚举字段需要允许值')
    lo,hi = field.get('minimum'),field.get('maximum')
    span = max(abs(base)*.2,1)
    lo = max(base-span,lo) if lo is not None else base-span
    hi = min(base+span,hi) if hi is not None else base+span
    if hi<lo:
        raise DeepPriceError('invalid_input','当前数值不在业务合法范围')
    if field['type']=='integer':
        lo,hi = math.ceil(lo),math.floor(hi)
    values = [lo+(hi-lo)*i/(count-1) for i in range(count)]
    if field['type']=='integer':
        values = list(dict.fromkeys(round(v) for v in values))
    return values
