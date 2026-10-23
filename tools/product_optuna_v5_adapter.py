# -*- coding: utf-8 -*-
"""Compatibility import for the V5 native adapter."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import automl_pipeline.product_optuna_v5_adapter as _implementation
if __name__=='__main__':raise SystemExit(getattr(_implementation,'main',lambda:0)())
sys.modules[__name__]=_implementation
