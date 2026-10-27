# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import automl_pipeline.product_effectiveness_import as _implementation
if __name__=='__main__':raise SystemExit(_implementation.main())
sys.modules[__name__]=_implementation
