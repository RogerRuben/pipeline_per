import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import automl_pipeline.product_price_optuna_v7 as _implementation
sys.modules[__name__]=_implementation
