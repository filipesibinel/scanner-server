# Card Scanner - Technical Documentation

How the scanner works inside. For what it is and how to use it see [README.md](../README.md); for
installing the server, the camera stations and the phone app see [INSTALL.md](INSTALL.md).

The program and this document were written by an AI coding assistant (Claude, through Claude
Code), directed by the project's author, who ran it on real hardware with real cards. The
numbers below - thresholds, timings, counts - come from those runs and from recorded sessions;
each says when it was measured, and "not tested" marks what never was.

## Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Stations](#stations)
4. [Card detection](#card-detection)
5. [Auto-capture](#auto-capture)
6. [Identification (vision AI)](#identification-vision-ai)
7. [Matching the printing](#matching-the-printing)
8. [Foil and finish](#foil-and-finish)
9. [Database](#database)
10. [Focus](#focus)
11. [Collection page and decks](#collection-page-and-decks)
12. [Web interface](#web-interface)
13. [Configuration and files](#configuration-and-files)
14. [Performance](#performance)
15. [Tests](#tests)
16. [Known limitations](#known-limitations)

## Overview

The scanner turns a camera pointed into a box into a card-cataloguing station: cards are dropped
onto a pile, each new card is detected, captured once, identified down to the exact printing
and finish, and added to an inventory. It is split in two:

- **Camera stations** - a small computer with a USB webcam (a PC, a laptop, a Raspberry Pi), or
  a phone - find the card, decide when to capture it, and send one picture per card.
- **One server** reads the pictures (OCR, then a vision AI), matches the printing, and keeps
  everything that lasts: the card data, the scanned cards, the collection, the decks, the
  settings. Its web pages are the only user interface of the computer stations.

Design choices:

- **Classic computer vision for detection.** A card is found by its outline, not by an object
  detection model: fast (milliseconds, fine on a Raspberry Pi), exact corners for a flat
  perspective-corrected crop, and unaffected by foil glare.
- **Detection runs next to the camera.** Stillness and "is this a new card?" are judged frame
  by frame at camera speed, and the lens is moved between frames; over a network both would
  depend on its jitter. A station therefore sends one image per card (and a small preview),
  not a video stream - about 1 Mbit/s, fine on Wi-Fi - and the server does no image work per
  frame, whatever the number of cameras.
- **Reading is shared.** One OCR reader and one AI configuration serve every station, each
  behind its own queue. The AI only reads text: the name, collector number and set code from
  the crop, and the ★/• foil marker from a zoomed corner. Any provider works: Gemini, OpenAI,
  Anthropic, or a local Ollama model.
- **The local Scryfall database decides.** What was read is matched against 110,000+ printings;
  set code + collector number identify a printing exactly, and only confirmed matches are added
  without review.
- **A station keeps nothing.** Its camera settings are saved on the server, and a captured
  image is deleted once the server has it. A station can be replaced or reinstalled without
  losing anything.

## Architecture

### The programs

| Program | Runs on | What it does |
|---|---|---|
| `app.py` - the server (the package `server/`) | One machine; a Docker image (`deploy/Dockerfile` target `server`) | Web pages, reading (OCR + AI), matching, card data, scanned cards, collection, decks, backups; holds every station's settings |
| `station.py` - a camera station (the package `card_scanner/station/`) | The machine a USB webcam is plugged into; a Docker image (target `client`, x86-64 and ARM64) | Camera, outline detection, stillness, new-card rules, focus; uploads each capture; shows nothing itself |
| The Android app ([mtg-scanner-android](https://github.com/filipesibinel/mtg-scanner-android)) in client mode | A phone over the box | The same job with the phone's camera (a Kotlin port of the detection); uploads each capture. It can also work on its own, without a server |

The server opens no camera itself. Until 2026-10-12 it could (`camera.type` other than
`remote`, as the single-machine scanner this grew from): that path was removed - every
machine in use already had its camera at a station, and it was the one part of start-up that
the tests of the server never ran. A machine that is server and camera in one runs both
programs (`scripts/start.sh`, or the two services of `scripts/deploy.sh --service`).
Everything below about detection, auto-capture and focus is `CardScanner`, which runs inside
`station_client.py`.

### Modules

All the code is one package, `src/card_scanner/`, sorted by which program a module belongs
to: `station/` (a camera station: `station_client.py`, `station_setup.py`, `station_login.py`,
`scanner.py`, `object_detector.py`), `core/` (reading cards, card data, the collection - no
web app, no camera), `server/` (the web app, with its pages in `server/templates/` and
`server/static/` - written `templates/...` and `static/...` in this document), `games/`, and
at the package's top what both programs use (`paths.py`, `config.py`, `config_loader.py`,
`settings.py`). Modules are named by their file below (`inventory.py` is
`src/card_scanner/core/inventory.py`). The OCR reader (Node.js) is beside the package, in
`src/ocr/`. The top folder holds the two launchers - `app.py` starts the server, `station.py`
a station - `config.yaml` and the data (`data/`, `scanned_cards/`, `station_captures/`);
`paths.py` says where the folders are (`TOP`: what ships; `USER_DIR`: the person's data,
`.env` and own `config.yaml` - the same folder in a checkout, `~/.mtg-scanner` when
packaged, `MTG_SCANNER_HOME`; `CODE`), and nothing else builds a path
from where its own file lies. One server runs per data folder (`data/app.lock`). Which part may import which is a test (`tests/test_layout.py`):
a station imports nothing of `core/` or `server/`, and `core/` nothing of `server/` or
`station/`.

| Module | Runs in | Responsibility |
|---|---|---|
| `app.py`, `server/` | server | `app.py` only starts it; `server/` is the Flask + Socket.IO app, a module per area (listed in `server/__init__.py`): pages, routes, events; one desk per station (`Desk`, `desk()`); station captures (`station_capture`, `submit_capture`); what happens to a read card (`route_identified`) |
| `identification.py` | server | Reading a card, for every camera: light-ocr, the vision AI, the queue in front of each (`Identification`) |
| `card_ocr.py`, `ocr/server.mjs` | server | light-ocr reader: the Node.js process that runs the OCR models (kept running, one request per line), and the parsers that pick name, number, set code and foil marker from the text lines |
| `card_identifier.py` | server | Vision AI providers (`_ask_*`), response parsing, foil marker check, model warm-up |
| `prompts.py` | server | Built-in prompts and the ones edited in Settings (`data/prompts.json`), per model |
| `stations.py` | server | The stations (`data/stations.json`): name, location, counters, and each one's settings (`StationSettings`) |
| `remote_scanner.py` | server | `RemoteScanner`: what the server holds in place of a station's `CardScanner`; `CameraHub`: the `/station` Socket.IO namespace the clients connect to |
| `pending.py` | server | Captures in the queues, on disk until settled, and what became of them (`data/pending_captures.db`); the `applied_captures` table the inventory and review queue note applied captures in |
| `review.py` | server | The review queue (`review_queue` table, `data/review/`), per station |
| `database.py` | server | Scryfall download and import, schema / migrations, searches, printing lookup, match confidence |
| `games/` | server | Card games: `base.Game` (the interface the app uses), `mtg.Magic` (Scryfall data, matching, finishes, exports); `games.active()` is the game being scanned |
| `card_search.py` | server | Magic search helpers combining name, number, set and treatment |
| `trades.py` | server | Cards of the collection set aside for a trade until it is confirmed (`Trades`); the tables and the triggers that keep the copies are in `inventory.py` |
| `inventory.py` | server | Inventory table for every game: schema + migration, add (merging duplicates), undo, edit / split, bulk edits, locations and tags, which station scanned what, move to the collection, CSV import / export |
| `decks.py`, `games/mtg_decks.py` | server | Decks (`decks`, `deck_cards`): lists of card names; formats, deck checks, text decklists |
| `recommendations.py` | server | Deck ideas from EDHREC, MTGJSON, Archidekt and Moxfield, cached in `data/web_cache.db` |
| `backups.py` | server | Backups of the collection, the scanned cards and the decks; full backups of every database file at one moment |
| `storage.py` | server | What every database file has in common: connection setup, named migrations, table rebuilds |
| `settings.py` | server | Choices made in the web interface, in `data/settings.json` |
| `cleanup.py`, `setup_database.py` | server | Old capture images (on startup and as a CLI); downloading the card database |
| `station_client.py` | station | `Station`: connects to the server, runs a `CardScanner`, reports status and preview, executes the server's commands, uploads captures |
| `scanner.py` | station | `CardScanner`: camera (USB via OpenCV / V4L2, or Pi camera), capture thread, detection state, stillness, auto-capture, fixed area, focus |
| `object_detector.py` | station (or server) | Outline detection (`find_card_outline`), perspective warp (`warp_card`) |
| `config.py`, `config_loader.py` | both | Settings from `config.yaml` and the environment |
| `templates/`, `static/` | server | The pages: `stations.html` (the cameras), `scanner.html` (one camera's page), `collection.html`; shared `_topbar.html`, `_dialogs.html`, `_icons.html`, `common.js` |
| `tests/` | - | `test_ownership.py`: which station scanned what, and backups (see [Tests](#tests)) |

A station must not import the server's modules: its image contains only `station_client.py`,
`scanner.py`, `object_detector.py`, `settings.py`, `config.py`, `config_loader.py` and
`config.yaml`.

### Threads

Server:

| Thread | What it does |
|---|---|
| Main | Flask + Socket.IO (threading mode): one thread per request - pages, REST, Socket.IO events of the pages and of the stations, the MJPEG streams, capture uploads (several at once) |
| OCR worker (`Identification._ocr_worker`) | Reads queued captures with light-ocr, one at a time; passes on what it can't settle |
| AI workers (`Identification._ai_worker`, `vision_ai.workers`, default 3) | Ask the vision AI about the cards OCR passed on, several at once |
| Identified worker (`Identification._done_worker`) | Hands each read card back - lookup, add or review queue (`route_identified`) - one at a time |
| Background tasks | Card database update / rebuild, startup image cleanup, model warm-up, card data update check (10 s after startup, then daily), "What can I build?" searches |

Station (`station_client.py`):

| Thread | What it does |
|---|---|
| Main (`Station.report_loop`) | Status every 0.2 s; the preview while a page is watched |
| Capture (`CardScanner._capture_frames`) | Reads frames, detects the card, tracks stillness, draws the overlay, triggers auto-captures |
| Auto-capture callback (`Station.auto_captured`) | One short-lived thread per capture: crops and saves the card, waits for a focus probe, sends the beep, queues the upload |
| Uploads (`Station.upload_loop`) | Sends captures in order; retries until the server has each |
| Socket.IO client | The server's commands (`on_command`, `on_set`); focus sweeps and probes run in their own threads |

Frames and detection state are shared under `scanner.frame_lock`; the card database, each
inventory, the review queue, the stations and the pending captures each have their own lock
around a SQLite connection or file. On the server, anything that concerns one station's card
runs "at its desk" (`in_desk`), which decides where its events and log lines go - see
[A page per camera](#a-page-per-camera).

**No camera.** A station starts without one: `CardScanner._open_camera` logs one warning and
sets `camera_error`, and the capture thread tries again every `CAMERA_RETRY_SECONDS` (3 s),
silently, until it opens - no restart. On Linux a missing `/dev/videoN` is seen before OpenCV is
asked (which would print its own errors at each try); a device that opens but sends no first
frame counts as no camera too. While running, `CAMERA_LOST_AFTER` (20, ~2 s) failed reads in a
row (`_read_failed`: unplugged) close the camera, clear the frame and go back to waiting - one
warning instead of an error per frame. `camera_error` is part of the station's status and of
`/api/detection_status`: the station's page covers the video with the reason and disables the
capture button. **No station** is the same to the page: `RemoteScanner.camera_error` then says
that no camera station is connected.

### Flow of a card

```
 CAMERA STATION                                   SERVER
 camera frame ─> outline ─> settled? ─> new card?
                                            │ capture: flat, perspective-corrected card image
                                            │ (the beep: drop the next card)
                                            └── upload ──────────> saved, on record until settled
                                                                          │
                                  OCR queue: light-ocr reads name, collector number, set code, ★/•
                                     confirmed printing? done (the AI only for a missed ★/•)
                                                                          │ otherwise
                                  AI queue: vision AI reads name, number, set code (+ ★/• corner)
                                                                          │
                                  card data: set + number (checked against the name)
                                             → name + number → name → fuzzy name
                                                                          │
                                  confirmed printing ──> scanned cards (with Undo), the station's page is told
                                  uncertain ───────────> the station's review queue; scanning goes on
                                                                          │
                                  "Add to collection" ──> the collection (all cameras, or one camera's)
```

With "Add cards automatically" off (a computer station only), a capture is read at once and
shown on the station's page, and the station holds its next capture until **Add** or **Skip**.

## Stations

A station is a camera somewhere: it finds and captures the card itself and sends the picture
to the server, which does everything after that. There are two kinds, and they use the same
upload:

| | Camera station (`station_client.py`) | Capture-only station (the Android app, or any program) |
|---|---|---|
| Sends captures | `POST /api/stations/<id>/captures` | The same |
| Stays connected | Yes: Socket.IO namespace `/station` - status, live view, commands | No |
| Its page on the server | Live view, capture button, auto scanning, focus, fixed area, rotation | The same page without the camera panel |
| User interface | The server's page | Its own (the phone's screen) |

A station is known by an **id** it chooses and keeps (1-40 letters, digits, `-`, `_`). It
appears the first time it connects or sends a card (`stations.py`, `data/stations.json`: name,
location, capture count, last seen, whether it has a camera the server can show, its settings).

### Sending a capture

`POST /api/stations/<id>/captures` (multipart; `server/stations_api.py: station_capture`):

| Field | |
|---|---|
| `image` | The card as the station cut it out (JPEG as it is, or PNG) |
| `foil_image` | Optional: the perspective-corrected card, for the ★/• foil check by the AI - when `image` is something else (a fixed area) |
| `foil_is_image` | Optional, `1`: `image` is the perspective-corrected card itself, so no `foil_image` is needed |
| `name` | Optional: what the station calls itself (used until it is renamed on the server) |
| `wait` | Optional: seconds to wait for the outcome (default 30, at most 120; 0 answers at once) |
| `capture_id` | Optional: the station's own id for this capture (up to 64 characters). Sent again - no answer came the first time - it is the same capture, answered with its outcome, not a second card |
| `mode` | `station_client.py` only: `review` = read it now and show it on the station's page instead of adding it (a manual capture, or auto scanning that waits for Add / Skip) |

The image is saved in `scanned_cards/` as `<id>_<n>_<time>.jpg` and goes through the two
queues (see [Identification](#identification-vision-ai)), always as when adding automatically:
a confirmed printing is added to the scanned cards, anything else goes to the station's review
queue. The answer:

```
{"capture": 12, "status": "added", "seconds": 0.31, "sync_key": "5f0c...",
 "card": {"name": ..., "set": ..., "number": ..., "finish": ..., "quantity": 1},
 "read": {"name": ..., "number": ..., "set": ..., "foil": ..., "reader": "light-ocr"}}
{"capture": 13, "status": "review", "reason": "printing not confirmed" | "not found" | "not read" | "game switched", ...}
```

or `202` with `"status": "pending"` when the card isn't settled within `wait` (it still is,
afterwards). Errors: `400` (bad id, no readable picture), `401` (token), `413` (over 30 MB).
Every settled answer - this one, the same capture sent again, and the outcome asked for later,
also after a restart - has `capture` (the station's capture number) and `sync_key` (the
capture's identity on the server; see [Capture synchronization](#capture-synchronization)).

| Also | |
|---|---|
| `GET /api/stations/<id>/captures/<n>` | The outcome of a capture that was still pending (`202` while it still is). Outcomes of the last 500 captures are kept in memory: `404` after a restart |
| `GET /api/stations/<id>/captures/<n>/sync` | Where that capture's copy is now: `{"success": true, "state": "pending" \| "scanned" \| "review" \| "resolved"}` (below) |
| `DELETE /api/stations/<id>/captures/<n>/sync` | Remove that capture's copy from the scanned cards or the review queue - never from the collection: `"state": "removed"`, or `"pending"` / `"resolved"` when there was nothing to remove |
| `POST /api/stations/<id>/undo` | Take back the station's last automatic add |
| `GET /api/stations` | The stations, with whether each is connected, sees a card, is scanning, and how many cards it has in review |
| `PUT /api/stations/<id>` | Rename it (`name`) or set where its cards are put (`location`) |
| `DELETE /api/stations/<id>` | Forget it - after clearing its scanned cards and review items (`clear_camera`, every game), so nothing of it is left where no page shows it. Cards already in the collection stay |

#### Capture synchronization

A phone keeps a row for every card it scanned, and the two sides are kept in step through
the capture: removing a row on the phone removes that card on the server, and a card that
left the scanned cards (moved into the collection, or removed on a page) disappears from the
phone. The Android app asks `GET .../sync` about every 10 s for each row that has a
`sync_key`, and sends `DELETE .../sync` when a row is removed (`station_capture_sync`).

| `state` | Meaning |
|---|---|
| `pending` | Still being read - or failed and kept to be read again at the next start. A `DELETE` removes nothing and says `pending` too: the phone asks again |
| `scanned` | Its copy is in the scanned cards |
| `review` | It waits in the review queue |
| `resolved` | Known, and in neither place: moved into the collection, or removed, skipped or cleared on the server. Also the answer to a `DELETE` repeated after `removed` |
| `removed` | This `DELETE` took it out |

- **The state is the one of this moment**, not what the capture first became: queued for review
  and added from there it is `scanned`; once in the collection, `resolved`.
- **Exactly this copy.** Each photographed copy in the scanned cards is a row of
  `inventory_captures`, which now carries the capture's uid (`capture_key`; the uid is the one
  from `pending.py` that already makes a capture count once, and it is the `sync_key`). The
  row stays with its copy when an entry is split, merged or given another printing
  (`_move_captures`), so `InventoryManager.captured_copy` finds the entry the copy is in now
  and removes that photo, one copy, and the station's claim on it - of three identical cards
  scanned in a row, the one named. A review item carries the same uid (`review_queue.capture_key`),
  and the card added from it takes it along (`handle_add_inventory`).
- **The page's Add and a station's removal take turns** (`maintenance_barrier`, held by
  `handle_add_inventory` and by the route). Without it an Add could read which review item is
  open, the station's `DELETE` then remove that item and answer `removed`, and the Add still
  put the card into the scanned cards. Everything the Add reads about the page is read under
  the barrier: after a removal there is no card on the page to add, and an item that left the
  queue some other way is refused ("no longer in the review queue") instead of added again.
- **A reviewed capture stays its station's card**, whichever page works through the review:
  the page without a station lists every station's items, and a card added there is counted
  as the capture's station's (`source`), not as nobody's - or its phone could never follow it.
- **The collection is out of reach.** `take_from` copies the photos without the key and deletes
  the rows in the scanned cards: afterwards there is no row to find, in either file. A `DELETE`
  then answers `resolved` and changes nothing.
- **Unknown is never `resolved`.** A capture the server has no outcome for - a number the
  station never had, or one settled more than 30 days ago (`pending.KEEP_DAYS`) - is `404`. A
  capture it knows but cannot follow is `409` with a message: settled before this was added
  (no `sync_key`), failed for good, shown on the camera's page instead of added (`mode:
  review`), added without a photo (`unfollowed_captures`: the thumbnail could not be made), or
  a copy that is another station's. The phone keeps its row in both cases.
- **Removed stays removed.** The uid stays in `applied_captures`: the capture sent or read
  again after its copy went adds nothing.
- **Numbers can come round again.** The capture is named by station and number, and a station
  forgotten on the server (`DELETE /api/stations/<id>`) counts from 1 again: a row the phone
  kept from before then names a new capture. `?sync_key=<key>` on either request makes it
  exact - a number that belongs to another capture now is refused (`409`). The Android app
  sends it. A row whose capture is answered `404` / `409` can never be kept in step: the app
  then offers to take it off the phone only, on a second Remove.
- **Pages**: a removal from the scanned cards sends `inventory_updated` (`cleared: true`) to every
  page, which count and list again; one from the review queue sends `review_queue_update` to the
  station's page, and closes the item there if it was the one open (`review_item` with
  `id: null`) - or its Add would still add the card.
- **Migration**: two columns that may be empty (`inventory_captures.capture_key`,
  `review_queue.capture_key`), a unique index on each and one small table
  (`unfollowed_captures`) are added in place at the first start; no table is rebuilt. Tried on
  copies of the real databases (5,407 cards in 2,151 entries, 4,107 photos, 15 decks, 147
  settled outcomes): every row of every table was the same before and after. What was scanned
  before has no `sync_key` and stays as it is: the phone is answered `409` for it.
- Tested (`tests/test_capture_sync.py`, 39 cases on temporary databases, the routes through
  Flask's test client and the page's Add through Socket.IO's): a removal arriving in the
  middle of an Add (it waits, then removes the card), a review worked through on the page of
  every station, duplicates, repeated removal, two stations, pending and failed
  captures, restarts, review removal and hand-off, splits, merges, printing changes, the move
  into the collection with a location (collection rows compared before and after a `DELETE`),
  forgotten outcomes, a number used again. Also run once on a local server with test data
  (2026-10-12), with real uploads: two pictures of the same card and one nothing can be read
  from; the answers carried `capture` and `sync_key` (also for the capture sent again, before
  and after a restart); one of the two identical cards removed, a second `DELETE` `resolved`;
  the review item added through the page's events (`review_open`, `search_card`,
  `select_printing`, `add_to_inventory`) read `scanned` and was removed; the last card moved
  into the collection with a location read `resolved`, and a `DELETE` left the collection's
  count unchanged. **Not tried**: against the Android app, or on the real server.

- **Token**: with `stations.token` in `config.yaml` (or `SCANNER_STATION_TOKEN`) set, uploads,
  the outcome, the capture synchronization, undo and the `/station` connection need it (header `X-Station-Token`, or `token`
  in the connection's `auth`). Without it anyone on the network can send cards - like the web
  interface itself, which has no login.
- **Order**: a card the AI had to read is added after cards dropped later that OCR confirmed.
  The entry gets the time its capture arrived (`add_card(when=...)`), so the scanned list stays
  in dropping order.
- **Location**: a station's cards get its own location (the page's *Scan into location*).
- **Undo** is kept per station (`InventoryManager.last_added[source]`): one station's Undo never
  takes back another's card.
- **The Android app** in client mode (`server/ScannerServer.kt` there): uploads each capture
  with a `capture_id`, shows the answer, asks again while it is pending and uses the undo
  endpoint. Change the answer's fields and its `ServerOutcome.parse` (and `ServerOutcomeTest`)
  must follow.

**Captures survive a restart.** Every capture that goes into the queues is on record in
`data/pending_captures.db` (`pending.py`, written by `submit_capture`) from the moment it is
accepted until it is added or in the review queue; its image is in `scanned_cards/` (a
separate foil image as `<name>_foil.jpg`, deleted afterwards). At startup `resume_pending`
queues what is still on record. Measured 2026-10-08: container killed (`docker kill`) with 110
captures on record; after the restart all were added, 120 cards for 119 accepted uploads - the
extra card was one added just before the kill and not yet taken off the record (then added
again), which is what the next paragraph is about.

**A capture counts once** (since 2026-10-09, after plans/DATABASE_REVIEW.md):
- Each capture on record has a `uid`. The add (`InventoryManager.add_card(capture_key=)`) or
  the review item (`ReviewQueue.add(capture_key=)`) notes it in its own commit, in a table
  `applied_captures` of the database it writes to; a capture read again after a crash between
  that commit and its removal from the record finds its uid there and changes nothing.
  `submit_capture` hands the uid down through `settling` (thread-local) / `capture_key()`.
- What became of an uploaded capture is written to `settled_captures` (in
  `pending_captures.db`) in the commit that takes it off the record (`PendingCaptures.settle`).
  A station that sends the same `capture_id` again - also after a restart, when
  `seen_captures` (this run's memory) is empty - gets that answer; asking and claiming a
  `capture_id` is one step (`capture_accept_lock`), so two uploads of it at once are one capture.
- A capture whose result could not be applied stays on record and is tried at the next starts,
  three times in all (`PendingCaptures.fail`, `MAX_ATTEMPTS`); then it is settled as an error.
- A sender's `capture_id` can be on record once (a unique index), whatever asks first.
- The station deletes a picture only when the server's answer names the capture
  (`station_client.upload_verdict`: 200 / 202 with `capture`); 400 / 413 from the server mean
  it will never be taken (kept, not sent again); everything else - a wrong token, a proxy's
  page - is tried again. It used to delete on any answer below 500.
- Applied uids and outcomes are kept 30 days (`KEEP_DAYS`). `applied_captures` is not part of a
  backup and stays when cards move to the collection or a backup is restored.

Verified 2026-10-09 on an isolated copy of the server (copies of the databases, another port):
one upload sent twice, another sent twice at the same moment, the process killed (`kill -9`),
the first capture put back on the record as a crash would leave it, restart - 2 cards for 2
captures, and both repeats answered from the record. **Not tested**: a kill at exactly the
moment between the two commits (the state was built by hand); the station's new rule against
a real server (its decisions are unit-tested); and the Android app, which still deletes its
picture on any answer below 500 (its own repository - `ServerOutcome.parse` there).

Measured 2026-10-08 (server in Docker, OCR on an RTX 4070 Ti SUPER, a laptop on Wi-Fi sending
recorded captures): one station at a card every 2.5 s - 30 of 30 added, 29 by OCR in 0.34 s
(median, upload included) and 1 by the AI in 1.2 s; four stations at a card every 0.5 s each
(7.8 cards/s) - 100 of 100 added, OCR median 0.48 s, worst 1.0 s.

### A camera station

`station_client.py` runs the real `CardScanner` next to the camera - capture thread, outline
detection, stillness, new-card rules, focus sweeps and probes, all at camera speed. On the
server holds a `RemoteScanner` (`remote_scanner.py`) in its place, one per station
(`CameraHub.scanner(id)`): the same attributes and methods, so the page's handlers are the same
whether the camera is local or at a station.

The client opens the connection (Socket.IO namespace `/station`, `auth`: id, name, token) and
keeps it; nothing has to be configured on the server to add a camera.

| Client → server | |
|---|---|
| `hello` | Answered with the camera's saved settings (`CAMERA_SETTINGS`: focus position, rotation, focus check interval, fixed area, debug trace) and the attributes the server decides (`DECIDED`: add automatically, stability frames, capture delay). The client creates its `CardScanner` with them - which is when the camera opens; after a reconnect only the decided attributes are applied again |
| `status` (every 0.2 s) | The scanner's state and `get_detection_status()`; the answer says whether the preview is being watched |
| `preview` | The annotated live view as JPEG (`get_stream_jpeg`), at most ~12 a second and only while a page shows `/video_feed` (measured: 12 fps, 66-81 KB a frame over Wi-Fi). A card the station finds by its outline has no border: it is tinted, see-through, orange while it settles, green when ready, blue while held, and a capture washes it a stronger green that fades down to the tint in 0.6 s (`_mark_auto_card` / `_wash_card`, drawn into the picture by the station - the page has only the beep). The fixed area keeps its rectangle (the area that was drawn, green / orange / blue by state) and gets the same wash at a capture. The app does the same on its own overlay. On the page the camera's frame has the shape of the picture it shows (`updateVideoFrame` sets `--picture-aspect` from the stream's size every second; `.video-container` is as wide as the panel but at most `--frame-max-height`, 62% of the window's height or 620 px): a camera cropped to the box gives a smaller, upright frame - the same for every camera cropped alike, and with no bars - and an uncropped webcam fills the panel as a 16:9 one. (Until 2026-10-15 a portrait picture switched the frame to a 9:16 box, which left bars above and below a 4:5 picture.) |
| `setting` | A camera setting the scanner changed (a focus sweep's result, ...): saved on the server |
| `log`, `captured` | The scanner's log lines, shown on the station's page only (lines no station owns - backups, card data, the collection - go to pages that show no station: room `general`); `capture_received` (`url`, `number`: a camera-less station's page, a phone, shows the picture it sent last where a camera's live view would be; `/api/last_capture` gives the current one on load); the capture beep (`auto_capture_triggered` to its pages) |

| Server → client | |
|---|---|
| `command` | A `CardScanner` method (`METHODS`: rotation, fixed area, focus, detection, and `capture` for the page's Capture button), waited for (6 s); the answer carries the result or the error text (raised as `ValueError` on the server) and the state after it |
| `set` | Attributes: `card_under_review`, `auto_capture_enabled`, and the decided ones when they change |

What the server reads on its scanner comes from three places (`remote_scanner.py`): `DECIDED`
attributes are the server's and are sent to the client; `SENT` ones are set by the server now
and then but otherwise the client's own (`auto_capture_enabled`, `card_under_review`);
`REPORTED` ones only come from the client's status. With no client connected the camera
settings are read from the saved values, so the page still shows them.

- **Its settings live on the server.** The client's `CardScanner` gets a `ServerSettings`
  object (get / set) filled by `hello`; every change it makes is sent back (`setting`).
  `StationSettings` keeps them per station in `stations.json`; a value a station doesn't have
  yet is read from `settings.json` (what the single camera used before stations).
- **Captures** go out through a queue (`Station.upload_loop`): in order, each with a
  `capture_id`, retried until the server has it, then the local file is deleted. `mode: auto`
  is added or queued for review; `mode: review` is read at once and shown on the page, and the
  client holds its next auto-capture until the page says Add or Skip (`card_under_review`).
  **A capture the server answers oddly 20 times** (an error page, a 5xx for that picture, a wrong
  token - anything but "received" or "refused"; no answer at all does not count) is **parked**
  (`Station.park`, since 2026-10-16): it joins the held captures with its picture and note on disk, the
  setup page shows it ("the server kept answering with something other than ...") and offers
  "Send them to this server", the next start tries it again, and the captures behind it go on instead
  of waiting for ever. `tests/test_station_uploads.py`.
- **The server or the network goes away**: the station keeps scanning and the captures wait;
  the client reconnects by itself (~5 s after a server restart). Its own state decides
  `auto_capture_enabled`: a connection that drops and comes back goes on scanning; a client
  that restarts has it off, and its pages are told (`auto_capture_toggled`).
- **The same id connecting again** replaces its earlier connection.
- **Its setup page is linked from the server**: a station tells its setup page's port when it
  connects (`auth.setup_port`), the server keeps it with the address it connected from
  (`CameraHub.setup_page`, `setup` in `GET /api/stations`) and the scanner page's Stations list
  is not on the scanner page any more (it was under the camera's picture until 2026-10-16): the
  **Settings** page's Cameras category has a **Camera setup** button for the chosen camera
  (`settings.js`: `stationSetupUrl`) - a station on this computer connects from a loopback address, and
  the link then uses the host the page was opened on; no button while the station has not told a setup port.
  Tried only through the page's code, not in a browser.
- **Which camera**: `camera.usb_index` in the station's `config.yaml` (`auto`, the default: the
  first USB camera that is not a laptop's own - `integrated`, `built-in`, `facetime` or `IR` in
  its name - else the first one, read from /sys, `config.auto_camera_index`), or
  `SCANNER_CAMERA_INDEX` - a number, or a `/dev/v4l/by-id/...` path, which stays the same
  camera when the numbers change (`config.configured_camera`) - or chosen on the station's
  setup page. The path is turned into its `/dev/videoN` at every attempt to open the camera
  (`camera_index_or_none`, `scanner._open_camera`), not once at the start: while the camera
  is unplugged the path does not exist, which used to stop the station from starting at all
  (in Docker it restarted without end - found on the Pi 5 with its webcam removed,
  2026-10-11). Now the station starts, connects, says "No camera" and finds the camera when
  it is plugged in again, also under another number. Tried on that Pi 5 without its camera;
  plugging the camera back in has not been tried.

#### The station's setup page

`station_setup.py` serves one page on the station itself (port 8080, `SCANNER_SETUP_PORT`,
`--setup-port 0` for none), with Python's built-in web server - a station has no Flask. It
holds only what a station must know *before* it can reach the server; everything about
scanning stays on the camera's page on the server.

| On the page | Saved as (`data/station.json`) |
|---|---|
| Server address, station token. *Test connection* asks the address for `/api/stations` (only a scanner server answers that) and, if the server wants a token, for this station's capture 0 - a request that needs the token and changes nothing: 401 means the token is wrong | `server`, `token` |
| Name, id | `name`, `id` |
| Camera: *automatic*, a USB camera (listed from `/sys/class/video4linux`, saved by its `/dev/v4l/by-id/` name where it has one) or a Raspberry Pi camera (listed by `picamera2`) | `camera`, `camera_index` |
| A crop, dragged on a picture of the whole camera image (any camera, since 2026-10-15). A Pi camera reads out just that part of its sensor; a USB camera sends its whole picture and the station cuts the crop from every frame (`CardScanner._crop_view`: a slice of the decoded picture, before the rotation, no scaling - the pixels are the camera's own; the live frames are cut at half size, the captures at full size). The picture the page draws on is the camera's whole one (`get_whole_picture`), and a fixed area has to be drawn again after the crop changes | `crop` |
| Status: connected, the camera and its pictures a second, captures waiting to be sent, temperature | - |

- **Who wins** (`resolve`): the command line, then the environment (`SCANNER_SERVER`, ...),
  then the saved file, then the default. A value from the first two is shown on the page as
  fixed and is not saved - a station set up with `.env` behaves as before the page existed.
- **Setup mode**: without a server address from any of them, `station_client.py` serves only
  the page and waits; the camera is not opened.
- **Saving** checks the form (`checked`), writes the file (to a temporary file, then renamed:
  whole or not at all; mode 600, it can hold the token) and restarts the station: the page's
  thread raises a flag, the main thread ends `Station.run()` - which releases the camera - and
  replaces the program by a new copy of itself (`os.execv`), so a container keeps running.
  Captures still waiting to be sent are not lost by it: see *Captures that are not on the
  server yet* below. One that is on its way gets up to 10 s to arrive first.
- **Captures that are not on the server yet** (`station_client.py`: `write_note`,
  `load_pending`, `send_until_settled`): the upload queue is in memory, so every queued
  capture also has a note beside its picture, `station_captures/<image>.upload.json` - its
  `capture_id`, mode, foil picture, and where it was going (server address, station id).
  A new start queues the notes it finds, oldest first, under the same `capture_id`: a
  capture that may or may not have arrived before the program ended is sent again and is
  still one card (the server counts an id once). The note goes when the server has the
  capture, or has refused it for good (the picture is then kept, as before). This holds for
  a restart from the setup page, a stopped container and a power cut alike; in Docker the
  folder is the volume `station-captures`, so it also outlives a new image.
  - *Taken for another server, or under another station id* (the setup page changed one
    while captures waited): they are **held** - kept, not sent. The old server may have moved
    to a new address, or the station may have been pointed at another collection; the
    program cannot know which. The setup page's status shows them with *Send them to this
    server* and *Delete them* (asked twice).
  - The token is not in the note: a capture is sent with the token the station has now, so
    correcting a wrong token on the setup page lets the waiting captures through.
  - Until this was added (2026-10-11, found by a Codex review of the setup page), a
    station's unsent captures were only sent by the run that took them, and the setup
    page's restart made that a common case.
  - *A folder of its own.* The waiting captures are in `station_captures/`
    (`Config.STATION_CAPTURES_DIR`), not in `scanned_cards/`, where the server keeps the
    pictures it received and deletes those older than `cleanup.days`. On a machine that
    runs both programs from one checkout the two folders were the same until 2026-10-12, and
    a capture that waited longer than the server keeps pictures would have been deleted
    before it was ever sent - the station then found no picture and dropped its note. What
    an earlier version left in `scanned_cards/` is moved over at the next start
    (`adopt_old_folder`: every note with its pictures; the server writes no notes, so
    nothing of its own is touched). In Docker the volume `station-captures` is mounted at
    the new folder and carries over what was waiting. Found by a Codex review of the
    one-machine setup; `tests/test_station_uploads.py` runs the server's clean-up over a
    waiting capture a year old.
  - *Stopping.* The station ends in an orderly way (the camera released) on SIGINT and
    SIGTERM, and takes both back explicitly at its start: a shell that starts a program in
    the background makes it ignore SIGINT, and Python then never raises `KeyboardInterrupt`.
    With a server address the Socket.IO library happened to re-arm the signal; in setup mode
    nothing did, and the station could only be killed (reproduced on the version before:
    still running 10 s after `kill -INT`). `scripts/start.sh` also gives the station 10 s
    and then kills it, so the script itself cannot hang. Tried: started in the background,
    both modes, both signals - stopped each time; the start script with Ctrl+C in a
    terminal, and with the server killed - the station followed within a second.
- **The camera in Config** (`apply_camera`): the saved camera and crop are put into `Config`
  before the scanner is created, and only where the environment did not decide.
- **Show picture** (`SetupPage.picture`): a free camera is opened for one picture and closed.
- **Crop shape** (setup page, `fitShape`): the crop is dragged in a chosen shape - 4:5 upright
  (the default), 16:9 or free - counted in the picture's own pixels, so a Pi 4 and a webcam both
  set to 4:5 give live views of the same shape on the scanner page (whose frame follows the
  picture's shape). Only the drawing is constrained: what is saved is still the four fractions,
  and detection, the capture and its storage are unchanged (the card is cut from the full
  picture by its outline). Tested in `tests/test_station_setup.py` (`CropShape`, run with node).
  A drag that starts inside the drawn crop moves it (`moveBox`, same size, stops at the picture's
  edge); one that starts outside draws a new one; a right click clears it.
  A USB camera the scanner is using: the scanner's current frame. A Pi camera the scanner is
  using: the scanner shows only its crop, so it lets go of the camera for the moment
  (`CardScanner.with_camera_released` - the capture thread closes it, the picture is taken,
  it opens again; 4.5 s on a Pi 4, scanning pauses meanwhile).
- **The token never reaches the page** (only whether one is saved).
- **Login** (`station_login.py`): the page asks for a password - one password, no user names.
  At the first visit it asks to choose one (at least 8 characters); `SCANNER_SETUP_PASSWORD`
  fixes it from outside instead (kept the same way: a hash and a random key in the file,
  rotated when the variable's password changes). Until a password exists, whoever opens the page first
  chooses it. Everything but the page's HTML and the login itself answers 401 without a
  login.
  - *Kept hashed, not encrypted*: `data/station_login.json` (mode 600) holds a random salt,
    the scrypt hash of the password (N 2^14, r 8, p 1) and a random key - never the password.
    An encrypted password could be decrypted by whoever has the key, and the key would lie
    beside it; a hash cannot be turned back.
  - *Sessions*: a cookie with an expiry time (30 days) and its HMAC-SHA256 signature made
    with that key (`HttpOnly`, `SameSite=Strict`). The station keeps no list of logins, so a
    login survives the restart after *Save and restart*. Changing the password makes a new
    key: every browser has to log in again. The key is always random and this station's
    own - never made from the password. (The first version made it from
    `SCANNER_SETUP_PASSWORD` with one SHA-256: a password guess could then be tested by
    signing a cookie with it, without a login, scrypt or the lock, and stations with the same
    password accepted each other's cookies. Found by the same review; fixed before release.)
  - *Wrong passwords*: the next attempt has to wait 1 s, then 2, 4 ... up to 30 s. The wait
    is for everyone, so someone guessing can make the owner wait up to 30 s.
  - *Forgotten*: delete `data/station_login.json` on the station and restart it.
  - *Not protected*: the page is plain HTTP - the password crosses the network unencrypted
    when it is typed. This keeps out the curious on a home network, not someone listening
    on it. The server's own pages still have no login.

Tried 2026-10-11 on the Pi 4 below, from its own Python and in the container: setup mode →
*Test connection* → save → restarted, connected and scanning; a picture in setup mode (2 s)
and while scanning; settings kept over a restart of the container; the page driven in a
headless Chromium (picture shown, a crop dragged, no script errors). On a PC only setup mode
and the page's routes were tried; choosing between two USB cameras has not been tried at all.
`tests/test_station_setup.py` covers who wins, the form's checks, the connection test (wrong
and right tokens, addresses that cannot be requested), the file, and the password
(hashing, the lock after wrong attempts, session cookies, that a cookie cannot be made from
the environment's password); `tests/test_station_uploads.py` the notes (a restart with
captures waiting, a capture sent twice under one id, held captures sent or deleted) against
a stand-in for the server. Tried on a PC: a capture left waiting, the station restarted from
its page - the new run queued it and went on trying; two captures for another server shown
as kept, and deleted with the page's buttons in a headless Chromium. Not tried: a real
server accepting a capture re-sent after a restart (the server's side of that, the same
`capture_id` twice, was measured 2026-10-08). The login was tried on a PC in
setup mode: every route refused without it, a second "choose a password" refused, a forged
cookie refused, the old cookie refused after a change; and its form driven in a headless
Chromium.

Measured 2026-10-08 (laptop, Ryzen 5 7235HS, Anker PowerConf C200, Wi-Fi; server in Docker):
manual capture to card on the page 2.7 s when the AI had to read it, auto-capture to added
0.4 s (OCR); a focus sweep asked from the page, its result saved on the server; fixed area,
rotation, focus check interval and detection switched from the page; server stopped for 12 s
with a capture waiting - sent when it was back, one card; the same `capture_id` twice - one
card. Client load: ~30% of one core, 155 MB (66 MB in the container). A live run of 17 cards:
17 captures, 17 added, all read by OCR (median 0.11 s), 1.7 s per card.

Measured 2026-10-09 on a Raspberry Pi 5 (8 GB, Raspberry Pi OS / Debian 13, the same camera,
Wi-Fi, the client in its container): ~50% of one core idle, ~70% while scanning, 75 °C, no
throttling; live view 11.6 fps; a live run of 18 cards in 28 s (1.6 s per card), all read by
OCR.

### A page per camera

Every station has its own scanner page, `/scan/<id>`: its live view and controls, the card it
is showing, its review queue, its Undo, its activity log and its scanned-cards count. `/` lists
the stations (`templates/stations.html`, refreshed from `GET /api/stations`). A station's count there is
`scanned`, its copies in the scanned cards now (what its page lists); `captures` in
`data/stations.json` counts every picture it ever sent and only numbers the captures.

Review items without a station (`review_queue.station` NULL) are listed on **every** camera's
page beside its own (`ReviewQueue._of`), and counted for each station in the list. They come
from the camera the server itself could have until 2026-10-12 - what it had queued, and its
captures that were still waiting to be read at the update - whose page went with it; shown
nowhere, they could never have been resolved. Resolving one on any page removes it from all;
clearing a camera's scanned cards leaves them. (Tested on a temporary database; no real
installation with such items was updated.)

On the server each station has a **desk** (`server/desks.py: Desk`, `desk_for`): its scanner, the card
on its page (`current_card_info`), its open review, the capture being reviewed, its captures
being read. The handlers were written for one camera and still read that way: `desk()` gives
the current desk and `scanner` is a proxy for `desk().scanner`. Which desk is current:

| Where | The desk |
|---|---|
| A scanner page's Socket.IO event | The station the page named when it connected (`io({query: {station}})`, `page_stations`); the page's socket is in the room `station:<id>` |
| An HTTP request | `?station=<id>` (the scanner page adds it to every `/api/` request - a wrapper around `fetch` at the top of `scanner.js`), or the id in `/api/stations/<id>/...` |
| A worker thread | The one set with `in_desk(...)`: `submit_capture` remembers its desk for the result, the OCR / AI workers run each job in its desk (`Job.scope`), a `RemoteScanner`'s callbacks in its station's (`at_station`) |
| Anything else | `default_desk`: a desk with no camera (a request that names no station) |

Events about a card go to the desk's room only (`emit_desk`: captured, found, added, queued
for review, log lines, the capture beep, camera settings); events about shared things stay
broadcasts (card data, game, AI provider, the scanned list changing).

| Per station | Shared by all |
|---|---|
| Camera settings (focus, rotation, fixed area, focus check interval), add automatically, debug trace | The game being scanned (switching it stops every camera) |
| Location for scanned cards | AI provider, model, keys, prompts, OCR first |
| Review queue, Undo, the card on the page | Card data, the collection, decks, backups |
| Count of cards scanned | Sound settings |

### The scanned cards, by camera

The scanned cards are **one list** (`scan_inventory`), with a camera filter - so there is one
place to check everything and one "Add to collection", and each camera's page still shows only
its own.

The same printing scanned by two cameras into the same location is one entry, so who scanned
what is recorded beside the entries: the station of every capture (`inventory_captures.station`)
and how many of an entry's copies each station added (`inventory_sources`; `add_card(source=)`).
Everything that concerns "a camera's cards" goes through `InventoryManager._station_rows`: the
entries a station has copies in, with as many copies as it added - at most what the entry still
has.

| | With a camera chosen | "All cameras" |
|---|---|---|
| List (`GET /api/inventory?area=scan&camera=<id>`) | That camera's entries; `quantity` is its copies, `entry_quantity` the entry's, `shared` when they differ - the page then offers no edit / delete, which act on the whole entry | Every entry, with `cameras: {name: copies}` |
| Add to collection (`POST /api/scan_inventory/to_collection`, `take_from(station=)`) | Only its copies and captures move; the rest stays | Everything |
| Clear (`POST /api/clear_inventory?area=scan`, `clear_camera`) | Its copies, its capture images and its review items (a review open on its page is closed) | Every scanned card; the review queues stay |
| Top bar count (`scan_stats`) | On a station's page: its own cards | |

- A station's page opens the list on its own camera.
- **Ownership follows the copies** when an entry is split or merged (`_move_sources`): the
  stations of the newest captures give up a copy each, as those captures move too. For copies
  without a capture (added by hand), the station that came to the entry last gives up first.
- **A move is finished after a crash for the camera it was for**: the note in `pending_moves`
  carries the station (simulated 2026-10-08: moved once, the other camera's copy still scanned).
- **Backups** include `inventory_sources`; a backup made before stations restores with no
  ownership rather than keeping today's.

Measured 2026-10-08 with two pages open at once (a camera station and a station uploading
captures): each page received only its own station's events, including the OCR / AI log
lines; review queues, focus-check interval and location were each station's own.

## Card detection

This chapter, [Auto-capture](#auto-capture) and [Focus](#focus) describe `CardScanner` (`scanner.py`, `object_detector.py`): it runs where the camera is, inside `station_client.py`
on a camera station. The Android app
has the same rules ported to Kotlin (its README maps the files).

**Frames.** USB cameras deliver MJPEG; the scanner asks OpenCV for the raw JPEG
(`CAP_PROP_CONVERT_RGB = 0`) and `grab()`s every frame off the camera (so frames are never stale)
but decodes only `camera.fps` of them. For cameras of 1920 px and wider, live frames are decoded
at **half size** (2560 × 1440 → 1280 × 720): detection, stability, focus measurement and the
preview stream all work at that size, and a capture decodes the stored JPEG at full size and
scales the card's corners up (`get_detected_card`, `get_full_frame`). OpenCV runs with 2 threads
(`cv2.setNumThreads(2)`) - its default of one thread per core spent more CPU spin-waiting than
working. The preview encodes each new frame once (`get_stream_jpeg`): a station sends that JPEG to the
server while a page is watching, and the server hands it to every browser tab showing the camera.

**Rotation** (`camera.rotate` or **Settings → Camera rotation**, saved per station as `camera_rotation`;
`set_camera_rotation` / `camera_rotation_updated`): every frame is rotated right after it is
decoded - live and the full-size capture (`CardScanner._rotate`) - so detection, crops, the
fixed area and focus all see an upright card. The card stands along the frame's short side
(1440 px) otherwise: measured over a 67-card lot, the card grew 16% (1038 → 1204 px) as the
pile rose ~20 mm, i.e. the camera sat ~14.5 cm above the box floor, and the card would reach
the frame's edge after ~115 cards. With the camera mounted sideways and the image rotated 90°,
the card's long side runs along 2560 px: at the same distance the pile can grow ~270 cards, or
the camera can come closer (~10 cm) for ~45% more detail per card and still ~125 cards.
Changing the rotation turns the fixed area off (it was drawn for the other orientation).

`object_detector.find_card_outline(frame, previous=...)` runs on every frame (~3 ms):

0. **Follow the previous card** (`_track_outline`, when the scanner passes the last outline -
   across detector gaps of up to 2 frames; after a new card is detected it starts afresh, since
   following the old outline once latched onto a new foil's inner frame, and the photo lost its
   set line): fit a line to the edges along each side of the
   previous outline (within 6 px at 640 px, corner zones left out) and intersect them. The
   result is kept if it is card-shaped, within 15% of the previous size, has edges along ≥ 80%
   of its perimeter, moved ≤ 2% (more means it was fitted to another edge, e.g. the inner
   frame of a blurry card - taking it made the outline flip) and is a rectangle (step 6) - one
   that moved ≤ 2% but is skewed keeps the previous outline as it was; otherwise it is only a last
   resort after steps 1-5. On a sleeved pile the top card's outline often merges with the
   edge of a card underneath or with the box's corner crease once the pile is high - then no
   closed contour exists (in recorded pile frames the detector found nothing in most frames
   of a still card), or the largest outline flips between the top card and the whole pile.
   Both made cards wait 8-19 s and caused duplicate captures (the flip looks like a drop).
   Replaying 5 recorded pile moments through `CardScanner`: a card the current detector never
   captured in its 4 s recording was captured 1.3 s after landing, and a duplicate case gave
   one capture.

1. Downscale to 640 px on the long side, grayscale, Gaussian blur.
2. Canny edges with thresholds derived from the median brightness, dilated to close gaps.
3. For each contour covering at least 2% of the frame, fit the minimum-area rectangle and keep
   it if:
   - its aspect ratio is within 8% of a card's (88 × 63 mm = 1.397; settled cards measure
     1.33-1.37 on the widened edges). It was 18% until a borderless Rivendell, whose name
     banner runs from side to side, was photographed without its name (2026-10-06): when the
     card's own outline did not close, the part below the banner (1.18-1.25) was the largest
     card-shaped outline. On 1,600 recorded frames of that session 104 outlines had that
     shape at 18% and none at 8%, with an outline in 1,408 frames instead of 1,421; the
     outline also jumped less between frames (38 → 7 jumps over 5%),
   - the contour fills at least 85% of the rectangle (it really is rectangular),
   - it is portrait - unless `detection.allow_landscape` (a card's landscape art box has
     nearly the card's proportions),
   - it isn't the frame *inside* a card's dark border: the band just outside the rectangle
     must not be darker than the band just inside (this happens when the card's outer edge
     is cut off by the image border).
4. The largest remaining rectangle is the card; its four corners are returned.
   - **Unless it is the box** (`_card_inside_box`): with the whole scanning box in view, its
     floor is a closed, card-shaped outline, while a card pushed into the box's corner shares
     two sides with it and has no closed contour of its own - the outline was then the box,
     and the photo had the floor around the card. If the largest rectangle's edge is about as
     bright inside as outside (inside ≥ 75% of outside: white floor, white wall), a
     card-shaped rectangle inside it with a dark border (inside ≤ 50% of outside), edges along
     ≥ 80% of its perimeter and at least half its area is taken instead. A side must lie ≥ 4%
     of the box's length away: a clear sleeve's edge is such a pale outline too, 1-2% outside
     its card, and is left alone (on 400 recorded frames, 15 of a sleeved card would otherwise
     have changed; with the 4% rule none do).
5. **Broken outline fallback** (only when step 3 finds nothing): where a card's edge is as
   bright as the background - a borderless foil's silver frame against the white box - the
   outline has a gap and no closed contour exists (a Gwen Stacy borderless: the right edge
   along the text box, ~100 px, had no edge at all). Edge pieces within ~15 px of each other
   are grouped; a group's hull counts as the card if its rectangle passes the same ratio,
   portrait and inner-frame checks, the hull fills ≥ 90% of it, and edges run along ≥ 80% of
   its perimeter. On 100 recorded frames it found only that card (no false detection on empty
   boxes, piles or screenshots); it costs ~5 ms more on frames where it runs.
6. **Rectangle check** for the outlines of steps 0 and 5, which are built from edge pieces or
   fitted lines: opposite sides within 4% of each other and corners within 3° of square (the
   camera looks straight down). A holo Pokémon card (N's Zoroark ex) once gave a skewed outline
   whose "top edge" was a streak of the holo art running from the name to the top-right corner,
   and the photo lost the card name. Rejected outlines leave the photo to the other steps or,
   in fixed area mode, to the area itself. The check was 8% / 8° at first and skipped the
   followed outline that had moved ≤ 2%: such an outline creeps a little every frame, and a
   showcase Théoden was photographed with corners of 84° and 94° (top edge 6° off, the name
   chopped; 2026-10-06). On 400 recorded pile frames, followed outlines that were right were
   within 2% / 1°, wrong ones 7-15% / 3-7° off; with 4% / 3° none of the skewed ones is left.
   A skewed followed outline is not dropped but replaced by the previous outline: dropping it
   left the photo to the other steps, which on a borderless The Shire took the frame inside
   the card - the outline went back and forth and the same card was captured 9 times (2026-10-06).
7. **Gap closing** (only when steps 3 and 5 find nothing; `GAP_CLOSING`): a card lying against
   the wall of a small box has a side where its black border meets the wall's shadow, and that
   side of the outline comes out in pieces. The edge image is closed with a 21 px square (gaps
   up to ~7% of a card's width) and step 3 runs again. Closing *before* step 3 was tried and
   dropped: it lost outlines that are found without it (10 of 271 frames of one session).
   Measured on recorded frames, old detector → new, with the outline followed as the scanner
   does: three Pi camera sessions 274 → 423 of 471, 256 → 365 of 513 and 389 → 585 of 764
   frames with an outline; 704 frames of the USB camera 593 → 606 (the 13 gained are cards in
   motion). No frame lost its outline. On stretches where a still card had waited, found in
   0-48% of the frames before and 82-100% after (one card, the worst, still only ~55%).
   Live: one session of 17 cards on the Pi camera (below); not run live on a USB camera.
8. **The picture's own border is not a card** (`_is_whole_picture`; in step 3 and on the final
   result): an outline whose corners all lie within 3 px of the picture's corners is dropped.
   A dark picture with a card's proportions - a Pi camera's crop around the box, the lamp off -
   gave exactly that: the edge thresholds follow the picture's median brightness, near zero
   the sensor's noise becomes one sheet of edges, and the sheet's outline is the picture.
   Seen once on a station in the dark (2026-10-12; never captured, the picture was too
   blurry). With synthetic dark frames (brightness 2-8 of 255, with noise): 474x640 and
   1382x1866 pictures gave the whole picture as a card in 96 of 120 frames before, 0 of 120
   after; a 16:9 picture gave none either way. The 3,200 recorded frames on the development
   machine (USB camera) give the same result before and after, and a drawn card 12 px from
   every border is still found. The 1,600 recorded frames of the Pi camera (three sessions)
   give the same result before and after as well. Not run live with the lamp off. The
   Android app's port has the rule too (`CardOutline.kt`: `isWholePicture`).

`warp_card(frame, corners)` maps the corners to an upright rectangle with the card's aspect
ratio - the image sent to the AI is flat and tightly cropped, and the collector line is always in
the same place.

For display, the last detection is held for 6 s when the card is briefly lost ("HOLD"), but a
held detection never counts as a still card.

## Auto-capture

### When a card is ready

A frame counts toward `stable_frames` only if `_is_card_settled()` holds:

- the corners moved less than **1.5%** (`auto_capture.max_movement`; 1% until 2026-10-15. 2% was tried for one run of the
  same 69 cards (no waits, but one double: Captain America again 1 s after its capture, jump 3.8% with the
  picture unchanged) and put back - the limit is not to be moved on one run's evidence; 1% was changed when
  a Pi 5 run of 211 cards showed the corner network's outline jittering 1-1.5% on a still card
  on a tall sleeved pile: 32 waits, 18 of 5 s or more, the longest 15 s; the outline detector
  measured <= 0.4%) of the card size since the previous frame **and** since
  the still streak began - a sleeved card sliding slowly (a few px per frame) passes the
  frame-to-frame test on every frame and used to be captured mid-slide: blurred, then captured
  again when it stopped (seen live with a Lake-town),
- sharpness (variance of the Laplacian on a 160 px wide crop) changed by less than **20%**
  (autofocus still adjusting changes it a lot),
- sharpness is at least `auto_capture.min_sharpness` (**250**) - a camera that hasn't focused
  yet is steady but blurry.

A capture needs `auto_capture.stability_frames` (5) such frames in a row, or
`fast_scan.stability_frames` (6, about 0.3 s) when cards are added automatically. Simulated
sleeve slides (0.8 s after landing): with 4 frames and only the frame-to-frame test, 12 of 12
cards were captured mid-slide; with the drift test and 6 frames none (slides of 2-3 px/frame),
captured ~0.5 s after the card stopped. 4 frames with the drift test still let a 2 px/frame
slide through. The status
pill shows *Focusing*, *Stabilizing n/N*, *Ready*, *Capturing - wait for the beep*, or
*Captured - drop the next card*.

**Why is it waiting?** When auto scanning waits more than 2 s for a card to become ready,
the scanner's log - the station's output, its page's Activity, and `data/logs/app.log` on the
server - says why, once a second (`_trace_waiting`): *no card outline found* (every 5 s), or
*card not ready (n/N)* with the frame's movement, drift, sharpness change and sharpness against
their limits (1%, 1%, 20%, `min_sharpness`). With **Settings → Cameras → Advanced → Debug trace** on (remembered per station: `debug_trace`), the scanner also
keeps the last 3 s of frames (640 px wide - what the outline detector works on) and saves them,
plus the next second, to `data/debug_frames/<time>_<reason>/` (on the station's machine) when a card waits over 2 s or a
new card is detected, or the card is "gone", less than 2 s after a capture (a likely duplicate:
a foil Gwen Stacy lost its outline right after its capture and was captured again - by its
look the pair differed 0.79, so appearance can't tell such a case from a new card); the newest 30 dumps of each
reason are kept (counted per reason since 2026-10-15: a run of captures writes one "soon after capture" dump each and
used to push the "waiting" ones out); the older dumps go
are kept. Replaying such frames through `CardScanner` with a fake camera reproduces the case.

**Camera frames as training data (debug trace on; since 2026-10-14, never tested on a Pi).** A
capture is the card cut out and straightened: the frame it came from and the outline found in it
are gone, and those are what a better detector would be trained and scored on. With debug trace
on, each capture is also sent with its whole camera frame (`CardScanner._frame_snapshot`: the
camera's own JPEG, encoded again only when the camera is rotated) and a note
(`station_client.queue_upload`, form fields `frame` and `frame_meta`): `corners` of the outline in
that frame's pixels (full size, also when detection ran at half size; `null` when no outline was
found and the capture is the whole frame), `frame_width` / `frame_height`, `card_size`, `rotation`,
`fixed_area`, `stable_frames` / `required_stable_frames`, `new_card` (what made the card count as
new: `kind` = `jump_or_image_change` with `jump` and `image_change`, `area_change` (fixed area:
`area_change`, `big_changes`, `differs_from_captured`), `dropped_while_focusing` or
`card_gone`, and `seconds_before` the capture), `seconds_since_previous_capture`, `capture_id` and
`previous_capture_id` (a double capture shows as a pair that follow each other within seconds).
The pair waits on the station like the capture does (`<image>_frame.jpg`, in the upload note) and
goes with it; an older server ignores the two fields. The server keeps it as
`data/frames/<capture_id>.jpg` + `.json` (`core/frames.py`, `stations_api.keep_camera_frame`): once per
capture (a retried upload adds nothing), never failing the capture, capped at 3 GB (oldest first),
outside the collection backups. The note also has `station`, `number` and `image` (the capture's
file in `scanned_cards/`). Since 2026-10-15 a frame goes with its scanned card: once the capture is settled
the server notes its `uid` in the frame's `.json` (`frames.attach`, called from `submit_capture`), and
when the scanned cards are cleared (a camera's, or all: `clear_camera`, `clear_inventory`) or added to the
collection (`scan_to_collection`) the frames of the captures that left the scanned list are deleted
(`inventory_api.release_frames`, `Inventory.capture_keys`, `frames.forget`) - unless **Keep camera frames
after clearing or adding** is on (collection page, Settings → Storage; `keep_frames` in
`data/settings.json`, through `/api/capture_copy`), which is for the days frames are labelled. A frame
without a `uid` (taken before this, or never settled) stays until the 3 GB cap. Tests:
`tests/test_frames_release.py`. Only captures have a frame: a card that was *missed* leaves none (the
stations' own `debug_frames/` dumps are what show those).

**Labelling and scoring the frames** (stdlib / this checkout only, never against the live server's
`data/`: copy `data/frames/` first). `scripts/label_frames.py --frames <folder>` serves a local page
(127.0.0.1) that shows each frame with the detector's outline and lets a person drag the four corners
onto the card's own edge (not its sleeve, not the cards under it); it writes
`<capture_id>.label.json` (`corners` top left, top right, bottom right, bottom left, in the frame's
pixels, and `status`: `labelled` / `no_card` / `unsure`) beside the pair and changes nothing else.
`scripts/score_detector.py --frames <folder> [--station ID]` replays the labelled frames through
`ObjectDetector` and prints the intersection over union (IoU) with the label per station and mode -
for the outline the station stored (its note) and for the one this code finds now. The replay starts
cold on one frame (no `previous`), so it is for comparing one version of the code with another, not
for the live numbers. First measurement (2026-10-14, 78 labelled frames, one Pi box with sleeved
cards, a webcam): the laptop's stored outlines matched the labels (IoU 0.997, 21 frames); the Pi's
were too large on nearly every edge (IoU 0.90 in outline mode, 13 frames; mean 3-4% of the card's
width per side, up to ~10%), following the pile's silhouette - the cards under the top one and the
sleeves' rims. Among the candidates the detector has, the best one would have reached 0.93, and on
5 of 13 frames a cold detection found none: edge selection is not the whole problem.

**Our own corner network (2026-10-14; used by the stations since 2026-10-15, not by the Android app yet).** The camera of this
project is always fixed overhead of a box, which is what the network is built for: no
perspective or arbitrary rotation to cover, only a card of nearly one size in a window of
nearly one place. `scripts/train_corner_net.py` trains a small network (0.76 M parameters; one
card per 192 x 320 window, four corner maps read as soft-argmax positions plus a presence
score) only from labels made with `scripts/label_frames.py` - no other model's weights or
outputs. A frame is read in two stages: the whole frame letterboxed, then a window of 1.45 x
the card's box cut from the full-resolution frame (twice). Validation is by whole station
(`--train` / `--val`). The labelling page has a click mode (press D: corners 1, 3, 2, 4 - top left,
bottom right, then top right, bottom left; after the first two the box is there, the other two
put a tilt right; Escape keeps what is placed) and a station filter. Training on a GPU needs a
CUDA build of PyTorch (not in `requirements*.txt`): `--device auto` uses it when there is one
(RTX 3050 laptop: 3000 steps in 2.5 minutes, 17 on the CPU).
First result (trained on 57 Pi frames, scored on 21 webcam frames of another camera): mean IoU
0.966, worst 0.954. A box with the same place and size in every frame already scores 0.90 - 0.97
inside one station (and 0.47 across stations), so a within-station score proves little: the
held-out one, and the phone's frames are the tests that count. Trained on the Pi's and the
webcam's frames and scored on the phone's 49 labelled captures: mean IoU 0.969, worst 0.926
(the app's own outline on the same frames: 0.861; the phone's frames exist only for captures the
app did make, so the "No card" stretches of the screen recording are the test still to run).
The exported file (3 MB) loads in OpenCV's dnn and agrees with PyTorch to 4e-6; 2.6 ms a
window on a laptop CPU. Not tested: speed on a Pi, the Android build.

*In the station* (`station/corner_net.py`; the weights are `station/corner_net.onnx`, 3 MB, beside it
and so inside the images' `station/` folder; trained on 253 labelled frames, 3 "no card" ones
as negatives): `CornerNet.find` runs the whole frame in one window, then one window around the card (a
second gave the same IoU, 0.978 on 127 labelled frames) with OpenCV's dnn (no PyTorch on a
station); given the card's previous outline it skips the whole-frame stage. On a Pi 4
(OpenCV 4.10 in the `client-pi` image, 2026-10-15) the file loads and runs: 58 ms a frame cold
(the whole frame, then one window), 28 ms with a previous outline (one window); with two
refinement windows it was 86 / 57 ms. The Android app has the same network (`CornerNet.kt`,
`CardFinder.kt`, `assets/corner_net.onnx`): on an emulator (OpenCV 4.14) its corners match this
code's to 0.05 px on four frames, cold and tracked (`CornerNetParityTest`); Settings → "Find
cards with the corner network" turns it off.

*Retrained on 2026-10-15 (the file in the stations since then)*: 42 more frames were labelled, the
most doubtful outlines of the Pi 5's runs of the same 69-card pile (odd shapes, tilted edges, the
doubles' frames; `~/mtg-detector-data/label_next`), and the network was trained again on all 299
labels with the same recipe (3000 steps, ~2 min on an RTX 3050; file size and speed unchanged).
Held out by whole runs, twice (the same physical cards, so optimistic): 16 frames mean IoU 0.933 ->
0.966 (worst 0.870 -> 0.939), 26 frames 0.932 -> 0.954 (worst 0.898 -> 0.921). On all 295 labelled
frames (trained on): 0.970 -> 0.975, worst 0.870 -> 0.935. Live, the same 69 cards on the Pi 5: 69
captures, none for review, no double, no wait but the first card's (the run before: Monica's left side
was cut off and the card went to review). Not tested on the Pi 4, the phone (its app has the old
network until its asset is replaced) or a different pile.

*Live runs of the same 57 sleeved cards with the network on (2026-10-15, one pile, each run once;
the outline detector alone had needed fixed area on the Pi and lost the card for ~6 s at a time
on the phone):* Pi 4, outline mode - run 1: 57 captures, no waits; run 2: 58 captures (one double
inside the run of identical copies at the end: its triggers were image change 0.36 and 0.26),
11 "card not ready" waits, the longest 9 s. Phone: 57 captures, 57 added, none for review,
nothing waited longer than 3.9 s. Webcam: 55 captures, no "no outline" waits (how many cards
were dropped was not counted). Not measured: a different pile, unsleeved cards, other lighting. `ObjectDetector`
asks it first (`detection.neural` in config.yaml, or `SCANNER_NEURAL_DETECTOR=off`) and believes
the answer when the score is >= 0.5 and the shape is a card's (convex, 1.12 - 1.68 tall, inside the
frame, >= 2% of it); otherwise - or when the network fails or its file is missing - the outline
detector answers as before, so `None` still means "no card" to the capture logic. Scored through the
station's own code path (`scripts/score_detector.py --neural`, on the frames it was trained on, so
not a held-out number): IoU 0.977 over 253 frames, an outline on every one, 12 ms a frame on a
laptop CPU; the outline detector alone: 0.65 with 64 frames without an outline. Frames labelled
"no card" scored 0.003 - 0.061, card frames 1.00 (those 3 were in training). On the 20 recorded
drop sequences of 2026-10-06 (webcam, 20 fps) it found an outline on 94% of frames (the outline
detector 87%); on frames that lie still, the corners moved at most 0.65% of the card's height
between frames for 90% of pairs (0.85% for the outline detector), median 0.24% against 0.15%,
far below the 1% still threshold. **Not measured:** the capture logic with it (double captures,
misses) on a live run, a Pi 4's speed (the station runs at ~7 fps there; with a previous outline
it is one window), the containers' OpenCV reading the file.

**The capture beep is the signal to drop the next card.** `auto_capture_triggered` (beep +
flash) reaches the station's pages once the image is taken and a focus probe started by that
capture is done (`scanner.capture_pending` / status `capturing` until then): a station sends
`captured` at that moment (`Station.auto_captured`) and the server passes it on; with a camera
on the server it is `app.handle_auto_capture` (`announce_capture`). Auto-captures take the image at once (`capture_card_image_only(settle=0)` - the card has
already been still for `stability_frames`); the beep used to come *before* the image, which was
taken 0.3 s later, and a card dropped right away could land in it. Adding a card (after the AI,
1-2 s later) only plays the success ding. Measured with a fake camera and a probe on every
capture: image at t, probe done and beep at t + 1.0 s; without a probe, right after the image.

### One capture per card

Cards are dropped onto a pile, so the view never becomes empty. After every capture (automatic
or manual) `awaiting_new_card` is set and a tiny normalized thumbnail of the card is stored.
Auto-capture re-arms (`_new_card_arrived`) when:

- the card jumps more than **3%** of its size, or its image (32 × 45 thumbnail) changes by more
  than **0.3** between frames - the drop itself, or a hand;
- after a gap in detection (a falling card usually can't be detected for a few frames), the card
  reappears more than **0.8%** away from where it was - a dropped card never lands exactly on the
  previous one, while a detector hiccup leaves it within ~0.3%;
- no card is seen for **6** frames or more;
- the settled card looks different from the captured one.

Identical copies are caught by the drop, not by their looks. Measured noise of a card lying
still: movement ≤ 0.4%, image change ≤ 0.07, detection gaps ≤ 2 frames. In a live test with
11 drops (including two identical copies) every card was captured exactly once; real drops
measured 2–5 frames without a card, jumps of 4.5–8.5% and image changes of 0.7–1.2.

*Image change, since 2026-10-15:* the corner network's outline of a still card wobbles by 1–1.5%
of its size from frame to frame (card sizes 867, 874, 879, 890 px within 4 s), which shifts the
32 × 45 thumbnail by a fraction of a pixel. On a detailed card (Nick Fury, Agent of S.H.I.E.L.D.)
that alone made the frame-to-frame change 0.3–0.5, the card "disturbed" and so a new card, again
and again within a second (5 captures of 2 cards in one run, 3 and 2 of 2 in the next; none were
drops: jumps of 0.5–1.5%). The two comparisons that look at the picture - frame to frame
(`card_disturbed`) and the settled card against the captured one (`_new_card_arrived`) - now blur
both thumbnails (σ 1.5) and normalise them again before comparing (`_thumbnail_difference(smooth=True)`),
and the limit is `IMAGE_CHANGE_LIMIT` = 0.35 (was 0.3 on unblurred ones). Measured on 11 consecutive
capture pairs of that run: the same card differed 0.45 at most unblurred, 0.22 blurred; different
cards 0.67 and 0.51 at least. The flicker test above stays unblurred. `tests/test_image_change.py`.

Captures are also at least `auto_capture.delay` (1 s) apart.

When the only sign was the outline vanishing for a frame and coming back shifted (a hand
approaching), the card must not settle as the very image just captured (`_outline_flicker`:
thumbnail difference < 0.05) - in a 67-card lot a card was captured twice this way (0.02), while
real copies of a card differed 0.07-0.50 and came with bigger signs (jumps, image changes).
Over that lot the pile grew 16% in height; pace (2.0-2.5 s per card), text sharpness and focus
stayed the same.

### Fixed area (sleeved cards)

On a pile of sleeved cards the outline is unreliable: the top card's outline merges with the
card underneath or the box's corner crease, or flips between the top card and the whole pile -
cards waited seconds and some were captured twice. The **Fixed area** toggle in the camera panel
(`set_fixed_area`, saved per station as `fixed_area_enabled` / `fixed_area`) judges
cards by the image inside an area instead (`CardScanner._fixed_area_step`). The area is drawn
on the video (**Area**: drag around the card; saved as fractions of the frame) or taken from the
detected card plus 5% (**Use detected card**).

Per frame, a 48×64 grayscale thumbnail of the area with its mean brightness removed (so a
shadow or exposure change is not a change) is compared with the previous frame's:

| Measured on recorded sleeved piles | Thumbnail difference |
|---|---|
| Still card, frame to frame | ≤ 2.4 (≤ 5.4 over 1 s with the light changing) |
| A hand's shadow | ≤ 0.6 |
| A card falling in, frame to frame | 10-38, several frames in a row |
| A card settling after its capture (sleeve slide) | 13 in a single frame |
| A different card settled, compared with the last one | ~21 |

- **card present:** area sharpness ≥ `min_sharpness` (empty box ~30, a card ~1,600);
- **still:** change < 3 frame to frame and < 4 since the still streak began (a slow slide
  drifts), for `stability_frames` frames;
- **new card after a capture:** change > 8 in 2 frames in a row (a card falling in - also an
  identical copy), or the settled area differs > 10 from the captured one.

**The photo is the area as drawn.** The outline detector still runs, only for the ★/• foil
check: an outline inside the area gives the flat card whose corner is read (no outline, no foil
check). The photo used to be cut along that outline, but a holo Pokémon card's streak passed for
its top edge and the photo lost the card name - the area is what the user chose, so nothing
inside it is cut off. The outline is not drawn on the video in this mode. Replaying the five
recorded pile moments: every card captured once (the outline mode duplicated one); simulator
drops of identical copies, sleeve slides and focus probes: every card captured once, none
mid-slide.

### Adding automatically vs. reviewing

With **Add cards automatically** (default; internally `fast_scan_mode`, saved per station as
`auto_add`): the capture is uploaded, put on record and queued (`submit_capture`), read in the background (`Identification.submit`: OCR, then the AI
workers - see [The two queues](#the-two-queues)), and a confirmed printing
is added immediately by the server (`add_automatically`: one Near Mint copy in
`Game.suggested_finish` - the same rule as the page's `suggestedFinish`), never through the
current card, so it can't replace a card being reviewed. It used to be the page that sent the
add; a tab still running an older script then sent every add without its card and all confirmed
cards ended in the review queue - and with no page open (phone asleep) nothing was added. The
station's pages are told (`inventory_updated` with `auto: true`) and show the card with an
**Undo** button (`undo_last_add`).

Anything uncertain - printing not confirmed, name not found, no name read - goes to the
station's **review queue** (`review.py`, table `review_queue` with the station's id, a copy of the
capture in `data/review/`) and scanning goes on (it used to pause until the card was reviewed). Items keep what the AI
read, the ★/• result and the best match. The *Review* counter opens them oldest first
(`review_open` → `review_item`): the capture beside the suggested card, the AI read, and a
search prefilled with it (without a match kept, the search runs at once). Add resolves the item
- the capture becomes the entry's thumbnail - and the server sends the next; Skip drops it
(`review_skip`; the review's **Delete** button does the same when the card can't be found);
Close leaves the rest (`review_close`). Cards added automatically meanwhile
don't disturb the open item, and a card captured during a review in any other way (manual
capture, auto scanning without automatic adds) goes to the queue too (`route_identified`)
instead of taking the reviewed card's place and capture. The review belongs to the page that
opened it (`review_sid`): when that page disconnects the server closes it, and the page opens
it again when it reconnects. Per station and game; kept across restarts.

With the switch off, each capture is uploaded with `mode: review`, read at once
(`Identification.identify`) and shown on the station's page, where it waits for **Add** / **Skip**;
the station holds its next capture until then (`card_under_review`), and a card dropped
meanwhile is captured right after.

## Identification (vision AI)

### The two queues

Reading a card is shared by every camera (`identification.py`, one `Identification` in
the server) and has two stages, each behind its own queue:

1. **OCR** - one worker, because the reader takes one request at a time (0.10 s per card on a
   GPU). A confirmed read with its foil marker is done here.
2. **Vision AI** - `vision_ai.workers` (default 3) at once, for what OCR passes on: a read that
   isn't a confirmed match (or no read at all - full-art and borderless cards), and the foil
   marker of a confirmed card when OCR missed it.

So a card OCR confirms never waits behind a slow AI answer. Results are handed back by one more
thread, one card at a time, in the order they are settled - not the order captured.
`Identification.identify()` runs both stages in the caller's thread (a capture sent with
`mode: review`: manual capture, and auto scanning that waits for Add / Skip). A capture of a game that is no longer the active one is
passed through unread at either stage (see `route_identified`).

Workers measured 2026-10-08 with Ollama (qwen3.5:9b-q8_0, RTX 4070 Ti SUPER, default
`OLLAMA_NUM_PARALLEL`), 12 cards: 1 at a time 1.3 cards/s, 2 at a time 1.9, 3 at a time 2.0,
6 at a time 2.0 - more than 3 workers gains nothing there.

### OCR first

With *Read with OCR first* on (Settings; `ocr_first` in `data/settings.json`, on by default),
`identification.py` reads the card with
[light-ocr](https://github.com/arcships/light-ocr) (PP-OCRv6, offline) before any AI request:

1. `CardOcr.read_card()` sends the card image to the reader process and gets every text line
   with its confidence and corner coordinates. `parse_magic()` takes the name from the title
   bar (first line in the top 16%, left of the mana cost), and from the bottom-left (below 84%
   of the height) the collector line (`U 0172`, `M0128`, `M.0010`; or `123/281` on older cards)
   and the set line (`HOB·EN`), including the symbol between set and language code.
2. `Game.confirmed_read()` looks the read up. If it identifies the exact printing (a confirmed
   match - for set + number that includes the name agreeing), the card is done without the AI.
   Only when OCR read no `★`/`•` is the AI's foil check still asked.
3. Otherwise the vision AI identifies the card as before. If the AI fails too, or none is
   configured, what OCR read is used (it usually still finds the card by name for review).

Measured on 747 saved scans (Magic, mostly *The Hobbit* and *Spider-Man*, with a few Pokémon
cards and unreadable test captures among them), against qwen3.5:9b-q8_0 on the same images:

| | Confirmed match | Time per card |
|---|---|---|
| light-ocr | 658 (88.1%) | 0.17 s (GPU through WebGPU/Vulkan; 0.77 s on the CPU) |
| Vision AI | 708 (94.8%) | 1.04 s + 0.62 s foil check |
| OCR first, then AI | 710 (95.0%) | |

Where both were confirmed (656 cards) they chose the same printing every time. The collector
number pattern is strict on purpose: with a looser one, a `202米` read from a mana/level symbol
above the collector line matched another printing of the same card. What OCR doesn't confirm
is mostly a collector line too soft to read, which the AI still manages.

The reader is `src/ocr/server.mjs`, started by `CardOcr` when the app starts (the models take
1-2 s to load) and restarted after a failure (at most once a minute); its errors go to
`data/logs/ocr.log`. It needs Node.js 22+ and `npm install` in `src/ocr/` (the server's Docker image
has both; `deploy.sh` does it for an installation without Docker); without them the switch is
disabled and cards go to the AI. Its native library needs glibc 2.38 or newer, and on an NVIDIA
GPU inside a container the Vulkan driver's X11 / GLVND libraries (see INSTALL.md). `ocr.provider` in `config.yaml`
chooses GPU or CPU. Only Magic has a parser (`card_ocr.PARSERS`); other games are read by the AI.

### The vision AI request

**Retries** (since 2026-10-16; `CardIdentifier._post_with_retry`, `tests/test_ai_retry.py`): the three cloud providers
(Gemini, OpenAI, Anthropic) are asked up to three times when the answer is a rate limit (429), the provider's own
error (500 / 502 / 503 / 504) or a timeout / connection error - after 1 s and 3 s, or the `Retry-After` the provider
sent (at most 8 s). A wrong key or a bad request (400 / 401 / 403 / 404) is not repeated. The last answer is handed
back and the caller raises for its status as before (the card then goes to review). A local server has its own
fallback (native API, then the OpenAI-compatible one) and is not retried. Before this, one 429 in a batch sent the
card to the review queue as "not identified".

`CardIdentifier.identify_card()` sends the flat card image (JPEG, longest side
`vision_ai.image_size`, 1024 px) with a prompt asking for three values from fixed places on the
card:

```
NAME: <card name>            top of the card
NUMBER: <collector number>   bottom-left, line 1 ("U 0014")
SET: <set code>              bottom-left, line 2 ("HOB • EN")
```

The answer format asks for all three lines with their labels ("…, or Unknown"): with a plain
"exactly three lines" qwen3.5:9b often dropped the labels and sometimes the number line
("Mirkwood / HOB"), which lost 4 of 25 cards in one session; with the labels it answered 25/25.
The parser still accepts answers without labels, or with only some of them, and places bare
lines by their shape (number or set code). It also copes with chatty answers - Markdown
(`- **NAME**: Riolu (The card is ...)`), a comment in parentheses, a trailing ★, a number buried
in a sentence (`The number at the bottom left is "84/145"`), a language code after the set
(`PAL EN`). Letters read for digits in a mostly-digit number are
corrected (`018B` → `0188`: O/D→0, B→8, I/l→1, S→5, Z→2). Ollama
answers are capped (`num_predict`), so a model that starts reasoning aloud can't take seconds.

### Prompts

Prompts live in `prompts.py` and are editable in Settings → Card reading → **Edit prompts**. Each
prompt (card identification, foil marker) has two parts:

- **instructions** - what the card looks like and where each value is; this is what the editor
  changes;
- **answer format** - fixed (`NAME: / NUMBER: / SET:`, or "one word: star, dot, or unclear")
  and appended by the code, so an edit can never break the parser.

Edited instructions are saved in `data/prompts.json`, keyed by game (`mtg`; ready for other
games), prompt kind, and either `default` (all models) or `provider:model`. The prompt used is
this model's, else the all-models one, else the built-in one. **Restore default** removes the
saved prompt in effect (the model's first).

**Test on last capture** runs the AI on the last card read, from any camera
(`identification.last_capture`), with the
text in the editor, without saving, and shows the raw answer, how it was read, and the database
match it would get (confirmed → added automatically, or review). The foil test needs a capture
with a detected outline.

**The built-in identification prompt has no example values.** With examples ("E 0367",
"LTR · EN", "Lightning Bolt / 0367 / M21") models copied them on blurry cards instead of
answering Unknown - once producing a real but wrong LTR #367 printing. Measured on 90 recorded
scans (the 4 blurry ones checked by eye), with the native Ollama API:

| Prompt, image | qwen3.5:9b (server) | qwen3.5:4b (RTX 3050) |
|---|---|---|
| old prompt with examples, 2048 px | 86/90 correct, 84 confirmed, 1.42 s | - |
| same, answer format moved last, 2048 px | 87/90, 85 confirmed, 1.41 s | 89/90, 85 confirmed, 2.42 s |
| **no examples, 1024 px (built-in)** | **89/90, 83 confirmed, 0.92 s** | **89/90, 86 confirmed, 1.50 s** |
| no examples, 2048 px | 88/90, 84 confirmed, 1.27 s | 90/90, 85 confirmed, 2.23 s |
| no examples, 768 px | 87/90, 80 confirmed, 0.75 s | 88/90, 83 confirmed, 1.13 s |

No variant produced a confirmed (auto-added) wrong printing. Halving the image cuts the image
tokens (~1,400 → ~900), which is most of the prompt; shortening the text saved ~350 tokens.

Providers share one request function per API (`_ask_gemini`, `_ask_openai`, `_ask_anthropic`,
`_ask_local`). For Ollama, requests set `think: false` (thinking models otherwise spend the
whole token budget reasoning and return nothing), `temperature: 0`, and `keep_alive: 30m`; the
model is preloaded (`warm_up`) when auto scanning starts, because loading a 9B model takes about
10 s. Ollama error bodies (e.g. "model not found") are logged.

## Matching the printing

`CardDatabase.search_card_exact(name, number, set_code)` tries, in order:

| Step | Match | Tag |
|---|---|---|
| 1 | Set code + collector number (unique per printing), accepted if the name roughly matches (`names_match`: same, prefix, a double-faced card's face, ≥ 60% similar, or ≥ 75% similar to the short name before the comma - "Thands" / "Thanos, the Mad Titan") | `set_number` |
| 2 | Name + collector number, then shortened name ("Thanos" → "Thanos, the Mad Titan") + number. One printing → confirmed; several (the same card under the same number in more than one set - Solemn Offering #33 in M10 and M15, basic lands; ~9% of printings) → the one whose set code is closest to the one read, else the oldest, for review (`_by_name_number`) | `name_number` / `name_number_ambiguous` |
| 3 | Name only (exact, flavor name, shortened), else fuzzy (`difflib`, cutoff 0.6, candidates sharing the first letters) - then the number to pick the printing (`name_number` / `name_number_ambiguous`, as in step 2), else the printing from the same set (if any) whose collector number is closest to the one read. If the name was read exactly and exactly one printing of it in the set read is one digit off the number read - a digit misread, dropped or doubled, compared as printed with leading zeros ("0189" for #188, "6186" for 0186, "017" for 0117) - that printing is certain (with two printings a digit away it goes to review) | `name_set_digit` / `name_set` / `name` / `fuzzy` |
| 4 | Name unrecognizable but set + number exist: trust the printed set + number | `set_number_unverified` |

Collector numbers are compared in their variants ("0014" → 14, 0014, 14s, 0014s). Names are
compared through `name_search` / `flavor_search` columns - lowercase, accent-free copies
(`search_key`: "Fíli" → "fili", "Æther" → "aether").

Only `CONFIRMED_MATCHES` (`set_number`, `name_number`, `name_set_digit`) are added automatically; the card panel
warns "Printing not confirmed" for the others.

**Manual search** (`CardSearcher.find_printings`): set + number go straight to the printing -
also without a name, or with a name no card has (runes, another language, a misread name: the
AI read the Dwarvish-rune Arcane Signet as "Nthryx-Cipher"); otherwise all printings of the name (optionally filtered by a treatment: regular, borderless,
showcase, extended art, full art, retro frame, etched, surge foil) are listed newest first and
shown as a picker when there's more than one.

## Foil and finish

Modern cards print a star instead of a dot between set code and language on foil copies
(`HOB★EN` vs `HOB•EN`). For captures found by outline detection - where the corner is known to
be in the image - `read_foil_symbol()` sends the bottom-left corner (lowest 14% × left 45% of
the flat card, enlarged 3×) and asks which symbol it is (`vision_ai.detect_foil`).

The wording matters. Asked only "is the separator a STAR or a DOT?", qwen3.5:9b called regular
cards foil when the capture was a little soft: 25 of 84 regular cards (from a scanning session
where auto-added lands came out foil). Describing both shapes ("a dot is a plain round point; a
star has five sharp points") fixed that on the same set - checked by eye: 84/84 regular and
40/40 readable foils right; 3 unreadable, blurry foils were called dot. qwen3.5:4b still called
12 of the 84 regular cards foil (23 with the old wording). The prompt also says to answer
*unclear* when the set line is cut off: a crop that missed it (see below) was answered "dot"
and a foil went in as regular; with the sentence that crop gives *unknown*, and 160/160
readable cards (the set above plus later captures) stay right. The taller crop keeps the set line in
view when the detected outline also takes in the edge of the card underneath in the pile.

With *OCR first*, a marker light-ocr read is used instead: a `*`/`★` is foil, a `·`/`•`
regular. On the 747 scans that agreed with the AI on 524 of the 526 OCR-confirmed cards where
OCR read a marker, and on the two others the corner crops show a star (the AI said dot). OCR
reads no symbol on about a third of the foils (and some regular cards), so a missing one
counts as unknown and the AI is asked as above.

The web page combines this with the printing's `finishes` in `suggestedFinish()`:

1. only printed in foil → Foil (Surge foil if the printing is a surge foil) - certain;
2. only printed non-foil → Regular - certain;
3. otherwise ★ → Foil / Surge foil, • → Regular;
4. unknown → Regular.

The suggested finish is pre-filled with quantity 1 and the reason is shown ("Foil: ★ next to
the set code"). Automatic adds use the same suggestion. Cards printed before the marker existed
(roughly 2020) rely on step 1–2 only.

## Database

One SQLite file, `data/cards_database.db`.

**`cards`** - Scryfall's "default cards" bulk data (gzipped JSON lines), tokens/emblems/art
cards excluded. Columns are defined once in `database.CARD_COLUMNS`:

**The download is streamed** (since 2026-10-16; `CardDatabase._stream_cards`, `tests/test_card_data_stream.py`):
`download_scryfall_data` hands back a generator that decompresses and parses the gzipped JSON lines as they arrive,
and the import (`populate_database`) inserts them in batches of 5,000 - the file is never held whole. Measured with a
real download into a throwaway database: 112,899 cards in 10 s, peak memory 44 MB (the old path joined, decompressed
and parsed everything into one list; the review of 2026-10-16 estimated ~1.5 GB). A file cut off mid-way raises and the
swap (`replace_table`) does not happen: the old cards stay.

| Group | Columns |
|---|---|
| Identity | `id` (Scryfall id), `name`, `flavor_name`, `set_code`, `set_name`, `collector_number`, `rarity`, `released_at` |
| Prices | `price_usd`, `price_usd_foil` |
| Card text | `type_line`, `mana_cost`, `oracle_text`, `colors` (JSON), `image_uri` (front face for double-faced cards) |
| Treatment | `border_color`, `frame`, `frame_effects` (JSON), `full_art`, `promo_types` (JSON), `finishes` (JSON) |
| Search | `name_search`, `flavor_search` |
| Deck building | `oracle_id` (the same for every printing of a card), `cmc`, `color_identity` (JSON), `legalities` (JSON: format → `legal` / `restricted` / `banned`; formats a card is not legal in are left out), `keywords` (JSON) |

Indexes: name, flavor name, search names, (set code, collector number), rarity, type, oracle id.
Missing columns are added on startup (search names are filled in automatically; treatment and
deck-building data arrive with the next card database update - until then
`CardDatabase.has_deck_data()` is false and the Decks tab offers the update). "Rebuild database schema" copies the table into the
canonical column order by column name.

**`inventory`** (created and migrated by `inventory.py`) - one row per game + card name + set +
number + condition + finish + location (`UNIQUE`); adding an existing combination increases `quantity`.
`location` ('' = none) says where the copies are (a binder, a box), so copies of one printing
can be in two places; `tags` ("trade, keep") belong to the entry and are not part of the key -
entries that merge keep both sets. Moving part of a stack to another location splits the row,
like a finish change (`split_quantity`). **Edit card → Printing** changes an entry to another
printing of the same card (`update_card(printing=...)`, `card_id` in `/api/inventory/update`;
the choices come from `GET /api/inventory/<id>/printings`, i.e. `Game.printings` - Magic only so
far): the set, number, rarity, card id and price change, the photos stay with the copies, and
several copies split the same way. `bulk_update` applies one change (delete, condition,
location - whole stacks -, add / remove a tag) to several entries. Cards added while scanning
get their station's location (Settings → Scan into location; `scan_location()`). Inventories from before
locations are rebuilt once on startup (the key changed): backup in
`data/backups/inventory_before_locations_<time>.db`, row ids kept (the captures point at them),
entry and card counts checked, one transaction.
Rows are addressed by `id` (edit, delete, undo). Also stores the printing id (`card_id`), set
code, rarity, type, mana cost, colors, color identity, price and timestamp. `finish` is one of
the game's finish keys (Magic: `regular`, `foil`, `surge`). Editing the finish of part of a stack
splits the row; an edit that makes a row identical to another merges them. A new finish takes
the printing's price in that finish (`Game.get_card` + `inventory_fields`); rows without a printing id (CSV imports) keep their price.

**`inventory_captures`** (also `inventory.py`) - one row per captured copy behind an entry:
`inventory_id`, `file` (a thumbnail in `data/captures/`, 400 px tall, ~25 KB - the captures in
`scanned_cards/` are deleted after `cleanup.days`; since 2026-10-14 a large copy of the same
name, at most 1200 px tall (~100-150 KB; the person's choice, below), is kept in `data/captures_full/` for training data later:
the pages never load it, it goes with the thumbnail when the capture is deleted, it is in
`scripts/backup.sh`'s archive and, since 2026-10-15, in the app's backups as well - the folder
`captures_full/` beside `captures/`, as hard links, in the zip of a backup, and brought back by a
restore and by an uploaded zip, so a standalone program that restores a server's backup has the
pictures too - and in a full backup (as optional pictures: not "missing" for captures that have
none); captures from before have none, and a zip made before has no such folder), `captured_at`, `station` (which station's
capture it is) and `capture_key` (the uid of the capture the copy came from, unique; empty in
the collection and for copies from before 2026-10-12 - see
[Capture synchronization](#capture-synchronization)). The capture follows the card from the queues to the add: `search_and_emit_card` puts it on the matched card
(`card['capture']`, so a queued automatic add can't take another card's photo), and
`pending_capture` keeps the one under review for a manual search (the automatic "not found"
dismissal keeps it, Skip drops it). Several finishes added at once arrive as one
`add_to_inventory` event (`items`) - separate events ran in parallel threads - and the photo
goes with the first. The photos follow the copies: undo removes that add's photo; moving
copies to another finish moves the newest photos with them; merging moves all; lowering a
quantity drops the newest photos (the usual reason is a card captured twice); deleting or
clearing entries deletes their files. Entries added before this, or imported, have none.
`/api/inventory` returns each entry's `captures` (newest first, URLs under `/captures/`).

**`inventory_sources`** (also `inventory.py`) - `inventory_id`, `station`, `quantity`: how many of
an entry's copies each station added. It only matters for the scanned cards - see
[The scanned cards, by camera](#the-scanned-cards-by-camera).

**`trades`** and **`trade_cards`** (also `inventory.py`: `TRADES_TABLE`, `TRADE_CARDS_TABLE`;
used in the collection - the scanned cards' file has them too, empty) - a trade is `game`,
`name`, `status` (`open` / `done`), `created_at`, `closed_at`; a row of `trade_cards` is
`trade_id`, `inventory_id`, `quantity` and `card`. In an open trade the row names the entry
and how many of its copies are set aside (one row per entry and trade; several trades may hold
copies of one entry, never more between them than it has - `Trades.add` / `set_quantity`).
A confirmed trade's rows have no entry (`inventory_id` NULL) and keep the card as it was, as
JSON, in `card`. Two triggers (`TRADE_TRIGGERS`, made again at every start, because a rebuilt
table loses its triggers) keep what an open trade holds: an entry with such a row cannot be
deleted (`trade_keeps_entry`) and its quantity cannot be lowered below the copies set aside
(`trade_keeps_copies`) - by any part of the program; the statement fails with `TRADE_HELD`,
the method's changes are rolled back, and the page shows the sentence. See [Trades](#trades).

Inventories from before multi-game support (`foil`/`surge` flags) are rebuilt once on startup:
the old table is first copied to `data/backups/inventory_before_multigame_<time>.db`, the
migration checks that the card count is unchanged, and it runs in one transaction.

Exports and imports are on the collection page only, per game and per site / app, so more can
be added: `Game.export_formats` fills the **Export…** menu (Magic: Moxfield CSV - `Edition` is
the set code, which Moxfield matches; with the set name it took the alphabetically first set of
the card; surge foils go out as `foil`; the file has the 13 columns of Moxfield's own export, in
its order, because Archidekt's **Moxfield** preset (+ **Set columns**) reads them by position - a
9-column file failed there with "Row columns do not match header columns"; the 13-column one
imported there on 2026-10-14 (Moxfield's own import: not tried); every game: "Card Scanner (everything)",
`games.base.write_collection_csv` - all the columns of an entry, with `Card ID`, `Set Code` and
`Timestamp`, so `import_csv` puts each entry back as it was), `Game.import_rows`
reads another app's file (`import_formats` names them). `POST /api/import_inventory` picks by
the file's columns: Moxfield (`Count`, `Name`, `Edition`) → `Magic.import_rows` matches each row
to its printing by set code + collector number as written (`get_card_by_set_number(exact=True)`:
"M19-128" of The List) - when the card's front name agrees with the row's, since 2026-10-15
(before, an exact match was taken whatever the name said) - then the number's variants if the
name agrees, then the name alone (counted as `by_name`: maybe another printing); `foil`/`etched` become foil, or surge for a
surge foil printing; Moxfield's conditions map to the ones here (Lightly Played → Excellent,
Moderately → Good, Heavily → Played, Damaged → Poor); tags are kept, the location is empty
(Moxfield has none - entries that differ only by location merge) → `inventory.import_entries`.
The app's own columns (`Card Name`, `Set`) → `import_csv`; CSVs written before the `Card ID` /
`Set Code` columns existed import without a link to their printing. Any other
file, or one where no card is found, is refused before "replace" deletes anything - and since
2026-10-15 so is a Moxfield replacement in which any row is unresolved (`not_found`) or has no
name (`unnamed`), as the app's own file already was: the answer names the cards, nothing is
changed, and adding instead still takes the known ones and lists the rest (`skipped` counts
not-found cards and unnamed rows both; `unnamed` says how many of them). Every upload of the
app's own CSV is written to a temporary file of its own in `data/uploads/` (`tempfile`), removed
afterwards. Tests: `tests/test_import_replace.py`.

**The database files, in common** (`storage.py`, since 2026-10-09). Every manager's connection
comes from `storage.connect`: usable from any thread (each manager has its lock), rows by
column name, WAL, `synchronous=NORMAL` (unchanged: with WAL a power cut can lose the last
commits, not corrupt the file) and `foreign_keys=ON` - SQLite enforces foreign keys only on
connections that ask. A change to an existing table is a **named migration**, recorded in the
table `schema_migrations` of the file it changed (a table, not `PRAGMA user_version`: the card
database file is shared by four managers). `storage.rebuild` does one: a backup of the file
first (`data/backups/migrations/<file>_before_<name>_<time>.db`, opened and checked - or the
migration does not start), then one transaction with every table copied into its new
definition, ids and AUTOINCREMENT counters kept, `PRAGMA foreign_key_check`, and counts and
totals compared before and after; anything else is rolled back. The older column additions
(`ALTER TABLE ... ADD COLUMN` at startup, each checked against the columns there) stay as they
are.

**Constraints** (migrations `inventory_constraints_1`, in both inventory files, and
`deck_constraints_1`): `inventory.quantity` is an integer above 0, `inventory_batches` follows an entry (`REFERENCES inventory ON DELETE CASCADE`) with a quantity above 0, `added_quantity` NULL or not
negative; `inventory_captures` and `inventory_sources` belong to an entry (`REFERENCES
inventory(id) ON DELETE CASCADE`), a station's `quantity` is above 0; `deck_cards` belong to a
deck (cascade), `quantity` above 0, `board` one of commander / main / side. The standalone program (until
2026-10-14) kept these rules as triggers on tables without CHECK or REFERENCES; a file it made is
given the rules themselves by the same migrations - they drop its triggers (`LEGACY_TRIGGERS`,
`storage.rebuild(prepare=...)`: one that names a rebuilt table made the rename fail and the server
unable to start on the file) and rebuild `inventory_batches` too. One case per rule:
`tests/test_constraints.py`, with a file in the standalone's form. So an entry or a
station's row is deleted, never kept at 0 (`_delete_entries`, `_take_copies`, `_set_source` in
`inventory.py`) - and `_delete_entries` removes the captures itself first, because the cascade
takes rows, not thumbnail files. There is no foreign key from an entry's `card_id` to `cards`:
entries outlive card data updates and other games have other card tables. Restoring a backup
made before the constraints leaves out rows that could not stand (`backups.RESTORABLE`). Run on
a copy of the collection in use (1,910 entries, 3,982 captures, 857 deck rows): every row
identical afterwards, counters kept, 0.3 s.

**Measured, not guessed** (`scripts/benchmark_db.py`, on a copy, 2026-10-09, 112,765 cards,
warm medians): a printing by set + number 0.02 ms and a card by exact name 0.02 ms (both by
index); a name by substring 34 ms and the deck builder's search 41 ms (both read every card);
the collection list 17 ms; one camera's totals among 5,000 scanned entries 4.9 ms; a camera's
oldest review item among 2,000, 0.12 ms. Done with that: `idx_deck_cards_deck` removed - the
UNIQUE index on (deck_id, card_name, board) answers the same question as fast (0.05 / 0.08 ms).
Not done, on purpose: indexes on `inventory_sources(station, inventory_id)` (4.7 -> 3.9 ms at a
size the table never has), `review_queue(game, station, id)` (0.12 -> 0.02 ms) and
`decks(game, updated_at)` (0.16 ms with 10 decks) - nothing a person would notice; and FTS5
with the trigram tokenizer for substring search, which answered in 0.05-0.14 ms (index built in
0.33 s) but would be a second structure to rebuild with every card data update and another
definition of "matches" beside the one identification relies on, for 34 ms nobody waits on.
Worth doing when the card data is several times larger.

**Imports never leave a half-filled table.** The download fills a staging table (`cards_import`) on a connection of its own (one refresh at a time), committing every 5,000 rows so the inventory can still write, and swap
it in at the end in one transaction (`CardDatabase.replace_table`: `BEGIN IMMEDIATE`, drop,
rename, indexes, the `card_data_info` row, commit - or all of it rolled back) - scanning keeps
using the old data while an update runs. Before anything is touched the import is refused when
its table is missing, empty, or holds less than half the cards in use (`MIN_SHARE`: a download
cut short); the cards in use then stay. Only `cards` / `cards_import` are accepted (`CARD_TABLES`).

**`card_data_info`** - per game: the source's own date (Scryfall's `updated_at`), when it was downloaded, and the card count.

**Update check.** 10 s after startup and then once a day, `Game.check_for_update()` runs for
each game with data. Magic: Scryfall's bulk data description (one small request) - since
Scryfall republishes every day for prices, the data only counts as outdated once it is
`database.update_after_days` (7) older than Scryfall's, or when its date is not recorded (data
downloaded before this check existed). A result
is kept in `data_update_notices`, sent as `database_update_available`, and returned by
`/api/stats`; the page puts a dot on the Database counter (click → confirm → update) and shows
one notification per page load. Updates are never started without the user.

## Focus

USB cameras start in **continuous autofocus**, unless a focus position has been locked. Measured
on an Anker PowerConf C200 looking into the box, continuous autofocus settled at a position
about 4× less sharp than the best manual position, and re-hunts whenever the image changes -
so a card dropped at the wrong moment can end up blurry.

Because the camera-to-card distance is fixed, the scanner can **lock** the focus instead:

- **Refocus** (button, `reset_focus` → `CardScanner.refocus`, on a station through the `command`
  message): switches autofocus off and runs
  `focus_sweep()` - a coarse pass over the camera's `focus_absolute` range (step 50), then a fine
  pass (step 10) around the best position, scoring each position by the sharpness of the card
  (or the image centre when there's no card); finally a parabola through the best position and
  its neighbours predicts the peak between the fine steps, which is measured and kept if sharper.
  On the C200 the sweep found 444 against a measured peak of 446 (without the parabola: 440, 4%
  less sharp). A lens move takes ~0.4 s to show up in the frames
  (lens + camera buffer), so each position waits 0.45 s; if re-measuring the chosen position
  doesn't confirm it, the sweep repeats with 0.8 s. About 10 s in total. The position is saved
  (`focus_value`, per station - on the server) and restored when the station starts.
  A station without a position of its own is given the one in the server's shared settings;
  a camera whose lens cannot be set from the station (a Pi camera, a webcam without the focus
  control) ignores it and reports continuous autofocus (`_open_camera`) - until 2026-10-12 it
  took the number over and its page showed autofocus as off. Checked with fake cameras only.
- **The lens has play**: the same `focus_absolute` reached from above measured up to 5× blurrier
  than from below (395: 22 vs 118, peak 2,480). Sweeps measure while moving up, and every final
  or restored position is approached from 30 below (`_move_focus`).
- **Refocus when the sharpness drops** (`_check_sharpness_drop`, `auto_capture.refocus_on_drop`, default
  0.35, since 2026-10-15; with a locked focus only): the sharpness of each captured card is noted
  (the last 24); when the last 3 were all below that fraction of the median of the ones before them,
  and that median was at least 600 (a soft baseline, a pile of plain tokens, says nothing), the
  station refocuses with a sweep (~8 s, as "card out of focus") and starts the history again; not
  more than once a minute; 0 switches it off. A per-camera setting (`CardScanner.refocus_on_drop`,
  `set_refocus_on_drop`, the `set_refocus_on_drop` event, Settings → Cameras → Refocus when the
  cards get blurry; `config.yaml`'s `refocus_on_drop` is the default). Why: on the Pi 5's Anker C200 the camera's own
  continuous autofocus left stretches of captures at about a quarter of the usual text sharpness
  (21:16-21:19 about half, 21:35 351 against 1,200-2,200) that never came near `min_sharpness`,
  the only trigger there was. Tested on synthetic sequences (`tests/test_sharpness_drop.py`); not
  yet seen to fire on a real pile.
- **Focus probe while scanning** (`_run_focus_probe`, every `auto_capture.refocus_every`
  captures, default 10; **Settings → Cameras → Check the focus every** changes it while scanning; the count starts from zero each time auto scanning is started - `auto_capture_enabled` is a property that resets `captures_since_focus` on off → on - and is not reset by stopping, so a session of 12 cards with a check every 20 has none, and `tests/test_focus_count.py`): the best position drifts - in one session it moved from 395 to 430 in
  about 45 minutes (lens warming up, pile height), and the text in the captures became ~10×
  blurrier while the card as a whole still passed `min_sharpness`, so the automatic refocus
  never started. In the gap after a capture (~1 s, the image is already taken) the probe
  measures the card here and one step (10) away and keeps the sharper position (> 5% better);
  an improvement keeps the direction for the next probe, otherwise the next one tries the other
  way. Near the peak the sharpness changes ~3× per step, far more than the noise. Probes
  approach their positions from only 15 below (instead of 30) to keep the card readable.
  The interval was 3 captures until 2026-10-06: in a session of 489 captures the 163 probes
  moved the focus 5 times (420 ↔ 430 ↔ 440, 1.12-1.99× sharper), OCR had read all 9 cards
  before each move, and a capture followed by a probe held the next one back 0.9 s (2.5 s
  to the next capture instead of 1.6 s). Every 10 captures still looks about every 20 s,
  far more often than the position moves (one step in 10+ minutes).
- **While the lens moves** (sweep or probe, and 0.6 s after) the new-card rules are paused: the
  blur can hide the card for a few frames and shift its outline, and the "reappeared elsewhere"
  / "card gone" rules then took the same card for a new one (a probe caused two duplicate
  captures in a live session, reproduced in simulation: 22 captures for 15 drops). A real drop
  during a probe is still recognized by its jump (> 3%; the blur shifted the outline ≤ 1.9%) -
  in fixed-area mode by the area changing > 8 in 2 frames in a row: the card counts as new
  once the focus is done, and the probe's measurement is discarded. Otherwise the card in view
  becomes the reference for "looks different from the captured one": after a probe a foil's
  glare and outline changed enough (thumbnail 0.34, other cards 0.39-0.62) that a foil was
  captured twice. In fixed-area mode only when the area is still within 10 of the capture.
  The "dropped during the probe" flag is cleared at every capture: when the dropped card was
  taken as new another way ("card gone"), the flag used to survive and made the *next* card
  "new" right after its own capture - that card was captured twice (seen 2026-09-24 and 09-25).
  Still open: a probe itself sometimes reads as a drop right after a capture ("card dropped
  meanwhile" with the same card, 2 of ~630 captures), and a card that shifts ~2% right after
  its capture (image change 0.34) can't be told from a real drop - real drops of other cards
  often measure 0.30-0.42 with jumps under 3% - so that threshold stays.
  Simulated with 30 drops every 1.5 s and a peak 35 away: every card captured once, focus at
  the peak after 9 cards.
- **Automatic refocus** (`_check_focus_drift`): with a locked focus, a card that stays still but
  below `auto_capture.min_sharpness` for 3 s triggers a new sweep (at most every 15 s) - the pile
  grows toward the camera as cards are added.
- During a sweep the status shows *Focusing* and auto-capture pauses; new-card detection is off
  until 0.6 s after it (the heavy blur changes the card image like a drop would).
- **Settings → Camera autofocus** (`set_autofocus`) returns to continuous autofocus and forgets
  the locked position; switching it off runs a sweep.

Cameras without a `focus_absolute` control (and the Pi camera module) keep their own autofocus.

### Pi camera module

`camera.type: picamera` (`_initialize_picamera`). The sensor runs at its full resolution and
only `camera.crop` of it (fractions x1, y1, x2, y2 of the whole image, in the station's
`config.yaml`) is read out, pixel for pixel: a wide lens sees far more than the box, and the
cropped frames are small enough to be used as they are for detection, the live view and the
capture (no half-size decoding). Without a crop the whole image is scaled to
`camera.resolution`. Autofocus is continuous, over the whole lens range, measured inside the
crop. The frame rate is what the sensor's full-resolution mode gives, not `camera.fps`.
A rotation of 180° is done by the camera (the crop is mirrored to stay on the same spot) and
the frames arrive in RGB order; 90° / 270° are turned per frame as for other cameras. Changing
the rotation reopens the camera (`reopen_camera`, done by the capture thread).

The camera also delivers a small copy of each frame (640 px on its longer side, the
detector's working size; `current_small`): the outline is searched there (`_detect_outline`, corners scaled back to the frame) and the debug trace
saves it. Scaling the full frame down for the detector had taken half of its 33 ms.

**Frame counts and frame rate.** "Card gone" - no outline for 6 frames - was measured at 20
frames a second, where it is 0.3 s. For the Pi camera it is counted as that time at the rate
the station really runs at (`_frames`: 4 frames at 14 a second, never fewer than 3). The still
frames before a capture are *not* shortened: this camera's autofocus is still settling after
a drop, and a replay with fewer still frames captured one card twice. USB cameras are paced
to `camera.fps` and keep their numbers.

Tried on 2026-10-11, a Raspberry Pi 4 with a Camera Module 3 Wide NoIR (IMX708, 4608x2592,
102° wide) about 19 cm above the bottom of the box, crop `[0.33, 0.08, 0.67, 0.75]` =
1566x1736 px:

- Frame rate: the camera gives 14-15 a second in this mode. Turning the image by 180° and
  reordering its colours per frame took 24 ms of a 70 ms frame on the Pi 4, and with outline
  detection (40 ms) the loop missed every other frame: 7 a second in the first session of 15
  drops, 1-3 s from drop to capture. With both done by the camera: 14.2 a second (12.2 with
  the debug trace on, 10 with a 90° rotation) - measured on the empty box, not yet over a
  session of drops.
- That session (15 cards, ~140 lux, 60 ms exposure): every card captured once and confirmed by
  OCR, the ★/• markers right on the six looked at by eye. Three cards waited 9-18 s: for two
  no outline was found (the card's lower edge in the shadow of the box wall) until the
  autofocus rescanned by itself; the third is unexplained.
- Second session (17 cards, camera lowered and turned, ~1,700 lux, 7.7 frames a second with a
  card in view, the trace on and the live view watched; the Pi at 75 °C): 15 captured and
  read by OCR; one lay 17 s without an outline - its left edge against the shadowed wall of
  the box - and was captured by hand (read by the AI from the whole frame); and one, the
  third copy of a card, was **never captured**: it landed 0.4% from the copy before it, the
  fall hid the card for 3 frames (0.4 s), and "card gone" asked for 6. This led to the two
  changes above. Replaying the session's trace frames through `CardScanner` (a fake Pi camera
  at the recorded pace) reproduces the loss with the old count and captures the card with the
  new one, with the same captures otherwise; the first session's frames replay the same with
  both. The replay is on 640 px trace frames, not the camera's own.
- With the small frame from the camera: 14.4 frames a second on the empty box, also with the
  trace on and the live view watched (61 °C).
- Third session (17 cards, the changes above in place; 10.9 frames a second with cards, the
  Pi at 77 °C): all 17 captured once, the first 13 about 2.3 s apart. Three of the last four
  waited 11-27 s until the box was shaken: the card lay against a wall, its outline was found
  in about every other frame, and six still frames in a row never came. That led to the
  detector's gap closing (step 7 of [the outline detector](#card-detection)).
  Replaying the session with it captures those cards 1-2 s after they land, with no capture
  added. Replays of the first session (camera further away, frames smaller than the detector
  saw them live) show two repeat captures with it, where the outline of a still card dropped
  out for a moment and where the autofocus rescanned by itself - both can happen live.
- Fourth session (the same 17 cards, gap closing in place): 17 captures in 38 s, 2.4 s a card,
  none waiting, all read by OCR; the count of each card and of its foils is that of the pile
  (2, 2, 2 with 1 foil, 3, 8 with 2 foil), so none twice and none lost. The Pi at 70 °C.
  One session: the repeat captures the replay warned of have not been seen, nor ruled out.
- The detector with a card in view took 67 ms a frame on the Pi 4 (16 ms on the empty box),
  so with cards the station fell back to 7.7 frames a second. Three of its helpers were made
  cheaper without changing what they compute (`_card_inside_box` skips specks by their
  bounding box, `_outline_bands` works on the outline's surroundings instead of the whole
  image, `_perimeter_coverage` without a Python loop), and the camera's small frame is now
  exactly the detector's working size (no rescaling): about 40 ms, and 13.9 frames a second
  with a card in view. Checked on all 2,452 recorded frames (Pi and USB camera), alone and
  following the previous outline: every outline and score identical to before.
- **In Docker** (target `client-pi`, `deploy/docker-compose.client-pi.yml`): Debian 13 with Raspberry
  Pi's package archive for `picamera2` / libcamera - its keyring package is fetched over HTTPS
  from the archive's package list, because the key file offered for download is signed with
  SHA-1, which Debian 13's apt refuses. The container runs privileged with `/run/udev`
  mounted: a Pi camera is a chain of devices (sensor, image processor, picture memory) whose
  numbers differ between models. The image is 1.33 GB and built in a few minutes on the Pi 4.
  Tried: set up through its page, connected, 14 pictures a second, pictures taken from the
  page. No session of cards has run from the container yet.
- **Heat**: with a heatsink the Pi 4 ran at 77-79 °C while only watching the box, 1-3 °C under
  the point where it slows itself down. It was never seen throttling, but it has no margin.
- **Container against the Pi's own Python** (`scripts/benchmark_station.py`, the same camera,
  crop and card in view, two runs each, alternating, 20 s a phase):

  | | Python | Container |
  |---|---|---|
  | Scanning, frames a second | 14.3, 14.2 | 14.3, 14.4 |
  | ... processor (100% = one core) | 93%, 119% | 92%, 93% |
  | Scanning with the live view, frames a second | 14.3, 14.0 | 14.3, 14.3 |
  | ... processor | 140%, 165% | 138%, 139% |
  | Detector alone, ms a frame (afresh / following) | 22.6 / 32.3, 22.5 / 32.1 | 25.1 / 34.7, 23.0 / 32.6 |

  No difference beyond what two runs of the same thing differ by: both run the same Debian
  packages on the same kernel, and a container adds no layer between a program and the
  processor. 14.3 is the sensor's limit in its full-resolution mode.
- A card on the bottom of the box is 637x891 px, about 10 px per mm; a manual capture of it
  was confirmed by OCR (name, number, set, foil marker) in 0.5 s. One card, once.
- The lens: sharpest for that card at lens position 0, the far end of its range, where things
  a metre away stay blurred - on this module the range lies closer than its scale says. The
  bottom of the box is at its edge; a pile brings the cards into it. Not looked into further.
- Sharpness (Laplacian, the card's area) was about 200-250 there, around the
  `min_sharpness` of 250 that auto scanning asks for - a number measured with the USB webcam.

## Collection page and decks

**The decks are pages of their own since 2026-10-13** (`/decks`, `/decks/<id>`; before, a tab
of the collection page). What follows about decks describes what they do, which did not
change; how they look did. *The list*: a tile per deck with its commander's picture and
colours (`GET /api/decks`: `commander_cards`), filter and sort above; the other two tabs hold
"Build around a card" with "Commanders you own", and the preconstructed decks. *A deck*: a
banner (the commander's picture, name, format, legality, cards, value, share owned), the
charts in one strip, and the cards in one of two views (`decks.js`: `showDeckView`,
`deckGroups`) - **Columns** (`deckRowHtml`: a decklist in as many columns as fit; pointing at
a line shows the card beside it and the line's buttons) and **Stacks** (`deckCardHtml`: the
cards themselves, overlapping, one stack per group; pointing at one pulls it out) - grouped by
type, by cost or by colour (`groupOf`). The buttons are the same in both
(`entryActionsHtml`): one copy more or less, the printing, move to main / maybe, make
commander. Adding cards - search, suggestions, popular decks - is a panel that slides in
(`showAdding`); buy list, duplicate and delete are behind a menu. The view, the grouping and
the list's tab are remembered in the browser. Made for a screen with a pointer: on a phone
nothing can be pointed at, and the pages were not laid out or tried there.
**The scanner page's timers and handlers** (since 2026-10-16): `everyWhileVisible` (`scanner.js`) runs the detection
poll (500 ms), the stats (5 s), the camera link (15 s) and the picture-shape check (1 s) only while the tab is
visible and once at once when it comes back; the detection poll never has two requests open. The printings and
similar-card lists hold their value in `data-printing` / `data-similar` with one delegated click handler (a card
named `A\B` or `It's` broke the old `onclick="f('...')"`), and `escapeHtml` takes null / undefined / numbers.
`scripts/check_scanner_page.py` tries these in headless Chromium against a server on copied data that knows a
station called `check`. Not done: the inventory modal still rebuilds every row on an update (not measured as a problem).

`scripts/check_deck_pages.py` tries the pages in headless Chromium (on a server with copied
data only: it creates and deletes decks).

**Scanned cards and the collection are two inventories.** Scanning - every station - adds to
`scan_inventory` - a second `InventoryManager` on its own file, `data/scan_inventory.db`, with
the same tables - and its top bar counter, inventory window, edit / delete, Undo, export, import
and "Clear all" work on that one only (`?area=scan` on the `/api/inventory*` endpoints,
`inventory_area()` in `server/inventory_api.py`; `/api/stats` reports it as `inventory` and the collection as
`collection`). So a scanning session can be checked, corrected or thrown away without touching
the collection. **Add to collection** (`POST /api/scan_inventory/to_collection`,
`InventoryManager.take_from`) moves every scanned entry of the active game - or, with a camera
chosen in the list, that camera's copies - into the collection
(`inventory` in `cards_database.db`): entries that exist there get the copies added, tags are
joined, and the capture thumbnails follow (their rows move, the files stay). Both pages ask
first, with one dialog (`addScannedToCollection` in common.js; `GET` on the same address gives
the number of cards waiting, the locations in use - offered in a dropdown, the ones named after a deck under a "Decks" heading - and the decks' names): a **location** chosen or typed there (JSON
`location`) is given to every card moved, in place of the one it was scanned into; empty keeps
those. It commits the
collection first and then empties the scanned cards - two files, so two commits. A crash
between them must not leave the cards in both (a second "Add to collection" would double
them): the move is first noted in the scanned cards' file (`pending_moves`: game, a move id),
the id is written to the collection (`arrived_moves`) in the commit that brings the cards, and
the note goes in the commit that empties the scanned cards. On startup
`finish_interrupted_moves` finishes a move whose id arrived and drops the note of one that
did not (the scanned cards are then still waiting). An error while copying rolls the
collection back. Decks, statistics and "owned" only look at
the collection; the collection page shows a notice while scanned cards are waiting.

**Batches** (`inventory_batches`, since 2026-10-14). Which copies of an entry came in together
is one row per batch and entry: `batch` (an id, unique for each time cards came in - two in the
same second are still two), `inventory_id`, `added_at`, `quantity`. A batch is one "Add to
collection" (`take_from`: one for the whole move, also with a camera chosen), one import file
(`import_entries`, `import_csv`, `replace_all`), the cards of one precon deck added at once, or
one scanned card (`add_card`; `new_batch()` + `batch=` to make several cards one). An entry's
rows never add up to more than its quantity; copies no row accounts for were there before
(shown as "Added before <time>"). They follow the copies: a split (`_copy_with`) and a merge
(`update_card`) move the **newest batch's** copies first - as the newest captures go - and a
lowered quantity, an undo (its own batch first), a station's copies taken out or a removed
camera count (`_take_copies`) take from the batches the same way (`_take_from_batches`,
`_trim_batches`). The collection page filters by batch ("Added <time> (n cards)", from every
entry's `batches`) and, with one chosen, offers **Remove this batch**
(`POST /api/inventory/remove_batch`, JSON `batch`; `InventoryManager.remove_batch`): each
entry loses only the copies that batch brought, with their newest captures and the cameras'
counts; entries with no other copies are deleted. The cards are deleted, not moved back to the
scanner. `added_at` / `added_quantity` in an entry's JSON are its newest batch's (0 copies: all
of them were there before); the entry's own `added_at` column (the page's "Last added" sort) and
`added_quantity` column are still written, and `added_quantity` is no longer read. The table is
made on startup and, once (`inventory_batches_1`), filled with the one batch each entry knew of
(`old:<added_at>`) - nothing is filled where the table has rows already, as the standalone
program's had. A backup holds the table (`backups.INVENTORY_TABLES`); one made before it gets
the same one batch per entry when it is restored (`rebuild_batches`). `tests/test_batches.py`.

### Trades

Cards promised to someone stay in the collection until the trade has happened
(`trades.py`: `Trades`, made on the collection's `InventoryManager` - its connection, its lock;
tables in [Database](#database)). **Set aside for trade** on the bulk bar asks for a name
(`POST /api/trades`, JSON `name`, `ids`): the open trade with that name, whatever the case, or
a new one, takes every copy of the selected entries that no trade holds yet. A list row has
the same as its own button (⇄, `setAsideForTrade(card)`), for one card without ticking it. Nothing in the
inventory changes: the cards are still owned - totals, statistics and decks count them - and
their rows get a **Trade: name** badge (`/api/inventory` gives each entry `trades`:
`[{id, name, quantity}]`, `Trades.by_entry`); `trade:name` in the search field finds them.

The **Trades** tab (`GET /api/trades`; `collection.js`: `loadTrades`, `renderTrades`,
`tradeAction`) lists the open trades with their cards, value and date. Per card: **−** / **+**
change how many copies the trade holds, **×** takes the card out
(`PUT /api/trades/<id>/cards/<row>`, JSON `quantity`, 0 = out). Per trade: **Export…**
downloads its cards in one of the game's export formats (`GET /api/trades/<id>/export/<format>`
- Moxfield's collection CSV for Magic, or the app's own; `quantity` is the copies in the
trade), **Rename** (`PUT /api/trades/<id>`), **Cancel trade** (`DELETE /api/trades/<id>`: the
trade and its rows go, the cards are simply free again) and **Confirm trade**
(`POST /api/trades/<id>/confirm`, `Trades.confirm`): in one transaction each row keeps the
card as it was and lets go of its entry, the copies leave the collection - the entry is
deleted or lowered, with the newest photos and the cameras' counts as for any lowered quantity
(`_take_copies`, `_trim_sources`, `_trim_captures`) - and the trade becomes `done`. It stays
under **Confirmed trades** as a record (cards, value, date; it can still be exported) until
**Delete from history** (`DELETE` again).

While a trade holds copies, the database refuses everything that would take them: deleting the
entry, lowering its quantity below what is held, moving a whole stack of several copies to
another location or merging the entry into another one by an edit (both delete the entry; a
single card is changed where it is and stays in its trade), "Remove this batch", clearing
the inventory, an import that replaces it. The page shows "These copies are set aside for a
trade: take them out of the trade first" (`TRADE_HELD`; `server/trades_api.py`: `write_refused`). Copies the
trade does not hold can be changed as always, and so can condition, tags and price. Bulk
delete and bulk move, (one edit since 2026-10-14: all entries or none) are refused before anything changes when
one of the entries is held. Trades are in every backup (`backups.INVENTORY_TABLES`): a restore
brings back the trades of that moment with their entries - a trade confirmed since is open
again, its cards back - and a backup from before trades restores with none.

Never tested: two browsers changing one trade at the same moment (each change is one
transaction under the collection's lock, so the second sees the first's result or is refused).

### Backups

They are on the settings page, **Collection and data** (see [The settings page](#the-settings-page)).
**Back up now** (`POST /api/backups`, `backups.create`) writes `data/backups/<date_time>/`:
`backup.db` with plain copies of `inventory`, `inventory_captures`, `inventory_sources`, `trades` and `trade_cards` of the collection
(`collection_*`) and of the scanned cards (`scanned_*`), `decks`, `deck_cards` and an `info`
row (time, note, counts) - every game - and `captures/`, hard links to the thumbnails those
entries point at (no extra space; they survive the app deleting its own). It is written to a
`.tmp` folder and moved into place, under the managers' locks. Since 2026-10-13 a backup
also holds the digital collection (`arena_inventory`, `arena_arena_account`:
`ARENA_TABLES`; `arena` in its info counts the cards). Restoring a backup that has no such
part - every one made before - leaves the digital collection as it is; the full backup
takes `data/arena_inventory.db`. (On temporary files in the tests; not tried on a running
server.)

**Automatic backups** (`backups.create_scheduled`, marked `daily` in their info whatever the
interval) are asked for at startup (`initialize_components`, note "Application start") and
every ten minutes while the app runs (`run_backup_schedule`, note "Automatic") - a server stays
up for weeks, and a backup made only at startup would never come. One is made when the last
automatic backup is as old as the interval; none while nothing has changed since the newest
backup of any kind (`fingerprint` in a backup's info: a hash of every row it holds), and none
of an empty collection - both decided before anything is written or cleared away, under the
managers' locks. The interval and how many are kept are set in the drawer
(`POST /api/backups/schedule`; `backup_every_hours` - 0 off, 1, 6, 12, 24, 168 - and
`backup_keep`, 1-60, in `data/settings.json`; default every day, the last 7); older ones are
deleted when a new one is made, not when the number is lowered. A failure is logged and does
not stop the app. Backups made by hand are never deleted automatically.

**A full backup** (`POST /api/backups/full`, `backups.create_full`, used by
`scripts/backup.sh` while the server runs) is everything at one moment: the three database
files (SQLite's backup), hard links to the pictures their rows name (capture thumbnails, review
images, captures waiting to be read) and the settings files, in
`data/backups/full/<date_time>/` with a `manifest.json` (rows per table, pictures, the ones
missing, `verified`). It is taken holding every manager's lock (in the order `take_from` uses)
and `maintenance_barrier`, which a capture being settled and the clean-up of old pictures hold
too - so a card on its way to the collection or a capture being added is in none of the
snapshots or in all of them; the files are opened and checked after the locks are let go. The
last 2 are kept. `scripts/backup.sh` (which runs with `umask 077`: its archive holds the API keys, so the archive is 0600 and `~/scanner-backups` 0700, the older ones tightened on the next run - `tests/test_backup_script.py`) then archives those snapshots with the rest of `data/`
(without a running server it snapshots each file on its own, which is the same when nothing
runs). Bringing one back is done by hand with the server stopped (INSTALL.md). Tested on
temporary data (`FullBackup`: opened in another folder, an interrupted move in it is finished)
and against an isolated copy of the server; **not tested**: restoring a whole server from one.

The backups of the list above are in
`data/backups/`, on the same disk as the data: against a lost disk, download a backup - the
arrow beside it, `GET /api/backups/<id>/download`, `backups.archive`: a zip of its folder
(`<id>/backup.db`, `<id>/captures/`), packed into a temporary file. **Upload a backup
file** (`POST /api/backups/upload`, `backups.add_archive`) takes such a zip in again, from this
server or another: it only joins the list (marked `uploaded`, kept like one made by hand) and is
restored like any other. The file is not trusted: exactly one backup folder, `backup.db` and
captures with plain names, at most 2 GB unpacked (counted while unpacking, not as the file
claims), a `backup.db` whose `info` and tables read; a backup of the same time already there is
refused. It is unpacked into a `.tmp` folder and moved into place.
(`scripts/backup.sh` archives everything.) Covered by `tests/test_ownership.py`
(`ScheduledBackups`); the ten-minute timer itself and the drawer's controls were not tested.

**Restore** (`POST /api/backups/<id>/restore`, `backups.restore`) first makes an automatic
backup of the current state ("Before restoring ...", the last 5 are kept - never clearing away
the backup being restored, which may itself be the oldest of them), then replaces the
rows of each table (the columns the backup has; row ids are kept) and links missing
thumbnails back. Collection, scanned cards and decks are committed one after the other - the
collection and the decks are two connections to one file - so a failure part way leaves the
earlier parts restored; the automatic backup has the state from before. The automatic backup
and the replacement are one critical section - the route holds the maintenance barrier, then
`restore` holds every manager's lock from before the backup is made until the last table is
in place (`tests/test_restore_locking.py`): a card accepted in between would be in neither the
backup nor the restored tables. A capture arriving meanwhile waits and is added to the
restored state. Every page reloads its inventory (`inventory_updated`). Card data, the review queue, the stations and settings are not
part of these backups: copy the server's `data/` folder for everything (`scripts/backup.sh`
does that for an installation without Docker).

`/collection` (`templates/collection.html`, `static/js/collection.js`) works on the active
game's inventory over the REST endpoints; it listens to `inventory_updated`, `inventory_undone`
and `inventory_prices_updated` to follow what is scanned meanwhile, and reloads on `game_changed`.
A change to a trade is announced as `collection_updated` (`server/trades_api.py`: `collection_changed`, no
payload), which only this page listens to: a scanner page takes `inventory_updated` for a card
that was just added and would drop the card it is showing.

**Inventory tab.** `/api/inventory` adds to every row its `location`, `tags` and `details` from
`Game.card_details` (Magic: image, mana value, color identity). Filters (text,
color identity, type, rarity, set, finish, location, tag, price), sorts, the list / image grid
and the statistics are computed in the browser from that one response. The text field looks
for what is typed anywhere in name, set name, set code, type line, rarity, location and tags;
a term `field:value` (`collection.js`: `SEARCH_FIELDS`, `parseSearch`) looks in one place:
`set:` is the whole set code when the collection has a set with that code (`set:HOB` does not
find "Hobbiton"), otherwise part of the set's name; `name:` and `type:` are part of the text,
`rarity:` its beginning, `tag:`, `loc:` / `location:`, `finish:` and `number:` the whole value
(`loc:""`: no location), `qty:` the number of copies (`qty:>4`, `qty:<=2`, `qty:3` - the
entries with spare copies), `trade:` part of the name of a trade that holds copies. Quotes
keep spaces together, a minus in front turns one term around
(`-set:HOB`), and a word with a colon that names no field stays ordinary text. **Not** turns
the whole field around. The filters are kept in the browser (`localStorage`: `collectionFilters`,
`saveFilters` / `restoreFilters`) and are there again on the next visit - all but the "Added"
batch; a set, tag or location that is gone from the collection is
not filtered by, and **Clear filters** can only be pressed while something is filtered. What
narrows the list is also shown as chips above it (`activeFilters`: "Rarity: rare ×"), each
taken off with a click, and is written into the page's address (`FILTER_PARAMS`:
`/collection?q=qty%3A%3E1&rarity=rare&colors=G`; `history.replaceState`, so no history entry
per filter) - an address with filters, a bookmark, wins over the remembered ones. A click on a
row's rarity, finish, location, tag or trade badge filters by it, a second click takes that off
(`filterByBadge`). Type, Rarity, Set, Finish,
Location and Tags are dropdowns with ticks (`MULTI_FILTERS`, `multi`, `renderMulti`,
`fillMulti`, `passesMulti`): several values can be ticked - a card shows when it has *any* of
them - and a **Not** tick at the top of the list turns that around (every card with none of
them). A list of more than 12 values has a Find box. Each is one chip ("Not Set: Alpha, Beta"),
and is written into the address as `rarity=rare|mythic&rarity_not=1` (the values joined by a bar:
a set's name can have a comma; `type`, `rarity`, `set`, `finish`, `loc`, `tags`), so a bookmark
or a saved filter with one plain value still works. A row's badge ticks only that value (a
second click lets go). The one-tag list ("Any tag") stays beside the Tags dropdown. The Arena
view does not apply Location or Tags. `tests/test_multi_filter.py`. A list row shows the card twice: on the left what was captured (a click opens the captures),
and by its buttons the downloaded picture, small (`.card-peek`; Scryfall's `small` size of the
`image_uri`) - resting the pointer on that one shows the picture large beside it (`data-image`,
`showPreview`, `placePreview`, as for the deck builder's rows; devices with a mouse - the box
is not there on touch screens and narrow windows). The rest of the row shows nothing: a
picture that came up anywhere in the row was in the way. The large box appears at once,
card-shaped, and the picture when it has loaded; the previous card's picture is dropped first. The
list is shown in pages of 25 / 50 / 100 / 200 entries (100 until chosen, `collectionPageSize`;
`showPage`): a changed filter or sort starts at the first page, a reload after an edit stays
on the page. A tick with Shift held (`pick`) ticks or unticks every entry from the one ticked
before to this one, in the order shown and also across pages. **Export…** asks which cards
when some are ticked or the list is filtered - everything, the ones shown, or the ones selected
(`POST /api/export_inventory/<format>` with JSON `ids` answers with the file of those entries;
the `GET` is everything, as before).

**Undo.** A row's bin and every bulk action (delete, move, tag, condition) wait `UNDO_SECONDS`
(6 s) before the server hears of them (`changeLater`): the rows are greyed, a bar at the bottom
says what is about to happen and has **Undo** (also Ctrl+Z), which simply drops the change.
When the time is over the change goes to `POST /api/inventory/bulk` (`sendPending`). It is
never left behind: the page's `fetch` is wrapped so that any other write waits until the
pending change has been sent, a second change sends the first (the calls are taken one after
the other, `changesAsked`), and leaving the page sends it on the way out (`pagehide`; the
request is `keepalive`, so it is also finished when the page is left while it is on its way). The single delete no longer asks first; the bulk
delete still does. A change the server refuses (a card a trade holds) is reported when it is
sent, not when it is clicked. Never tested: closing the browser during the six seconds on a
phone.

**Keys** (`inventoryKey`; Inventory tab, no dialog open, not while typing): `/` goes to the
search field (Esc leaves it), Esc clears the selection, ← / → turn the page, Ctrl+A selects
everything shown, Del deletes the selection (asks), Ctrl+Z undoes the waiting change.
Selected entries get the bulk bar (`POST /api/inventory/bulk`). Rows also carry `decks`:
the names of the decks that use the card (by name, `DeckManager.needed_by_name`; a Commander
deck's considered cards don't count) - shown as a badge, and hidden by the **Not in a deck**
tick. A deck lists names, not copies, so the location decides which copies are its own
(`decks_using` in `server/decks_api.py`): when the location named after the deck holds as many copies as the
deck plays, only the entries there count for it and the same card elsewhere is free (the basic
lands in a box, next to the ones in ten precons); with fewer copies there - or no such
location - every entry of the card counts. The deck builder has the same idea as **Not in other decks** on the card search
(`free=1&deck_id=` → `search_cards(exclude_names=)`) and on the suggestions (`elsewhere`). The deck
builder goes by card name, not by copies: a card with three copies owned and one in a deck is "in a deck".
There used to be a second tick, **No use in my decks**, that also hid the cards EDHREC lists for
the commander of one of the decks; it was taken off the filters on 2026-10-14 because it needs the
internet (the page waited for EDHREC) and became **Find cards to trade or sell** on the Trades
tab (`collection.js`: `findSpareCards`, `spareCards`, `renderSpare`): asked by its button, never
on page load; the list is the entries with no deck and no EDHREC suggestion, with the copies no
trade holds, most valuable first, for one location or all (`spare-location`: before the search every location
of the collection, after it those with spare cards and how many); its ticks go to `setAsideForTrade(null, ids)` like the
Inventory's (`GET /api/inventory/suggested` →
`Recommendations.commander_cards` per deck with a commander, matched to the owned names by front
face; asked when the tick is set, one request per deck the first time, then from the 7-day
cache). Decks EDHREC has no answer for come back in `unknown` and are named in a notification.

**Decks** (Magic; a game without `Game.deck_formats` has no Decks tab). A deck (`decks.py`) is a
name, a format and `deck_cards` rows: card name, count, board (`commander`, `main`, `side`) and
a printing id for the image. The card is identified by its name, so any printing owned counts.
Nothing a deck does changes the inventory.

**A deck for MTG Arena** (the deck page's **Arena** button; `GET /api/decks/<id>/arena`, and as
a file `.../export/arena`). Arena, the digital game, makes a deck from a list on the
clipboard (its Decks screen: Import). `games/mtg_decks.py: format_arena` writes that list:
sections `Commander`, `Deck` and, for a 60-card deck, `Sideboard` (a Commander deck's
considered cards are not written); lines `4 Card Name` - names only, no set and number: the
deck's printings are paper ones, which Arena often does not have. Only cards that exist on
Arena are written, and the window names the rest: a card is taken to be on Arena when it is
legal, restricted or banned in a format only Arena has (`ARENA_FORMATS`, `on_arena` - mostly
`timeless`; the card table does not keep Scryfall's `games` list). A two-part name is written
as Arena does as far as known (`arena_name`: split cards and Rooms with both names, adventure
cards and double-faced cards by their front), decided from the type line. Of the laptop's
test decks, paper Commander precons, 66 to 75 of 100 cards are on Arena - Arena has no
Commander, and such a list imports as a Brawl deck at best. The window's Copy works on plain
http too (`copyArena`: `navigator.clipboard` exists only over https or on the machine
itself). **Compared with a deck Arena exported** (2026-10-13, a 60-card deck the user copied
out of Arena): Arena writes the heading `Deck` and lines `1 Llanowar Elves (M19) 314` - the
same heading, and its three adventure cards by their front name, as `arena_name` does. Pasted
into "New deck", that text was read whole by the deck import (60 cards, none unknown, the
adventure cards found by their front name); written back with the Arena button it gave the
same cards and counts, and every card was judged to be on Arena (27 of 27 the card data
knew by name; the three others it has under their two-part names). **Tried in Arena by the user the
same day**: a list of four lines without set and number (`4 Llanowar Elves`, `2 The
Arkenstone`, `1 Beorn, Reluctant Host`, `7 Forest` under `Deck`) was imported; exported
again, Arena had chosen the printings itself (the four Llanowar Elves as one of one
printing and three of another). So names alone are enough, and an adventure card is found
by its front name. **Still not tried**: a whole deck written by the Arena button, the
`Commander` and `Sideboard` sections, a split card, and what Arena says about a card it
does not have.

**The digital collection (MTG Arena)** - since 2026-10-13; plan and record in
`docs/plans/ARENA_COLLECTION_PLAN.md`. A third inventory, `arena_inventory`
(`data/arena_inventory.db`, the same tables once more), holds what the user has in Arena.
Arena gives no list of that; an exporter - the same author's
[mtga-export](https://github.com/filipesibinel/mtga-export), a separate program for Linux
(Steam / Proton) - writes one while the game runs, and
the collection page's **Arena** view uploads its files (`POST /api/arena/import`,
`server/arena_api.py`): `mtga_collection.json`, `mtga_decks.json`, or both - each replaces
its own part (the cards; the decks Arena brought) and leaves the other. The cards are
stored **without a price** and the Arena view shows none (`priceText`; no value in the
summary or the statistics): what a paper printing costs says nothing about a digital card.
The filters the view hides - price, location, tag - are not applied there either
(`applyFilters`), or a minimum price remembered from the paper view would empty the list.
`core/arena.py` reads them (`read_collection`, `read_decks`; anything else is refused -
`NotAnExport`) and finds each entry's card (`find_card`, `resolve`): by Arena's set and
collector number **only when the card there has the entry's name** (fronts compared),
else by name. The reason, measured on the one export there was (1,972 entries): 1,891 are
the printed set's number, but Arena numbers the basic lands of one set its own way (its
Forest LCI #400 is the printed set's Mountain), 26 are *rebalanced* cards ("A-Ancestral
Katana": the number of the card they change - skipped, as the exporter's own totals do),
and 49 are in Arena-only sets the card data lacks. Everything is read and looked up before
anything changes; then `InventoryManager.replace_all` swaps the contents in one
transaction, with the account (`arena_account`: the export's date, wildcards by rarity,
gold, gems, how the import went) written in the same one. **An import replaces
everything** - Arena has the original - and the list is **read only**: the page hides
what changes a collection (`body.arena-view` in `collection.css`), its handlers refuse
(`collection.js`: `inArena`, `toggleSelected`, `changeLater`, `setAsideForTrade`), every
write of the page names its area (`areaQuery()`), and the server refuses a write to
`?area=arena` (`inventory_api.py`: `digital_collection_is_read_only`,
`CHANGES_AN_INVENTORY` - a new route that changes an inventory belongs in that set). The
second and third are not decoration: an Arena entry's id sent without its area is an id
of the *paper* collection. `GET /api/arena` gives the numbers of the page's bar; an
import or a restore sends `arena_updated` to every page (no data; the collection page's
Arena view and the deck pages read again).

**Decks are of two kinds**: `decks.kind` is `paper` (Physical on the pages; every deck
from before) or `arena`, and `decks.arena_id` is Arena's id of a deck an import brought
(migration `deck_kind_1`). The kind says which collection a deck counts against
(`decks_api.py`: `collection_of`): owned copies, the other decks that want them
(`needed_by_name(kind=)` - a paper and an Arena deck never compete), the buy list, the
card search (`?kind=arena`), EDHREC's suggestions. An Arena deck's basic lands count as
owned (`is_basic_land`: Arena gives them freely); what it lacks is counted in wildcards
(`wildcards`: `needed` by rarity from `arena.wildcards_needed`, `have` from the account) -
the rarity is the card data's, i.e. of a printed card, so the count is close and not
checked against Arena; and a card Arena does not have is a problem of the deck ("Not on
Arena"), not a wildcard. `DeckManager.replace_arena_decks` replaces, in one transaction,
the decks the last import brought by the ones this one brings (a deck still there keeps
its cover); decks made here, also Arena ones, have no `arena_id` and stay. So a change
made here to a deck that came from Arena lasts until the next import with a deck file.
The deck list filters All | Physical | Arena (`decks.js`: `deckKindShown`); New deck asks
for the kind, and so does **Duplicate** (`POST /api/decks/<id>/duplicate` with `kind`;
`DeckManager.duplicate(kind)`): a copy of the other kind is the same list counted against
the other collection - an Arena deck copied as Physical shows what of it is on the shelf
and what the rest costs to buy (cards cannot be bought singly in Arena, on paper they
can). Such a copy is named "... (Physical)" / "... (Arena)" and is a deck made here. Arena's formats were added for these decks (Historic, Timeless, Alchemy,
Brawl, Standard Brawl - keys of Scryfall's legalities like the others) and any deck can
have them. **"What can I build?"** has the same choice, Build from Physical | Arena
(`decks.js`: `buildKind`, `showBuildKind`): it decides whose cards "Build around a card"
lists (`/api/cards/search?kind=arena`), which collection the two rankings count as owned
(`collection` in the body of `POST /api/decks/ideas/<commanders|card>`, echoed in the
state so results of the other collection are not shown), and the kind of a deck started
there - from a commander of the Arena collection as a Brawl deck. The ranking of
preconstructed decks stays with the paper cards. **The Arena window of any deck** now also answers "can you build it in
Arena?" (`arena_check`, `check` in `GET /api/decks/<id>/arena`; `decks.js`:
`arenaCheckHtml`): copies already in the Arena collection, the cards to craft and their
wildcards beside the ones the account has, and how many copies are of cards Arena does
not have. **Measured** (test servers, copies of the laptop's data, the user's export):
3,061 cards in 1,946 entries, the exporter's own totals, the same count for every card
name; three decks in, fully owned; each file imported alone; `scripts/check_arena_pages.py` 32 of 32 (on copied
data only: it imports, and makes and deletes a deck). **Not tried**: a sideboard or a
commander in the exporter's deck file, another version of the exporter, the wildcard
counts against Arena; of "What can I build?" for Arena, the results of the two rankings
(they ask EDHREC and Archidekt - only that they start for the right collection). The
user looked at the pages and imported through the page's button in a real browser
(2026-10-13, a preview server on copied data).

**A deck's cover** is the card whose picture stands for it: the deck list shows every deck as
that card, and the deck's banner has its art behind the name. `decks.cover` holds the name of
the card chosen (the **Cover** button among a card's buttons; `PUT /api/decks/<id>` with
`cover`, `''` to give the choice up) - a column added on 2026-10-13 by the named migration
`deck_cover_1` (`storage.rebuild`: the file copied first, every deck compared before and
after; tried on a copy of the real database - 15 decks and 969 deck cards unchanged). What is
shown is `deck_covers` in `server/decks_api.py`, in the answers as `covers`: the card chosen
while it is in the deck; else the commanders in the order they were added - a deck with two
shows the first, and a button turns to the other (which one is showing is the browser's to
remember, `decks.js`: `coverIndex`, `turnCover`); else the deck's most valuable card that is
not a land. A card taken out of the deck stops being its cover without anything being
written. `deck_payload` in `server/decks_api.py` puts together what the
page shows:

- card data: one printing per name (`CardDatabase.cards_by_names` - the newest one that has an
  image and exists non-foil, so foil-only inserts and collector editions are not the default)
  and the cheapest price among all printings, which is what the deck and buy-list totals use.
  The image shown is the entry's own printing (`deck_cards.card_id`): the search result
  clicked, or the printing a precon / Moxfield / Archidekt list names (`scryfall_id` in
  `resolve_entries`). The set code + number button on a deck row opens the printing picker
  (`GET /api/cards/printings?name=`: every printing, the ones owned first; choosing one sends
  `printing` to `POST /api/decks/<id>/cards`). The printing only decides the picture - any
  printing owned counts as owned;
- `owned`: copies in the inventory over every printing, finish and location
  (`InventoryManager.owned_by_name`), and `elsewhere`: other decks with the card
  (`DeckManager.needed_by_name`) - the page shows *owned*, *shared* (owned, but other decks
  want more copies than there are), *n of m* or *missing*;
- `issues` from `games/mtg_decks.check_deck`, which reports and never blocks:

| Format | Checked |
|---|---|
| Commander | exactly 100 cards with the commander; one copy of each card; every card within the commanders' color identity; `legalities.commander`; a commander is a legendary creature (or says it can be one); two commanders need Partner, Friends forever, a Background or Doctor's companion |
| Standard, Pioneer, Modern, Legacy, Vintage, Pauper | at least 60 cards; at most 15 in the sideboard; at most 4 copies over main deck and sideboard (1 when restricted); legal in the format |

  Basic lands and cards that say "A deck can have any number of cards named" have no copy
  limit; "up to seven cards named" sets it to seven. A Commander deck's `side` board is a list
  of cards being considered: not counted, not checked, not in the buy list.

Card search for the builder is `CardDatabase.search_cards`: one row per card name, filtered by
name, type, rules text, mana value, rarity, color identity (within the commander's / including
chosen colors), legality in the deck's format and "owned"; the exact name sorts first. Text
decklists (`1 Sol Ring`, `4x Lightning Bolt (2X2) 117`, `Commander` / `Deck` / `Sideboard`
sections, a blank line before the sideboard of a 60-card list) are read and written by
`parse_decklist` / `format_decklist`.

**Deck ideas from other sites** (`recommendations.py`). Every answer is cached in
`data/web_cache.db` - its own file since 2026-10-09 (it was a table of the card database, 138 MB
beside 91 MB of cards, and its writes shared the collection's file): compressed (the same 419
answers are 37 MB), entries older than 90 days dropped, the oldest making room above 200 MB
(`MAX_BYTES`), an answer above 8 MB not kept. The file can be deleted at any time and is in no
backup; a cache that cannot be written only makes the next request slower. At the first start
the old table's answers are copied over and the table dropped (the card database file does not
shrink - its pages are used again). Cached: (EDHREC and the MTGJSON list 7 days, precon lists 90 days, deck searches and
decks 1 day; a 403/404 is cached too), requests to one site are at least 1 s apart (MTGJSON
0.25 s) with a 10 s timeout, and a site that fails or answers in another shape raises
`Unavailable`: that panel says so and the rest works. Nothing here runs while scanning.

| Source | Used for |
|---|---|
| EDHREC `json.edhrec.com/pages/commanders/<slug>.json`, `/average-decks/<slug>.json` | Suggestions for a Commander deck (inclusion % = decks with the card / decks that could play it, synergy), "Start from the average deck", and the commander ranking. A pair is filed under one order of the names; the other order answers with a `redirect` that is followed |
| MTGJSON `DeckList.json`, `decks/<file>.json` | Preconstructed decks (the deck types in `PRECON_TYPES`: Commander, Challenger, theme and intro decks, starter kits, duel decks, Jumpstart packs ...) - the only source that is published for programs |
| Archidekt `api/decks/v3/?commanderName=` / `?deckFormat=`, `api/decks/<id>/` | Most viewed public decks of a commander or format; a deck by its address |
| Moxfield `api2.moxfield.com/v2/decks/search?fmt=`, `/v3/decks/all/<id>` | Most viewed public decks of a format (its search can't be narrowed to a commander by name); a deck by its address. Unofficial - Moxfield may refuse it |

"What can I build?" (`run_deck_ideas`) needs one request per item, so it runs in a background
thread and the page polls `GET /api/decks/ideas/<kind>`: `commanders` ranks the legendary
creatures in the inventory by the share (weighted by inclusion) of their EDHREC cards that is
owned; `precons` ranks the preconstructed decks by the share of their cards owned (only the kind chosen on
the page, JSON `type`, or all of them; one request per list the first time, then cached - about
230 Commander and Challenger lists took about a minute, all kinds together were never timed); `card` ("Build around a card": the page lists the owned
cards legal in a format - `/api/cards/search?owned=1&format=`, with `free=1` for the "Not in a deck" tick and `commander=1` for "Commanders only" (legendary creatures and "can be your commander" cards, offered for formats with a commander). The search has one printing per name; a card that is owned is returned in the printing owned (`InventoryManager.owned_printing_by_name`: the one with most copies), so its image is the card on the shelf. A click shows the card - `chooseAround` - and **Find
decks** starts the run, **Stop** in its place while it runs: `findAround`, `renderAround`) reads the
ten most viewed Archidekt decks of that format with the card (`?cardName=&deckFormat=`, which
can take Archidekt half a minute the first time for a much played card: 45 s timeout) and ranks
them by the share of each that is owned. `POST` starts a run and replaces one that is running;
`POST {"stop": true}` ends it at its next step, keeping what was found - the button that
started a search stops it while it runs.

**A preconstructed deck is shown as a card too** (since 2026-10-13), like a deck of your own.
MTGJSON's list of decks has no pictures and does not say what is in a deck - only each deck's
own file does, about a megabyte apiece and some 1,500 of them. So a tile starts as a plain
card with the deck's name, and asks for its cover when it comes into view (`decks.js`:
`watchPreconTiles`, `loadPreconCovers` - an `IntersectionObserver`, one request at a time,
8 decks each): `GET /api/precons/covers?files=...` (`precon_covers`) reads those decks' files
(`Recommendations.precon`: from MTGJSON the first time, 4 a second, then from the cache for
90 days), and answers with the same choice as for your own decks (`deck_covers`: the
commander - two: both, with the turn button - else the most valuable card that is not a
land), in the printing that is in the box. What was worked out is kept in the server's
memory while it runs. Measured on the laptop, nothing cached: the first 18 covers 6 seconds
after the tab was opened. 60 tiles are shown at once; the rest is reached by typing. A click
on a tile opens the window with the deck's cards, where pointing at a line shows its card
(`/api/precons/<file>` gives each card's `image_uri`).

**Preconstructed decks.** The Decks tab lists them from `GET /api/precons` (one cached request)
with a search and a choice of kind (MTGJSON's deck `type`). `PRECON_TYPES` says which types are
listed and the format each becomes as a deck: printed decks only - nothing that exists only
online, no Secret Lair drops, bundles or box sets. MTGJSON does not name a format, so decks
sold beside a set are `standard` (an old one's page then lists its cards as no longer legal
there) and reprint products `legacy`; a Jumpstart list is one 20-card pack - the Foundations
Beginner Box's ten are of that type - and its page reports it as short of 60 cards. Until
2026-10-11 only Commander and Challenger decks were listed (about 230); now 1,472. The
list and one Beginner Box pack were read from MTGJSON that day; the page's kind filter was
**not tested in a browser**. The ranking only adds the share owned. "View cards" shows a list
(`GET /api/precons/<file>`, `precon_cards`: its cards by board, with how many of each are
owned) and saves nothing - the button was "Open as deck" and created the deck at once, which
left decks nobody asked for. "Create deck" in that window creates a deck from the list
(`POST /api/decks`, `precon`), asking first when a deck of that name exists. "I own it" (`precon_own`) also adds the cards to the inventory: MTGJSON names the printing
in the box (`identifiers.scryfallId`, else set + number, else any printing of the name) and
whether it is foil, so the entries get the right set, finish (`Game.suggested_finish`) and
price; they are Near Mint at the location given (the deck's name by default), which is also how
to find them again - filter by that location to move or delete them. Undo does not cover it.

## The desktop program (AppImage)

`app.py --app` is the server as a program on one person's computer (`server/program.py`; the
launchers `packaging/AppRun`, `scripts/build-appimage.sh`, `.github/workflows/appimage.yml`).
It is the same server, and differs from one on a server only here:

- **The camera station is started beside it** (`program.start_station`: `station.py --server
  http://localhost:<port>`, the same file `scripts/start.sh` starts) and ended with it
  (`stop_station`: SIGINT, ten seconds, then killed - the server never hangs there; what the
  station has not sent is on disk). `--no-station`, or `camera.type: remote`, starts none.
- **Who can reach it**: `flask.host: auto` means the whole network on a server and
  `127.0.0.1` as a desktop program; the switch "Reachable from other devices" (Settings ->
  Program, `POST /api/network_access`, `data/settings.json: network_access`) overrides it and
  applies after a restart (`program.network_access`, `listen_host`). The station's setup page
  gets `SCANNER_SETUP_HOST=127.0.0.1` under the same rule.
- **Where it reaches itself**: the station, the browser and `--quit` use `program.local_url`
  - `localhost` for a server that listens on loopback or on every address, the address itself
  for one that listens on an address of its own (`flask.host: 192.168.1.5` answers there, not
  on loopback). Before the saved settings are read (`--quit`, a second start) both are tried
  (`candidate_urls`).
- **Quit** (`POST /api/quit`): only as a desktop program and only from the computer it runs
  on (a server is stopped with its service or container). `--quit` calls it.
- **Shutdown happens once**: however often it is asked (two tabs, a repeated SIGTERM) one stop
  is scheduled and a second SIGTERM during the cleanup is ignored; a quit asked before the stop
  began cancels a restart.
- **Restart** (`POST /api/restart`, in every mode): the program shuts down - the station
  first - and runs the same command in the same process (`os.execv`; the lock and the port
  are released by it); the page reloads when the server answers again.
- **One copy per data folder** (`data/app.lock`): a second start shows the running one;
  `--app` opens its page.
- **Where its files are**: `paths.USER_DIR` (`~/.mtg-scanner`), see *Configuration
  and files*. With no card data the program starts and downloads it in the background - the
  **first download is tried again** after a failure (1, 5, 15, then every 30 minutes, for as long
  as the program runs: `card_data.start_card_data_update(retry=True)`; an update of data that is
  there is asked for again by the person, not repeated), and every page shows a banner while a
  game has no cards (`common.js: watchCardData`, from `database.total_cards`, `updating` and
  `error` of `GET /api/stats`; the page reloads when the data arrives).
- **The AppImage** holds `app.py`, `station.py`, `config.yaml` and `src/` (OCR reader
  included) under `usr/app/`, the Python under `usr/python/`, Node.js as `usr/app/bin/node`
  (`paths.tool`). Never tried: another architecture than x86_64, a system without FUSE for
  anything but `--appimage-extract-and-run`, a real camera under it.

## Web interface

Three pages, all served by the server (`server/pages.py`), with one header (`templates/_topbar.html`)
and one icon in the browser's tab (`templates/_favicon.html`: `static/favicon.svg`, and
`static/favicon.ico` for browsers that want that - a card's outline, white on the brand
colour, like the header's mark; `/favicon.ico` answers with the same file, for a browser
that opens an address which is not a page). The station's own setup page carries the same
drawing inside its one file:

| Page | Template, script | What it is |
|---|---|---|
| `/` | `stations.html` (inline script) | The cameras: each station with its state and a link to its page |
| `/scan/<id>` | `scanner.html`, `static/js/scanner.js` | One station's scanner page. `window.STATION` tells the script which; a station without a camera gets the page without the camera panel |
| `/collection` | `collection.html`, `static/js/collection.js` | Inventory, trades, statistics, backups |
| `/decks`, `/decks/<id>` | `decks.html`, `static/js/decks.js`, `static/css/decks.css` | The deck list (tabs: My decks, What can I build?, Preconstructed decks) and one deck. One page for both addresses: the script shows what the address says (`showWhatTheAddressSays`) and keeps the address in step (`showAddress`: `history.pushState`), so a deck can be bookmarked, a reload stays in it and Back leads to the list |

`static/js/common.js` and `templates/_dialogs.html` hold what the pages share (text helpers,
in-page dialogs and notifications, the edit dialog, the capture viewer, "Add to collection");
`static/js/library.js` what the collection page and the deck pages share and the scanner page
does not (`$`, `api`, `valueDialog` with `templates/_value_dialog.html`, the card picture
beside the pointer - the scanner's script has its own `$`);
`static/css/style.css` has the dark / light theme (CSS variables).

**The scanner page**: top bar with counters; the camera on the left with the manual search
below it (first the scan, then the search; the search was above the camera until 2026-10-15), card panel on
the right, activity log below; its settings are on the settings page. It polls `/api/detection_status`
every 500 ms for the status pill (and `camera_error`, the message over the video), shows
`/video_feed?station=<id>` (MJPEG: each new frame once), and talks to the server over
Socket.IO. Its socket joins its station's room, and its `/api/` requests carry `?station=`, so
it only sees its own camera - see [A page per camera](#a-page-per-camera). The prompt editor
(a modal) stays on this page; the settings page's "Edit prompts" opens it (`/scan/<id>#prompts`).

#### The settings page

`/settings` (`server/pages.py: settings_page`, `templates/settings.html`, `static/js/settings.js`,
`static/css/settings.css`; since 2026-10-15 - before it each scanner page and the collection page
had a drawer of its own with a different part of the settings). Settings is the last link of the header's lower row, in its right corner (it was a gear button in the top row until 2026-10-16; on a narrow window it stays in the first row, the page's own tabs go below), a link to it
on every page. Five categories, a list on the left and one at a time on the right (`#cameras`,
`#sounds`, `#reading`, `#data`, `#server` in the address; `/scan/<id>#settings` and
`/collection#settings` go to it):

| Category | Holds |
|---|---|
| **Cameras** | one camera chosen at the top (its own connection, `?station=`): name, location, status, camera setup / undo last add / forget; Add cards automatically, Camera autofocus, Check the focus every, Refocus when the cards get blurry (Off / half / a third / a fifth as sharp: `refocus_on_drop`, per camera, saved in `data/stations.json` like the other camera settings), Camera rotation, the fixed area's state (it is drawn on the camera's page); under Advanced: Debug trace |
| **Sounds** | Sound effects, Capture beep, Card added, Volume - for every camera |
| **Card reading** | Read with OCR first, the vision AI (provider, key, model), Prompts (the summary, and a link to the editor) |
| **Collection and data** | Backups (schedule, make, upload, list: restore / download / delete), Card data (update), Storage (capture size, keep camera frames), under Advanced: Rebuild database schema |
| **Server** | Restart Card Scanner, Quit (desktop program only), Reachable from other devices, Settings password, under Advanced: Debug mode |

**Debug mode** (Server → Advanced) is two things: the request log (`data/logs/requests.log`) and Werkzeug's
interactive debugger. Since 2026-10-16 the debugger only runs when the server listens on this computer
alone (`program.debugger_allowed`; `services.debugger_running`; the page says which is on) or when the
server is started with `SCANNER_LAN_DEBUGGER=1` - the switch can be reached from the network, and an
error page with a Python console (its PIN goes to the log) would be one step from running code for
anyone on it (an outside code review, 2026-10-16; the real server had it on until 2026-10-15).
Each setting is saved when it is changed, through the routes and Socket.IO events the drawers
used (nothing new on the server side except the page's route). Not carried over: the
"Auto-detection" switch (its state was never read back from the camera; the scanner page
shows "Manual Mode" no more). A scanner page that is open while a setting changes on the
settings page reads them again when its tab is looked at again. `tests/test_settings_page.py`.

Socket.IO events of the pages (namespace `/`; the stations' own connection is described under
[A camera station](#a-camera-station)):

| Page → server | Server → page |
|---|---|
| `capture_card`, `search_card`, `select_printing`, `add_to_inventory` (`finish` + `quantity`, or `items` for several finishes), `undo_last_add` (with `station` from Settings → Stations: that station's), `dismiss_card` (`keep_capture` from the automatic "not found" dismissal) | `card_captured`, `card_found`, `card_printings`, `similar_cards`, `card_not_found`, `inventory_updated` (`added`: what was added; `undone`: a station's card taken back; `cleared`: scanned cards were cleared - only the counters reload), `inventory_prices_updated` (prices fetched after an add), `inventory_undone`, `card_dismissed` |
| `toggle_auto_capture`, `toggle_fast_scan` (add automatically), `toggle_detection`, `toggle_ocr` (read with OCR first), `toggle_debug_trace`, `toggle_debug_mode` (Debug mode, for the next start: the request log, and the interactive debugger only where `program.debugger_allowed` says so), `reset_focus` (refocus + lock), `set_autofocus`, `set_fixed_area` (`enabled` / `area` / `use_detected`), `set_camera_rotation`, `set_refocus_every` (`captures`: focus probe interval) | `auto_capture_triggered` (image taken, focus probe done: drop the next card), `auto_capture_toggled` (also when the station's client restarted or went away), `processing_queue_update` (the station's captures being read), `*_toggled`, `focus_reset`, `fixed_area_updated`, `camera_rotation_updated`, `refocus_every_updated` |
| `set_ai_provider`, `save_ai_credential`, `update_database` (the active game's data), `rebuild_database` | `ai_provider_set`, `ai_credential_saved`, `database_update_progress` / `_complete` / `_error`, `database_update_available` (update check found newer data), `database_rebuild_*`, `log`, `error` |
| `save_prompt` (scope `model` / `all`), `reset_prompt`, `test_prompt` | `prompts_updated`, `prompt_test_result` (sent only to the page that asked) |
| `review_open`, `review_skip`, `review_close` | `review_item` (the station's oldest item, or `id: null` when empty; `station`: its name), `review_queue_update` (count; `queued: true` when a card was just queued - the page plays the queue alert) |
| `set_game` | `game_changed` (to every page; stops every camera's auto scanning; downloads the game's card data if it has none). Captures still waiting to be read keep their game (`game_id` in `route_identified`): they are not looked up as cards of the new game but go, unread, to their own game's review queue |

HTTP endpoints are listed in the README. The collection page adds no Socket.IO events; it only
listens to the inventory, game and card data events above (and sends `update_database`).

**Inventory captures.** Each entry shows its newest capture as a thumbnail. Hovering an entry
(only on devices with a mouse) shows a grid of its captures - up to 8, then "+N more"; clicking
the thumbnail (tapping, on a phone) opens all of them in a viewer.

**Card games.** The page loads `/api/games` (games, their finishes and export formats) and
builds the quantity grid, the edit dialog's finish choices and the export buttons from the
active game; the manual search's Treatment filter (Magic only), the set / number examples and
the card data hint follow it too. The game selector in the top bar only appears when more than
one game exists. Card payloads may carry `finish_options` (only those finishes are offered),
`prices` (`[[finish label, USD]]`) and `thumb_uri` (printing picker).

## The MCP server (decks and collection for an AI assistant)

`mcp_server/` (since 2026-10-15; its README.md has the install and the Claude settings): a
separate program on the machine of the AI client that speaks MCP over standard input / output and
asks this server's web API - `/api/decks`, `/api/decks/<id>`, `/api/decks/<id>/suggestions`,
`/api/decks/ideas/<kind>`, `/api/cards/search`, `/api/inventory`, `/api/stats` - so nothing changes
here. **Version 1 only reads.** Ten tools: `list_decks`, `get_deck`, `check_deck`, `missing_cards`,
`suggest_for_deck` (EDHREC through the server), `what_can_i_build` (starts the server's background
search and waits up to a time), `propose_deck_changes` (works out whether each card exists, is
legal, fits the commander's colours, how many copies are owned and which decks use it, the
singleton rule of commander formats, and the deck's size before and after - **nothing is
changed**), `search_collection`, `find_card`, `collection_summary`. Answers are short on purpose
(small local models). Tested: `tests/test_mcp_tools.py` on a stand-in API; the protocol (tool list,
calls, an error in the tool's own words) with the library's client against the real server, 2026-10-15.
Not tried: a local model as the client. The server has no login: writes (add / remove a card) are for
later, one at a time, each confirmed.

## Configuration and files

**Where a setting comes from, and which wins** (written 2026-10-16 after an outside review found the
four sources hard to tell apart). Later in the list wins over earlier, per setting:

| Source | What it holds | Notes |
|---|---|---|
| `config.yaml` (shipped, top folder) | every default | never edited per machine (it has no machine's own values and no secret) |
| the person's `config.yaml` (`USER_DIR`; the same file in a checkout) | only what they change, laid over the shipped one section by section | `${NAME}` in a value is replaced by that environment variable (anywhere in the text) |
| the environment and `.env` | `SCANNER_*`, `LOCAL_AI_ENDPOINT`, API keys ... | the server reads `.env` when it starts (`server/__init__.py`) and it does not override a variable that is already set |
| `data/api_keys.env` | keys and the local AI address saved from the Settings page | **overrides** the environment and `.env` (`load_dotenv(override=True)`) |
| `data/settings.json` | choices made in the web interface, everyone's (provider, OCR first, sounds, backups, capture size, debug mode ...) | where a setting also exists in `config.yaml` (`refocus_every`, `refocus_on_drop`), this wins once it has been set |
| `data/stations.json` | each camera's own choices on the server (rotation, focus, refocus, debug trace, fixed area) | per station; falls back to `data/settings.json` |
| a station's own: command line > environment > `data/station.json` > default | the server's address, the station's id / name / token, the camera | `station_setup.resolve`; a value from the command line or the environment is shown as fixed on the setup page |

**The local AI endpoint** (since 2026-10-16; `api_keys.check_local_endpoint`, `tests/test_local_endpoint.py`) is where every
card's picture is sent, and the Settings page can change it from any device that reaches the server (unless a settings
password is set). It must be `http(s)` with no user name, and its host is **resolved**: every address it resolves to must
be loopback, private (10/8, 172.16/12, 192.168/16, fc00::/7), link-local or Tailscale's 100.64/10 - the text of a name proves
nothing (`http://134744072` is 8.8.8.8 to the resolver, found by the outside review; a public DNS name can end in `.lan`);
a name that does not resolve is refused. Another address is refused unless the server's environment has
`ALLOW_PUBLIC_AI_ENDPOINT=1`. The check is made again, and is bound to the connection, at **every request**
(`api_keys.pinned_endpoint`, used by `CardIdentifier._local` and the models route): the host is resolved then, every
address must be on the own network, and an `http` request is sent to that address itself with the name in the `Host`
header - so a name whose DNS answer was changed to a public server after it was saved (DNS rebinding, found by the second
Codex review) is refused and cannot change between the check and the connection. An `https` endpoint must be an **IP address**
(`refuse_https_by_name`; a name is resolved again when a request connects, and pinning https to the checked address while
keeping the name for the certificate is not done - the third Codex review found it; a local AI server is http in practice). Redirects are not followed. This
applies to the address in `config.yaml` or the environment too: a deliberately public one needs
`ALLOW_PUBLIC_AI_ENDPOINT=1`. Tests in `tests/test_local_endpoint.py`. `/api/local_ai_models` says only "could not get the models" (the cause is in the log).

**How much a request may carry** (since 2026-10-16): `MAX_CONTENT_LENGTH` is 64 MB (`server/state.py`) - a
capture is at most ~30 MB, a collection or Arena file a few MB - and `backups_api.limit_the_request_body`
refuses a bigger body with 413 before any route runs (Flask only checks when a route reads it, and several
answer "not ready" first). Uploading a backup (`upload_backup`) raises the limit for itself to 8 GB: it
holds the captures. `tests/test_upload_limit.py`.

**Who may use the server** (`server/access.py`, `tests/test_access.py`): there is no login, and
a browser sends any web page's requests to a local or network address, so a page open in
another tab could otherwise change the collection or restore a backup. Every request's Host
must be `localhost`, an IP address, this computer's own name (alone or under `.local`, `.lan`,
`.home`, `.home.arpa`, `.internal`, `.localdomain`) or in `flask.allowed_hosts` - another name
that resolves here is DNS rebinding. A changing request carrying an `Origin` must come from the
page it was sent to, and a cross-site `/api/` request (`Sec-Fetch-Site`) is refused; Socket.IO
connections follow the same rule (`socket_origin_allowed`). Stations send no `Origin`, so they
pass on the Host check alone: a station reaching the server by a host name that is not the
computer's own (a Docker service name, a reverse proxy) needs it in `flask.allowed_hosts`.
Never tested with the Android app in server mode reaching the server by a host name.

**The settings password** (since 2026-10-16; `server/lock.py`, `tests/test_settings_lock.py`,
`scripts/check_settings_lock.py`): the pages have no login, but the few actions that can hurt beyond one
card can be locked behind one optional password, chosen on Settings -> Server (or fixed by
`SCANNER_SETTINGS_PASSWORD`). Locked when it is set: the AI keys and endpoint (`save_ai_credential`), restart,
quit, network access, Debug mode (`toggle_debug_mode`) and, of the backups, restore, upload, download, delete and
making a *full* backup (it holds the keys). Open: scanning, the cameras and their settings, the collection, the
decks, listing backups, making an ordinary backup. A route answers `401 {"locked": true}`; an event answers an
`error` event with `locked: true`. `common.js` wraps every page's `fetch`: on a locked answer it asks for the
password (`passwordDialog`), unlocks and repeats the request once; the Settings page reconnects its Socket.IO
connection after an unlock (the cookie is read when a connection is made) and asks for the event again; a backup's
download link asks first (`ensureSettingsUnlocked`), as a refused link would be saved as a file. It is the camera
station's setup-page mechanism (`station_login.Login`, parametrised): the password only as an scrypt hash in
`data/settings_login.json`, an HttpOnly SameSite=Strict cookie signed with a random key, 12-hour sessions, wrong
passwords wait 1, 2, 4 ... 30 s (so a stranger can make the owner wait). The first password can be set by anyone
who reaches the page; changing or removing it needs the current one. Not covered: the page is plain HTTP, so the
password crosses the network unencrypted - it keeps out mistakes and the curious, not someone listening. A
forgotten password: delete `data/settings_login.json` and restart. `scripts/backup.sh` asks for the full backup, which is guarded: it logs in with `SCANNER_SETTINGS_PASSWORD` (the environment, else the checkout's `.env`; `tests/test_backup_script.py`) and says so when the server is locked and it has no password. A new guarded route takes `@guarded`, a new
guarded event `@guarded_event` (below `@socketio.on`).

**`SCANNER_BIND`** (Docker, `deploy/docker-compose.yml`): the address the port is published on; unset it is every
address (as before), `127.0.0.1` only this computer. It is read by Compose for the `ports:` line only - the server
inside the container still listens on all of its addresses.

**Server**

| Where | What |
|---|---|
| `config.yaml` | Vision AI defaults, web server (`flask.allowed_hosts`: names the server may be reached under, besides localhost, IP addresses and the computer's own name - see *Who may use the server*), station token, cleanup, card data update interval. `camera.*` is read by a station, not by the server |
| `.env` (beside `deploy/docker-compose.yml`) | API keys (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`); `VISION_AI_PROVIDER`, `LOCAL_AI_ENDPOINT`, `SCANNER_STATION_TOKEN`; for Docker also `SCANNER_PORT`, `SCANNER_UID`, `SCANNER_GID` |
| `data/secret_key` | Signs Flask's cookies: a random key made on first start (mode 600) unless `flask.secret_key` is set |
| `data/api_keys.env` | Keys and local endpoint entered in Settings (`api_keys.py`, mode 600); overrides `.env`. The page only ever receives masked keys (`/api/ai_credentials`) - the web interface has no login |
| `data/settings.json` | Choices made in the web interface that are everyone's: AI provider / model, OCR first, the game, automatic backups (`backup_every_hours`, `backup_keep`), sound on / off, the capture beep and the card-added ding each on / off (`sound_capture`, `sound_added`) and volume (`POST /api/sound`), the size of the large copy kept with each capture (`capture_copy_size`: `off` / `small` 600 px / `medium` 900 px / `large` 1200 px - the default - `GET` / `POST /api/capture_copy`: a choice of the server for every camera, on the collection page's Settings under Storage as "Capture size (all cameras)", with the number and size of the copies kept so far; measured on 40 captures at JPEG 85: ~56, ~100 and ~145 KB each; it applies to new captures, existing copies stay), debug mode (`debug_mode`: Flask's debugger, and every request in `data/logs/requests.log`; taken when the server starts, always without the reloader). Also the camera settings of a camera on the server itself - which a new station inherits until it has its own |
| `data/stations.json` | The stations: name, location, capture count, last seen, `camera` (it connects as a camera station), and `settings` - each station's `focus_value`, `camera_rotation`, `refocus_every`, `fixed_area`, `fixed_area_enabled`, `debug_trace`, `auto_add` |
| `data/prompts.json` | Prompt instructions edited in Settings, per game / kind / model (`prompts.py`) |
| `data/cards_database.db` | Card data (`cards`, `card_data_info`), the collection (`inventory`, `inventory_captures`, `inventory_sources`, and the trades: `trades`, `trade_cards`), decks (`decks`, `deck_cards`), the review queue (`review_queue`), and the captures already applied to it (`applied_captures`) |
| `data/web_cache.db` | Answers cached from other sites (`recommendations.py`): disposable, not in backups |
| `data/scan_inventory.db` | Cards scanned and not yet added to the collection (the same three inventory tables, and `pending_moves`) |
| `data/arena_inventory.db` | The digital collection: what the user has in MTG Arena as the last import said (the inventory tables again, of which only `inventory` is used, and `arena_account`). Replaced as a whole by each import |
| `data/pending_captures.db` | Captures waiting in the OCR / AI queues, queued again after a restart (`pending_captures`), and what became of uploaded ones (`settled_captures`, 30 days) |
| `data/review/` | Captures waiting in the review queue (deleted when resolved) |
| `data/captures/` | Thumbnails of the captures behind inventory entries (deleted with their entry) |
| `data/backups/` | Backups (`<date_time>/`, see Backups); copies of the inventory table made before a migration rebuilds it (`inventory_before_*.db`) |
| `data/logs/` | `app.log` (also every station's scanner lines), `ai.log`, `database.log`, `scanned_cards.log` (one CSV line per identified card; the model column says `light-ocr` when OCR read it), `ocr.log` (errors of the OCR reader process), `requests.log` (debug mode) |
| `scanned_cards/` | Captured images as uploaded (deleted after `cleanup.days`) |

**Camera station** - nothing it must keep:

| Where | What |
|---|---|
| `config.yaml` | The camera (`camera.usb_index`, `resolution`, `fps`), detection and auto-capture thresholds |
| Environment, or `.env` beside `deploy/docker-compose.client.yml` | `SCANNER_SERVER` (needed), `SCANNER_CAMERA_INDEX`, `SCANNER_STATION_ID` (default: the machine's name), `SCANNER_STATION_NAME`, `SCANNER_STATION_TOKEN` - or the same as `--server`, `--id`, `--name`, `--token` |
| `station_captures/` | Captures not sent yet, each with its note `<image>.upload.json` (deleted once the server has them); `data/debug_frames/` with debug trace on; on the server `data/frames/` (the camera frames behind captures, debug trace on) and `data/captures_full/` (large copies of the collection's capture thumbnails) |

## Performance

Measured with an Anker PowerConf C200 at 2560 × 1440 on the station, and on the server (4
cores of a Ryzen 7 7800X3D, RTX 4070 Ti SUPER) OCR on the GPU and `qwen3.5:9b-q8_0` on Ollama:

| Step | Where | Time |
|---|---|---|
| Camera | station | 27-29 fps at 2560 × 1440 (MJPEG); 20 fps processed (`camera.fps`) |
| Per processed frame | station | ~10 ms (half-size decode, detection, stability) - was ~24 ms at full size |
| Outline detection | station | ~3 ms per frame |
| CPU while scanning | station | ~30% of one core on a laptop (Ryzen 5 7235HS), ~70% on a Raspberry Pi 5 |
| Card landed → capture | station | ~0.15-0.5 s (settling) |
| Upload, OCR, lookup, add | server | 0.3-0.5 s from capture to "added" for a card OCR confirms (0.10 s of it OCR; 0.67 s on the CPU) |
| AI identification | server | ~0.9 s (qwen3.5:9b, 1024 px image); the foil check runs in parallel (+~0.3 s with Ollama); ~10 s once if the model has to load. About 2 cards a second with 3 workers |
| Set + number lookup | server | 0.1 ms; fuzzy name search ~90 ms |
| Live view | station → server | 12 fps, 66-81 KB a frame (~1 Mbit/s), only while a page shows it |
| Server load | server | ~0% CPU idle, 210-260 MB; the per-frame work is all on the stations |

A whole pile, card after card: 1.6-1.7 s per card on a laptop station and on a Pi 5 station,
every card read by OCR. Four simulated stations at a card every 0.5 s each: all added, OCR
median 0.48 s.

Vision models compared on 90 scans (identification + foil check, before the prompt and image
size change - identification alone is now 0.9 s / 1.5 s, see *Prompts*):

| Model | Hardware | Time per card | Notes |
|---|---|---|---|
| `qwen3.5:9b` (Q4_K_M, 6.6 GB) | Ollama server on the network | 1.6 s | reference; says "unknown" when the ★/• is unreadable |
| `qwen3.5:4b` (Q4_K_M, 3.4 GB) | laptop RTX 3050 6 GB (fits entirely) | 3.4 s | image processing ~920 vs ~2,560 tokens/s on the server; called 3 regular cards foil |

The 9B doesn't fit in a 6 GB GPU (it would be split with the CPU); the 4B is a usable fallback.

## Tests

`tests/test_ownership.py` (`venv/bin/python -m unittest discover tests`; also inside the server
container) covers which station scanned what - through splits, merges, a move to the
collection, a clear - and backups: ownership restored, restored over another state, a backup
from before stations, restoring the oldest automatic backup. Everything runs on temporary
databases and folders. These are the cases of a code review of 2026-10-09; 13 of the 14 fail
against the code from before its fixes.

`tests/test_trades.py` covers the cards set aside for a trade: they stay in the collection,
two trades never hold the same copy, nothing else can delete, lower, move or clear what a
trade holds (and a refused change leaves nothing half done), cancelling changes nothing,
confirming removes the copies with their photos and leaves a record, a confirm that fails
changes nothing, and backups (a restore brings a trade back with its entries, over an open
trade, from before trades). The routes and the page were tried in headless Chromium against
the server's code on a temporary copy of test data (2026-10-10), not against the server.

`tests/test_arena_collection.py` covers the digital collection and the decks' two kinds, on
temporary files and made-up exports: reading the exporter's two files and refusing others,
finding an entry's card (set and number only when the name agrees), an inventory replaced
whole or not at all, the deck table's new columns on an old file, Arena's decks replaced
while decks made here stay, and the digital collection in backups.

There are no automated tests for the rest. [TEST_CASES.md](TEST_CASES.md) lists what to check
by hand. For scanner logic, recorded or synthetic frames can be fed through `CardScanner` with
a fake camera (patch `detect_camera_type` / `_initialize_usb_camera`). **Never test against the
server's real `data/`**: use copies of the databases in a temporary folder, or a second
container with its own data folder and port.

## Known limitations

- **White-bordered cards on a white background** have no visible outline; use a darker
  background or capture them manually with detection off.
- **An identical copy landing within ~0.7 mm of the previous card** without the fall hiding the
  card for 6 frames isn't recognized as new - press Capture.
- **A card hidden for a moment** (a hand over the box for ~0.3 s) counts as a change of card:
  when it is still the same card it is captured again.
- **Cards without the ★/• marker** (older printings) get their finish from printing data only.
- **Undo** takes back only a station's most recent add; older adds are edited in the list.
- **At least once**: a capture is never lost by a restart, but in a crash at the wrong moment
  it can be added twice (see [Sending a capture](#sending-a-capture)).
- **No login**: the web interface and, without a token, the upload are open to the network.
  For a home or shop network, not the internet.
- **A station without a connection** shows no live view and no result until it is back; the
  captures wait, and are kept across restarts of the station and of the app (a computer
  station: pictures with a note each in its `station_captures/`; see the station's setup page).
- **One camera per station**, opened by one process.
- **Raspberry Pi camera modules** go through `picamera2`, which is not in the client's Docker
  image: the station runs from the Pi's own Python there (see [Pi camera module](#pi-camera-module)).
  Focus is the camera's own continuous autofocus - no sweep, lock or drift check. Tried once
  (2026-10-11) with one card; auto scanning, a growing pile and the capture thresholds, all
  measured with a USB webcam, have not been tested with it.
- **Entries shared between cameras** can't be edited from a list filtered to one camera.
