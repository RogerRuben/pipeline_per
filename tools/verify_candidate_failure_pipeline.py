# -*- coding: utf-8 -*-
import json,sys
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from sklearn.dummy import DummyRegressor
from tools.product_pipeline import build
from tools.product_price_automl_v2 import FitWorker as RealWorker
from tools.product_price_tabpfn import factory as real_factory


def candidate_factory(candidate,seed,specs,config):
    if candidate['family']=='tabpfn':
        return DummyRegressor(strategy='constant',constant=-1.)
    return real_factory(candidate,seed,specs,config)


def worker_factory(config,deadline,ignored_factory=None):
    return RealWorker(config,deadline,candidate_factory)


def main():
    output=ROOT/'outputs/candidate_failure_fix_20261015/negative_neural_pipeline'
    with patch('tools.product_price_tabpfn.FitWorker',worker_factory):
        manifest=build(ROOT/'tests/fixtures/product_pipeline_v2/virtual_10x50.xlsx',output,
             product_code='NEGATIVE_CANDIDATE_TEST',product_name='候选失败隔离验证',
             price_engine='tabpfn',price_config=ROOT/'docs/examples/price_tabpfn_micro_robust.config.json',virtual_data=True)
    report=json.loads((output/'price/training_report.json').read_text(encoding='utf-8'))
    assert manifest['status']=='ready' and manifest['targets_met']
    assert report['failed_candidates'] and report['tabpfn_only']['status']=='unavailable'
    assert not (output/'price/tabpfn_only_bundle.pkl').exists()
    assert all(row['family']!='tabpfn' for row in report['member_ledger'])
    assert report['native_equivalence_max_delta_wan']<=1e-5
    print('PASS: real subprocess candidates predicted -1, valid models still trained, passed MAPE/R2 and exported all three stages',flush=True)

if __name__=='__main__':main()