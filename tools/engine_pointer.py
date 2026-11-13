# -*- coding: utf-8 -*-
"""Point packaging at the engine frozen from the current sources."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _kit():
    parent = ROOT.parent
    if (parent / 'KIT_MANIFEST.json').is_file():
        return parent
    return None


def remember_engine(engine_dir):
    from product.common import read_json, write_json, sha
    engine_dir = Path(engine_dir).resolve()
    meta = read_json(engine_dir / 'BOOTSTRAP_META.json')
    if meta.get('kind') != 'pyinstaller-full-app-onedir':
        raise ValueError('冻结结果不是可发布的完整引擎')
    record = {'engine': str(engine_dir), 'exe_sha256': meta.get('exe_sha256'), 'kind': meta['kind']}
    kit = _kit()
    if kit is not None:
        try:
            record['engine'] = engine_dir.relative_to(kit).as_posix()
        except ValueError:
            record['engine'] = str(engine_dir)
        record['refreshed_project_files'] = _refresh_project_hashes(kit, sha, read_json, write_json)
        write_json(kit / 'LAST_ENGINE.json', record)
    else:
        write_json(ROOT / 'outputs' / 'LAST_ENGINE.json', record)
    return record


def _refresh_project_hashes(kit, sha, read_json, write_json):
    manifest_path = kit / 'KIT_MANIFEST.json'
    manifest = read_json(manifest_path)
    files = dict(manifest['files'])
    changed = []
    for relative, expected in list(files.items()):
        if not relative.startswith('project/'):
            continue
        path = kit / relative
        if not path.is_file():
            continue
        digest = sha(path)
        if digest != expected:
            files[relative] = digest
            changed.append(relative)
    source_build = kit / 'project' / 'SOURCE_BUILD.json'
    if source_build.is_file():
        source = read_json(source_build)
        project_files = dict(source.get('files') or {})
        project = kit / 'project'
        for path in sorted(project.rglob('*')):
            if not path.is_file() or path.name == 'SOURCE_BUILD.json':
                continue
            relative = path.relative_to(project).as_posix()
            if relative in project_files:
                project_files[relative] = sha(path)
        source['files'] = project_files
        write_json(source_build, source)
        files['project/SOURCE_BUILD.json'] = sha(source_build)
    manifest['files'] = files
    write_json(manifest_path, manifest)
    return changed


def resolve_engine(explicit=None):
    from product.common import read_json
    if explicit:
        return Path(explicit).resolve()
    pointers = []
    kit = _kit()
    if kit is not None:
        pointers.append(kit / 'LAST_ENGINE.json')
    pointers.append(ROOT / 'outputs' / 'LAST_ENGINE.json')
    for pointer in pointers:
        if not pointer.is_file():
            continue
        record = read_json(pointer)
        engine = Path(record.get('engine') or '')
        if not engine.is_absolute():
            engine = (pointer.parent / engine).resolve()
        if (engine / 'BOOTSTRAP_META.json').is_file() and (engine / 'product.exe').is_file():
            return engine
    if kit is not None and (kit / 'bootstrap' / 'product.exe').is_file():
        return (kit / 'bootstrap').resolve()
    return None
