#!/usr/bin/env python3
"""
Captures waiting to be read (in the OCR / AI queues of identification.py), kept on disk so a
restart doesn't lose them: each is recorded when it is queued and removed when it is settled
(added or in the review queue). What is still here at startup is queued again (app.py:
resume_pending). The images themselves are the files in scanned_cards/.

A capture must count once, also across a crash or a restart:
  - each has a `uid`; whatever its result changes (the scanned cards, the review queue) notes
    that uid in the same commit (APPLIED_CAPTURES_TABLE, in that database), so a capture read
    again after a crash between that commit and its removal here changes nothing twice;
  - what became of an uploaded capture (`capture_id`: the station's own id for it) is kept
    here (settled_captures), in the commit that removes it from the queue - a station that
    sends it again after a restart gets that answer instead of a second card;
  - a capture whose result could not be applied stays on record and is tried again at the
    next starts (MAX_ATTEMPTS), instead of being dropped at the first error.

data/pending_captures.db - its own small file: it has nothing to do with the collection.
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta

import storage
from config import Config

PENDING_FILE = Config.DATA_DIR / 'pending_captures.db'
MAX_ATTEMPTS = 3   # a capture that failed this often is settled as an error
KEEP_DAYS = 30     # how long outcomes (here) and applied uids (beside the cards) are kept

# In the databases a capture's result is written to (inventory.py, review.py): the uids of the
# captures already applied there. `target`: what it became ('inventory:<id>', 'review:<id>')
APPLIED_CAPTURES_TABLE = ('CREATE TABLE IF NOT EXISTS applied_captures '
                          '(key TEXT PRIMARY KEY, target TEXT, applied_at TEXT NOT NULL)')


def timestamp(days_ago=0):
    return (datetime.now() - timedelta(days=days_ago)).strftime('%Y-%m-%d %H:%M:%S')


def prune_applied(conn):
    """Forget applied uids older than KEEP_DAYS (their captures left the queue long ago)"""
    conn.execute('DELETE FROM applied_captures WHERE applied_at < ?', (timestamp(KEEP_DAYS),))


class PendingCaptures:
    def __init__(self, db_file=None):
        self.conn = storage.connect(db_file or PENDING_FILE)
        self._lock = threading.RLock()
        with self._lock:
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS pending_captures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game TEXT NOT NULL,         -- the game being scanned when it was captured
                    station TEXT,               -- stations.py id (NULL: the scanner page's camera)
                    number INTEGER,             -- the capture's number (per station)
                    image TEXT NOT NULL,        -- file name in scanned_cards/
                    foil_image TEXT,            -- file of the perspective-corrected card, if another one
                    foil_is_image INTEGER,      -- 1: the card image is the perspective-corrected card
                    captured_at TEXT NOT NULL,
                    capture_id TEXT,            -- the sender's own id for it: a repeat is not a new capture
                    sender TEXT                 -- the station that uploaded it (also when station is NULL)
                )''')
            columns = {row['name'] for row in self.conn.execute('PRAGMA table_info(pending_captures)')}
            for column, kind in (('capture_id', 'TEXT'), ('sender', 'TEXT'), ('uid', 'TEXT'),
                                 ('attempts', 'INTEGER NOT NULL DEFAULT 0'), ('last_error', 'TEXT')):
                if column not in columns:
                    self.conn.execute(f'ALTER TABLE pending_captures ADD COLUMN {column} {kind}')
            # Queued by a version without uids: they get one now, and keep it from here on
            for row in self.conn.execute('SELECT id FROM pending_captures WHERE uid IS NULL').fetchall():
                self.conn.execute('UPDATE pending_captures SET uid = ? WHERE id = ?', (uuid.uuid4().hex, row['id']))
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS settled_captures (
                    capture_id TEXT PRIMARY KEY,   -- the sender's own id for the capture
                    sender TEXT,
                    number INTEGER,
                    outcome TEXT NOT NULL,         -- JSON: what became of it (route_identified's outcome)
                    settled_at TEXT NOT NULL
                )''')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_settled_sender ON settled_captures(sender, number)')
            # One record per capture a sender names: the database refuses a second one, whatever
            # the code that asks first (app.py: capture_accept_lock) does
            try:
                self.conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_capture_id '
                                  'ON pending_captures(COALESCE(sender, station, \'\'), capture_id) WHERE capture_id IS NOT NULL')
            except sqlite3.IntegrityError:  # two records of one capture from before: they are read, then it holds
                pass
            self.conn.execute('DELETE FROM settled_captures WHERE settled_at < ?', (timestamp(KEEP_DAYS),))
            self.conn.commit()

    def add(self, game, station, number, image, foil_image, foil_is_image, captured_at, capture_id=None, sender=None):
        """Put a capture on record; returns (its row id, its uid). A capture its sender named
        before (capture_id) is not recorded twice: the record there is returns its id and uid."""
        uid = uuid.uuid4().hex
        with self._lock:
            try:
                pending_id = self.conn.execute(
                    'INSERT INTO pending_captures (game, station, number, image, foil_image, foil_is_image, captured_at, '
                    'capture_id, sender, uid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (game, station, number, image, foil_image, int(bool(foil_is_image)), captured_at, capture_id,
                     sender, uid)).lastrowid
                self.conn.commit()
            except sqlite3.IntegrityError:
                self.conn.rollback()
                row = self.conn.execute(
                    "SELECT id, uid FROM pending_captures WHERE COALESCE(sender, station, '') = ? AND capture_id = ?",
                    (sender or station or '', capture_id)).fetchone()
                return row['id'], row['uid']
        return pending_id, uid

    def remove(self, pending_id):
        with self._lock:
            self.conn.execute('DELETE FROM pending_captures WHERE id = ?', (pending_id,))
            self.conn.commit()

    def settle(self, pending_id, outcome):
        """A capture's result is applied: off the queue, and - in the same commit - what became
        of it on record for the station that may ask or send it again"""
        with self._lock:
            try:
                row = self.conn.execute('SELECT capture_id, sender, station, number FROM pending_captures WHERE id = ?',
                                        (pending_id,)).fetchone()
                if row and row['capture_id']:
                    self.conn.execute('INSERT OR REPLACE INTO settled_captures VALUES (?, ?, ?, ?, ?)',
                                      (row['capture_id'], row['sender'] or row['station'], row['number'],
                                       json.dumps(outcome, default=str), timestamp()))
                self.conn.execute('DELETE FROM pending_captures WHERE id = ?', (pending_id,))
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def fail(self, pending_id, error, outcome):
        """
        A capture's result could not be applied. It stays on record to be tried again at the
        next start - unless it failed MAX_ATTEMPTS times: then it is settled with `outcome`
        (the error). Returns True when it is kept.
        """
        with self._lock:
            self.conn.execute('UPDATE pending_captures SET attempts = attempts + 1, last_error = ? WHERE id = ?',
                              (str(error)[:500], pending_id))
            self.conn.commit()
            row = self.conn.execute('SELECT attempts FROM pending_captures WHERE id = ?', (pending_id,)).fetchone()
            if row and row['attempts'] < MAX_ATTEMPTS:
                return True
            self.settle(pending_id, outcome)
            return False

    def settled(self, capture_id):
        """(sender, number, outcome) of a capture that was settled, by the sender's id for it - or None"""
        if not capture_id:
            return None
        with self._lock:
            row = self.conn.execute('SELECT sender, number, outcome FROM settled_captures WHERE capture_id = ?',
                                    (capture_id,)).fetchone()
        return (row['sender'], row['number'], json.loads(row['outcome'])) if row else None

    def outcome(self, sender, number):
        """What became of a sender's capture number, if it was settled with an id - or None"""
        with self._lock:
            row = self.conn.execute('SELECT outcome FROM settled_captures WHERE sender IS ? AND number = ? '
                                    'ORDER BY settled_at DESC LIMIT 1', (sender, number)).fetchone()
        return json.loads(row['outcome']) if row else None

    def has(self, sender, number):
        with self._lock:
            return self.conn.execute('SELECT 1 FROM pending_captures WHERE COALESCE(sender, station) IS ? AND number = ?',
                                     (sender, number)).fetchone() is not None

    def all(self):
        """Oldest first"""
        with self._lock:
            return [dict(row) for row in self.conn.execute('SELECT * FROM pending_captures ORDER BY id')]

    def count(self):
        with self._lock:
            return self.conn.execute('SELECT COUNT(*) FROM pending_captures').fetchone()[0]
