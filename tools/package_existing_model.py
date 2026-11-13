# -*- coding: utf-8 -*-
"""Copy one already trained price model into a package folder. Does not retrain."""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description='封装指定目录里的已训练价格模型')
    parser.add_argument('--release', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    release = Path(args.release).resolve()
    bundle = release / 'price_native_bundle.pkl'
    if not bundle.is_file():
        raise SystemExit('指定目录没有 price_native_bundle.pkl：' + str(release))
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(bundle), str(output / 'price_native_bundle.pkl'))
    for name in ('best_config.json', 'train_price_only.json'):
        source = release / name
        if source.is_file():
            shutil.copy2(str(source), str(output / name))
    record = {'release': str(release), 'model': str(output / 'price_native_bundle.pkl'),
              'bytes': (output / 'price_native_bundle.pkl').stat().st_size}
    (output / 'package_record.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(record, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
