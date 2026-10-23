# -*- coding: utf-8 -*-
"""Choose saved V2/V3 models, or train both under the same comparison budget."""
import argparse
import copy
import csv
import io
import json
import sys
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.console_compat import enable_plain_console
enable_plain_console()


def saved_pointer(root,engine):
    from tools.product_portable import inside,digest
    root=Path(root).resolve()
    for path in (root/'state'/('latest_'+engine+'.json'),root/'state/latest.json'):
        if not path.is_file():continue
        pointer=json.loads(path.read_text(encoding='utf-8-sig'));output=(root/pointer['output']).resolve()
        if not inside(output,root/'outputs'):raise ValueError('保存版本路径越出outputs目录。')
        manifest_path=output/'prototype_manifest.json'
        if not manifest_path.is_file():continue
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('price_engine')!=engine or manifest.get('status')!='ready':continue
        for key in ('price_model','effectiveness_model','business_package'):
            artifact=(output/manifest[key]).resolve()
            if not inside(artifact,output) or not artifact.is_file() or digest(artifact)!=manifest['artifact_sha256'][key]:raise ValueError('保存模型校验失败：'+key)
        return pointer
    return None


def select(root,engine):
    from tools.product_portable import write_json
    root=Path(root).resolve();path=root/'config/portable.json'
    settings=json.loads(path.read_text(encoding='utf-8-sig'))
    if engine in ('optuna_v4','optuna_v5') or settings.get('mode_registry_enabled'):
        from .product_modes import select as select_mode
        return select_mode(root,engine)
    if engine not in ('optuna_v2','optuna_v3'):raise ValueError('请选择optuna_v2或optuna_v3。')
    if not (root/('config/price_'+engine+'.json')).is_file():raise ValueError('引擎配置缺失，请先应用双版本补丁。')
    settings=json.loads(path.read_text(encoding='utf-8-sig'));pointer=saved_pointer(root,engine)
    settings.update(price_engine=engine,startup_policy='saved_only',engine_selection_required=True)
    write_json(path,settings)
    if pointer:
        write_json(root/'state'/('latest_'+engine+'.json'),pointer);write_json(root/'state/latest.json',pointer)
        print('已选择%s；START将运行该版本已保存模型：%s。'%(engine,pointer['output']),flush=True)
    else:print('已选择%s；该版本尚未训练，请先CHECK_INPUT，再RETRAIN。START不会借用另一版模型或自动训练。'%engine,flush=True)
    print('切换不会重新训练或删除任何模型。RETRAIN只训练当前选择的版本。',flush=True)
    return pointer


def compare(root,trials=None):
    from tools import product_portable as portable
    from .product_price_optuna_v3 import load_config as v3_config
    root=Path(root).resolve()
    if root!=portable.ROOT:raise ValueError('请使用本交付包内的对比入口。')
    portable.verify_runtime();source=portable.resolve_input()
    v2_prepared=portable.prepare(source,price_engine='optuna_v2')
    v3_prepared=portable.prepare(source,price_engine='optuna_v3')
    budget=300 if trials is None else trials
    if not isinstance(budget,int) or budget<2 or budget>100000:raise ValueError('对比预算须为2—100000整数，两版各使用该总轮数。')
    v2=copy.deepcopy(v2_prepared[1]);v2['search']['n_trials']=budget
    v3=copy.deepcopy(v3_prepared[1])
    for key,value in v2.items():
        if key!='schema_version':v3[key]=copy.deepcopy(value)
    v3['v3']['warm_start'].update(auto_history=False,history_file=None,config_file=None,reuse_metrics=False)
    v3=v3_config(overrides=v3)
    selected=json.loads((root/'config/portable.json').read_text(encoding='utf-8-sig')).get('price_engine')
    records=[];outputs=[];identities=[]
    try:
        for engine,prepared,cfg in (('optuna_v2',v2_prepared,v2),('optuna_v3',v3_prepared,v3)):
            settings,_,effect,details=prepared
            identity=copy.deepcopy(details);identity['comparison_budget']=budget
            output=portable.rebuild(source,(settings,cfg,effect,identity));outputs.append(output)
            report=json.loads((output/'price/training_report.json').read_text(encoding='utf-8'))
            identities.append(report['search_identity'])
            records.append({'版本':engine.upper().replace('OPTUNA_',''),'有效记录':report['training_rows'],'原始属性':report['feature_count'],
                '预算轮数':budget,'完成新试验':report['trials_completed'],'开发MAPE百分比':report['validation_metrics']['mape']*100,
                '开发R方':report['validation_metrics']['r2'],'搜索秒数':report['elapsed_seconds'],'所选划分种子':report['representative_seed'],
                '开发验证成品':','.join(report['validation_record_ids']),'有效模型数':report['member_count'],'开发目标达到':report['targets_met'],
                '模型权重':json.dumps(report['weights'],ensure_ascii=False),'保存目录':output.relative_to(root).as_posix()})
    finally:
        if json.loads((root/'config/portable.json').read_text(encoding='utf-8-sig')).get('mode_registry_enabled'):
            from .product_modes import selected_pointer
            pointer=selected_pointer(root)
            if pointer:portable.write_json(root/'state/latest.json',pointer)
        elif selected in ('optuna_v2','optuna_v3'):
            pointer=saved_pointer(root,selected)
            if pointer:portable.write_json(root/'state/latest.json',pointer)
    if len(set(identities))!=1:raise ValueError('两次训练的数据或预处理协议不一致，不能标记为同条件对比。各模型已分别保存。')
    destination=root/'outputs'/('compare_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'));destination.mkdir()
    text=io.StringIO(newline='');writer=csv.DictWriter(text,fieldnames=list(records[0]));writer.writeheader();writer.writerows(records)
    (destination/'V2_V3对比.csv').write_text(text.getvalue(),encoding='utf-8-sig')
    portable.write_json(destination/'comparison.json',{'status':'PASS','new_trial_budget_each':budget,'matching_data_and_protocol':True,
        'metric_scope':'searched_split_development','external_test_provided':False,'selection_automatically_changed':False,'results':records})
    lines=['# V2 / V3 同条件对比','','两版使用同一成品表、评分、类型、缩放、目标和新试验预算；对比运行关闭V3历史成绩复用。',
        '开发划分参与搜索，两版选中的验证记录可能不同；以下指标是搜索开发成绩，不能当作独立泛化性能结论。',
        '两版模型均已保存，当前选择保持不变；用SELECT_V2或SELECT_V3切换，再START。','',
        '| 版本 | 新试验数 | 开发MAPE | 开发R² | 秒数 | 划分种子 |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for r in records:lines.append('| %s | %d | %.3f%% | %.6f | %.2f | %d |'%(r['版本'],r['完成新试验'],r['开发MAPE百分比'],r['开发R方'],r['搜索秒数'],r['所选划分种子']))
    (destination/'对比说明.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('两版已分别保存；对比报告：'+str(destination),flush=True)
    return destination


def main():
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='command',required=True)
    select_parser=sub.add_parser('select');select_parser.add_argument('--engine',required=True,choices=['optuna_v2','optuna_v3','optuna_v4','optuna_v5'])
    comparison=sub.add_parser('compare');comparison.add_argument('--trials',type=int)
    args=parser.parse_args()
    try:
        if args.command=='select':select(ROOT,args.engine)
        else:compare(ROOT,args.trials)
        return 0
    except Exception as exc:print('版本选择/对比失败：'+str(exc),file=sys.stderr,flush=True);return 1
if __name__=='__main__':sys.exit(main())
