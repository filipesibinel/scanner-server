"""
The cases of DATABASE_REVIEW.md (2026-10-09): transactions that are whole or not at all, reads
that do not write, station ownership that follows the quantity, captures that count once, and
the web cache's own bounded file. Everything runs on temporary databases and folders; no
application data is touched.

    venv/bin/python -m unittest discover tests
"""
import sqlite3
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backups  # noqa: E402
import inventory as inv  # noqa: E402
import pending as pending_store  # noqa: E402
import recommendations  # noqa: E402
import review as review_store  # noqa: E402
from database import CardDatabase  # noqa: E402
from test_ownership import CARD, GAME, OTHER, TemporaryData  # noqa: E402


class ReadsDoNotWrite(TemporaryData):
    def test_a_filtered_list_leaves_no_transaction_open(self):
        self.add('desk-a')
        self.scan.get_all_cards(GAME, 'desk-a')
        self.scan.get_stats(GAME, 'desk-a')
        self.assertFalse(self.scan.conn.in_transaction)
        other = sqlite3.connect(str(self.tmp / 'scan.db'), timeout=0.2)  # used to fail: database is locked
        other.execute('UPDATE inventory SET tags = ?', ('seen',))
        other.commit()
        other.close()

    def test_deleting_an_entry_clears_what_stood_beside_it(self):
        row_id = self.add('desk-a')
        self.scan.delete_card(row_id, quiet=True)
        count = lambda table: self.scan.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        self.assertEqual((count('inventory_sources'), count('inventory_captures')), (0, 0))
        self.assertEqual(list(inv.CAPTURES_DIR.glob('*.jpg')), [])


class StationsNeverClaimMoreThanThereIs(TemporaryData):
    def claims(self):
        """[(entry quantity, copies the stations claim)] - the second never above the first"""
        return self.scan.conn.execute('''
            SELECT i.quantity, COALESCE((SELECT SUM(s.quantity) FROM inventory_sources s WHERE s.inventory_id = i.id), 0)
            FROM inventory i''').fetchall()

    def assert_consistent(self):
        for quantity, claimed in self.claims():
            self.assertLessEqual(claimed, quantity)

    def test_lowering_the_quantity_takes_from_the_stations(self):
        self.add('desk-a', copies=2)
        row_id = self.add('desk-b', copies=2)
        self.scan.update_card(row_id, quantity=2, quiet=True)
        self.assert_consistent()
        # The newest copies go, like their captures: desk-b scanned last
        self.assertEqual((self.total('desk-a'), self.total('desk-b')), (2, 0))
        self.assertEqual(self.total(), 2)

    def test_copies_added_by_hand_go_before_a_stations(self):
        row_id = self.add('desk-a', copies=2)
        self.scan.add_card(CARD, GAME, 'regular', quantity=3, quiet=True)   # nobody's
        self.scan.update_card(row_id, quantity=3, quiet=True)
        self.assertEqual(self.total('desk-a'), 2)
        self.scan.update_card(row_id, quantity=1, quiet=True)
        self.assertEqual(self.total('desk-a'), 1)
        self.assert_consistent()

    def test_consistent_through_splits_merges_undo_and_moves(self):
        self.add('desk-a', copies=3)
        row_id = self.add('desk-b', copies=2)
        self.scan.update_card(row_id, location='Box', split_quantity=2, quiet=True)   # split
        self.assert_consistent()
        self.scan.update_card(row_id, quantity=2, quiet=True)                         # lowered
        self.assert_consistent()
        moved = self.scan.conn.execute("SELECT id FROM inventory WHERE location = 'Box'").fetchone()[0]
        self.scan.update_card(moved, location='', quantity=1, quiet=True)             # merged back, one fewer
        self.assert_consistent()
        self.add('desk-a', OTHER)
        self.scan.undo_last_add(source='desk-a')                                      # undo
        self.assert_consistent()
        self.collection.take_from(self.scan, GAME, station='desk-a')                  # one camera's cards move
        self.assert_consistent()
        self.assertEqual(self.total('desk-a'), 0)


class FailedWritesLeaveNothing(TemporaryData):
    def test_a_failed_change_is_not_committed_with_the_next_one(self):
        row_id = self.add('desk-a')

        def broken(*args, **kwargs):
            raise RuntimeError('disk trouble')
        trim, self.scan._trim_captures = self.scan._trim_captures, broken
        try:
            with self.assertRaises(RuntimeError):
                self.scan.update_card(row_id, tags='half done', quiet=True)
        finally:
            self.scan._trim_captures = trim
        self.assertFalse(self.scan.conn.in_transaction)
        self.scan.set_price(row_id, 2.0)                       # commits - and used to take the tags along
        self.assertEqual(self.scan.get_entry(row_id)['tags'], '')

    def test_thumbnails_go_only_when_the_delete_is_committed(self):
        row_id = self.add('desk-a')
        files = list(inv.CAPTURES_DIR.glob('*.jpg'))
        with self.scan._lock:
            self.scan._delete_entries('id = ?', (row_id,))
            self.assertTrue(files[0].exists())                 # not yet: it could still be rolled back
            self.scan._rollback()
        self.assertTrue(files[0].exists())
        self.assertEqual(self.total('desk-a'), 1)
        self.scan.delete_card(row_id, quiet=True)
        self.assertFalse(files[0].exists())

    def test_replacing_with_a_file_without_usable_rows_keeps_the_inventory(self):
        self.collection.add_card(CARD, GAME, 'regular', quiet=True)
        bad = self.tmp / 'bad.csv'
        bad.write_text('Card Name,Set,Finish\n,,\nSome Card,Some Set,hologram\n')
        result = self.collection.import_csv(bad, GAME, ['regular', 'foil'], replace_existing=True)
        self.assertFalse(result['success'])
        self.assertEqual(self.collection.get_stats(GAME)['total_cards'], 1)
        self.assertFalse(self.collection.import_entries([], GAME, replace_existing=True)['success'])
        self.assertEqual(self.collection.get_stats(GAME)['total_cards'], 1)
        good = self.tmp / 'good.csv'
        good.write_text('Card Name,Set,Finish,Quantity\nSome Card,Some Set,foil,3\n')
        self.assertTrue(self.collection.import_csv(good, GAME, ['regular', 'foil'], replace_existing=True)['success'])
        self.assertEqual(self.collection.get_stats(GAME)['total_cards'], 3)


class CsvImport(TemporaryData):
    FINISHES = ['regular', 'foil']

    def file(self, text):
        path = self.tmp / 'import.csv'
        path.write_text(text)
        return path

    def test_headers_in_any_case(self):
        result = self.collection.import_csv(self.file(' card name ,SET,quantity,FINISH\nSome Card,Some Set,2,Foil\n'),
                                            GAME, self.FINISHES)
        self.assertEqual((result['added'], result['skipped']), (1, 0))
        card, = self.collection.get_all_cards(GAME)
        self.assertEqual((card['name'], card['quantity'], card['finish']), ('Some Card', 2, 'foil'))

    def test_a_refused_replacement_keeps_cards_captures_and_cameras(self):
        self.add('desk-a', copies=2)
        one_bad_row = self.file('Card Name,Set,Finish\nGood Card,Some Set,foil\nBad Card,Some Set,hologram\n')
        result = self.scan.import_csv(one_bad_row, GAME, self.FINISHES, replace_existing=True)
        self.assertFalse(result['success'])
        self.assertIn('1 row', result['error'])
        self.assertEqual((self.total(), self.total('desk-a')), (2, 2))
        self.assertEqual(len(list(inv.CAPTURES_DIR.glob('*.jpg'))), 2)
        # Added instead of replacing, the row that cannot be used is only left out
        result = self.scan.import_csv(one_bad_row, GAME, self.FINISHES)
        self.assertEqual((result['success'], result['added'], result['errors']), (True, 1, 1))
        self.assertEqual(self.total(), 3)


class OldOverclaims(TemporaryData):
    def test_repaired_at_the_next_start(self):
        self.add('desk-a', copies=2)
        row_id = self.add('desk-b', copies=2)
        self.scan.conn.execute('UPDATE inventory SET quantity = 2 WHERE id = ?', (row_id,))   # as an older version left it
        self.scan.conn.commit()
        self.scan.conn.close()
        self.scan = inv.InventoryManager(db_file=self.tmp / 'scan.db')
        claimed = self.scan.conn.execute('SELECT SUM(quantity) FROM inventory_sources').fetchone()[0]
        self.assertEqual((claimed, self.total()), (2, 2))


class DeckChangesAreWhole(TemporaryData):
    def setUp(self):
        super().setUp()
        self.deck = self.decks.create(GAME, 'Test', 'commander')
        self.decks.set_card(self.deck, 'Original', 'main', 1)

    def names(self, board=None):
        return sorted(card['name'] for card in self.decks.get(self.deck)['cards'] if board in (None, card['board']))

    def test_a_failed_import_replaces_nothing(self):
        with self.assertRaises(ValueError):
            self.decks.import_cards(self.deck, [{'name': 'New', 'quantity': 1}, {'name': 'Bad', 'quantity': 'many'}],
                                    replace=True)
        self.decks.update(self.deck, notes='later')            # used to commit the half-done replacement
        self.assertEqual(self.names(), ['Original'])

    def test_a_failure_inside_an_import_is_rolled_back(self):
        touch, self.decks._touch = self.decks._touch, lambda deck_id: 1 / 0
        try:
            with self.assertRaises(ZeroDivisionError):
                self.decks.import_cards(self.deck, [{'name': 'New', 'quantity': 1}], replace=True)
        finally:
            self.decks._touch = touch
        self.assertFalse(self.decks.conn.in_transaction)
        self.decks.update(self.deck, notes='later')
        self.assertEqual(self.names(), ['Original'])

    def test_a_move_is_there_and_gone_or_neither(self):
        calls = []
        set_card = self.decks.set_card

        def fails_on_removal(deck_id, name, board, quantity, card_id=None):
            calls.append(board)
            if len(calls) == 2:
                raise RuntimeError('before the source is removed')
            return set_card(deck_id, name, board, quantity, card_id)
        self.decks.set_card = fails_on_removal
        try:
            with self.assertRaises(RuntimeError):
                self.decks.move_card(self.deck, 'Original', 'main', 'side')
        finally:
            del self.decks.set_card
        self.assertEqual((self.names('main'), self.names('side')), (['Original'], []))
        self.decks.move_card(self.deck, 'Original', 'main', 'side')
        self.assertEqual((self.names('main'), self.names('side')), ([], ['Original']))

    def test_replacing_with_nothing_is_refused(self):
        for entries in ([], [{'name': '  ', 'quantity': 1}]):
            with self.assertRaises(ValueError):
                self.decks.import_cards(self.deck, entries, replace=True)
        self.assertEqual(self.names(), ['Original'])

    def test_a_move_takes_the_printing_along(self):
        self.decks.set_printing(self.deck, 'Original', 'main', 'printing-a')
        self.decks.move_card(self.deck, 'Original', 'main', 'side')
        self.assertEqual([card['card_id'] for card in self.decks.get(self.deck)['cards']], ['printing-a'])
        self.decks.set_card(self.deck, 'Original', 'main', 1, 'printing-b')
        self.decks.move_card(self.deck, 'Original', 'main', 'side')           # the side board's printing stays
        card, = self.decks.get(self.deck)['cards']
        self.assertEqual((card['quantity'], card['card_id']), (2, 'printing-a'))

    def test_no_cards_for_a_deck_that_is_not_there(self):
        with self.assertRaises(ValueError):
            self.decks.set_card(999, 'Ghost', 'main', 1)
        with self.assertRaises(ValueError):
            self.decks.import_cards(999, [{'name': 'Ghost'}])
        self.assertEqual(self.decks.conn.execute('SELECT COUNT(*) FROM deck_cards WHERE deck_id = 999').fetchone()[0], 0)


class CatalogSwap(TemporaryData):
    def setUp(self):
        super().setUp()
        self.cards = CardDatabase(db_file=self.tmp / 'cards.db')
        self.cards.conn.execute("INSERT INTO cards (id, name) VALUES ('1', 'Kept Card')")
        self.cards.conn.commit()

    def tearDown(self):
        self.cards.conn.close()
        super().tearDown()

    def kept(self):
        return [row[0] for row in self.cards.conn.execute('SELECT name FROM cards')]

    def stage(self, rows):
        cursor = self.cards.conn.cursor()
        cursor.execute('DROP TABLE IF EXISTS cards_import')
        self.cards._create_cards_table(cursor, table='cards_import')
        cursor.executemany('INSERT INTO cards_import (id, name) VALUES (?, ?)', rows)
        self.cards.conn.commit()

    def test_a_missing_or_empty_import_leaves_the_cards(self):
        with self.assertRaises(RuntimeError):                  # used to drop `cards` and then fail
            self.cards.replace_table('cards_import', 'cards')
        self.assertEqual(self.kept(), ['Kept Card'])
        self.stage([])
        with self.assertRaises(RuntimeError):
            self.cards.replace_table('cards_import', 'cards')
        self.assertEqual(self.kept(), ['Kept Card'])

    def test_a_failure_while_swapping_is_rolled_back(self):
        self.stage([('2', 'New Card')])

        def broken(cursor):
            raise RuntimeError('index trouble')
        with self.assertRaises(RuntimeError):
            self.cards.replace_table('cards_import', 'cards', broken)
        self.assertEqual(self.kept(), ['Kept Card'])
        self.cards.replace_table('cards_import', 'cards', self.cards._create_card_indexes)
        self.assertEqual(self.kept(), ['New Card'])


    def test_a_much_smaller_import_is_refused(self):
        self.cards.conn.executemany('INSERT INTO cards (id, name) VALUES (?, ?)', [(f'k{n}', f'Card {n}') for n in range(9)])
        self.cards.conn.commit()
        self.stage([('2', 'New Card')])                        # 1 card where 10 are in use: a download cut short
        with self.assertRaises(RuntimeError):
            self.cards.replace_table('cards_import', 'cards')
        self.assertEqual(len(self.kept()), 10)

    def test_the_cards_and_what_is_known_about_them_change_together(self):
        self.cards.set_data_info('mtg', 'old', 1)
        self.stage([('2', 'New Card')])

        def broken(cursor):
            raise RuntimeError('index trouble')
        with self.assertRaises(RuntimeError):
            self.cards.replace_table('cards_import', 'cards', broken, info=('mtg', 'new', 1))
        self.assertEqual(self.cards.get_data_info('mtg')['source_updated'], 'old')
        self.cards.replace_table('cards_import', 'cards', info=('mtg', 'new', 1))
        self.assertEqual((self.kept(), self.cards.get_data_info('mtg')['source_updated']), (['New Card'], 'new'))

    def test_only_card_tables(self):
        with self.assertRaises(ValueError):
            self.cards.replace_table('cards_import', 'inventory')


class CapturesCountOnce(TemporaryData):
    def setUp(self):
        super().setUp()
        self.pending = pending_store.PendingCaptures(db_file=self.tmp / 'pending.db')
        self.saved_review = review_store.REVIEW_DIR
        review_store.REVIEW_DIR = self.tmp / 'review'
        self.review = review_store.ReviewQueue(db_file=self.tmp / 'collection.db')

    def tearDown(self):
        self.pending.conn.close()
        self.review.conn.close()
        review_store.REVIEW_DIR = self.saved_review
        super().tearDown()

    def queue(self, capture_id='cap-1'):
        return self.pending.add(GAME, 'desk-a', 7, 'a.jpg', None, True, '2026-10-09 12:00:00',
                                capture_id=capture_id, sender='desk-a')

    def test_a_capture_read_again_is_not_added_twice(self):
        _, uid = self.queue()
        first = self.scan.add_card(CARD, GAME, 'regular', capture=self.photo, source='desk-a', quiet=True, capture_key=uid)
        # ... a crash before the capture left the queue: at the next start it is read again
        again = self.scan.add_card(CARD, GAME, 'regular', capture=self.photo, source='desk-a', quiet=True, capture_key=uid)
        self.assertEqual(first, again)
        self.assertEqual((self.total(), self.total('desk-a')), (1, 1))
        self.assertEqual(len(list(inv.CAPTURES_DIR.glob('*.jpg'))), 1)
        _, other = self.queue('cap-2')
        self.scan.add_card(CARD, GAME, 'regular', source='desk-a', quiet=True, capture_key=other)
        self.assertEqual(self.total(), 2)                       # another capture of the same card counts

    def test_a_failed_add_notes_nothing(self):
        _, uid = self.queue()
        with self.assertRaises(KeyError):
            self.scan.add_card({'set_name': 'no name'}, GAME, 'regular', quiet=True, capture_key=uid)
        self.scan.add_card(CARD, GAME, 'regular', quiet=True, capture_key=uid)
        self.assertEqual(self.total(), 1)

    def test_a_capture_is_queued_for_review_once(self):
        _, uid = self.queue()
        self.assertIsNotNone(self.review.add(GAME, self.photo, 'Unknown', station='desk-a', capture_key=uid))
        self.assertIsNone(self.review.add(GAME, self.photo, 'Unknown', station='desk-a', capture_key=uid))
        self.assertEqual(self.review.count(GAME), 1)
        self.assertEqual(len(list(review_store.REVIEW_DIR.glob('*.jpg'))), 1)

    def test_the_outcome_survives_a_restart(self):
        pending_id, _ = self.queue()
        self.assertTrue(self.pending.has('desk-a', 7))
        self.pending.settle(pending_id, {'status': 'added', 'card': {'name': 'Test Card'}})
        self.pending.conn.close()
        self.pending = pending_store.PendingCaptures(db_file=self.tmp / 'pending.db')   # the next start
        self.assertFalse(self.pending.has('desk-a', 7))
        self.assertEqual(self.pending.settled('cap-1'), ('desk-a', 7, {'status': 'added', 'card': {'name': 'Test Card'}}))
        self.assertEqual(self.pending.outcome('desk-a', 7)['status'], 'added')
        self.assertIsNone(self.pending.settled('never-sent'))
        self.assertIsNone(self.pending.outcome('desk-a', 8))

    def test_a_failed_capture_is_kept_a_few_times_then_settled(self):
        pending_id, uid = self.queue()
        error = {'status': 'error', 'message': 'database is locked'}
        for _ in range(pending_store.MAX_ATTEMPTS - 1):
            self.assertTrue(self.pending.fail(pending_id, 'database is locked', error))
            self.assertEqual([row['uid'] for row in self.pending.all()], [uid])      # still to be read again
        self.assertFalse(self.pending.fail(pending_id, 'database is locked', error))
        self.assertEqual(self.pending.all(), [])
        self.assertEqual(self.pending.settled('cap-1')[2], error)

    def test_captures_queued_before_uids_get_one(self):
        self.pending.conn.execute("INSERT INTO pending_captures (game, number, image, captured_at) VALUES ('mtg', 1, 'old.jpg', 'then')")
        self.pending.conn.commit()
        self.pending.conn.close()
        self.pending = pending_store.PendingCaptures(db_file=self.tmp / 'pending.db')
        self.assertTrue(all(row['uid'] for row in self.pending.all()))


class WebCache(TemporaryData):
    def cache(self, **kwargs):
        return recommendations.Recommendations(db_file=self.tmp / 'web_cache.db', old_file=self.tmp / 'collection.db', **kwargs)

    def test_answers_come_back_and_expire(self):
        cache = self.cache()
        cache._store('https://example.org/a', {'cards': ['Sol Ring'] * 500})
        self.assertEqual(len(cache.cached('https://example.org/a', 60)['cards']), 500)
        self.assertIsNone(cache.cached('https://example.org/a', 0))
        self.assertIsNone(cache.cached('https://example.org/none', 60))
        with cache._connect() as conn:
            size, = conn.execute('SELECT size FROM web_cache').fetchone()
            conn.execute('UPDATE web_cache SET fetched_at = ?', (time.time() - recommendations.MAX_AGE - 60,))
        self.assertLess(size, 500)                              # compressed
        cache.prune()
        with cache._connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM web_cache').fetchone()[0], 0)

    def test_the_oldest_make_room(self):
        cache = self.cache()
        for number in range(5):
            cache._store(f'https://example.org/{number}', {'text': str(number) * 50})
            with cache._connect() as conn:
                conn.execute('UPDATE web_cache SET fetched_at = ? WHERE url = ?', (time.time() - 100 + number, f'https://example.org/{number}'))
        with cache._connect() as conn:
            sizes = [row[0] for row in conn.execute('SELECT size FROM web_cache ORDER BY fetched_at')]
        limit, recommendations.MAX_BYTES = recommendations.MAX_BYTES, sum(sizes[-2:])
        try:
            cache.prune()
        finally:
            recommendations.MAX_BYTES = limit
        with cache._connect() as conn:
            self.assertEqual([row[0][-1] for row in conn.execute('SELECT url FROM web_cache ORDER BY fetched_at')], ['3', '4'])

    def test_the_cache_in_the_card_database_moves_over(self):
        old = sqlite3.connect(str(self.tmp / 'collection.db'))
        old.execute('CREATE TABLE web_cache (url TEXT PRIMARY KEY, fetched_at REAL NOT NULL, body TEXT NOT NULL)')
        old.execute('INSERT INTO web_cache VALUES (?, ?, ?)', ('https://example.org/old', time.time(), '{"kept": true}'))
        old.commit()
        old.close()
        cache = self.cache()
        self.assertEqual(cache.cached('https://example.org/old', 60), {'kept': True})
        old = sqlite3.connect(str(self.tmp / 'collection.db'))
        self.assertIsNone(old.execute("SELECT 1 FROM sqlite_master WHERE name = 'web_cache'").fetchone())
        self.assertEqual(old.execute('SELECT COUNT(*) FROM inventory').fetchone()[0], 0)   # the rest of the file is as it was
        old.close()
        self.cache()                                            # a second start finds nothing to move



OLD_INVENTORY = """
    CREATE TABLE inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT, game TEXT NOT NULL DEFAULT 'mtg', card_id TEXT, card_name TEXT NOT NULL,
        set_name TEXT NOT NULL, set_code TEXT, card_number TEXT NOT NULL DEFAULT '', rarity TEXT, type_line TEXT,
        mana_cost TEXT, colors TEXT, color_identity TEXT, price_usd REAL, quantity INTEGER NOT NULL DEFAULT 1,
        condition TEXT NOT NULL DEFAULT 'Near Mint', finish TEXT NOT NULL DEFAULT 'regular', timestamp TEXT NOT NULL,
        location TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '', added_at TEXT, added_quantity INTEGER,
        UNIQUE(game, card_name, set_name, card_number, condition, finish, location))"""


class Constraints(TemporaryData):
    def old_file(self, with_stations=True):
        """A file as the versions before the constraints made it - also before stations"""
        path = self.tmp / 'old.db'
        conn = sqlite3.connect(str(path))
        conn.execute(OLD_INVENTORY)
        conn.execute('CREATE TABLE inventory_captures (id INTEGER PRIMARY KEY AUTOINCREMENT, inventory_id INTEGER NOT NULL, '
                     'file TEXT NOT NULL, captured_at TEXT NOT NULL' + (', station TEXT)' if with_stations else ')'))
        if with_stations:
            conn.execute('CREATE TABLE inventory_sources (inventory_id INTEGER NOT NULL, station TEXT NOT NULL, '
                         'quantity INTEGER NOT NULL, PRIMARY KEY (inventory_id, station))')
        entry = "INSERT INTO inventory (id, card_name, set_name, card_number, quantity, timestamp) VALUES (?, ?, 'Set', ?, ?, 'then')"
        conn.executemany(entry, [(3, 'Kept', '1', 2), (7, 'Also kept', '2', 1), (9, 'No copies', '3', 0)])
        conn.execute("DELETE FROM inventory WHERE id = 7")
        conn.execute(entry, (12, 'Newest', '4', 5))            # ids 7 and 10-11 were given out and are gone
        conn.executemany("INSERT INTO inventory_captures (id, inventory_id, file, captured_at) VALUES (?, ?, ?, 'then')",
                         [(1, 3, 'a.jpg'), (2, 12, 'b.jpg'), (3, 7, 'orphan.jpg'), (4, 9, 'of-nothing.jpg')])
        if with_stations:
            conn.executemany('INSERT INTO inventory_sources VALUES (?, ?, ?)',
                             [(3, 'desk-a', 2), (12, 'desk-a', 1), (7, 'desk-a', 1), (12, 'desk-b', 0)])
        conn.commit()
        conn.close()
        return path

    def test_an_older_file_is_rebuilt_with_them_once(self):
        import storage
        try:
            path = self.old_file()
            old = inv.InventoryManager(db_file=path)
            backups_made = list((self.tmp / 'backups' / 'migrations').glob('*.db'))
            self.assertEqual(len(backups_made), 1)
            kept = sqlite3.connect(str(backups_made[0]))       # as it was, readable
            self.assertEqual(kept.execute('SELECT COUNT(*) FROM inventory').fetchone()[0], 3)
            kept.close()
            rows = lambda sql: [tuple(row) for row in old.conn.execute(sql)]
            self.assertEqual(rows('SELECT id, card_name, quantity FROM inventory ORDER BY id'), [(3, 'Kept', 2), (12, 'Newest', 5)])
            self.assertEqual(rows('SELECT id, inventory_id FROM inventory_captures ORDER BY id'), [(1, 3), (2, 12)])
            self.assertEqual(rows('SELECT inventory_id, station, quantity FROM inventory_sources ORDER BY 1'),
                             [(3, 'desk-a', 2), (12, 'desk-a', 1)])
            self.assertEqual(rows('PRAGMA foreign_key_check'), [])
            # Ids are not given out again: the next entry is 13, the next capture 5
            new_id = old.add_card(CARD, GAME, 'regular', capture=self.photo, source='desk-a', quiet=True)
            self.assertEqual((new_id, rows('SELECT MAX(id) FROM inventory_captures')), (13, [(5,)]))
            old.conn.close()
            again = inv.InventoryManager(db_file=path)          # a second start: nothing to do
            self.assertEqual(len(list((self.tmp / 'backups' / 'migrations').glob('*.db'))), 1)
            self.assertEqual(again.get_stats(GAME)['total_cards'], 8)
            again.conn.close()
        finally:
            storage._backed_up.clear()

    def test_a_file_from_before_stations(self):
        import storage
        try:
            old = inv.InventoryManager(db_file=self.old_file(with_stations=False))
            self.assertEqual(old.get_stats(GAME)['total_cards'], 7)
            self.assertEqual(old.get_stats(GAME, 'desk-a')['total_cards'], 0)      # nobody is given old cards
            self.assertEqual(old.conn.execute('SELECT COUNT(*) FROM inventory_captures').fetchone()[0], 2)
            old.conn.close()
        finally:
            storage._backed_up.clear()

    def test_what_the_database_refuses_now(self):
        row_id = self.add('desk-a')
        for sql in ("INSERT INTO inventory_captures (inventory_id, file, captured_at) VALUES (999, 'x.jpg', 'now')",
                    "INSERT INTO inventory_sources VALUES (999, 'desk-a', 1)",
                    f"INSERT INTO inventory_sources VALUES ({row_id}, 'desk-b', 0)",
                    f'UPDATE inventory SET quantity = 0 WHERE id = {row_id}',
                    f"UPDATE inventory SET quantity = 'two' WHERE id = {row_id}",
                    f'UPDATE inventory SET added_quantity = -1 WHERE id = {row_id}'):
            with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                self.scan.conn.execute(sql)
            self.scan.conn.rollback()
        deck = self.decks.create(GAME, 'Test', 'commander')
        self.decks.set_card(deck, 'Card', 'main', 1)
        for sql in ("INSERT INTO deck_cards (deck_id, card_name, quantity, board) VALUES (999, 'Ghost', 1, 'main')",
                    f"INSERT INTO deck_cards (deck_id, card_name, quantity, board) VALUES ({deck}, 'Zero', 0, 'main')",
                    f"INSERT INTO deck_cards (deck_id, card_name, quantity, board) VALUES ({deck}, 'Odd', 1, 'attic')"):
            with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                self.decks.conn.execute(sql)
            self.decks.conn.rollback()
        # The children go with their parent, in the database itself
        self.decks.conn.execute('DELETE FROM decks WHERE id = ?', (deck,))
        self.decks.conn.commit()
        self.assertEqual(self.decks.conn.execute('SELECT COUNT(*) FROM deck_cards').fetchone()[0], 0)
        self.scan.conn.execute('DELETE FROM inventory WHERE id = ?', (row_id,))
        self.scan.conn.commit()
        count = lambda table: self.scan.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        self.assertEqual((count('inventory_captures'), count('inventory_sources')), (0, 0))

    def test_old_decks_are_rebuilt_too(self):
        import storage
        try:
            path = self.tmp / 'olddecks.db'
            conn = sqlite3.connect(str(path))
            conn.execute("CREATE TABLE decks (id INTEGER PRIMARY KEY AUTOINCREMENT, game TEXT NOT NULL, name TEXT NOT NULL, "
                         "format TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE deck_cards (id INTEGER PRIMARY KEY AUTOINCREMENT, deck_id INTEGER NOT NULL, card_name TEXT NOT NULL, "
                         "card_id TEXT, quantity INTEGER NOT NULL DEFAULT 1, board TEXT NOT NULL DEFAULT 'main', UNIQUE(deck_id, card_name, board))")
            conn.execute('CREATE INDEX idx_deck_cards_deck ON deck_cards(deck_id)')
            conn.execute("INSERT INTO decks VALUES (4, 'mtg', 'Kept', 'commander', '', 'then', 'then')")
            conn.executemany('INSERT INTO deck_cards (id, deck_id, card_name, quantity, board) VALUES (?, ?, ?, ?, ?)',
                             [(10, 4, 'A', 1, 'main'), (11, 4, 'B', 3, 'side'), (12, 99, 'Of a deck that is gone', 1, 'main'),
                              (13, 4, 'None', 0, 'main')])
            conn.commit()
            conn.close()
            from decks import DeckManager
            decks = DeckManager(db_file=path)
            self.assertEqual([(card['name'], card['quantity'], card['board']) for card in decks.get(4)['cards']],
                             [('A', 1, 'main'), ('B', 3, 'side')])
            decks.set_card(4, 'C', 'main', 1)
            self.assertEqual(decks.conn.execute("SELECT id FROM deck_cards WHERE card_name = 'C'").fetchone()[0], 14)
            decks.conn.close()
        finally:
            storage._backed_up.clear()

    def test_a_backup_from_before_restores(self):
        self.add('desk-a')
        backup_id = self.backup()
        # As a backup made before the constraints could be: rows beside entries that were gone
        old = sqlite3.connect(str(backups.BACKUPS_DIR / backup_id / 'backup.db'))
        old.execute("INSERT INTO scanned_inventory_sources VALUES (999, 'desk-b', 1)")
        old.execute("INSERT INTO scanned_inventory_captures (inventory_id, file, captured_at) VALUES (999, 'gone.jpg', 'then')")
        old.execute("INSERT INTO deck_cards (deck_id, card_name, quantity, board) VALUES (999, 'Ghost', 1, 'main')")
        old.commit()
        old.close()
        self.scan.clear_inventory(GAME)
        self.restore(backup_id)
        self.assertEqual((self.total(), self.total('desk-a'), self.total('desk-b')), (1, 1, 0))
        self.assertEqual(self.scan.conn.execute('PRAGMA foreign_key_check').fetchall(), [])


class Batches(TemporaryData):
    def batch(self, added_at):
        """Copies of the collection that came with one "Add to collection\""""
        return sum(inv.batch_quantity(row) for row in self.collection.conn.execute(
            'SELECT * FROM inventory WHERE added_at = ?', (added_at,)))

    def test_a_split_takes_only_its_share_of_the_batch(self):
        self.collection.add_card(CARD, GAME, 'regular', quantity=3, quiet=True, when='2026-01-01 10:00:00')   # there before
        self.add('desk-a', copies=2)
        self.collection.take_from(self.scan, GAME)                                    # the batch: 2 of the 5
        row = self.collection.conn.execute('SELECT * FROM inventory').fetchone()
        added_at = row['added_at']
        self.assertEqual(self.batch(added_at), 2)
        self.collection.update_card(row['id'], location='Box', split_quantity=3, quiet=True)   # 3 move, 2 stay
        self.assertEqual(self.batch(added_at), 2)                                     # used to be 5: 3 + 2
        removed = self.collection.remove_batch(GAME, added_at)
        self.assertEqual(removed['cards'], 2)
        self.assertEqual(self.collection.get_stats(GAME)['total_cards'], 3)


class CardDataImport(TemporaryData):
    def card(self, number):
        return {'id': f'id-{number}', 'name': f'Card {number}', 'set': 'tst', 'set_name': 'Test', 'collector_number': str(number),
                'layout': 'normal', 'prices': {}, 'image_uris': {'normal': ''}}

    def test_an_import_on_its_own_connection(self):
        cards = CardDatabase(db_file=self.tmp / 'cards.db')
        try:
            cards.conn.execute("INSERT INTO cards (id, name) VALUES ('old', 'Old Card')")   # not committed: a search's connection
            cards.conn.rollback()
            self.assertEqual(cards.populate_database([self.card(n) for n in range(40)]), 40)
            self.assertEqual(cards.conn.execute('SELECT COUNT(*) FROM cards').fetchone()[0], 40)
            self.assertEqual(cards.get_data_info('mtg')['card_count'], 40)
            self.assertFalse(cards.conn.in_transaction)
            with self.assertRaises(RuntimeError):                                      # a download cut short
                cards.populate_database([self.card(n) for n in range(5)])
            self.assertEqual(cards.conn.execute('SELECT COUNT(*) FROM cards').fetchone()[0], 40)
            cards._import_lock.acquire()
            try:
                with self.assertRaises(RuntimeError):                                  # one refresh at a time
                    cards.populate_database([self.card(n) for n in range(40)])
            finally:
                cards._import_lock.release()
        finally:
            cards.conn.close()


class StationUploads(unittest.TestCase):
    def test_a_picture_is_deleted_only_when_the_server_has_it(self):
        import importlib.util
        if importlib.util.find_spec('socketio') is None:
            self.skipTest('the station client needs python-socketio')
        from station_client import upload_verdict

        class Answer:
            def __init__(self, status, body):
                self.status_code, self.body = status, body

            def json(self):
                if not isinstance(self.body, (dict, list)):
                    raise ValueError('not JSON')
                return self.body
        for status, body, verdict in (
                (200, {'success': True, 'capture': 7, 'status': 'added'}, 'accepted'),
                (202, {'success': True, 'capture': 7, 'status': 'pending'}, 'accepted'),
                (200, {'success': False, 'capture': 7, 'status': 'error'}, 'accepted'),   # on record there, failed there
                (400, {'success': False, 'message': 'No readable picture'}, 'refused'),
                (413, {'success': False, 'message': 'Capture too large'}, 'refused'),
                (401, {'success': False, 'message': 'Wrong token'}, 'retry'),
                (404, '<html>Not Found</html>', 'retry'),                               # a proxy, another program
                (200, '<html>login</html>', 'retry'),
                (200, {'success': True}, 'retry'),
                (502, 'Bad Gateway', 'retry')):
            self.assertEqual(upload_verdict(Answer(status, body)), verdict, (status, body))


class FullBackup(TemporaryData):
    def setUp(self):
        super().setUp()
        import threading
        self.barrier = threading.RLock()
        self.pending = pending_store.PendingCaptures(db_file=self.tmp / 'pending.db')
        self.saved_review, self.saved_full = review_store.REVIEW_DIR, backups.FULL_DIR
        review_store.REVIEW_DIR = self.tmp / 'review'
        backups.FULL_DIR = self.tmp / 'full'
        self.review = review_store.ReviewQueue(db_file=self.tmp / 'collection.db')
        (self.tmp / 'settings.json').write_text('{}')

    def tearDown(self):
        self.pending.conn.close()
        self.review.conn.close()
        review_store.REVIEW_DIR, backups.FULL_DIR = self.saved_review, self.saved_full
        super().tearDown()

    def full(self, **kwargs):
        pictures = 'SELECT file FROM inventory_captures'
        return backups.create_full(
            managers=[self.collection, self.scan, self.decks, self.review, self.pending], barrier=self.barrier,
            databases=[self.tmp / 'collection.db', self.tmp / 'scan.db', self.tmp / 'pending.db'],
            linked=[('captures', inv.CAPTURES_DIR, self.tmp / 'collection.db', pictures),
                    ('captures', inv.CAPTURES_DIR, self.tmp / 'scan.db', pictures),
                    ('review', review_store.REVIEW_DIR, self.tmp / 'collection.db', 'SELECT file FROM review_queue'),
                    ('scanned_cards', self.tmp, self.tmp / 'pending.db', 'SELECT image FROM pending_captures')],
            copied=[self.tmp / 'settings.json', self.tmp / 'not-there.json'], **kwargs)

    def test_everything_at_one_moment_and_usable_elsewhere(self):
        self.add('desk-a', copies=2)
        self.collection.add_card(CARD, GAME, 'regular', capture=self.photo, quiet=True)
        self.review.add(GAME, self.photo, 'Unread', station='desk-a')
        self.pending.add(GAME, 'desk-a', 3, 'capture.jpg', None, True, 'now', capture_id='c3', sender='desk-a')
        # A move to the collection cut short after its first commit: noted, nothing arrived
        self.scan.conn.execute("INSERT INTO pending_moves (game, move_id, added_at) VALUES ('mtg', 'm1', 'now')")
        self.scan.conn.commit()
        made = self.full()
        self.assertTrue(made['verified'], made['missing'])
        self.assertEqual(made['files'], {'captures': 3, 'review': 1, 'scanned_cards': 1})
        self.assertEqual(made['databases']['scan.db']['inventory'], 1)
        self.assertEqual(made['copied'], ['settings.json'])
        self.assertEqual([item['id'] for item in backups.list_full()], [made['id']])

        # What is there now goes away; the backup, opened in a folder of its own, is a working state
        self.scan.clear_inventory(GAME)
        self.collection.clear_inventory(GAME)
        folder = Path(made['folder'])
        captures, inv.CAPTURES_DIR = inv.CAPTURES_DIR, folder / 'captures'
        try:
            collection = inv.InventoryManager(db_file=folder / 'collection.db')
            scan = inv.InventoryManager(db_file=folder / 'scan.db')
            self.assertEqual(collection.finish_interrupted_moves(scan), [])          # the cut-short move: cleared, cards stay
            self.assertEqual(scan.conn.execute('SELECT COUNT(*) FROM pending_moves').fetchone()[0], 0)
            self.assertEqual((scan.get_stats(GAME)['total_cards'], collection.get_stats(GAME)['total_cards']), (2, 1))
            for manager in (collection, scan):
                for entry in manager.get_all_cards(GAME):
                    for capture in entry['captures']:
                        self.assertTrue((folder / 'captures' / Path(capture['url']).name).is_file())
            waiting = pending_store.PendingCaptures(db_file=folder / 'pending.db')
            self.assertEqual([row['image'] for row in waiting.all()], ['capture.jpg'])
            self.assertTrue((folder / 'scanned_cards' / 'capture.jpg').is_file())
            for conn in (collection.conn, scan.conn, waiting.conn):
                conn.close()
        finally:
            inv.CAPTURES_DIR = captures

    def test_it_waits_for_what_is_under_way_and_keeps_the_newest(self):
        import threading
        self.add('desk-a')
        done = []
        with self.barrier:                                       # a capture being settled
            worker = threading.Thread(target=lambda: done.append(self.full(keep=1)))
            worker.start()
            worker.join(0.3)
            self.assertEqual(done, [])                           # not while that is half done
        worker.join(10)
        self.assertEqual(len(done), 1)
        import time as clock
        clock.sleep(1.1)                                         # names are to the second
        second = self.full(keep=1)
        self.assertEqual([item['id'] for item in backups.list_full()], [second['id']])

    def test_a_picture_that_is_gone_is_reported(self):
        self.add('desk-a')
        for file in inv.CAPTURES_DIR.glob('*.jpg'):
            file.unlink()
        made = self.full()
        self.assertFalse(made['verified'])
        self.assertEqual(len(made['missing']), 1)


if __name__ == '__main__':
    unittest.main()
