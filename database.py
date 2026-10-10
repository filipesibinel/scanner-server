# ============================================================================
# FILE: database.py
# Database management for card data
# ============================================================================
import gzip
import re
import sqlite3
import json
import logging
import requests
import threading
from datetime import datetime
from difflib import SequenceMatcher, get_close_matches
import storage
from config import Config
from utils import normalize_text

# Create database logger
logger = logging.getLogger('database')

# Scryfall asks API clients to send a User-Agent and Accept header
SCRYFALL_HEADERS = {'User-Agent': 'CardScanner/1.0', 'Accept': 'application/json'}

# Cards table columns in canonical order (name, SQL type)
CARD_COLUMNS = [
    ('id', 'TEXT PRIMARY KEY'),
    ('name', 'TEXT NOT NULL'),
    ('flavor_name', 'TEXT'),
    ('set_code', 'TEXT'),
    ('set_name', 'TEXT'),
    ('collector_number', 'TEXT'),
    ('rarity', 'TEXT'),
    ('price_usd', 'REAL'),
    ('price_usd_foil', 'REAL'),
    ('image_uri', 'TEXT'),
    ('oracle_text', 'TEXT'),
    ('type_line', 'TEXT'),
    ('colors', 'TEXT'),
    ('mana_cost', 'TEXT'),
    # Printing treatment (from Scryfall). JSON arrays are stored as text.
    ('border_color', 'TEXT'),
    ('frame', 'TEXT'),
    ('frame_effects', 'TEXT'),
    ('full_art', 'INTEGER DEFAULT 0'),
    ('promo_types', 'TEXT'),
    ('finishes', 'TEXT'),
    ('released_at', 'TEXT'),
    # Lowercase, accent-free names for searching ("Fíli" -> "fili"), see search_key()
    ('name_search', 'TEXT'),
    ('flavor_search', 'TEXT'),
    # Deck building (from Scryfall; empty until the card data is downloaded again).
    # oracle_id is the same for every printing of a card; JSON is stored as text.
    ('oracle_id', 'TEXT'),
    ('cmc', 'REAL'),
    ('color_identity', 'TEXT'),
    ('legalities', 'TEXT'),
    ('keywords', 'TEXT'),
]
CARD_COLUMN_NAMES = [name for name, _ in CARD_COLUMNS]

# Manual-search treatment filters: key -> SQL condition on the cards table
TREATMENT_FILTERS = {
    'regular': ("COALESCE(border_color, '') != 'borderless' AND COALESCE(full_art, 0) = 0"
                " AND COALESCE(frame_effects, '') NOT LIKE '%\"showcase\"%'"
                " AND COALESCE(frame_effects, '') NOT LIKE '%\"extendedart\"%'"),
    'borderless': "border_color = 'borderless'",
    'showcase': "frame_effects LIKE '%\"showcase\"%'",
    'extendedart': "frame_effects LIKE '%\"extendedart\"%'",
    'fullart': "full_art = 1",
    'retro': "frame IN ('1993', '1997')",
    'etched': "finishes LIKE '%\"etched\"%'",
    'surgefoil': "promo_types LIKE '%\"surgefoil\"%'",
}

# Promo types worth showing as a treatment label
PROMO_TYPE_LABELS = {
    'surgefoil': 'Surge Foil',
    'galaxyfoil': 'Galaxy Foil',
    'textured': 'Textured',
    'serialized': 'Serialized',
}


# Matches that identify the exact printing (safe to add to the inventory without review)
CONFIRMED_MATCHES = {'set_number', 'name_number', 'name_set_digit'}


def collector_number_variants(collector_number):
    """
    Collector numbers a scanned/typed number may correspond to in Scryfall data
    ("0330" -> {"330", "0330", "330s", "0330s"}). Returns an empty set if no digits.
    """
    number_match = re.search(r'(\d+)', collector_number or '')
    if not number_match:
        return set()
    number_str = number_match.group(1)
    base_number = str(int(number_str))  # Remove leading zeros: "0330" -> "330"
    return {base_number, number_str, base_number + 's', number_str + 's'}


def _leading_number(collector_number):
    """Numeric part of a collector number ("0134" -> 134, "401z" -> 401), or None"""
    match = re.search(r'\d+', collector_number or '')
    return int(match.group()) if match else None


def _edit_distance(a, b):
    """Levenshtein distance between two short strings"""
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[-1]


def _digit_distance(read_number, collector_number):
    """
    How many digits a collector number read from a card is off from a printing's: one digit
    misread ("0189" for 0188, "6186" for 0186), dropped ("017" for 0117) or doubled. Compared as
    printed - modern cards show 4 digits with leading zeros - and without the zeros.
    """
    read = re.search(r'\d+', read_number or '')
    actual = _leading_number(collector_number)
    if not read or actual is None:
        return None
    read, actual = read.group(), str(actual)
    return min(_edit_distance(read, actual.zfill(4)), _edit_distance(read.lstrip('0') or '0', actual))


def search_key(text):
    """Normalize a card name for searching: lowercase, no accents ("Fíli" -> "fili", "Æther" -> "aether")"""
    if not text:
        return None
    return normalize_text(text).lower().replace('æ', 'ae').strip()


def names_match(query, row):
    """
    Loose check that a name read from a card matches a database row: same name, a
    shortened legendary name ("Thanos" / "Thanos, the Mad Titan"), a double-faced
    card's front face, or a close spelling. Checks the flavor name too.
    """
    query_key = search_key(query)
    if not query_key:
        return False
    for full_name in (row['name'], row['flavor_name']):
        for face in (full_name or '').split(' // '):
            key = search_key(face)
            if not key:
                continue
            if key == query_key or key.startswith(query_key) or query_key.startswith(key):
                return True
            if SequenceMatcher(None, query_key, key).ratio() >= 0.6:
                return True
            # A misread short name ("Thands") against a legendary name ("Thanos, the Mad Titan")
            short_name = key.split(',')[0]
            if short_name != key and SequenceMatcher(None, query_key.split(',')[0], short_name).ratio() >= 0.75:
                return True
    return False


def _json_list(value):
    """Decode a JSON array column, tolerating NULL/invalid values"""
    if not value:
        return []
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return []


def _json_dict(value):
    if not value:
        return {}
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


class CardDatabase:
    """Manages local card database"""

    def __init__(self, db_file=None):
        self.db_file = db_file or Config.DATABASE_FILE
        self._import_lock = threading.Lock()  # populate_database: one refresh at a time
        self.conn = None
        self._lock = threading.RLock()  # Thread-safe database access
        self.initialize_database()

    def initialize_database(self):
        """Create database tables if they don't exist"""
        logger.info(f"Initializing database: {self.db_file}")
        self.conn = storage.connect(self.db_file)  # WAL, rows by column name - as every manager's

        cursor = self.conn.cursor()
        self._create_cards_table(cursor, if_not_exists=True)

        # Migration: add any columns missing from older databases
        existing_columns = {row['name'] for row in cursor.execute("PRAGMA table_info(cards)").fetchall()}
        for column, column_type in CARD_COLUMNS:
            if column not in existing_columns:
                logger.info(f"Adding {column} column to existing cards table")
                cursor.execute(f"ALTER TABLE cards ADD COLUMN {column} {column_type}")

        if 'name_search' not in existing_columns:
            logger.info("Filling search name columns")
            self.conn.create_function('search_key', 1, search_key, deterministic=True)
            cursor.execute("UPDATE cards SET name_search = search_key(name), flavor_search = search_key(flavor_name)")

        self._create_card_indexes(cursor)

        # When each game's card data was downloaded, and the source's own date (update check)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS card_data_info (
                game TEXT PRIMARY KEY,
                source_updated TEXT,
                downloaded_at TEXT NOT NULL,
                card_count INTEGER
            )
        ''')

        # The inventory table (same file) is created and migrated by inventory.InventoryManager
        self.conn.commit()
        logger.info("Database tables and indexes initialized successfully")

    def _create_cards_table(self, cursor, table='cards', if_not_exists=False):
        """Create the cards table with the canonical column order"""
        columns_sql = ',\n                '.join(f"{name} {column_type}" for name, column_type in CARD_COLUMNS)
        cursor.execute(f'''
            CREATE TABLE {'IF NOT EXISTS ' if if_not_exists else ''}{table} (
                {columns_sql}
            )
        ''')

    def _create_card_indexes(self, cursor):
        """Create performance indexes on the cards table"""
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_card_name ON cards(name COLLATE NOCASE)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_card_flavor_name ON cards(flavor_name COLLATE NOCASE)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_card_name_search ON cards(name_search)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_card_flavor_search ON cards(flavor_search)')
        # Composite index for exact version lookups
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_card_set_number ON cards(set_code, collector_number)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_card_rarity ON cards(rarity)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_card_type ON cards(type_line)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_card_oracle ON cards(oracle_id)')

    def get_data_info(self, game):
        """{'source_updated', 'downloaded_at', 'card_count'} of a game's card data, or None"""
        with self._lock:
            row = self.conn.execute('SELECT * FROM card_data_info WHERE game = ?', (game,)).fetchone()
            return dict(row) if row else None

    def set_data_info(self, game, source_updated, card_count):
        with self._lock:
            self.conn.execute('INSERT OR REPLACE INTO card_data_info VALUES (?, ?, ?, ?)',
                              (game, source_updated, datetime.now().isoformat(timespec='seconds'), card_count))
            self.conn.commit()

    # replace_table: the card tables that may be replaced, and how much smaller than the table
    # in use an import may be (a download cut short must not replace a complete catalog)
    CARD_TABLES = {'cards': 'cards_import'}
    MIN_SHARE = 0.5

    def replace_table(self, staging, table, create_indexes=None, info=None):
        """
        Put a freshly filled staging table in place of a card table in one step, so searches
        never see a half-imported table (imports fill `staging`, committing as they go). One
        transaction: the old table is dropped, the new one renamed and indexed and - with info:
        (game, source_updated, card_count) - its card_data_info row written, or, when any of it
        fails, everything stays as it was. Refused before anything is touched: a staging table
        that is missing, empty, has repeated ids, or less than MIN_SHARE of the rows in use.
        """
        if self.CARD_TABLES.get(table) != staging:
            raise ValueError(f"Not a card table and its import table: {table}, {staging}")
        with self._lock:
            self.conn.commit()
            one = lambda sql, *values: self.conn.execute(sql, values).fetchone()
            exists = one("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", staging)
            rows = one(f'SELECT COUNT(*), COUNT(DISTINCT id) FROM {staging}') if exists else (0, 0)
            current = one(f'SELECT COUNT(*) FROM {table}')[0] if one(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", table) else 0
            problem = (f'{staging} is missing' if not exists else f'{staging} is empty' if not rows[0]
                       else 'card ids repeat' if rows[0] != rows[1]
                       else f'only {rows[0]} cards, {current} are in use' if rows[0] < current * self.MIN_SHARE else None)
            if problem:
                logger.error(f"Card data import refused ({problem}): {table} is unchanged")
                raise RuntimeError(f"The card data was not replaced: {problem}")
            cursor = self.conn.cursor()
            cursor.execute('BEGIN IMMEDIATE')  # explicit: Python's sqlite3 does not open one for DROP / ALTER
            try:
                cursor.execute(f'DROP TABLE IF EXISTS {table}')
                cursor.execute(f'ALTER TABLE {staging} RENAME TO {table}')
                if create_indexes:
                    create_indexes(cursor)
                if info:
                    cursor.execute('INSERT OR REPLACE INTO card_data_info VALUES (?, ?, ?, ?)',
                                   (info[0], info[1], datetime.now().isoformat(timespec='seconds'), info[2]))
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def fetch_scryfall_info(self):
        """Scryfall's bulk data description: download URL, size and updated_at"""
        response = requests.get(Config.SCRYFALL_BULK_URL, headers=SCRYFALL_HEADERS, timeout=30)
        if response.status_code != 200:
            raise Exception("Failed to fetch bulk data info")
        return response.json()

    def download_scryfall_data(self, progress_callback=None):
        """Download latest Scryfall bulk data"""
        if progress_callback:
            progress_callback("Fetching Scryfall bulk data information...")
        
        headers = SCRYFALL_HEADERS
        bulk_info = self.fetch_scryfall_info()
        self.last_download_source = bulk_info.get('updated_at')
        # Scryfall now publishes gzipped JSON Lines (jsonl_download_uri); older API used a JSON array (download_uri)
        download_url = bulk_info.get('jsonl_download_uri') or bulk_info.get('download_uri')
        if not download_url:
            raise Exception(f"Unexpected Scryfall bulk data response: {list(bulk_info.keys())}")
        file_size = (bulk_info.get('compressed_size') or bulk_info.get('size', 0)) / (1024 * 1024)

        if progress_callback:
            progress_callback(f"Downloading card database (~{file_size:.1f} MB)...")

        response = requests.get(download_url, headers=headers, stream=True, timeout=60)
        response.raise_for_status()
        total_size = int(response.headers.get('content-length', 0))

        # Collect raw bytes and decode once at the end - decoding per chunk
        # corrupts multi-byte UTF-8 characters split across chunk boundaries
        chunks = []
        downloaded = 0
        last_percent = -1

        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                downloaded += len(chunk)
                chunks.append(chunk)

                if total_size > 0 and progress_callback:
                    percent = int(downloaded * 100 / total_size)
                    if percent != last_percent:
                        last_percent = percent
                        progress_callback(f"Download progress: {percent}%")

        if progress_callback:
            progress_callback("Parsing card data...")

        data = b''.join(chunks)
        if data[:2] == b'\x1f\x8b':  # gzip magic bytes
            data = gzip.decompress(data)

        if '.jsonl' in download_url:
            return [json.loads(line) for line in data.splitlines() if line.strip()]
        return json.loads(data)
    
    def populate_database(self, cards_data, progress_callback=None):
        """Populate database with card data"""
        if progress_callback:
            progress_callback(f"Populating database with {len(cards_data)} cards...")
        
        # One refresh at a time: a second one would drop the table the first is filling
        if not self._import_lock.acquire(blocking=False):
            raise RuntimeError('The card data is being updated already')
        try:
            return self._populate(cards_data, progress_callback)
        finally:
            self._import_lock.release()

    def _populate(self, cards_data, progress_callback):
        # Filled beside the current table and swapped in at the end (replace_table): scanning
        # keeps working on the old data meanwhile. On a connection of its own - its commits
        # every few thousand rows must not commit whatever else has the shared one open
        importer = storage.connect(self.db_file)
        try:
            cursor = importer.cursor()
            cursor.execute('DROP TABLE IF EXISTS cards_import')
            self._create_cards_table(cursor, table='cards_import')
            importer.commit()
            inserted = self._fill_import(importer, cursor, cards_data, progress_callback)
        finally:
            importer.close()

        # The new cards and what is known about them go in together (or neither)
        self.replace_table('cards_import', 'cards', self._create_card_indexes,
                           info=('mtg', getattr(self, 'last_download_source', None), inserted))
        if progress_callback:
            progress_callback(f"Database populated with {inserted} cards!")
        return inserted

    def _fill_import(self, importer, cursor, cards_data, progress_callback):
        inserted = 0
        for card in cards_data:
            try:
                if card.get('layout') in ['token', 'emblem', 'art_series']:
                    continue
                
                prices = card.get('prices', {})
                price_usd = prices.get('usd')
                price_usd_foil = prices.get('usd_foil')
                
                # Double-faced cards keep their images on each face
                image_uris = card.get('image_uris') or (card.get('card_faces') or [{}])[0].get('image_uris', {})
                image_uri = image_uris.get('normal', '')

                cursor.execute(f'''
                    INSERT OR REPLACE INTO cards_import ({', '.join(CARD_COLUMN_NAMES)})
                    VALUES ({', '.join('?' * len(CARD_COLUMN_NAMES))})
                ''', (
                    card.get('id'),
                    card.get('name'),
                    card.get('flavor_name'),
                    card.get('set'),
                    card.get('set_name'),
                    card.get('collector_number'),
                    card.get('rarity'),
                    float(price_usd) if price_usd else None,
                    float(price_usd_foil) if price_usd_foil else None,
                    image_uri,
                    card.get('oracle_text'),
                    card.get('type_line'),
                    json.dumps(card.get('colors', [])),
                    card.get('mana_cost'),
                    card.get('border_color'),
                    card.get('frame'),
                    json.dumps(card.get('frame_effects', [])),
                    1 if card.get('full_art') else 0,
                    json.dumps(card.get('promo_types', [])),
                    json.dumps(card.get('finishes', [])),
                    card.get('released_at'),
                    search_key(card.get('name')),
                    search_key(card.get('flavor_name')),
                    card.get('oracle_id') or (card.get('card_faces') or [{}])[0].get('oracle_id'),
                    card.get('cmc'),
                    json.dumps(card.get('color_identity', [])),
                    # Only the formats a card is not "not_legal" in - a tenth of the text
                    json.dumps({name: status for name, status in (card.get('legalities') or {}).items()
                                if status != 'not_legal'}),
                    json.dumps(card.get('keywords', [])),
                ))
                
                inserted += 1
                if inserted % 5000 == 0:
                    # Short transactions: the inventory (own connection) can still write
                    importer.commit()
                    if progress_callback:
                        progress_callback(f"Inserted {inserted} cards...")
            
            except Exception as e:
                if progress_callback:
                    progress_callback(f"Error inserting card {card.get('name')}: {e}")
                continue
        
        importer.commit()
        return inserted
    
    def search_card_exact(self, card_name, collector_number=None, set_code=None):
        """
        Search for an exact printing by set code, collector number and name

        Strategy:
        1. Set code + collector number (unique per printing), if the name roughly matches
        2. Name (accent/case-insensitive, or shortened legendary name) + collector number
        3. Name only
        4. Fuzzy name match, then the collector number to pick the printing

        Args:
            card_name: The card name
            collector_number: Optional collector number (e.g., "123", "0330", "123s")
            set_code: Optional set code (e.g., "HOB")

        Returns:
            Card dict if found, None otherwise
        """
        with self._lock:
            logger.info(f"Searching for card: '{card_name}'" + (f" #{collector_number}" if collector_number else "")
                        + (f" [{set_code}]" if set_code else ""))
            cursor = self.conn.cursor()
            key = search_key(card_name)
            number_variants = collector_number_variants(collector_number)
            number_placeholders = ', '.join('?' * len(number_variants))

            # Step 1: Set code + collector number identify the printing exactly
            set_number_row = None
            if set_code and number_variants:
                set_number_row = self._find_by_set_number(set_code, number_variants)
                if set_number_row and names_match(card_name, set_number_row):
                    logger.info(f"Found by set + number: {set_number_row['name']} "
                                f"({set_number_row['set_code']} #{set_number_row['collector_number']})")
                    return self._tagged(set_number_row, 'set_number')
                if set_number_row:
                    logger.warning(f"{set_code} #{collector_number} is '{set_number_row['name']}', not '{card_name}' - searching by name")

            if key and number_variants:
                # Step 2: Name + collector number, then shortened name ("Thanos" for
                # "Thanos, the Mad Titan") + collector number
                for comparison, value in (('=', key), ('LIKE', key + '%')):
                    rows = cursor.execute(f'''
                        SELECT * FROM cards
                        WHERE (name_search {comparison} ? OR flavor_search {comparison} ?)
                        AND collector_number IN ({number_placeholders})
                    ''', (value, value, *number_variants)).fetchall()
                    if rows:
                        return self._by_name_number(rows, set_code)

            # Step 3: Name only
            match = self.search_card(card_name, fuzzy=True)

            # Fuzzy matching resolves the name; use the collector number to pick the printing
            if match and number_variants:
                rows = cursor.execute(f'''
                    SELECT * FROM cards WHERE name = ? AND collector_number IN ({number_placeholders})
                ''', (match['name'], *number_variants)).fetchall()
                if rows:
                    return self._by_name_number(rows, set_code)

            # Number didn't match (misread): prefer a printing from the same set, and the
            # printing whose collector number is closest to what was read
            if match and (set_code or number_variants):
                rows = cursor.execute('SELECT * FROM cards WHERE name = ?', (match['name'],)).fetchall()
                wanted_set = (set_code or '').strip().lower()
                in_set = [row for row in rows if row['set_code'] == wanted_set]
                candidates = in_set or rows
                read_number = _leading_number(collector_number)

                def closeness(row):
                    # (distance to the number read, collector number) - lowest wins
                    number = _leading_number(row['collector_number'])
                    if number is None:
                        return (float('inf'), float('inf'))
                    return (abs(number - read_number) if read_number is not None else 0, number)

                if candidates and (in_set or read_number is not None):
                    best = min(candidates, key=closeness)
                    # The name read exactly and exactly one printing of it in the set read one
                    # digit off the number read (a blurry "0188" read as "0189", "0117" read as
                    # "017"): that printing
                    one_off = [row for row in in_set
                               if _digit_distance(collector_number, row['collector_number']) == 1]
                    if match['match'] == 'name' and len(one_off) == 1:
                        return self._tagged(one_off[0], 'name_set_digit')
                    return self._tagged(best, 'name_set' if in_set else 'name')

            # Name not found at all (badly misread): trust the printed set + number
            if not match and set_number_row:
                logger.warning(f"No card named '{card_name}' - using {set_code} #{collector_number}: {set_number_row['name']}")
                return self._tagged(set_number_row, 'set_number_unverified')
            return match

    def _by_name_number(self, rows, set_code):
        """
        The printing for a name + collector number: confirmed ('name_number') only when it is
        the only one. Several sets print the same card under the same number (Solemn Offering
        #33 in M10 and M15, basic lands), and many of those cards carry no set code - then the
        likeliest printing is suggested for review ('name_number_ambiguous'): the set code
        closest to the one read, else the oldest (cards before 2014 have no printed set code)
        """
        if len(rows) == 1:
            logger.info(f"Found name match with collector number: {rows[0]['name']} #{rows[0]['collector_number']}")
            return self._tagged(rows[0], 'name_number')
        wanted_set = (set_code or '').strip().lower()
        best = min(rows, key=lambda row: (_edit_distance(wanted_set, row['set_code'] or '') if wanted_set else 0,
                                          row['released_at'] or ''))
        logger.warning(f"{len(rows)} printings of {best['name']} #{best['collector_number']} "
                       f"({', '.join(sorted({row['set_code'] or '?' for row in rows}))}) - not confirmed")
        return self._tagged(best, 'name_number_ambiguous')

    def _tagged(self, row, match):
        """
        Card dict plus how it was matched: 'set_number' / 'name_number' (the printing is
        confirmed - see CONFIRMED_MATCHES), 'name_set_digit' (confirmed too: the only printing
        of the name in the set read whose number is one digit off the one read),
        'name_number_ambiguous' (several printings share the name and number), 'name_set', 'name', 'fuzzy' or
        'set_number_unverified' (the printed set + number, name not recognized)
        """
        card = self._format_card_result(row)
        card['match'] = match
        return card

    def _find_by_set_number(self, set_code, number_variants):
        """Row for a set code + collector number (any of the number variants), or None"""
        placeholders = ', '.join('?' * len(number_variants))
        return self.conn.execute(f'''
            SELECT * FROM cards WHERE set_code = ? AND collector_number IN ({placeholders}) LIMIT 1
        ''', (set_code.strip().lower(), *number_variants)).fetchone()

    def get_card_by_set_number(self, set_code, collector_number, exact=False):
        """
        Card dict for a set code + collector number, or None. exact: the number as written
        ("M19-128", "12a": from another app's file) instead of what a read number may stand for
        """
        number_variants = [collector_number.strip()] if exact and collector_number \
            else collector_number_variants(collector_number)
        if not set_code or not number_variants:
            return None
        with self._lock:
            row = self._find_by_set_number(set_code, number_variants)
            return self._format_card_result(row) if row else None

    def search_card(self, card_name, fuzzy=True):
        """Search for a card by name or flavor name (case- and accent-insensitive)"""
        key = search_key(card_name)
        if not key:
            return None

        with self._lock:
            cursor = self.conn.cursor()

            # Exact name or flavor name
            result = cursor.execute('''
                SELECT * FROM cards WHERE name_search = ? OR flavor_search = ? LIMIT 1
            ''', (key, key)).fetchone()
            if result:
                return self._tagged(result, 'name')

            # Shortened legendary name ("Thanos" -> "Thanos, the Mad Titan") - fuzzy
            # matching on whole names would prefer unrelated cards like "Thayan Evokers"
            result = cursor.execute('''
                SELECT * FROM cards WHERE name_search LIKE ? OR flavor_search LIKE ? LIMIT 1
            ''', (key + ',%', key + ',%')).fetchone()
            if result:
                logger.info(f"Found card via shortened name: {card_name} -> {result['name']}")
                return self._tagged(result, 'name')

            if not fuzzy:
                return None

            # Fuzzy match against names sharing the first letters (widen if there are few)
            candidates = {}
            for prefix_length in (3, 2):
                rows = cursor.execute('''
                    SELECT DISTINCT name, flavor_name, name_search, flavor_search FROM cards
                    WHERE name_search LIKE ? OR flavor_search LIKE ?
                ''', (key[:prefix_length] + '%', key[:prefix_length] + '%')).fetchall()
                for row in rows:
                    candidates[row['name_search']] = row['name']
                    if row['flavor_search']:
                        candidates[row['flavor_search']] = row['name']  # flavor name -> card name
                if len(candidates) >= 50:
                    break

            matches = get_close_matches(key, candidates.keys(), n=1, cutoff=0.6)
            if matches:
                lookup_name = candidates[matches[0]]
                result = cursor.execute('SELECT * FROM cards WHERE name = ? LIMIT 1', (lookup_name,)).fetchone()
                if result:
                    logger.info(f"Found card via fuzzy match: {card_name} -> {lookup_name}")
                    return self._tagged(result, 'fuzzy')

            return None

    def search_cards_by_partial_name(self, partial_name, limit=10):
        """Search for cards with partial name match (searches both name and flavor_name)"""
        key = search_key(partial_name) or ''
        with self._lock:
            return self.conn.execute('''
                SELECT name, set_name, price_usd FROM cards
                WHERE name_search LIKE ? OR flavor_search LIKE ?
                ORDER BY name
                LIMIT ?
            ''', (f'%{key}%', f'%{key}%', limit)).fetchall()

    def _format_card_result(self, row):
        """Format database row as card dict"""
        card = {
            'id': row['id'],
            'name': row['name'],
            'flavor_name': row['flavor_name'],
            'set_code': row['set_code'],
            'set': row['set_name'],
            'number': row['collector_number'],
            'rarity': row['rarity'],
            'price': row['price_usd'] or 0.0,
            'price_foil': row['price_usd_foil'] or 0.0,
            'image_uri': row['image_uri'],
            'oracle_text': row['oracle_text'],
            'type_line': row['type_line'],
            'colors': _json_list(row['colors']),
            'mana_cost': row['mana_cost'] or '',
            'border_color': row['border_color'],
            'frame': row['frame'],
            'frame_effects': _json_list(row['frame_effects']),
            'full_art': bool(row['full_art']),
            'promo_types': _json_list(row['promo_types']),
            'finishes': _json_list(row['finishes']),
            'released_at': row['released_at'],
            'oracle_id': row['oracle_id'],
            'cmc': row['cmc'] or 0.0,
            'color_identity': _json_list(row['color_identity']),
            'legalities': _json_dict(row['legalities']),
        }
        card['treatments'] = self._treatment_labels(card)
        return card

    @staticmethod
    def _treatment_labels(card):
        """Human-readable treatment labels for a formatted card dict"""
        labels = []
        if card['border_color'] == 'borderless':
            labels.append('Borderless')
        if 'showcase' in card['frame_effects']:
            labels.append('Showcase')
        if 'extendedart' in card['frame_effects']:
            labels.append('Extended Art')
        if card['full_art']:
            labels.append('Full Art')
        if card['frame'] in ('1993', '1997'):
            labels.append('Retro Frame')
        if 'etched' in card['finishes']:
            labels.append('Etched')
        labels.extend(label for promo, label in PROMO_TYPE_LABELS.items() if promo in card['promo_types'])
        return labels

    def get_card_by_id(self, card_id):
        """Get a single printing by its Scryfall id"""
        with self._lock:
            row = self.conn.execute('SELECT * FROM cards WHERE id = ?', (card_id,)).fetchone()
            return self._format_card_result(row) if row else None

    # ------------------------------------------------------------------------
    # Collection page and deck builder
    # ------------------------------------------------------------------------

    def has_deck_data(self):
        """Whether the card data has mana values, color identities and legalities (card data
        downloaded before deck building existed doesn't, until the next update)"""
        with self._lock:
            return self.conn.execute('SELECT 1 FROM cards WHERE oracle_id IS NOT NULL LIMIT 1').fetchone() is not None

    def cards_by_ids(self, card_ids):
        """{printing id: card} for the ids that exist"""
        result = {}
        ids = [card_id for card_id in dict.fromkeys(card_ids) if card_id]
        with self._lock:
            for start in range(0, len(ids), 500):  # SQLite's parameter limit
                chunk = ids[start:start + 500]
                for row in self.conn.execute(
                        f"SELECT * FROM cards WHERE id IN ({', '.join('?' * len(chunk))})", chunk):
                    result[row['id']] = self._format_card_result(row)
        return result

    # One row per card name: with a single MAX(), SQLite takes the other columns from the row
    # that has it - the newest ordinary printing: one with an image, then one that exists
    # non-foil (foil-only inserts and collector editions are rarely the copy meant), then the
    # latest release. cheapest: lowest price of any printing
    _ONE_PER_NAME = """
        SELECT c.*, MAX((CASE WHEN image_uri != '' THEN '1' ELSE '0' END)
                        || (CASE WHEN finishes LIKE '%"nonfoil"%' THEN '1' ELSE '0' END)
                        || COALESCE(released_at, '')) AS _pick,
               (SELECT MIN(COALESCE(p.price_usd, p.price_usd_foil)) FROM cards p
                WHERE p.name_search = c.name_search) AS cheapest
        FROM cards c WHERE {where} GROUP BY c.name
    """

    def _named_card(self, row):
        card = self._format_card_result(row)
        card['cheapest'] = row['cheapest'] or 0.0
        return card

    def cards_by_names(self, names):
        """
        {search_key(name): card} - one representative printing per card (the newest), with
        'cheapest': the lowest price among its printings. Names are matched without case or
        accents; a double-faced card also by its front face ("Delver of Secrets")
        """
        keys = [key for key in dict.fromkeys(search_key(name) for name in names) if key]
        result = {}
        with self._lock:
            for start in range(0, len(keys), 500):
                chunk = keys[start:start + 500]
                for row in self.conn.execute(self._ONE_PER_NAME.format(
                        where=f"name_search IN ({', '.join('?' * len(chunk))})"), chunk):
                    result[row['name_search']] = self._named_card(row)
            for key in keys:
                if key not in result:
                    row = self.conn.execute(self._ONE_PER_NAME.format(where='name_search LIKE ?')
                                            + ' LIMIT 1', (key + ' // %',)).fetchone()
                    if row:
                        result[key] = self._named_card(row)
        return result

    def search_cards(self, text=None, type_text=None, oracle_text=None, identity=None, colors=None,
                     cmc=None, rarity=None, legal_in=None, names=None, exclude_names=None, commander=False,
                     limit=60, offset=0):
        """
        Cards (one per name) for the deck builder, by name.

        Args:
            text: part of the name; type_text / oracle_text: part of the type line / rules text
            identity: color letters ("WUB") the card's color identity must fit within
            colors: color letters the card's identity must include ("C" = colorless only)
            cmc: mana value (7 = seven or more)
            legal_in: Scryfall format key ("commander", "modern") the card is legal or restricted in
            names: only these card names (the "owned only" filter)
            exclude_names: not these card names (cards other decks already use)
            commander: only cards that can be a commander (as games.mtg_decks.can_be_commander:
                a legendary creature on the front face, or "can be your commander")
        Returns:
            (cards, whether there are more)
        """
        where, params = ["COALESCE(name, '') != ''"], []
        order, order_params = 'name', []
        if text and search_key(text):
            # The card with exactly this name first, then names starting with it
            order = '(name_search = ?) DESC, (name_search LIKE ?) DESC, name'
            order_params = [search_key(text), search_key(text) + '%']
            where.append('(name_search LIKE ? OR flavor_search LIKE ?)')
            params += [f'%{search_key(text)}%'] * 2
        if type_text:
            for word in type_text.split():
                where.append('type_line LIKE ?')
                params.append(f'%{word}%')
        if oracle_text:
            where.append('oracle_text LIKE ?')
            params.append(f'%{oracle_text}%')
        if identity is not None:
            for color in 'WUBRG':
                if color not in identity.upper():
                    where.append("COALESCE(color_identity, '') NOT LIKE ?")
                    params.append(f'%"{color}"%')
        if colors:
            if 'C' in colors.upper():
                where.append("color_identity = '[]'")
            for color in colors.upper():
                if color in 'WUBRG':
                    where.append('color_identity LIKE ?')
                    params.append(f'%"{color}"%')
        if cmc is not None:
            where.append('cmc >= ?' if cmc >= 7 else 'cmc = ?')
            params.append(cmc)
        if rarity:
            where.append('rarity = ?')
            params.append(rarity)
        if commander:
            front = "CASE WHEN instr(type_line, ' // ') > 0 THEN substr(type_line, 1, instr(type_line, ' // ') - 1) ELSE type_line END"
            where.append(f"(({front} LIKE '%Legendary%' AND {front} LIKE '%Creature%') "
                         "OR COALESCE(oracle_text, '') LIKE '%can be your commander%')")
        if legal_in:
            if not re.fullmatch(r'[a-z]+', legal_in):
                raise ValueError(f"Unknown format: {legal_in}")
            where.append(f"json_extract(legalities, '$.{legal_in}') IN ('legal', 'restricted')")
        if names is not None:
            keys = [search_key(name) for name in names]
            if not keys:
                return [], False
            where.append(f"name_search IN ({', '.join('?' * len(keys))})")
            params += keys
        if exclude_names:
            keys = [search_key(name) for name in exclude_names]
            where.append(f"name_search NOT IN ({', '.join('?' * len(keys))})")
            params += keys
        with self._lock:
            rows = self.conn.execute(
                self._ONE_PER_NAME.format(where=' AND '.join(where)) + f' ORDER BY {order} LIMIT ? OFFSET ?',
                (*params, *order_params, limit + 1, offset)).fetchall()
        return [self._named_card(row) for row in rows[:limit]], len(rows) > limit

    def find_printings(self, card_name, treatment=None, limit=200):
        """
        List all printings of a card, optionally filtered by treatment

        Args:
            card_name: Card name or flavor name (falls back to fuzzy match)
            treatment: Optional key from TREATMENT_FILTERS (e.g. 'borderless')
            limit: Maximum number of printings to return

        Returns:
            tuple: (resolved_name, printings) - resolved_name is None if the card
            was not found at all; printings may be empty if the treatment filter
            excluded every printing
        """
        with self._lock:
            key = search_key(card_name)
            name_condition = "(name_search = ? OR flavor_search = ?)"
            params = (key, key)

            exact = self.conn.execute(f"SELECT name FROM cards WHERE {name_condition} LIMIT 1", params).fetchone()
            if exact:
                resolved_name = exact['name']
            else:
                # Resolve typos/accents to the real card name
                match = self.search_card(card_name, fuzzy=True)
                if not match:
                    return None, []
                resolved_name = match['name']
                name_condition = "LOWER(name) = LOWER(?)"
                params = (resolved_name,)

            treatment_condition = ""
            if treatment:
                if treatment not in TREATMENT_FILTERS:
                    raise ValueError(f"Unknown treatment: {treatment}")
                treatment_condition = f" AND ({TREATMENT_FILTERS[treatment]})"

            rows = self.conn.execute(f'''
                SELECT * FROM cards
                WHERE {name_condition}{treatment_condition}
                ORDER BY released_at DESC, set_name, CAST(collector_number AS INTEGER)
                LIMIT ?
            ''', (*params, limit)).fetchall()

            return resolved_name, [self._format_card_result(row) for row in rows]

    def get_database_stats(self):
        """Get database statistics"""
        cursor = self.conn.cursor()

        # Get total cards
        try:
            cursor.execute('SELECT COUNT(*) FROM cards')
            result = cursor.fetchone()
            total_cards = result[0] if result else 0
        except Exception:
            total_cards = 0

        # Get cards with prices
        try:
            cursor.execute('SELECT COUNT(*) FROM cards WHERE price_usd IS NOT NULL')
            result = cursor.fetchone()
            cards_with_prices = result[0] if result else 0
        except Exception:
            cards_with_prices = 0

        return {
            'total_cards': total_cards,
            'cards_with_prices': cards_with_prices
        }

    def rebuild_database_schema(self, progress_callback=None):
        """
        Rebuild the cards table with the canonical column order and fresh indexes.

        Columns are copied by name, so this works for any older layout (e.g. databases
        where flavor_name or the treatment columns were appended by migration).
        The inventory table is not touched.
        """
        if progress_callback:
            progress_callback("Starting database schema rebuild...")

        logger.info("Beginning database schema rebuild")

        with self._lock:
            try:
                cursor = self.conn.cursor()
                existing_columns = {row['name'] for row in cursor.execute("PRAGMA table_info(cards)").fetchall()}
                shared_columns = ', '.join(c for c in CARD_COLUMN_NAMES if c in existing_columns)

                if progress_callback:
                    progress_callback("Copying card data into optimized table...")

                cursor.execute("DROP TABLE IF EXISTS cards_rebuild")
                self._create_cards_table(cursor, table='cards_rebuild')
                cursor.execute(f"INSERT INTO cards_rebuild ({shared_columns}) SELECT {shared_columns} FROM cards")
                imported = cursor.rowcount

                cursor.execute("DROP TABLE cards")
                cursor.execute("ALTER TABLE cards_rebuild RENAME TO cards")

                if progress_callback:
                    progress_callback("Rebuilding indexes...")
                self._create_card_indexes(cursor)

                self.conn.commit()

                if progress_callback:
                    progress_callback("Database rebuild complete!")
                logger.info(f"Database schema rebuild completed: {imported} cards")

                return {
                    'success': True,
                    'cards_imported': imported,
                    'schema_type': 'current'
                }

            except Exception as e:
                logger.exception(f"Database rebuild failed: {e}")
                self.conn.rollback()
                if progress_callback:
                    progress_callback(f"Rebuild failed: {str(e)}")
                return {
                    'success': False,
                    'error': str(e)
                }

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
