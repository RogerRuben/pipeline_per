# -*- coding: utf-8 -*-
"""Read an existing V7 comparison; no training, model updates or input required."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.product_portable import latest_output,write_json
from automl_pipeline.product_optuna_v4_adapter import write_csv

def compare(version=None):
    out=Path(version).resolve() if version else latest_output()
    if out is None or ROOT/'outputs' not in out.parents:raise ValueError('请先选择包内已完成V7版本。')
    r=json.loads((out/'price/training_report.json').read_text(encoding='utf-8'))
    if r.get('engine')!='optuna_v7':raise ValueError('当前版本不是V7；请选V7保存模型再比较。')
    base=r['baseline_v3'];rows=[]
    for name,a,b in [('实际全量训练',base['full_training'],r['train_metrics']),('原V3划分开发',base['primary'],r['selected_split_development_metrics']),('相同共同开发CV',base['common_development'],r['common_development_metrics'])]:
        rows.append({'范围':name,'原V3_MAPE%':a['mape_percent'] if a else None,'V3+_MAPE%':b['mape_percent'] if b else None,
            '原V3_R2':a['r2'] if a else None,'V3+_R2':b['r2'] if b else None})
    report={'format':'v3plus-comparison-1.0','training_called':False,'independent_test_provided':False,'baseline_sha256':base['artifact_sha256'],
        'rows':rows,'enhancement_applied':r['enhancement_applied'],'selected_model':r['selected_model'],'consistency':r['expert_grade_consistency'],
        'candidate_reviews':r['candidate_reviews'],'warning':r['warning']}
    write_json(out/'price/comparison_v3plus.json',report);write_csv(out/'price/comparison_v3plus.csv',rows)
    print(json.dumps(report,ensure_ascii=False,indent=2));return report

def main():
    p=argparse.ArgumentParser();p.add_argument('--version');a=p.parse_args()
    try:compare(a.version);return 0
    except Exception as e:print('V3+对照读取失败：'+str(e),file=sys.stderr);return 1
if __name__=='__main__':sys.exit(main())
