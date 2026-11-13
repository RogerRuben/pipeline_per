# -*- coding: utf-8 -*-
"""Train one price model without building the desktop program."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def smoke_data(records=6, features=4):
    import numpy as np
    rng = np.random.RandomState(13)
    matrix = rng.uniform(0.2, 1.0, (records, features))
    prices = np.exp(3.2 + 0.4 * matrix[:, 0] + rng.normal(0, 0.02, records))
    specs = [{'parameter_id': 'x%d' % j, 'label': '属性%d' % j, 'value_type': 'number', 'unit': '',
              'required': True, 'inference': {'observed_count': records}} for j in range(features)]
    return {'products': [{'product_code': 'R13_SMOKE', 'product_name': '独立训练检查'}], 'parameters': specs,
            'agreements': [{'agreement_id': 'S%03d' % i, 'product_code': 'R13_SMOKE',
                            'historical_price_wan': float(prices[i]),
                            'params': {'x%d' % j: float(matrix[i, j]) for j in range(features)}} for i in range(records)]}


TRAINERS = {
    'optuna_v2': ('automl_pipeline.product_price_optuna_v2', 'train_price_optuna_v2'),
    'optuna_v3': ('automl_pipeline.product_price_optuna_v3', 'train_price_optuna_v3'),
    'optuna_v4': ('automl_pipeline.product_price_optuna_v4', 'train_price_optuna_v4'),
    'optuna_v5': ('automl_pipeline.product_price_optuna_v5', 'train_price_optuna_v5'),
    'optuna_v6': ('automl_pipeline.product_price_optuna_v6', 'train_price_optuna_v6'),
    'optuna_v7': ('automl_pipeline.product_price_optuna_v7', 'train_price_optuna_v7'),
}


def overrides_for(backend, trials):
    common = {'search': {'n_trials': trials}, 'expert': {'enabled': False}, 'targets': {'hard_gate': False}}
    if backend == 'optuna_v3':
        common['v3'] = {'startup_trials': 1, 'warm_start': {'auto_history': False}}
    if backend == 'optuna_v4':
        common['v4'] = {'search': {'mode': 'random', 'coarse_splits': 1, 'global_trials': trials, 'trials_per_split': trials},
                        'splits': {'random_splits': 1, 'max_splits': 1},
                        'ensemble': {'weight_trials': 1, 'max_configurations': 1},
                        'evaluation': {'mode': 'development', 'holdout_fraction': 0.2}}
    return common


def main(argv=None):
    parser = argparse.ArgumentParser(description='单独训练价格模型，不生成整套软件')
    parser.add_argument('--backend', default='optuna_v3', choices=tuple(TRAINERS))
    parser.add_argument('--output', required=True)
    parser.add_argument('--trials', type=int, default=2)
    parser.add_argument('--smoke', action='store_true', help='用内置小样本真正训练一次，写出模型文件')
    args = parser.parse_args(argv)
    if args.trials < 1:
        raise SystemExit('试验次数至少为 1')
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if not args.smoke:
        raise SystemExit('请提供已整理的数据接口，或使用 --smoke 做一次真实小样本训练。当前没有静默成功。')
    import importlib
    module_name, function_name = TRAINERS[args.backend]
    train = getattr(importlib.import_module(module_name), function_name)
    kwargs = {'overrides': overrides_for(args.backend, args.trials), 'progress': lambda _line: None}
    if args.backend == 'optuna_v2':
        kwargs['engine'] = args.backend
    _, report = train(smoke_data(), output, **kwargs)
    summary = {'backend': args.backend, 'trials': args.trials, 'training_rows': report.get('training_rows'),
               'model': str(output / 'price_native_bundle.pkl'), 'engine': report.get('engine', args.backend)}
    (output / 'train_price_only.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    if not (output / 'price_native_bundle.pkl').is_file():
        raise SystemExit('训练结束但没有写出价格模型文件')
    from services.price_service.native_bundle import load_bundle
    loaded = load_bundle(output / 'price_native_bundle.pkl')
    summary['format_version'] = loaded.get('format_version')
    summary['features'] = len(loaded.get('feature_order') or [])
    (output / 'train_price_only.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
