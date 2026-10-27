# -*- coding: utf-8 -*-
"""Validate and replace only a saved product's effectiveness component."""
import argparse,copy,hashlib,json,os,shutil,subprocess,sys,tempfile,uuid,zipfile
from datetime import datetime
from pathlib import Path,PurePosixPath
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from services.price_service.optuna_v5.persistence import atomic_json,replace_atomic

FORMATS=('effectiveness-original-runtime-package-1.0','effectiveness-frozen-runtime-package-1.0')


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def inside(path,root):return Path(path).resolve()==Path(root).resolve() or Path(root).resolve() in Path(path).resolve().parents


def relative(value):
    text=str(value).replace('\\','/');p=PurePosixPath(text)
    if not text or p.is_absolute() or '..' in p.parts or ':' in text:raise ValueError('效能包包含越界路径：'+text)
    return Path(*p.parts)


def select_manifest(path):
    path=Path(path)
    if path.is_file():return path
    exact=list(path.rglob('effectiveness_runtime_manifest.json'))
    if not exact:exact=list(path.rglob('effectiveness_bundle.json'))
    if len(exact)!=1:raise ValueError('请提供恰好一份效能manifest/快照，或指定具体JSON文件。')
    return exact[0]


def safe_extract(source,destination):
    with zipfile.ZipFile(source) as z:
        if len(z.infolist())>10000 or sum(i.file_size for i in z.infolist())>1024**3:raise ValueError('效能包文件数或展开大小超过限额。')
        for item in z.infolist():
            p=relative(item.filename)
            if (item.external_attr>>16)&0o170000==0o120000:raise ValueError('效能ZIP不接受符号链接。')
            target=Path(destination)/p
            if not inside(target,destination):raise ValueError('效能ZIP路径越界。')
            if item.is_dir():target.mkdir(parents=True,exist_ok=True)
            else:target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(z.read(item))


def probe(manifest_path,sample_path):
    from services.effectiveness_service.app import SnapshotBackend,backend_from_package,EffectivenessService
    path=Path(manifest_path);raw=read(path)
    backend=backend_from_package(path) if raw.get('format_version') in FORMATS else SnapshotBackend(path)
    service=EffectivenessService(backend);sample=read(sample_path)
    schema=backend.schema()
    if schema.get('product_code')!=sample.get('product_code'):raise ValueError('专家效能包成品代号不一致：'+str(schema.get('product_code')))
    missing=[f['field_name'] for f in schema.get('fields',[]) if f.get('required',True) and f['field_name'] not in sample['parameters']]
    if missing:raise ValueError('专家包需要未提供的属性代号：'+','.join(missing))
    result=service.handle_post('/api/v1/evaluate',sample)
    return {'schema':schema,'sample_result':result,'backend':'expert_package' if raw.get('format_version') in FORMATS else 'expert_snapshot'}


def install(source,root=ROOT,output=None):
    root=Path(root).resolve()
    if output is None:
        from tools import product_portable
        output=product_portable.latest_output()
    if output is None:raise ValueError('先选择一个已经保存的成品模型版本。')
    output=Path(output).resolve()
    if not inside(output,root/'outputs'):raise ValueError('只能替换本包outputs中的保存版本。')
    manifest_path=output/'prototype_manifest.json';old=read(manifest_path)
    if old.get('status')!='ready':raise ValueError('目标版本尚未构建完成。')
    from .process_group import process_alive
    if (output/'running.json').is_file() and process_alive(read(output/'running.json').get('pid')):raise ValueError('请先关闭该版本的START窗口，再导入专家效能包。')
    price_path=(output/old['price_model']).resolve();price_sha=sha(price_path)
    if price_sha!=old['artifact_sha256']['price_model']:raise ValueError('现有价格模型摘要不一致，停止替换。')
    business=read(output/old['business_package']);data=business.get('data') or (business.get('release') or {}).get('data') or {}
    samples=data.get('agreements') or []
    if not samples:raise ValueError('成品业务包没有可用于验证字段的历史记录。')
    sample_params=samples[0].get('params') or samples[0].get('parameters') or {}
    if not sample_params:raise ValueError('业务记录缺少参数，不能验证效能接口。')
    effects=output/'effectiveness';effects.mkdir(exist_ok=True)
    source=Path(source).resolve()
    if not source.exists():raise ValueError('效能包路径不存在。')
    with tempfile.TemporaryDirectory(prefix='.effect_import_',dir=str(effects)) as temporary:
        work=Path(temporary);origin=source
        if source.is_file() and source.suffix.lower()=='.zip':safe_extract(source,work/'unpacked');origin=work/'unpacked'
        selected=select_manifest(origin);raw=read(selected);payload=work/'payload';payload.mkdir()
        kind='expert_package' if raw.get('format_version') in FORMATS else 'expert_snapshot'
        name='effectiveness_runtime_manifest.json' if kind=='expert_package' else 'effectiveness_bundle.json'
        destination=payload/name;destination.write_bytes(selected.read_bytes())
        if kind=='expert_package':
            for key in ('source_root','model','workbook','state'):
                if raw.get(key):
                    p=relative(raw[key])
                    if not inside(selected.parent/p,selected.parent):raise ValueError('效能包声明路径越界。')
            if not raw.get('files'):raise ValueError('专家运行包缺少文件摘要清单。')
            for item in raw['files']:
                rel=relative(item.get('path'));src=selected.parent/rel
                if not inside(src,selected.parent) or not src.is_file() or sha(src)!=item.get('sha256'):raise ValueError('专家效能包文件校验失败：'+str(rel))
                dest=payload/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(str(src),str(dest))
        sample=work/'sample.json';atomic_json(sample,{'product_code':old['product_code'],'parameters':sample_params})
        probe_result=work/'probe_result.json'
        command=[sys.executable,'-X','utf8',str(ROOT/'tools/product_effectiveness_import.py'),'probe','--source',str(destination),'--sample',str(sample),'--report',str(probe_result)]
        env=dict(os.environ,PYTHONIOENCODING='utf-8',EFFECT_SOURCE_ROOT='',EFFECT_WORKBOOK='',EFFECT_RUNTIME_PACKAGE='')
        run=subprocess.run(command,cwd=str(root),env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if run.returncode:raise ValueError('专家效能包加载/试算失败，旧效能保留：'+run.stderr.decode('utf-8',errors='replace')[-3000:])
        checked=read(probe_result);schema=checked['schema']
        if schema.get('product_code')!=old['product_code']:raise ValueError('专家包成品代号与价格/业务成品不一致，请在专家软件导出时使用：'+old['product_code'])
        fields=schema.get('fields') or []
        missing=[f['field_name'] for f in fields if f.get('required',True) and f['field_name'] not in sample_params]
        if missing:raise ValueError('专家包需要业务表未提供的属性代号：'+','.join(missing))
        specs={p['parameter_id']:p for p in data.get('parameters',[])}
        for f in fields:
            current=specs.get(f['field_name']) or {};unit=current.get('unit')
            if unit and f.get('unit') and str(unit).strip()!=str(f['unit']).strip():raise ValueError('效能属性单位不一致，须显式统一单位：'+f['field_name'])
            previous=current.get('value_type');incoming=str(f.get('dtype') or 'number').lower()
            old_enum=previous in ('enum','text','boolean');new_enum=incoming in ('enum','text','string','categorical','category','boolean','bool')
            if previous and old_enum!=new_enum:raise ValueError('效能属性类型不一致，须先明确数值/配置类型映射：'+f['field_name'])
        stamp=datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f');target=effects/('expert_'+stamp+'_'+uuid.uuid4().hex[:6])
        if not inside(target,effects) or not inside(payload,effects):raise ValueError('效能提交路径越界。')
        replace_atomic(payload,target)
        installed=target/name;updated=copy.deepcopy(old)
        updated.update(effectiveness_model=installed.relative_to(output).as_posix(),effectiveness_backend=kind,
            effectiveness_integration={'source_name':source.name,'installed_at_utc':stamp,'model_version':schema.get('model_version'),
                'schema':schema,'backend':kind,'declared_expert_confirmation':raw.get('expert_confirmation'),
                'validation':'hash_contract_and_actual_sample_evaluation','price_retrained':False})
        updated['artifact_sha256']['effectiveness_model']=sha(installed)
        backup=output/'backups'/('prototype_manifest.before_effect_'+stamp+'.json');backup.parent.mkdir(exist_ok=True);atomic_json(backup,old)
        if sha(price_path)!=price_sha:raise AssertionError('效能导入不得修改价格模型。')
        atomic_json(manifest_path,updated)
        return {'status':'IMPORTED','output':str(output),'effectiveness_model':str(installed),'model_version':schema.get('model_version'),
                'backend':kind,'price_retrained':False,'price_sha256':price_sha,'backup_manifest':str(backup)}


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('command',choices=['install','probe'],nargs='?',default='install')
    p.add_argument('--source');p.add_argument('--sample');p.add_argument('--output');p.add_argument('--report');a=p.parse_args(argv)
    try:
        source=a.source or input('请输入专家效能ZIP、包目录或JSON完整路径：').strip().strip('"')
        result=probe(source,a.sample) if a.command=='probe' else install(source,ROOT,a.output)
        if a.report:atomic_json(Path(a.report),result)
        print(json.dumps(result,ensure_ascii=False));return 0
    except Exception as exc:print('效能包处理失败：'+str(exc),file=sys.stderr);return 1


if __name__=='__main__':sys.exit(main())
