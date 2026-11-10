"""Startup side effects around an existing userdata directory.

Backup and migration never delete the product directory. A failed backup
cancels the write. A failed migration puts the database bytes back.
"""
from contextlib import closing
import json
import sqlite3
from pathlib import Path

from product.common import write_json
from product.compatibility import StartupBlocked, StartupConfirmation, accept_initial, load_contract, validate_data
from product.recovery import backup_data


def record_change(data, report):
    path = Path(data)/'logs'/'startup_changes.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + '\n')


def check_live_database(data):
    """Only actual SQLite integrity is a startup precondition, not release equality."""
    database=Path(data)/'data/protocol_demo.db'
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
        if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise ValueError('用户数据库损坏，请在管理入口查看恢复说明。')


def prepare_existing(package, data, manifest, confirm=None):
    """Start from live data. Contract drift is diagnostic, never an automatic migration."""
    data=Path(data);check_live_database(data)
    try:
        state=validate_data(package,data,manifest)
    except (ValueError,sqlite3.Error,KeyError,TypeError) as exc:
        diagnostic={'status':'review','startup_action':'start_live_database','message':str(exc),
                    'database_modified':False,'contract_accepted':False}
        record_change(data,diagnostic)
        write_json(data/'runtime/startup_diagnostics.json',diagnostic)
        return diagnostic
    write_json(data/'runtime/startup_diagnostics.json',{'status':'ok','startup_action':'start_live_database'})
    return dict(state,startup_action='start_live_database')


def capture_compatible_state(package,data,manifest):
    """Never claim an incompatible snapshot was accepted, and never close the web UI."""
    try:
        state=accept_initial(data,manifest,load_contract(package,manifest))
    except (ValueError,sqlite3.Error,KeyError,TypeError) as exc:
        diagnostic={'status':'review','startup_action':'start_live_database','message':str(exc),
                    'database_modified':False,'contract_accepted':False}
        record_change(data,diagnostic);write_json(Path(data)/'runtime/startup_diagnostics.json',diagnostic)
        return
    write_json(Path(data)/'delivery_state.json',state)


def _restore_database(archive, database):
    """Put back the checkpointed database and drop a half-written WAL."""
    import zipfile
    with zipfile.ZipFile(str(archive)) as bundle:
        database.write_bytes(bundle.read('data/protocol_demo.db'))
    for suffix in ('-wal', '-shm'):
        extra = Path(str(database) + suffix)
        if extra.exists():
            extra.unlink()


def run_guarded_migration(data, mutator):
    """Back up first. This is not the desktop startup path.

    Incompatible models are refused before any migration. Restoration uses the
    checkpoint inside the backup, then removes WAL sidecars.
    """
    data = Path(data)
    archive = backup_data(data)
    database = data/'data/protocol_demo.db'
    try:
        with closing(sqlite3.connect(str(database))) as connection:
            mutator(connection)
            connection.commit()
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Migration left the database unreadable')
    except Exception:
        _restore_database(archive, database)
        raise
    return archive
