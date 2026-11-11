import json,os,subprocess,sys,hashlib,shutil
from pathlib import Path
r=Path(__file__).resolve().parents[1];out=r/'outputs/r12_1_kit_acceptance';out.mkdir(exist_ok=False);target=r/'outputs/k12b';archive=r/'deliverables/CB12B.7z';seven='C:/Program Files/7-Zip/7z.exe';report={'checks':{},'win7_tested':False}
def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def write(p,d):Path(p).write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def check(k,v):
 report['checks'][k]=bool(v);write(out/'report.json',report)
 if not v:raise AssertionError(k)
for args,label in [(['t',str(archive)],'archive'),(['x',str(archive),'-o'+str(target),'-y'],'extract')]:
 p=subprocess.run([seven]+args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=600);(out/(label+'.log')).write_bytes(p.stdout);check(label,p.returncode==0)
kit=target/'CB12B';m=read(kit/'KIT_MANIFEST.json');check('under_100_MB',archive.stat().st_size<100000000);check('all_files_match',all(sha(kit/n)==h for n,h in m['files'].items()))
env=dict(os.environ,PATH='',PRODUCT_NO_PAUSE='1')
for key in list(env):
 if key.startswith(('PYTHON','IPDEMO_','_PYI','_MEIPASS')):env.pop(key,None)
cmd=str(Path(os.environ['SystemRoot'])/'System32/cmd.exe')
def run(batch,label):
 with (out/(label+'.log')).open('wb') as log:p=subprocess.run([cmd,'/d','/c','call',str(kit/batch)],cwd=str(kit),env=env,stdout=log,stderr=subprocess.STDOUT,timeout=900)
 check(label,p.returncode==0)
run('CHECK_ENVIRONMENT.bat','offline_price_environment')
release=kit/'project/outputs/verified_price';shutil.copytree(r/'outputs/r12_1_enum/price_release',release);manifest=read(release/'prototype_manifest.json');original={p.relative_to(release).as_posix():sha(p) for p in release.rglob('*') if p.is_file()}
write(kit/'LAST_TRAINED.json',{'release':release.relative_to(kit).as_posix(),'price_backend':'optuna_v3','source_sha256':manifest['source_sha256'],'model_sha256':manifest['artifact_sha256']['price_model']})
source=r/'outputs/r12_1_enum/pump/declared_domain_test.zip';shutil.copy2(source,kit/'input/pump_export.zip');cfg=read(kit/'settings/project.json');cfg['effectiveness_package']='input/pump_export.zip';write(kit/'settings/project.json',cfg)
run('REPACK_LAST.bat','repack_with_original_expert_zip')
success=read(kit/'LAST_SUCCESS.json');job=Path(success['job']);package=Path(read(job/'package_report.json')['package']);smoke=read(job/'delivery_smoke.json');check('actual_exe_smoke',smoke['status']=='pass')
check('original_expert_preloaded',read(package/'PACKAGE_MANIFEST.json')['release']['effectiveness']=='original_frozen_expert')
check('expert_binary_included',(package/'expert/effect_worker.exe').is_file());check('price_release_not_retrained',original=={p.relative_to(release).as_posix():sha(p) for p in release.rglob('*') if p.is_file()})
check('immutable_kit_not_modified',all(sha(kit/n)==h for n,h in m['files'].items()))
report.update(status='pass',package=str(package),source_sha=m['source_sha'],immutable_files=len(m['files']),archive_sha256=sha(archive),archive_bytes=archive.stat().st_size);write(out/'report.json',report)
print(report)
