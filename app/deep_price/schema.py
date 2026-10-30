import json
import math
import re

from .errors import DeepPriceError


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
        if kind not in ('number', 'integer', 'enum', 'boolean', 'string'):
            raise DeepPriceError('unavailable', '未知业务字段类型：' + key, 503)
        group = {'功能': 'function', '性能': 'performance', '环境': 'environment'}.get(definition.get('parameter_group'), 'other')
        field = {'field_id': key, 'label_zh': definition['label'], 'unit': unit, 'type': kind,
                 'required': bool(spec.get('required', definition.get('required'))) if key in specs else False,
                 'allowed_values': parsed(definition.get('allowed_values_json'), []),
                 'model_value_mapping': parsed(definition.get('model_value_mapping_json'), {}),
                 'group': group, 'price_model_input': key in specs, 'additive_rule_id': None}
        for source, target in [('min_value', 'minimum'), ('max_value', 'maximum')]:
            if definition.get(source) is not None:
                field[target] = definition[source]
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
