# -*- coding: utf-8 -*-
"""Build a startable customer folder from the latest engine and a chosen model."""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def kit_root():
    from tools.kit_entries import kit_root_from_project
    return kit_root_from_project(ROOT)


def discover_models(kit):
    found = []
    seen = set()
    roots = [kit / 'project' / 'outputs', kit / 'project' / 'deliverables', kit / 'examples']
    for base in roots:
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(str(base)):
            if 'product.exe' in filenames:
                dirnames[:] = []
                continue
            if 'price_native_bundle.pkl' not in filenames:
                continue
            folder = Path(dirpath)
            if (folder.parent / 'PACKAGE_MANIFEST.json').is_file() or (folder.parent.parent / 'PACKAGE_MANIFEST.json').is_file():
                continue
            key = str(folder.resolve())
            if key in seen:
                continue
            seen.add(key)
            report = folder / 'training_report.json'
            engine = ''
            if report.is_file():
                try:
                    engine = json.loads(report.read_text(encoding='utf-8-sig')).get('engine') or ''
                except ValueError:
                    engine = ''
            try:
                label = folder.resolve().relative_to(kit).as_posix()
            except ValueError:
                label = str(folder)
            if engine:
                label += '  (' + engine + ')'
            found.append({'dir': folder, 'label': label, 'mtime': folder.stat().st_mtime, 'engine': engine})
    found.sort(key=lambda item: item['mtime'], reverse=True)
    return found


def select_model(found, choice=None):
    if not found:
        raise SystemExit('没有找到已训练的价格模型。请先训练，模型目录里需要 price_native_bundle.pkl。')
    if choice:
        number = int(choice)
        if number < 1 or number > len(found):
            raise SystemExit('序号超出范围：' + choice)
        return found[number - 1]
    if len(found) == 1 or not sys.stdin.isatty():
        print('使用模型：' + found[0]['label'])
        return found[0]
    print('请选择要封装的模型：')
    for index, item in enumerate(found, 1):
        print('%d. %s' % (index, item['label']))
    raw = input('输入序号，直接回车使用 1：').strip()
    number = int(raw) if raw else 1
    if number < 1 or number > len(found):
        raise SystemExit('序号无效')
    return found[number - 1]


def input_table(model_dir, explicit=None):
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise SystemExit('找不到成品表：' + str(path))
        return path
    candidates = []
    for folder in (model_dir / 'input', model_dir, model_dir.parent / 'input'):
        if not folder.is_dir():
            continue
        candidates.extend(sorted(folder.glob('*.xlsx')))
        candidates.extend(sorted(folder.glob('*.csv')))
    candidates = [path for path in candidates if path.name != 'price_native_bundle.pkl']
    if not candidates:
        raise SystemExit('这个模型目录里没有成品表。请把 Excel 或 CSV 放进该目录的 input 文件夹，或使用 --input。')
    return candidates[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description='用最近一次编译的引擎和已有模型生成客户运行包')
    parser.add_argument('--choice', help='模型序号，从 1 开始')
    parser.add_argument('--release', help='直接指定模型目录')
    parser.add_argument('--input', help='成品表。默认使用模型目录里的表')
    parser.add_argument('--product-code')
    parser.add_argument('--product-name')
    args = parser.parse_args(argv)
    kit = kit_root()
    if kit is None:
        raise SystemExit('请在完整开发包里运行。')
    from product.common import read_json
    from tools.build_frozen_engine import engine_sources
    from tools.engine_pointer import resolve_engine
    from tools.package_existing_model import assemble, package_exe
    engine = resolve_engine()
    if engine is None or not (engine / 'product.exe').is_file():
        raise SystemExit('没有可用引擎。请先双击“重新编译软件”。')
    if read_json(engine / 'BOOTSTRAP_SOURCE.json') != engine_sources():
        raise SystemExit('当前源码和引擎不一致。请先重新编译，成功后再生成运行包。')
    if args.release:
        chosen = {'dir': Path(args.release).resolve(), 'label': args.release, 'engine': ''}
    else:
        chosen = select_model(discover_models(kit), args.choice)
    model_dir = chosen['dir']
    if not (model_dir / 'price_native_bundle.pkl').is_file():
        nested = model_dir / 'price' / 'price_native_bundle.pkl'
        if nested.is_file() and (model_dir / 'prototype_manifest.json').is_file():
            release = model_dir
            table = None
        else:
            raise SystemExit('指定目录没有价格模型：' + str(model_dir))
    else:
        table = input_table(model_dir, args.input)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        release = ROOT / 'deliverables' / ('release_' + stamp)
        assemble(model_dir, table, release, product_code=args.product_code, product_name=args.product_name, virtual_data=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    exe_out = ROOT / 'deliverables' / ('product_' + stamp)
    version = 'client-' + stamp
    built = package_exe(release, exe_out, engine, version, archive=False)
    manifest = exe_out / 'PACKAGE_MANIFEST.json'
    record = {'status': 'pass', 'engine': str(engine), 'release': str(release), 'package': built['package'],
              'manifest': str(manifest), 'files': built['files'], 'model_sha256': built.get('model_sha256')}
    print(json.dumps(record, ensure_ascii=False, indent=2))
    print('客户运行包已生成：' + built['package'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
