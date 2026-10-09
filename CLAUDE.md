# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Camera-based scanner for Magic: The Gathering cards, split into a **server** and **camera
stations**. Cards are dropped onto a pile in a box under a camera; a station finds each new
card by its outline (OpenCV), captures it once and uploads the picture; the server reads it
(light-ocr first, a vision AI - Gemini / OpenAI / Anthropic / local Ollama - for what OCR can't
confirm: name, collector number, set code, ★/• foil marker), matches the exact printing in a
local Scryfall SQLite database, and adds it to the scanned cards, from where it goes into the
collection.

| Program | Where it runs | Entry point |
|---|---|---|
| Server (Flask + Socket.IO web app; collection, decks, OCR, AI) | One machine, normally the Docker image (target `server`) | `app.py` |
| Camera station (camera, detection, auto-capture, focus) | Each machine with a USB webcam - PC, laptop, Raspberry Pi; Docker image (target `client`) or a venv | `station_client.py` |
| Android app, in client mode a station too | A phone | its own repository, [filipesibinel/mtg-scanner-android](https://github.com/filipesibinel/mtg-scanner-android) (checked out beside this one, `../mtg-scanner-android`) |

The Android app is a Kotlin port of detection and auto-capture (and, standalone, of AI,
matching and the inventory); its README maps each Kotlin file to the Python code here - keep
both in step when changing that logic, the upload's answer (`ServerOutcome.parse` there) or the
collection CSV (`ExportParityTest` there).

The single-machine scanner this grew from is a separate project
([filipesibinel/scanner](https://github.com/filipesibinel/scanner), `../scanner`): never push
to it or change it from here (the remote `desktop` is fetch-only on purpose).

The project is developed entirely with AI (this assistant, directed by the user), and the
documentation says so (README.md, *How this project was built*): keep that statement true and
in place, and keep marking in the docs what was measured and what was never tested.

**How everything works is documented in [PROGRAM_DOCUMENTATION.md](PROGRAM_DOCUMENTATION.md)**
(stations, detection, auto-capture rules, matching, foil logic, schema, events, measured
thresholds) - read the relevant section before changing behavior, and keep it up to date.
User docs: [README.md](README.md); installation: [INSTALL.md](INSTALL.md); manual test list:
[TEST_CASES.md](TEST_CASES.md).

## Common Commands

```bash
# server
docker compose up -d --build                                                   # data/ and scanned_cards/ are volumes
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build   # ... with OCR on an NVIDIA GPU
venv/bin/python app.py                    # without Docker (camera.type decides: remote, or a local camera)
venv/bin/python setup_database.py         # (re)download the Scryfall card database
venv/bin/python cleanup.py --stats        # capture images; --days N / --dry-run / --all

# camera station, on the machine the webcam is plugged into
docker compose -f docker-compose.client.yml up -d --build         # SCANNER_SERVER in .env
venv/bin/python station_client.py --server http://<server>:5000   # without Docker (requirements-client.txt)

# tests
venv/bin/python -m unittest discover tests

# single machine without Docker
./scripts/deploy.sh [--service | --update | --camera N]
```

Dependencies: `requirements.txt` (server, ~300 MB, Python 3.10+; the OCR reader also needs
Node.js 22+ and `npm install` in `ocr/`), `requirements-client.txt` (station). The dev venv on
this machine is Python 3.12 and has both.

**Testing.** `tests/test_ownership.py` covers which station scanned what (splits, merges,
moves, clears) and backups; `tests/test_database.py` the cases of DATABASE_REVIEW.md (failed
writes rolled back, reads that do not write, captures that count once, the card data swap, the
web cache) - all on temporary databases. Nothing else is automated: verify changes
by running the programs, and for scanner logic by feeding recorded / synthetic frames through
`CardScanner` with a fake camera (patch `detect_camera_type` / `_initialize_usb_camera`).
**Never test against a server's real `data/`** (adds, clears, forgets, imports, load tests):
use copies of the databases in a temporary folder - as the tests do, also inside the server
container - or a second container with its own data folder and port.

## Where Things Live

| Area | Code |
|---|---|
| **Server** | |
| One desk per camera: whose card, review and events this is | `app.py`: `Desk`, `desk`, `desk_for`, `in_desk`, `at_station`, `emit_desk`, the `scanner` proxy, `make_remote_scanner`, `handle_connect` (`page_stations`) |
| A station's capture arriving; kept until settled | `app.py`: `station_capture`, `submit_capture`, `resume_pending`, `capture_outcome_response`, `seen_captures`; `pending.py`: `PendingCaptures` (`settle`, `fail`, `settled`), `APPLIED_CAPTURES_TABLE` (`data/pending_captures.db`) |
| Reading a card: OCR stage, AI stage, their queues and workers, AI provider / OCR switch | `identification.py`: `Identification` (`submit` → `_ocr_worker` → `_ai_worker` → `_done_worker`; `identify` in the caller's thread; `set_ai_provider`, `set_ocr_enabled`, `last_capture`) |
| OCR reader process and parsers | `card_ocr.py`: `CardOcr`, `parse_magic`, `PARSERS`; `ocr/server.mjs` (Node.js); `Game.confirmed_read` |
| AI providers, foil check, Ollama warm-up | `card_identifier.py`: `_ask_*`, `identify_card`, `read_foil_symbol`, `warm_up` |
| Prompts (built-in + edited per model) | `prompts.py`: `BUILT_IN`, `prompt`, `save`, `reset`; editor events in `app.py` (`save_prompt`, `test_prompt`) |
| What becomes of a read card: lookup, automatic add, review, events | `app.py`: `announce_and_route`, `route_identified` (returns the outcome), `search_and_emit_card`, `add_automatically`, `queue_for_review`, `queue_changed`, `set_auto_add`; `Game.suggested_finish` |
| Stations: registry, per-station settings, list / rename / forget | `stations.py`: `Stations`, `StationSettings` (`data/stations.json`); `app.py`: `station_list`, `station_item`, `station_undo`, `clear_camera`; `scanner.js`: `loadStations`, `renderStations` |
| The server's stand-in for a station's camera; the stations' connection | `remote_scanner.py`: `RemoteScanner` (`METHODS`, `DECIDED`, `SENT`, `REPORTED`, `CAMERA_SETTINGS`), `CameraHub` (namespace `/station`) |
| Review queue (per station and game) | `review.py` (`review_queue`, `data/review/`, `ANY`, `clear`); `app.py`: `review_open` / `review_skip` / `review_close`; `scanner.js`: `renderReview`, `reviewSearch` |
| Card games (the active one drives search, finishes, exports / imports) | `games/`: `base.Game`, `mtg.Magic`, `games.active()`; plan in `MULTI_GAME_IMPLEMENTATION_PLAN.md` |
| Card data: schema, updates, search, printing match, confidence | `database.py`: `CARD_COLUMNS`, `replace_table`, `card_data_info`, `search_card_exact`, `search_card`, `find_printings`, `CONFIRMED_MATCHES`, `search_key`, `names_match`; `Game.check_for_update`; `app.py`: `start_card_data_update`, `check_card_data_updates` |
| Inventory: add / merge / undo / split / export, locations, tags, bulk edits, capture thumbnails | `inventory.py` (`KEY_COLUMNS`, `update_card`, `bulk_update`, `inventory_captures`, `data/captures/`); capture → add: `desk().pending_capture`, `card['capture']`; `scan_location` |
| Scanned cards by camera (filter, per-camera move, clear) | `inventory.py`: `inventory_sources`, `_station_rows`, `_move_sources`, `stations_by_entry`, `get_all_cards(station)`, `get_stats(station)`, `take_from(station)`, `clear_inventory(station)`; `app.py`: `scan_camera`, `scan_stats`; `scanner.js`: `scannedCamera`, `loadInventoryCameras`; `common.js`: `addScannedToCollection(camera)` |
| Backups of the collection, scanned cards and decks | `backups.py`: `create` (`keep`), `create_daily`, `restore`, `list_backups`, `INVENTORY_TABLES` (`data/backups/<date_time>/`); `app.py`: `collection_backups`, `restore_backup`; `collection.js`: `openSettings`, `renderBackups` |
| Decks, deck checks, decklist text | `decks.py`: `DeckManager`; `games/mtg_decks.py`: `DECK_FORMATS`, `check_deck`, `parse_decklist`; `app.py`: `deck_payload`, `resolve_entries`; `database.py`: `search_cards`, `cards_by_names` |
| Deck ideas from other sites (EDHREC, MTGJSON, Archidekt, Moxfield) | `recommendations.py`: `Recommendations` (`_get` cache + throttle, `Unavailable`); `app.py`: `deck_suggestions`, `popular_decks`, `run_deck_ideas` |
| Pages | `templates/stations.html` (the cameras), `scanner.html` + `static/js/scanner.js` (one camera: `STATION`, `suggestedFinish`, `displayCard`, `updateDetectionStatus`), `collection.html` + `collection.js`; shared: `common.js`, `_topbar.html`, `_dialogs.html`, `_icons.html` |
| **Camera station** | |
| Connection, status, preview, commands, uploads | `station_client.py`: `Station` (`hello`, `report_loop`, `on_command`, `on_set`, `auto_captured`, `manual_capture`, `upload_loop`), `ServerSettings` |
| Capture loop, stillness, auto-capture, new-card detection, fixed area | `scanner.py`: `_capture_frames` (camera missing / lost: `_open_camera`, `_read_failed`, `_wait_for_camera`), `_is_card_settled`, `_new_card_arrived`, `_mark_captured`, `_fixed_area_step`, `set_fixed_area` |
| Focus sweep / lock / drift tracking | `scanner.py`: `focus_sweep`, `refocus`, `_run_focus_sweep`, `_run_focus_probe`, `_move_focus` (approach from below: the lens has play), `_check_focus_drift`, `set_continuous_autofocus` |
| Outline detection, warp | `object_detector.py`: `find_card_outline` (+ `_track_outline`, `_outline_from_edge_groups`, `_card_inside_box`), `warp_card`, `ObjectDetector.detect` |
| **Both** | |
| Settings | `config.yaml` (+ `config.py`: also `SCANNER_CAMERA`, `SCANNER_CAMERA_INDEX`, `SCANNER_STATION_TOKEN`), `.env`; on the server `data/settings.json` (everyone's choices), `data/stations.json` (each station's), `data/api_keys.env`, `data/prompts.json` |
| Images | `Dockerfile` (targets `server`, `client`), `docker-compose.yml`, `docker-compose.gpu.yml`, `docker-compose.client.yml`, `scripts/docker-entrypoint.sh` |

## Conventions and Pitfalls

**Server and stations**

- **Two programs.** `scanner.py` / `object_detector.py` run in `station_client.py` (they still
  run inside `app.py` when `camera.type` is not `remote`). The client must not import server
  modules (`database`, `inventory`, `identification`, Flask), and its image copies a fixed list
  of files: a new module it imports must be added to the `COPY` line of the `client` target.
- **`RemoteScanner` mirrors `CardScanner`.** Anything `app.py` reads or calls on `scanner` must
  exist on both: a new attribute goes into the client's `state()` and one of `DECIDED` / `SENT`
  / `REPORTED`; a new method into `METHODS` on both sides.
- **Camera settings live on the server.** The client's `CardScanner` gets a `ServerSettings`
  filled by `hello`; a new per-camera setting key must be in `CAMERA_SETTINGS` or it is neither
  sent nor saved. On the server they are per station (`StationSettings`), falling back to
  `data/settings.json`.
- **One desk per camera.** The handlers look single-camera, but `scanner` is a proxy for
  `desk().scanner`, and the card on a page, its open review and its capture are
  `desk().current_card_info` / `.current_review_id` / `.pending_capture` - never module globals.
  Code that runs outside a page's request (threads, callbacks) must be inside
  `with in_desk(...)`, or it works for `default_desk`. `if scanner:` works; `scanner is None`
  does not.
- **`emit_desk` vs `socketio.emit`.** What concerns one camera's card goes to its pages only
  (`emit_desk`); `socketio.emit` reaches every page - right only for shared things. A scanner
  page gets `?station=` on every `/api/` request from the `fetch` wrapper at the top of
  `scanner.js`, so a new route that depends on the camera needs nothing more than `desk()`.
- **Every queued capture goes through `submit_capture`** (on record until settled, its desk
  remembered, counted). Cards come back on `Identification`'s one results thread (`on_done`) -
  never route a card from the OCR / AI workers themselves. Station captures arrive on request
  threads, several at once.
- **Uploads are "at least once", a capture counts once.** `capture_id` makes a repeat the same
  capture (`seen_captures`, and `settled_captures` across a restart); a capture's `uid` is noted
  by whatever its result writes (`add_card(capture_key=)`, `review.add(capture_key=)`), in the
  same commit. A new place a capture's result is written to must take `capture_key()` too. Keep
  both when touching `station_capture`, `submit_capture` or the clients' retry loops.
- **A change is whole or not at all.** Inventory methods that write are `@_writes` (rolled back
  when they fail) and commit through `_commit()` (thumbnails of deleted captures go only then);
  deck changes are inside `with self._writing()`. **Reads never write**: no DELETE / UPDATE in a
  method that only returns rows - it leaves a write transaction open and other connections get
  "database is locked". Entries are deleted together with `_drop_orphans()`; a quantity is
  lowered together with `_trim_sources()` (before `_trim_captures`).
- **Scanned cards and cameras.** Entries merge across cameras; who scanned what is beside them
  (`inventory_sources`, `inventory_captures.station`; `add_card(source=...)`). Anything that
  shows or moves "a camera's cards" goes through `_station_rows`; anything that moves copies
  between entries must call `_move_sources` (before the quantity and captures change); a new
  table beside the entries belongs in `backups.INVENTORY_TABLES`. A filtered list's `quantity`
  is the camera's copies, not the entry's (`entry_quantity`).
- **New Socket.IO events** need a handler in `app.py` and in `static/js/scanner.js`, and a line
  in PROGRAM_DOCUMENTATION.md. Messages between server and station are in `remote_scanner.py`
  and `station_client.py` - change both.
- **Stations are open without a token** (`stations.token`), like the web UI, which has no login.
  **Never send full API keys to the browser**: `api_keys.credential_status()` masks them; the UI
  can only replace or remove a key.
- **Docker images.** light-ocr's native library needs glibc 2.38+ (Debian 13 base images - on
  Debian 12 OCR silently fails to start), and NVIDIA's Vulkan driver needs the X11 / GLVND
  libraries plus `NVIDIA_DRIVER_CAPABILITIES=all`, or OCR falls back to the CPU. After changing
  the server image, check `data/logs/ai.log` for `light-ocr ready (webgpu)`.

**Card data, matching, inventory**

- **Schema**: cards columns are defined once in `database.py:CARD_COLUMNS`; missing columns are
  added on startup (`initialize_database`). Rows are `sqlite3.Row` - access by column name.
  New search-relevant columns may need filling in the migration (see `name_search`).
- **Match confidence**: `search_card_exact` tags results (`card['match']`); only the game's
  `confirmed_matches` (Magic: `CONFIRMED_MATCHES`) may be added without review - check with
  `game.is_confirmed(card)`. Keep new search paths tagged.
- **Games**: app code goes through `games.active()` (identify, find_printings, card_payload,
  inventory_fields, export_formats), never straight to `database` / `CardSearcher`. Inventory
  rows carry `game` and `finish` (a key of `game.finishes`) and are addressed by `id`; the
  inventory schema lives in `inventory.py`, not `database.py`.
- **Two inventories**: `scan_inventory` (`data/scan_inventory.db`: what stations add) and
  `inventory` (the collection). Decks, statistics and "owned" read `inventory`; REST routes pick
  with `inventory_area()` (`?area=scan`).
- **Inventory key**: `location` is part of `inventory.KEY_COLUMNS` (and the table's UNIQUE);
  anything that looks an entry up by its key must include it. Changing the key means rebuilding
  the table (`_migrate_add_location` is the pattern: backup, keep ids, check counts).
- **Moves to the collection are crash-safe** (`take_from`, `pending_moves` / `arrived_moves`,
  `finish_interrupted_moves`): two files, two commits - keep the note-first order.
- **OCR parser**: keep `parse_magic` strict - a loose collector-number pattern once confirmed
  the wrong printing. Re-run a batch of captures through `CardOcr.read_card` +
  `Game.confirmed_read` (on a copy of the database) and compare with the AI before loosening it.
- **Prompts**: change the built-in text in `prompts.py:BUILT_IN` (instructions + fixed
  `answer_format`); the parser relies on the answer format, which the editor cannot change.
  A user's saved prompt in `data/prompts.json` overrides built-in edits.
- **Ollama**: requests must send `think: false` (thinking models otherwise return empty answers)
  and `keep_alive`; the parser accepts answers with or without `NAME:/NUMBER:/SET:` labels.
- **Other sites** (`recommendations.py`): only MTGJSON is a published API. Go through `_get`
  (cache, 1 request/s per site), catch shape changes and raise `Unavailable`; never call these
  from scanning code paths. The cache is `data/web_cache.db`: disposable, bounded, in no backup -
  nothing that must be kept goes into it.

**Camera and detection** (the station)

- **Auto-capture thresholds** in `scanner.py` come from measured camera noise and a live drop
  test (documented in PROGRAM_DOCUMENTATION.md). Re-measure before changing them.
- **The camera is exclusive**: only one process can open it; stop the station before testing
  with the real camera. Camera *controls* (`v4l2-ctl -c ...`) can be changed while another
  process streams.
- **The camera may be missing**: a station must start and connect without it.
  `scanner.camera_error` says why (also in its status and `/api/detection_status`);
  `scanner.camera` is `None` then and the capture thread retries (`_open_camera`). Don't log
  per failed frame or retry.
- **Frame sizes**: with a raw-JPEG camera ≥ 1920 px wide, `current_frame`, detections, corners
  and the preview are **half size**; only captures decode full size (`get_detected_card`,
  `get_full_frame`). Don't crop captures from `current_frame` directly.
- **CPU**: `cv2.setNumThreads(2)` in scanner.py - OpenCV's default (all cores) doubled CPU use.
- **Focus timing**: a `focus_absolute` change takes ~0.4 s to show up in frames; measure after
  that, or sweeps score the previous lens position.
- **Thread safety**: frames / detection state under `scanner.frame_lock`. Auto-capture
  callbacks, uploads, focus sweeps and the server's commands each run in their own thread.

**Web UI**

- **JSON from NumPy**: values sent through `jsonify` / Socket.IO must be plain Python types
  (`bool(...)`, `float(...)`) - a `numpy.bool_` broke `/api/detection_status` once.
- **Restart after template changes**: Flask caches templates; a running server keeps serving
  the old page. Static files are served fresh - bump the `?v=N` query in the template when
  changing CSS / JS so browsers don't use cached copies.
- **No native `confirm()` / `alert()`**: browsers can silently block them, after which
  `confirm()` always returns false - this broke "Clear all". Use `confirmDialog()` /
  `choiceDialog()` / `notify()` in common.js.
- **Pages share code**: helpers used by more than one page live in `static/js/common.js` /
  `templates/_dialogs.html`; the header is `templates/_topbar.html` (brand, page links and the
  settings button stay in the same place on every page - only the `.topbar-page` part is a
  page's own; each page defines `openSettings()` and `inventoryChanged()`). `scanner.js` runs
  scanner-only code on load - never include it in another page.
- **`hidden` vs. display**: `.btn`, `.chip-toggle` etc. set `display`, which beats the `hidden`
  attribute; `collection.css` has `[hidden] { display: none !important }` for that page.

**Logs**: on the server `data/logs/app.log` (also every station's scanner lines), `ai.log`,
`database.log`, `scanned_cards.log`, `ocr.log`; `scanner.log` only with a camera on the server
itself; `requests.log` only with Debug mode on. A station logs to its console
(`docker compose -f docker-compose.client.yml logs`).
