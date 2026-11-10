"""Frozen DS pricing behind the same HTTP contract as the V3 service."""
import copy
import hashlib
import json
import math
from pathlib import Path
import uuid
from services.common.http_service import ServiceApplication,JsonServiceError
from services.price_service.ds_engine import _DSModel


class DSPriceService(ServiceApplication):
    service_name='price-prediction-service'
    backend='ds_three_layer'
    def __init__(self,path):
        raw=json.dumps(path,ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8') if isinstance(path,dict) else Path(path).read_bytes();self.document=json.loads(raw.decode('utf-8-sig'))
        if self.document.get('format')!='pipeline-ds-release-1.0':raise ValueError('Invalid DS release')
        self.predictor=_DSModel(self.document['model'])
        self.fields=copy.deepcopy(self.document['fields'])
        expected={r['name'] for r in json.loads(self.predictor.schema_json())['required']}
        ids=[f['field_name'] for f in self.fields];names=[f['ds_name'] for f in self.fields]
        import re
        if len(set(ids))!=len(ids) or any(not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',v) for v in ids):raise ValueError('DS field IDs must be unique stable identifiers')
        if len(set(names))!=len(names) or set(names)!=expected:raise ValueError('DS field mapping must cover all priced attributes exactly once')
        self.model_sha256=hashlib.sha256(raw).hexdigest();self.model_instance_id=uuid.uuid4().hex
    def schema(self):
        return {'service':self.service_name,'backend':self.backend,'product_code':self.document['product_code'],
          'product_name':self.document['product_name'],'model_version':self.document['model']['version'],
          'fields':copy.deepcopy(self.fields),'feature_order':[f['field_name'] for f in self.fields],
          'model_count':1,'model_names':['DS三层定价'],'required_modules':[],
          'virtual_fixture':bool(self.document.get('virtual_data')),'analysis_metadata':self.analysis_metadata()}
    def analysis_metadata(self):
        return {'metrics':{},'feature_importance':None,'active_members':[],'expert_scores':None,
                'source':'loaded_ds_model','model_names':['DS三层定价'],
                'capabilities':{'layers':True,'prediction':True,'sensitivity':True,'automl_metrics':False}}
    def health(self):
        return dict(ServiceApplication.health(self),backend=self.backend,model_version=self.document['model']['version'],
                    product_code=self.document['product_code'],model_sha256=self.model_sha256,
                    model_instance_id=self.model_instance_id,model_count=1,model_names=['DS三层定价'],exact_mode=True)
    def _identity(self,payload):
        for k,v in [('expected_model_sha256',self.model_sha256),('expected_model_instance_id',self.model_instance_id)]:
            if k in payload and payload[k]!=v:raise JsonServiceError('价格模型实例已变化，请刷新后重试',409,'version_conflict')
    def _one(self,payload):
        self._identity(payload)
        params=payload.get('parameters') or payload.get('params') or {}
        if not isinstance(params,dict):raise JsonServiceError('parameters 必须为对象',400,'invalid_input')
        values={}
        for field in self.fields:
            key=field['field_name']
            if key not in params or params[key] is None or params[key]=='':raise JsonServiceError('缺少价格属性：'+key,400,'invalid_input')
            value=params[key];aliases=field.get('value_aliases') or {}
            values[field['ds_name']]=aliases.get(str(value),value)
        try:result=json.loads(self.predictor.predict_json(json.dumps(values,ensure_ascii=False,allow_nan=False)))
        except (ValueError,TypeError) as exc:raise JsonServiceError(str(exc),400,'invalid_input')
        if not result.get('success'):raise JsonServiceError(result['error']['message'],400,'invalid_input')
        total=result['total_price'];layers=result['layers']
        if not math.isfinite(total) or abs(sum(layers.values())-total)>1e-7*max(1,abs(total)):
            raise JsonServiceError('三层金额合计异常',503,'invalid_model_output')
        return {'success':True,'request_id':payload.get('request_id'),'candidate_id':payload.get('candidate_id'),
          'prediction':{'predicted_price_wan':total/10000.,'price_interval_wan':None,'confidence':'unverified'},
          'model':dict(self.health(),prediction_mode=self.backend),
          'input_status':{'filled_fields':{},'ignored_fields':sorted(set(params)-{f['field_name'] for f in self.fields}),'warnings':result['warnings']},
          'domain_status':{'in_domain':not bool(result['warnings']),'warnings':result['warnings']},
          'explanation':{'pricing_backend':self.backend,'unit':'元','total_price':total,'layers':layers,
                         'details':result['details'],'status':result['status'],'warnings':result['warnings']},
          'ds_result':result}
    def handle_post(self,path,payload):
        if not isinstance(payload,dict):raise JsonServiceError('请求必须为对象',400,'invalid_input')
        self._identity(payload)
        if path=='/api/v1/predict':return self._one(payload)
        if path=='/api/v1/predict/batch':
            items=payload.get('items') or []
            if not isinstance(items,list) or len(items)>100:raise JsonServiceError('单批最多100条',400,'batch_too_large')
            if any(not isinstance(i,dict) for i in items):raise JsonServiceError('批量条目必须为对象',400,'invalid_input')
            results=[self._one(i) for i in items]
            return {'success':True,'items':results,'model':self.health(),'request_id':payload.get('request_id')}
        raise JsonServiceError('接口不存在',404,'not_found')
    def openapi(self):return {'openapi':'3.0.0','info':{'title':'DS price service','version':'1.0'}}
    def example_request(self):
        return {'product_code':self.document['product_code'],'parameters':self.document['reference_parameters']}


def load_price_service(model_path=None,fallback_json=None,**kwargs):
    if model_path and Path(model_path).suffix.lower()=='.json':
        raw=json.loads(Path(model_path).read_text(encoding='utf-8-sig'))
        if raw.get('format')=='pipeline-ds-release-1.0':return DSPriceService(model_path)
        from services.price_service.app import PriceService
        return PriceService(None,model_path,**kwargs)
    from services.price_service.app import PriceService
    return PriceService(model_path,fallback_json,**kwargs)
