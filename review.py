"""
Review queue: captures that were not added automatically (printing not confirmed, name not
found, nothing read) while cards are added automatically. Scanning goes on; the queue is
worked through at the end, with the capture next to the suggested card and a manual search.

Items live in the card database file (table `review_queue`), per game, with a copy of the
capture in data/review/ (scanned_cards/ is cleaned after cleanup.days) until resolved.
"""
import logging
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime

from config import Config

logger = logging.getLogger('database')

REVIEW_DIR = Config.DATA_DIR / 'review'


class ReviewQueue:
    def __init__(self, db_file=None):
        self.conn = sqlite3.connect(str(db_file or Config.DATABASE_FILE), check_same_thread=False, timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS review_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game TEXT NOT NULL,
                    file TEXT,              -- copy of the capture in REVIEW_DIR
                    ai_name TEXT,           -- what the AI read
                    ai_number TEXT,
                    ai_set TEXT,
                    foil TEXT,              -- foil / non-foil / unknown (the star/dot marker)
                    card_id TEXT,           -- best match, if any
                    match TEXT,             -- how it was matched (Game.confirmed_matches)
                    created_at TEXT NOT NULL,
                    station TEXT            -- stations.py id the capture came from (NULL: the scanner page's camera)
                )''')
            if 'station' not in {row['name'] for row in self.conn.execute('PRAGMA table_info(review_queue)')}:
                self.conn.execute('ALTER TABLE review_queue ADD COLUMN station TEXT')
            self.conn.commit()

    def add(self, game, image_path, name='', number='', set_code='', foil='unknown', card=None, station=None):
        """Queue a capture; returns the item id"""
        file = None
        if image_path:
            try:
                REVIEW_DIR.mkdir(parents=True, exist_ok=True)
                file = f"{uuid.uuid4().hex}.jpg"
                shutil.copyfile(image_path, REVIEW_DIR / file)
            except OSError as e:
                logger.warning(f"Could not keep the capture for review: {e}")
                file = None
        with self._lock:
            item_id = self.conn.execute(
                'INSERT INTO review_queue (game, file, ai_name, ai_number, ai_set, foil, card_id, match, created_at, '
                'station) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (game, file, name or '', number or '', set_code or '', foil or 'unknown',
                 card['id'] if card else None, card.get('match') if card else None,
                 datetime.now().strftime('%Y-%m-%d %H:%M:%S'), station)).lastrowid
            self.conn.commit()
        return item_id

    def count(self, game):
        with self._lock:
            return self.conn.execute('SELECT COUNT(*) FROM review_queue WHERE game = ?', (game,)).fetchone()[0]

    def first(self, game):
        """The oldest item of a game, and its position info: (item dict or None, total)"""
        with self._lock:
            row = self.conn.execute('SELECT * FROM review_queue WHERE game = ? ORDER BY id LIMIT 1', (game,)).fetchone()
        return (dict(row) if row else None), self.count(game)

    def get(self, item_id):
        with self._lock:
            row = self.conn.execute('SELECT * FROM review_queue WHERE id = ?', (item_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def image_path(item):
        return REVIEW_DIR / item['file'] if item and item.get('file') else None

    def remove(self, item_id):
        """Resolve an item (added or skipped): delete it and its capture copy"""
        with self._lock:
            row = self.conn.execute('SELECT file FROM review_queue WHERE id = ?', (item_id,)).fetchone()
            if not row:
                return False
            self.conn.execute('DELETE FROM review_queue WHERE id = ?', (item_id,))
            self.conn.commit()
        if row['file']:
            (REVIEW_DIR / row['file']).unlink(missing_ok=True)
        return True
