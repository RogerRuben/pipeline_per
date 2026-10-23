# -*- coding: utf-8 -*-
"""Select configurations without training; preserve saved models by semantic mode."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.console_compat import enable_plain_console
enable_plain_console()

LABELS={'random':'随机划分搜索','global':'全划分统一搜索','per_split':'逐划分搜索','hybrid':'混合搜索与复核'}


def loader(engine):
    if engine=='optuna_v5':from .product_price_optuna_v5 import load_config
    elif engine=='optuna_v4':from .product_price_optuna_v4 import load_config
    elif engine=='optuna_v3':from .product_price_optuna_v3 import load_config
    elif engine=='optuna_v2':from .product_price_optuna_v2 import load_config
    else:raise ValueError('只支持V2、V3、V4、V5。')
    return load_config


def descriptor(engine,cfg):
    value={'engine':engine,'expert_enabled':cfg['expert']['enabled'],'deployment_scope':cfg['deployment']['model_scope']}
    if engine=='optuna_v4':
        value.update(search_mode=cfg['v4']['search']['mode'],train_gate=cfg['v4']['train_gate'],
                     evaluation_mode=cfg['v4']['evaluation']['mode'],ensemble_enabled=cfg['v4']['ensemble']['enabled'],
                     targets={k:v for k,v in cfg['targets'].items() if k!='hard_gate'},coverage=cfg['v4']['splits']['coverage'])
    elif engine=='optuna_v5':
        value.update(search_mode=cfg['v5']['mode'],train_gate=cfg['v5']['train_gate'],evaluation_mode=cfg['v5']['evaluation']['mode'],
                     ensemble_enabled=cfg['v5']['fusion']['enabled'],audit_enabled=cfg['v5']['audit']['enabled'],
                     baseline_finish=cfg['v5']['baseline']['finish'],targets={k:v for k,v in cfg['targets'].items() if k!='hard_gate'})
    return value


def mode_key(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()[:16]


def current(root):
    root=Path(root);settings=json.loads((root/'config/portable.json').read_text(encoding='utf-8-sig'));engine=settings['price_engine']
    return descriptor(engine,loader(engine)(root/('config/price_'+engine+'.json')))


def validate_pointer(root,pointer):
    from tools.product_portable import inside,digest
    root=Path(root).resolve();output=(root/pointer['output']).resolve()
    if not inside(output,root/'outputs'):raise ValueError('模式保存路径越出outputs。')
    path=output/'prototype_manifest.json'
    if not path.is_file():return None
    m=json.loads(path.read_text(encoding='utf-8'))
    if m.get('status')!='ready':return None
    for key in ('price_model','effectiveness_model','business_package'):
        artifact=(output/m[key]).resolve()
        if not inside(artifact,output) or not artifact.is_file() or digest(artifact)!=m['artifact_sha256'][key]:raise ValueError('保存模型校验失败：'+key)
    return output


def register(root,pointer,cfg=None):
    from tools.product_portable import write_json
    root=Path(root);output=validate_pointer(root,pointer)
    if output is None:raise ValueError('模型尚未完成，不能注册。')
    manifest=json.loads((output/'prototype_manifest.json').read_text(encoding='utf-8'));engine=manifest['price_engine']
    cfg=cfg or json.loads((output/'price/effective_config.json').read_text(encoding='utf-8'))
    desc=descriptor(engine,cfg);key=mode_key(desc);path=root/'state/modes.json'
    registry=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {'format':'price-saved-modes-1.0','models':{}}
    entry=dict(pointer,mode=desc,mode_key=key);registry['models'][key]=entry;write_json(path,registry)
    return entry


def selected_pointer(root):
    root=Path(root);path=root/'state/modes.json'
    if not path.is_file():return None
    desc=current(root);entry=json.loads(path.read_text(encoding='utf-8'))['models'].get(mode_key(desc))
    if not entry:return None
    output=validate_pointer(root,entry)
    if output is None:return None
    manifest=json.loads((output/'prototype_manifest.json').read_text(encoding='utf-8'))
    effective=json.loads((output/'price/effective_config.json').read_text(encoding='utf-8'))
    if descriptor(manifest['price_engine'],effective)!=desc:raise ValueError('保存模型与所选模式不一致。')
    return entry


def select(root,engine=None,**changes):
    from tools.product_portable import write_json
    root=Path(root);path=root/'config/portable.json';settings=json.loads(path.read_text(encoding='utf-8-sig'))
    engine=engine or settings['price_engine'];load=loader(engine);cfg_path=root/('config/price_'+engine+'.json');cfg=load(cfg_path)
    for name,value in changes.items():
        if value is None:continue
        if name=='experts':cfg['expert']['enabled']=value=='on'
        elif name=='scope':cfg['deployment']['model_scope']=value
        elif name=='trials':cfg['search']['n_trials']=int(value)
        elif engine=='optuna_v5':
            if name=='strategy':cfg['v5']['mode']=value
            elif name=='gate':cfg['v5']['train_gate']['mode']=value
            elif name=='evaluation':cfg['v5']['evaluation']['mode']=value
            elif name=='ensemble':cfg['v5']['fusion']['enabled']=value=='on'
            elif name=='publish':cfg['v5']['publish']['policy']=value
            elif name=='audit':cfg['v5']['audit']['enabled']=value=='on'
            elif name=='baseline_finish':cfg['v5']['baseline']['finish']=value
            elif name=='new_fits':cfg['v5']['enhancement']['max_new_fits']=int(value)
            elif name=='max_fits':cfg['v5']['budget']['max_member_fits']=int(value) if int(value)>0 else None
            elif name=='seconds':cfg['search']['timeout_seconds']=float(value) if float(value)>0 else None
            elif name=='continue_after_targets':cfg['v5']['enhancement']['continue_after_targets']=value=='on'
            else:raise ValueError(name+'不适用于V5；跨折训练门槛范围/覆盖策略可切换V4后设置。')
        elif engine!='optuna_v4':raise ValueError(name+'仅适用于V4。')
        elif name=='strategy':cfg['v4']['search']['mode']=value
        elif name=='gate':cfg['v4']['train_gate']['mode']=value
        elif name=='gate_scope':cfg['v4']['train_gate']['scope']=value
        elif name=='evaluation':cfg['v4']['evaluation']['mode']=value
        elif name=='ensemble':cfg['v4']['ensemble']['enabled']=value=='on'
        elif name=='coverage':cfg['v4']['splits']['coverage']=value
        elif name=='publish':cfg['v4']['publish']['policy']=value
        elif name=='budget':cfg['v4']['search']['budget_mode']=value
        else:raise ValueError('未知模式设置：'+name)
    cfg=load(overrides=cfg);write_json(cfg_path,cfg)
    settings.update(price_engine=engine,startup_policy='saved_only',engine_selection_required=True,mode_registry_enabled=True);write_json(path,settings)
    pointer=selected_pointer(root)
    if pointer:write_json(root/'state/latest.json',pointer)
    label=engine.upper().replace('OPTUNA_','')+(' / '+LABELS[cfg['v4']['search']['mode']] if engine=='optuna_v4' else ' / '+{'goal_first':'V3基线达标优先','balanced':'高分与复核权衡','robust':'跨划分融合'}[cfg['v5']['mode']] if engine=='optuna_v5' else '')
    print('已选择%s；专家价格评分%s。'%(label,'开启' if cfg['expert']['enabled'] else '关闭'),flush=True)
    print('START直接载入：'+pointer['output'] if pointer else '此模式尚无已训练模型，请CHECK_INPUT后RETRAIN；START不会自动训练。',flush=True)
    print('切换与修改预算均不触发训练；训练配置在下次RETRAIN生效。',flush=True)
    return pointer


def menu(root):
    while True:
        desc=current(root);print('\n当前模式：'+json.dumps(desc,ensure_ascii=False))
        print('1 版本V2/V3/V4/V5　2 搜索模式　3 训练门槛　4 V4门槛范围　5 专家评分　6 部署范围\n7 评估模式　8 融合　9 V3/V5基线轮数或V4总轮数　10 V4预算方式　11 V4划分覆盖　12 发布策略\n13 V5复核开关　14 V5新增fit预算　15 V5总fit上限　16 V5秒数预算　17 V5基线结束方式　18 V5达标后继续　0 退出')
        option=input('选择编号：').strip()
        if option=='0':return
        choices={
            '1':('engine',['optuna_v5','optuna_v4','optuna_v3','optuna_v2'],['V5','V4','V3','V2']),
            '2':('strategy',['hybrid','random','global','per_split'],['混合复核','随机划分','全划分统一搜索','逐划分搜索']),
            '3':('gate',['hard','soft','off'],['硬门槛','软惩罚','关闭']),
            '4':('gate_scope',['pooled','every_split','fraction'],['汇总训练预测','每个划分均达标','达标划分占比']),
            '5':('experts',['on','off'],['价格1—5分评分开启','纯数据关闭评分']),
            '6':('scope',['full_data','selected_split'],['全部开发记录重拟合','所选开发划分快照']),
            '7':('evaluation',['development','locked_holdout'],['开发复核','固定外层留出（至少6条）']),
            '8':('ensemble',['on','off'],['多参数组合融合','单套参数六模型融合']),
            '10':('budget',['total','per_split'],['共享总预算','每个划分独立预算']),
            '11':('coverage',['auto','exhaustive','sampled'],['自动枚举/限额抽样','穷举全部唯一划分','固定抽样']),
            '12':('publish',['allow_draft','require_targets'],['未达标允许试用草稿','达标才更新运行模型']),
            '13':('audit',['on','off'],['启用跨划分复核','关闭复核（达标优先模式）']),
            '17':('baseline_finish',['fixed_budget','targets_met'],['完整V3基线预算','开发目标达到后停止基线']),
            '18':('continue_after_targets',['off','on'],['达到训练/开发门槛后停止新增fit','达标后继续改善'])}
        if desc['engine']=='optuna_v5':choices['2']=('strategy',['goal_first','balanced','robust'],['V3基线达标优先','高分与复核权衡','跨划分融合'])
        try:
            if option=='9':select(root,trials=int(input('配置搜索轮数（融合权重另计）：')));continue
            if option in ('14','15','16'):
                name={'14':'new_fits','15':'max_fits','16':'seconds'}[option]
                select(root,**{name:int(input('输入预算（总上限/秒数填0表示不设）：'))});continue
            if option not in choices:continue
            name,values,labels=choices[option]
            for i,label in enumerate(labels):print('%d. %s'%(i+1,label))
            index=int(input('选择：'))-1
            if not 0<=index<len(values):raise ValueError('选项超出范围。')
            select(root,**{name:values[index]})
        except (ValueError,KeyError) as exc:print('设置未应用：'+str(exc))


def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['menu','select','status']);p.add_argument('--engine',choices=['optuna_v2','optuna_v3','optuna_v4','optuna_v5'])
    for name,values in [('strategy',list(LABELS)+['goal_first','balanced','robust']),('gate',['off','soft','hard']),('gate-scope',['pooled','every_split','fraction']),('experts',['on','off']),
                        ('scope',['full_data','selected_split']),('evaluation',['development','locked_holdout']),('ensemble',['on','off']),
                        ('coverage',['auto','exhaustive','sampled']),('publish',['allow_draft','require_targets']),('budget',['total','per_split']),
                        ('audit',['on','off']),('baseline-finish',['fixed_budget','targets_met']),('continue-after-targets',['on','off'])]:p.add_argument('--'+name,choices=values)
    for name in ('trials','new-fits','max-fits','seconds'):p.add_argument('--'+name,type=int)
    a=p.parse_args()
    try:
        if a.command=='menu':menu(ROOT)
        elif a.command=='status':print(json.dumps({'selected':current(ROOT),'saved':selected_pointer(ROOT)},ensure_ascii=False,indent=2))
        else:select(ROOT,a.engine,**{k:v for k,v in vars(a).items() if k not in ('engine','command')})
        return 0
    except Exception as exc:print('模式操作失败：'+str(exc),file=sys.stderr,flush=True);return 1
if __name__=='__main__':sys.exit(main())
