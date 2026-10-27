# -*- coding: utf-8 -*-
"""Canonical AutoML sources required by source, PATCH and saved-runtime exports."""
from pathlib import Path

MODULES=(
    'product_pipeline','product_effectiveness_bootstrap',
    'product_price_optuna_v2','product_price_optuna_v3','product_price_optuna_v4',
    'product_optuna_v2_input','product_optuna_v4_adapter','product_price_optuna_v5','product_optuna_v5_adapter','product_price_optuna_v6','product_optuna_v6_adapter','process_group','product_effectiveness_import','product_modes','product_optuna_versions')
SOURCE_FILES=('automl_pipeline/__init__.py','automl_pipeline/__main__.py','automl_pipeline/service_runner.py','tools/process_group.py')+tuple('automl_pipeline/'+name+'.py' for name in MODULES)+(
    'automl_pipeline/README.md','automl_pipeline/requirements.txt',
    'automl_pipeline/configs/price_optuna_v2.json','automl_pipeline/configs/price_optuna_v3.json','automl_pipeline/configs/price_optuna_v4.json',
    'automl_pipeline/configs/price_optuna_v5.json',
    'automl_pipeline/configs/price_optuna_v6.json',
    'automl_pipeline/examples/virtual_16x36.xlsx','automl_pipeline/examples/virtual_v6_16x36.xlsx')


def source_entries(root):
    root=Path(root)
    return {name:(root/name).read_bytes() for name in SOURCE_FILES}
