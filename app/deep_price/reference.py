"""Historical prices only for the selected unchanged product; analyses stay model-only."""
import math
from .provider import BusinessSnapshot
from .errors import DeepPriceError
from app.value_semantics import values_equal

def reference_price(core,payload):
    if not isinstance(payload,dict):raise DeepPriceError('invalid_input','请求必须为对象')
    ref=payload.get('reference_product_id');params=payload.get('parameters')
    if not isinstance(params,dict):raise DeepPriceError('invalid_input','parameters必须为对象')
    status,schema=core.handle('GET','/schema')
    if status!=200:return status,schema
    with_snapshot=BusinessSnapshot(core.store)
    try:
        definitions={p['parameter_id']:p for p in with_snapshot.definitions}
        item=next((p for p in with_snapshot.products if p['product_id']==ref),None)
        if set(params)-set(definitions):raise DeepPriceError('invalid_input','存在未知业务属性')
        exact=item is not None and all(k in params and values_equal(params[k],item['parameters'].get(k),d) for k,d in definitions.items())
        price=item.get('historical_price_wan') if item else None
        if with_snapshot.fingerprint!=schema.get('business_snapshot_sha256'):raise DeepPriceError('version_conflict','业务数据已更新，请刷新',409)
        with_snapshot.assert_unchanged()
    finally:with_snapshot.close()
    identity=('product_code','release_id','model_version','price_model_sha256','model_instance_id','business_snapshot_sha256','price_output_signature')
    if any(k in payload and payload[k]!=schema.get(k) for k in identity):raise DeepPriceError('version_conflict','产品或模型版本已更新，请刷新',409)
    if exact and isinstance(price,(int,float)) and not isinstance(price,bool) and math.isfinite(price):
        result={k:schema.get(k) for k in identity}
        result.update(success=True,pricing_mode='historical',price_source='historical',predicted_price_wan=price,
                      historical_product_id=ref,business_parameters=params)
        return 200,result
    request={k:payload[k] for k in identity if k in payload};request['parameters']=params
    status,result=core.handle('POST','/predict',request)
    if status==200:result['price_source']='model'
    return status,result
