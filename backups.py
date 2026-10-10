# ============================================================================
# FILE: backups.py
# Backups of what the user made - the collection, the scanned cards and the
# decks - taken from the collection page (before a big load) and restored there
# ============================================================================
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import zipfile
from zlib import error as zlib_error
from datetime import datetime
from pathlib import Path

from config import Config
from inventory import CAPTURES_DIR

logger = logging.getLogger(__name__)

# Made by the user (kept until deleted), on a schedule (create_scheduled) and before a restore.
# One folder per backup, named by its time: backup.db (the tables below as plain copies, and
# `info`) and captures/ - the capture thumbnails the entries point at, as hard links (no extra
# space; they stay when the app deletes its own). Card data and settings are not part of it:
# scripts/backup.sh archives everything.
BACKUPS_DIR = Config.DATA_DIR / 'backups'
BACKUP_ID = re.compile(r'^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(-\d+)?$')
# inventory_sources: which station scanned how many of an entry's copies (the scanned cards'
# camera filter) - it goes with the entries, or a restore would leave today's ownership on
# yesterday's entries
INVENTORY_TABLES = ('inventory', 'inventory_captures', 'inventory_sources')
DECK_TABLES = ('decks', 'deck_cards')
CAPTURE_NAME = re.compile(r'^[A-Za-z0-9_-]+\.(jpg|jpeg|png)$')  # a capture inside an uploaded backup
UPLOAD_MAX_BYTES = 2 * 1024 ** 3  # an uploaded backup, unpacked (a 4,600-card one is ~100 MB)
UPLOAD_MAX_FILES = 200_000
# restore: the rows of a backup that can stand in the tables as they are now (the constraints of
# inventory.py / decks.py) - backups made before them may hold entries without copies and rows
# beside entries or decks that were gone already. {prefix}: 'collection_' / 'scanned_' / ''
RESTORABLE = {
    'inventory': 'quantity > 0',
    'inventory_captures': 'inventory_id IN (SELECT id FROM {prefix}inventory WHERE quantity > 0)',
    'inventory_sources': 'quantity > 0 AND inventory_id IN (SELECT id FROM {prefix}inventory WHERE quantity > 0)',
    'decks': '1',
    'deck_cards': "quantity > 0 AND board IN ('commander', 'main', 'side') AND deck_id IN (SELECT id FROM decks)",
}
KEEP_AUTOMATIC = 5  # backups made before a restore; the ones the user makes are kept until deleted
# The scheduled ones (create_scheduled; marked 'daily' in their info, whatever the interval):
# how often, and how many are kept, are settings (backup_every_hours, backup_keep) - these are
# the choices and the defaults
SCHEDULE_HOURS = (0, 1, 6, 12, 24, 168)  # 0: off
EVERY_HOURS = 24
KEEP_DAILY = 7
KEEP_MAX = 60


class BackupError(Exception):
    pass


def _parts(inventory, scan_inventory, deck_store):
    """(prefix of the tables in backup.db, manager, its tables) - also the order of the locks
    (collection before scanned cards, as InventoryManager.take_from)"""
    return (('collection_', inventory, INVENTORY_TABLES), ('scanned_', scan_inventory, INVENTORY_TABLES),
            ('', deck_store, DECK_TABLES))


def _columns(conn, table):
    return [row[1] for row in conn.execute(f'PRAGMA table_info({table})')]


def _folder(backup_id):
    if not BACKUP_ID.match(backup_id or ''):
        raise BackupError('Unknown backup')
    folder = BACKUPS_DIR / backup_id
    if not (folder / 'backup.db').is_file():
        raise BackupError('Unknown backup')
    return folder


def _link(source, target):
    """A capture file in another place: a hard link, or a copy where links don't work"""
    if target.exists() or not source.exists():
        return
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def _fingerprint(inventory, scan_inventory, deck_store):
    """A hash of every row a backup would hold (call it under the three locks)"""
    digest = hashlib.sha1()
    for prefix, manager, tables in _parts(inventory, scan_inventory, deck_store):
        for table in tables:
            columns = _columns(manager.conn, table)
            digest.update(repr((prefix + table, columns)).encode())
            for row in manager.conn.execute(f"SELECT {', '.join(columns)} FROM {table}"):
                digest.update(repr(tuple(row)).encode())
    return digest.hexdigest()


def create(inventory, scan_inventory, deck_store, note='', automatic=False, daily=False, keep=None,
           keep_daily=KEEP_DAILY):
    """
    Back up the collection, the scanned cards and the decks as they are now; returns its info.
    keep: the id of a backup that must not be cleared away to make room for this one (the one
    about to be restored - it may be the oldest automatic backup itself). keep_daily: how many
    of the scheduled backups stay, when this is one (daily).
    """
    created = datetime.now()
    backup_id = created.strftime('%Y-%m-%d_%H-%M-%S')
    number = 1
    while (BACKUPS_DIR / backup_id).exists():  # another one this second
        number += 1
        backup_id = f"{created.strftime('%Y-%m-%d_%H-%M-%S')}-{number}"
    folder = BACKUPS_DIR / backup_id
    work = BACKUPS_DIR / f'.{backup_id}.tmp'  # moved into place when complete
    (work / 'captures').mkdir(parents=True)
    try:
        target = sqlite3.connect(str(work / 'backup.db'))
        parts = _parts(inventory, scan_inventory, deck_store)
        with inventory._lock, scan_inventory._lock, deck_store._lock:
            fingerprint = _fingerprint(inventory, scan_inventory, deck_store)
            for prefix, manager, tables in parts:
                for table in tables:
                    columns = _columns(manager.conn, table)
                    target.execute(f"CREATE TABLE {prefix}{table} ({', '.join(columns)})")
                    target.executemany(
                        f"INSERT INTO {prefix}{table} VALUES ({', '.join('?' * len(columns))})",
                        (tuple(row) for row in manager.conn.execute(f"SELECT {', '.join(columns)} FROM {table}")))
            for prefix in ('collection_', 'scanned_'):
                for (file,) in target.execute(f'SELECT file FROM {prefix}inventory_captures'):
                    _link(CAPTURES_DIR / file, work / 'captures' / file)
        count = lambda sql: target.execute(sql).fetchone()[0]
        info = {
            'id': backup_id, 'created': created.strftime('%Y-%m-%d %H:%M:%S'), 'note': (note or '').strip()[:80],
            'automatic': bool(automatic), 'daily': bool(daily),
            'cards': count('SELECT COALESCE(SUM(quantity), 0) FROM collection_inventory'),
            'entries': count('SELECT COUNT(*) FROM collection_inventory'),
            'scanned': count('SELECT COALESCE(SUM(quantity), 0) FROM scanned_inventory'),
            'decks': count('SELECT COUNT(*) FROM decks'),
            'fingerprint': fingerprint,  # create_scheduled: nothing changed since, no new backup
        }
        target.execute('CREATE TABLE info (value TEXT)')
        target.execute('INSERT INTO info VALUES (?)', (json.dumps(info),))
        target.commit()
        target.close()
        os.replace(work, folder)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    logger.info(f"Backup {backup_id}: {info['cards']} cards, {info['scanned']} scanned, {info['decks']} decks")
    # Only its own kind makes room: the daily ones don't push out the ones before a restore
    for kind, limit in (('daily', keep_daily if daily else None), ('automatic', KEEP_AUTOMATIC if automatic else None)):
        if limit:
            for old in [item for item in list_backups() if item.get(kind)][limit:]:
                if old['id'] != keep:
                    delete(old['id'])
    return info


def create_scheduled(inventory, scan_inventory, deck_store, every_hours=EVERY_HOURS, keep_count=KEEP_DAILY,
                     note='Automatic'):
    """
    The backup made on a schedule (asked for when the app starts and every few minutes after):
    one when the last scheduled backup is every_hours old or older - and none while every_hours
    is 0 (off), nothing has changed since the newest backup of any kind, or there is nothing
    to keep. The newest keep_count scheduled backups stay. Returns its info, or None.
    """
    if not every_hours:
        return None
    existing = list_backups()
    last = next((item for item in existing if item.get('daily')), None)
    if last:
        age = datetime.now() - datetime.strptime(last['created'], '%Y-%m-%d %H:%M:%S')
        if age.total_seconds() < every_hours * 3600:
            return None
    # Decided and made under the locks, so what is judged is what is saved. Nothing to keep is
    # decided before create: an empty backup once pushed out the last one with cards in it
    # (create clears older ones away) and was then deleted itself
    with inventory._lock, scan_inventory._lock, deck_store._lock:
        if not any(manager.conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()
                   for manager, table in ((inventory, 'inventory'), (scan_inventory, 'inventory'), (deck_store, 'decks'))):
            return None
        if existing and existing[0].get('fingerprint') == _fingerprint(inventory, scan_inventory, deck_store):
            return None
        return create(inventory, scan_inventory, deck_store, note=note, daily=True,
                      keep_daily=max(1, min(KEEP_MAX, int(keep_count))))


def parse_schedule(data):
    """
    The settings in a request to change the schedule ({'every_hours', 'keep'}, each optional), as
    {setting key: value} - or ValueError, before anything is saved: a request refused for one
    value must not have changed the other.
    """
    changes = {}
    try:
        if 'every_hours' in data:
            changes['backup_every_hours'] = int(data['every_hours'])
            if changes['backup_every_hours'] not in SCHEDULE_HOURS or isinstance(data['every_hours'], bool):
                raise ValueError
        if 'keep' in data:
            if isinstance(data['keep'], bool):
                raise ValueError
            changes['backup_keep'] = max(1, min(KEEP_MAX, int(data['keep'])))
    except TypeError:
        raise ValueError
    return changes


def list_backups():
    """The backups, newest first"""
    found = []
    if not BACKUPS_DIR.is_dir():
        return found
    for folder in sorted(BACKUPS_DIR.iterdir(), reverse=True):
        if not BACKUP_ID.match(folder.name) or not (folder / 'backup.db').is_file():
            continue
        try:
            source = sqlite3.connect(f"file:{folder / 'backup.db'}?mode=ro", uri=True)
            info = json.loads(source.execute('SELECT value FROM info').fetchone()[0])
            source.close()
        except (sqlite3.Error, TypeError, ValueError) as e:
            logger.warning(f"Backup {folder.name} cannot be read: {e}")
            continue
        found.append({**info, 'id': folder.name})
    return found


def restore(backup_id, inventory, scan_inventory, deck_store):
    """
    Put the collection, the scanned cards and the decks back as they were in a backup (what
    is there now is backed up first, so a restore can be taken back). Returns
    {'restored': the backup's info, 'previous': the backup made of the state replaced}.
    """
    folder = _folder(backup_id)
    # keep: the backup made here must not push out the one being restored (restoring the oldest
    # of the automatic backups once deleted it before it was read)
    previous = create(inventory, scan_inventory, deck_store, note=f"Before restoring {backup_id.replace('_', ' ')}",
                      automatic=True, keep=backup_id)
    source = sqlite3.connect(f"file:{folder / 'backup.db'}?mode=ro", uri=True)
    parts = _parts(inventory, scan_inventory, deck_store)
    try:
        with inventory._lock, scan_inventory._lock, deck_store._lock:
            # One part at a time: the collection and the decks are two connections to one
            # file, which cannot both hold a write. If a part fails the ones before it are
            # restored already - the backup just made has the state from before
            for prefix, manager, tables in parts:
                try:
                    for table in tables:
                        # Columns added since the backup keep their defaults
                        saved = _columns(source, prefix + table)
                        columns = [column for column in _columns(manager.conn, table) if column in saved]
                        manager.conn.execute(f'DELETE FROM {table}')
                        if not saved:
                            # A table the backup was made without (inventory_sources, before
                            # stations): it stays empty - what is there now belongs to other entries
                            continue
                        manager.conn.executemany(
                            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                            (tuple(row) for row in source.execute(
                                f"SELECT {', '.join(columns)} FROM {prefix}{table} "
                                f"WHERE {RESTORABLE[table].format(prefix=prefix)}")))
                    manager.conn.commit()
                except Exception as e:
                    manager.conn.rollback()
                    raise BackupError(f"The restore stopped part way ({e}) - the state from before is in the "
                                      f"backup \"{previous['note']}\"") from e
            inventory.last_added, scan_inventory.last_added = {}, {}
            # Captures deleted since the backup come back with their entries
            CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            for saved in (folder / 'captures').glob('*'):
                _link(saved, CAPTURES_DIR / saved.name)
        info = json.loads(source.execute('SELECT value FROM info').fetchone()[0])
    finally:
        source.close()
    logger.info(f"Restored backup {backup_id} (the state before it: backup {previous['id']})")
    return {'restored': {**info, 'id': backup_id}, 'previous': previous}


def archive(backup_id, target):
    """
    Write a backup as a zip file into `target` (a file open for writing) - to keep a copy on
    another machine. Inside is the backup's folder as it is in data/backups/: unpacked there, it
    is a backup again (listed, restorable). Returns the file name to give it.
    """
    folder = _folder(backup_id)
    with zipfile.ZipFile(target, 'w') as zipped:
        zipped.write(folder / 'backup.db', f'{backup_id}/backup.db', zipfile.ZIP_DEFLATED)
        for capture in sorted((folder / 'captures').glob('*')):
            zipped.write(capture, f'{backup_id}/captures/{capture.name}', zipfile.ZIP_STORED)  # JPEGs don't shrink
    return f'scanner-backup-{backup_id}.zip'


def add_archive(source):
    """
    Take in a backup downloaded earlier (archive's zip; `source`: a file open for reading), from
    this server or another one. It is only added to the list - nothing is restored. The file
    comes from outside, so nothing in it is trusted: one backup folder, backup.db and captures
    with plain names only (nothing is written outside its folder), a size limit on what it
    unpacks to, and a backup.db that reads as one. It counts as made by hand (never cleared
    away), whatever it was. Returns its info; BackupError says what is wrong with the file.
    """
    try:
        zipped = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError, ValueError):
        raise BackupError('This is not a backup file (a zip downloaded here is expected)')
    with zipped:
        members = [member for member in zipped.infolist() if not member.is_dir()]
        ids = {member.filename.split('/')[0] for member in members}
        backup_id = ids.pop() if len(ids) == 1 else ''
        if not BACKUP_ID.match(backup_id) or f'{backup_id}/backup.db' not in zipped.namelist():
            raise BackupError('This is not a backup file (a zip downloaded here is expected)')
        for member in members:
            parts = member.filename.split('/')
            if not (parts == [backup_id, 'backup.db']
                    or (len(parts) == 3 and parts[1] == 'captures' and CAPTURE_NAME.match(parts[2]))):
                raise BackupError(f'This is not a backup file: it contains "{member.filename[:80]}"')
        if len(members) > UPLOAD_MAX_FILES or sum(member.file_size for member in members) > UPLOAD_MAX_BYTES:
            raise BackupError('This backup file is too large')
        if (BACKUPS_DIR / backup_id).exists():
            raise BackupError(f"The backup of {backup_id.replace('_', ' ')} is here already")
        work = BACKUPS_DIR / f'.{backup_id}.upload.tmp'  # moved into place when complete and checked
        shutil.rmtree(work, ignore_errors=True)
        (work / 'captures').mkdir(parents=True)
        try:
            left = UPLOAD_MAX_BYTES  # what is really unpacked - the sizes above are only what the file claims
            for member in members:
                with zipped.open(member) as packed, open(work.joinpath(*member.filename.split('/')[1:]), 'wb') as out:
                    while chunk := packed.read(min(1024 * 1024, left + 1)):
                        left -= len(chunk)
                        if left < 0:
                            raise BackupError('This backup file is too large')
                        out.write(chunk)
            try:
                saved = sqlite3.connect(str(work / 'backup.db'))
                info = json.loads(saved.execute('SELECT value FROM info').fetchone()[0])
                for prefix, _manager, tables in _parts(None, None, None):
                    for table in tables[:1] if prefix else tables:  # inventory_sources: newer than some backups
                        saved.execute(f'SELECT 1 FROM {prefix}{table} LIMIT 1')
                info = {**info, 'id': backup_id, 'automatic': False, 'daily': False, 'uploaded': True,
                        'created': str(info['created']), 'note': str(info.get('note') or '')[:80],
                        **{key: int(info.get(key) or 0) for key in ('cards', 'entries', 'scanned', 'decks')}}
                saved.execute('UPDATE info SET value = ?', (json.dumps(info),))
                saved.commit()
                saved.close()
            except (sqlite3.Error, TypeError, ValueError, KeyError, AttributeError) as e:
                raise BackupError(f'This backup file cannot be read ({e})')
            os.replace(work, BACKUPS_DIR / backup_id)
        except (zipfile.BadZipFile, EOFError, zlib_error) as e:
            raise BackupError(f'This backup file is damaged ({e})')
        finally:
            shutil.rmtree(work, ignore_errors=True)
    logger.info(f"Backup {backup_id} uploaded: {info['cards']} cards, {info['scanned']} scanned, {info['decks']} decks")
    return info


# -- Everything, at one moment ------------------------------------------------------------
#
# The backups above hold what the user made (collection, scanned cards, decks). A full backup is
# the server's whole state for moving it or bringing it back after a lost disk: every database
# file, the files their rows point at, and the settings - taken at ONE moment. Snapshots of the
# files one after the other are each valid, but together they can show a card in the scanned
# cards and in the collection, or a capture both waiting and added.

FULL_DIR = BACKUPS_DIR / 'full'
KEEP_FULL = 2  # each holds a copy of the card database (the pictures are hard links)
FULL_ID = re.compile(r'^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$')


def create_full(managers, barrier, databases, linked, copied=(), keep=KEEP_FULL):
    """
    Snapshot every database file and what its rows point at, at one moment.

    managers: the objects that write (each with ._lock) - held, in this order, while the
        snapshots are taken: nothing of theirs is half way (a move between two files, a capture
        being settled) and no file they point at is deleted meanwhile;
    barrier: a lock held by whatever else settles captures or deletes pictures (app.py:
        maintenance_barrier);
    databases: the database files; linked: [(folder name in the backup, source folder,
        database file, SQL giving the file names)] - the pictures the rows point at, as hard
        links; copied: small files beside them (settings).
    Afterwards, without the locks, the snapshots are opened and checked and every file a row
    names is looked for. Returns the manifest (also manifest.json in the folder): 'id',
    'folder', 'databases' {name: rows per table}, 'files' {folder: count}, 'missing', 'verified'.
    """
    created = datetime.now()
    backup_id = created.strftime('%Y-%m-%d_%H-%M-%S')
    folder = FULL_DIR / backup_id
    work = FULL_DIR / f'.{backup_id}.tmp'
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        with contextlib.ExitStack() as held:
            held.enter_context(barrier)
            for manager in managers:
                held.enter_context(manager._lock)
            # Held as shortly as the copying takes: nothing is archived or compressed in here
            for database in databases:
                source = sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=30.0)
                target = sqlite3.connect(str(work / Path(database).name))
                try:
                    source.backup(target)
                finally:
                    source.close()
                    target.close()
            for name, source_dir, database, sql in linked:
                (work / name).mkdir(exist_ok=True)
                snapshot = sqlite3.connect(f'file:{work / Path(database).name}?mode=ro', uri=True)
                try:
                    for (file,) in snapshot.execute(sql):
                        if file:
                            _link(Path(source_dir) / file, work / name / file)
                finally:
                    snapshot.close()
            for file in copied:
                if Path(file).is_file():
                    shutil.copy2(file, work / Path(file).name)

        manifest = {'id': backup_id, 'created': created.strftime('%Y-%m-%d %H:%M:%S'), 'databases': {}, 'files': {},
                    'missing': [], 'copied': [Path(file).name for file in copied if (work / Path(file).name).is_file()]}
        for database in databases:
            snapshot = sqlite3.connect(f'file:{work / Path(database).name}?mode=ro', uri=True)
            try:
                if snapshot.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise BackupError(f'The snapshot of {Path(database).name} cannot be read back')
                tables = [row[0] for row in snapshot.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                manifest['databases'][Path(database).name] = {
                    table: snapshot.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
            finally:
                snapshot.close()
        for name, _source_dir, database, sql in linked:
            snapshot = sqlite3.connect(f'file:{work / Path(database).name}?mode=ro', uri=True)
            try:
                named = {file for (file,) in snapshot.execute(sql) if file}
            finally:
                snapshot.close()
            manifest['files'][name] = manifest['files'].get(name, 0) + len(named)
            manifest['missing'] += sorted(f'{name}/{file}' for file in named if not (work / name / file).is_file())
        manifest['verified'] = not manifest['missing']
        (work / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        os.replace(work, folder)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    logger.info(f"Full backup {backup_id}: {len(databases)} database files, "
                f"{sum(manifest['files'].values())} pictures"
                + (f", {len(manifest['missing'])} pictures missing" if manifest['missing'] else ''))
    for old in list_full()[max(1, keep):]:
        shutil.rmtree(FULL_DIR / old['id'], ignore_errors=True)
    return {**manifest, 'folder': str(folder)}


def list_full():
    """The full backups' manifests, newest first"""
    found = []
    if FULL_DIR.is_dir():
        for folder in sorted(FULL_DIR.iterdir(), reverse=True):
            if FULL_ID.match(folder.name) and (folder / 'manifest.json').is_file():
                try:
                    found.append(json.loads((folder / 'manifest.json').read_text()))
                except ValueError:
                    continue
    return found


def delete(backup_id):
    shutil.rmtree(_folder(backup_id))
    logger.info(f"Backup {backup_id} deleted")
