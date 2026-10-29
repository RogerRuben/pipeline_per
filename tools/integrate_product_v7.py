# -*- coding: utf-8 -*-
"""Reviewed additive hooks; never replace a historical optimizer or model class."""
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def edit(name,old,new,all=False):
    p=ROOT/name;s=p.read_text(encoding='utf-8')
    if new in s:return
    if old not in s:raise ValueError('Missing V7 integration anchor: '+name+' '+old[:70])
    p.write_text(s.replace(old,new) if all else s.replace(old,new,1),encoding='utf-8')

def main():
    # Add V7 to shared routing without changing the older engine-specific paths.
    for file in ('automl_pipeline/product_pipeline.py','automl_pipeline/product_optuna_versions.py','tools/product_portable.py','automl_pipeline/__main__.py'):
        p=ROOT/file;s=p.read_text(encoding='utf-8')
        for a,b in [("'optuna_v5','optuna_v6')","'optuna_v5','optuna_v6','optuna_v7')"),
            ('"optuna_v5","optuna_v6")','"optuna_v5","optuna_v6","optuna_v7")'),
            ("'optuna_v5','optuna_v6']","'optuna_v5','optuna_v6','optuna_v7']"),
            ('"optuna_v5", "optuna_v6"]','"optuna_v5", "optuna_v6", "optuna_v7"]')]:s=s.replace(a,b)
        p.write_text(s,encoding='utf-8')
    edit('automl_pipeline/product_pipeline.py',"elif price_engine=='optuna_v6':\n                from .product_price_optuna_v6 import load_config as load_optuna_config",
         "elif price_engine=='optuna_v7':\n                from .product_price_optuna_v7 import load_config as load_optuna_config\n            elif price_engine=='optuna_v6':\n                from .product_price_optuna_v6 import load_config as load_optuna_config",True)
    edit('automl_pipeline/product_pipeline.py',"            if price_engine=='optuna_v6':\n                from .product_price_optuna_v6 import train_price_optuna_v6",
         "            if price_engine=='optuna_v7':\n                from .product_price_optuna_v7 import train_price_optuna_v7\n                price_bundle,price_report=train_price_optuna_v7(data,output/\"price\",price_config,progress,optuna_config,input_path=source)\n            elif price_engine=='optuna_v6':\n                from .product_price_optuna_v6 import train_price_optuna_v6")
    edit('automl_pipeline/product_pipeline.py',"        if price_engine=='optuna_v6':\n            from tools.product_saved_start import fixed_version_launcher_bytes",
         "        if price_engine=='optuna_v7':\n            from tools.product_saved_start import fixed_version_launcher_bytes\n            note='# V7 / V3+成品构建\\n\\n'+price_report['warning']+'\\n\\n训练：'+str(price_report['train_metrics'])+'\\n共同开发：'+str(price_report['common_development_metrics'])+'\\n专家分档：'+str(price_report['expert_grade_consistency'])+'\\n'\n            (output/'构建结果.md').write_text(note,encoding='utf-8')\n            (output/'START_PROTOTYPE_WIN7.bat').write_bytes(fixed_version_launcher_bytes())\n            status.update(status='completed',stage='complete',targets_met=manifest['targets_met']);progress('V7/V3+价格、效能与推荐草稿构建完成：'+str(output));return manifest\n        if price_engine=='optuna_v6':\n            from tools.product_saved_start import fixed_version_launcher_bytes")
    edit('automl_pipeline/__main__.py',"    if engine=='optuna_v6':from .product_price_optuna_v6 import load_config",
         "    if engine=='optuna_v7':from .product_price_optuna_v7 import load_config\n    elif engine=='optuna_v6':from .product_price_optuna_v6 import load_config")
    edit('automl_pipeline/__main__.py',"    if engine=='optuna_v6':\n        from services.price_service.optuna_v6.protocol import make_plan",
         "    if engine=='optuna_v7':\n        from services.price_service.optuna_v7.review import make_folds\n        _,plan=make_folds(frame,np.arange(len(frame)),cfg)\n        result.update(common_development_plan=plan,budget=cfg['v7']['budget'],expert_consistency=cfg['v7']['consistency'])\n    elif engine=='optuna_v6':\n        from services.price_service.optuna_v6.protocol import make_plan")
    edit('automl_pipeline/__main__.py',"default='optuna_v6'","default='optuna_v7'",True)
    edit('automl_pipeline/__main__.py',"                if args.price_engine=='optuna_v6':cfg['search']['n_trials']=12;",
         "                if args.price_engine=='optuna_v7':cfg['search']['timeout_seconds']=300;cfg['v7']['budget'].update(max_extra_fits=128,extra_seconds=90)\n                if args.price_engine=='optuna_v6':cfg['search']['n_trials']=12;")
    edit('automl_pipeline/__main__.py',"                if args.price_engine=='optuna_v6':cfg['search']['n_trials']=120;",
         "                if args.price_engine=='optuna_v7':cfg['search'].update(n_trials=5000,timeout_seconds=1800);cfg['v7']['budget'].update(max_extra_fits=1000,extra_seconds=300)\n                elif args.price_engine=='optuna_v6':cfg['search']['n_trials']=120;")
    edit('automl_pipeline/product_optuna_v2_input.py',"    elif engine=='optuna_v6':\n        from .product_price_optuna_v6 import load_config as loader",
         "    elif engine=='optuna_v7':\n        from .product_price_optuna_v7 import load_config as loader\n    elif engine=='optuna_v6':\n        from .product_price_optuna_v6 import load_config as loader")
    edit('automl_pipeline/product_optuna_v2_input.py',"    if engine=='optuna_v6':\n        from services.price_service.optuna_v6.protocol import make_plan",
         "    if engine=='optuna_v7':\n        from services.price_service.optuna_v7.review import make_folds\n        _,review_plan=make_folds(frame,np.arange(len(y)),cfg)\n        split_report=dict(split_report,common_development_plan=review_plan)\n        p=cfg['v7']['persistence'];p['directory']=str((root/(p['directory'] or 'state/v7')).resolve()) if not p['directory'] or not Path(p['directory']).is_absolute() else p['directory']\n        if cfg['v7']['consistency']['mode']!='off' and any(float(x)!=int(float(x)) for x in cfg['expert']['scores'].values()):raise ValueError('五档指标要求1—5整数评分。')\n    if engine=='optuna_v6':\n        from services.price_service.optuna_v6.protocol import make_plan")
    edit('automl_pipeline/product_optuna_v2_input.py',"    if engine=='optuna_v3':\n        warm=cfg['v3']['warm_start']", "    if engine in ('optuna_v3','optuna_v7'):\n        warm=cfg['v3']['warm_start']")
    edit('automl_pipeline/product_optuna_v2_input.py',"    if engine=='optuna_v6':code_files+=", "    if engine=='optuna_v7':code_files+=('automl_pipeline/product_price_optuna_v7.py','automl_pipeline/product_optuna_v7_adapter.py',\n        'services/price_service/optuna_v7/backend.py','services/price_service/optuna_v7/engine.py','services/price_service/optuna_v7/review.py','services/price_service/optuna_v7/importance.py',\n        'services/price_service/optuna_v2/optimizer_v3.py','services/price_service/optuna_v2/cached_backend.py')\n    if engine=='optuna_v6':code_files+=")
    edit('automl_pipeline/product_optuna_v2_input.py',"    if engine=='optuna_v6':\n        notes[0]=", "    if engine=='optuna_v7':\n        notes[0]='V7/V3+：完整保留原V3对照，少量候选共同开发复核、训练种子平均和受约束融合。'\n        notes[2]='复核与原划分均参与选模，不能称独立测试；专家五档错分率可选，默认关闭。START不训练。'\n    if engine=='optuna_v6':\n        notes[0]=")
    edit('tools/product_export_saved.py',"'optuna_v6':'ProductOptunaV6_Saved_'", "'optuna_v6':'ProductOptunaV6_Saved_','optuna_v7':'ProductOptunaV7_Saved_'")
    edit('tools/pipeline_source_layout.py',"'product_price_optuna_v6','product_optuna_v6_adapter'", "'product_price_optuna_v6','product_optuna_v6_adapter','product_price_optuna_v7','product_optuna_v7_adapter'")
    edit('tools/pipeline_source_layout.py',"    'automl_pipeline/configs/price_optuna_v6.json',", "    'automl_pipeline/configs/price_optuna_v6.json','automl_pipeline/configs/price_optuna_v7.json',")
    edit('tools/upgrade_product_optuna_v2.py'," 'tools/product_price_optuna_v6.py','tools/product_optuna_v6_adapter.py',", " 'tools/product_price_optuna_v6.py','tools/product_optuna_v6_adapter.py',\n 'tools/product_price_optuna_v7.py','tools/product_optuna_v7_adapter.py','services/price_service/optuna_v7/__init__.py',\n 'services/price_service/optuna_v7/backend.py','services/price_service/optuna_v7/engine.py','services/price_service/optuna_v7/review.py','services/price_service/optuna_v7/importance.py',")
    edit('services/price_service/app.py',"'weights_fitted_on_validation','test_metrics'):", "'weights_fitted_on_validation','test_metrics','feature_importance','expert_grade_consistency'):")

if __name__=='__main__':main()
