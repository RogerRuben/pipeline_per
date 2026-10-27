"""Explicit V21.6 frontend/backend dependency closure for cumulative packages."""
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SOURCE_FILES=tuple(sorted(p.relative_to(ROOT).as_posix()
    for folder in ('app','cost_effectiveness_analysis') for p in (ROOT/folder).rglob('*')
    if p.is_file() and (p.suffix in ('.py','.html','.js','.css') or 'defaults' in p.parts and p.suffix=='.json')
    and not set(p.relative_to(ROOT).parts)&{'__pycache__','tests'}))
