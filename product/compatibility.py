"""Read-only desktop upgrade contracts. Never imports a model or writes SQLite."""
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from product.common import read_json,write_json,contained


class StartupConfirmation(ValueError):
    """A business publication changed in a way the saved snapshot cannot classify."""

    def __init__(self, prompt, report):
        super(StartupConfirmation, self).__init__(prompt)
        self.prompt = prompt
        self.report = report


class StartupBlocked(ValueError):
    """The new model cannot use this database. Data stays unchanged."""

    def __init__(self, prompt, report=None):
        super(StartupBlocked, self).__init__(prompt)
        self.prompt = prompt
        self.report = report or {'change_class': 'incompatible'}


_PRESERVED = '\n现有用户数据未修改，也不会被删除。'
_PRESENTATION_KEYS = ('label', 'description', 'adjustment_hint', 'display_order', 'parameter_group',
                      'decimal_places', 'display_value_mapping', 'display_value_mapping_json')
_PRODUCT_PRESENTATION = ('product_name', 'product_description')


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


def _selected(fields, model_ids):
    rows = list(fields or [])
    if model_ids is None:
        return rows
    return [row for row in rows if row.get('parameter_id') in model_ids]


def calculation_schema(fields, model_ids=None):
    """Model-input identity: English id, type, unit and business-to-model encoding."""
    rows = []
    for row in _selected(fields, model_ids):
        rows.append({'parameter_id': row.get('parameter_id'), 'value_type': row.get('value_type'),
                     'unit': row.get('unit') or '', 'model_value_mapping': row.get('model_value_mapping') or {}})
    return sorted(rows, key=lambda row: str(row['parameter_id']))


def generation_schema(fields):
    """Search space. A change invalidates generation, but does not block startup."""
    rows = []
    for row in fields or []:
        rows.append({'parameter_id': row.get('parameter_id'), 'search_type': row.get('search_type') or 'auto',
                     'min_value': row.get('min_value'), 'max_value': row.get('max_value'),
                     'required': bool(row.get('required', False)),
                     'allowed_values': row.get('allowed_values') or [],
                     'special_value_keys': row.get('special_value_keys') or []})
    return sorted(rows, key=lambda row: str(row['parameter_id']))


def _whole(value):
    if value in (None, ''):
        return None
    return int(value)


def presentation_parameters(rows):
    fields = []
    for row in rows or []:
        if not row.get('enabled', 1):
            continue
        fields.append({'parameter_id': row.get('parameter_id'), 'label': row.get('label') or '',
                       'description': row.get('description') or '', 'adjustment_hint': row.get('adjustment_hint') or '',
                       'parameter_group': row.get('parameter_group') or '', 'display_order': _whole(row.get('display_order')),
                       'decimal_places': _whole(row.get('decimal_places')),
                       'display_value_mapping': decoded(row.get('display_value_mapping_json', row.get('display_value_mapping')), {})})
    return sorted(fields, key=lambda row: str(row['parameter_id']))


def presentation_products(rows):
    fields = [{'product_code': row.get('product_code') or '', 'product_name': row.get('product_name') or '',
               'product_description': row.get('product_description') or ''} for row in rows or []]
    return sorted(fields, key=lambda row: row['product_code'])


def substantive_document(document):
    """Release content with display names removed, so a rename is not a data change."""
    if not isinstance(document, dict):
        return document
    result = {}
    for key, value in document.items():
        if key == 'parameters' and isinstance(value, list):
            result[key] = [dict((item_key, item_value) for item_key, item_value in dict(item).items()
                                if item_key not in _PRESENTATION_KEYS) for item in value]
        elif key == 'products' and isinstance(value, list):
            result[key] = [dict((item_key, item_value) for item_key, item_value in dict(item).items()
                                if item_key not in _PRODUCT_PRESENTATION) for item in value]
        elif key in _PRODUCT_PRESENTATION:
            continue
        else:
            result[key] = value
    return result


def _schema_field_names(schema):
    return set(row.get('field_name') for row in (schema or {}).get('fields') or [] if row.get('field_name'))


def model_field_ids(contract):
    """Price inputs and effectiveness-only inputs. Display-only fields stay out."""
    return _schema_field_names(contract.get('model_schema')) | _schema_field_names(contract.get('effectiveness_schema'))


def _effect_sources(raw):
    features = []
    schema = raw.get('schema') if isinstance(raw.get('schema'), dict) else {}
    for source in (schema.get('features'), raw.get('features'), raw.get('feature_schema')):
        if isinstance(source, list) and source:
            features = source
            break
    bindings = {}
    for row in raw.get('model_input_bindings') or []:
        if isinstance(row, dict) and row.get('field_name'):
            bindings[row['field_name']] = row
    return features, bindings


def _effect_field(spec, binding):
    key = spec.get('field_name') or spec.get('key') or binding.get('field_name')
    return {'field_name': key,
            'dtype': spec.get('dtype') or spec.get('type') or binding.get('dtype') or 'number',
            'unit': spec.get('unit') if spec.get('unit') not in (None, '') else binding.get('unit') or '',
            'required': bool(spec.get('required', binding.get('required', True))),
            'missing_policy': spec.get('missing_policy') or binding.get('missing_policy') or 'reject',
            'allowed_values': spec.get('allowed_values') or binding.get('allowed_values') or [],
            'category_mapping': spec.get('category_mapping') or binding.get('category_mapping') or {},
            'source': spec.get('source') or binding.get('source_type') or '',
            'parser': spec.get('parser') or binding.get('parser') or ''}


def effectiveness_schema_from_document(raw):
    """Read V3 features or a V4 feature_schema. A bundle with no fields is an error."""
    if not isinstance(raw, dict):
        raise ValueError('效能模型没有可校验的输入字段')
    features, bindings = _effect_sources(raw)
    fields = []
    seen = set()
    for spec in features:
        if not isinstance(spec, dict):
            continue
        key = spec.get('field_name') or spec.get('key')
        if not key or key in seen:
            continue
        seen.add(key)
        fields.append(_effect_field(spec, bindings.get(key) or {}))
    if not fields:
        for key, binding in sorted(bindings.items()):
            if key in seen:
                continue
            seen.add(key)
            fields.append(_effect_field(binding, binding))
    if not fields:
        raise ValueError('效能模型没有可校验的输入字段')
    order = [row['field_name'] for row in fields]
    if len(order) != len(set(order)):
        raise ValueError('Effectiveness schema has duplicate fields')
    return {'feature_order': order, 'fields': sorted(fields, key=lambda row: row['field_name'])}


def binding_ids(rows):
    return set(row.get('parameter_id') for row in rows or [] if row.get('enabled', 1) != 0 and row.get('parameter_id'))


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


def price_output(config):
    unit=str(config.get('unit') or 'wan_yuan').strip().lower()
    scale=float(config.get('scale',1.0))
    if unit not in ('yuan','thousand_yuan','wan_yuan','million_yuan') or not math.isfinite(scale) or scale<=0:
        raise ValueError('Invalid price output unit/scale')
    return {'unit':unit,'scale':scale}


def check_data_root(data):
    data=Path(data)
    if (data/'runtime/restore_pending.json').exists():
        raise ValueError('Interrupted restore detected; restore the backup into a new empty directory')
    if not (data/'data/protocol_demo.db').exists() and any((data/name).exists() for name in
                  ('installed.json','delivery_state.json','runtime/initialized.json')):
        raise ValueError('Initialized installation has lost its database; restore a backup instead of reseeding')


def _check_model_fields(model, definitions):
    numeric = {'number', 'integer', 'boolean', 'ip_grade'}
    for row in model['fields']:
        definition = definitions.get(row['field_name'])
        if not definition:
            raise ValueError('Model field missing from published business: ' + row['field_name'])
        if row.get('unit') and definition['unit'] and row['unit'] != definition['unit']:
            raise ValueError('Model/business unit mismatch: ' + row['field_name'])
        actual, expected = definition['value_type'], row.get('dtype')
        if expected and actual != expected and not {actual, expected} <= numeric:
            raise ValueError('Model/business type mismatch: ' + row['field_name'])


def make_contract(schema,business,model_sha,price_output,effectiveness_schema=None,effectiveness_model_sha=None):
    model=model_schema(schema)
    fields=business_schema(business['data'])
    definitions={row['parameter_id']:row for row in fields}
    _check_model_fields(model, definitions)
    contract={'format':'pipeline-delivery-contract-1.0','product_code':business['product_code'],
            'model_sha256':model_sha,'model_version':schema['model_version'],
            'model_schema':model,'model_schema_sha256':fingerprint(model),
            'business_schema_sha256':fingerprint(fields),'business_schema':fields,
            'business_source_release_id':business['source_release_id'],
            'business_package_payload_sha256':business['payload_sha256'],
            'price_basis':{'kind':'model_unit_price','raw_output':price_output,'normalized_unit':'wan_yuan'},
            'calculation_version':2,
            'compatible_legacy':[]}
    if effectiveness_schema is not None:
        if isinstance(effectiveness_schema, dict) and 'fields' in effectiveness_schema and 'feature_order' in effectiveness_schema:
            effect=effectiveness_schema
        else:
            effect=effectiveness_schema_from_document(effectiveness_schema)
        _check_model_fields(effect, definitions)
        contract['effectiveness_schema']=effect
        contract['effectiveness_schema_sha256']=fingerprint(effect)
        if effectiveness_model_sha:
            contract['effectiveness_model_sha256']=effectiveness_model_sha
    return contract


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
    if contract.get('effectiveness_schema') is not None and fingerprint(contract['effectiveness_schema'])!=contract.get('effectiveness_schema_sha256'):
        raise ValueError('Delivery contract fingerprints/product are inconsistent')
    return contract


def _snapshot_from(release_id, document, live_rows, published_schema, live_schema, price_ids, effect_ids):
    union=set(price_ids or [])|set(effect_ids or [])
    compared=union or None
    return {'release_id': release_id,
            'payload_sha256': fingerprint(document),
            'calculation_version': 2,
            'calculation': calculation_schema(live_schema, compared),
            'price_calculation': calculation_schema(live_schema, price_ids or None),
            'effectiveness_calculation': calculation_schema(live_schema, effect_ids) if effect_ids else [],
            'generation': generation_schema(live_schema),
            'presentation': {'parameters': presentation_parameters(live_rows),
                             'products': presentation_products(document.get('products') if isinstance(document, dict) else [])},
            'release_presentation': {'parameters': presentation_parameters(document.get('parameters') if isinstance(document, dict) else []),
                                     'products': presentation_products(document.get('products') if isinstance(document, dict) else [])},
            'substantive_sha256': fingerprint(substantive_document(document)),
            'published_calculation': calculation_schema(published_schema, compared)}


def inspect_database(database, model_ids=None, price_ids=None, effect_ids=None):
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
        document=json.loads(release['data_json'])
        if not isinstance(document, dict):
            raise ValueError('Activated release payload is not a business document')
        live_rows=[dict(row) for row in conn.execute('SELECT * FROM parameter_definitions')]
        live_schema=business_schema({'parameters': live_rows})
        published_schema=business_schema(document)
        bindings=[dict(row) for row in conn.execute('SELECT * FROM model_input_bindings')]
        if price_ids is None and effect_ids is None:
            price_ids=model_ids if model_ids else binding_ids(bindings)
            effect_ids=set()
        price_ids=set(price_ids or [])
        effect_ids=set(effect_ids or [])
        compared=price_ids|effect_ids or None
        if calculation_schema(live_schema, compared)!=calculation_schema(published_schema, compared):
            raise ValueError('Live business schema differs from the activated release; explicit migration required'+_PRESERVED)
        digest=fingerprint(substantive_document(document))
        audited=_release_audited(conn, release['release_id'], digest)
    snapshot=_snapshot_from(release['release_id'], document, live_rows, published_schema, live_schema, price_ids, effect_ids)
    return {'product_code':products[0]['product_code'],'active_release_id':release['release_id'],
            'active_release_payload_sha256':fingerprint(document),
            'business_schema_sha256':fingerprint(live_schema),'binding_schema_sha256':fingerprint(binding_schema(bindings)),
            'live_schema':live_schema,'content_snapshot':snapshot,
            'release_document':document,'release_audited':audited,
            'integrity_errors':release_integrity_errors(document, compared)}


def _release_audited(conn, release_id, substantive_sha256):
    """An old activate row does not approve content that was edited afterwards."""
    names=set(row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    if 'audit_log' not in names:
        return False
    rows=conn.execute("SELECT detail_json FROM audit_log WHERE object_type=? AND object_id=? AND action IN ('activate','update')",
                      ('product_release', str(release_id))).fetchall()
    for row in rows:
        try:
            detail=json.loads(row[0] or '{}')
        except (TypeError, ValueError):
            detail={}
        if isinstance(detail, dict) and detail.get('substantive_sha256')==substantive_sha256:
            return True
    return False


def release_integrity_errors(document, model_ids):
    """Structure only. A matching hash is not proof that the publication is valid."""
    errors=[]
    parameters=document.get('parameters') if isinstance(document, dict) else None
    if not isinstance(parameters, list):
        return ['missing-parameters']
    ids=[]
    for row in parameters:
        if not isinstance(row, dict) or not row.get('parameter_id'):
            errors.append('missing-parameter-id')
            continue
        ids.append(row['parameter_id'])
    if len(ids)!=len(set(ids)):
        errors.append('duplicate-parameter-id')
    known=set(ids)
    for key in sorted(model_ids or []):
        if key not in known:
            errors.append('model-field-missing:'+str(key))
    for item in document.get('agreements') or []:
        params=item.get('params') if isinstance(item, dict) else None
        if not isinstance(params, dict):
            errors.append('bad-agreement')
            continue
        if any(key not in known for key in params):
            errors.append('unknown-agreement-field')
    return errors


def _runtime_code(value, mapping):
    """Map a business value when a code exists. Otherwise the value itself is the input."""
    if isinstance(mapping, dict) and mapping:
        text=str(value).strip()
        if text in mapping:
            return mapping[text]
        if str(value) in mapping:
            return mapping[str(value)]
    return value


def _accepted_codes(spec):
    codes=set()
    for item in spec.get('allowed_values') or []:
        codes.add(item)
        codes.add(str(item).strip())
    categories=spec.get('category_mapping') or {}
    if isinstance(categories, dict):
        for key, val in categories.items():
            codes.add(key)
            codes.add(str(key).strip())
            codes.add(val)
            codes.add(str(val).strip())
    return codes


def _code_accepted(code, spec):
    accepted=_accepted_codes(spec)
    if accepted:
        return code in accepted or str(code).strip() in accepted
    dtype=str(spec.get('dtype') or '')
    if dtype in ('number', 'integer', 'float', 'boolean', 'ip_grade'):
        if isinstance(code, bool):
            return dtype in ('boolean', 'integer', 'number')
        if isinstance(code, (int, float)):
            return True
        try:
            float(str(code).strip())
            return True
        except (TypeError, ValueError):
            return False
    return True


def encoding_gaps(live_fields, contract, model_ids):
    """Check the value each model will actually receive, for price and effectiveness separately."""
    specs={}
    for source in (contract.get('effectiveness_schema'), contract.get('model_schema')):
        for row in (source or {}).get('fields') or []:
            specs.setdefault(row.get('field_name'), []).append(row)
    by_id={row.get('parameter_id'): row for row in live_fields or []}
    gaps=[]
    for key in sorted(model_ids or []):
        business=by_id.get(key) or {}
        mapping=business.get('model_value_mapping') or {}
        allowed=business.get('allowed_values') or []
        for spec in specs.get(key) or []:
            for value in allowed:
                code=_runtime_code(value, mapping)
                if not _code_accepted(code, spec):
                    gaps.append('%s:%s->%s'%(key, value, code))
        if any(bool(spec.get('required')) for spec in specs.get(key) or []) and not business.get('required'):
            gaps.append(key+':required')
    return gaps


def _incompatible(contract, current, data):
    raise StartupBlocked('当前运行包与已有用户数据不兼容，已停止启动以保护数据。'
                     '\n运行包产品：'+str(contract['product_code'])+
                     '\n已有数据产品：'+str(current['product_code'])+
                     '\n用户数据目录：'+str(data)+
                     '\n请使用匹配的原版本；独立测试请通过 --data-dir 指定新的空目录。'
                     '\n模型输入字段、类型、单位或编码不一致时不能继续计算。'+_PRESERVED)


def _calculation_changed(previous, current, contract):
    """Old startup files listed only price fields. Adding effectiveness fields is a rule upgrade."""
    snapshot=previous.get('content_snapshot') or {}
    stored=snapshot.get('calculation')
    if stored is None:
        return False
    price_ids=_schema_field_names(contract.get('model_schema'))
    effect_ids=_schema_field_names(contract.get('effectiveness_schema'))
    current_price=calculation_schema(current['live_schema'], price_ids or None)
    current_effect=calculation_schema(current['live_schema'], effect_ids) if effect_ids else []
    if int(snapshot.get('calculation_version') or 1)<2:
        stored_ids=set(row.get('parameter_id') for row in stored if isinstance(row, dict))
        return calculation_schema(current['live_schema'], stored_ids)!=stored
    if snapshot.get('price_calculation') not in (None, current_price):
        return True
    if effect_ids and snapshot.get('effectiveness_calculation') not in (None, current_effect):
        return True
    return False


def _classify(previous, current):
    stored=previous.get('content_snapshot') or {}
    current_snapshot=current['content_snapshot']
    substantive=stored.get('substantive_sha256')!=current_snapshot['substantive_sha256']
    generation=stored.get('generation')!=current_snapshot['generation']
    presentation=(stored.get('presentation')!=current_snapshot['presentation'] or
                  stored.get('release_presentation')!=current_snapshot['release_presentation'])
    payload_changed=stored.get('payload_sha256')!=current_snapshot['payload_sha256']
    explained=substantive or generation or presentation or not payload_changed
    release_changed=stored.get('release_id')!=current_snapshot['release_id']
    if release_changed and not (substantive or generation or presentation):
        raise ValueError('Accepted business release/binding changed: active_release_id'+_PRESERVED)
    if payload_changed and not explained:
        report={'change_class':'unclassified','release_id':current_snapshot['release_id'],
                'payload_sha256':current_snapshot['payload_sha256']}
        raise StartupConfirmation('检测到业务发布内容变化，但保存的契约快照无法把它分成展示、生成或业务内容。'
                                  '\n可以选择先备份，再把当前数据库记为新的已接受基线。'
                                  '\n不会删除用户数据，也不能用删除整个目录作为修复。', report)
    if substantive:
        if current.get('integrity_errors'):
            raise StartupBlocked('业务发布内容不完整或引用了未知字段，不能接受为新的启动基线。'+_PRESERVED+
                                 '\n'+'、'.join(current.get('integrity_errors') or []),
                                 {'change_class':'substantive_invalid','errors':current.get('integrity_errors')})
        if not current.get('release_audited'):
            raise StartupConfirmation('业务发布内容有变化，但没有管理界面的保存记录。请先确认这是你自己的修改。'
                                      '\n确认后会先备份，再把它记为新的基线。不会删除用户数据。',
                                      {'change_class':'substantive_unreviewed','release_id':current_snapshot['release_id']})
        change_class='substantive'
    elif generation:
        change_class='generation'
    elif presentation:
        change_class='presentation'
    elif ((previous.get('model_sha256') and previous.get('model_sha256')!=current.get('model_sha256')) or
          (previous.get('effectiveness_model_sha256') and current.get('effectiveness_model_sha256') and
           previous.get('effectiveness_model_sha256')!=current.get('effectiveness_model_sha256'))):
        change_class='model_weights'
    else:
        change_class='none'
    action='backup' if change_class in ('generation','substantive') else 'start'
    return change_class, action


def _legacy_state(previous, current):
    if current['active_release_id']!=previous.get('active_release_id'):
        raise ValueError('Accepted business release/binding changed: active_release_id'+_PRESERVED)
    if current['business_schema_sha256']!=previous.get('business_schema_sha256'):
        raise ValueError('Accepted business release/binding changed: business_schema_sha256'+_PRESERVED)
    if current['binding_schema_sha256']!=previous.get('binding_schema_sha256'):
        raise ValueError('Accepted business release/binding changed: binding_schema_sha256'+_PRESERVED)
    if current['active_release_payload_sha256']!=previous.get('active_release_payload_sha256'):
        report={'change_class':'unclassified','release_id':current['active_release_id'],
                'payload_sha256':current['active_release_payload_sha256']}
        raise StartupConfirmation('检测到业务发布指纹变化。旧的启动状态只保存了整包指纹，不能判断是中文名称还是历史协议被修改。'
                                  '\n可以先备份，再把当前数据库记为新的已接受基线。不会删除用户数据。', report)


def _public_state(current, manifest, contract, change_class, action):
    return {'product_code':current['product_code'],'active_release_id':current['active_release_id'],
            'active_release_payload_sha256':current['active_release_payload_sha256'],
            'business_schema_sha256':current['business_schema_sha256'],
            'binding_schema_sha256':current['binding_schema_sha256'],
            'content_snapshot':current['content_snapshot'],
            'format':'pipeline-userdata-contract-1.0','model_schema_sha256':contract['model_schema_sha256'],
            'model_sha256':contract['model_sha256'],'model_version':contract['model_version'],
            'effectiveness_schema_sha256':contract.get('effectiveness_schema_sha256'),
            'effectiveness_model_sha256':contract.get('effectiveness_model_sha256'),
            'price_basis':contract['price_basis'],'package_version':manifest['version'],
            'change_class':change_class,'startup_action':action}


def validate_data(package,data,manifest,contract=None):
    """Reject unsafe model use before Application/Store can sync an existing database."""
    package,data=Path(package),Path(data)
    check_data_root(data)
    contract=contract or load_contract(package,manifest)
    price_ids=_schema_field_names(contract.get('model_schema'))
    effect_ids=_schema_field_names(contract.get('effectiveness_schema'))
    ids=price_ids|effect_ids
    current=inspect_database(data/'data/protocol_demo.db', price_ids=price_ids, effect_ids=effect_ids)
    if current['product_code']!=contract['product_code'] or calculation_schema(current['live_schema'], ids or None)!=calculation_schema(contract['business_schema'], ids or None):
        _incompatible(contract, current, data)
    settings=read_json(data/'config/model_services.json')
    output=price_output(settings.get('price_output') or {})
    if output!=contract['price_basis']['raw_output']:
        raise StartupBlocked('Price output unit/scale differs from the delivery contract; explicit migration required'+_PRESERVED)
    state_path=data/'delivery_state.json'
    change_class, action='none', 'start'
    if state_path.exists():
        previous=read_json(state_path)
        if previous.get('model_schema_sha256')!=contract['model_schema_sha256'] or previous.get('price_basis')!=contract['price_basis']:
            raise StartupBlocked('Replacement model schema or price basis is incompatible'+_PRESERVED)
        if previous.get('effectiveness_schema_sha256') not in (None, contract.get('effectiveness_schema_sha256')):
            raise StartupBlocked('Effectiveness model schema is incompatible'+_PRESERVED)
        if current['binding_schema_sha256']!=previous.get('binding_schema_sha256'):
            raise StartupBlocked('Accepted business release/binding changed: binding_schema_sha256'+_PRESERVED)
        if _calculation_changed(previous, current, contract):
            raise StartupBlocked('模型输入契约与上次接受的用户数据不一致。'+_PRESERVED+
                             '\n没有可靠迁移规则时不会改写数据库。请保留该目录，或用 --data-dir 指定另一个空目录做独立试验。')
        gaps=encoding_gaps(current['live_schema'], contract, ids)
        if gaps:
            raise StartupBlocked('新增的业务取值还不能交给当前模型计算：'+'、'.join(gaps)+_PRESERVED)
        if previous.get('content_snapshot'):
            change_class, action=_classify(
                previous,
                dict(current, model_sha256=contract['model_sha256'],
                     effectiveness_model_sha256=contract.get('effectiveness_model_sha256')))
        else:
            _legacy_state(previous, current)
            if previous.get('model_sha256') and previous.get('model_sha256')!=contract['model_sha256']:
                change_class='model_weights'
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
    return _public_state(current, manifest, contract, change_class, action)


def accept_initial(data,manifest,contract):
    price_ids=_schema_field_names(contract.get('model_schema'))
    effect_ids=_schema_field_names(contract.get('effectiveness_schema'))
    ids=price_ids|effect_ids
    current=inspect_database(Path(data)/'data/protocol_demo.db', price_ids=price_ids, effect_ids=effect_ids)
    if calculation_schema(current['live_schema'], ids or None)!=calculation_schema(contract['business_schema'], ids or None):
        raise ValueError('Seeded database differs from its delivery contract'+_PRESERVED)
    return _public_state(current, manifest, contract, 'none', 'start')
