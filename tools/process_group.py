"""Compatibility alias for the canonical service ownership implementation."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import automl_pipeline.process_group as _implementation
sys.modules[__name__]=_implementation
