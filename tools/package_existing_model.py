# -*- coding: utf-8 -*-
"""Assemble a full release around one already trained price model, then optionally freeze a startable package.

Does not retrain. The price file, its training report and its manifest stay the files that training wrote.
"""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ENGINES = ('optuna_v2', 'optuna_v3', 'optuna_v4', 'optuna_v5', 'optuna_v6', 'optuna_v7')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def align_sidecar(manifest_path, bundle_path):
    """Point the sidecar hash at the price file that is actually being packaged."""
    manifest = _read(manifest_path)
    digest = _sha(bundle_path)
    previous = manifest.get('bundle_sha256', manifest.get('sha256'))
    manifest['bundle_sha256'] = digest
    manifest['bundle_file'] = Path(bundle_path).name
    _write(manifest_path, manifest)
    return previous, digest


def assemble(model_dir, table, output, backend=None, product_code=None, product_name=None, virtual_data=True):
    model_dir = Path(model_dir).resolve()
    output = Path(output).resolve()
    bundle = model_dir / 'price_native_bundle.pkl'
    report_path = model_dir / 'training_report.json'
    sidecar = model_dir / 'price_native_bundle.pkl.manifest.json'
    if not bundle.is_file() or not report_path.is_file() or not sidecar.is_file():
        raise ValueError('指定目录需要价格模型、训练报告和模型清单')
    report = _read(report_path)
    engine = backend or report.get('engine')
    if engine not in ENGINES:
        raise ValueError('训练报告里的算法版本无法封装：' + str(engine))
    if output.exists() and any(output.iterdir()):
        raise ValueError('封装输出目录必须是空目录')
    output.mkdir(parents=True, exist_ok=True)
    recorded = _read(sidecar)
    code = product_code or recorded.get('product_code') or 'R13_TRAIN'
    name = product_name or recorded.get('product_name') or '独立训练'
    from tools.train_price_only import load_input_table
    data = load_input_table(table, engine, code, name)
    price = output / 'price'
    price.mkdir()
    shutil.copy2(str(bundle), str(price / 'price_native_bundle.pkl'))
    shutil.copy2(str(sidecar), str(price / 'price_native_bundle.pkl.manifest.json'))
    shutil.copy2(str(report_path), str(price / 'training_report.json'))
    previous, digest = align_sidecar(price / 'price_native_bundle.pkl.manifest.json', price / 'price_native_bundle.pkl')
    source = Path(table).resolve()
    raw = source.read_bytes()
    saved = output / 'input' / source.name
    saved.parent.mkdir(parents=True, exist_ok=True)
    if saved.resolve() != source.resolve():
        shutil.copy2(str(source), str(saved))
    from automl_pipeline.product_effectiveness_bootstrap import build_effectiveness, load_config
    build_effectiveness(data, output / 'effectiveness', load_config(saved, allow_legacy=False))
    from automl_pipeline.product_pipeline import stage_business
    draft, validation = stage_business(data, output)
    if not validation['valid']:
        raise ValueError('业务草稿没有通过校验')
    manifest = {
        'format': 'product-prototype-pipeline-1.0', 'status': 'ready', 'prototype': True,
        'virtual_data': bool(virtual_data), 'product_code': data['products'][0]['product_code'],
        'product_name': data['products'][0]['product_name'], 'source_filename': source.name,
        'source_sha256': hashlib.sha256(raw).hexdigest(), 'header_rows': 2,
        'price_model': 'price/price_native_bundle.pkl',
        'effectiveness_model': 'effectiveness/effectiveness_bundle.json',
        'business_package': 'business_release.iprelease.json',
        'release_id': draft['release_id'], 'release_status': 'validated_draft',
        'price_engine': engine, 'targets_met': report.get('targets_met'),
        'packaged_from': str(model_dir), 'retrained': False,
        'previous_bundle_sha256': previous, 'shipped_bundle_sha256': digest,
    }
    if engine in ('optuna_v4', 'optuna_v5', 'optuna_v6', 'optuna_v7'):
        if report.get('quality_status') is not None:
            manifest['quality_status'] = report['quality_status']
        if report.get('search_mode') is not None:
            manifest['search_mode'] = report['search_mode']
    manifest['artifact_sha256'] = {
        key: _sha(output / manifest[key]) for key in ('price_model', 'effectiveness_model', 'business_package')}
    _write(output / 'prototype_manifest.json', manifest)
    return manifest


def package_exe(release_dir, exe_out, bootstrap=None, version='r13-closed-loop', archive=False):
    from tools.engine_pointer import resolve_engine
    from tools.build_product_exe import build
    engine = resolve_engine(bootstrap)
    if engine is None:
        raise ValueError('没有可用的冻结引擎。请先重新冻结，再生成完整运行包。')
    runtime = ROOT.parent / 'runtime' / 'python38'
    if not (runtime / 'python.exe').is_file():
        runtime = Path(sys.executable).resolve().parent
    return build(release_dir, runtime, engine, exe_out, version, engine, archive=archive)


def main(argv=None):
    parser = argparse.ArgumentParser(description='把已训练的价格模型组装成完整发布目录，并可生成可启动运行包')
    parser.add_argument('--release', required=True, help='含 price_native_bundle.pkl 的训练目录')
    parser.add_argument('--output', required=True, help='完整发布目录')
    parser.add_argument('--input', help='双行表头 Excel 或 CSV。已有完整发布清单时可省略')
    parser.add_argument('--backend', choices=ENGINES)
    parser.add_argument('--product-code')
    parser.add_argument('--product-name')
    parser.add_argument('--customer', action='store_true', help='标记为非虚拟数据。不会改写指标')
    parser.add_argument('--bootstrap', help='新冻结的引擎目录。省略时使用最近一次冻结结果或开发包引擎')
    parser.add_argument('--exe-out', help='写出带 PACKAGE_MANIFEST.json 的可启动目录')
    parser.add_argument('--version', default='r13-closed-loop')
    parser.add_argument('--no-archive', action='store_true')
    args = parser.parse_args(argv)
    release = Path(args.release)
    output = Path(args.output)
    ready = (release / 'prototype_manifest.json').is_file() and not args.input
    if ready:
        manifest = _read(release / 'prototype_manifest.json')
        if manifest.get('status') != 'ready':
            raise SystemExit('已有发布清单但状态不是已完成')
        packaged = release
    else:
        if not args.input:
            raise SystemExit('请用 --input 指定训练时使用的成品表')
        manifest = assemble(release, args.input, output, args.backend, args.product_code, args.product_name, virtual_data=not args.customer)
        packaged = output
    record = {'release': str(packaged), 'price_engine': manifest.get('price_engine'), 'status': manifest.get('status'),
              'retrained': False}
    if args.exe_out:
        built = package_exe(packaged, args.exe_out, args.bootstrap, args.version, archive=not args.no_archive)
        record['package'] = built['package']
        record['manifest'] = str(Path(built['package']) / 'PACKAGE_MANIFEST.json')
        record['files'] = built['files']
    else:
        record['model'] = str(packaged / manifest['price_model'])
        record['business_package'] = str(packaged / manifest['business_package'])
        record['effectiveness_model'] = str(packaged / manifest['effectiveness_model'])
    _write(packaged / 'package_record.json', record)
    print(json.dumps(record, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
