#!/usr/bin/env python3
"""
Times the database requests the app makes, and what candidate indexes would change - on COPIES
of the database files, never on the ones in use:

    venv/bin/python scripts/benchmark_db.py <folder with copies of cards_database.db and scan_inventory.db>

Take the copies with SQLite's backup (python3 -c "import sqlite3; ..." or scripts/backup.sh),
not with cp of a file that is open. The folder is written to: rows are added to the copies to
measure at sizes the tables do not have yet, and indexes are created and dropped.

It prints, for each request: the query plan of its SQL and the median time of repeated runs
(the first, cold run apart). Nothing here changes the app: an index is added to the code only
when these numbers say so (PROGRAM_DOCUMENTATION.md, Card database, has the last measurement).
"""
import sqlite3
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RUNS = 15


def timed(call, runs=RUNS):
    """(first run, median of the others) in milliseconds"""
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        call()
        times.append((time.perf_counter() - start) * 1000)
    return times[0], statistics.median(times[1:])


def plan(conn, sql, values=()):
    return ' | '.join(row[-1] for row in conn.execute('EXPLAIN QUERY PLAN ' + sql, values))


def report(label, call, conn=None, sql=None, values=()):
    cold, warm = timed(call)
    print(f"{label:<58} cold {cold:8.2f} ms   warm {warm:8.2f} ms")
    if sql:
        print(f"    plan: {plan(conn, sql, values)}")
    return warm


def with_index(conn, statement, name, label, call):
    """Time a request without and with a candidate index (dropped again afterwards)"""
    _, before = timed(call)
    conn.execute(statement)
    conn.commit()
    _, after = timed(call)
    size = conn.execute("SELECT SUM(pgsize) FROM dbstat WHERE name = ?", (name,)).fetchone()[0] or 0
    conn.execute(f'DROP INDEX {name}')
    conn.commit()
    print(f"    {label}: {before:.2f} ms -> {after:.2f} ms with {name} ({size / 1024:.0f} KiB)")


def main(folder):
    folder = Path(folder)
    import inventory as inv
    inv.CAPTURES_DIR = folder / 'captures'
    from database import CardDatabase
    from decks import DeckManager
    from review import ReviewQueue

    cards = CardDatabase(db_file=folder / 'cards_database.db')
    collection = inv.InventoryManager(db_file=folder / 'cards_database.db')
    scan = inv.InventoryManager(db_file=folder / 'scan_inventory.db')
    decks = DeckManager(db_file=folder / 'cards_database.db')
    review = ReviewQueue(db_file=folder / 'cards_database.db')
    count = lambda conn, table: conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    print(f"cards {count(cards.conn, 'cards')}, collection entries {count(collection.conn, 'inventory')}, "
          f"decks {count(decks.conn, 'decks')}, deck cards {count(decks.conn, 'deck_cards')}, "
          f"review items {count(review.conn, 'review_queue')}, scanned entries {count(scan.conn, 'inventory')}\n")

    print('-- card data --')
    report('exact printing (set + number)', lambda: cards.get_card_by_set_number('hob', '160'), cards.conn,
           'SELECT * FROM cards WHERE set_code = ? AND collector_number = ?', ('hob', '160'))
    report('exact name', lambda: cards.search_card('Sol Ring', fuzzy=False), cards.conn,
           'SELECT * FROM cards WHERE name_search = ? LIMIT 1', ('sol ring',))
    report('name substring, common ("dragon")', lambda: cards.search_cards_by_partial_name('dragon', limit=10), cards.conn,
           'SELECT * FROM cards WHERE name_search LIKE ? OR flavor_search LIKE ? LIMIT 10', ('%dragon%', '%dragon%'))
    report('name substring, rare ("mirkwood nurt")', lambda: cards.search_cards_by_partial_name('mirkwood nurt', limit=10))
    report('name substring, no match', lambda: cards.search_cards_by_partial_name('zzzqqqxxx', limit=10))
    report('deck builder: text "dragon"', lambda: cards.search_cards(text='dragon'))
    report('deck builder: text "dragon", type "creature"', lambda: cards.search_cards(text='dragon', type_text='creature'))
    report('deck builder: no text, type "planeswalker"', lambda: cards.search_cards(type_text='planeswalker'))
    report('printings of a card', lambda: cards.find_printings('Sol Ring'))
    names = [row[0] for row in cards.conn.execute('SELECT DISTINCT name FROM cards LIMIT 100')]
    report('100 cards by name (a deck)', lambda: cards.cards_by_names(names))

    # Would FTS5 with the trigram tokenizer answer the substring searches? (Only measured: it
    # would be one more structure to rebuild with every card data update.)
    try:
        cards.conn.execute("CREATE VIRTUAL TABLE temp.name_fts USING fts5(name_search, flavor_search, tokenize='trigram')")
        start = time.perf_counter()
        cards.conn.execute('INSERT INTO temp.name_fts (rowid, name_search, flavor_search) '
                           'SELECT rowid, name_search, COALESCE(flavor_search, \'\') FROM cards')
        built = (time.perf_counter() - start) * 1000
        fts = lambda text: cards.conn.execute(
            'SELECT c.* FROM temp.name_fts f JOIN cards c ON c.rowid = f.rowid WHERE f.name_fts MATCH ? LIMIT 10',
            ('"' + text + '"',)).fetchall()
        print(f"    FTS5 trigram index over the names: built in {built:.0f} ms")
        for text in ('dragon', 'mirkwood nurt', 'zzzqqqxxx'):
            _, warm = timed(lambda: fts(text))
            print(f"    FTS5 trigram \"{text}\": {warm:.2f} ms ({len(fts(text))} rows)")
        cards.conn.execute('DROP TABLE temp.name_fts')
    except sqlite3.OperationalError as e:
        print(f"    FTS5 trigram is not available in this SQLite: {e}")

    print('\n-- collection and decks (as they are) --')
    report('collection list', lambda: collection.get_all_cards('mtg'))
    report('collection totals', lambda: collection.get_stats('mtg'))
    report('copies owned by name', lambda: collection.owned_by_name('mtg'))
    report('deck list', lambda: decks.list_decks('mtg'), decks.conn,
           'SELECT d.* FROM decks d WHERE d.game = ? ORDER BY d.updated_at DESC, d.id DESC', ('mtg',))
    report('which decks need each card', lambda: decks.needed_by_name('mtg'))
    deck_id = decks.conn.execute('SELECT id FROM decks LIMIT 1').fetchone()
    if deck_id:
        report('one deck', lambda: decks.get(deck_id[0]), decks.conn,
               'SELECT * FROM deck_cards WHERE deck_id = ?', (deck_id[0],))
        unique = 'SELECT * FROM deck_cards INDEXED BY sqlite_autoindex_deck_cards_1 WHERE deck_id = ?'
        _, warm = timed(lambda: decks.conn.execute(unique, (deck_id[0],)).fetchall())
        print(f"    through the UNIQUE(deck_id, card_name, board) index instead of idx_deck_cards_deck: {warm:.2f} ms - "
              + plan(decks.conn, unique, (deck_id[0],)))

    print('\n-- scanned cards and review queue, at a size they do not have yet --')
    # 5,000 scanned entries from 4 cameras, 2,000 review items: far more than is ever waiting
    stamp = '2026-01-01 00:00:00'
    scan.conn.executemany(
        'INSERT INTO inventory (game, card_name, set_name, card_number, quantity, condition, finish, timestamp, location, tags) '
        "VALUES ('mtg', ?, 'Bench', ?, 2, 'Near Mint', 'regular', ?, '', '')",
        [(f'Bench card {n}', str(n), stamp) for n in range(5000)])
    ids = [row[0] for row in scan.conn.execute("SELECT id FROM inventory WHERE set_name = 'Bench'")]
    scan.conn.executemany('INSERT INTO inventory_sources VALUES (?, ?, 1)',
                          [(row_id, f'cam{number % 4}') for number, row_id in enumerate(ids)])
    scan.conn.commit()
    sql = ('SELECT i.*, s.quantity AS station_copies FROM inventory i JOIN inventory_sources s ON s.inventory_id = i.id '
           'WHERE i.game = ? AND s.station = ? AND s.quantity > 0 AND i.quantity > 0 ORDER BY i.timestamp DESC, i.id DESC')
    report("one camera's scanned cards (1,250 of 5,000)", lambda: scan.get_all_cards('mtg', 'cam1'), scan.conn, sql, ('mtg', 'cam1'))
    report("one camera's totals", lambda: scan.get_stats('mtg', 'cam1'))
    with_index(scan.conn, 'CREATE INDEX bench_sources_station ON inventory_sources(station, inventory_id)',
               'bench_sources_station', "one camera's totals", lambda: scan.get_stats('mtg', 'cam1'))

    review.conn.executemany(
        "INSERT INTO review_queue (game, file, ai_name, foil, created_at, station) VALUES ('mtg', NULL, 'Bench', 'unknown', ?, ?)",
        [(stamp, f'cam{n % 4}') for n in range(2000)])
    review.conn.commit()
    report("a camera's oldest review item + count (2,000 waiting)", lambda: review.first('mtg', 'cam1'), review.conn,
           'SELECT * FROM review_queue WHERE game = ? AND station IS ? ORDER BY id LIMIT 1', ('mtg', 'cam1'))
    with_index(review.conn, 'CREATE INDEX bench_review_station ON review_queue(game, station, id)',
               'bench_review_station', "a camera's oldest review item + count", lambda: review.first('mtg', 'cam1'))
    report('every review item count', lambda: review.count('mtg'))


if __name__ == '__main__':
    if len(sys.argv) != 2 or not (Path(sys.argv[1]) / 'cards_database.db').is_file():
        sys.exit(__doc__)
    main(sys.argv[1])
