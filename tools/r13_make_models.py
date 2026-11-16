# -*- coding: utf-8 -*-
"""Train synthetic V2-V7 bundles and write a support matrix. Not a customer-data proof."""
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.train_price_only import TRAINERS, overrides_for, smoke_data


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / 'outputs' / 'r13_matrix')
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    data = smoke_data()
    for backend in TRAINERS:
        folder = out / backend
        trials = 8 if backend == 'optuna_v6' else 1
        item = {'backend': backend, 'status': 'failed', 'verified_against_customer_file': False}
        try:
            import importlib
            module_name, function_name = TRAINERS[backend]
            train = getattr(importlib.import_module(module_name), function_name)
            kwargs = {'overrides': overrides_for(backend, trials), 'progress': lambda _line: None}
            if backend == 'optuna_v2':
                kwargs['engine'] = backend
            _, report = train(data, folder, **kwargs)
            from services.price_service.native_bundle import load_bundle, predict_batch
            bundle = load_bundle(folder / 'price_native_bundle.pkl')
            sample = data['agreements'][0]['params']
            predicted = predict_batch(bundle, [sample])
            item.update(status='trained_and_reloaded', format_version=bundle.get('format_version'),
                        features=len(bundle.get('feature_order') or []),
                        predicted_price_wan=predicted[0].get('predicted_price_wan'),
                        engine=report.get('engine', backend))
        except Exception as exc:
            item['error'] = '%s: %s' % (type(exc).__name__, exc)
            (folder / 'error.txt').parent.mkdir(parents=True, exist_ok=True)
            (folder / 'error.txt').write_text(traceback.format_exc(), encoding='utf-8')
        rows.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
    (out / 'matrix.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    failed = [row for row in rows if row['status'] != 'trained_and_reloaded']
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
