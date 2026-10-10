"""
Decks: named lists of cards (by card name, with a count and a board) built on the collection
page. A deck is only a list - the copies owned are counted from the inventory when it is shown,
and editing a deck never changes the inventory.

    decks = DeckManager()
    deck_id = decks.create('mtg', 'Elves', 'commander')
    decks.add_card(deck_id, 'Llanowar Elves', 'main', 1)
"""
import contextlib
import sqlite3
import threading

import storage
from config import Config
from database import search_key
from inventory import now

BOARDS = ('commander', 'main', 'side')
# card_id: the printing shown (image); the card itself is identified by its name. A deck's
# cards go with the deck (ON DELETE CASCADE, on connections from storage.connect)
DECK_CARDS_TABLE = '''
    CREATE TABLE {table} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
        card_name TEXT NOT NULL,
        card_id TEXT,
        quantity INTEGER NOT NULL DEFAULT 1 CHECK (typeof(quantity) = 'integer' AND quantity > 0),
        board TEXT NOT NULL DEFAULT 'main' CHECK (board IN ('commander', 'main', 'side')),
        UNIQUE(deck_id, card_name, board)
    )
'''
# No index of its own on deck_id: UNIQUE(deck_id, card_name, board) starts with it and answers
# "the cards of a deck" as fast (measured 2026-10-09, scripts/benchmark_db.py: 0.05 ms / 0.08 ms)
DECK_CARDS_INDEXES = ()
CONSTRAINTS_MIGRATION = 'deck_constraints_1'


class DeckManager:
    def __init__(self, db_file=None):
        self._lock = threading.RLock()
        self._writes = 0  # _writing blocks open (they nest)
        self.db_file = db_file or Config.DATABASE_FILE
        self.conn = storage.connect(self.db_file)
        with self._lock:
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS decks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game TEXT NOT NULL,
                    name TEXT NOT NULL,
                    format TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )''')
            if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'deck_cards'").fetchone():
                self.conn.execute(DECK_CARDS_TABLE.format(table='deck_cards'))
                storage.record(self.conn, CONSTRAINTS_MIGRATION, 'new file')
            self.conn.execute('DROP INDEX IF EXISTS idx_deck_cards_deck')
            self.conn.commit()
            # Files from before the constraints: rebuilt with them, once. Left out are only cards
            # of decks that are gone and rows without copies or on a board there is not
            valid = ("deck_id IN (SELECT id FROM decks) AND CAST(quantity AS INTEGER) > 0 "
                     "AND board IN ('commander', 'main', 'side')")
            storage.rebuild(self.db_file, CONSTRAINTS_MIGRATION, [
                ('deck_cards', DECK_CARDS_TABLE,
                 f'SELECT id, deck_id, card_name, card_id, CAST(quantity AS INTEGER), board FROM deck_cards WHERE {valid}',
                 DECK_CARDS_INDEXES)],
                lambda conn: tuple(conn.execute(
                    f'SELECT COUNT(*), COALESCE(SUM(CAST(quantity AS INTEGER)), 0), COALESCE(MAX(id), 0) '
                    f'FROM deck_cards WHERE {valid}').fetchone()))

    @contextlib.contextmanager
    def _writing(self):
        """
        One change, whole or not at all: committed when the outermost block ends, rolled back
        when anything in it fails - a change that failed half way used to stay in the
        connection and went in with the next commit of something else.
        """
        with self._lock:
            self._writes += 1
            try:
                yield
                if self._writes == 1:
                    self.conn.commit()
            except BaseException:
                if self._writes == 1:
                    self.conn.rollback()
                raise
            finally:
                self._writes -= 1

    def _require(self, deck_id):
        if not self.conn.execute('SELECT 1 FROM decks WHERE id = ?', (deck_id,)).fetchone():
            raise ValueError(f"Unknown deck: {deck_id}")

    # -- Decks ---------------------------------------------------------------

    def list_decks(self, game):
        """A game's decks, most recently changed first, with their card counts"""
        with self._lock:
            rows = self.conn.execute('''
                SELECT d.*, COALESCE(SUM(CASE WHEN c.board != 'side' THEN c.quantity END), 0) AS cards,
                       COALESCE(SUM(CASE WHEN c.board = 'side' THEN c.quantity END), 0) AS side_cards
                FROM decks d LEFT JOIN deck_cards c ON c.deck_id = d.id
                WHERE d.game = ? GROUP BY d.id ORDER BY d.updated_at DESC, d.id DESC''', (game,)).fetchall()
            commanders = {}
            for row in self.conn.execute('''
                    SELECT c.deck_id, c.card_name FROM deck_cards c JOIN decks d ON d.id = c.deck_id
                    WHERE d.game = ? AND c.board = 'commander' ORDER BY c.id''', (game,)):
                commanders.setdefault(row['deck_id'], []).append(row['card_name'])
        return [{**dict(row), 'commanders': commanders.get(row['id'], [])} for row in rows]

    def get(self, deck_id):
        """The deck with its 'cards' ([{'name', 'card_id', 'quantity', 'board'}]), or None"""
        with self._lock:
            deck = self.conn.execute('SELECT * FROM decks WHERE id = ?', (deck_id,)).fetchone()
            if not deck:
                return None
            cards = self.conn.execute(
                'SELECT * FROM deck_cards WHERE deck_id = ? ORDER BY card_name COLLATE NOCASE', (deck_id,)).fetchall()
        return {**dict(deck), 'cards': [{'name': row['card_name'], 'card_id': row['card_id'],
                                         'quantity': row['quantity'], 'board': row['board']} for row in cards]}

    def create(self, game, name, deck_format, notes=''):
        with self._writing():
            deck_id = self.conn.execute(
                'INSERT INTO decks (game, name, format, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
                (game, name.strip() or 'New deck', deck_format, notes or '', now(), now())).lastrowid
        return deck_id

    def update(self, deck_id, name=None, deck_format=None, notes=None):
        with self._writing():
            deck = self.conn.execute('SELECT * FROM decks WHERE id = ?', (deck_id,)).fetchone()
            if not deck:
                return False
            self.conn.execute('UPDATE decks SET name = ?, format = ?, notes = ?, updated_at = ? WHERE id = ?', (
                (name.strip() if name is not None else '') or deck['name'], deck_format or deck['format'],
                notes if notes is not None else deck['notes'], now(), deck_id))
        return True

    def delete(self, deck_id):
        with self._writing():
            self.conn.execute('DELETE FROM deck_cards WHERE deck_id = ?', (deck_id,))
            deleted = self.conn.execute('DELETE FROM decks WHERE id = ?', (deck_id,)).rowcount
        return bool(deleted)

    def duplicate(self, deck_id):
        with self._writing():  # the copy with its cards, or no copy
            deck = self.get(deck_id)
            if not deck:
                return None
            new_id = self.create(deck['game'], f"{deck['name']} (copy)", deck['format'], deck['notes'])
            self.import_cards(new_id, deck['cards'])
        return new_id

    # -- Cards ---------------------------------------------------------------

    def _touch(self, deck_id):
        self.conn.execute('UPDATE decks SET updated_at = ? WHERE id = ?', (now(), deck_id))

    def _quantity(self, deck_id, name, board):
        row = self.conn.execute('SELECT quantity FROM deck_cards WHERE deck_id = ? AND card_name = ? AND board = ?',
                                (deck_id, name, board)).fetchone()
        return row['quantity'] if row else 0

    def set_card(self, deck_id, name, board, quantity, card_id=None):
        """Set how many copies of a card a board has (0 removes it)"""
        if board not in BOARDS:
            raise ValueError(f"Unknown board: {board}")
        with self._writing():
            self._require(deck_id)
            if quantity <= 0:
                self.conn.execute('DELETE FROM deck_cards WHERE deck_id = ? AND card_name = ? AND board = ?',
                                  (deck_id, name, board))
            else:
                self.conn.execute('''
                    INSERT INTO deck_cards (deck_id, card_name, card_id, quantity, board) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(deck_id, card_name, board) DO UPDATE SET
                        quantity = excluded.quantity, card_id = COALESCE(excluded.card_id, card_id)''',
                                  (deck_id, name, card_id, int(quantity), board))
            self._touch(deck_id)

    def set_printing(self, deck_id, name, board, card_id):
        """Choose the printing an entry is shown in"""
        with self._writing():
            self.conn.execute('UPDATE deck_cards SET card_id = ? WHERE deck_id = ? AND card_name = ? AND board = ?',
                              (card_id, deck_id, name, board))
            self._touch(deck_id)

    def add_card(self, deck_id, name, board, change=1, card_id=None):
        """Add (or, with a negative change, take out) copies; returns the new count"""
        with self._writing():
            quantity = max(0, self._quantity(deck_id, name, board) + int(change))
            self.set_card(deck_id, name, board, quantity, card_id)
        return quantity

    def move_card(self, deck_id, name, from_board, to_board):
        """Move every copy of a card to another board (added to the copies already there) -
        in one commit: there and gone from here, or neither"""
        with self._writing():
            quantity = self._quantity(deck_id, name, from_board)
            if not quantity or from_board == to_board:
                return
            # The printing shown comes along - unless the card is on the other board already,
            # which keeps the printing it shows there
            there = self._quantity(deck_id, name, to_board)
            printing = None if there else self.conn.execute(
                'SELECT card_id FROM deck_cards WHERE deck_id = ? AND card_name = ? AND board = ?',
                (deck_id, name, from_board)).fetchone()['card_id']
            self.set_card(deck_id, name, to_board, there + quantity, printing)
            self.set_card(deck_id, name, from_board, 0)

    def import_cards(self, deck_id, entries, replace=False):
        """Add entries ([{'name', 'quantity', 'board', 'card_id'}]) to a deck; returns how many cards"""
        # Read in full before anything is changed: a bad entry must not leave a deck half replaced.
        # (An entry without a quantity is one copy, one without a known board goes to 'main'.)
        rows = []
        for entry in entries:
            name = str(entry.get('name') or '').strip()
            if not name:
                raise ValueError('A card without a name')
            rows.append((deck_id, name, entry.get('card_id'), max(1, int(entry.get('quantity') or 1)),
                         entry.get('board') if entry.get('board') in BOARDS else 'main'))
        if replace and not rows:
            raise ValueError('No cards to replace the deck with')  # emptying a deck is not an import
        added = 0
        with self._writing():
            self._require(deck_id)
            if replace:
                self.conn.execute('DELETE FROM deck_cards WHERE deck_id = ?', (deck_id,))
            for row in rows:
                quantity = row[3]
                self.conn.execute('''
                    INSERT INTO deck_cards (deck_id, card_name, card_id, quantity, board) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(deck_id, card_name, board) DO UPDATE SET quantity = quantity + excluded.quantity''',
                                  row)
                added += quantity
            self._touch(deck_id)
        return added

    def needed_by_name(self, game, commander_formats=()):
        """
        {search_key(card name): [{'deck_id', 'deck', 'quantity'}]} over every deck of a game -
        which decks want a card, to tell when two decks need the same copy. The 'side' board of
        a deck in commander_formats is a list of cards being considered, not counted.
        """
        needed = {}
        with self._lock:
            rows = self.conn.execute('''
                SELECT d.id, d.name, d.format, c.card_name, c.board, c.quantity
                FROM deck_cards c JOIN decks d ON d.id = c.deck_id WHERE d.game = ?''', (game,)).fetchall()
        for row in rows:
            if row['board'] == 'side' and row['format'] in commander_formats:
                continue
            decks = needed.setdefault(search_key(row['card_name']), [])
            existing = next((deck for deck in decks if deck['deck_id'] == row['id']), None)
            if existing:
                existing['quantity'] += row['quantity']
            else:
                decks.append({'deck_id': row['id'], 'deck': row['name'], 'quantity': row['quantity']})
        return needed
