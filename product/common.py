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
    selected = records if full else {key: value for key, value in records.items()
                                    if key.startswith('release/')}
    for relative, expected in selected.items():
        target = contained(root, relative)
        if not target.is_file() or sha(target) != expected:
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
        target = (Path(local)/'PipelinePer').resolve()
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
