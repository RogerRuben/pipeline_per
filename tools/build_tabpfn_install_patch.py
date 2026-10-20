# -*- coding: utf-8 -*-
"""Build small overlays against the shipped 2026-10-13 TabPFN packages."""
import argparse
import copy
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def normalized(manifest):
    return {entry['path'].replace('\\', '/'): entry for entry in manifest['files']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, default=ROOT/'deliverables/tabpfn_micro_fixed_20261013/tabpfn_micro_fixed_20261013.zip')
    parser.add_argument('--latest', type=Path, default=ROOT/'deliverables/tabpfn_install_verified_20261014')
    parser.add_argument('--output', type=Path, default=ROOT/'deliverables/tabpfn_install_patch_20261014')
    args = parser.parse_args()
    output = args.output.resolve()
    if ROOT/'deliverables' not in output.parents or output.exists():
        raise ValueError('Choose a new directory within this project deliverables directory.')
    output.mkdir(parents=True)
    results = []
    with zipfile.ZipFile(args.base) as baseline:
        for package, role, wheels in (('TabPFN_WIN11_CPU_OFFLINE', 'win11', 40), ('TabPFN_WIN7_LAN_CLIENT_OFFLINE', 'win7_client', 12)):
            before_bytes = baseline.read(package+'/delivery_manifest.json')
            before = json.loads(before_bytes)
            old = normalized(before)
            with zipfile.ZipFile(args.latest/(package+'.zip')) as latest:
                after = json.loads(latest.read('delivery_manifest.json'))
                new = normalized(after)
                assets = {key for key in old.keys() | new.keys() if key.startswith('assets/')}
                for key in assets:
                    if key not in old or key not in new or old[key]['sha256'] != new[key]['sha256']:
                        raise ValueError('Dependency/model assets changed: '+key)
                    if digest(baseline.read(package+'/'+key)) != new[key]['sha256'] or digest(latest.read(key)) != new[key]['sha256']:
                        raise ValueError('Archive asset checksum mismatch: '+key)
                assert sum(key.endswith('.whl') for key in assets) == wheels
                for key in old:
                    if key.startswith('prototype/') and key.endswith(('.pkl', 'effectiveness_bundle.json', 'business_release.iprelease.json')):
                        assert key in new and old[key]['sha256'] == new[key]['sha256'], key
                changed = [key for key in sorted(new) if key not in old or old[key]['sha256'] != new[key]['sha256']]
                payload = {}
                omitted = []
                for key in changed:
                    if key.startswith(('prototype/', 'assets/', 'runtime/', 'data/', 'models/')):
                        omitted.append(key)
                        continue
                    if key == 'README_FIRST.md' or key.endswith('.bat') or key.split('/')[0] in ('app', 'tools', 'services', 'tests', 'docs'):
                        assert not key.endswith(('.whl', '.pkl', '.db', '.xlsx', '.csv', '.ckpt', '.exe', '.dll', '.png'))
                        payload[key] = latest.read(key)
                    else:
                        raise ValueError('Unclassified update: '+key)
                for key in ('tools/install_tabpfn_offline.py', 'tools/check_tabpfn_environment.py'):
                    payload[key] = (ROOT/key).read_bytes()
                title = 'TabPFN_WIN11_InstallFix_PATCH_ONLY' if role == 'win11' else 'TabPFN_WIN7_ClientFix_PATCH_ONLY'
                startup = 'START.bat' if role == 'win11' else 'START.bat http://Win11机局域网IP:18901'
                readme = ('适用：2026-10-13版 '+package+' 已解压目录。\n\n'
                    '关闭该目录启动的试用程序，将本补丁完整解压覆盖到旧包根目录（能看到assets和tools的那层），同名文件选择替换。不要单独运行补丁目录。\n'
                    '然后运行 INSTALL.bat，安装成功后运行 '+startup+'。安装复用旧包本地wheel，无需下载任何依赖。\n\n'
                    '本补丁只更新代码、批处理、检查脚本和说明。40个Win11 wheel、12个Win7 wheel均无变化；原Python压缩包、模型权重、价格bundle、样本、数据库、专家配置和运行环境文件不在补丁中。\n'
                    '重新运行安装会建立经过真实导入与预测检查的新包内依赖环境，通过后切换；原环境保留。依赖目录使用短名称，避免较深目录触发Windows路径上限。\n\n'
                    '主要修复：tqdm明确列入依赖并验证真实导入；现代训练不再误用sklearn0.24检查；安装隔离、离线完整校验及缺失依赖自动修复；直接启动已训练三环节示例。\n'
                    'PATCH_MANIFEST.json列出本次替换文件。delivery_manifest.json以旧包为基础更新代码摘要，保留旧包模型与数据摘要。\n')
                # Legacy combined ZIPs used the local Chinese code page for one
                # Markdown filename without setting the ZIP UTF-8 flag. Supply
                # that same static report under its intended Unicode filename.
                filename_repairs = []
                baseline_names = set(baseline.namelist())
                for info in baseline.infolist():
                    if info.flag_bits & 2048 or not any(ord(char) > 127 for char in info.filename):
                        continue
                    try:
                        decoded = info.filename.encode('cp437').decode('gbk')
                    except (UnicodeEncodeError, UnicodeDecodeError):
                        continue
                    prefix = package+'/'
                    if not decoded.startswith(prefix):
                        continue
                    relative = decoded[len(prefix):]
                    if decoded not in baseline_names and relative in old and relative.startswith('prototype/') and relative.endswith('.md'):
                        content = baseline.read(info)
                        assert digest(content) == old[relative]['sha256']
                        payload[relative] = content
                        filename_repairs.append(relative)
                payload['PATCH_README.md'] = readme.encode('utf-8')
                patch = {'format':'tabpfn-install-overlay-1.0', 'patch_id':'tabpfn-install-fix-20261014', 'role':role,
                    'base_package':package, 'base_manifest_sha256':digest(before_bytes), 'wheel_count_reused':wheels,
                    'new_wheels':[], 'changed_wheels':[], 'legacy_filename_repairs':filename_repairs, 'preserves_assets_models_database_and_configuration':True,
                    'files':[{'path':key,'sha256':digest(data),'size':len(data)} for key,data in sorted(payload.items())]}
                payload['PATCH_MANIFEST.json'] = json.dumps(patch,ensure_ascii=False,indent=2).encode('utf-8')
                merged = copy.deepcopy(before)
                records = {key:dict(entry,path=key) for key,entry in old.items()}
                for key,data in payload.items():
                    records[key] = {'path':key,'size':len(data),'sha256':digest(data)}
                merged['files'] = [records[key] for key in sorted(records)]
                merged['applied_patch'] = {'id':patch['patch_id'],'base_manifest_sha256':patch['base_manifest_sha256']}
                payload['delivery_manifest.json'] = json.dumps(merged,ensure_ascii=False,indent=2).encode('utf-8')
                archive = output/(title+'_20261014.zip')
                with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as target:
                    for key,data in sorted(payload.items()):
                        target.writestr(key,data)
                Path(str(archive)+'.sha256').write_text(digest(archive.read_bytes())+'  '+archive.name+'\n',encoding='ascii')
                results.append({'role':role,'path':str(archive),'size_bytes':archive.stat().st_size,'sha256':digest(archive.read_bytes()),
                    'payload_files':len(payload),'changed_code_files':len(payload)-3,'wheel_count_reused':wheels,'new_wheels':[],
                    'omitted_runtime_data_and_verification_artifacts':omitted})
    (output/'patch_report.json').write_text(json.dumps({'status':'BUILT_PENDING_OVERLAY_TEST','patches':results},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(results,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()