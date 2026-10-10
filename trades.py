# ============================================================================
# FILE: trades.py
# Cards set aside for a trade: they stay in the collection (still owned, still
# counted) until the trade is confirmed - only then are they removed - or it is
# cancelled. The tables and the triggers that keep the copies are in
# inventory.py (TRADES_TABLE, TRADE_CARDS_TABLE, TRADE_TRIGGERS).
# ============================================================================
import json

from inventory import _row_dict, now


class TradeError(Exception):
    """What the page shows as it is"""


class Trades:
    """
    The collection's trades. An open trade's rows name the entries and how many of their copies
    are set aside (several trades can hold copies of one entry, never more between them than
    it has); the database refuses to delete such an entry or to lower it below that. A
    confirmed trade is history: its rows keep the cards as they were (JSON), no entry.
    """

    def __init__(self, inventory):
        self.inventory = inventory
        self.conn = inventory.conn
        self._lock = inventory._lock

    def _writing(self, change):
        """Run a change under the collection's lock: committed whole, or rolled back"""
        with self._lock:
            try:
                result = change()
                self.inventory._commit()
                return result
            except BaseException:
                self.inventory._rollback()
                raise

    def _trade(self, trade_id, status='open'):
        row = self.conn.execute('SELECT * FROM trades WHERE id = ?', (trade_id,)).fetchone()
        if not row or row['status'] != status:
            raise TradeError('This trade is not open any more' if status == 'open' else 'Trade not found')
        return row

    def _set_aside(self, entry_id, except_row=None):
        """How many of an entry's copies the open trades hold (without one of their rows)"""
        return self.conn.execute('SELECT COALESCE(SUM(quantity), 0) FROM trade_cards WHERE inventory_id = ? AND id IS NOT ?',
                                 (entry_id, except_row)).fetchone()[0]

    # ------------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------------

    def list(self, game):
        """
        A game's trades, the open ones first, then the confirmed ones (newest first):
        {'id', 'name', 'status' ('open' / 'done'), 'created_at', 'closed_at', 'cards': [...],
        'quantity', 'value'}. A card is the inventory row as the page and the exporters know it,
        with 'quantity' the copies in the trade, 'row' (the trade's row, to change it) and - in
        an open trade - 'entry_quantity' and 'most' (how many the trade could hold).
        """
        with self._lock:
            trades = [dict(row) for row in self.conn.execute(
                "SELECT * FROM trades WHERE game = ? ORDER BY status = 'done', COALESCE(closed_at, created_at) DESC, id DESC",
                (game,))]
            for trade in trades:
                trade['cards'] = self.cards(trade['id'])
                trade['quantity'] = sum(card['quantity'] for card in trade['cards'])
                trade['value'] = round(sum(card['price'] * card['quantity'] for card in trade['cards']), 2)
        return trades

    def cards(self, trade_id):
        """A trade's cards (see list), by name"""
        with self._lock:
            cards = []
            for row in self.conn.execute('''
                    SELECT c.id AS trade_row, c.quantity AS copies, c.card, i.* FROM trade_cards c
                    LEFT JOIN inventory i ON i.id = c.inventory_id WHERE c.trade_id = ?''', (trade_id,)).fetchall():
                if row['id'] is None:  # confirmed: the card as it was
                    card = {**json.loads(row['card']), 'row': row['trade_row'], 'quantity': row['copies']}
                else:
                    card = {**_row_dict(row), 'row': row['trade_row'], 'quantity': row['copies'],
                            'entry_quantity': row['quantity'],
                            'most': row['quantity'] - self._set_aside(row['id'], row['trade_row'])}
                cards.append(card)
        return sorted(cards, key=lambda card: (card['name'].lower(), card['set_name'], card['number']))

    def by_entry(self, game):
        """{inventory id: [{'id', 'name', 'quantity'}]}: the open trades holding copies of each entry"""
        result = {}
        with self._lock:
            for row in self.conn.execute('''
                    SELECT c.inventory_id, c.quantity, t.id, t.name FROM trade_cards c JOIN trades t ON t.id = c.trade_id
                    WHERE t.game = ? AND c.inventory_id IS NOT NULL ORDER BY t.id''', (game,)):
                result.setdefault(row['inventory_id'], []).append(
                    {'id': row['id'], 'name': row['name'], 'quantity': row['quantity']})
        return result

    # ------------------------------------------------------------------------
    # Changing
    # ------------------------------------------------------------------------

    def add(self, game, name, entry_ids):
        """
        Set entries aside for the open trade with this name (made when there is none): every
        copy of each that no trade holds yet. Returns {'trade': id, 'name', 'cards': copies set
        aside now, 'skipped': entries that had none free}.
        """
        name = ' '.join(str(name or '').split())[:60]
        if not name:
            raise TradeError('The trade needs a name')

        def change():
            trade = self.conn.execute("SELECT id, name FROM trades WHERE game = ? AND status = 'open' AND name = ? COLLATE NOCASE",
                                      (game, name)).fetchone()
            if trade:
                trade_id, trade_name = trade['id'], trade['name']
            else:
                trade_id, trade_name = self.conn.execute(
                    "INSERT INTO trades (game, name, status, created_at) VALUES (?, ?, 'open', ?)",
                    (game, name, now())).lastrowid, name
            cards = skipped = 0
            for entry_id in dict.fromkeys(entry_ids):
                entry = self.inventory._get_row(entry_id)
                free = entry['quantity'] - self._set_aside(entry_id) if entry and entry['game'] == game else 0
                if free <= 0:
                    skipped += 1
                    continue
                self.conn.execute('''INSERT INTO trade_cards (trade_id, inventory_id, quantity) VALUES (?, ?, ?)
                                     ON CONFLICT(trade_id, inventory_id) DO UPDATE SET quantity = quantity + excluded.quantity''',
                                  (trade_id, entry_id, free))
                cards += free
            if not cards and not trade:
                raise TradeError('None of these cards has a copy that is not in a trade already')
            return {'trade': trade_id, 'name': trade_name, 'cards': cards, 'skipped': skipped}
        return self._writing(change)

    def set_quantity(self, trade_id, row_id, quantity):
        """How many copies of one of its cards an open trade holds; 0 takes the card out"""
        def change():
            self._trade(trade_id)
            row = self.conn.execute('SELECT * FROM trade_cards WHERE id = ? AND trade_id = ?', (row_id, trade_id)).fetchone()
            if not row:
                raise TradeError('This card is not in the trade any more')
            if quantity <= 0:
                self.conn.execute('DELETE FROM trade_cards WHERE id = ?', (row_id,))
                return
            entry = self.inventory._get_row(row['inventory_id'])
            most = entry['quantity'] - self._set_aside(entry['id'], row_id)
            if quantity > most:
                raise TradeError(f"Only {most} of {entry['card_name']} can go into this trade")
            self.conn.execute('UPDATE trade_cards SET quantity = ? WHERE id = ?', (quantity, row_id))
        return self._writing(change)

    def rename(self, trade_id, name):
        name = ' '.join(str(name or '').split())[:60]
        if not name:
            raise TradeError('The trade needs a name')

        def change():
            trade = self.conn.execute('SELECT * FROM trades WHERE id = ?', (trade_id,)).fetchone()
            if not trade:
                raise TradeError('Trade not found')
            if trade['status'] == 'open' and self.conn.execute(
                    "SELECT 1 FROM trades WHERE game = ? AND status = 'open' AND name = ? COLLATE NOCASE AND id != ?",
                    (trade['game'], name, trade_id)).fetchone():
                raise TradeError(f'There is an open trade called "{name}" already')
            self.conn.execute('UPDATE trades SET name = ? WHERE id = ?', (name, trade_id))
        return self._writing(change)

    def cancel(self, trade_id):
        """The trade is off: its cards are simply the collection's again. Returns the copies freed."""
        def change():
            self._trade(trade_id)
            freed = self.conn.execute('SELECT COALESCE(SUM(quantity), 0) FROM trade_cards WHERE trade_id = ?',
                                      (trade_id,)).fetchone()[0]
            self.conn.execute('DELETE FROM trades WHERE id = ?', (trade_id,))  # its rows go with it
            return freed
        return self._writing(change)

    def confirm(self, trade_id):
        """
        The trade happened: its copies leave the collection (the newest captures and the
        stations' counts with them, as when a quantity is lowered) and the trade stays as a
        record of the cards as they were. One transaction. Returns {'cards', 'entries', 'value'}.
        """
        def change():
            self._trade(trade_id)
            manager = self.inventory
            cards = entries = 0
            value = 0.0
            for row in self.conn.execute('SELECT * FROM trade_cards WHERE trade_id = ?', (trade_id,)).fetchall():
                entry = manager._get_row(row['inventory_id'])
                card = {**_row_dict(entry), 'quantity': row['quantity']}
                # No entry any more: the copies are free to go (TRADE_TRIGGERS)
                self.conn.execute('UPDATE trade_cards SET inventory_id = NULL, card = ? WHERE id = ?',
                                  (json.dumps(card), row['id']))
                left = entry['quantity'] - row['quantity']
                manager._take_copies(entry['id'], row['quantity'])
                if left > 0:
                    manager._trim_sources(entry['id'], left)
                    manager._trim_captures(entry['id'], left)
                cards += row['quantity']
                entries += 1
                value += card['price'] * row['quantity']
            self.conn.execute("UPDATE trades SET status = 'done', closed_at = ? WHERE id = ?", (now(), trade_id))
            manager.last_added = {}  # an undo must not reach into entries that changed under it
            return {'cards': cards, 'entries': entries, 'value': round(value, 2)}
        result = self._writing(change)
        self.inventory.log(f"Trade confirmed: {result['cards']} cards left the collection", level="success")
        return result

    def delete(self, trade_id):
        """Take a confirmed trade out of the history"""
        def change():
            self._trade(trade_id, status='done')
            self.conn.execute('DELETE FROM trades WHERE id = ?', (trade_id,))
        return self._writing(change)
