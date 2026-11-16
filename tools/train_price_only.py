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


def load_input_table(path, backend, product_code='R13_TRAIN', product_name='独立训练', config_path=None, overrides=None):
    """Read a dual-header Excel or CSV with the same config the trainer will use."""
    from automl_pipeline.__main__ import config_loader
    from app.historical_onboarding import HistoricalProductOnboarding
    from app.product_table import product_table_rows
    from automl_pipeline.product_price_optuna_v2 import workbook_config, apply_attribute_groups
    from services.price_service.optuna_v2.typed_features import apply_types, restore_configurations
    from tools.product_identity import workbook_product_name
    source = Path(path)
    raw = source.read_bytes()
    loader = config_loader(backend)
    cfg = loader(config_path, overrides)
    table = product_table_rows(source.name, raw, require_dual_header=True)
    product_name = workbook_product_name(source, product_name)
    analyzed = HistoricalProductOnboarding().analyze(
        source.name, raw, product_code, product_name, cfg['input']['missing_tokens'], require_dual_header=True)
    if not analyzed['price_column_detected']:
        raise ValueError('成品表须包含价格列 price_wan')
    data = analyzed['data']
    cfg = workbook_config(source, cfg, {p['parameter_id'] for p in data['parameters']}, loader=loader)
    restore_configurations(data, table, cfg['expert']['field_types'], cfg['input']['missing_tokens'])
    apply_types(data, cfg['expert']['field_types'])
    if backend in ('optuna_v2', 'optuna_v3'):
        apply_attribute_groups(data, cfg)
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description='单独训练价格模型，不生成整套软件')
    parser.add_argument('--backend', default='optuna_v3', choices=tuple(TRAINERS))
    parser.add_argument('--output', required=True)
    parser.add_argument('--input', help='双行表头的 Excel 或 CSV')
    parser.add_argument('--config', help='可选训练配置 JSON')
    parser.add_argument('--product-code', default='R13_TRAIN')
    parser.add_argument('--product-name', default='独立训练')
    parser.add_argument('--trials', type=int, default=2)
    parser.add_argument('--smoke', action='store_true', help='不用外部表格，用内置小样本训练')
    args = parser.parse_args(argv)
    if args.trials < 1:
        raise SystemExit('试验次数至少为 1')
    if args.smoke and args.input:
        raise SystemExit('--input 与 --smoke 请只选一个')
    if not args.smoke and not args.input:
        raise SystemExit('请用 --input 指定成品表，或用 --smoke 做内置小样本训练')
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    import importlib
    module_name, function_name = TRAINERS[args.backend]
    train = getattr(importlib.import_module(module_name), function_name)
    overrides = overrides_for(args.backend, args.trials)
    data = smoke_data() if args.smoke else load_input_table(
        args.input, args.backend, args.product_code, args.product_name, args.config, overrides)
    kwargs = {'overrides': overrides, 'progress': lambda _line: None}
    if args.config:
        kwargs['config_path'] = args.config
    if args.input:
        kwargs['input_path'] = str(Path(args.input).resolve())
    if args.backend == 'optuna_v2':
        kwargs['engine'] = args.backend
    _, report = train(data, output, **kwargs)
    if args.input:
        import shutil
        saved = output / 'input' / Path(args.input).name
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(args.input), str(saved))
    import hashlib
    model_path = output / 'price_native_bundle.pkl'
    summary = {'backend': args.backend, 'trials': args.trials, 'training_rows': report.get('training_rows'),
               'model': str(model_path), 'model_sha256': hashlib.sha256(model_path.read_bytes()).hexdigest() if model_path.is_file() else None,
               'engine': report.get('engine', args.backend), 'config': str(Path(args.config).resolve()) if args.config else None,
               'input': str(Path(args.input).resolve()) if args.input else None}
    requested = output / 'requested_config.json'
    if requested.is_file():
        from tools.package_existing_model import preprocessing_view
        summary['preprocess'] = preprocessing_view(json.loads(requested.read_text(encoding='utf-8-sig')))
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
