# -*- coding: utf-8 -*-
"""Cumulative small source-only fix for the shipped TabPFN input pipeline."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
FILES=('app/product_table.py','app/static/price_ensemble.js','tools/product_pipeline.py',
       'tools/product_price_automl.py','tools/product_price_tabpfn.py','tools/product_r4_input.py',
       'tools/price_attribute_input.py','tools/build_tabpfn_offline_delivery.py','docs/PRICE_INPUT_PASTE_FIX_20261015.md')

def sha(data): return hashlib.sha256(data).hexdigest()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous',type=Path,default=ROOT/'deliverables/tabPFN+autoML')
    parser.add_argument('--output',type=Path,default=ROOT/'deliverables/tabpfn_price_input_fix_20261015')
    args=parser.parse_args();out=args.output.resolve()
    if ROOT/'deliverables' not in out.parents or (out.exists() and any(out.iterdir())): raise ValueError('Choose a new or empty output under deliverables')
    out.mkdir(parents=True,exist_ok=True)
    results=[]
    for role,oldname,newname in [('win11','TabPFN_WIN11_InstallFix_PATCH_ONLY_20261014.zip','TabPFN_WIN11_PriceInputFix_PATCH_20261015.zip'),
                                ('win7_client','TabPFN_WIN7_ClientFix_PATCH_ONLY_20261014.zip','TabPFN_WIN7_PriceInputFix_PATCH_20261015.zip')]:
        with zipfile.ZipFile(args.previous/oldname) as old:
            oldplan=json.loads(old.read('PATCH_MANIFEST.json'))
            manifest=json.loads(old.read('delivery_manifest.json'))
            payload={name:old.read(name) for name in old.namelist() if name not in ('PATCH_MANIFEST.json','PATCH_README.md','delivery_manifest.json')}
        for name in FILES: payload[name]=(ROOT/name).read_bytes()
        readme=('本补丁适用于10月13日原包或已覆盖10月14日补丁的目录，包含此前安装修复。\n\n'
                '关闭旧程序，将本补丁完整解压覆盖到旧包根目录（能看到assets、tools的那层），同名文件选择替换，再重新运行原训练脚本。不要在单独的补丁目录中运行。\n'
                'whl、Python压缩包、模型、数据库、样本、自定义配置均复用原文件。环境已正常时无需重装；仍有环境问题时INSTALL.bat会离线复用旧wheel修复。\n\n'
                '本次修复合法代号被空列/文本列或示例旧评分误拦截；清理复制带入的不可见字符；支持评分列调换和实际单元格定位。详见docs/PRICE_INPUT_PASTE_FIX_20261015.md。\n'
                '真实JSON评分和工作簿评分会保留，未知实际评分仍给出具体字段和位置；不修改任何现有评分工作簿或JSON文件。\n')
        payload['PATCH_README.md']=readme.encode('utf-8')
        plan={'format':'tabpfn-input-overlay-1.0','patch_id':'tabpfn-price-input-fix-20261015','role':role,
              'base_package':oldplan['base_package'],'base_manifest_sha256':oldplan['base_manifest_sha256'],
              'supersedes':['tabpfn-install-fix-20261014'],'new_or_changed_wheels':[],
              'preserves_models_database_and_user_config':True,
              'files':[{'path':name,'size':len(data),'sha256':sha(data)} for name,data in sorted(payload.items())]}
        payload['PATCH_MANIFEST.json']=json.dumps(plan,ensure_ascii=False,indent=2).encode('utf-8')
        entries={row['path'].replace('\\','/'):dict(row,path=row['path'].replace('\\','/')) for row in manifest['files']}
        for name,data in payload.items(): entries[name]={'path':name,'size':len(data),'sha256':sha(data)}
        manifest['files']=[entries[name] for name in sorted(entries)]
        manifest['applied_patch']={'id':plan['patch_id'],'base_manifest_sha256':plan['base_manifest_sha256']}
        payload['delivery_manifest.json']=json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8')
        archive=out/newname
        assert not any(name.endswith(('.whl','.pkl','.db','.dll','.exe','.ckpt','.xlsx','.csv')) for name in payload)
        with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as target:
            for name,data in sorted(payload.items()): target.writestr(name,data)
        digest=sha(archive.read_bytes())
        Path(str(archive)+'.sha256').write_text(digest+'  '+archive.name+'\n',encoding='ascii')
        results.append({'role':role,'path':str(archive),'size_bytes':archive.stat().st_size,'sha256':digest,'files':len(payload)})
    (out/'patch_report.json').write_text(json.dumps({'status':'BUILT_PENDING_VERIFICATION','patches':results},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(results,ensure_ascii=False,indent=2))

if __name__=='__main__': main()