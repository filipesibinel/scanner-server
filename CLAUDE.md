# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Camera-based scanner for Magic: The Gathering cards (Flask + Socket.IO web app). Cards are
dropped onto a pile in a box; each new card is found by its outline (OpenCV), captured once,
identified by a vision AI (Gemini / OpenAI / Anthropic / local Ollama: name, collector number,
set code, ★/• foil marker), matched to the exact printing in a local Scryfall SQLite database,
and added to an inventory. Runs on a Raspberry Pi or any Linux PC (USB webcam or Pi camera).

The Android app (a Kotlin port of detection, auto-capture, AI, matching and the inventory) lives
in its own repository, [filipesibinel/mtg-scanner-android](https://github.com/filipesibinel/mtg-scanner-android)
(checked out beside this one, `../mtg-scanner-android`); its README maps each Kotlin file to the
Python code here - keep both in step when changing that logic.

**How everything works is documented in [PROGRAM_DOCUMENTATION.md](PROGRAM_DOCUMENTATION.md)**
(detection, auto-capture rules, matching, foil logic, schema, events, measured thresholds) -
read the relevant section before changing behavior, and keep it up to date.
User docs: [README.md](README.md); deployment: [INSTALL.md](INSTALL.md).

## Common Commands

```bash
./scripts/deploy.sh                       # install/repair: packages, venv, .env, camera check, card DB
./scripts/deploy.sh --service             # + systemd service (template: scripts/mtg-scanner.service)
./scripts/deploy.sh --update              # git pull + update + restart service
venv/bin/python app.py                    # run (http://localhost:5000)
venv/bin/python setup_database.py         # (re)download the Scryfall card database
venv/bin/python cleanup.py --stats        # scanned images; --days N / --dry-run / --all
docker compose up -d --build              # the server as a container (data/ and scanned_cards/ are volumes)
venv/bin/python station_client.py --server http://<server>:5000   # the camera, on the machine it is plugged into
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build   # ... with OCR on an NVIDIA GPU
```

Dependencies: `requirements.txt` (~300 MB, Python 3.10+). The dev venv on this machine is
Python 3.12.

There is no automated test suite. Verify changes by running the app (or a copy of it on another
port with a copy of the database - never test adds against the real `data/cards_database.db`
inventory or `data/scan_inventory.db`), and for scanner logic by feeding recorded/synthetic frames through `CardScanner`
with a fake camera (patch `detect_camera_type` / `_initialize_usb_camera`).

## Where Things Live

| Area | Code |
|---|---|
| Outline detection, warp | `object_detector.py`: `find_card_outline` (+ `_track_outline` following the previous card, `_outline_from_edge_groups` for broken outlines, `_card_inside_box` when the largest outline is the box itself), `warp_card`, `ObjectDetector.detect` |
| Capture loop, stability, auto-capture, new-card detection | `scanner.py`: `_capture_frames` (camera missing / lost: `_open_camera`, `_read_failed`, `_wait_for_camera`), `_is_card_settled`, `_new_card_arrived`, `_mark_captured`; fixed area (sleeves): `_fixed_area_step`, `set_fixed_area` |
| Focus sweep / lock / automatic refocus | `scanner.py`: `focus_sweep`, `refocus`, `_run_focus_sweep`, `_run_focus_probe` (drift tracking between drops), `_move_focus` (approach from below: the lens has play), `_check_focus_drift`, `set_continuous_autofocus` |
| OCR first (light-ocr reader process, parsers, AI fallback) | `card_ocr.py`: `CardOcr`, `parse_magic`, `PARSERS`; `ocr/server.mjs` (Node.js); `Game.confirmed_read` |
| Reading a card, for every camera: OCR stage, AI stage, their queues and workers, AI provider / OCR switch | `identification.py`: `Identification` (`submit` → `_ocr_worker` → `_ai_worker` → `_done_worker`; `identify` in the caller's thread; `set_ai_provider`, `set_ocr_enabled`, `last_capture`) |
| AI providers, foil check, Ollama warm-up | `card_identifier.py`: `_ask_*`, `identify_card`, `read_foil_symbol`, `warm_up` |
| Prompts (built-in + edited per model) | `prompts.py`: `BUILT_IN`, `prompt`, `save`, `reset`; editor events in `app.py` (`save_prompt`, `test_prompt`) |
| Card games (the active one drives search, finishes, exports / imports per site: `export_formats`, `import_rows`) | `games/`: `base.Game`, `mtg.Magic`, `games.active()`; plan in `MULTI_GAME_IMPLEMENTATION_PLAN.md` |
| Card data updates (staged import, update check) | `database.py`: `replace_table`, `card_data_info`; `Game.check_for_update`; `app.py`: `start_card_data_update`, `check_card_data_updates` |
| Card search / printing match / confidence (Magic) | `database.py`: `search_card_exact`, `search_card`, `find_printings`, `CONFIRMED_MATCHES`, `search_key`, `names_match` |
| The scanner page's camera at a station (server has none: `camera.type: remote`) | `station_client.py`: `Station` (runs `CardScanner`; `hello`, `report_loop`, `on_command`, `auto_captured`, `upload_loop`); `remote_scanner.py`: `RemoteScanner` (stands in for `CardScanner` in `app.py`; `METHODS`, `DECIDED`, `SENT`, `CAMERA_SETTINGS`, the `/station` namespace); `app.py`: `station_capture` (`camera`, `mode`, `capture_id`) |
| A page per camera: desks, rooms, the cameras' overview | `app.py`: `Desk`, `desk`, `desk_for`, `in_desk`, `emit_desk`, `make_remote_scanner`, `scan_page`, `handle_connect` (`page_stations`); `remote_scanner.py`: `CameraHub`; `stations.py`: `StationSettings`; `templates/stations.html`; `scanner.js`: `STATION` |
| Scanned cards by camera (filter, per-camera move to the collection) | `inventory.py`: `inventory_sources`, `_station_rows`, `stations_by_entry`, `get_all_cards(station)`, `get_stats(station)`, `take_from(station)`; `app.py`: `scan_camera`, `scan_stats`; `scanner.js`: `scannedCamera`, `loadInventoryCameras`; `common.js`: `addScannedToCollection(camera)` |
| Stations (cameras elsewhere that upload their captures) | `stations.py`: `Stations` (`data/stations.json`); `app.py`: `station_capture`, `station_capture_outcome`, `station_undo`, `station_list`, `station_item`; `scanner.js`: `loadStations`, `renderStations` (Settings) |
| Captures in the queues kept across a restart | `pending.py`: `PendingCaptures` (`data/pending_captures.db`); `app.py`: `submit_capture` (every queued capture goes through it), `resume_pending` |
| Capture orchestration, auto-add gate, events | `app.py`: `handle_auto_capture` (in `initialize_components`), `announce_and_route`, `route_identified` (returns the outcome), `search_and_emit_card`, `queue_changed`, `set_auto_add`; automatic adds on the server (`add_automatically`, `Game.suggested_finish`) |
| Review queue (unconfirmed cards while adding automatically) | `review.py` (`review_queue`, `data/review/`); `app.py`: `queue_for_review`, `review_open`/`review_skip`/`review_close`; `scanner.js`: `renderReview`, `reviewSearch` |
| Inventory add/merge/undo/split/export, locations and tags, bulk edits, capture thumbnails | `inventory.py` (`KEY_COLUMNS`, `update_card`, `bulk_update`, `inventory_captures`, `data/captures/`); capture → add: `app.py` `pending_capture`, `card['capture']`; `scan_location` |
| Collection page (`/collection`: inventory filters / grid / bulk bar, deck builder, statistics) | `templates/collection.html`, `static/js/collection.js`, `static/css/collection.css`; shared with the scanner page: `static/js/common.js`, `templates/_topbar.html` (the header of both pages), `templates/_dialogs.html`, `templates/_icons.html` |
| Backups of the collection, scanned cards and decks (collection page's settings drawer) | `backups.py`: `create`, `create_daily` (at startup, one per day), `restore`, `list_backups` (`data/backups/<date_time>/`); `app.py`: `collection_backups`, `restore_backup`; `collection.js`: `openSettings`, `renderBackups`, `backupAction` |
| Decks (lists; ownership computed from the inventory), deck checks, decklist text | `decks.py`: `DeckManager`; `games/mtg_decks.py`: `DECK_FORMATS`, `check_deck`, `parse_decklist`; `app.py`: `deck_payload`, `resolve_entries`; `database.py`: `search_cards`, `cards_by_names` |
| Deck ideas from other sites (EDHREC, MTGJSON, Archidekt, Moxfield) | `recommendations.py`: `Recommendations` (`_get` cache + throttle, `Unavailable`); `app.py`: `deck_suggestions`, `popular_decks`, `run_deck_ideas` |
| UI logic (finish suggestion, printing picker, status) | `static/js/scanner.js`: `suggestedFinish`, `displayCard`, `displayPrintings`, `updateDetectionStatus` |
| Settings | `config.yaml` (+ `config.py`), `.env` (API keys), `data/api_keys.env` (keys entered in the UI, `api_keys.py`), `data/settings.json` (UI choices: AI provider/model, `auto_add`, `focus_value`), `data/prompts.json` (edited prompts) |

## Conventions and Pitfalls

- **Schema**: cards columns are defined once in `database.py:CARD_COLUMNS`; missing columns are
  added on startup (`initialize_database`). Rows are `sqlite3.Row` - access by column name.
  New search-relevant columns may need filling in the migration (see `name_search`).
- **Match confidence**: `search_card_exact` tags results (`card['match']`); only the game's
  `confirmed_matches` (Magic: `CONFIRMED_MATCHES`) may be added without review - check with
  `game.is_confirmed(card)`. Keep new search paths tagged.
- **Games**: app code goes through `games.active()` (identify, find_printings, card_payload,
  inventory_fields, export_formats), never straight to `database`/`CardSearcher`. Inventory rows
  carry `game` and `finish` (a key of `game.finishes`) and are addressed by `id`; the inventory
  schema lives in `inventory.py`, not `database.py`.
- **Auto-capture thresholds** in `scanner.py` come from measured camera noise and a live drop
  test (documented in PROGRAM_DOCUMENTATION.md). Re-measure before changing them.
- **JSON from NumPy**: values sent through `jsonify`/Socket.IO must be plain Python types
  (`bool(...)`, `float(...)`) - a `numpy.bool_` broke `/api/detection_status` once.
- **Restart after template changes**: Flask caches `templates/scanner.html`; a running app keeps
  serving the old page. Static files are served fresh - bump the `?v=N` query in the template
  when changing CSS/JS so browsers don't use cached copies.
- **The camera is exclusive**: only one process can open it; stop the running app before
  testing with the real camera. Camera *controls* (`v4l2-ctl -c ...`) can be changed while
  another process streams - handy for focus experiments measured through `/video_feed`.
- **The camera may be missing**: the app must start and serve the collection without it.
  `scanner.camera_error` says why (also in `/api/detection_status`); `scanner.camera` is `None`
  then and the capture thread retries (`_open_camera`). Don't log per failed frame or retry.
- **Frame sizes**: with a raw-JPEG camera ≥ 1920 px wide, `current_frame`, detections, corners
  and the stream are **half size**; only captures decode full size (`get_detected_card`,
  `get_full_frame`). Don't crop captures from `current_frame` directly.
- **CPU**: `cv2.setNumThreads(2)` in scanner.py - OpenCV's default (all cores) doubled CPU use.
- **Focus timing**: a `focus_absolute` change takes ~0.4 s to show up in frames; measure after
  that, or sweeps score the previous lens position.
- **Ollama**: requests must send `think: false` (thinking models otherwise return empty answers)
  and `keep_alive`; the parser accepts answers with or without `NAME:/NUMBER:/SET:` labels.
- **OCR parser**: keep `parse_magic` strict - a loose collector-number pattern once confirmed
  the wrong printing. Re-run a batch of `scanned_cards/` through `CardOcr.read_card` +
  `Game.confirmed_read` (on a copy of the database) and compare with the AI before loosening it.
- **Thread safety**: frames/detection state under `scanner.frame_lock`; DB and inventory use
  their own `RLock`. Auto-capture callbacks run in their own threads; identified cards come back
  on `Identification`'s one results thread (`on_done`) - never route a card from the OCR / AI
  workers themselves. Station captures arrive on request threads, several at once.
- **Prompts**: change the built-in text in `prompts.py:BUILT_IN` (instructions + fixed
  `answer_format`); the parser relies on the answer format, which the editor cannot change.
  A user's saved prompt in `data/prompts.json` overrides built-in edits - check it when a prompt
  change seems to have no effect.
- **Never send full API keys to the browser** (no login on the web UI): `api_keys.credential_status()`
  masks them; the UI can only replace or remove a key.
- **No native `confirm()` / `alert()`** in the web UI: browsers can silently block them ("prevent
  this page from creating additional dialogs"), after which `confirm()` always returns false - this
  broke "Clear all". Use `confirmDialog()` / `choiceDialog()` / `notify()` in common.js.
- **Two pages share code**: helpers used by both the scanner and the collection page live in
  `static/js/common.js` / `templates/_dialogs.html`; the header is `templates/_topbar.html`
  (brand, page links and the settings button must stay in the same place on every page - only
  the `.topbar-page` part is a page's own; each page defines `openSettings()`); each page defines `inventoryChanged()`.
  `scanner.js` runs scanner-only code on load - never include it in another page.
- **`hidden` vs. display**: `.btn`, `.chip-toggle` etc. set `display`, which beats the `hidden`
  attribute; `collection.css` has `[hidden] { display: none !important }` for that page.
- **Two inventories**: `scan_inventory` (the scanner page: `data/scan_inventory.db`) and
  `inventory` (the collection). Scanning code adds to `scan_inventory`; decks, statistics and
  "owned" read `inventory`; REST routes pick with `inventory_area()` (`?area=scan`). The
  `stats` in `inventory_*` Socket.IO events are the scanned cards' (the scanner's top bar).
- **Inventory key**: `location` is part of `inventory.KEY_COLUMNS` (and the table's UNIQUE);
  anything that looks an entry up by its key must include it. Changing the key means rebuilding
  the table (`_migrate_add_location` is the pattern: backup, keep ids, check counts).
- **Other sites** (`recommendations.py`): only MTGJSON is a published API. Go through `_get`
  (cache, 1 request/s per site), catch shape changes and raise `Unavailable`; never call these
  from scanning code paths.
- **New Socket.IO events** need a handler in `app.py` and in `static/js/scanner.js`, and a line
  in PROGRAM_DOCUMENTATION.md.
- **One desk per camera** (`app.py`: `Desk`, `desk()`, `in_desk`, `desk_for`): the handlers
  look single-camera but `scanner` is a proxy for `desk().scanner`, and the card on a page,
  its open review and its capture are `desk().current_card_info` / `.current_review_id` /
  `.pending_capture` - never module globals again. Code that runs outside a page's request
  (threads, callbacks) must be inside `with in_desk(...)`, or it works for `default_desk`.
  `scanner` is never `None`-comparable (`if scanner:` works, `scanner is None` does not).
- **`emit_desk` vs `socketio.emit`**: what concerns one camera's card goes to its pages only
  (`emit_desk`); `socketio.emit` reaches every camera's page - right only for shared things.
  A scanner page gets `?station=` on every `/api/` request from the `fetch` wrapper at the top
  of `scanner.js`; a new route that depends on the camera needs nothing more than `desk()`.
- **Scanned cards and cameras**: entries merge across cameras; who scanned what is in
  `inventory_sources` / `inventory_captures.station` (`add_card(source=...)`). Anything that
  shows or moves "a camera's cards" goes through `_station_rows`; a filtered list's
  `quantity` is the camera's copies, not the entry's (`entry_quantity`).
- **Two programs now**: `app.py` is the server; `scanner.py` / `object_detector.py` run in
  `station_client.py` on the camera's machine (they still run inside `app.py` with a local
  camera, `camera.type` other than `remote`). Anything `app.py` reads or calls on `scanner`
  must also exist on `RemoteScanner` - a new attribute goes into the client's `state()` and
  one of `DECIDED` / `SENT` / `REPORTED`, a new method into `METHODS` on both sides. The client
  must not import server modules (`database`, `inventory`, `identification`, Flask).
- **Camera settings live on the server**: the client's `CardScanner` gets a `ServerSettings`
  (get / set) filled by `hello`; a new per-camera setting key must be added to
  `CAMERA_SETTINGS` or it is neither sent nor saved.
- **Docker image** (`Dockerfile`, target `server`; INSTALL.md "Server in Docker"): light-ocr's
  native library needs glibc 2.38+ (Debian 13 base images - on Debian 12 OCR silently fails to
  start), and NVIDIA's Vulkan driver needs the X11 / GLVND libraries plus
  `NVIDIA_DRIVER_CAPABILITIES=all`, or OCR falls back to the CPU. After changing the image,
  check `data/logs/ai.log` for `light-ocr ready (webgpu)`.
- **Logs**: `data/logs/app.log`, `ai.log`, `scanner.log`, `database.log`, `scanned_cards.log`; `requests.log` (web requests) only with Debug mode on.
