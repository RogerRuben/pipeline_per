# -*- coding: utf-8 -*-
"""DS integration: one file, Python 3.8+, standard library only.

from ds_model import load_model
model = load_model("DS_model.json")
output_json = model.predict_json(input_json)

Generated from the unchanged frozen DS API/engine plus a small public facade.
Rebuild with tools/build_sdk.py; do not edit the generated engine sections.
"""


# ---- ds_attributes.py ----
# -*- coding: utf-8 -*-
"""Typed expert pricing. Unpriced columns remain observations, never implicit zeros."""
import math

TYPES = ('布尔', '数值', '等级', '类别', '文本')
METHODS = ('开关加价', '按单位计价', '按取值查价', '按传感器计价', '仅记录')


def text(value):
    return '' if value is None else str(value).strip()


def typed(value, kind, name, allow_missing=False):
    if value is None or value == '':
        if allow_missing: return None
        raise ValueError(name + '缺少属性值；不按0处理')
    if kind == '布尔':
        if value in ('是','有',True,1,'1'): return 1
        if value in ('否','无',False,0,'0'): return 0
        raise ValueError(name + '是布尔属性，请填是/否或0/1')
    if kind == '数值':
        if isinstance(value, bool): raise ValueError(name + '必须填数值')
        try: v = float(value)
        except (ValueError, TypeError): raise ValueError(name + '必须填数值')
        if not math.isfinite(v): raise ValueError(name + '必须是有限数值')
        return v
    return text(value)


def parse_catalog(rows, price_parser, scale, core_names):
    catalog, observations = {}, {}
    for row in rows:
        name = text(row['属性名称'])
        if name in core_names: raise ValueError(name+'与核心字段冲突，不在属性价格中重复计价')
        kind = text(row['属性类型'])
        method = text(row['计价方式']) or '仅记录'
        if kind not in TYPES or method not in METHODS: raise ValueError(name+'属性类型或计价方式选项不正确')
        if method == '仅记录':
            if name in catalog or name in observations: raise ValueError(name+'仅记录字段不能重复或同时计价')
            if any(row[k] not in (None,'') for k in ('参考金额','下限','上限','成品计价系数','取值')):
                raise ValueError(name+'选了仅记录却填写价格或取值，请选明确计价方式，或清空这些值')
            observations[name] = kind
            continue
        if name in observations: raise ValueError(name+'不能既仅记录又计价')
        if method == '开关加价' and kind != '布尔': raise ValueError(name+'开关加价只用于布尔属性')
        if method == '按传感器计价' and kind != '布尔': raise ValueError(name+'按传感器计价只用于是/否属性')
        if method == '按单位计价' and kind != '数值': raise ValueError(name+'按单位计价只用于有明确单位价的数值属性')
        if method == '按取值查价' and kind not in ('数值','等级','类别'):
            raise ValueError(name+'查价用于数值/等级/类别，文本只作记录')
        if method not in ('按取值查价','按传感器计价') and row['取值'] not in (None,''):
            raise ValueError(name+'只有按取值查价或按传感器计价才填写取值')
        layer = text(row['所属层'])
        if layer not in ('第二层','第三层'): raise ValueError(name+'计价属性须选第二层或第三层')
        p = price_parser(row, '属性名称', '参考金额', scale, 2 if layer == '第二层' else 3)
        match = typed(row['取值'], kind, name+'查价取值') if method == '按取值查价' else None
        if method == '按传感器计价':
            match = text(row['取值'])
            if not match: raise ValueError(name+'按传感器计价须在取值列填写传感器型号')
        if name not in catalog:
            catalog[name] = dict(name=name, type=kind, method=method, layer=p['layer'], rules=[])
        spec = catalog[name]
        if (spec['type'],spec['method'],spec['layer']) != (kind,method,p['layer']):
            raise ValueError(name+'同一属性各行的类型、计价方式、层级必须一致')
        if spec['rules'] and method not in ('按取值查价','按传感器计价'): raise ValueError(name+'非查价属性只能一行')
        if any(r['match'] == match for r in spec['rules']): raise ValueError(name+'查价取值重复')
        spec['rules'].append(dict(p,match=match))
    return catalog, observations


def parse_values(row, catalog, observations, sid):
    values = {}
    for name, spec in catalog.items():
        kind = spec.get('type','布尔')
        v = typed(row.get(name),kind,sid+'的'+name)
        if spec.get('method') == '按单位计价' and v < 0:
            raise ValueError(sid+'的'+name+'按单位计价须非负；负温度等性能值应查价或仅记录')
        if spec.get('method') == '按取值查价' and not any(r['match'] == v for r in spec['rules']):
            raise ValueError(sid+'的'+name+'取值“'+str(v)+'”没有对应专家价；请补查价行或明确改为仅记录，不默认0')
        if spec.get('method') == '按传感器计价' and v:
            sensor_rule(spec, text(row.get('传感器型号')), sid)
        values[name] = v
    extra = {name:typed(row.get(name),kind,sid+'的'+name,True) for name,kind in observations.items()}
    return values, extra


def prices(spec):
    return spec.get('rules',[spec])


def sensor_rule(spec, sensor_type, sid=''):
    rule = next((r for r in spec['rules'] if r['match'] == sensor_type), None)
    if rule is None:
        raise ValueError(sid+'的'+spec['name']+'已启用，但传感器型号“'+sensor_type+
                         '”没有每只附加价；请在属性价格表补充该型号规则，不默认0')
    return rule


def charge(spec, value, endpoint, context=None):
    method = spec.get('method','开关加价')
    if method == '按传感器计价':
        if context is None: raise ValueError(spec['name']+'按传感器计价需要本样本的型号和数量')
        sensor_type = context['sensor_type']
        # Disabled features require no price knowledge for an unused surcharge.
        if value:
            p = sensor_rule(spec, sensor_type, context.get('id',''))
            multiplier = context['count']
        else:
            p = next((r for r in spec['rules'] if r['match'] == sensor_type), None)
            multiplier = 0
        unit = p[endpoint]*p['factor'] if p is not None else None
        return dict(layer=spec['layer'],name=spec['name'],quantity=multiplier,unit_price=unit,
                    amount=multiplier*unit if value else 0.,input_value=value,pricing_method=method,
                    sensor_type=sensor_type,sensor_count=context['count'],
                    pricing_note='每只额外加价×传感器数量' if value else '未启用，不计附加费')
    if method == '按取值查价':
        p = next(r for r in spec['rules'] if r['match'] == value)
        multiplier = 1.
    else:
        p = prices(spec)[0]
        multiplier = value
    unit = p[endpoint]*p['factor']
    return dict(layer=spec['layer'],name=spec['name'],quantity=multiplier,unit_price=unit,
                amount=multiplier*unit,input_value=value,pricing_method=method)


# ---- ds_core.py ----
# -*- coding: utf-8 -*-
"""DS V1: one shared intercept, fixed expert component prices, honest LOO."""
import copy
import math

VERSION = 'ds-price-v1.2'


def mean(values):
    return sum(values) / len(values)


def component(row, catalog, endpoint='value'):
    sensor = catalog['sensors'][row['sensor_type']]
    sensor_amount = row['count'] * sensor[endpoint] * sensor['factor']
    details = [dict(layer=1, name='传感器:' + row['sensor_type'], quantity=row['count'],
                    unit_price=sensor[endpoint] * sensor['factor'], amount=sensor_amount)]
    layers = {2: 0., 3: 0.}
    for name, spec in catalog['features'].items():
        detail = charge(spec, row['features'][name], endpoint, row)
        layers[spec['layer']] += detail['amount']
        details.append(detail)
    return dict(sensor=sensor_amount, stage2=layers[2], stage3=layers[3],
                known=sensor_amount + layers[2] + layers[3], details=details)


def calibrate(rows, catalog, endpoint='value'):
    if not rows:
        return None
    # No nonnegative clipping: a negative remainder must remain visible as a conflict.
    return mean([r['actual'] - component(r, catalog, endpoint)['known'] for r in rows])


def predict_row(row, catalog, base, endpoint='value'):
    c = component(row, catalog, endpoint)
    valid = base is not None and base >= 0 and math.isfinite(base)
    total = c['known'] + base if valid else None
    y = row['actual']
    signed = (total - y) / y * 100 if total is not None and y is not None else None
    return dict(sample_id=row['id'], actual=y, sensor_price=c['sensor'], base=base,
                stage1=c['sensor'] + base if valid else None, stage2=c['stage2'], stage3=c['stage3'],
                predicted=total, signed_pct=signed, ape_pct=abs(signed) if signed is not None else None,
                remainder=y-c['known'] if y is not None else None,
                known_price=c['known'], details=c['details'] + [dict(layer=1, name='共用基础金额B',
                    quantity=1, unit_price=base, amount=base)],
                status='计算完成' if valid else '基础金额为负或缺失，停止报价；没有截成零')


def score(rows, expected):
    valid = [r for r in rows if r['ape_pct'] is not None]
    complete = len(valid) == expected and expected > 0
    return dict(complete=complete, count=len(valid), expected=expected,
                mape_pct=mean([r['ape_pct'] for r in valid]) if complete else None,
                max_ape_pct=max([r['ape_pct'] for r in valid]) if complete else None)


def leave_one_out(rows, catalog, endpoint='value'):
    result = []
    for i, held in enumerate(rows):
        train = rows[:i] + rows[i+1:]
        base = calibrate(train, catalog, endpoint)
        p = predict_row(held, catalog, base, endpoint)
        p['training_ids'] = [r['id'] for r in train]
        result.append(p)
    return result


def diagnose(project):
    catalog, rows, settings = project['catalog'], project['rows'], project['settings']
    real = [r for r in rows if r['actual'] is not None]
    base = calibrate(real, catalog)
    predictions = [predict_row(r, catalog, base) for r in rows]
    for row, p in zip(rows, predictions):
        p['role'] = '训练回代' if row['actual'] is not None else '待预测'
    loo = leave_one_out(real, catalog)
    metrics = score(loo, len(real))
    findings = []
    def add(code, priority, fact, advice):
        findings.append(dict(code=code, priority=priority, fact=fact, advice=advice))
    provisional = []
    for group in catalog.values():
        provisional.extend(s['name'] for s in group.values() if any(not p['basis_confirmed'] for p in prices(s)))
    if provisional:
        add('PRICE_BASIS', 1, '部分单价口径或采购成本转成品系数未确认：' + '、'.join(provisional),
            '先确认这是成品计价贡献还是采购成本。当前仅按表内金额、未定系数暂按1作情景测算；不得据此正式报价。')
    if settings['platform'] != '相同':
        add('PLATFORM', 1, '共用基础平台：' + settings['platform'],
            '确认主板、电源、壳体、通道配套是否可共享一个基础金额B；若不同，优先补这些部件的加价，不用两条数据分别拟合。')
    if settings['overlap'] != '已核对无重复':
        add('OVERLAP', 1, '重复计价核对：' + settings['overlap'],
            '核对功能金额是否已包含传感器、板卡或第三层项目；同一金额只计入一个项目。')
    real_pred = [p for p in predictions if p['actual'] is not None]
    negative = [p['sample_id'] for p in real_pred if p['remainder'] < 0]
    if negative:
        add('NEGATIVE_REMAINDER', 1, '已知项之和超过总价：' + '、'.join(negative),
            '先查金额单位、单价口径、数量、重复功能或组合折扣；保留负剩余证据，不调小原价或用零替代。')
    if len(real) < 2:
        add('TOO_FEW', 1, '有价真实样本不足2条', '先补齐两条已知总价；现在只提供数据诊断，不保存预测模型。')
    else:
        add('SMALL_SAMPLE', 3, '有价样本{}条；每次留一只用{}条校准B'.format(len(real), len(real)-1),
            '留一只是交换样本检查，不视为稳定泛化精度；已知单价须是独立业务知识，不能由留出总价反推。')
    remainders = [p['remainder'] for p in real_pred]
    spread = max(remainders)-min(remainders) if remainders else None
    spread_pct = spread / mean([abs(x) for x in remainders]) * 100 if remainders and any(remainders) else (0. if remainders else None)
    if not metrics['complete']:
        add('LOO_INCOMPLETE', 1, '留一未完成全部有效预测', '不要引用部分样本MAPE；先处理留一训练基础金额为负、缺少真实价等问题。')
    elif metrics['mape_pct'] > 10:
        add('BASE_MISMATCH', 2, '当前固定已知价格、共用B的留一MAPE={:.3f}%'.format(metrics['mape_pct']),
            '优先解释两条扣除已知项后的差额。查看本地产品差异，向专家确认一项未计价部件/结构差价；不要先加树模型或重量曲线。10%仅为诊断提示线，不是DS验收约定。')
    else:
        add('BASE_WORKS_LOCALLY', 3, '当前共用B在这些样本的交换预测误差较小',
            '先保留一参数方案；新增产品仍须核对平台和未计价属性，不因两条留一较小承诺新产品精度。')
    differing = []
    for name in sorted({k for r in real for k in r['extras']}):
        vals = [r['extras'].get(name) for r in real]
        if len({str(v) for v in vals}) > 1:
            differing.append(dict(field=name, values=vals))
    if differing:
        add('UNPRICED_DIFFERENCES', 2, '未参与计价的差异字段：' + '、'.join(x['field'] for x in differing),
            '只向专家核实其中与剩余金额相关的一项；字段不同不等于它造成价差，不把相关性当因果。')
    observed_names = sorted({name for row in rows for name in row['extras']})
    if observed_names:
        add('UNPRICED_ATTRIBUTES', 2, '当前只记录、未单独计价的属性：'+'、'.join(observed_names),
            '这些属性的原值已保留；改变它们不会改变本版报价。未知价格不等于价格为0，其影响可能混在B中；先核实少量关键差价。')
    add('WEIGHT_NOT_LEARNED', 3, '重量已记录但未拟合系数；第三层仅累计明确填写的专家加价，无ML',
        '若需要重量计价，补同传感器/功能条件下的结构差价信息；避免将传感器带来的重量变化重复计价。')
    issues = [f['code'] for f in findings if f['priority'] == 1]
    ready = base is not None and base >= 0 and len(real) >= 2
    status = '可试用（非精度认证）' if ready and not issues else ('仅情景试算' if ready else '仅诊断，未生成模型')
    sensitivities = []
    if any(p['low'] != p['high'] for group in catalog.values() for s in group.values() for p in prices(s)):
        for endpoint in ('low', 'value', 'high'):
            ep_base = calibrate(real, catalog, endpoint)
            ep_rows = [predict_row(r, catalog, ep_base, endpoint) for r in rows]
            ep_loo = leave_one_out(real, catalog, endpoint)
            sensitivities.append(dict(scenario=endpoint, base=ep_base, predictions=ep_rows,
                                      loo_metrics=score(ep_loo, len(real))))
    model = None
    if ready:
        model = dict(version=VERSION, base=base, catalog=copy.deepcopy(catalog), settings=copy.deepcopy(settings),
                     status=status, issues=issues, training_ids=[r['id'] for r in real],
                     coverage=dict(sensor_types=sorted({r['sensor_type'] for r in real}),
                                   count_min=min(r['count'] for r in real), count_max=max(r['count'] for r in real),
                                   weight_min=min([r['weight'] for r in real if r['weight'] is not None], default=None),
                                   weight_max=max([r['weight'] for r in real if r['weight'] is not None], default=None)),
                     validation=metrics)
    findings.sort(key=lambda f: f['priority'])
    return dict(version=VERSION, kind='training_diagnosis', status=status, settings=settings,
                model=model, catalog=catalog, rows=rows, predictions=predictions, loo=loo, metrics=metrics,
                fit_metrics=score(real_pred, len(real)), base=base, findings=findings,
                remainder_spread_pct=spread_pct, differences=differing, sensitivity=sensitivities,
                sensitivity_note='low/value/high为全部专家金额同时取下限/参考/上限并重新校准B的三个情景；不是置信区间，也不是价格的全局上下界。')


def predict_frozen(model, project):
    if model.get('version') not in (VERSION, 'ds-price-v1.0', 'ds-price-v1.1') or not math.isfinite(model.get('base', float('nan'))) or model['base'] < 0:
        raise ValueError('请选择本版成功生成的DS_model.json')
    if project['catalog'] != model['catalog']:
        raise ValueError('输入表的价格表与保存模型不一致。价格或层级改变请重新训练，不静默套用旧价格')
    predictions = []
    cov = model['coverage']
    for row in project['rows']:
        p = predict_row(row, model['catalog'], model['base'])
        p['role'] = '冻结模型新输入预测（实际价不参与）'
        warns = []
        if row['sensor_type'] not in cov['sensor_types']: warns.append('此型号仅有专家价格，未在训练产品出现')
        if row['count'] < cov['count_min'] or row['count'] > cov['count_max']: warns.append('数量超训练范围，配套电子系统未验证')
        if row['weight'] is not None and cov['weight_min'] is not None and not cov['weight_min'] <= row['weight'] <= cov['weight_max']:
            warns.append('重量超训练范围且没有重量计价模型')
        if project['settings']['platform'] != '相同': warns.append('新输入平台未确认相同，共用B仅情景试算')
        if project['settings']['overlap'] != '已核对无重复': warns.append('新输入重复计价尚未核对')
        p['warnings'] = warns
        predictions.append(p)
    scenario = project['settings']['platform'] != '相同' or project['settings']['overlap'] != '已核对无重复'
    return dict(version=VERSION, kind='independent_prediction', status='仅情景试算' if scenario else model['status'], model=model,
                settings=project['settings'], catalog=model['catalog'], rows=project['rows'], base=model['base'],
                predictions=predictions, loo=[], metrics=dict(complete=False, count=0, expected=0, mape_pct=None, max_ape_pct=None),
                historical_metrics=model['validation'], findings=[dict(code='FROZEN', priority=3,
                    fact='使用保存模型B及专家价格；输入实际价不参与训练',
                    advice='历史留一不是新产品实测。新平台、数量范围及未计价属性差异见各行风险提示。')],
                sensitivity=[], differences=[], remainder_spread_pct=None)


# ---- ds_api.py ----
# -*- coding: utf-8 -*-
"""DS single-sample inference. Python 3.8+, standard library only.

predict = load_predictor('DS_model.json')
result_json = predict(sample_json)
"""
import copy
import json
import math
from pathlib import Path


API_VERSION = 'ds-api-v1.0'
CORE = ('样本编号', '实际总价', '传感器型号', '传感器数量', '重量kg')
VERSIONS = ('ds-price-v1.0', 'ds-price-v1.1', 'ds-price-v1.2')


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def _error(code, message):
    return _json(dict(success=False,error=dict(code=code,message=message)))


def _read_sample_json(value):
    if not isinstance(value,str): raise ValueError('输入须是JSON文本；Python字典请先用json.dumps转换')
    def pairs(items):
        result={}
        for key,item in items:
            if key in result: raise ValueError('JSON属性名重复：'+key)
            result[key]=item
        return result
    def invalid_constant(value): raise ValueError('JSON不支持NaN或Infinity：'+value)
    try:
        return json.loads(value,object_pairs_hook=pairs,parse_constant=invalid_constant)
    except json.JSONDecodeError as exc:
        raise ValueError('JSON格式错误：第{}行第{}列，{}'.format(exc.lineno,exc.colno,exc.msg)) from exc


def _number(value, name, minimum=0, optional=False):
    if optional and value in (None, ''): return None
    if isinstance(value, bool): raise ValueError(name+'必须是数值，不能是布尔值')
    try: value = float(value)
    except (ValueError, TypeError, OverflowError): raise ValueError(name+'必须填写有效数值')
    if not math.isfinite(value) or value < minimum:
        raise ValueError(name+'必须是有限且不小于{}的数'.format(minimum))
    return value


def _check_model(model):
    """Validate the persisted model boundary, without changing saved prices."""
    try:
        if not isinstance(model, dict) or model.get('version') not in VERSIONS:
            raise ValueError('需要DS训练生成的DS_model.json，不能使用报告、泵模型或其他版本')
        json.dumps(model, allow_nan=False)
        # Saved model numbers are already in yuan, even when Excel used 万元.
        if not isinstance(model['base'], (int,float)) or isinstance(model['base'], bool):
            raise ValueError('模型基础金额B必须是数值')
        _number(model['base'], '模型基础金额B')
        cat = model['catalog']; sensors = cat['sensors']; features = cat['features']
        if not isinstance(sensors,dict) or not sensors or not isinstance(features,dict):
            raise ValueError('模型缺少传感器价表或属性规则')
        for group,items in [('sensors',sensors),('features',features)]:
            for name,spec in items.items():
                if not isinstance(name,str) or not name.strip() or spec['name'] != name:
                    raise ValueError('模型价表名称无效')
                if group=='sensors':
                    if spec['layer'] != 1: raise ValueError('基础传感器必须属于第一层')
                else:
                    method=spec.get('method','开关加价'); kind=spec.get('type','布尔')
                    if name in CORE or spec['layer'] not in (2,3) or kind not in TYPES:
                        raise ValueError('模型属性名称、类型或层级无效')
                    if method not in METHODS or method=='仅记录': raise ValueError('模型计价方式无效')
                    if method in ('开关加价','按传感器计价') and kind!='布尔': raise ValueError('开关规则须为布尔属性')
                    if method=='按单位计价' and kind!='数值': raise ValueError('单位规则须为数值属性')
                    if method=='按取值查价' and kind not in ('数值','等级','类别'): raise ValueError('查价属性类型无效')
                rules=prices(spec)
                if not isinstance(rules,list) or not rules: raise ValueError('模型计价规则为空')
                for rule in rules:
                    for key in ('value','low','high','factor'):
                        if not isinstance(rule[key],(int,float)) or isinstance(rule[key],bool):
                            raise ValueError('模型价格/系数必须是数值')
                        _number(rule[key], '模型价格/系数')
                    if not rule['low'] <= rule['value'] <= rule['high'] or rule['factor'] <= 0:
                        raise ValueError('模型价格区间或系数无效')
                    if rule['layer'] != spec['layer']: raise ValueError('模型规则层级冲突')
                if group=='features':
                    if method in ('按取值查价','按传感器计价'):
                        matches=[r['match'] for r in rules]
                        if len(set(matches)) != len(matches) or any(v is None or v=='' for v in matches):
                            raise ValueError('模型查价项缺失或重复')
                        if method=='按传感器计价' and any(not isinstance(v,str) for v in matches):
                            raise ValueError('传感器附加价的匹配项须为型号文字')
                    elif len(rules)!=1: raise ValueError('单价规则只能有一行')
        settings=model['settings']; cov=model['coverage']
        if settings['platform'] not in ('相同','不同','未确认') or settings['overlap'] not in ('已核对无重复','存在重复','未确认'):
            raise ValueError('模型业务确认状态无效')
        if not isinstance(cov['sensor_types'],list) or not isinstance(model['validation'],dict):
            raise ValueError('模型缺少覆盖或历史验证信息')
        if _number(cov['count_min'],'训练数量下界',1)>_number(cov['count_max'],'训练数量上界',1):
            raise ValueError('训练数量范围无效')
        lo=_number(cov['weight_min'],'训练重量下界',optional=True)
        hi=_number(cov['weight_max'],'训练重量上界',optional=True)
        if (lo is None)!=(hi is None) or (lo is not None and lo>hi): raise ValueError('训练重量范围无效')
        if model['status'] not in ('可试用（非精度认证）','仅情景试算') or not isinstance(model['issues'],list):
            raise ValueError('模型没有可用预测状态')
    except (KeyError,TypeError,AttributeError,OverflowError) as exc:
        raise ValueError('DS模型内容不完整或格式损坏；请选择训练输出的DS_model.json') from exc


class _Predictor:
    def __init__(self, model, field_map=None):
        _check_model(model)
        self._model=copy.deepcopy(model)
        self._fields={'component_type':'部件型号','component_count':'部件数量'}
        if field_map is not None:
            if not isinstance(field_map,dict) or set(field_map)-set(self._fields):
                raise ValueError('field_map只接受component_type、component_count两个字段映射')
            self._fields.update(field_map)
        names=list(self._fields.values())
        if any(not isinstance(n,str) or not n.strip() or n!=n.strip() for n in names) or len(set(names))!=2:
            raise ValueError('部件字段映射须为不同的非空字段名称')
        forbidden=set(model['catalog']['features'])|{'样本编号','实际总价','重量kg'}
        if any(n in forbidden for n in names): raise ValueError('部件字段映射不能与其他计价属性或核心字段重名')
        if names[0] in ('传感器数量','部件数量') or names[1] in ('传感器型号','部件型号'):
            raise ValueError('部件型号和数量字段映射互相冲突')

    def _message(self, message):
        return message.replace('传感器型号',self._fields['component_type']).replace('传感器数量',self._fields['component_count']).replace('传感器','部件').replace('共用B','第三层剩余校准项')

    def input_spec(self):
        """Return required attribute names and types as JSON text."""
        cat=self._model['catalog']
        fields=[dict(name=self._fields['component_type'],type='类别',allowed_values=list(cat['sensors'])),
                dict(name=self._fields['component_count'],type='正整数')]
        for name,spec in cat['features'].items():
            item=dict(name=name,type=spec.get('type','布尔'),method=spec.get('method','开关加价'),layer=spec['layer'])
            if item['method']=='按取值查价': item['allowed_values']=[r['match'] for r in spec['rules']]
            if item['method']=='按传感器计价': item['priced_sensor_types']=[r['match'] for r in spec['rules']]
            if item['method']=='按传感器计价':
                item['method']='按部件计价'
                item['priced_component_types']=item.pop('priced_sensor_types')
            fields.append(item)
        return _json(dict(required=fields,optional=['样本编号','重量kg'],output_unit='元',
                    note='其他字段可原值传入但未单独计价；重量仅记录。第三层计价属性仍需输入，结果只给第三层合计。实际总价不用于预测。'))

    def __call__(self, sample_json, *, platform=None, overlap=None):
        """JSON text in/out. Invalid input returns success=false, never a zero quote."""
        try:
            sample=_read_sample_json(sample_json)
            result=self._predict_dict(sample,platform=platform,overlap=overlap)
            return _json(dict(success=True,**result))
        except ValueError as exc:
            return _error('INVALID_INPUT',self._message(str(exc)))

    def _predict_dict(self, sample, *, platform=None, overlap=None):
        """Predict once from flat Chinese attribute names, without IO or fitting."""
        if not isinstance(sample,dict): raise ValueError('样本须是属性名称到取值的字典')
        row={}
        for name,value in sample.items():
            if not isinstance(name,str) or not name.strip(): raise ValueError('样本属性名须为非空文字')
            name=name.strip()
            if name in row: raise ValueError('去除首尾空白后属性名重复：'+name)
            if not isinstance(value,(str,int,float,bool,type(None))): raise ValueError(name+'须为单个数值、文字、布尔值或None')
            if isinstance(value,float) and not math.isfinite(value): raise ValueError(name+'不是有限数值')
            row[name]=value
        # External aliases do not rename persisted model fields or change prices.
        for key,canonical,old in [('component_type','部件型号','传感器型号'),('component_count','部件数量','传感器数量')]:
            candidates=set((self._fields[key],canonical,old))
            present=[name for name in candidates if name in row]
            if len(present)>1: raise ValueError('同一核心字段重复提供：'+'、'.join(sorted(present)))
            if present: row[old]=row.pop(present[0])
        sid=str(row.get('样本编号') or '单条预测').strip()
        sensor=str(row.get('传感器型号') or '').strip()
        cat=self._model['catalog']
        if sensor not in cat['sensors']: raise ValueError('传感器型号未在模型价表中找到：'+sensor)
        count=_number(row.get('传感器数量'),'传感器数量',1)
        if count!=int(count): raise ValueError('传感器数量必须是正整数')
        weight=_number(row.get('重量kg'),'重量kg',optional=True)
        # This reuses exactly the Excel attribute parser, including unknown-price errors.
        row['传感器型号']=sensor
        features,_=parse_values(row,cat['features'],{},sid)
        extras={k:v for k,v in row.items() if k not in CORE and k not in cat['features']}
        parsed=dict(id=sid,actual=None,sensor_type=sensor,count=int(count),weight=weight,features=features,extras=extras)
        settings=copy.deepcopy(self._model['settings'])
        if platform is not None:
            if platform not in ('相同','不同','未确认'): raise ValueError('platform选相同/不同/未确认')
            settings['platform']=platform
        if overlap is not None:
            if overlap not in ('已核对无重复','存在重复','未确认'): raise ValueError('overlap选已核对无重复/存在重复/未确认')
            settings['overlap']=overlap
        report=predict_frozen(self._model,dict(rows=[parsed],catalog=cat,settings=settings))
        p=report['predictions'][0]
        amounts=[p[k] for k in ('predicted','stage1','stage2','stage3','sensor_price','base')]
        amounts.extend(d['amount'] for d in p['details'])
        if any(v is None or not math.isfinite(v) for v in amounts): raise ValueError('预测金额溢出或无效，请检查输入数量和模型')
        warnings=[self._message(w) for w in p['warnings']]
        if extras: warnings.append('另有{}个输入属性未配置计价规则，本版未学习其价格作用'.format(len(extras)))
        if weight is not None: warnings.append('重量已记录，当前模型未拟合重量价格作用')
        if '实际总价' in row: warnings.append('输入实际总价已忽略，没有重新训练或计算本次精度')
        stage3=p['stage3']+p['base']
        # Only first/second layer details are exposed; third-layer rules and B
        # remain in the original engine calculation, but are one aggregate output.
        details=[]
        for d in p['details']:
            if d['layer']==3 or d['name']=='共用基础金额B': continue
            d=copy.deepcopy(d)
            if d['layer']==1: d['name']='核心部件:'+sensor
            if d.get('pricing_method')=='按传感器计价': d['pricing_method']='按部件计价'
            if 'sensor_type' in d: d['component_type']=d.pop('sensor_type')
            if 'sensor_count' in d: d['component_count']=d.pop('sensor_count')
            if 'pricing_note' in d: d['pricing_note']=self._message(d['pricing_note'])
            details.append(d)
        details.append(dict(layer=3,name='第三层综合金额',amount=stage3))
        return dict(api_version=API_VERSION,model_version=self._model['version'],sample_id=sid,
                    total_price=p['predicted'],unit='元',
                    layers=dict(stage1=p['sensor_price'],stage2=p['stage2'],stage3=stage3),
                    details=details,status=report['status'],warnings=warnings,
                    model_issues=list(self._model['issues']),unpriced_field_count=len(extras),
                    recorded_weight_kg=weight,
                    assumptions=dict(platform=settings['platform'],overlap=settings['overlap'],
                                     platform_source='模型' if platform is None else '本次指定',
                                     overlap_source='模型' if overlap is None else '本次指定'))


def load_predictor(model_path, field_map=None):
    """Load DS_model.json once; returns predict(sample_json) -> result_json.

    A bad model raises ValueError at initialization. For a fully JSON-returning
    one-shot call including model-load failures, use predict_json instead.
    """
    try:
        model=json.loads(Path(model_path).read_text(encoding='utf-8-sig'))
    except (OSError,UnicodeError,ValueError) as exc:
        raise ValueError('无法读取DS模型文件：'+str(model_path)+'；'+str(exc)) from exc
    return _Predictor(model,field_map=field_map)


def predict_json(sample_json, model_path, field_map=None):
    """One-shot JSON interface, including JSON errors for invalid model files."""
    try:
        predictor=load_predictor(model_path,field_map=field_map)
    except ValueError as exc:
        return _error('INVALID_MODEL',str(exc))
    return predictor(sample_json)


# ---- interface.py ----
# -*- coding: utf-8 -*-
"""Public integration facade, appended to the frozen engine by build_sdk.py."""
__all__ = ['load_model']
SDK_VERSION = 'ds-sdk-v1.0'


class _DSModel:
    def __init__(self, saved_model, field_map=None):
        self._predictor = _Predictor(saved_model, field_map=field_map)
        self._schema = self._predictor.input_spec()

    def predict_json(self, input_json, *, platform=None, overlap=None):
        """One flat sample JSON string in, prediction/error JSON string out."""
        return self._predictor(input_json, platform=platform, overlap=overlap)

    def schema_json(self):
        """Optional field names, types and choices as JSON (not a sample)."""
        return self._schema


def load_model(model_path, field_map=None):
    """Initialize once from DS_model.json (or a report embedding a valid model).

    Returns an object with predict_json(input_json) and schema_json().
    Initialization errors raise ValueError; prediction errors are JSON responses.
    Loading never trains or generates samples. Later source changes require reloading.
    """
    try:
        saved = _read_sample_json(Path(model_path).read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError('无法加载DS模型：' + str(exc)) from exc
    if isinstance(saved, dict) and isinstance(saved.get('model'), dict):
        saved = saved['model']
    return _DSModel(saved, field_map=field_map)
