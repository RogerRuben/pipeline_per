# -*- coding: utf-8 -*-
"""Optional on-site page files. They never replace Python code."""
from pathlib import Path

ALLOWED = {'.html', '.css', '.js', '.png', '.svg', '.json', '.woff', '.woff2', '.ico'}


def override_file(root, relative):
    """Return a file inside root, or None when the path escapes or is not a page asset."""
    if not root or not relative or relative.startswith(('/', '\\')) or '..' in Path(relative).parts:
        return None
    base = Path(root).resolve()
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    if candidate.suffix.lower() not in ALLOWED or not candidate.is_file():
        return None
    return candidate
