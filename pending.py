#!/usr/bin/env python3
"""
Captures waiting to be read (in the OCR / AI queues of identification.py), kept on disk so a
restart doesn't lose them: each is recorded when it is queued and removed when it is settled
(added or in the review queue). What is still here at startup is queued again (app.py:
resume_pending). The images themselves are the files in scanned_cards/.

data/pending_captures.db - its own small file: it has nothing to do with the collection.
"""

import sqlite3
import threading

from config import Config

PENDING_FILE = Config.DATA_DIR / 'pending_captures.db'


class PendingCaptures:
    def __init__(self, db_file=None):
        self.conn = sqlite3.connect(str(db_file or PENDING_FILE), check_same_thread=False, timeout=10.0)
        self.conn.row_factory = sqlite3.Row
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
                    captured_at TEXT NOT NULL
                )''')
            self.conn.commit()

    def add(self, game, station, number, image, foil_image, foil_is_image, captured_at):
        with self._lock:
            pending_id = self.conn.execute(
                'INSERT INTO pending_captures (game, station, number, image, foil_image, foil_is_image, captured_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (game, station, number, image, foil_image, int(bool(foil_is_image)), captured_at)).lastrowid
            self.conn.commit()
        return pending_id

    def remove(self, pending_id):
        with self._lock:
            self.conn.execute('DELETE FROM pending_captures WHERE id = ?', (pending_id,))
            self.conn.commit()

    def has(self, station, number):
        with self._lock:
            return self.conn.execute('SELECT 1 FROM pending_captures WHERE station IS ? AND number = ?',
                                     (station, number)).fetchone() is not None

    def all(self):
        """Oldest first"""
        with self._lock:
            return [dict(row) for row in self.conn.execute('SELECT * FROM pending_captures ORDER BY id')]

    def count(self):
        with self._lock:
            return self.conn.execute('SELECT COUNT(*) FROM pending_captures').fetchone()[0]
