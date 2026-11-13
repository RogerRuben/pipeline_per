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


def verify_sidecar(manifest_path, bundle_path):
    """Refuse to package when the training manifest does not name this model file."""
    manifest = _read(manifest_path)
    recorded = manifest.get('bundle_sha256', manifest.get('sha256'))
    digest = _sha(bundle_path)
    if not recorded or recorded != digest:
        raise ValueError('模型清单记录的摘要与模型文件不一致。清单 %s，文件 %s。封装不会改写清单。' % (recorded, digest))
    return digest


def preprocessing_view(cfg):
    """Only the settings that change how an input value is read."""
    expert = cfg.get('expert') or {}
    incoming = cfg.get('input') or {}
    return {'missing_tokens': list(incoming.get('missing_tokens') or []),
            'field_types': dict(expert.get('field_types') or {})}


def _same_type(model_dtype, data_type):
    from services.price_service.optuna_v2.typed_features import canonical_type
    try:
        return canonical_type(model_dtype) == canonical_type(data_type)
    except ValueError:
        return str(model_dtype) == str(data_type)


def model_feature_order(bundle):
    schema = bundle.get('feature_schema') or []
    source = list(bundle.get('source_feature_order') or [])
    if source:
        return [str(item) for item in source]
    order = []
    for item in schema:
        name = str(item.get('source_column') or item.get('field_name') or item.get('key'))
        if name not in order:
            order.append(name)
    if order:
        return order
    return [str(item) for item in (bundle.get('feature_order') or [])]


def check_input_contract(field_ids, data, bundle):
    """Stop when the table would give the saved model different fields, types, or codes."""
    order = model_feature_order(bundle)
    if not order:
        raise ValueError('价格模型没有字段顺序，不能封装')
    schema = {}
    for item in bundle.get('feature_schema') or []:
        schema[str(item.get('field_name') or item.get('key'))] = item
        if item.get('source_column'):
            schema.setdefault(str(item['source_column']), item)
    missing = [key for key in order if key not in field_ids]
    if missing:
        raise ValueError('成品表缺少模型字段：' + ','.join(missing))
    present = [key for key in field_ids if key in order]
    if present != order:
        raise ValueError('成品表里模型字段的顺序与训练时不同。训练顺序：%s；当前顺序：%s' % (','.join(order), ','.join(present)))
    by_id = {item['parameter_id']: item for item in data['parameters']}
    for key in order:
        spec = schema.get(key) or {}
        if not _same_type(spec.get('dtype') or 'number', by_id[key].get('value_type') or 'number'):
            raise ValueError('字段%s的类型与模型不一致：模型%s，当前表%s' % (key, spec.get('dtype'), by_id[key].get('value_type')))
        mapping = spec.get('category_mapping') or {}
        if not mapping:
            continue
        allowed = {str(name) for name in mapping}
        seen = set()
        for row in data['agreements']:
            if key in (row.get('params') or {}):
                seen.add(str(row['params'][key]))
        unknown = sorted(seen - allowed)
        if unknown:
            raise ValueError('字段%s出现了模型编码里没有的取值：%s' % (key, ','.join(unknown)))
    return order


def assemble(model_dir, table, output, backend=None, product_code=None, product_name=None, virtual_data=True, config_path=None):
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
    digest = verify_sidecar(sidecar, bundle)
    recorded = _read(sidecar)
    code = product_code or recorded.get('product_code') or 'R13_TRAIN'
    name = product_name or recorded.get('product_name') or '独立训练'
    saved_config = model_dir / 'requested_config.json'
    from automl_pipeline.__main__ import config_loader
    loader = config_loader(engine)
    if saved_config.is_file() and config_path:
        if preprocessing_view(loader(saved_config)) != preprocessing_view(loader(config_path)):
            raise ValueError('这份配置的缺失值规则或属性类型与训练时不一致，已停止封装')
    parse_config = saved_config if saved_config.is_file() else config_path
    from tools.train_price_only import load_input_table
    from app.product_table import product_table_rows
    from services.price_service.native_bundle import load_bundle
    data = load_input_table(table, engine, code, name, str(parse_config) if parse_config else None)
    fields = check_input_contract(
        product_table_rows(Path(table).name, Path(table).read_bytes(), require_dual_header=True)['field_ids'],
        data, load_bundle(bundle))
    output.mkdir(parents=True, exist_ok=True)
    price = output / 'price'
    price.mkdir()
    shutil.copy2(str(bundle), str(price / 'price_native_bundle.pkl'))
    shutil.copy2(str(sidecar), str(price / 'price_native_bundle.pkl.manifest.json'))
    shutil.copy2(str(report_path), str(price / 'training_report.json'))
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
        'model_sha256': digest, 'sidecar_matches_model': True, 'feature_order': fields,
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
    parser.add_argument('--config', help='须与训练时的缺失值规则和属性类型一致')
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
        manifest = assemble(release, args.input, output, args.backend, args.product_code, args.product_name,
                            virtual_data=not args.customer, config_path=args.config)
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
