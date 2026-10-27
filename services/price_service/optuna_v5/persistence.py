# -*- coding: utf-8 -*-
"""Atomic local checkpoints; identity and runtime must match before unpickling."""
import hashlib
import json
import os
import pickle
import sys
import time
from pathlib import Path
import numpy as np
import optuna
import sklearn
import scipy
import struct


def replace_atomic(source,destination):
    # Antivirus/indexers may briefly open a freshly written file on Windows.
    # Preserve the committed destination; never delete it to force replacement.
    for attempt in range(7):
        try:
            os.replace(str(source),str(destination));return
        except PermissionError:
            if attempt==6:raise
            time.sleep(min(.05*2**attempt,.5))


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8');replace_atomic(tmp,path)


def environment():return {'python':list(sys.version_info[:3]),'bits':struct.calcsize('P')*8,'numpy':np.__version__,'scipy':scipy.__version__,'sklearn':sklearn.__version__,'optuna':optuna.__version__}


def save_state(path,value,identity):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    # Content-addressed snapshots leave the previous committed metadata valid
    # if the process dies between writing data and atomically updating its link.
    payload=pickle.dumps(value,protocol=4);digest=hashlib.sha256(payload).hexdigest();blob=path.with_name(path.name+'.'+digest[:16]+'.pkl')
    if not blob.exists():
        tmp=blob.with_name(blob.name+'.tmp');tmp.write_bytes(payload);replace_atomic(tmp,blob)
    meta=path.with_suffix('.json');old=None
    if meta.is_file():old=json.loads(meta.read_text(encoding='utf-8')).get('blob')
    atomic_json(meta,{'format':'price-v5-checkpoint-1.0','identity':identity,'environment':environment(),'sha256':digest,'blob':blob.name})
    if old and old!=blob.name:
        previous=path.parent/old
        if previous.parent==path.parent and previous.is_file():previous.unlink()


def load_state(path,identity):
    path=Path(path);meta=path.with_suffix('.json')
    if not meta.is_file():return None
    info=json.loads(meta.read_text(encoding='utf-8-sig'))
    if info.get('format')!='price-v5-checkpoint-1.0' or info.get('identity')!=identity or info.get('environment')!=environment():raise ValueError('V5检查点数据、设置或环境不一致，拒绝复用。请更换检查点目录或关闭resume。')
    blob=path.parent/info['blob']
    if blob.parent.resolve()!=path.parent.resolve() or not blob.is_file():raise ValueError('V5检查点路径或文件无效。')
    payload=blob.read_bytes()
    if hashlib.sha256(payload).hexdigest()!=info['sha256']:raise ValueError('V5检查点摘要不一致，已有模型未改动。')
    return pickle.loads(payload)


class BaselineObserver(object):
    def __init__(self,directory,identity,interval=10,resume=True,finish='fixed_budget',progress=None,train_gate=None,enabled=True):
        self.directory=Path(directory);self.identity=identity;self.interval=interval;self.resume=resume;self.finish=finish;self.progress=progress or print
        self.directory.mkdir(parents=True,exist_ok=True);self.restored=False;self.best_key=None;self.best_record=None;self.last_saved=0;self.started=time.monotonic();self.elapsed_before=0.
        self.train_gate=train_gate;self.qualified_seen=False;self.feasible_key=None;self.restored_fits=0
        self.enabled=enabled;self.feasible_snapshot=None
        self.trace=None
    def restore(self,driver):
        value=load_state(self.directory/'baseline',self.identity) if self.resume and self.enabled else None
        if value is None:return None
        self.restored=True;self.best_key=value['best_key'];self.best_record=value['best_record'];self.elapsed_before=value.get('elapsed',0.)
        self.qualified_seen=value.get('qualified_seen',False);self.feasible_key=value.get('feasible_key');self.restored_fits=value.get('stats',{}).get('model_fits',0)
        driver.backend.stats.update(value.get('stats',{}))
        self.progress('V5恢复V3基线：已完成%d轮；采样状态已恢复，预测缓存冷启动。'%sum(value['counts'].values()))
        return value
    def should_stop(self,driver):
        return self.finish=='targets_met' and self.qualified_seen
    def after_trial(self,driver,study,trial,phase):
        record={'number':trial.number,'phase':phase,'valid':bool(trial.user_attrs.get('valid')),'parameters':trial.params,'values':trial.values,'train_metrics':trial.user_attrs.get('train_metrics'),'error':trial.user_attrs.get('error')}
        if self.enabled:
            with (self.directory/'baseline_trials.jsonl').open('a',encoding='utf-8') as handle:handle.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
        changed=False
        if record['valid']:
            r,m=map(float,trial.values);miss=max(0.,driver.r2_target-r)/max(.1,abs(driver.r2_target))+max(0.,m-driver.mape_target)/max(driver.mape_target,1e-8)
            key=(miss>0,miss,m,-r)
            if self.best_key is None or key<self.best_key:
                self.best_key=key;self.best_record=record;changed=True
                captured=driver.backend.last_evaluation
                if self.enabled and captured and captured.get('bundle') is not None:
                    save_state(self.directory/'search_champion',{'bundle':captured['bundle'],'record':record,'train_rows':captured['train_rows'],'validation_rows':captured['validation_rows']},self.identity)
            g=self.train_gate;train=trial.user_attrs.get('train_metrics',{});train_ok=True
            if g is not None and g['mode']!='off':
                train_ok=train.get('mape_percent') is not None and train['mape_percent']<=g['mape_percent_max'] and (g['r2_min'] is None or train.get('r2') is not None and train['r2']>=g['r2_min'])
            if miss==0 and train_ok:
                self.qualified_seen=True;feasible_key=(m,-r)
                if self.feasible_key is None or feasible_key<self.feasible_key:
                    self.feasible_key=feasible_key;captured=driver.backend.last_evaluation
                    if captured and captured.get('bundle') is not None:
                        self.feasible_snapshot=captured['bundle']
                        if self.enabled:save_state(self.directory/'feasible_champion',{'bundle':captured['bundle'],'record':record},self.identity)
        count=sum(driver.counts.values())
        if self.trace is not None:
            self.trace.emit('v3_trial_complete',trial=record,monitor_best=self.best_record,model_fits=dict(driver.backend.stats))
        if changed or count-self.last_saved>=self.interval:self.flush(driver,study,phase)
    def flush(self,driver,study,phase):
        if not self.enabled:return
        value={'study':study,'phase':phase,'counts':dict(driver.counts),'history':driver.history,'anchors':driver.anchors,
               'tpe_history_count':getattr(driver,'tpe_history_count',0),'best_key':self.best_key,'best_record':self.best_record,
               'stats':dict(driver.backend.stats),'elapsed':self.elapsed_before+time.monotonic()-self.started}
        value.update(qualified_seen=self.qualified_seen,feasible_key=self.feasible_key)
        save_state(self.directory/'baseline',value,self.identity);self.last_saved=sum(driver.counts.values())
