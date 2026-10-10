# ============================================================================
# FILE: storage.py
# What every SQLite file of the server has in common: how a connection is set
# up, and how a change of schema is applied once (named migrations, with a
# backup before and a check after). Server only - the camera station has no
# database.
# ============================================================================
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger('database')

# Each change of an existing table is recorded here by name, in the file it changed. A table
# rather than PRAGMA user_version: the card database file is shared by several managers (cards,
# collection, decks, review queue), which must not each count their own version in one number
MIGRATIONS_TABLE = ('CREATE TABLE IF NOT EXISTS schema_migrations '
                    '(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL, note TEXT)')
MIGRATION_BACKUPS = Path('backups') / 'migrations'  # beside the database file that is migrated
_backed_up = {}  # {database file: its backup of this run} - one per file, however many migrations


def connect(path, timeout=10.0):
    """
    A connection as every manager uses it: usable from any thread (the managers lock), rows by
    column name, WAL (readers are not held up by a writer), and the foreign keys enforced -
    SQLite only does that on connections that ask for it. synchronous=NORMAL as before: with
    WAL a power cut can lose the last commits but not corrupt the file.
    """
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def applied(conn, name):
    conn.execute(MIGRATIONS_TABLE)
    return conn.execute('SELECT 1 FROM schema_migrations WHERE name = ?', (name,)).fetchone() is not None


def record(conn, name, note=''):
    """Note a migration as done - in the transaction that did it"""
    conn.execute(MIGRATIONS_TABLE)
    conn.execute('INSERT OR REPLACE INTO schema_migrations VALUES (?, ?, ?)',
                 (name, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), note))


def backup_file(path, label):
    """
    A copy of a database file before it is migrated (SQLite's backup: consistent, also while the
    file is in use), opened and checked - or the migration does not start. Returns the copy.
    """
    path = Path(path)
    if path in _backed_up:
        return _backed_up[path]
    folder = path.parent / MIGRATION_BACKUPS
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{path.stem}_before_{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    source = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=30.0)
    copy = sqlite3.connect(str(target))
    try:
        source.backup(copy)
        if copy.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError(f"The backup {target} cannot be read back")
    finally:
        source.close()
        copy.close()
    _backed_up[path] = target
    logger.info(f"{path.name} backed up to {target} before the migration {label}")
    return target


def rebuild(path, name, tables, facts):
    """
    Rebuild tables of a database file with a new definition (SQLite cannot add a constraint to
    a table), as one migration: a backup first, then one transaction - every table copied into
    its new form, the old one dropped, the new one renamed, ids and AUTOINCREMENT counters
    kept - checked (`facts` must say the same before and after, and no foreign key may be
    violated) and recorded under `name`. Anything else: rolled back, the file as it was.

    tables: [(table, "CREATE TABLE {table} (...)", "SELECT <columns> FROM <table> WHERE ...",
              [index statements])] - parents before the tables that refer to them;
    facts(conn): what must be the same afterwards (counts, totals) - of the rows the SELECTs keep.
    Returns True when it was applied now, False when it had been before.
    """
    check = sqlite3.connect(str(path), timeout=30.0)
    try:
        if applied(check, name):
            return False
        check.commit()
    finally:
        check.close()
    backup = backup_file(path, name)

    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30.0)
    try:
        conn.execute('PRAGMA foreign_keys=OFF')  # tables are dropped and renamed under the ones that refer to them
        conn.execute('BEGIN IMMEDIATE')
        if applied(conn, name):  # another process was faster
            conn.execute('ROLLBACK')
            return False
        before = facts(conn)
        notes = []
        for table, create, select, indexes in tables:
            rows = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            counter = conn.execute('SELECT seq FROM sqlite_sequence WHERE name = ?', (table,)).fetchone() \
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone() else None
            conn.execute(f'DROP TABLE IF EXISTS {table}_new')
            conn.execute(create.format(table=f'{table}_new'))
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info({table}_new)')]
            kept = conn.execute(f"INSERT INTO {table}_new ({', '.join(columns)}) {select}").rowcount
            conn.execute(f'DROP TABLE {table}')
            conn.execute(f'ALTER TABLE {table}_new RENAME TO {table}')
            if counter:  # ids of rows deleted long ago are not given out again
                conn.execute('DELETE FROM sqlite_sequence WHERE name = ?', (table,))
                conn.execute('INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)', (table, counter[0]))
            for index in indexes:
                conn.execute(index)
            if kept != rows:
                notes.append(f'{table}: {rows - kept} of {rows} rows left out')
        violations = conn.execute('PRAGMA foreign_key_check').fetchall()
        if violations:
            raise RuntimeError(f"{len(violations)} rows refer to rows that do not exist")
        after = facts(conn)
        if after != before:
            raise RuntimeError(f"the data changed ({before} -> {after})")
        record(conn, name, '; '.join(notes))
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        logger.exception(f"Migration {name} of {Path(path).name} failed - the file is unchanged (backup: {backup})")
        raise
    finally:
        conn.close()
    logger.info(f"Migration {name} of {Path(path).name} applied ({'; '.join(notes) or 'every row kept'}); "
                f"backup: {backup}")
    return True
