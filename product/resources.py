"""Read immutable UI resources from the executable, or source in development."""
from pathlib import Path
import sys


def read_asset(name, package=None):
    if getattr(sys,'frozen',False):
        from product._frozen_resources import read
        return read(name)
    source=Path(__file__).resolve().parents[1]/name
    if not source.exists() and package is not None:
        source=Path(package)/'payload'/name
    return source.read_bytes()
