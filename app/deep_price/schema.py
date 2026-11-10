import hashlib
import json
import math
import re

from .errors import DeepPriceError

ANALYSIS_PROTOCOL = 'permutation-mae-20261102'


def parsed(value, default):
    if value in (None, ''):
        return default
    if isinstance(value, type(default)):
        return value
    return json.loads(value)


def fields_for(snapshot, schema):
    specs = {str(s.get('field_name') or s.get('key')): s for s in schema.get('fields', [])}
    definitions = {d['parameter_id']: d for d in snapshot.definitions}
    missing = set(specs) - set(definitions)
    if missing:
        raise DeepPriceError('unavailable', '模型字段缺少业务定义：' + ', '.join(sorted(missing)), 503)
    result = []
    for key, definition in definitions.items():
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise DeepPriceError('unavailable', '业务字段ID不满足英文稳定标识契约', 503)
        spec = specs.get(key, {})
        unit = definition.get('unit') or spec.get('unit') or ''
        if definition.get('unit') and spec.get('unit') and definition['unit'] != spec['unit']:
            raise DeepPriceError('unavailable', '业务/模型字段单位不一致：' + key, 503)
        kind = definition.get('value_type') or 'number'
        kind = {'float': 'number', 'bool': 'boolean', 'ip_grade': 'integer'}.get(kind, kind)
        if kind=='number' and definition.get('search_type')=='integer':
            kind='integer'
        if kind not in ('number', 'integer', 'enum', 'boolean', 'string'):
            raise DeepPriceError('unavailable', '未知业务字段类型：' + key, 503)
        group_label = definition.get('parameter_group') or '其他'
        group = {'功能':'function','功能参数':'function','function':'function',
                 '性能':'performance','性能参数':'performance','performance':'performance',
                 '环境':'environment','环境参数':'environment','environment':'environment'}.get(group_label,'other')
        field = {'field_id': key, 'label_zh': definition['label'], 'unit': unit, 'type': kind,
                 'required': bool(spec.get('required', definition.get('required'))) if key in specs else False,
                 'allowed_values': parsed(definition.get('allowed_values_json'), []),
                 'model_value_mapping': parsed(definition.get('model_value_mapping_json'), {}),
                 'group': group, 'group_label':group_label, 'price_model_input': key in specs, 'additive_rule_id': None}
        for source, target in [('min_value', 'minimum'), ('max_value', 'maximum')]:
            if definition.get(source) is not None:
                field[target] = definition[source]
        field['analysis_supported']=bool(key in specs and (field['type'] in ('number','integer','boolean') or field['allowed_values']))
        field['analysis_unavailable_reason']=None if field['analysis_supported'] else '自由文本或未提供枚举合法取值，不能执行单属性扰动'
        if field['allowed_values'] and spec.get('allowed_values'):
            from app.value_semantics import values_equal
            supported=list(spec['allowed_values'])+list((spec.get('category_mapping') or {}).values())
            legal=[v for v in field['allowed_values'] if any(values_equal(snapshot.codec.runtime_parameters({key:v}).get(key),candidate) for candidate in supported)]
            field['analysis_values']=legal
            if not legal:field.update(analysis_supported=False,analysis_unavailable_reason='当前模型不支持业务字典的这些取值')
        field['missing_policy']=spec.get('missing_policy','service_owned')
        result.append(field)
    return result, specs


def encode(snapshot, parameters, fields, specs):
    if not isinstance(parameters, dict):
        raise DeepPriceError('invalid_input', 'parameters必须为对象')
    by_key = {f['field_id']: f for f in fields}
    if set(parameters) - set(by_key):
        raise DeepPriceError('invalid_input', '包含未知或未启用字段')
    for key, value in parameters.items():
        if isinstance(value, (list, dict)) or isinstance(value, float) and not math.isfinite(value):
            raise DeepPriceError('invalid_input', '属性必须是有限标量：' + key)
    business = snapshot.codec.canonical_business_parameters(parameters)
    for key, value in business.items():
        f = by_key[key]
        # Missing policies, including sentinel values such as -1, remain owned
        # by the price service. Do not replace missing inputs here.
        if value in (None, ''):
            continue
        allowed = f['allowed_values']
        if allowed and value not in allowed:
            raise DeepPriceError('invalid_input', '属性值不在业务允许值中：' + key)
        if f['type'] in ('number', 'integer'):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise DeepPriceError('invalid_input', '属性须为有限数值：' + key)
            if f['type'] == 'integer' and int(value) != value:
                raise DeepPriceError('invalid_input', '属性须为整数：' + key)
        if f['type'] == 'boolean' and value not in (True, False, 0, 1) and not allowed:
            raise DeepPriceError('invalid_input', '属性须为布尔值：' + key)
        if f['type'] == 'string' and not isinstance(value, str):
            raise DeepPriceError('invalid_input', '属性须为文本：' + key)
        # Business engineering ranges are enforced for numeric inputs. Model
        # generation/training ranges are diagnostic and are not validation bounds.
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value != -1:
            if 'minimum' in f and value < f['minimum'] or 'maximum' in f and value > f['maximum']:
                raise DeepPriceError('invalid_input', '属性超出业务范围：' + key)
    encoded = snapshot.codec.runtime_parameters(business)
    return business, {key: value for key, value in encoded.items() if key in specs}

def _as_mapping(value):
    if isinstance(value, str) and value:
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def document_tables(data):
    """Accept either a release document or a wrapped business package."""
    if isinstance(data, dict) and 'parameters' not in data and isinstance(data.get('data'), dict):
        data = data['data']
    data = data or {}
    return data.get('parameters') or [], data.get('agreements') or []


def analysis_binding_signature(parameters, agreements, sample_ids, field_ids, model_sha):
    """Bind importance to encoded sample inputs, not Chinese display names."""
    definitions = {row.get('parameter_id'): row for row in parameters or []}
    wanted = set(sample_ids or [])
    rows = [row for row in agreements or [] if row.get('agreement_id') in wanted]
    if len(rows) != len(wanted):
        return None
    rows.sort(key=lambda row: str(row.get('agreement_id')))
    encoded = []
    for row in rows:
        values = dict(row.get('params') or {})
        coded = {}
        for key in field_ids or []:
            definition = definitions.get(key) or {}
            mapping = _as_mapping(definition.get('model_value_mapping_json') or definition.get('model_value_mapping') or {})
            value = values.get(key)
            if mapping:
                value = mapping.get(str(value), value)
            coded[key] = value
        encoded.append({'agreement_id': row.get('agreement_id'),
                        'historical_price_wan': row.get('historical_price_wan'), 'inputs': coded})
    payload = {'protocol': ANALYSIS_PROTOCOL, 'model_sha256': model_sha, 'samples': encoded}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _overlay_labels(importance, parameters):
    definitions = {row.get('parameter_id'): row for row in parameters or []}
    for item in importance.get('items') or []:
        label = (definitions.get(item.get('field_id')) or {}).get('label')
        if label:
            item['label_zh'] = label


def bound_metadata(metadata,snapshot,model_sha):
    import copy
    result=copy.deepcopy(metadata or {})
    value=result.get("feature_importance")
    if isinstance(value,dict) and value.get("kind")=="permutation_mae_increase":
        data=json.loads(snapshot.release["data_json"])
        parameters, agreements = document_tables(data)
        if value.get('analysis_signature'):
            keys = value.get('analysis_keys') or [item.get('field_id') for item in value.get('items') or []]
            observed = analysis_binding_signature(parameters, agreements, value.get('sample_ids'), keys, model_sha)
            mismatched = value.get('model_sha256') != model_sha or observed != value.get('analysis_signature')
        else:
            observed=hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode("utf-8")).hexdigest()
            mismatched = value.get("model_sha256")!=model_sha or value.get("business_data_sha256")!=observed
        if mismatched:
            result["feature_importance"]=None
            result["analysis_unavailable_reason"]="重要度与当前模型或分析样本不匹配"
        else:
            live = getattr(snapshot, 'definitions', None)
            _overlay_labels(value, live or parameters)
    return result
