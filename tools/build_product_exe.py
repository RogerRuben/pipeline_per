"""Package only the precompiled software engine, models and external config."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from product.common import read_json,write_json,sha,verify_package
from tools.verify_v3_release import preflight
from tools.bind_price_evaluation_metadata import bind
from tools.build_frozen_engine import engine_sources
from tools.source_provenance import source_commit
from product.compatibility import effectiveness_schema_from_document, make_contract
from product.desktop_config import template_config,validate_config

FORBIDDEN={'.py','.pyw','.ipynb','.pdb','.spec','.c','.cpp','.h','.bat','.ps1'}

def audit_runtime(out):
    forbidden=[p.relative_to(out).as_posix() for p in Path(out).rglob('*')
               if p.is_file() and p.suffix.lower() in FORBIDDEN]
    if forbidden:raise ValueError('Runtime contains source or build scripts: '+str(forbidden))
    for directory in ('payload','project','runtime/python38','app','tools','automl_pipeline'):
        if (Path(out)/directory).exists():raise ValueError('Development directory in runtime: '+directory)

def build(release_dir,runtime,tool_dir,out,version,bootstrap=None,legacy_packages=(),desktop_config=None,effectiveness_package=None):
    if sys.version_info[:3]!=(3,8,10) or sys.maxsize<=2**32:
        raise ValueError('Use the pinned CPython 3.8.10 x64 builder')
    if not bootstrap:raise ValueError('A precompiled full software engine is required')
    bootstrap=Path(bootstrap).resolve();meta=read_json(bootstrap/'BOOTSTRAP_META.json')
    if meta.get('kind')!='pyinstaller-full-app-onedir':
        raise ValueError('Source-based launcher packages are no longer accepted')
    sources=engine_sources()
    if read_json(bootstrap/'BOOTSTRAP_SOURCE.json')!=sources:
        raise ValueError('Frozen software engine differs from the current application sources')
    if sha(bootstrap/'product.exe')!=meta['exe_sha256']:raise ValueError('Engine EXE hash changed')
    if meta.get('expert_runtime') and sha(bootstrap/'expert/effect_worker.exe')!=meta['expert_runtime']['exe_sha256']:raise ValueError('Expert EXE hash changed')
    manifest,artifacts,source,environment=preflight(release_dir,'unconfirmed')
    out=Path(out).resolve()
    if out.exists() or ROOT not in out.parents or out.parts[len(ROOT.parts)] not in ('outputs','deliverables'):
        raise ValueError('Choose a new project outputs/ or deliverables/ path')
    stamp=out.parent/(out.name+'_build');stamp.mkdir(parents=True)
    out.mkdir()
    for p in bootstrap.iterdir():
        if p.name in ('BOOTSTRAP_SOURCE.json','BOOTSTRAP_META.json'):continue
        if p.is_dir():shutil.copytree(str(p),str(out/p.name))
        else:shutil.copy2(str(p),str(out/p.name))
    release=out/'release';release.mkdir();paths={}
    for key,path in artifacts.items():
        name={'price_model':('ds_release.json' if manifest['price_engine']=='ds_three_layer' else 'price_native_bundle.pkl'),'effectiveness_model':'effectiveness_bundle.json',
              'business_package':'business_release.iprelease.json'}[key]
        shutil.copy2(str(path),str(release/name));paths[key]='release/'+name
    if effectiveness_package:
        from services.effectiveness_service.expert_packages import read_export
        raw=read_export(Path(effectiveness_package).read_bytes())
        if raw is None:raise ValueError('请选择原效能程序的一键冻结导出 ZIP')
        (out/paths['effectiveness_model']).write_bytes(raw)
        from product.expert_process import clean_env
        import subprocess
        checked=stamp/'expert_preflight.json'
        done=subprocess.run([str(out/'expert/effect_worker.exe'),'--asset-check','effectiveness',str(out/paths['effectiveness_model']),'-',str(checked)],env=clean_env(),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),timeout=90)
        if done.returncode or read_json(checked).get('status')!='pass':raise ValueError('专家效能模型不能在当前软件加载：'+str(read_json(checked)))
    if manifest['price_engine']=='ds_three_layer':
        shutil.copy2(str(artifacts['price_model'].parent/'evaluation_metadata.json'),str(release/'evaluation_metadata.json'))
    else:
        for name,src in [('training_report.json',artifacts['price_model'].parent/'training_report.json'),
                         ('price_native_bundle.pkl.manifest.json',Path(str(artifacts['price_model'])+'.manifest.json'))]:
            shutil.copy2(str(src),str(release/name))
        classification='synthetic' if manifest.get('virtual_data') else 'unconfirmed'
        bind(release/'price_native_bundle.pkl',release/'training_report.json',release/'evaluation_metadata.json',classification,business=artifacts['business_package'])
    classification='synthetic' if manifest.get('virtual_data') else 'unconfirmed'
    # No optimizer: inspect the already-trained model using the pinned builder.
    from services.price_service.ds_backend import load_price_service as PriceService
    service=PriceService(out/paths['price_model'],None,evaluation_metadata=release/'evaluation_metadata.json')
    observed={'health':service.health(),'schema':service.schema()}
    write_json(stamp/'model_preflight.json',observed)
    effect_raw=read_json(out/paths['effectiveness_model'])
    contract=make_contract(observed['schema'],read_json(artifacts['business_package']),sha(artifacts['price_model']),
                           {'unit':'wan_yuan','scale':1.0},
                           effectiveness_schema=effectiveness_schema_from_document(effect_raw),
                           effectiveness_model_sha=sha(out/paths['effectiveness_model']))
    for legacy_dir in legacy_packages:
        old=verify_package(legacy_dir,True)
        if old['files'][old['release']['price_model']]!=contract['model_sha256'] or old['files'][old['release']['business_package']]!=sha(artifacts['business_package']):
            raise ValueError('Legacy predecessor must use the same model and business package')
        contract['compatible_legacy'].append({'version':old['version'],'business_package_sha256':old['files'][old['release']['business_package']]})
    write_json(release/'delivery_contract.json',contract)
    paths.update(price_backend=manifest['price_engine'],evaluation_metadata='release/evaluation_metadata.json',product_code=manifest['product_code'],
                 product_name=manifest['product_name'],classification=classification,
                 effectiveness='original_frozen_expert' if effectiveness_package else 'trial_snapshot',virtual_data=manifest.get('virtual_data'),
                 delivery_contract='release/delivery_contract.json')
    defaults=out/'defaults';defaults.mkdir()
    shutil.copy2(str(ROOT/'app/defaults/scenario_config.json'),str(defaults/'scenario_config.json'))
    write_json(defaults/'model_services.json',{'execution_mode':'services','local_fallback':False,'price_output':{'unit':'wan_yuan','scale':1.0}})
    write_json(defaults/'deep_price_adjustment.json',{'confirmed':False,'message':'未配置正式价格校正规则'})
    desktop=validate_config(read_json(desktop_config)) if desktop_config else template_config()
    write_json(defaults/'desktop.json',desktop)
    (out/'使用说明.txt').write_text('完整解压后双击 product.exe。运行包无需安装 Python，也不含 Python 业务源码。\nPortal: http://127.0.0.1:%d/portal\nManagement: http://127.0.0.1:%d/\n首次端口配置：defaults/desktop.json；以后使用管理页显示的用户目录 config/desktop.json。\n退出软件请使用管理页。复制新版程序不会覆盖用户数据库。\nWindows 7 SP1 x64 尚待实机验证。\n'%(desktop['ports']['web'],desktop['ports']['control']),encoding='utf-8-sig')
    audit_runtime(out)
    source_sha=source_commit(ROOT)
    files={p.relative_to(out).as_posix():sha(p) for p in sorted(out.rglob('*')) if p.is_file()}
    write_json(out/'PACKAGE_MANIFEST.json',{'format':'pipeline-per-desktop-1.0','version':version,'data_schema':1,
               'entrypoint':'product.exe','packaging':meta['kind'],'build_source_sha':source_sha,
               'launcher_source_sha256':sources,'files':files,'release':paths,'training_environment':environment,
               'runtime_python':'3.8.10','architecture':'x64','python_source_included':False,'ui_resources':'embedded','configurable_files':['defaults/desktop.json'],
               'default_data_dir':'%LOCALAPPDATA%/PipelinePer/products/<product-code-sha256-prefix32>',
               'external_python_required':False,'training_on_start':False,'win7_validated':False,'code_signed':False})
    archive=Path(str(out)+'.zip')
    with zipfile.ZipFile(str(archive),'x',zipfile.ZIP_DEFLATED,compresslevel=6) as bundle:
        for p in sorted(out.rglob('*')):
            if p.is_file():bundle.write(str(p),out.name+'/'+p.relative_to(out).as_posix())
    result={'status':'built','package':str(out),'archive':str(archive),'archive_sha256':sha(archive),
            'archive_bytes':archive.stat().st_size,'files':len(files),'model_sha256':sha(artifacts['price_model']),
            'source_sha':source_sha,'python_source_included':False,'win7_validated':False}
    Path(str(archive)+'.sha256').write_text(result['archive_sha256']+'  '+archive.name+'\n',encoding='ascii')
    write_json(stamp/'build_report.json',result);return result

if __name__=='__main__':
    p=argparse.ArgumentParser()
    for flag in ('release-dir','python-runtime','build-tools','out','version','bootstrap'):p.add_argument('--'+flag,required=True)
    p.add_argument('--legacy-package',action='append',default=[]);p.add_argument('--desktop-config')
    p.add_argument('--effectiveness-package');a=p.parse_args()
    print(json.dumps(build(a.release_dir,a.python_runtime,a.build_tools,a.out,a.version,a.bootstrap,a.legacy_package,a.desktop_config,a.effectiveness_package),indent=2))

