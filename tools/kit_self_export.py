# -*- coding: utf-8 -*-
"""Pack the current kit into a new offline zip. No git, network, or external tools."""
import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(target))


def _copy_filtered(source_root, target_root, predicate):
    source_root = Path(source_root)
    for path in source_root.rglob('*'):
        if not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError('不复制链接：' + str(path))
        relative = path.relative_to(source_root).as_posix()
        if not predicate(relative):
            continue
        _copy_file(path, Path(target_root) / relative)


def export_kit(kit, dest_zip=None):
    from product.common import read_json, write_json, sha
    from tools.build_frozen_engine import engine_sources
    from tools.engine_pointer import resolve_engine
    from tools.kit_entries import include_in_reexport, write_operator_bats
    from tools.client_kit_paths import batch_entry, MAX_KIT_ROOT_CHARS
    kit = Path(kit).resolve()
    if not (kit / 'KIT_MANIFEST.json').is_file():
        raise SystemExit('这不是完整开发包。')
    for name in ('runtime/python38/python.exe', 'build_tools', 'wheels', 'bootstrap/product.exe'):
        if not (kit / name).exists():
            raise SystemExit('开发包缺少 ' + name + '，不能导出。')
    engine = resolve_engine()
    if engine is None or not (engine / 'product.exe').is_file():
        raise SystemExit('没有可用引擎。请先重新编译，再导出开发包。')
    if read_json(engine / 'BOOTSTRAP_SOURCE.json') != engine_sources():
        raise SystemExit('当前源码和引擎不一致。请先重新编译，成功后再导出。')
    example = kit / 'examples' / 'products_example.xlsx'
    provenance = kit / 'examples' / 'PROVENANCE.json'
    example_ok = False
    if example.is_file() and provenance.is_file():
        recorded = read_json(provenance)
        example_ok = recorded.get('classification') == 'synthetic' and recorded.get('source_sha256') == sha(example)
    if not example_ok:
        raise SystemExit('示例成品表已不是随包的虚拟表，为避免带出客户数据，已停止导出。')
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    zip_path = Path(dest_zip) if dest_zip else kit / 'deliverables' / ('开发包_' + stamp + '.zip')
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        raise SystemExit('导出文件已存在：' + str(zip_path))
    stage_parent = Path(tempfile.mkdtemp(prefix='kit-export-'))
    folder_name = '开发包_' + stamp
    stage = stage_parent / folder_name
    stage.mkdir()
    try:
        _copy_filtered(kit, stage, include_in_reexport)
        # The verified engine becomes the baseline inside the new kit.
        if (stage / 'bootstrap').exists():
            shutil.rmtree(str(stage / 'bootstrap'))
        shutil.copytree(str(engine), str(stage / 'bootstrap'))
        sample = example.read_bytes()
        (stage / 'input').mkdir(exist_ok=True)
        (stage / 'input' / 'products.xlsx').write_bytes(sample)
        settings = json.loads((stage / 'settings' / 'project.json').read_text(encoding='utf-8-sig'))
        settings['input_file'] = 'input/products.xlsx'
        settings['data_kind'] = 'synthetic'
        (stage / 'settings' / 'project.json').write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8-sig')
        write_operator_bats(stage)
        from tools.kit_entries import legacy_bat_names
        for filename, command in legacy_bat_names().items():
            (stage / filename).write_bytes(batch_entry(command))
        project = stage / 'project'
        source_files = {path.relative_to(project).as_posix(): sha(path) for path in sorted(project.rglob('*')) if path.is_file() and path.name != 'SOURCE_BUILD.json'}
        old_sha = 'local'
        old_seal = kit / 'project' / 'SOURCE_BUILD.json'
        if old_seal.is_file():
            old_sha = read_json(old_seal).get('source_sha') or old_sha
        write_json(project / 'SOURCE_BUILD.json', {'format': 'pipeline-source-distribution-1.0', 'source_sha': old_sha, 'files': source_files})
        dependencies = read_json(kit / 'KIT_MANIFEST.json').get('training_dependencies') or {}
        files = {path.relative_to(stage).as_posix(): sha(path) for path in sorted(stage.rglob('*')) if path.is_file()
                 and path.relative_to(stage).parts[0] not in ('input', 'settings', 'logs', 'deliverables')}
        write_json(stage / 'KIT_MANIFEST.json', {
            'format': 'pipeline-client-build-kit-1.0', 'source_sha': old_sha, 'engine': 'optuna_v3',
            'target': 'Windows 7 SP1 x64', 'python': '3.8.10', 'files': files,
            'example_sha256': sha(stage / 'examples' / 'products_example.xlsx'),
            'training_dependencies': dependencies, 'external_python_required': False, 'git_required': False,
            'compiler_on_client_required': False, 'network_required': False, 'win7_machine_validated': False,
            'build_tools': 'build_tools', 'wheels': 'wheels', 'bootstrap': 'bootstrap',
            'path_policy': {'max_root_utf16_chars': MAX_KIT_ROOT_CHARS, 'recommended_root': 'C:\\CB6'},
            'mutable_directories': ['input', 'settings', 'logs', 'deliverables', 'builder_state', 'project/outputs', 'project/deliverables']})
        import zipfile
        with zipfile.ZipFile(str(zip_path), 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            for path in sorted(stage.rglob('*')):
                if path.is_file():
                    bundle.write(str(path), folder_name + '/' + path.relative_to(stage).as_posix())
        digest = sha(zip_path)
        Path(str(zip_path) + '.sha256').write_text(digest + '  ' + zip_path.name + '\n', encoding='utf-8')
        report = {'status': 'exported', 'archive': str(zip_path), 'sha256': digest, 'bytes': zip_path.stat().st_size,
                  'files': len(files), 'engine_exe_sha256': read_json(engine / 'BOOTSTRAP_META.json').get('exe_sha256'),
                  'external_tools_required': False, 'network_required': False}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print('新开发包：' + str(zip_path))
        return report
    finally:
        shutil.rmtree(str(stage_parent), ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='把当前开发包再导出为一份完整压缩包')
    parser.add_argument('--out', help='zip 路径。默认写到本包 deliverables，且不会把自己打进去')
    args = parser.parse_args(argv)
    kit = ROOT.parent
    export_kit(kit, args.out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
