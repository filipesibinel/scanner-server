Runtime data of the scanner server (not in git). With Docker this folder is a volume: it is
all that needs keeping - copy it, with the server stopped, to back everything up.

- cards_database.db    - Scryfall card data AND your collection (tables "inventory",
                         "inventory_captures"), decks and the review queue
- scan_inventory.db    - cards scanned and not yet added to the collection
- pending_captures.db  - captures still being read (queued again after a restart)
- stations.json        - the cameras: name, location and each one's settings
- settings.json        - choices made in the web interface for everyone (AI provider/model, OCR, sound)
- api_keys.env         - API keys entered in Settings; prompts.json - edited AI prompts
- captures/            - thumbnails of the captures behind inventory entries
- review/              - captures waiting in the review queue
- backups/             - backups of the collection, scanned cards and decks (made daily at
                         startup and from the collection page's Settings)
- logs/                - app.log, ai.log, database.log, scanned_cards.log, ocr.log

On a camera station this folder only holds debug frames, if debug trace is on.
