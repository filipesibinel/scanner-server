# Card Scanner - Technical Documentation

How the scanner works inside. For what it is and how to use it see [README.md](README.md); for
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
| `app.py` - the server | One machine; a Docker image (`Dockerfile` target `server`) | Web pages, reading (OCR + AI), matching, card data, scanned cards, collection, decks, backups; holds every station's settings |
| `station_client.py` - a camera station | The machine a USB webcam is plugged into; a Docker image (target `client`, x86-64 and ARM64) | Camera, outline detection, stillness, new-card rules, focus; uploads each capture; shows nothing itself |
| The Android app ([mtg-scanner-android](https://github.com/filipesibinel/mtg-scanner-android)) in client mode | A phone over the box | The same job with the phone's camera (a Kotlin port of the detection); uploads each capture. It can also work on its own, without a server |

`app.py` can still open a camera itself (`camera.type` other than `remote`): the server then
has one local camera, as the single-machine scanner this grew from. Everything below about
detection, auto-capture and focus is the same code in both cases - `CardScanner` either runs
inside `app.py` or inside `station_client.py`.

### Modules

| Module | Runs in | Responsibility |
|---|---|---|
| `app.py` | server | Flask + Socket.IO: pages, routes, events; one desk per station (`Desk`, `desk()`); station captures (`station_capture`, `submit_capture`); what happens to a read card (`route_identified`) |
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
| `scanner.py` | station (or server, with a local camera) | `CardScanner`: camera (USB via OpenCV / V4L2, or Pi camera), capture thread, detection state, stillness, auto-capture, fixed area, focus |
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

`POST /api/stations/<id>/captures` (multipart; `app.py: station_capture`):

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
{"capture": 12, "status": "added", "seconds": 0.31,
 "card": {"name": ..., "set": ..., "number": ..., "finish": ..., "quantity": 1},
 "read": {"name": ..., "number": ..., "set": ..., "foil": ..., "reader": "light-ocr"}}
{"capture": 13, "status": "review", "reason": "printing not confirmed" | "not found" | "not read" | "game switched", ...}
```

or `202` with `"status": "pending"` when the card isn't settled within `wait` (it still is,
afterwards). Errors: `400` (bad id, no readable picture), `401` (token), `413` (over 30 MB).

| Also | |
|---|---|
| `GET /api/stations/<id>/captures/<n>` | The outcome of a capture that was still pending (`202` while it still is). Outcomes of the last 500 captures are kept in memory: `404` after a restart |
| `POST /api/stations/<id>/undo` | Take back the station's last automatic add |
| `GET /api/stations` | The stations, with whether each is connected, sees a card, is scanning, and how many cards it has in review |
| `PUT /api/stations/<id>` | Rename it (`name`) or set where its cards are put (`location`) |
| `DELETE /api/stations/<id>` | Forget it - after clearing its scanned cards and review items (`clear_camera`, every game), so nothing of it is left where no page shows it. Cards already in the collection stay |

- **Token**: with `stations.token` in `config.yaml` (or `SCANNER_STATION_TOKEN`) set, uploads,
  the outcome, undo and the `/station` connection need it (header `X-Station-Token`, or `token`
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

**A capture counts once** (since 2026-10-09, after DATABASE_REVIEW.md):
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
server `app.py` holds a `RemoteScanner` (`remote_scanner.py`) in its place, one per station
(`CameraHub.scanner(id)`): the same attributes and methods, so the page's handlers are the same
whether the camera is local or at a station.

The client opens the connection (Socket.IO namespace `/station`, `auth`: id, name, token) and
keeps it; nothing has to be configured on the server to add a camera.

| Client → server | |
|---|---|
| `hello` | Answered with the camera's saved settings (`CAMERA_SETTINGS`: focus position, rotation, focus check interval, fixed area, debug trace) and the attributes the server decides (`DECIDED`: add automatically, stability frames, capture delay). The client creates its `CardScanner` with them - which is when the camera opens; after a reconnect only the decided attributes are applied again |
| `status` (every 0.2 s) | The scanner's state and `get_detection_status()`; the answer says whether the preview is being watched |
| `preview` | The annotated live view as JPEG (`get_stream_jpeg`), at most ~12 a second and only while a page shows `/video_feed` (measured: 12 fps, 66-81 KB a frame over Wi-Fi) |
| `setting` | A camera setting the scanner changed (a focus sweep's result, ...): saved on the server |
| `log`, `captured` | The scanner's log lines, shown on the station's page; the capture beep (`auto_capture_triggered` to its pages) |

| Server → client | |
|---|---|
| `command` | A `CardScanner` method (`METHODS`: rotation, fixed area, focus, detection, and `capture` for the page's Capture button), waited for (6 s); the answer carries the result or the error text (raised as `ValueError` on the server) and the state after it |
| `set` | Attributes: `card_under_review`, `auto_capture_enabled`, and the decided ones when they change |

What `app.py` reads on its scanner comes from three places (`remote_scanner.py`): `DECIDED`
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
- **The server or the network goes away**: the station keeps scanning and the captures wait;
  the client reconnects by itself (~5 s after a server restart). Its own state decides
  `auto_capture_enabled`: a connection that drops and comes back goes on scanning; a client
  that restarts has it off, and its pages are told (`auto_capture_toggled`).
- **The same id connecting again** replaces its earlier connection.
- **Which camera**: `camera.usb_index` in the station's `config.yaml`, or
  `SCANNER_CAMERA_INDEX` - a number, or a `/dev/v4l/by-id/...` path, which stays the same
  camera when the numbers change (`config.usb_camera_index`).

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
the stations (`templates/stations.html`, refreshed from `GET /api/stations`) - or, when the
server has a camera of its own, is that camera's scanner page. A station's count there is
`scanned`, its copies in the scanned cards now (what its page lists); `captures` in
`data/stations.json` counts every picture it ever sent and only numbers the captures.

On the server each station has a **desk** (`app.py: Desk`, `desk_for`): its scanner, the card
on its page (`current_card_info`), its open review, the capture being reviewed, its captures
being read. The handlers were written for one camera and still read that way: `desk()` gives
the current desk and `scanner` is a proxy for `desk().scanner`. Which desk is current:

| Where | The desk |
|---|---|
| A scanner page's Socket.IO event | The station the page named when it connected (`io({query: {station}})`, `page_stations`); the page's socket is in the room `station:<id>` |
| An HTTP request | `?station=<id>` (the scanner page adds it to every `/api/` request - a wrapper around `fetch` at the top of `scanner.js`), or the id in `/api/stations/<id>/...` |
| A worker thread | The one set with `in_desk(...)`: `submit_capture` remembers its desk for the result, the OCR / AI workers run each job in its desk (`Job.scope`), a `RemoteScanner`'s callbacks in its station's (`at_station`) |
| Anything else | `default_desk`: the server's own camera - or, without one, a desk with no camera |

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

This chapter, [Auto-capture](#auto-capture) and [Focus](#focus) describe `CardScanner` (`scanner.py`, `object_detector.py`): it runs where the camera is - inside `station_client.py`
on a camera station, or inside the server when that has a camera of its own. The Android app
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

`warp_card(frame, corners)` maps the corners to an upright rectangle with the card's aspect
ratio - the image sent to the AI is flat and tightly cropped, and the collector line is always in
the same place.

For display, the last detection is held for 6 s when the card is briefly lost ("HOLD"), but a
held detection never counts as a still card.

## Auto-capture

### When a card is ready

A frame counts toward `stable_frames` only if `_is_card_settled()` holds:

- the corners moved less than **1%** of the card size since the previous frame **and** since
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
their limits (1%, 1%, 20%, `min_sharpness`). With **Settings → Debug trace** on (remembered per station: `debug_trace`), the scanner also
keeps the last 3 s of frames (640 px wide - what the outline detector works on) and saves them,
plus the next second, to `data/debug_frames/<time>_<reason>/` (on the station's machine) when a card waits over 2 s or a
new card is detected, or the card is "gone", less than 2 s after a capture (a likely duplicate:
a foil Gwen Stacy lost its outline right after its capture and was captured again - by its
look the pair differed 0.79, so appearance can't tell such a case from a new card); the newest 20 dumps
are kept. Replaying such frames through `CardScanner` with a fake camera reproduces the case.

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
`app.py`) and has two stages, each behind its own queue:

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

The reader is `ocr/server.mjs`, started by `CardOcr` when the app starts (the models take
1-2 s to load) and restarted after a failure (at most once a minute); its errors go to
`data/logs/ocr.log`. It needs Node.js 22+ and `npm install` in `ocr/` (the server's Docker image
has both; `deploy.sh` does it for an installation without Docker); without them the switch is
disabled and cards go to the AI. Its native library needs glibc 2.38 or newer, and on an NVIDIA
GPU inside a container the Vulkan driver's X11 / GLVND libraries (see INSTALL.md). `ocr.provider` in `config.yaml`
chooses GPU or CPU. Only Magic has a parser (`card_ocr.PARSERS`); other games are read by the AI.

### The vision AI request

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

Prompts live in `prompts.py` and are editable in Settings → Vision AI → **Edit prompts**. Each
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
`scanned_cards/` are deleted after `cleanup.days`), `captured_at`, and `station` (which station's
capture it is). The capture follows the card from the queues to the add: `search_and_emit_card` puts it on the matched card
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
the card; surge foils go out as `foil`; every game: "Card Scanner (everything)",
`games.base.write_collection_csv` - all the columns of an entry, with `Card ID`, `Set Code` and
`Timestamp`, so `import_csv` puts each entry back as it was), `Game.import_rows`
reads another app's file (`import_formats` names them). `POST /api/import_inventory` picks by
the file's columns: Moxfield (`Count`, `Name`, `Edition`) → `Magic.import_rows` matches each row
to its printing by set code + collector number as written (`get_card_by_set_number(exact=True)`:
"M19-128" of The List), then the number's variants if the name agrees, then the name alone
(counted as `by_name`: maybe another printing); `foil`/`etched` become foil, or surge for a
surge foil printing; Moxfield's conditions map to the ones here (Lightly Played → Excellent,
Moderately → Good, Heavily → Played, Damaged → Poor); tags are kept, the location is empty
(Moxfield has none - entries that differ only by location merge) → `inventory.import_entries`.
The app's own columns (`Card Name`, `Set`) → `import_csv`; CSVs written before the `Card ID` /
`Set Code` columns existed import without a link to their printing. Any other
file, or one where no card is found, is refused before "replace" deletes anything.

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
`deck_constraints_1`): `inventory.quantity` is an integer above 0, `added_quantity` NULL or not
negative; `inventory_captures` and `inventory_sources` belong to an entry (`REFERENCES
inventory(id) ON DELETE CASCADE`), a station's `quantity` is above 0; `deck_cards` belong to a
deck (cascade), `quantity` above 0, `board` one of commander / main / side. So an entry or a
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
- **The lens has play**: the same `focus_absolute` reached from above measured up to 5× blurrier
  than from below (395: 22 vs 118, peak 2,480). Sweeps measure while moving up, and every final
  or restored position is approached from 30 below (`_move_focus`).
- **Focus probe while scanning** (`_run_focus_probe`, every `auto_capture.refocus_every`
  captures, default 10; **Settings → Check the focus every** changes it while scanning): the best position drifts - in one session it moved from 395 to 430 in
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

## Collection page and decks

**Scanned cards and the collection are two inventories.** Scanning - every station - adds to
`scan_inventory` - a second `InventoryManager` on its own file, `data/scan_inventory.db`, with
the same tables - and its top bar counter, inventory window, edit / delete, Undo, export, import
and "Clear all" work on that one only (`?area=scan` on the `/api/inventory*` endpoints,
`inventory_area()` in `app.py`; `/api/stats` reports it as `inventory` and the collection as
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

Every entry records `added_at` - when it came into its inventory; one time for all entries of
an "Add to collection" (`timestamp` stays the scan time) - and `added_quantity`, how many of
its copies came with that (the rest were there before). The collection page sorts by it ("Last
added to the collection", the default), filters by batch ("Added <time> (n cards)") and, with a
batch chosen, offers **Remove this batch** (`POST /api/inventory/remove_batch`,
`InventoryManager.remove_batch`): each entry loses only the copies that batch brought, with
their newest captures; entries with no other copies are deleted. An entry that keeps copies
gets `added_quantity = 0`: they belong to no batch any more (shown as "Added before <time>",
left out of the batch filter), so removing a batch again never takes them. Adds to one entry
at the same `added_at` (two in one second, a file with repeated rows) add up in
`added_quantity`; Undo lowers it. The cards are deleted, not
moved back to the scanner. Both columns are added on startup (`ALTER TABLE`; `added_at` starts
as the scan time).

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
trade: take them out of the trade first" (`TRADE_HELD`; `app.py`: `write_refused`). Copies the
trade does not hold can be changed as always, and so can condition, tags and price. Bulk
delete and bulk move, which commit entry by entry, are refused before anything changes when
one of the entries is held. Trades are in every backup (`backups.INVENTORY_TABLES`): a restore
brings back the trades of that moment with their entries - a trade confirmed since is open
again, its cards back - and a backup from before trades restores with none.

Never tested: two browsers changing one trade at the same moment (each change is one
transaction under the collection's lock, so the second sees the first's result or is refused).

### Backups

The gear button opens the page's settings drawer (`/collection#settings` opens it directly; the
scanner pages' drawers link there, and this one links to a scanner page's settings for camera,
AI and sound - `settings_url`).
**Back up now** (`POST /api/backups`, `backups.create`) writes `data/backups/<date_time>/`:
`backup.db` with plain copies of `inventory`, `inventory_captures`, `inventory_sources`, `trades` and `trade_cards` of the collection
(`collection_*`) and of the scanned cards (`scanned_*`), `decks`, `deck_cards` and an `info`
row (time, note, counts) - every game - and `captures/`, hard links to the thumbnails those
entries point at (no extra space; they survive the app deleting its own). It is written to a
`.tmp` folder and moved into place, under the three managers' locks.

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
last 2 are kept. `scripts/backup.sh` then archives those snapshots with the rest of `data/`
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
earlier parts restored; the automatic backup has the state from before. Every page reloads its
inventory (`inventory_updated`). Card data, the review queue, the stations and settings are not
part of these backups: copy the server's `data/` folder for everything (`scripts/backup.sh`
does that for an installation without Docker).

`/collection` (`templates/collection.html`, `static/js/collection.js`) works on the active
game's inventory over the REST endpoints; it listens to `inventory_updated`, `inventory_undone`
and `inventory_prices_updated` to follow what is scanned meanwhile, and reloads on `game_changed`.
A change to a trade is announced as `collection_updated` (`app.py`: `collection_changed`, no
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
batch and **No use in my decks**; a set, tag or location that is gone from the collection is
not filtered by, and **Clear filters** can only be pressed while something is filtered. What
narrows the list is also shown as chips above it (`activeFilters`: "Rarity: rare ×"), each
taken off with a click, and is written into the page's address (`FILTER_PARAMS`:
`/collection?q=qty%3A%3E1&rarity=rare&colors=G`; `history.replaceState`, so no history entry
per filter) - an address with filters, a bookmark, wins over the remembered ones. A click on a
row's rarity, finish, location, tag or trade badge filters by it, a second click takes that off
(`filterByBadge`). Hovering a row - anywhere in it - shows the card's picture large, beside the pointer and
following it (`data-image` on the row, `showPreview`, `placePreview`; devices with a mouse;
the deck builder's narrow rows show it beside the row). The box appears at once, card-shaped,
and the picture when it has loaded; the previous card's picture is dropped first. The
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
(`decks_using` in `app.py`): when the location named after the deck holds as many copies as the
deck plays, only the entries there count for it and the same card elsewhere is free (the basic
lands in a box, next to the ones in ten precons); with fewer copies there - or no such
location - every entry of the card counts. The deck builder has the same idea as **Not in other decks** on the card search
(`free=1&deck_id=` → `search_cards(exclude_names=)`) and on the suggestions (`elsewhere`). The deck
builder goes by card name, not by copies: a card with three copies owned and one in a deck is "in a deck".
**No use in my decks** goes further, to find what can be sold or given away: it also hides the
cards EDHREC lists for the commander of one of the decks (`GET /api/inventory/suggested` →
`Recommendations.commander_cards` per deck with a commander, matched to the owned names by front
face; asked when the tick is set, one request per deck the first time, then from the 7-day
cache). Decks EDHREC has no answer for come back in `unknown` and are named in a notification.

**Decks** (Magic; a game without `Game.deck_formats` has no Decks tab). A deck (`decks.py`) is a
name, a format and `deck_cards` rows: card name, count, board (`commander`, `main`, `side`) and
a printing id for the image. The card is identified by its name, so any printing owned counts.
Nothing a deck does changes the inventory. `deck_payload` in `app.py` puts together what the
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
| MTGJSON `DeckList.json`, `decks/<file>.json` | Preconstructed decks (Commander, Challenger, Pioneer Challenger) - the only source that is published for programs |
| Archidekt `api/decks/v3/?commanderName=` / `?deckFormat=`, `api/decks/<id>/` | Most viewed public decks of a commander or format; a deck by its address |
| Moxfield `api2.moxfield.com/v2/decks/search?fmt=`, `/v3/decks/all/<id>` | Most viewed public decks of a format (its search can't be narrowed to a commander by name); a deck by its address. Unofficial - Moxfield may refuse it |

"What can I build?" (`run_deck_ideas`) needs one request per item, so it runs in a background
thread and the page polls `GET /api/decks/ideas/<kind>`: `commanders` ranks the legendary
creatures in the inventory by the share (weighted by inclusion) of their EDHREC cards that is
owned; `precons` ranks the preconstructed decks by the share of their cards owned (about 230
lists the first time, then cached); `card` ("Build around a card": the page lists the owned
cards legal in a format - `/api/cards/search?owned=1&format=`, with `free=1` for the "Not in a deck" tick and `commander=1` for "Commanders only" (legendary creatures and "can be your commander" cards, offered for formats with a commander). The search has one printing per name; a card that is owned is returned in the printing owned (`InventoryManager.owned_printing_by_name`: the one with most copies), so its image is the card on the shelf. A click shows the card - `chooseAround` - and **Find
decks** starts the run, **Stop** in its place while it runs: `findAround`, `renderAround`) reads the
ten most viewed Archidekt decks of that format with the card (`?cardName=&deckFormat=`, which
can take Archidekt half a minute the first time for a much played card: 45 s timeout) and ranks
them by the share of each that is owned. `POST` starts a run and replaces one that is running;
`POST {"stop": true}` ends it at its next step, keeping what was found - the button that
started a search stops it while it runs.

**Preconstructed decks.** The Decks tab lists them from `GET /api/precons` (one cached request)
with a search; the ranking only adds the share owned. "View cards" shows a list
(`GET /api/precons/<file>`, `precon_cards`: its cards by board, with how many of each are
owned) and saves nothing - the button was "Open as deck" and created the deck at once, which
left decks nobody asked for. "Create deck" in that window creates a deck from the list
(`POST /api/decks`, `precon`), asking first when a deck of that name exists. "I own it" (`precon_own`) also adds the cards to the inventory: MTGJSON names the printing
in the box (`identifiers.scryfallId`, else set + number, else any printing of the name) and
whether it is foil, so the entries get the right set, finish (`Game.suggested_finish`) and
price; they are Near Mint at the location given (the deck's name by default), which is also how
to find them again - filter by that location to move or delete them. Undo does not cover it.

## Web interface

Three pages, all served by `app.py`, with one header (`templates/_topbar.html`):

| Page | Template, script | What it is |
|---|---|---|
| `/` | `stations.html` (inline script) | The cameras: each station with its state and a link to its page. With a camera on the server itself, `/` is that camera's scanner page instead |
| `/scan/<id>` | `scanner.html`, `static/js/scanner.js` | One station's scanner page. `window.STATION` tells the script which; a station without a camera gets the page without the camera panel |
| `/collection` | `collection.html`, `static/js/collection.js` | Inventory, decks, trades, statistics, backups |

`static/js/common.js` and `templates/_dialogs.html` hold what the pages share (text helpers,
in-page dialogs and notifications, the edit dialog, the capture viewer, "Add to collection");
`static/css/style.css` has the dark / light theme (CSS variables).

**The scanner page**: top bar with counters; search bar and camera on the left, card panel on
the right, activity log below; settings in a slide-out drawer. It polls `/api/detection_status`
every 500 ms for the status pill (and `camera_error`, the message over the video), shows
`/video_feed?station=<id>` (MJPEG: each new frame once), and talks to the server over
Socket.IO. Its socket joins its station's room, and its `/api/` requests carry `?station=`, so
it only sees its own camera - see [A page per camera](#a-page-per-camera). The settings drawer
has the station's own settings (scanning, camera), the list of stations (rename, location, undo
its last card, forget), and the shared ones (vision AI, prompts, card database, sound).

Socket.IO events of the pages (namespace `/`; the stations' own connection is described under
[A camera station](#a-camera-station)):

| Page → server | Server → page |
|---|---|
| `capture_card`, `search_card`, `select_printing`, `add_to_inventory` (`finish` + `quantity`, or `items` for several finishes), `undo_last_add` (with `station` from Settings → Stations: that station's), `dismiss_card` (`keep_capture` from the automatic "not found" dismissal) | `card_captured`, `card_found`, `card_printings`, `similar_cards`, `card_not_found`, `inventory_updated` (`added`: what was added; `undone`: a station's card taken back; `cleared`: scanned cards were cleared - only the counters reload), `inventory_prices_updated` (prices fetched after an add), `inventory_undone`, `card_dismissed` |
| `toggle_auto_capture`, `toggle_fast_scan` (add automatically), `toggle_detection`, `toggle_ocr` (read with OCR first), `toggle_debug_trace`, `toggle_debug_mode` (Flask's debug mode, for the next start), `reset_focus` (refocus + lock), `set_autofocus`, `set_fixed_area` (`enabled` / `area` / `use_detected`), `set_camera_rotation`, `set_refocus_every` (`captures`: focus probe interval) | `auto_capture_triggered` (image taken, focus probe done: drop the next card), `auto_capture_toggled` (also when the station's client restarted or went away), `processing_queue_update` (the station's captures being read), `*_toggled`, `focus_reset`, `fixed_area_updated`, `camera_rotation_updated`, `refocus_every_updated` |
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

## Configuration and files

**Server**

| Where | What |
|---|---|
| `config.yaml` | Vision AI defaults, web server, station token, cleanup, card data update interval; `camera.type: remote` (or `SCANNER_CAMERA=remote`, the Docker image's default) for a server without a camera |
| `.env` (beside `docker-compose.yml`) | API keys (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`); `VISION_AI_PROVIDER`, `LOCAL_AI_ENDPOINT`, `SCANNER_STATION_TOKEN`; for Docker also `SCANNER_PORT`, `SCANNER_UID`, `SCANNER_GID` |
| `data/api_keys.env` | Keys and local endpoint entered in Settings (`api_keys.py`, mode 600); overrides `.env`. The page only ever receives masked keys (`/api/ai_credentials`) - the web interface has no login |
| `data/settings.json` | Choices made in the web interface that are everyone's: AI provider / model, OCR first, the game, automatic backups (`backup_every_hours`, `backup_keep`), sound on / off, the capture beep and the card-added ding each on / off (`sound_capture`, `sound_added`) and volume (`POST /api/sound`), debug mode (`debug_mode`: Flask's debugger, and every request in `data/logs/requests.log`; taken when the server starts, always without the reloader). Also the camera settings of a camera on the server itself - which a new station inherits until it has its own |
| `data/stations.json` | The stations: name, location, capture count, last seen, `camera` (it connects as a camera station), and `settings` - each station's `focus_value`, `camera_rotation`, `refocus_every`, `fixed_area`, `fixed_area_enabled`, `debug_trace`, `auto_add` |
| `data/prompts.json` | Prompt instructions edited in Settings, per game / kind / model (`prompts.py`) |
| `data/cards_database.db` | Card data (`cards`, `card_data_info`), the collection (`inventory`, `inventory_captures`, `inventory_sources`, and the trades: `trades`, `trade_cards`), decks (`decks`, `deck_cards`), the review queue (`review_queue`), and the captures already applied to it (`applied_captures`) |
| `data/web_cache.db` | Answers cached from other sites (`recommendations.py`): disposable, not in backups |
| `data/scan_inventory.db` | Cards scanned and not yet added to the collection (the same three inventory tables, and `pending_moves`) |
| `data/pending_captures.db` | Captures waiting in the OCR / AI queues, queued again after a restart (`pending_captures`), and what became of uploaded ones (`settled_captures`, 30 days) |
| `data/review/` | Captures waiting in the review queue (deleted when resolved) |
| `data/captures/` | Thumbnails of the captures behind inventory entries (deleted with their entry) |
| `data/backups/` | Backups (`<date_time>/`, see Backups); copies of the inventory table made before a migration rebuilds it (`inventory_before_*.db`) |
| `data/logs/` | `app.log` (also every station's scanner lines), `ai.log`, `scanner.log` (a camera on the server itself), `database.log`, `scanned_cards.log` (one CSV line per identified card; the model column says `light-ocr` when OCR read it), `ocr.log` (errors of the OCR reader process), `requests.log` (debug mode) |
| `scanned_cards/` | Captured images as uploaded (deleted after `cleanup.days`) |

**Camera station** - nothing it must keep:

| Where | What |
|---|---|
| `config.yaml` | The camera (`camera.usb_index`, `resolution`, `fps`), detection and auto-capture thresholds |
| Environment, or `.env` beside `docker-compose.client.yml` | `SCANNER_SERVER` (needed), `SCANNER_CAMERA_INDEX`, `SCANNER_STATION_ID` (default: the machine's name), `SCANNER_STATION_NAME`, `SCANNER_STATION_TOKEN` - or the same as `--server`, `--id`, `--name`, `--token` |
| `scanned_cards/` | Captures not sent yet (deleted once the server has them); `data/debug_frames/` with debug trace on |

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
  captures wait (computer station: until the client is stopped - its unsent captures are files
  in its `scanned_cards/`, sent only by that run; phone: kept across restarts of the app).
- **One camera per station**, opened by one process.
- **Raspberry Pi camera modules** go through `picamera2`, which is not in the client's Docker
  image and has no focus control here; that path has not been run. USB webcams are what is
  tested.
- **Entries shared between cameras** can't be edited from a list filtered to one camera.
