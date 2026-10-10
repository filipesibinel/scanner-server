"""
Cards set aside for a trade (trades.py): they stay in the collection until the trade is
confirmed, nothing else can take them meanwhile, and a backup brings them back with their
entries. Everything runs on temporary databases and folders; no application data is touched.

    venv/bin/python -m unittest discover tests
"""
import io
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import inventory as inv  # noqa: E402
from games.base import write_collection_csv  # noqa: E402
from test_ownership import CARD, GAME, OTHER, TemporaryData  # noqa: E402
from trades import TradeError, Trades  # noqa: E402


class TradeData(TemporaryData):
    def setUp(self):
        super().setUp()
        self.trades = Trades(self.collection)
        # Three copies of one card (two of them photographed by a camera), one of another
        for _ in range(2):
            self.card = self.collection.add_card(CARD, GAME, 'regular', source='desk-a', quiet=True, capture=self.photo)
        self.collection.add_card(CARD, GAME, 'regular', quiet=True)
        self.other = self.collection.add_card(OTHER, GAME, 'regular', quiet=True, location='Binder 2')

    def owned(self):
        return {card['name']: card['quantity'] for card in self.collection.get_all_cards(GAME)}

    def trade(self, name='Jo'):
        return next(trade for trade in self.trades.list(GAME) if trade['name'] == name)


class SettingAside(TradeData):
    def test_the_cards_stay_in_the_collection(self):
        result = self.trades.add(GAME, 'Jo', [self.card, self.other])
        self.assertEqual((result['cards'], result['skipped']), (4, 0))
        self.assertEqual(self.owned(), {'Test Card': 3, 'Other Card': 1})
        self.assertEqual(self.collection.get_stats(GAME)['total_cards'], 4)
        trade = self.trade()
        self.assertEqual((trade['status'], trade['quantity']), ('open', 4))
        self.assertEqual(self.trades.by_entry(GAME)[self.card], [{'id': trade['id'], 'name': 'Jo', 'quantity': 3}])

    def test_the_same_name_is_the_same_trade(self):
        first = self.trades.add(GAME, 'Jo', [self.card])
        again = self.trades.add(GAME, ' jo ', [self.card, self.other])
        self.assertEqual(again['trade'], first['trade'])
        self.assertEqual((again['cards'], again['skipped']), (1, 1))  # the first card had no copy left
        self.assertEqual(len(self.trades.list(GAME)), 1)

    def test_two_trades_share_an_entry_but_not_a_copy(self):
        trade = self.trades.add(GAME, 'Jo', [self.card])['trade']
        row = self.trade()['cards'][0]['row']
        self.trades.set_quantity(trade, row, 1)
        self.assertEqual(self.trades.add(GAME, 'Ana', [self.card])['cards'], 2)
        with self.assertRaises(TradeError):
            self.trades.set_quantity(trade, row, 2)  # Ana holds the other two
        self.assertEqual(self.trade()['cards'][0]['most'], 1)

    def test_a_trade_of_nothing_is_not_made(self):
        self.trades.add(GAME, 'Jo', [self.card, self.other])
        with self.assertRaises(TradeError):
            self.trades.add(GAME, 'Ana', [self.card])
        with self.assertRaises(TradeError):
            self.trades.add(GAME, '  ', [self.other])
        self.assertEqual([trade['name'] for trade in self.trades.list(GAME)], ['Jo'])

    def test_taking_a_card_out(self):
        trade = self.trades.add(GAME, 'Jo', [self.card, self.other])['trade']
        row = next(card['row'] for card in self.trade()['cards'] if card['name'] == 'Other Card')
        self.trades.set_quantity(trade, row, 0)
        self.assertEqual([card['name'] for card in self.trade()['cards']], ['Test Card'])
        self.collection.delete_card(self.other, quiet=True)  # free again
        self.assertEqual(self.owned(), {'Test Card': 3})


class HeldCopiesStay(TradeData):
    def setUp(self):
        super().setUp()
        self.id = self.trades.add(GAME, 'Jo', [self.card])['trade']
        self.trades.set_quantity(self.id, self.trade()['cards'][0]['row'], 2)

    def refused(self, change):
        with self.assertRaises(sqlite3.IntegrityError) as raised:
            change()
        self.assertIn(inv.TRADE_HELD, str(raised.exception))
        self.assertFalse(self.collection.conn.in_transaction)  # nothing left half done
        self.assertEqual(self.owned(), {'Test Card': 3, 'Other Card': 1})
        self.assertEqual(self.trade()['quantity'], 2)

    def test_the_free_copy_can_go(self):
        self.collection.update_card(self.card, quantity=2, quiet=True)
        self.assertEqual(self.owned()['Test Card'], 2)

    def test_not_lowered_below_what_the_trade_holds(self):
        self.refused(lambda: self.collection.update_card(self.card, quantity=1, quiet=True))

    def test_not_deleted(self):
        self.refused(lambda: self.collection.delete_card(self.card, quiet=True))
        self.refused(lambda: self.collection.bulk_update([self.other, self.card], 'delete'))

    def test_not_moved_away(self):
        self.refused(lambda: self.collection.bulk_update([self.card], 'location', 'Box'))
        self.collection.update_card(self.card, location='Box', split_quantity=1, quiet=True)  # the free one
        self.assertEqual(sorted((card['location'], card['quantity']) for card in self.collection.get_all_cards(GAME)
                                if card['name'] == 'Test Card'), [('', 2), ('Box', 1)])

    def test_a_whole_stack_does_not_move_but_a_single_card_does(self):
        self.refused(lambda: self.collection.update_card(self.card, location='Box', split_quantity=3, quiet=True))
        self.refused(lambda: self.collection.import_entries(
            [{'fields': CARD, 'quantity': 1, 'finish': 'regular'}], GAME, replace_existing=True))
        # One copy is changed where it is: the entry stays, and the trade with it
        self.trades.add(GAME, 'Jo', [self.other])
        self.collection.update_card(self.other, location='Box', quiet=True)
        self.assertEqual({card['name']: card['location'] for card in self.trade()['cards']},
                         {'Test Card': '', 'Other Card': 'Box'})

    def test_not_cleared_or_taken_with_its_batch(self):
        cleared = self.collection.clear_inventory(GAME)
        self.assertEqual((cleared['success'], cleared['error']), (False, inv.TRADE_HELD))
        self.assertEqual(len(list(inv.CAPTURES_DIR.glob('*'))), 2)  # the photos too
        added = self.collection.get_all_cards(GAME)[0]['added_at']
        self.refused(lambda: self.collection.remove_batch(GAME, added))

    def test_more_copies_and_other_edits_are_fine(self):
        self.collection.add_card(CARD, GAME, 'regular', quiet=True)
        self.collection.update_card(self.card, condition='Played', tags='trade', quiet=True)
        self.assertEqual(self.owned()['Test Card'], 4)
        self.assertEqual(self.trade()['cards'][0]['condition'], 'Played')


class ClosingATrade(TradeData):
    def setUp(self):
        super().setUp()
        self.id = self.trades.add(GAME, 'Jo', [self.card, self.other])['trade']
        self.trades.set_quantity(self.id, next(card['row'] for card in self.trade()['cards'] if card['name'] == 'Test Card'), 2)

    def test_cancelled_nothing_changed(self):
        self.assertEqual(self.trades.cancel(self.id), 3)
        self.assertEqual(self.trades.list(GAME), [])
        self.assertEqual(self.owned(), {'Test Card': 3, 'Other Card': 1})
        self.collection.delete_card(self.card, quiet=True)

    def test_confirmed_the_cards_leave(self):
        value = self.trade()['value']
        result = self.trades.confirm(self.id)
        self.assertEqual((result['cards'], result['entries'], result['value']), (3, 2, value))
        self.assertEqual(self.owned(), {'Test Card': 1})
        # What stood beside the copies went with them: one photo and no camera's claim beyond the copy left
        self.assertEqual(len(self.collection.captures_by_entry(GAME)[self.card]), 1)
        self.assertLessEqual(sum(self.collection.stations_by_entry(GAME).get(self.card, {}).values()), 1)
        self.assertEqual(self.collection.conn.execute('SELECT COUNT(*) FROM inventory_captures').fetchone()[0], 1)

    def test_confirmed_it_is_a_record(self):
        self.trades.confirm(self.id)
        trade = self.trade()
        self.assertEqual((trade['status'], trade['quantity'], bool(trade['closed_at'])), ('done', 3, True))
        self.assertEqual({card['name']: (card['quantity'], card['location']) for card in trade['cards']},
                         {'Test Card': (2, ''), 'Other Card': (1, 'Binder 2')})
        self.assertEqual(self.trades.by_entry(GAME), {})
        text = io.StringIO()
        write_collection_csv(trade['cards'], text)  # a record can still be exported
        self.assertEqual(len(text.getvalue().splitlines()), 3)
        # ... and what is left of the entry is not held any more
        self.collection.delete_card(self.card, quiet=True)
        self.assertEqual(self.trade()['quantity'], 3)

    def test_confirmed_once(self):
        self.trades.confirm(self.id)
        for change in (lambda: self.trades.confirm(self.id), lambda: self.trades.cancel(self.id),
                       lambda: self.trades.set_quantity(self.id, 1, 1)):
            with self.assertRaises(TradeError):
                change()
        self.assertEqual(self.owned(), {'Test Card': 1})
        self.trades.delete(self.id)
        self.assertEqual(self.trades.list(GAME), [])

    def test_the_same_name_again_is_a_new_trade(self):
        self.trades.confirm(self.id)
        self.assertNotEqual(self.trades.add(GAME, 'Jo', [self.card])['trade'], self.id)
        self.assertEqual([trade['status'] for trade in self.trades.list(GAME)], ['open', 'done'])

    def test_a_failed_confirm_changes_nothing(self):
        original = self.collection._trim_captures
        self.collection._trim_captures = lambda *args: 1 / 0
        try:
            with self.assertRaises(ZeroDivisionError):
                self.trades.confirm(self.id)
        finally:
            self.collection._trim_captures = original
        self.assertEqual(self.owned(), {'Test Card': 3, 'Other Card': 1})
        self.assertEqual((self.trade()['status'], self.trade()['quantity']), ('open', 3))
        self.assertEqual(self.collection.conn.execute('SELECT COUNT(*) FROM inventory_captures').fetchone()[0], 2)
        self.assertEqual(len(list(inv.CAPTURES_DIR.glob('*'))), 2)


class TradesInBackups(TradeData):
    def test_a_restore_brings_the_trade_back_with_its_entries(self):
        trade = self.trades.add(GAME, 'Jo', [self.card, self.other])['trade']
        backup = self.backup()
        self.trades.confirm(trade)
        self.assertEqual(self.owned(), {})
        self.restore(backup)
        self.assertEqual(self.owned(), {'Test Card': 3, 'Other Card': 1})
        self.assertEqual((self.trade()['status'], self.trade()['quantity']), ('open', 4))
        with self.assertRaises(sqlite3.IntegrityError):
            self.collection.delete_card(self.card, quiet=True)

    def test_a_restore_over_an_open_trade(self):
        backup = self.backup()  # no trade yet
        self.trades.add(GAME, 'Jo', [self.card])
        self.restore(backup)
        self.assertEqual(self.trades.list(GAME), [])
        self.assertEqual(self.owned(), {'Test Card': 3, 'Other Card': 1})

    def test_a_backup_from_before_trades(self):
        backup = self.backup()
        source = sqlite3.connect(str(self.tmp / 'backups' / backup / 'backup.db'))
        for table in ('collection_trades', 'collection_trade_cards', 'scanned_trades', 'scanned_trade_cards'):
            source.execute(f'DROP TABLE {table}')
        source.commit()
        source.close()
        self.trades.add(GAME, 'Jo', [self.card])
        self.restore(backup)
        self.assertEqual(self.trades.list(GAME), [])
        self.assertEqual(self.owned(), {'Test Card': 3, 'Other Card': 1})

    def test_the_triggers_survive_a_new_start(self):
        self.trades.add(GAME, 'Jo', [self.card])
        self.collection.conn.close()
        self.collection = inv.InventoryManager(db_file=self.tmp / 'collection.db')
        self.trades = Trades(self.collection)
        with self.assertRaises(sqlite3.IntegrityError):
            self.collection.delete_card(self.card, quiet=True)
        self.assertEqual(self.trade()['quantity'], 3)


if __name__ == '__main__':
    unittest.main()
