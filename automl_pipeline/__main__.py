# -*- coding: utf-8 -*-
"""Source checkout entry point, independent of the portable package's state."""
import argparse
import json
from pathlib import Path

HERE=Path(__file__).resolve().parent
ENGINES=('optuna_v2','optuna_v3','optuna_v4')


def config_loader(engine):
    if engine=='optuna_v4':from .product_price_optuna_v4 import load_config
    elif engine=='optuna_v3':from .product_price_optuna_v3 import load_config
    else:from .product_price_optuna_v2 import load_config
    return load_config


def configuration(engine,path):
    return config_loader(engine)(path or HERE/'configs'/('price_'+engine+'.json'))


def inspect_input(source,engine,cfg):
    from app.historical_onboarding import HistoricalProductOnboarding
    from app.product_table import product_table_rows
    from .product_price_optuna_v2 import workbook_config,dataset,training_split
    from services.price_service.optuna_v2.typed_features import restore_configurations
    source=Path(source);raw=source.read_bytes();table=product_table_rows(source.name,raw,require_dual_header=True)
    analyzed=HistoricalProductOnboarding().analyze(source.name,raw,'SOURCE_PRODUCT','源码输入检查',cfg['input']['missing_tokens'],require_dual_header=True)
    if not analyzed['price_column_detected']:raise ValueError('成品表须包含价格列price_wan。')
    data=analyzed['data'];cfg=workbook_config(source,cfg,{p['parameter_id'] for p in data['parameters']},loader=config_loader(engine))
    restore_configurations(data,table,cfg['expert']['field_types'],cfg['input']['missing_tokens'])
    frame,y,specs,mappings,groups,ids=dataset(data,cfg);_,split=training_split(len(y),cfg)
    result={'status':'PASS','engine':engine,'records':len(y),'attributes':len(specs),'validation_split':split,
            'attribute_types':{p['parameter_id']:('配置类型' if p['parameter_id'] in mappings else '数值') for p in specs},'diagnostics':table['diagnostics']}
    if engine=='optuna_v4':
        from .product_optuna_v4_adapter import groups_for
        from services.price_service.optuna_v4.splits import holdout_indices,make_splits
        from services.price_service.optuna_v4.optimizer import budget_plan
        groups=groups_for(frame,ids,data,cfg);dev,outer=holdout_indices(len(y),ids,groups,cfg)
        if len(outer) and cfg['v4']['evaluation']['require_declared_types'] and set(frame.columns)-set(cfg['expert']['field_types']):raise ValueError('固定留出模式须声明全部属性类型。')
        folds,plan=make_splits(y[dev],groups[dev],cfg)
        if cfg['v4']['search']['mode']=='random':folds=folds[:cfg['v4']['splits']['random_splits']]
        result.update(v4_split_plan=plan,budget=budget_plan(cfg,len(folds)),development_records=len(dev),locked_holdout_records=len(outer))
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description='成品表 → AutoML价格模型 → 效能试用快照 → 推荐草稿；源码入口默认V4。')
    sub=p.add_subparsers(dest='command',required=True)
    for command in ('inspect','build'):
        parser=sub.add_parser(command,help='检查成品输入' if command=='inspect' else '在独立目录构建三环节产物')
        parser.add_argument('input');parser.add_argument('--price-engine',choices=ENGINES,default='optuna_v4');parser.add_argument('--price-config')
        if command=='build':
            parser.add_argument('--output',required=True);parser.add_argument('--product-code',default='SOURCE_PRODUCT');parser.add_argument('--product-name',default='成品价格效能与推荐试用')
            parser.add_argument('--effect-config');parser.add_argument('--virtual-data',action='store_true')
    config=sub.add_parser('config',help='生成V2/V3/V4配置；不会训练')
    config.add_argument('--price-engine',choices=ENGINES,default='optuna_v4');config.add_argument('--profile',choices=['standard','quick','deep'],default='standard');config.add_argument('--output',required=True)
    serve=sub.add_parser('serve',help='启动指定构建结果，不训练')
    serve.add_argument('--output',required=True);serve.add_argument('--activate-draft',action='store_true');serve.add_argument('--no-browser',action='store_true')
    serve.add_argument('--price-port',type=int,default=18701);serve.add_argument('--effect-port',type=int,default=18702);serve.add_argument('--app-port',type=int,default=18791)
    args=p.parse_args(argv)
    from app.console_compat import enable_plain_console
    enable_plain_console()
    try:
        if args.command=='serve':
            from .product_pipeline import serve as run
            return run(args.output,args.activate_draft,args.no_browser,args.price_port,args.effect_port,args.app_port)
        cfg=configuration(args.price_engine,getattr(args,'price_config',None))
        if args.command=='inspect':print(json.dumps(inspect_input(args.input,args.price_engine,cfg),ensure_ascii=False,indent=2));return 0
        if args.command=='config':
            path=Path(args.output)
            if path.exists():raise ValueError('配置文件已存在，请选择新路径以保留现有设置。')
            if args.profile=='quick':
                cfg['search']['n_trials']=40
                if args.price_engine=='optuna_v4':cfg['v4']['splits']['max_splits']=12
            elif args.profile=='deep':cfg['search']['n_trials']=5000
            cfg=config_loader(args.price_engine)(overrides=cfg);path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print('配置已保存：'+str(path));return 0
        from .product_pipeline import build
        build(args.input,args.output,args.product_code,args.product_name,effect_config=args.effect_config,virtual_data=args.virtual_data,
              price_engine=args.price_engine,price_overrides=cfg)
        return 0
    except (ValueError,OSError) as exc:p.exit(1,'流水线执行失败：'+str(exc)+'\n')


if __name__=='__main__':raise SystemExit(main())
