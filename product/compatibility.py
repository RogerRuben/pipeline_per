"""Read-only desktop upgrade contracts. Never imports a model or writes SQLite."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from product.common import read_json,write_json,contained


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
                                    separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()


def decoded(value,default):
    return json.loads(value) if isinstance(value,str) and value else default if value is None else value


def business_schema(snapshot):
    fields=[]
    for row in snapshot.get('parameters',[]):
        if not row.get('enabled',1):continue
        kind={'numeric':'number','integer':'number'}.get(row.get('value_type'),row.get('value_type'))
        field={key:row.get(key) for key in ('parameter_id','unit','search_type','min_value','max_value')}
        field.update(unit=row.get('unit') or '',value_type=kind,search_type=row.get('search_type') or 'auto',
                     required=bool(row.get('required',0)))
        for key in ('min_value','max_value'):
            if field[key] is not None:field[key]=float(field[key])
        for key,default in (('allowed_values',[]),('special_value_keys',[]),
                            ('model_value_mapping',{}),('display_value_mapping',{})):
            field[key]=decoded(row.get(key+'_json',row.get(key)),default)
        fields.append(field)
    return sorted(fields,key=lambda row:row['parameter_id'])


def model_schema(schema):
    keys=('field_name','dtype','unit','required','missing_policy','allowed_values','category_mapping','source','parser')
    fields=[{key:row.get(key) for key in keys} for row in schema['fields']]
    ids=[row['field_name'] for row in fields]
    order=schema['feature_order']
    if len(set(ids))!=len(ids) or len(set(order))!=len(order) or set(ids)!=set(order):
        raise ValueError('Model schema/order is incomplete or duplicated')
    return {'feature_order':order,'fields':sorted(fields,key=lambda row:row['field_name'])}


def binding_schema(rows):
    keys=('model_kind','parameter_id','source_type','data_type','unit','required','missing_policy','configured_value')
    return sorted([{key:row.get(key) for key in keys} for row in rows if row.get('enabled',1)],
                  key=lambda row:(str(row['model_kind']),str(row['parameter_id'])))


def make_contract(schema,business,model_sha,price_output):
    model=model_schema(schema)
    fields=business_schema(business['data'])
    definitions={row['parameter_id']:row for row in fields}
    for row in model['fields']:
        definition=definitions.get(row['field_name'])
        if not definition:
            raise ValueError('Model field missing from published business: '+row['field_name'])
        if row['unit'] and definition['unit'] and row['unit']!=definition['unit']:
            raise ValueError('Model/business unit mismatch: '+row['field_name'])
        actual,expected=definition['value_type'],row['dtype']
        numeric={'number','integer','boolean','ip_grade'}
        if actual!=expected and not {actual,expected}<=numeric:
            raise ValueError('Model/business type mismatch: '+row['field_name'])
    return {'format':'pipeline-delivery-contract-1.0','product_code':business['product_code'],
            'model_sha256':model_sha,'model_version':schema['model_version'],
            'model_schema':model,'model_schema_sha256':fingerprint(model),
            'business_schema_sha256':fingerprint(fields),'business_schema':fields,
            'business_source_release_id':business['source_release_id'],
            'business_package_payload_sha256':business['payload_sha256'],
            'price_basis':{'kind':'model_unit_price','raw_output':price_output,'normalized_unit':'wan_yuan'},
            'compatible_legacy':[]}


def load_contract(package,manifest):
    relative=manifest['release'].get('delivery_contract')
    if not relative or relative not in manifest['files']:
        raise ValueError('A sealed delivery compatibility contract is required')
    contract=read_json(contained(package,relative))
    if contract.get('format')!='pipeline-delivery-contract-1.0':
        raise ValueError('Unsupported delivery compatibility contract')
    if contract['model_sha256']!=manifest['files'][manifest['release']['price_model']]:
        raise ValueError('Delivery contract is not bound to the shipped model')
    if (fingerprint(contract['model_schema'])!=contract['model_schema_sha256'] or
        fingerprint(contract['business_schema'])!=contract['business_schema_sha256'] or
        contract['product_code']!=manifest['release']['product_code']):
        raise ValueError('Delivery contract fingerprints/product are inconsistent')
    return contract


def inspect_database(database):
    database=Path(database).resolve()
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as conn:
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
        if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise ValueError('User database integrity check failed')
        releases=[dict(row) for row in conn.execute("SELECT * FROM product_releases WHERE status='active'")]
        products=[dict(row) for row in conn.execute('SELECT * FROM products WHERE enabled=1')]
        if len(releases)!=1 or len(products)!=1 or releases[0]['product_code']!=products[0]['product_code']:
            raise ValueError('Exactly one matching active business release is required')
        release=releases[0]
        snapshot=json.loads(release['data_json'])
        live={'parameters':[dict(row) for row in conn.execute('SELECT * FROM parameter_definitions')]}
        live_schema=business_schema(live)
        if live_schema!=business_schema(snapshot):
            raise ValueError('Live business schema differs from the activated release; explicit migration required')
        bindings=[dict(row) for row in conn.execute('SELECT * FROM model_input_bindings')]
    return {'product_code':products[0]['product_code'],'active_release_id':release['release_id'],
            'active_release_payload_sha256':fingerprint(snapshot),
            'business_schema_sha256':fingerprint(live_schema),'binding_schema_sha256':fingerprint(binding_schema(bindings))}


def validate_data(package,data,manifest,contract=None):
    """Reject before Application/Store can migrate or sync an existing database."""
    package,data=Path(package),Path(data)
    contract=contract or load_contract(package,manifest)
    current=inspect_database(data/'data/protocol_demo.db')
    if current['product_code']!=contract['product_code'] or current['business_schema_sha256']!=contract['business_schema_sha256']:
        raise ValueError('Published business/model schema is incompatible with existing user data')
    settings=read_json(data/'config/model_services.json')
    output=settings.get('price_output',{'unit':'wan_yuan','scale':1.0})
    if output!=contract['price_basis']['raw_output']:
        raise ValueError('Price output unit/scale differs from the delivery contract; explicit migration required')
    state_path=data/'delivery_state.json'
    if state_path.exists():
        previous=read_json(state_path)
        for key in ('active_release_id','active_release_payload_sha256','business_schema_sha256','binding_schema_sha256'):
            if current[key]!=previous[key]:
                raise ValueError('Accepted business release/binding changed: '+key)
        if previous['model_schema_sha256']!=contract['model_schema_sha256'] or previous['price_basis']!=contract['price_basis']:
            raise ValueError('Replacement model schema or price basis is incompatible')
    else:
        # Legacy P4 has no model-schema state. Adopt only an explicitly checked
        # predecessor package, never infer compatibility from product_code.
        initialized=read_json(data/'runtime/initialized.json')
        installed=read_json(data/'installed.json') if (data/'installed.json').exists() else {}
        approved=initialized.get('delivery_contract_sha256')==fingerprint(contract) and not installed
        approved=approved or any(item['version']==installed.get('version') and
                     item['business_package_sha256']==initialized['business_package_sha256']
                     for item in contract['compatible_legacy'])
        if not approved:
            raise ValueError('Legacy userdata has no approved predecessor; validate its original package first')
    return dict(current,format='pipeline-userdata-contract-1.0',model_schema_sha256=contract['model_schema_sha256'],
                model_sha256=contract['model_sha256'],model_version=contract['model_version'],
                price_basis=contract['price_basis'],package_version=manifest['version'])


def accept_initial(data,manifest,contract):
    current=inspect_database(Path(data)/'data/protocol_demo.db')
    if current['business_schema_sha256']!=contract['business_schema_sha256']:
        raise ValueError('Seeded database differs from its delivery contract')
    return dict(current,format='pipeline-userdata-contract-1.0',model_schema_sha256=contract['model_schema_sha256'],
                model_sha256=contract['model_sha256'],model_version=contract['model_version'],
                price_basis=contract['price_basis'],package_version=manifest['version'])
