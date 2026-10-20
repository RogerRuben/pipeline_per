# -*- coding: utf-8 -*-
import argparse,hashlib,json,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
FILES=('tools/product_price_tabpfn.py','tools/product_price_automl_v2.py','tools/price_candidate_recovery.py',
       'tools/build_tabpfn_offline_delivery.py','services/tabpfn_price_service/app.py',
       'tests/candidate_recovery_test.py','docs/PRICE_CANDIDATE_RECOVERY_20261015.md')
def sha(data):return hashlib.sha256(data).hexdigest()
def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=ROOT/'deliverables/tabpfn_candidate_recovery_20261015')
    args=p.parse_args();output=args.output.resolve()
    if ROOT/'deliverables' not in output.parents or (output.exists() and any(output.iterdir())):raise ValueError('Choose a new output under deliverables')
    output.mkdir(parents=True,exist_ok=True);results=[]
    for role,prefix,oldname in [('win11','TabPFN_WIN11','TabPFN_WIN11_PriceInputFix_PATCH_20261015.zip'),('win7_client','TabPFN_WIN7','TabPFN_WIN7_PriceInputFix_PATCH_20261015.zip')]:
        with zipfile.ZipFile(ROOT/'deliverables/tabPFN+autoML'/oldname) as previous:
            oldplan=json.loads(previous.read('PATCH_MANIFEST.json'));manifest=json.loads(previous.read('delivery_manifest.json'))
            payload={name:previous.read(name) for name in previous.namelist() if name not in ('PATCH_MANIFEST.json','PATCH_README.md','delivery_manifest.json')}
        for name in FILES:payload[name]=(ROOT/name).read_bytes()
        text='关闭旧程序，把本补丁覆盖到旧包根目录（assets和tools所在层），再重新运行训练脚本。无需另下依赖；已有数据、模型和自定义配置不在补丁中。\n\n此补丁累积包含10月14日安装/路径修复与10月15日英文代号修复，并增加候选异常隔离、扩种保留验证前缀、重拟合重新融合、明确TabPFN对比模型不可用状态。详见docs/PRICE_CANDIDATE_RECOVERY_20261015.md。\n'
        payload['PATCH_README.md']=text.encode('utf-8')
        plan={'format':'tabpfn-candidate-recovery-overlay-1.0','patch_id':'tabpfn-candidate-recovery-20261015','role':role,
              'base_package':oldplan['base_package'],'base_manifest_sha256':oldplan['base_manifest_sha256'],
              'supersedes':['tabpfn-install-fix-20261014','tabpfn-price-input-fix-20261015'],'new_or_changed_wheels':[],
              'preserves_models_database_and_user_config':True,
              'files':[{'path':name,'size':len(data),'sha256':sha(data)} for name,data in sorted(payload.items())]}
        payload['PATCH_MANIFEST.json']=json.dumps(plan,ensure_ascii=False,indent=2).encode('utf-8')
        records={item['path'].replace('\\','/'):dict(item,path=item['path'].replace('\\','/')) for item in manifest['files']}
        for name,data in payload.items():records[name]={'path':name,'size':len(data),'sha256':sha(data)}
        manifest['files']=[records[name] for name in sorted(records)];manifest['applied_patch']={'id':plan['patch_id'],'base_manifest_sha256':plan['base_manifest_sha256']}
        payload['delivery_manifest.json']=json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8')
        assert not any(name.endswith(('.whl','.pkl','.db','.exe','.dll','.ckpt','.xlsx','.csv')) for name in payload)
        archive=output/(prefix+'_CandidateRecovery_PATCH_20261015.zip')
        with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
            for name,data in sorted(payload.items()):z.writestr(name,data)
        digest=sha(archive.read_bytes());Path(str(archive)+'.sha256').write_text(digest+'  '+archive.name+'\n',encoding='ascii')
        results.append({'role':role,'path':str(archive),'sha256':digest,'size_bytes':archive.stat().st_size,'files':len(payload)})
    (output/'patch_report.json').write_text(json.dumps({'status':'BUILT_PENDING_VERIFICATION','patches':results},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(results,ensure_ascii=False,indent=2))
if __name__=='__main__':main()