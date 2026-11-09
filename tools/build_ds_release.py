"""Import a frozen DS model and business workbook. Never trains a model."""
import json,shutil,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from product.common import read_json,write_json,sha
from services.price_service.ds_engine import load_model
from services.price_service.ds_backend import DSPriceService
from app.historical_onboarding import HistoricalProductOnboarding
from tools.product_identity import workbook_product_name


def build(source,out,product_code,product_name,config,virtual_data=False,inspect_only=False):
    source=Path(source).resolve();out=Path(out).resolve() if out else None
    if out and out.exists():raise ValueError('DS发布目录已存在，不能覆盖')
    model_path=Path(config['model_file']).resolve();model=read_json(model_path)
    sdk=load_model(model_path);specs=json.loads(sdk.schema_json())['required']
    if isinstance(model.get('model'),dict):model=model['model']
    product_name=workbook_product_name(source,product_name)
    report=HistoricalProductOnboarding().analyze(source.name,source.read_bytes(),product_code,product_name,[],require_dual_header=True)
    data=report['data'];definitions={p['parameter_id']:p for p in data['parameters']}
    mapping=config.get('field_map') or {p['parameter_id']:p['label'] for p in data['parameters'] if p['label'] in {s['name'] for s in specs}}
    if set(mapping)-set(definitions):raise ValueError('DS映射引用不存在的英文属性代号：'+','.join(sorted(set(mapping)-set(definitions))))
    if set(mapping.values())!={s['name'] for s in specs} or len(mapping)!=len(specs):raise ValueError('DS字段映射必须完整且不重复；模型要求：'+'、'.join(s['name'] for s in specs)+'；请在 settings/ds.json 的 field_map 中配置英文代号到这些名称的对应关系')
    fields=[]
    for key,name in mapping.items():
        p=definitions[key];spec=next(s for s in specs if s['name']==name)
        kind={'正整数':'integer','布尔':'boolean','数值':'number'}.get(spec['type'],'enum' if spec.get('allowed_values') else 'string')
        values=spec.get('allowed_values') or []
        p.update(value_type='number' if kind=='integer' else 'text' if kind=='string' else kind,required=1)
        if kind in ('number','integer','boolean'):
            p['allowed_values_json']=None
            p['search_type']='integer' if kind=='integer' else 'boolean' if kind=='boolean' else 'continuous'
        if kind=='integer':p.update(min_value=1)
        if kind=='number' and spec.get('method')=='按单位计价':p['min_value']=max(0,p.get('min_value') or 0)
        if values:p['allowed_values_json']=json.dumps(values,ensure_ascii=False)
        fields.append({'field_name':key,'field_label':p['label'],'ds_name':name,'dtype':kind,'unit':p.get('unit') or '',
                       'required':True,'missing_policy':'reject','allowed_values':values,
                       'generation_min':1 if kind=='integer' else max(0,p.get('min_value') or 0) if kind=='number' else p.get('min_value'), 'generation_max':p.get('max_value'),
                       'value_aliases':(config.get('value_aliases') or {}).get(key,{})})
    from app.value_semantics import normalize_boolean
    for row in data['agreements']:
        for field in fields:
            key=field['field_name'];value=row['params'].get(key);kind=field['dtype']
            if value is None:continue
            if kind in ('number','integer'):
                value=float(value)
                if kind=='integer' and value.is_integer():value=int(value)
                row['params'][key]=value
            elif kind=='boolean':
                value=normalize_boolean(value)
                if value is None:raise ValueError('无法解析布尔属性：'+key)
                row['params'][key]=bool(value)
    if not data['agreements']:raise ValueError('DS发布至少需要一条业务参考产品')
    wrapper={'format':'pipeline-ds-release-1.0','product_code':product_code,'product_name':product_name,
             'model':model,'fields':fields,'virtual_data':bool(virtual_data),'source_ds_sha256':sha(model_path),
             'reference_parameters':data['agreements'][0]['params']}
    service=DSPriceService(wrapper)
    predictions=[]
    for row in data['agreements']:
        result=service.handle_post('/api/v1/predict',{'parameters':row['params']})
        if row.get('historical_price_wan') is None:row['historical_price_wan']=result['prediction']['predicted_price_wan']
        predictions.append({'agreement_id':row['agreement_id'],'predicted_price_wan':result['prediction']['predicted_price_wan']})
    if inspect_only:return {'status':'PASS','backend':'ds_three_layer','training':False,'records':len(data['agreements']),'attributes':len(fields),'product_name':product_name,'source_model_sha256':sha(model_path)}
    out.mkdir(parents=True);price=out/'price';price.mkdir()
    model_file=price/'ds_release.json';write_json(model_file,wrapper)
    from automl_pipeline.product_effectiveness_bootstrap import build_effectiveness as effect_build
    effect_build(data,out/'effectiveness')
    from app.store import Store
    from app.product_releases import ProductReleaseService
    from app.model_service_client import DegradedServiceRuntime
    runtime=DegradedServiceRuntime(None,product_code,product_name)
    store=Store(out/'recommendation/data/protocol_demo.db',out/'unused.csv',runtime)
    releases=ProductReleaseService(store,runtime)
    draft=releases.create(product_code,product_name)
    for section in ('products','parameters','parameter_groups','tags','tag_rules','couplings','constraints','agreements'):
        if section in data:releases.set_section(draft['release_id'],section,data[section])
    validation=releases.validate(draft['release_id'])
    if not validation['valid']:raise ValueError('DS业务发布校验失败：'+str(validation))
    (out/'business_release.iprelease.json').write_bytes(releases.export_package(draft['release_id']))
    (out/'input').mkdir();shutil.copy2(source,out/'input'/source.name)
    metadata={'schema_version':'price-model-evaluation-metadata-1.0','price_model_sha256':sha(model_file),
              'data_classification':'synthetic' if virtual_data else 'unconfirmed','metrics':{},'feature_importance':None,
              'active_members':[],'capabilities':{'layers':True,'automl_metrics':False}}
    write_json(price/'evaluation_metadata.json',metadata)
    manifest={'format':'product-prototype-pipeline-1.0','status':'ready','price_engine':'ds_three_layer',
              'product_code':product_code,'product_name':product_name,'virtual_data':bool(virtual_data),
              'source_filename':source.name,'source_sha256':sha(source),'targets_met':None,
              'price_model':'price/ds_release.json','effectiveness_model':'effectiveness/effectiveness_bundle.json',
              'business_package':'business_release.iprelease.json','price_backend':'ds_three_layer'}
    manifest['artifact_sha256']={k:sha(out/manifest[k]) for k in ('price_model','effectiveness_model','business_package')}
    write_json(out/'prototype_manifest.json',manifest)
    write_json(out/'import_validation.json',{'status':'pass','trained':False,'source_model_sha256':sha(model_path),'predictions':predictions})
    return manifest
