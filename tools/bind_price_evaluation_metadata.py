"""Bind actual training report fields to an unchanged price artifact."""
import argparse
import hashlib
import json
from pathlib import Path


def bind(model,report,output,classification='unconfirmed'):
    model,report,output=Path(model),Path(report),Path(output)
    if output.exists():raise ValueError('不能覆盖既有元数据')
    source=json.loads(report.read_text(encoding='utf-8-sig'))
    if source.get('engine')!='optuna_v3':raise ValueError('本验收入口要求真实Optuna V3训练报告')
    keys=('selected_training_metrics','full_refit_training_metrics','deployed_training_metrics','validation_metrics',
          'validation_protocol','evaluation_scope','deployment_scope','business_accuracy_certified','training_rows',
          'feature_count','member_count','trials_completed','trials_requested')
    result={'schema_version':'price-model-evaluation-metadata-1.0',
            'data_classification':classification,
            'price_model_sha256':hashlib.sha256(model.read_bytes()).hexdigest(),
            'source_training_report_sha256':hashlib.sha256(report.read_bytes()).hexdigest(),
            'metrics':{key:source[key] for key in keys if source.get(key) is not None}}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return output


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--model',required=True);p.add_argument('--training-report',required=True);p.add_argument('--output',required=True);p.add_argument('--classification',choices=['formal','synthetic','unconfirmed'],default='unconfirmed');a=p.parse_args();bind(a.model,a.training_report,a.output,a.classification)
