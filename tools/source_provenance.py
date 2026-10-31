"""Source identity for a checkout or a sealed offline source distribution."""
import hashlib
import json
from pathlib import Path
import subprocess


def source_commit(root):
    root=Path(root).resolve()
    seal=root/'SOURCE_BUILD.json'
    if seal.is_file():
        manifest=json.loads(seal.read_text(encoding='utf-8-sig'))
        if manifest.get('format')!='pipeline-source-distribution-1.0':raise ValueError('Invalid source distribution seal')
        for relative,expected in manifest['files'].items():
            path=(root/relative).resolve()
            if root not in path.parents or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
                raise ValueError('Offline source changed: '+relative)
        return manifest['source_sha']
    if not (root/'.git').exists():raise ValueError('Source checkout or sealed offline source distribution required')
    return subprocess.check_output(['git','-c','safe.directory='+root.as_posix(),'rev-parse','HEAD'],cwd=str(root),text=True).strip()
