"""Offline operator entry: inspect -> existing V3 -> verify -> native EXE package."""
import argparse
from datetime import datetime
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
KIT=ROOT.parent
from product.common import read_json,write_json,sha,InstanceLock
from product.supervisor import fetch
from product.launcher import running
from product.desktop_config import DEFAULT_PORTS,validate_config
from tools.source_provenance import source_commit


def verify_kit(kit=KIT):
    kit=Path(kit).resolve();manifest=read_json(kit/'KIT_MANIFEST.json')
    if manifest.get('format')!='pipeline-client-build-kit-1.0':raise ValueError('离线开发包清单无效')
    for relative,expected in manifest['files'].items():
        path=(kit/relative).resolve()
        if kit not in path.parents or not path.is_file() or sha(path)!=expected:
            raise ValueError('开发包文件缺失或改变：'+relative)
    if sys.version_info[:3]!=(3,8,10) or sys.maxsize<=2**32:
        raise ValueError('请使用包内 CPython 3.8.10 x64 启动脚本')
    if (kit/'runtime/python38').resolve() not in Path(sys.executable).resolve().parents:
        raise ValueError('请使用包内解释器')
    for name,expected in manifest['training_dependencies'].items():
        if metadata.version(name)!=expected:raise ValueError('训练依赖版本不匹配：'+name)
    return manifest


def free_ports():
    sockets=[];ports={}
    try:
        for role in DEFAULT_PORTS:
            sock=socket.socket();sock.bind(('127.0.0.1',0));sockets.append(sock);ports[role]=sock.getsockname()[1]
    finally:
        for sock in sockets:sock.close()
    return {'ports':ports,'open_browser':False}


def new_job(prefix='build'):
    path=ROOT/'outputs/client_jobs'/('%s_%s_%s'%(prefix,datetime.now().strftime('%Y%m%d_%H%M%S'),uuid.uuid4().hex[:6]))
    path.mkdir(parents=True)
    return path


def environment_check():
    manifest=verify_kit()
    import numpy as np
    from scipy.optimize import minimize
    from sklearn.linear_model import Ridge
    import optuna
    result=minimize(lambda value:float((value[0]-2)**2),[0.],method='SLSQP')
    if not result.success:raise ValueError('SciPy 数值运行库自检失败')
    Ridge().fit(np.arange(8).reshape(-1,1),np.arange(8))
    return {'status':'pass','python':sys.version,'executable':sys.executable,
            'dependencies':manifest['training_dependencies'],'external_python_required':False,'network_required':False,
            'target':'Windows 7 SP1 x64','win7_machine_validated':False,'source_sha':source_commit(ROOT)}


def settings():
    raw=read_json(KIT/'settings/project.json')
    if set(raw)!={'product_code','product_name','input_file','data_kind'}:raise ValueError('project.json 配置项不完整或存在未知项目')
    if not re.fullmatch('[A-Za-z][A-Za-z0-9_]*',raw['product_code']):raise ValueError('产品编码须使用英文字母、数字和下划线，以字母开头')
    if not raw['product_name'].strip():raise ValueError('产品名称不能为空')
    if raw['data_kind'] not in ('synthetic','customer'):raise ValueError('data_kind 只能是 synthetic 或 customer')
    source=Path(raw['input_file']);source=source if source.is_absolute() else KIT/source
    if not source.is_file() or source.suffix.lower() not in ('.xlsx','.csv'):raise ValueError('请提供存在的双行表头 products.xlsx 或 CSV')
    raw['source']=source.resolve()
    return raw


def configuration(quick=False):
    from automl_pipeline.product_price_optuna_v3 import load_config
    cfg=load_config(KIT/'settings/v3.json')
    if quick:cfg['search'].update(n_trials=40,timeout_seconds=600)
    return load_config(overrides=cfg)


def smoke(package,job):
    data=job/'smoke-userdata';write_json(data/'config/desktop.json',free_ports())
    env=dict(os.environ,PATH='')
    for key in list(env):
        if key.startswith(('PYTHON','IPDEMO_','_PYI','_MEIPASS')):env.pop(key,None)
    process=subprocess.Popen([str(package/'product.exe'),'--no-browser','--data-dir',str(data)],
                             cwd=str(job),env=env,creationflags=subprocess.CREATE_NO_WINDOW)
    started=time.monotonic();report={'configured_ports':read_json(data/'config/desktop.json')['ports']}
    try:
        current=None
        while time.monotonic()-started<100:
            if process.poll() is not None:raise RuntimeError('交付 EXE 启动失败，请查看 '+str(data/'logs'))
            item=running(data)
            if item and item[1]['status']=='ready':current=item;break
            time.sleep(.2)
        if not current:raise TimeoutError('交付 EXE 启动超时')
        control,status=current
        if status['ports']!=report['configured_ports']:raise ValueError('交付服务未使用指定端口')
        web=status['services']['web']['url'];schema=fetch(web+'/api/deep-price/v1/schema')
        products=fetch(web+'/api/deep-price/v1/products')['items'];fields={row['field_id'] for row in schema['fields']}
        params={key:value for key,value in products[0]['parameters'].items() if key in fields}
        identity={key:schema[key] for key in ('product_code','release_id','model_version','price_model_sha256',
                  'model_instance_id','business_snapshot_sha256','price_output_signature')}
        deep=fetch(web+'/api/deep-price/v1/predict',dict(identity,parameters=params))
        simple=fetch(web+'/api/price-workbench/predict',{'parameters':params})
        recommendation=fetch(web+'/api/evaluate',{'parameters':params})
        values=[deep['predicted_price_wan'],simple['prediction']['predicted_price_wan'],recommendation['predicted_price_wan']]
        if max(values)-min(values)>1e-8:raise ValueError('推荐、简易与深度价格不一致')
        report.update(status='pass',startup_seconds=round(time.monotonic()-started,3),prices_wan=values,
                      field_count=len(fields),model_sha256=schema['price_model_sha256'],portal_url=status['portal_url'])
        write_json(job/'delivery_smoke.json',report)
        return report
    finally:
        current=running(data)
        if current:
            try:fetch(current[0]['url']+'/api/stop',{},current[0]['token'])
            except OSError:pass
        try:process.wait(timeout=20)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)


def package_release(train,job,desktop_file):
    from tools.build_product_exe import build
    version='client-v3-'+datetime.now().strftime('%Y%m%d_%H%M%S')+'-'+uuid.uuid4().hex[:4]
    package=ROOT/'deliverables'/version
    report=build(train,KIT/'runtime/python38',KIT/'bootstrap',package,version,KIT/'bootstrap',(),desktop_file)
    smoke(package,job)
    destination=KIT/'deliverables';destination.mkdir(exist_ok=True)
    archive=Path(report['archive']);published=destination/archive.name
    if published.exists():raise ValueError('交付压缩包已存在，不能覆盖')
    shutil.copy2(str(archive),str(published))
    Path(str(published)+'.sha256').write_text(sha(published)+'  '+published.name+'\n',encoding='ascii')
    report.update(published_archive=str(published),published_sha256=sha(published))
    write_json(job/'package_report.json',report)
    return report


def build_job(quick=False,self_test=False,repack=False):
    environment_check();job=new_job('selftest' if self_test else 'repack' if repack else 'build')
    status={'status':'running','stage':'input','job':str(job),'engine':'optuna_v3','win7_machine_validated':False}
    def save(stage=None):
        if stage:status['stage']=stage;print('['+stage+']',flush=True)
        write_json(job/'client_build_status.json',status)
    try:
        if repack:
            previous=read_json(KIT/'LAST_TRAINED.json');train=(KIT/previous['release']).resolve()
            if ROOT/'outputs' not in train.parents:raise ValueError('已保存模型目录必须属于本包 outputs')
            save('reuse_trained_release')
        else:
            raw=settings()
            if self_test:raw.update(source=KIT/'examples/products_example.xlsx',product_code='KIT_SELF_TEST',product_name='离线构建包验收产品',data_kind='synthetic')
            known=read_json(KIT/'KIT_MANIFEST.json')['example_sha256']
            if raw['data_kind']=='customer' and sha(raw['source'])==known:
                raise ValueError('当前仍是虚拟示例表，请替换为甲方真实数据或标记为 synthetic')
            snapshot=job/'input'/raw['source'].name;snapshot.parent.mkdir();shutil.copy2(str(raw['source']),str(snapshot))
            cfg=configuration(quick or self_test);write_json(job/'training_config.json',cfg)
            from automl_pipeline.__main__ import inspect_input
            inspected=inspect_input(snapshot,'optuna_v3',cfg);write_json(job/'input_inspection.json',inspected)
            status.update(product_code=raw['product_code'],product_name=raw['product_name'],source_sha256=sha(snapshot),
                          classification='synthetic' if raw['data_kind']=='synthetic' else 'unconfirmed')
            save('optuna_v3_training')
            from automl_pipeline.product_pipeline import build
            train=job/'trained_release'
            manifest=build(snapshot,train,raw['product_code'],raw['product_name'],virtual_data=raw['data_kind']=='synthetic',
                           price_engine='optuna_v3',price_overrides=cfg)
            save('http_consistency_validation')
            from tools.verify_v3_release import verify
            verification=verify(train,job/'api_validation',status['classification'],repeats=1)
            if verification['status']!='pass':raise ValueError('统一价格接口验收失败')
            status['targets_met']=manifest['targets_met']
            write_json(KIT/'LAST_TRAINED.json',{'release':train.relative_to(KIT).as_posix(),
                       'source_sha256':manifest['source_sha256'],'model_sha256':manifest['artifact_sha256']['price_model']})
        desktop_file=KIT/'settings/desktop.json'
        validate_config(read_json(desktop_file))
        if self_test:
            desktop_file=job/'desktop-test.json';write_json(desktop_file,free_ports())
        save('assemble_product_exe')
        report=package_release(train,job,desktop_file)
        status.update(status='pass',stage='complete',release=str(train),delivery_zip=report['published_archive'],
                      delivery_sha256=report['published_sha256'])
        save();write_json(KIT/'LAST_SUCCESS.json',status)
        receipt=Path(report['published_archive']).with_suffix('.receipt.json');write_json(receipt,status)
        print('\n构建完成：'+report['published_archive']+'\nSHA256：'+report['published_sha256'],flush=True)
        return status
    except Exception as exc:
        status.update(status='fail',error=str(exc));save()
        (job/'error.txt').write_text(traceback.format_exc(),encoding='utf-8')
        raise


def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['check','inspect','build','self-test','repack']);p.add_argument('--quick',action='store_true');a=p.parse_args()
    try:
        if a.command=='check':print(json.dumps(environment_check(),ensure_ascii=False,indent=2));return 0
        if a.command=='inspect':
            environment_check();raw=settings()
            from automl_pipeline.__main__ import inspect_input
            print(json.dumps(inspect_input(raw['source'],'optuna_v3',configuration(a.quick)),ensure_ascii=False,indent=2));return 0
        (KIT/'logs').mkdir(exist_ok=True)
        lock=InstanceLock(KIT/'builder_state')
        try:build_job(a.quick,a.command=='self-test',a.command=='repack')
        finally:lock.close()
        return 0
    except Exception as exc:
        print('构建失败：'+str(exc),file=sys.stderr,flush=True);return 1


if __name__=='__main__':raise SystemExit(main())
