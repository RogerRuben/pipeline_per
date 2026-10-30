import hashlib
import json
import math
import re
from pathlib import Path

from .errors import DeepPriceError
from .features import IDENTITY


def load_rule(root):
    path = Path(root)/'config/deep_price_adjustment.json'
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
        rule = json.loads(raw.decode('utf-8-sig'))
        if rule.get('confirmed') is not True:
            return None
        for key in ('version','source','approved_by','approved_at'):
            if not isinstance(rule.get(key),str) or not rule[key].strip():
                raise ValueError('缺少来源或审批记录')
        if rule.get('price_unit')!='万元' or rule.get('time_direction')!='base_to_target':
            raise ValueError('单位或校正方向未声明')
        ppi = rule['ppi']
        if not isinstance(ppi,dict) or str(rule['base_year']) not in ppi:
            raise ValueError('PPI缺少基年')
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0 for v in ppi.values()):
            raise ValueError('PPI必须为正有限数值')
        for key in ('reference_quantity','quantity_elasticity'):
            value = rule[key]
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
                raise ValueError('数量规则不是有限数值')
        if rule['reference_quantity']<=0:
            raise ValueError('基准数量必须为正')
        scope=rule['applicability']
        if not isinstance(scope,dict) or set(scope)!={'product_code','model_version','price_model_sha256','price_output_signature'}:
            raise ValueError('缺少明确的产品、模型及价格单位配置适用范围')
        for key in ('product_code','model_version'):
            if not isinstance(scope[key],str) or not scope[key].strip() or '*' in scope[key]:
                raise ValueError('适用产品/模型必须为明确标识，不允许通配符')
        for key in ('price_model_sha256','price_output_signature'):
            if not isinstance(scope[key],str) or not re.fullmatch('[0-9a-f]{64}',scope[key]):
                raise ValueError('适用模型与价格配置必须提供SHA256身份')
        basis=rule['price_basis']
        if (not isinstance(basis,dict) or basis.get('kind')!='model_unit_price'
            or not isinstance(basis.get('description'),str) or not basis['description'].strip()
            or isinstance(rule['base_year'],bool) or not isinstance(rule['base_year'],int)
            or not 1900<=rule['base_year']<=2200
            or basis.get('base_year')!=rule['base_year']
            or basis.get('reference_quantity')!=rule['reference_quantity']):
            raise ValueError('价格基准必须声明模型单价、适用基年及相同基准数量')
        rule['sha256'] = hashlib.sha256(raw).hexdigest()
        return rule
    except (ValueError,KeyError,TypeError) as exc:
        raise DeepPriceError('unavailable','已确认的校正规则配置无效：'+str(exc),503)


def scope_matches(rule,identity):
    return all(identity.get(key)==value for key,value in rule['applicability'].items())


def adjustment_status(root,core=None):
    rule=load_rule(root)
    if rule and core is not None:
        status,schema=core.handle('GET','/schema')
        if status!=200:
            raise DeepPriceError('unavailable','无法验证校正规则适用的当前模型',503)
        if not scope_matches(rule,schema):
            return {'status':'not_configured','rule':None,'message':'已确认规则不适用于当前产品、模型或价格基准配置，请配置匹配规则'}
    return {'status':'available' if rule else 'not_configured','rule':rule,
            'message':'使用已确认规则，模型原始价格与校正价分开展示' if rule else '未配置经确认的PPI／基年／数量弹性规则，暂不执行校正'}


def adjust(core,root,cache,sid,payload):
    if not isinstance(payload,dict) or not {'result_id','year','quantity','rule_version'}<=set(payload) or set(payload)-{'result_id','year','quantity','rule_version','rule_sha256'}:
        raise DeepPriceError('invalid_input','校正请求仅允许result_id、year、quantity和rule_version')
    rule=load_rule(root)
    if not rule:
        raise DeepPriceError('not_configured','未配置经确认的价格校正规则',422)
    if payload['rule_version']!=rule['version']:
        raise DeepPriceError('version_conflict','校正规则版本已变化',409)
    if 'rule_sha256' in payload and payload['rule_sha256']!=rule['sha256']:
        raise DeepPriceError('version_conflict','校正规则内容已变化，请刷新',409)
    original=cache.get(sid,payload['result_id'])
    if original.get('result_kind')!='/predict':
        raise DeepPriceError('invalid_input','校正基准必须为本会话的原始单次预测，不能重复校正')
    if not scope_matches(rule,original):
        raise DeepPriceError('version_conflict','校正规则不适用于此产品、模型或价格配置',409)
    status,schema=core.handle('GET','/schema')
    if status!=200:
        return status,schema
    if any(original[k]!=schema[k] for k in IDENTITY):
        raise DeepPriceError('version_conflict','原始预测对应的发布或模型已变化，请重新预测',409)
    year,quantity=payload['year'],payload['quantity']
    if isinstance(year,bool) or not isinstance(year,int) or not 1900<=year<=2200 or str(year) not in rule['ppi']:
        raise DeepPriceError('invalid_input','年份无有效PPI，不能默默使用基年指数')
    if isinstance(quantity,bool) or not isinstance(quantity,int) or not 1<=quantity<=10**9:
        raise DeepPriceError('invalid_input','数量必须为1至10亿的整数')
    price=original['predicted_price_wan']
    try:
        time_factor=rule['ppi'][str(year)]/rule['ppi'][str(rule['base_year'])]
        quantity_factor=(quantity/rule['reference_quantity'])**rule['quantity_elasticity']
        final=price*time_factor*quantity_factor
        if not math.isfinite(final):
            raise ValueError()
    except (ValueError,OverflowError,ZeroDivisionError):
        raise DeepPriceError('invalid_input','校正结果超出可计算范围')
    result={k:original[k] for k in IDENTITY+('price_unit','request_id')}
    result.update(success=True,original_price_wan=price,time_adjusted_price_wan=price*time_factor,
                  adjusted_price_wan=final,year=year,quantity=quantity,adjustment_rule=rule,
                  formula='P_model × PPI(target)/PPI(base) × (quantity/reference_quantity)^elasticity',
                  time_factor=time_factor,quantity_factor=quantity_factor,source_result_id=original['result_id'])
    return 200,result
