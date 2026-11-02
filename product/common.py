"""Standard-library helpers shared by the frozen launcher and workers."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def contained(root, relative):
    root = Path(root).resolve()
    target = (root/relative).resolve()
    if root not in target.parents or Path(relative).is_absolute():
        raise ValueError('Package path escapes its directory: '+str(relative))
    return target


def verify_package(root, full=False):
    root = Path(root).resolve()
    manifest = read_json(root/'PACKAGE_MANIFEST.json')
    if manifest.get('format') != 'pipeline-per-desktop-1.0':
        raise ValueError('Unsupported desktop package')
    records = manifest['files']
    configurable=manifest.get('configurable_files',[])
    if not isinstance(configurable,list) or any(v!='defaults/desktop.json' for v in configurable):
        raise ValueError('Unsupported configurable package file')
    selected = records if full else {key: value for key, value in records.items()
                                    if key.startswith('release/')}
    for relative, expected in selected.items():
        target = contained(root, relative)
        if relative in configurable:
            if not target.is_file():raise ValueError('Missing desktop port template')
            from product.desktop_config import validate_config
            validate_config(read_json(target))
        elif not target.is_file() or sha(target) != expected:
            raise ValueError('Package integrity check failed: '+relative)
    for key in ('price_model', 'effectiveness_model', 'business_package', 'evaluation_metadata'):
        relative = manifest['release'][key]
        if relative not in records or not relative.startswith('release/'):
            raise ValueError('Unsealed release artifact: '+key)
        contained(root, relative)
    return manifest


def data_directory(package, override=None):
    configured = override or os.environ.get('PIPELINE_PER_DATA_DIR')
    if configured:
        target = Path(configured).expanduser().resolve()
    else:
        local = os.environ.get('LOCALAPPDATA')
        if not local:
            raise ValueError('LOCALAPPDATA is unavailable; specify --data-dir')
        base = (Path(local)/'PipelinePer').resolve()
        manifest_path = Path(package)/'PACKAGE_MANIFEST.json'
        code = read_json(manifest_path).get('release',{}).get('product_code') if manifest_path.exists() else None
        if code:
            # Stable across repacks/model updates; never mix different products.
            key = hashlib.sha256(str(code).encode('utf-8')).hexdigest()[:32]
            target = base/'products'/key
            legacy = base/'data/protocol_demo.db'
            if not (target/'data/protocol_demo.db').exists() and legacy.exists():
                from product.compatibility import inspect_database
                try:
                    previous = inspect_database(legacy)
                except Exception as exc:
                    raise ValueError('旧用户数据无法核验，请先备份并检查，或用 --data-dir 指定独立目录：'+str(base)) from exc
                # Preserve discoverability of the same product's legacy data.
                # Full schema/price/upgrade checks still run before any writes.
                if previous['product_code'] == code:
                    target = base
        else:
            target = base
    package = Path(package).resolve()
    if target == package or package in target.parents or target in package.parents:
        raise ValueError('User data must be separate from the application package')
    return target


class InstanceLock:
    """OS-released byte lock, so stale PID files never authorize killing a PID."""
    def __init__(self, root):
        import msvcrt
        path = Path(root)/'runtime/instance.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open('a+b')
        self.stream.seek(0, os.SEEK_END)
        if not self.stream.tell():
            self.stream.write(b'0')
            self.stream.flush()
        self.stream.seek(0)
        try:
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.stream.close()
            raise RuntimeError('An instance already owns this user-data directory')

    def close(self):
        import msvcrt
        if not self.stream.closed:
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            self.stream.close()
