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


def prepare_existing(package, data, manifest, confirm):
    """Classify, back up when required, and refuse unclassified changes without a yes."""
    data = Path(data)
    try:
        state = validate_data(package, data, manifest)
    except StartupBlocked as exc:
        if confirm(exc.prompt + '\n\n是否先备份现有数据？备份后仍不会启动，也不会改用新模型。'):
            archive = backup_data(data)
            record_change(data, dict(exc.report, backup=str(archive), accepted=False))
            raise StartupBlocked(str(exc) + '\n已备份到：' + str(archive), exc.report)
        raise
    except StartupConfirmation as exc:
        if not confirm(exc.prompt):
            raise ValueError('已取消启动，用户数据未修改。\n' + exc.prompt)
        archive = backup_data(data)
        accepted = accept_initial(data, manifest, load_contract(package, manifest))
        write_json(data/'delivery_state.json', accepted)
        record_change(data, dict(exc.report, backup=str(archive), accepted=True))
        return dict(accepted, startup_action='confirmed', change_class='unclassified', backup=str(archive))
    action = state.get('startup_action') or 'start'
    if action == 'backup':
        archive = backup_data(data)
        accepted = accept_initial(data, manifest, load_contract(package, manifest))
        write_json(data/'delivery_state.json', accepted)
        state = dict(state, backup=str(archive))
    if state.get('change_class') not in (None, 'none'):
        record_change(data, {'change_class': state.get('change_class'), 'startup_action': action,
                             'active_release_id': state.get('active_release_id'),
                             'backup': state.get('backup')})
    return state


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
