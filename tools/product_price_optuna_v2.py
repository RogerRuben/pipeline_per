# -*- coding: utf-8 -*-
"""Compatibility entry point; implementation lives in automl_pipeline.product_price_optuna_v2."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import automl_pipeline.product_price_optuna_v2 as _implementation
if __name__=='__main__':
    raise SystemExit(getattr(_implementation,'main',lambda:0)())
sys.modules[__name__]=_implementation
