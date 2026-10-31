"""Versioned SQLite/config backups and offline restore into a fresh directory."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile

from product.common import InstanceLock,read_json,sha,write_json
from product.compatibility import validate_data

MAX_TOTAL=600*1024*1024
RESERVED={'CON','PRN','AUX','NUL'}|{'COM%d'%i for i in range(1,10)}|{'LPT%d'%i for i in range(1,10)}


def allowed(name):
    if name in ('data/protocol_demo.db','installed.json','delivery_state.json','runtime/initialized.json'):
        return True
    path=PurePosixPath(name)
    return (len(path.parts)==2 and path.parts[0]=='config' and
            re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.json',path.parts[1]) is not None
            and path.parts[1].split('.')[0].upper() not in RESERVED)


def backup_data(data):
    data=Path(data)
    backup_dir=data/'backups';backup_dir.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='snapshot-',dir=str(backup_dir)) as temporary:
        stage=Path(temporary);database=data/'data/protocol_demo.db'
        if not database.is_file():raise ValueError('No initialized database to back up')
        (stage/'data').mkdir()
        with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as source:
            with closing(sqlite3.connect(str(stage/'data/protocol_demo.db'))) as destination:
                source.backup(destination)
        files=['data/protocol_demo.db']
        sources=list((data/'config').glob('*.json'))+[data/name for name in
                 ('installed.json','delivery_state.json','runtime/initialized.json')]
        for path in sources:
            if path.is_file():
                relative=path.relative_to(data).as_posix()
                if not allowed(relative):raise ValueError('Unsupported backup configuration filename: '+relative)
                target=stage/relative;target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(str(path),str(target));files.append(relative)
        records={name:sha(stage/name) for name in files}
        write_json(stage/'BACKUP_MANIFEST.json',{'format':'pipeline-user-backup-1.0','files':records,
                   'scope':'SQLite, JSON config, installation and accepted contract state; no control tokens, logs or exports'})
        fd,filename=tempfile.mkstemp(prefix='backup-',suffix='.zip.tmp',dir=str(backup_dir));os.close(fd)
        pending=Path(filename);target=pending.with_suffix('')
        try:
            with zipfile.ZipFile(str(pending),'w',zipfile.ZIP_DEFLATED) as archive:
                for name in files+['BACKUP_MANIFEST.json']:archive.write(str(stage/name),name)
            os.replace(str(pending),str(target))
        finally:
            if pending.exists():pending.unlink()
    return target


def unpack_backup(archive_path,stage):
    """No extractall: reject duplicate, linked, oversized or unlisted entries."""
    with zipfile.ZipFile(str(archive_path)) as archive:
        members=archive.infolist();names=[item.filename for item in members]
        if len({name.casefold() for name in names})!=len(names):raise ValueError('Duplicate backup path')
        if sum(item.file_size for item in members)>MAX_TOTAL:raise ValueError('Backup is too large')
        for item in members:
            if item.filename!='BACKUP_MANIFEST.json' and not allowed(item.filename):
                raise ValueError('Unsafe or unsupported backup path: '+item.filename)
            if item.is_dir() or stat.S_ISLNK(item.external_attr>>16):raise ValueError('Linked/directory backup member')
        metadata=archive.getinfo('BACKUP_MANIFEST.json')
        if metadata.file_size>2*1024*1024:raise ValueError('Backup manifest is too large')
        manifest=json.loads(archive.read(metadata).decode('utf-8-sig'))
        if manifest.get('format')!='pipeline-user-backup-1.0':raise ValueError('Unsupported backup format')
        records=manifest['files']
        if set(names)!=set(records)|{'BACKUP_MANIFEST.json'} or 'data/protocol_demo.db' not in records:
            raise ValueError('Backup file inventory does not match its manifest')
        for item in members:
            if item.filename=='BACKUP_MANIFEST.json':continue
            target=stage/item.filename;target.parent.mkdir(parents=True,exist_ok=True)
            digest=hashlib.sha256();total=0
            with archive.open(item) as source,target.open('xb') as destination:
                for block in iter(lambda:source.read(1024*1024),b''):
                    total+=len(block)
                    if total>MAX_TOTAL:raise ValueError('Backup exceeded size budget')
                    digest.update(block);destination.write(block)
            if digest.hexdigest()!=records[item.filename]:raise ValueError('Backup SHA256 mismatch: '+item.filename)
    return records


def restore_backup(package,data,manifest,archive_path):
    """Restore never overwrites a database or existing config. Caller chooses a new root."""
    data=Path(data).resolve()
    if data.exists() and any(data.iterdir()):raise ValueError('Restore requires a new or empty data directory')
    data.mkdir(parents=True,exist_ok=True)
    lock=InstanceLock(data)
    installed=[]
    try:
        with tempfile.TemporaryDirectory(prefix='restore-',dir=str(data/'runtime')) as temporary:
            stage=Path(temporary)
            records=unpack_backup(archive_path,stage)
            state=validate_data(package,stage,manifest)
            # Validation is read-only. Publish only with exclusive ownership;
            # database is last, so an interrupted restore cannot look initialized.
            order=sorted(set(records)-{'data/protocol_demo.db'})+['data/protocol_demo.db']
            journal=data/'runtime/restore_pending.json'
            write_json(journal,{'backup_sha256':sha(archive_path),'files':order})
            try:
                for relative in order:
                    target=data/relative;target.parent.mkdir(parents=True,exist_ok=True)
                    if target.exists():raise ValueError('Restore refuses to replace '+relative)
                    os.replace(str(stage/relative),str(target));installed.append((relative,target))
                write_json(data/'runtime/restored.json',{'backup_sha256':sha(archive_path),
                           'active_release_id':state['active_release_id'],'files_restored':len(records)})
                journal.unlink()
            except Exception:
                for relative,target in reversed(installed):
                    original=stage/relative;original.parent.mkdir(parents=True,exist_ok=True)
                    os.replace(str(target),str(original))
                journal.unlink()
                raise
        return {'status':'restored','data_dir':str(data),'files_restored':len(records),
                'active_release_id':state['active_release_id'],'backup_sha256':sha(archive_path)}
    finally:
        lock.close()
