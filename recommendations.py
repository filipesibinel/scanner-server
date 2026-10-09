"""
Deck ideas from other sites, for the collection page's deck builder:

    EDHREC      cards played with a commander (inclusion %, synergy) and its average deck
    MTGJSON     preconstructed decks (open data)
    Archidekt   popular public decks of a commander or format; a deck by URL
    Moxfield    popular public decks of a format; a deck by URL

Only MTGJSON is meant to be read by programs; the others are the sites' own (undocumented)
JSON endpoints, so every answer is cached, requests are spaced out, and a site that fails or
changes raises Unavailable - the page then says so for that panel and everything else works.
Nothing here runs while scanning.

The cache is its own file, data/web_cache.db: it can be deleted at any time (everything in it
is fetched again when asked for), it is not part of any backup, and writing to it never waits
on - or holds up - the collection. Bodies are compressed, entries older than MAX_AGE go, and
the oldest make room when it grows past MAX_BYTES.
"""
import contextlib
import json
import logging
import re
import sqlite3
import threading
import time
import unicodedata
import zlib
from urllib.parse import quote, urlparse

import requests

from config import Config

logger = logging.getLogger('app')

HEADERS = {'User-Agent': 'CardScanner/1.0 (personal collection tool)', 'Accept': 'application/json'}
TIMEOUT = 10         # seconds per request
MIN_INTERVAL = 1.0   # seconds between two requests to the same site
SITE_INTERVALS = {'mtgjson.com': 0.25}  # a file server meant for downloads
DAY = 86400
CACHE_FILE = Config.DATA_DIR / 'web_cache.db'
MAX_AGE = 90 * DAY            # nothing is asked for with a longer max_age (precon lists: 90 days)
# Measured 2026-10-09 on the collection in use: 419 answers, 138 MB of JSON, the largest
# 0.98 MB (a precon list); compressed, 37 MB together. Ranking every precon reads ~230 lists
MAX_BYTES = 200 * 1024 ** 2   # of compressed bodies
MAX_ENTRY_BYTES = 8 * 1024 ** 2   # one answer, compressed (the largest seen: 0.98 MB before compression)
PRUNE_EVERY = 25              # writes between two clean-ups

EDHREC = 'https://json.edhrec.com/pages'
MTGJSON = 'https://mtgjson.com/api/v5'
ARCHIDEKT = 'https://archidekt.com/api/decks'
MOXFIELD = 'https://api2.moxfield.com'

# MTGJSON deck type -> deck format here (the products that are complete, playable decks)
PRECON_TYPES = {'Commander Deck': 'commander', 'Challenger Deck': 'standard',
                'Pioneer Challenger Deck': 'pioneer'}
# Archidekt's deckFormat numbers
ARCHIDEKT_FORMATS = {'standard': 1, 'modern': 2, 'commander': 3, 'legacy': 4, 'vintage': 5,
                     'pauper': 6, 'pioneer': 15}
MOXFIELD_BOARDS = {'commanders': 'commander', 'mainboard': 'main', 'sideboard': 'side',
                   'companions': 'side', 'maybeboard': 'maybe'}


class Unavailable(Exception):
    """A site didn't answer, or answered with something this code doesn't understand"""


def edhrec_slug(name):
    """ "Atraxa, Praetors' Voice" -> "atraxa-praetors-voice" (front face of a double-faced card)"""
    text = unicodedata.normalize('NFKD', name.split(' // ')[0])
    text = ''.join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^a-z0-9\s-]", '', text.replace('-', ' '))
    return '-'.join(text.split())


class Recommendations:
    def __init__(self, db_file=None, old_file=None):
        """old_file: the card database, where the cache was a table before it had its own file"""
        self.db_file = str(db_file or CACHE_FILE)
        self._lock = threading.RLock()
        self._site_locks = {}
        self._last_request = {}
        self._writes = 0
        with self._lock, self._connect() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('''CREATE TABLE IF NOT EXISTS web_cache (
                url TEXT PRIMARY KEY, fetched_at REAL NOT NULL, size INTEGER NOT NULL, body BLOB NOT NULL)''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_web_cache_age ON web_cache(fetched_at)')
        try:
            self._move_old_cache(str(old_file or Config.DATABASE_FILE))
        except sqlite3.Error as e:  # only a cache: what could not be moved is fetched again
            logger.warning(f"The web cache in the card database could not be moved: {e}")
        self.prune()

    @contextlib.contextmanager
    def _connect(self):
        """A connection for one piece of work: committed (rolled back on an error) and closed"""
        conn = sqlite3.connect(self.db_file, timeout=10.0)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _move_old_cache(self, old_file):
        """Once: the answers cached in the card database come over (compressed), and the table
        there goes - its pages are used again by that file; the file itself does not shrink"""
        if old_file == self.db_file:
            return
        old = sqlite3.connect(old_file, timeout=10.0)
        try:
            columns = [row[1] for row in old.execute('PRAGMA table_info(web_cache)')]
            if columns != ['url', 'fetched_at', 'body']:
                return
            moved = 0
            with self._lock, self._connect() as conn:
                for url, fetched_at, body in old.execute('SELECT url, fetched_at, body FROM web_cache'):
                    packed = zlib.compress(body.encode())
                    conn.execute('INSERT OR IGNORE INTO web_cache VALUES (?, ?, ?, ?)', (url, fetched_at, len(packed), packed))
                    moved += 1
            with self._connect() as conn:  # the copy is committed and can be read: only then the table goes
                if conn.execute('SELECT COUNT(*) FROM web_cache').fetchone()[0] < moved:
                    raise sqlite3.Error('the copied answers cannot all be read back')
            old.execute('DROP TABLE web_cache')
            old.commit()
            logger.info(f"Web cache moved to {self.db_file}: {moved} answers")
        finally:
            old.close()

    def prune(self):
        """Drop what is too old to be used again, then the oldest answers until the rest fits MAX_BYTES"""
        with self._lock, self._connect() as conn:
            conn.execute('DELETE FROM web_cache WHERE fetched_at < ?', (time.time() - MAX_AGE,))
            total = conn.execute('SELECT COALESCE(SUM(size), 0) FROM web_cache').fetchone()[0]
            if total > MAX_BYTES:
                for url, size in conn.execute('SELECT url, size FROM web_cache ORDER BY fetched_at').fetchall():
                    conn.execute('DELETE FROM web_cache WHERE url = ?', (url,))
                    total -= size
                    if total <= MAX_BYTES:
                        break

    # -- Fetching --------------------------------------------------------------

    def cached(self, url, max_age):
        """The cached answer for a URL when it is younger than max_age seconds, else None"""
        with self._lock, self._connect() as conn:
            row = conn.execute('SELECT fetched_at, body FROM web_cache WHERE url = ?', (url,)).fetchone()
        if row and time.time() - row[0] < max_age:
            try:
                return json.loads(zlib.decompress(row[1]))
            except (zlib.error, ValueError, TypeError):
                return None  # unreadable: fetched again
        return None

    def _store(self, url, data):
        # Only a cache: an answer that cannot be kept (too large, the file not writable) is
        # still the answer - it is asked for again next time
        try:
            packed = zlib.compress(json.dumps(data, separators=(',', ':')).encode())
            if len(packed) > MAX_ENTRY_BYTES:
                return
            with self._lock:
                with self._connect() as conn:
                    conn.execute('INSERT OR REPLACE INTO web_cache VALUES (?, ?, ?, ?)', (url, time.time(), len(packed), packed))
                self._writes += 1
                if self._writes % PRUNE_EVERY == 0:
                    self.prune()
        except (sqlite3.Error, OSError) as e:
            logger.warning(f"The answer of {url} could not be cached: {e}")

    def _get(self, url, max_age, timeout=TIMEOUT):
        """JSON at a URL: from the cache, or requested (one request at a time per site, spaced out)"""
        data = self.cached(url, max_age)
        if data is not None:
            return data
        site = urlparse(url).netloc
        with self._lock:
            site_lock = self._site_locks.setdefault(site, threading.Lock())
        with site_lock:
            data = self.cached(url, max_age)  # another thread may have fetched it meanwhile
            if data is not None:
                return data
            wait = self._last_request.get(site, 0) + SITE_INTERVALS.get(site, MIN_INTERVAL) - time.time()
            if wait > 0:
                time.sleep(wait)
            try:
                response = requests.get(url, headers=HEADERS, timeout=timeout)
                self._last_request[site] = time.time()
                if response.status_code in (403, 404):
                    data = {'_missing': True}  # remembered too: asking again won't help
                else:
                    response.raise_for_status()
                    data = response.json()
            except (requests.RequestException, ValueError) as e:
                self._last_request[site] = time.time()
                logger.warning(f"{site} not available: {e}")
                raise Unavailable(f"{site} is not available right now") from e
        self._store(url, data)
        return data

    # -- EDHREC ----------------------------------------------------------------

    def _edhrec_page(self, kind, commanders):
        """A commander's (or commander pair's) page of a kind ('commanders', 'average-decks'), or None"""
        slug = '-'.join(edhrec_slug(name) for name in commanders)
        for _ in range(2):  # a pair is filed under one order of the two names; the other redirects
            page = self._get(f"{EDHREC}/{kind}/{slug}.json", 7 * DAY)
            if page.get('redirect'):
                slug = page['redirect'].rstrip('/').split('/')[-1]
                continue
            return None if page.get('_missing') else page
        return None

    def commander_cards(self, commanders):
        """
        Cards played with a commander: {'decks': number of decks counted, 'url', 'categories':
        [{'title', 'cards': [{'name', 'inclusion' (% of decks), 'synergy' (%), 'decks'}]}]},
        or None when EDHREC has no page for it
        """
        page = self._edhrec_page('commanders', commanders)
        if not page:
            return None
        try:
            lists = page['container']['json_dict']['cardlists']
            categories, total = [], 0
            for card_list in lists:
                cards = []
                for view in card_list['cardviews']:
                    potential = view.get('potential_decks') or 0
                    total = max(total, potential)
                    cards.append({
                        'name': view['name'],
                        'decks': view.get('num_decks') or 0,
                        'inclusion': round(100 * (view.get('num_decks') or 0) / potential) if potential else 0,
                        'synergy': round(100 * (view.get('synergy') or 0)),
                    })
                if cards:
                    categories.append({'title': card_list.get('header') or 'Cards', 'cards': cards})
        except (KeyError, TypeError) as e:
            raise Unavailable("EDHREC's data has changed - suggestions are not available") from e
        slug = '-'.join(edhrec_slug(name) for name in commanders)
        return {'decks': total, 'url': f"https://edhrec.com/commanders/{slug}", 'categories': categories}

    def average_deck(self, commanders):
        """EDHREC's average deck for a commander: [{'name', 'quantity', 'board'}], or None"""
        page = self._edhrec_page('average-decks', commanders)
        if not page:
            return None
        try:
            deck = page['deck']
            entries = [{'name': name, 'quantity': 1, 'board': 'commander'} for name in deck['commander']]
            for cards in deck['cards'].values():
                entries += [{'name': name, 'quantity': int(count), 'board': 'main'} for name, count in cards]
        except (KeyError, TypeError, ValueError) as e:
            raise Unavailable("EDHREC's data has changed - the average deck is not available") from e
        return entries

    # -- MTGJSON ---------------------------------------------------------------

    def precon_list(self):
        """Preconstructed decks, newest first: [{'file', 'name', 'type', 'format', 'released', 'set'}]"""
        try:
            decks = self._get(f"{MTGJSON}/DeckList.json", 7 * DAY)['data']
            precons = [{'file': deck['fileName'], 'name': deck['name'], 'type': deck['type'],
                        'format': PRECON_TYPES[deck['type']], 'released': deck.get('releaseDate') or '',
                        'set': deck.get('code') or ''}
                       for deck in decks if deck.get('type') in PRECON_TYPES]
        except (KeyError, TypeError) as e:
            raise Unavailable("MTGJSON's deck list has changed - precons are not available") from e
        return sorted(precons, key=lambda deck: deck['released'], reverse=True)

    def precon(self, file_name):
        """A preconstructed deck's cards: [{'name', 'quantity', 'board', and the printing in the
        box: 'scryfall_id', 'set', 'number', 'foil'}], or None when there is no such deck"""
        if not re.fullmatch(r'[A-Za-z0-9_\-]+', file_name or ''):
            raise ValueError('Unknown deck')
        page = self._get(f"{MTGJSON}/decks/{file_name}.json", 90 * DAY)  # printed decks don't change
        if page.get('_missing'):
            return None
        try:
            deck = page['data']
            entries = []
            for key, board in (('commander', 'commander'), ('mainBoard', 'main'), ('sideBoard', 'side')):
                entries += [{'name': card['name'], 'quantity': int(card.get('count') or 1), 'board': board,
                             'scryfall_id': (card.get('identifiers') or {}).get('scryfallId'),
                             'set': (card.get('setCode') or '').lower(), 'number': card.get('number') or '',
                             'foil': bool(card.get('isFoil'))}
                            for card in deck.get(key) or []]
        except (KeyError, TypeError, ValueError) as e:
            raise Unavailable('MTGJSON does not have this deck') from e
        return entries

    # -- Archidekt / Moxfield --------------------------------------------------

    def popular_decks(self, commander=None, deck_format=None, limit=12, card=None):
        """
        Most viewed public decks with a commander, with a card in a format (both Archidekt -
        Moxfield's search can't be narrowed by card name) or of a format (Archidekt and Moxfield): ([{'source', 'name', 'author', 'views', 'cards', 'url'}], [messages about
        sites that didn't answer])
        """
        decks, problems = [], []
        try:
            query = f"commanderName={quote(commander)}&deckFormat={ARCHIDEKT_FORMATS['commander']}" if commander \
                else f"cardName={quote(card)}&deckFormat={ARCHIDEKT_FORMATS[deck_format]}" if card \
                else f"deckFormat={ARCHIDEKT_FORMATS[deck_format]}"
            # A search by card can take Archidekt half a minute the first time; it runs in the
            # background (with a Stop button), so it gets the time
            page = self._get(f"{ARCHIDEKT}/v3/?{query}&orderBy=-viewCount&pageSize={limit}", DAY,
                             timeout=45 if card else TIMEOUT)
            for deck in page['results'][:limit]:
                decks.append({'source': 'Archidekt', 'name': deck['name'],
                              'author': (deck.get('owner') or {}).get('username') or '',
                              'views': deck.get('viewCount') or 0, 'cards': deck.get('size') or 0,
                              'url': f"https://archidekt.com/decks/{deck['id']}"})
        except Unavailable as e:
            problems.append(str(e))
        except (KeyError, TypeError):
            problems.append("Archidekt's data has changed - its decks are not available")
        if not commander and not card:  # Moxfield's search can't be narrowed to a card by name
            try:
                page = self._get(f"{MOXFIELD}/v2/decks/search?pageNumber=1&pageSize={limit}&fmt={quote(deck_format)}"
                                 "&sortType=views&sortDirection=Descending", DAY)
                for deck in page['data'][:limit]:
                    decks.append({'source': 'Moxfield', 'name': deck['name'],
                                  'author': (deck.get('createdByUser') or {}).get('userName') or '',
                                  'views': deck.get('viewCount') or 0, 'cards': deck.get('mainboardCount') or 0,
                                  'url': deck['publicUrl']})
            except Unavailable as e:
                problems.append(str(e))
            except (KeyError, TypeError):
                problems.append("Moxfield's data has changed - its decks are not available")
        return sorted(decks, key=lambda deck: deck['views'], reverse=True), problems

    def deck_from_url(self, url):
        """
        A public Moxfield or Archidekt deck: {'name', 'format' (the site's name for it, may be
        None), 'entries': [{'name', 'quantity', 'board'}]} - board 'maybe' for cards outside the deck
        """
        moxfield = re.search(r'moxfield\.com/decks/([A-Za-z0-9_\-]+)', url or '')
        archidekt = re.search(r'archidekt\.com/(?:api/)?decks/(\d+)', url or '')
        if moxfield:
            return self._moxfield_deck(moxfield.group(1))
        if archidekt:
            return self._archidekt_deck(archidekt.group(1))
        raise ValueError('Paste the address of a Moxfield or Archidekt deck (moxfield.com/decks/..., archidekt.com/decks/...)')

    def _moxfield_deck(self, public_id):
        deck = self._get(f"{MOXFIELD}/v3/decks/all/{public_id}", DAY)
        if deck.get('_missing'):
            raise Unavailable('Moxfield does not show this deck (private, deleted, or access is blocked)')
        try:
            entries = [{'name': card['card']['name'], 'quantity': int(card.get('quantity') or 1), 'board': board,
                        'scryfall_id': card['card'].get('scryfall_id')}
                       for key, board in MOXFIELD_BOARDS.items()
                       for card in (deck['boards'].get(key) or {}).get('cards', {}).values()]
            return {'name': deck['name'], 'format': (deck.get('format') or '').lower() or None, 'entries': entries}
        except (KeyError, TypeError, ValueError) as e:
            raise Unavailable("Moxfield's data has changed - the deck could not be read") from e

    def _archidekt_deck(self, deck_id):
        deck = self._get(f"{ARCHIDEKT}/{deck_id}/", DAY)
        if deck.get('_missing'):
            raise Unavailable('Archidekt does not show this deck (private or deleted)')
        try:
            outside = {category['name'] for category in deck.get('categories') or []
                       if not category.get('includedInDeck', True)}
            entries = []
            for card in deck['cards']:
                category = (card.get('categories') or [''])[0]
                board = 'commander' if category == 'Commander' else 'side' if category == 'Sideboard' \
                    else 'maybe' if category in outside else 'main'
                entries.append({'name': card['card']['oracleCard']['name'],
                                'quantity': int(card.get('quantity') or 1), 'board': board,
                                'scryfall_id': card['card'].get('uid')})
            formats = {number: name for name, number in ARCHIDEKT_FORMATS.items()}
            return {'name': deck['name'], 'format': formats.get(deck.get('deckFormat')), 'entries': entries}
        except (KeyError, TypeError, ValueError) as e:
            raise Unavailable("Archidekt's data has changed - the deck could not be read") from e
