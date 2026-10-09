# ============================================================================
# FILE: backups.py
# Backups of what the user made - the collection, the scanned cards and the
# decks - taken from the collection page (before a big load) and restored there
# ============================================================================
import json
import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime

from config import Config
from inventory import CAPTURES_DIR

logger = logging.getLogger(__name__)

# Made by the user (kept until deleted), when the app starts (create_daily) and before a restore.
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
KEEP_AUTOMATIC = 5  # backups made before a restore; the ones the user makes are kept until deleted
KEEP_DAILY = 7      # backups made when the app starts (one per day, see create_daily)


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


def create(inventory, scan_inventory, deck_store, note='', automatic=False, daily=False, keep=None):
    """
    Back up the collection, the scanned cards and the decks as they are now; returns its info.
    keep: the id of a backup that must not be cleared away to make room for this one (the one
    about to be restored - it may be the oldest automatic backup itself).
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
    for kind, limit in (('daily', KEEP_DAILY if daily else None), ('automatic', KEEP_AUTOMATIC if automatic else None)):
        if limit:
            for old in [item for item in list_backups() if item.get(kind)][limit:]:
                if old['id'] != keep:
                    delete(old['id'])
    return info


def create_daily(inventory, scan_inventory, deck_store):
    """
    The backup made when the app starts: one per day - a later start the same day finds it and
    makes none, as does a start with nothing to keep. Returns its info, or None.
    """
    today = datetime.now().strftime('%Y-%m-%d')
    if any(item.get('daily') and item['created'].startswith(today) for item in list_backups()):
        return None
    made = create(inventory, scan_inventory, deck_store, note='Application start', daily=True)
    if not (made['entries'] or made['scanned'] or made['decks']):
        delete(made['id'])
        return None
    return made


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
                            (tuple(row) for row in source.execute(f"SELECT {', '.join(columns)} FROM {prefix}{table}")))
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


def delete(backup_id):
    shutil.rmtree(_folder(backup_id))
    logger.info(f"Backup {backup_id} deleted")
