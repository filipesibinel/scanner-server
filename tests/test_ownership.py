"""
Which station scanned what (inventory_sources) through edits, moves, clears and backups, and
restoring backups - the cases of CODE_REVIEW.md (2026-10-09). Everything runs on temporary
databases and folders; no application data is touched.

    venv/bin/python -m unittest discover tests
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import backups  # noqa: E402
import inventory as inv  # noqa: E402
from decks import DeckManager  # noqa: E402

GAME = 'mtg'
CARD = {'card_id': None, 'name': 'Test Card', 'set_name': 'Test Set', 'set_code': 'tst', 'number': '1', 'price': 1.0}
OTHER = dict(CARD, name='Other Card', number='2')


class TemporaryData(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (inv.CAPTURES_DIR, backups.CAPTURES_DIR, backups.BACKUPS_DIR)
        inv.CAPTURES_DIR = backups.CAPTURES_DIR = self.tmp / 'captures'
        backups.BACKUPS_DIR = self.tmp / 'backups'
        inv.CAPTURES_DIR.mkdir()
        backups.BACKUPS_DIR.mkdir()
        self.collection = inv.InventoryManager(db_file=self.tmp / 'collection.db')
        self.scan = inv.InventoryManager(db_file=self.tmp / 'scan.db')
        self.decks = DeckManager(db_file=self.tmp / 'collection.db')
        # A scanned card comes with its capture: the photo of a copy travels with it
        self.photo = self.tmp / 'capture.jpg'
        cv2.imwrite(str(self.photo), np.full((140, 100, 3), 128, np.uint8))

    def tearDown(self):
        for manager in (self.collection, self.scan, self.decks):
            manager.conn.close()
        inv.CAPTURES_DIR, backups.CAPTURES_DIR, backups.BACKUPS_DIR = self.saved
        shutil.rmtree(self.tmp)

    def add(self, station, card=CARD, copies=1, **kwargs):
        for _ in range(copies):
            row_id = self.scan.add_card(card, GAME, 'regular', source=station, quiet=True, capture=self.photo, **kwargs)
        return row_id

    def cards(self, station=None):
        """{(name, finish, location): quantity} of the scanned cards - a station's, or all"""
        return {(card['name'], card['finish'], card['location']): card['quantity']
                for card in self.scan.get_all_cards(GAME, station)}

    def total(self, station=None):
        return self.scan.get_stats(GAME, station)['total_cards']

    def backup(self, **kwargs):
        return backups.create(self.collection, self.scan, self.decks, **kwargs)['id']

    def restore(self, backup_id):
        return backups.restore(backup_id, self.collection, self.scan, self.decks)


class SplitsAndMerges(TemporaryData):
    def test_split_keeps_the_station(self):
        row_id = self.add('desk-a', copies=3)
        self.scan.update_card(row_id, finish='foil', split_quantity=1, quiet=True)
        self.assertEqual(self.cards(), {('Test Card', 'regular', ''): 2, ('Test Card', 'foil', ''): 1})
        self.assertEqual(self.cards('desk-a'), self.cards())

    def test_moving_every_copy_keeps_the_station(self):
        row_id = self.add('desk-a', copies=2)
        self.scan.update_card(row_id, location='Box 1', split_quantity=2, quiet=True)
        self.assertEqual(self.cards('desk-a'), {('Test Card', 'regular', 'Box 1'): 2})

    def test_merge_keeps_the_station(self):
        first = self.add('desk-a', location='Box 1')
        self.add('desk-a', location='Box 2')
        self.scan.update_card(first, location='Box 2', quiet=True)  # now the same entry as the second
        self.assertEqual(self.cards(), {('Test Card', 'regular', 'Box 2'): 2})
        self.assertEqual(self.cards('desk-a'), self.cards())

    def test_merge_of_two_stations(self):
        first = self.add('desk-a', location='Box 1')
        self.add('desk-b', location='Box 2')
        self.scan.update_card(first, location='Box 2', quiet=True)
        self.assertEqual(self.total(), 2)
        self.assertEqual(self.total('desk-a'), 1)
        self.assertEqual(self.total('desk-b'), 1)

    def test_split_of_an_entry_two_stations_scanned(self):
        # desk-a scanned two copies, then desk-b one: the newest copy - desk-b's - is the one that moves
        self.add('desk-a', copies=2)
        row_id = self.add('desk-b')
        self.scan.update_card(row_id, finish='foil', split_quantity=1, quiet=True)
        self.assertEqual(self.cards('desk-a'), {('Test Card', 'regular', ''): 2})
        self.assertEqual(self.cards('desk-b'), {('Test Card', 'foil', ''): 1})

    def test_split_never_gives_a_station_more_than_there_is(self):
        self.add('desk-a', copies=2)
        row_id = self.add('desk-b', copies=2)
        self.scan.update_card(row_id, finish='foil', split_quantity=3, quiet=True)
        self.assertEqual(self.total(), 4)
        self.assertEqual(self.total('desk-a') + self.total('desk-b'), 4)
        self.assertEqual(self.cards('desk-b'), {('Test Card', 'foil', ''): 2})
        self.assertEqual(self.cards('desk-a'), {('Test Card', 'foil', ''): 1, ('Test Card', 'regular', ''): 1})

    def test_station_cards_move_to_the_collection_after_a_split(self):
        row_id = self.add('desk-a', copies=3)
        self.add('desk-b', card=OTHER)
        self.scan.update_card(row_id, finish='foil', split_quantity=1, quiet=True)
        moved = self.collection.take_from(self.scan, GAME, station='desk-a')
        self.assertEqual(moved['cards'], 3)
        self.assertEqual(self.cards(), {('Other Card', 'regular', ''): 1})
        self.assertEqual(self.collection.get_stats(GAME)['total_cards'], 3)

    def test_split_without_captures(self):
        # Cards added without a photo: the station that came to the entry last gives up the copy
        for station in ('desk-a', 'desk-a', 'desk-b'):
            row_id = self.scan.add_card(CARD, GAME, 'regular', source=station, quiet=True)
        self.scan.update_card(row_id, finish='foil', split_quantity=1, quiet=True)
        self.assertEqual(self.cards('desk-a'), {('Test Card', 'regular', ''): 2})
        self.assertEqual(self.cards('desk-b'), {('Test Card', 'foil', ''): 1})

    def test_clearing_a_station_after_a_split(self):
        row_id = self.add('desk-a', copies=3)
        self.add('desk-b', card=OTHER)
        self.scan.update_card(row_id, finish='foil', split_quantity=1, quiet=True)
        self.assertEqual(self.scan.clear_inventory(GAME, station='desk-a')['deleted'], 3)
        self.assertEqual(self.cards(), {('Other Card', 'regular', ''): 1})


class Backups(TemporaryData):
    def test_restore_brings_back_the_stations(self):
        self.add('desk-a', copies=2)
        backup_id = self.backup(note='two cards of desk-a')
        self.scan.clear_inventory(GAME, station='desk-a')
        self.assertEqual(self.total(), 0)
        self.restore(backup_id)
        self.assertEqual(self.total(), 2)
        self.assertEqual(self.total('desk-a'), 2)

    def test_restore_over_another_ownership(self):
        self.scan.add_card(CARD, GAME, 'regular', quiet=True)   # scanned by no station
        backup_id = self.backup(note='one card of nobody')
        self.add('desk-b')                                       # desk-b adds a copy to the same entry
        self.assertEqual(self.total('desk-b'), 1)
        self.restore(backup_id)
        self.assertEqual(self.total(), 1)
        self.assertEqual(self.total('desk-b'), 0)

    def test_backup_made_before_stations(self):
        self.add('desk-a', copies=2)
        backup_id = self.backup(note='as an old backup')
        # As backups were made before inventory_sources was part of them
        import sqlite3
        old = sqlite3.connect(str(backups.BACKUPS_DIR / backup_id / 'backup.db'))
        old.execute('DROP TABLE scanned_inventory_sources')
        old.execute('DROP TABLE collection_inventory_sources')
        old.commit()
        old.close()
        self.add('desk-b', copies=1)
        self.restore(backup_id)
        self.assertEqual(self.total(), 2)
        self.assertEqual(self.total('desk-a'), 0)   # not known any more - and not desk-b's either
        self.assertEqual(self.total('desk-b'), 0)

    def test_restoring_the_oldest_automatic_backup(self):
        self.collection.add_card(CARD, GAME, 'regular', quiet=True)
        made = [self.backup(note=f'automatic {n}', automatic=True) for n in range(backups.KEEP_AUTOMATIC)]
        self.collection.add_card(OTHER, GAME, 'regular', quiet=True)
        result = self.restore(made[0])                # used to delete made[0] before reading it
        self.assertEqual(result['restored']['id'], made[0])
        self.assertTrue((backups.BACKUPS_DIR / made[0] / 'backup.db').is_file())
        self.assertEqual(self.collection.get_stats(GAME)['total_cards'], 1)

    def test_automatic_backups_are_still_limited(self):
        # (Made within one second the backups' names repeat, so only their number is checked)
        for n in range(backups.KEEP_AUTOMATIC + 3):
            self.backup(note=f'automatic {n}', automatic=True)
        kept = [item for item in backups.list_backups() if item.get('automatic')]
        self.assertEqual(len(kept), backups.KEEP_AUTOMATIC)



class DownloadedBackups(TemporaryData):
    def test_a_packed_backup_is_a_backup_again_when_unpacked(self):
        import io, zipfile
        self.add('desk-a')
        backup_id = self.backup(note='to keep elsewhere')
        packed = io.BytesIO()
        self.assertEqual(backups.archive(backup_id, packed), f'scanner-backup-{backup_id}.zip')
        names = zipfile.ZipFile(packed).namelist()
        self.assertIn(f'{backup_id}/backup.db', names)
        self.assertEqual(len([name for name in names if name.startswith(f'{backup_id}/captures/')]), 1)
        backups.delete(backup_id)
        self.scan.clear_inventory(GAME)
        zipfile.ZipFile(packed).extractall(backups.BACKUPS_DIR)
        self.assertEqual([item['note'] for item in backups.list_backups()], ['to keep elsewhere'])
        self.restore(backup_id)
        self.assertEqual(self.total('desk-a'), 1)

    def packed(self, backup_id):
        import io
        packed = io.BytesIO()
        backups.archive(backup_id, packed)
        return packed

    def test_an_uploaded_backup_joins_the_list(self):
        self.add('desk-a')
        backup_id = self.backup(note='to keep elsewhere', daily=True)
        packed = self.packed(backup_id)
        with self.assertRaises(backups.BackupError):              # it is here already
            backups.add_archive(packed)
        backups.delete(backup_id)
        self.scan.clear_inventory(GAME)
        added = backups.add_archive(packed)
        self.assertEqual((added['id'], added['scanned'], added['daily'], added['uploaded']), (backup_id, 1, False, True))
        self.assertEqual([item['id'] for item in backups.list_backups()], [backup_id])
        self.restore(backup_id)
        self.assertEqual(self.total('desk-a'), 1)
        self.assertEqual(len(list(inv.CAPTURES_DIR.glob('*.jpg'))), 1)

    def test_files_that_are_not_a_backup_are_refused(self):
        import io, zipfile
        self.add('desk-a')
        backup_id = self.backup()
        good = zipfile.ZipFile(self.packed(backup_id))
        database = good.read(f'{backup_id}/backup.db')
        backups.delete(backup_id)
        other = '2020-01-01_00-00-00'

        def made(files):
            packed = io.BytesIO()
            with zipfile.ZipFile(packed, 'w') as zipped:
                for name, data in files.items():
                    zipped.writestr(name, data)
            return packed

        bad = [io.BytesIO(b'not a zip'), made({}), made({'backup.db': database}),
               made({f'{backup_id}/backup.db': b'not a database'}),
               made({f'{backup_id}/backup.db': database, f'{backup_id}/captures/../../evil.jpg': b'x'}),
               made({f'{backup_id}/backup.db': database, f'{backup_id}/captures/sub/a.jpg': b'x'}),
               made({f'{backup_id}/backup.db': database, f'{backup_id}/run.sh': b'x'}),
               made({f'{backup_id}/backup.db': database, '../evil.txt': b'x'}),
               made({f'{backup_id}/backup.db': database, f'{other}/backup.db': database}),
               made({f'../{backup_id}/backup.db': database})]
        for packed in bad:
            with self.assertRaises(backups.BackupError):
                backups.add_archive(packed)
        self.assertEqual(list(backups.BACKUPS_DIR.iterdir()), [])  # nothing left behind, nothing outside
        self.assertFalse((self.tmp / 'evil.jpg').exists() or (self.tmp / 'evil.txt').exists())

    def test_an_upload_larger_than_the_limit_is_refused(self):
        self.add('desk-a')
        backup_id = self.backup()
        packed = self.packed(backup_id)
        backups.delete(backup_id)
        limit, backups.UPLOAD_MAX_BYTES = backups.UPLOAD_MAX_BYTES, 1000
        try:
            with self.assertRaises(backups.BackupError):
                backups.add_archive(packed)
        finally:
            backups.UPLOAD_MAX_BYTES = limit
        self.assertEqual(list(backups.BACKUPS_DIR.iterdir()), [])

    def test_unknown_backups_are_refused(self):
        import io
        for bad in ('../captures', '2026-01-01_00-00-00', ''):
            with self.assertRaises(backups.BackupError):
                backups.archive(bad, io.BytesIO())


class ScheduledBackups(TemporaryData):
    def scheduled(self, every_hours=24, keep_count=7):
        return backups.create_scheduled(self.collection, self.scan, self.decks, every_hours, keep_count)

    def age(self, backup_id, hours):
        """Make a backup look as if it was made that many hours ago"""
        import json, sqlite3
        from datetime import datetime, timedelta
        conn = sqlite3.connect(str(backups.BACKUPS_DIR / backup_id / 'backup.db'))
        info = json.loads(conn.execute('SELECT value FROM info').fetchone()[0])
        info['created'] = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('UPDATE info SET value = ?', (json.dumps(info),))
        conn.commit()
        conn.close()

    def test_made_when_due_and_changed(self):
        self.assertIsNone(self.scheduled())                       # nothing to keep yet
        self.assertEqual(backups.list_backups(), [])
        self.collection.add_card(CARD, GAME, 'regular', quiet=True)
        first = self.scheduled()
        self.assertTrue(first['daily'])
        self.collection.add_card(OTHER, GAME, 'regular', quiet=True)
        self.assertIsNone(self.scheduled())                       # changed, but not due
        self.age(first['id'], 25)
        self.assertEqual(self.scheduled()['cards'], 2)            # due and changed
        self.assertEqual(len(backups.list_backups()), 2)

    def test_none_while_nothing_changed(self):
        self.collection.add_card(CARD, GAME, 'regular', quiet=True)
        first = self.scheduled()
        self.age(first['id'], 25)
        self.assertIsNone(self.scheduled())                       # due, but the same as the last one
        self.add('desk-a')                                        # a scanned card is a change too
        self.assertIsNotNone(self.scheduled())

    def test_an_empty_collection_leaves_the_last_backup(self):
        row_id = self.collection.add_card(CARD, GAME, 'regular', quiet=True)
        first = self.scheduled(keep_count=1)
        self.age(first['id'], 25)
        self.collection.delete_card(row_id, quiet=True)
        self.assertIsNone(self.scheduled(keep_count=1))           # used to clear away `first` for an empty one
        self.assertEqual([item['id'] for item in backups.list_backups()], [first['id']])

    def test_a_refused_schedule_changes_nothing(self):
        self.assertEqual(backups.parse_schedule({'every_hours': 0, 'keep': '500'}),
                         {'backup_every_hours': 0, 'backup_keep': backups.KEEP_MAX})
        for bad in ({'every_hours': 0, 'keep': 'invalid'}, {'every_hours': 5}, {'every_hours': None},
                    {'keep': [1]}, {'every_hours': 24, 'keep': None}):
            with self.assertRaises(ValueError):                   # raised before the caller saves anything
                backups.parse_schedule(bad)

    def test_off(self):
        self.collection.add_card(CARD, GAME, 'regular', quiet=True)
        self.assertIsNone(self.scheduled(every_hours=0))
        self.assertEqual(backups.list_backups(), [])

    def test_keeps_the_newest_only_and_the_manual_ones(self):
        self.collection.add_card(CARD, GAME, 'regular', quiet=True)
        manual = self.backup(note='mine')
        for n in range(4):
            for item in backups.list_backups():
                if item.get('daily'):
                    self.age(item['id'], 2)
            self.collection.add_card(dict(CARD, number=str(10 + n)), GAME, 'regular', quiet=True)
            self.assertIsNotNone(self.scheduled(every_hours=1, keep_count=2))
        listed = backups.list_backups()
        self.assertEqual(len([item for item in listed if item.get('daily')]), 2)
        self.assertIn(manual, [item['id'] for item in listed])


if __name__ == '__main__':
    unittest.main()
