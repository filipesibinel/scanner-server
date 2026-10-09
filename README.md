# MTG Card Scanner - server and camera stations

A camera-based scanner for Magic: The Gathering cards. Drop cards onto a pile under a camera:
each one is found in the picture, captured once, identified down to the exact printing and
finish, and added to a collection you can filter, build decks from and export to Moxfield.

> **Built with AI.** All of this project - the code, the tests and this documentation - was
> written by an AI coding assistant (Claude, through Claude Code), directed by the project's
> author. See [How this project was built](#how-this-project-was-built).

It is made of two kinds of programs on your network:

- **Camera stations** - any number of them. A small computer with a USB webcam (a PC, a laptop,
  a Raspberry Pi), or an Android phone. A station finds the card and decides when to capture
  it, and sends one picture per card.
- **One server** - reads the pictures, finds the printing, and keeps the collection, the decks
  and all settings. You use it from a browser; every camera has its own page there.

```
  Pi + webcam ──┐
  laptop + webcam ──┼── one picture per card ──>  server  <── browser: a page per camera,
  phone ────────┘                                (OCR, AI,        the collection, decks
                                                  collection)
```

The server needs no camera, and a station keeps nothing: it can be unplugged, replaced or
reinstalled without losing a card. This repository grew out of the single-machine
[scanner](https://github.com/filipesibinel/scanner), which is a separate project; a single
machine with its own camera still works here too (see [INSTALL.md](INSTALL.md)).

## How it works

1. **Find the card** (on the station) - each camera frame is searched for the card's outline:
   the largest four-sided shape with a card's 88×63 mm proportions. This takes a few
   milliseconds and isn't fooled by foil glare inside the card.
2. **Capture** (on the station) - once the card lies still and sharp, it is cut out and
   perspective-corrected into a flat, upright image. You hear a beep: drop the next card.
3. **Read** (on the server) - [light-ocr](https://github.com/arcships/light-ocr) reads the
   name, collector number and set code in about a tenth of a second. Only when that isn't a
   certain match - full-art and borderless cards, a soft collector line - is a vision AI asked.
4. **Foil check** - modern cards print a star (`HOB★EN`) instead of a dot (`HOB•EN`) next to
   the set code on foil copies. OCR reads it; when it can't, the AI is shown a zoomed crop of
   that corner.
5. **Match the printing** - set code + collector number (unique for every printing), checked
   against the name, are looked up in a local copy of [Scryfall](https://scryfall.com)'s card
   data, which also provides prices, images and which finishes each printing exists in.
6. **Add** - a confirmed printing goes straight into the *scanned cards* (with an Undo);
   anything uncertain waits in that camera's review queue, and scanning goes on. When you have
   checked a session, **Add to collection** moves the scanned cards into your collection.

## Features

- **Several cameras at once**, each with its own page: live view, review queue, Undo, scanned
  count and inventory location. One shared scanned list, which can be narrowed to one camera -
  to add just that camera's cards to the collection, or clear them and scan its pile again.
- **Automatic or manual capture** - start auto scanning and just drop cards into the box (about
  1.7 s per card), or capture on demand.
- **OCR first, AI second** - most cards are read on the server in a fraction of a second
  without an AI request; the vision AI handles the rest. Providers: Google Gemini, OpenAI,
  Anthropic Claude, or a self-hosted model via Ollama (or another OpenAI-compatible server).
- **Exact printing identification** from the set code and collector number, with name
  matching that tolerates misreads, accents ("Fili" → "Fíli"), flavor names ("Bucklebury
  Ferry") and shortened legendary names ("Thanos" → "Thanos, the Mad Titan").
- **Foil detection** from the ★/• marker, combined with printing data: printings that only
  exist in foil (or only non-foil) are known for certain.
- **Nothing is lost on the way**: a station keeps scanning while the server or the Wi-Fi is
  away and sends its captures when it is back; captures waiting on the server survive a
  restart; a capture sent twice is one card.
- **Focus that stays sharp** on a USB webcam: the sharpest position is found once, locked, and
  followed as the pile grows.
- **Collection** with locations and tags, bulk edits, statistics, backups, import / export
  (Moxfield, and the app's own CSV), and a **deck builder** for Commander and the 60-card
  formats with ownership, legality checks, suggestions and preconstructed decks.
- **Android app** ([mtg-scanner-android](https://github.com/filipesibinel/mtg-scanner-android))
  as a station: the phone finds and captures, the server does the rest. It also works on its
  own, without a server.

## Requirements

| Part | Needs |
|---|---|
| Server | A Linux machine with Docker (x86-64). ~1 GB for the image, ~300 MB for the card data. An NVIDIA GPU is optional (OCR in 0.1 s instead of 0.7 s per card) |
| Camera station | A Linux machine with a USB webcam - x86-64 or ARM64 (a Raspberry Pi 5 is tested) - with Docker, or Python 3.10+. A webcam with manual focus control (`focus_absolute`) is best |
| Phone | Android 8 or newer, with the app |
| Vision AI | An API key for Gemini, OpenAI or Anthropic, **or** an Ollama server with a vision model (the server can run beside it) |
| Network | The stations must reach the server (Wi-Fi is enough: about 1 Mbit/s per camera while its page is open). Internet for the card data, card images and cloud AI providers |

## Installation

[DOCKER_GUIDE.md](DOCKER_GUIDE.md) walks through a server and a camera station step by step, with
what each command should print; [INSTALL.md](INSTALL.md) is the reference for every part. In short:

```bash
# the server
git clone https://github.com/filipesibinel/scanner-server.git && cd scanner-server
mkdir -p data scanned_cards
docker compose up -d --build              # the first start downloads the card data
# open http://<server>:5000 and choose the AI in Settings

# a camera station, on the machine with the webcam (same repository)
cp .env.station.example .env              # edit it: the server's address, the camera, a name
docker compose -f docker-compose.client.yml up -d --build
# it appears on the server's page by itself
```

## Choosing a vision AI provider

On a camera's page, **Settings → Vision AI**: pick a provider and a key field appears (keys you
saved before are shown masked, e.g. `AIza…3f9Q`, never in full). They are stored on the server
in `data/api_keys.env` (readable only by you) and used immediately. The choice is one for all
cameras.

| Provider | Setting | Key / endpoint |
|---|---|---|
| Google Gemini | `gemini` | `GEMINI_API_KEY` ([get a key](https://aistudio.google.com/app/apikey)) - default model Flash-Lite: same reads as Flash on a test of 10 cards at ~1/4 of the cost (~$0.79 per 1000 cards, Sept 2026) |
| OpenAI | `openai` | `OPENAI_API_KEY` |
| Anthropic Claude | `anthropic` | `ANTHROPIC_API_KEY` |
| Local (Ollama, vLLM, LM Studio) | `local` | `LOCAL_AI_ENDPOINT`, or `vision_ai.local.endpoint` in `config.yaml` |

Keys can also be put in `.env` beside the compose file; keys saved in the web interface take
precedence. The default provider is `vision_ai.provider` in `config.yaml` (or
`VISION_AI_PROVIDER`).

**Local models**: pick a vision-capable model in Settings - the list is loaded from your Ollama
server. Thinking is switched off for Ollama requests, so "thinking" models such as `qwen3.5`
answer in about a second instead of reasoning for tens of seconds. The model is preloaded when
you start auto scanning and kept loaded for 30 minutes.

`qwen3.5:9b` is the recommended model (about 1 s per card on a desktop GPU). It needs more
than 6 GB of GPU memory; on smaller GPUs `qwen3.5:4b` works, but it is slower on weak GPUs and
more often mistakes regular cards for foil - see PROGRAM_DOCUMENTATION.md for the comparison.

**Prompts**: **Settings → Vision AI → Edit prompts** shows what the AI is asked (card
identification and the foil marker check) and lets you change it - for all models or just the
one in use. **Test on last capture** runs the edited text on the last card read before you
save it. **Restore default** goes back to the built-in prompt. Only the instructions are
editable; the answer format is added automatically.

The foil check sends one small request for the cards whose marker OCR could not read. It is
free with a local model; for cloud providers you can turn it off with
`vision_ai.detect_foil: false`.

## Using the scanner

Open `http://<server>:5000`. The start page lists your cameras - connected or not, whether a
card is in view, whether it is scanning, how many cards wait for review - each with a link to
its own page, `http://<server>:5000/scan/<id>`. **Collection** in the top bar is your
collection and decks.

### Scanning cards

On a camera's page:

1. Put a card in the box. It gets an outline in the video and the status pill shows
   **Focusing** / **Stabilizing**, then **Ready** once the card is still and sharp.
2. Click **Start auto scanning** and drop cards onto the pile, or **Capture card** for one.
3. **Drop the next card when you hear the capture beep.** It sounds once the image is taken
   (every few cards the scanner also checks the focus first, ~1 s, while the status shows
   *Capturing - wait for the beep*); then the status shows **Captured - drop the next card**,
   and the next capture happens when a new card has landed and settled. The drop is recognized
   by the motion and by where the new card lands, so two identical copies in a row are both
   captured.
4. The card panel shows what was added (name, set, finish) with an **Undo** button; the
   *Processing* counter shows how many captures are still being read.

Only cards whose exact printing is confirmed (set code + number, or name + number) are added
automatically. Anything less certain goes to that camera's **Review** queue and scanning goes
on. Click the *Review* counter when you're done: each item shows the capture next to the
suggested card and what was read, with a search to correct it; **Add** adds the card shown (and
opens the next), **Skip** / **Delete** drop the capture, **Close** keeps the rest for later. A
card whose name can't be read (runes, another language) is found by its set code + number
alone - leave the name empty.

**Confirm each card instead:** turn off **Settings → Add cards automatically** on that
camera's page. Each capture is then shown with its printing, price and finish (pre-selected
with the reason, e.g. *"Foil: ★ next to the set code"*) and waits for **Add to inventory** or
**Skip**; the camera holds its next capture until you choose.

**Sleeved cards:** turn on **Fixed area** in the camera panel. The first time, drag a rectangle
on the video around where the cards land (or click **Use detected card**); **Area** redraws it.
Cards are then judged by the image inside that area instead of their outline, which a pile of
sleeves confuses, and the photo that is read is exactly that area - draw it around the whole
card.

**With the phone:** in the app's Settings, turn on *Send cards to a scanner server* and enter
the server's address. The phone shows the camera and each result itself; its page on the
server has its scanned cards and its review queue. Without a connection the phone keeps
capturing and sends the cards when it is back.

### Searching manually

Type a name in **Search**, optionally with the **Set** code and **Number** from the card's
bottom-left corner (e.g. `HOB` and `14`) - together they go straight to the exact printing -
and a **Treatment** (e.g. Borderless). If more than one printing matches, choose yours from
the thumbnail grid. After a capture, the fields are filled with what was read, so you can
correct a misread and search again.

### Scanned cards

What you scan is kept apart from your collection until you say so. The **Scanned** count in the
top bar opens the list; on a camera's page it counts and shows that camera's cards.

- The **camera** selector narrows the list to one camera or shows all of them.
- **Add to collection** moves what the list shows - one camera's cards, or everyone's - into
  the collection; you can choose one of your locations or type a new one (*Box #1*, a binder)
  first.
- **Clear** deletes what the list shows, never the collection: with a camera chosen, only that
  camera's scanned cards and its review queue, to scan its pile again.
- Each entry shows a thumbnail of what was captured: hover to see all its captured copies, or
  click the thumbnail to open them larger. You can filter, sort, and edit quantity / condition /
  finish (changing the finish of part of a stack splits it; the price follows the finish).
- **Settings → Scan into location** puts everything that camera scans next into a location.
- **Settings → Stations** renames a camera, sets its location, takes back its last card, or
  forgets it (with its scanned cards and review items).

An entry two cameras both scanned (the same printing into the same location) is one entry;
narrowed to one camera it shows that camera's copies and can't be edited from there - choose
*All cameras* for that.

### Collection page

**Collection** in the top bar (`http://<server>:5000/collection`) is the place to manage your
cards and to build decks. It works on a phone too.

- **Inventory** - filter by name, color identity, type, rarity, set, finish, location, tag and
  price, or tick **Not in a deck** to see only the cards no deck uses yet, or **No use in my
  decks** to also leave out the cards EDHREC lists for your decks' commanders - what remains
  can be sold or given away; switch between the list and a grid of card images; tick entries to
  move them to a **location**, tag them (*trade*, *keep*), change their condition, add them to
  a deck or delete them in one step. Copies of the same printing can be in two locations -
  moving part of a stack splits it.
- **Export / import** - the **Export…** menu writes the collection for Moxfield, or as
  **Card Scanner (everything)**: the app's own CSV with every entry's location, tags, finish
  and printing, which **Import** reads back exactly as it was (the Android app writes the same
  file). **Import** also takes a Moxfield collection CSV - each row is matched to its printing
  by set code and collector number (by name when that fails, which is reported).
- **Decks** (Magic) - Commander, Standard, Pioneer, Modern, Legacy, Vintage and Pauper. Search
  the whole card database and click to add. Every card shows whether you own it, are missing
  it, or need it in another deck too; the deck shows what is not legal or unfinished, its mana
  curve, its price and what completing it costs, and **Buy list** downloads the missing cards.
  A deck is only a list: it never changes the inventory. Import a pasted decklist or a
  Moxfield / Archidekt deck address; export as text.
- **Deck ideas** - for a Commander deck, *Suggestions* lists the cards played with its
  commander (EDHREC) and *Popular decks* lists public decks you can copy. *What can I build?*
  searches from what you own: **Build around a card**, the legendary creatures you own by how
  much of their usual deck you already have, and the preconstructed decks by how much of each
  you own. These read other sites, so they need internet; if a site doesn't answer, only that
  list is missing.
- **Preconstructed decks** - **Open as deck** makes a deck list to change as you like; **I own
  it** also adds its cards to your inventory (the printings and foils that come in the box).
- **Added by mistake?** The *Added* filter lists each "Add to collection" with its time. Choose
  one and click **Remove this batch** to take exactly those copies back out.
- **Statistics** - cards and value by color, type, rarity, finish, set, location and tag, and
  your most valuable cards.
- **Backups** - one is made automatically the first time the server starts each day (the last
  7 are kept). The gear button opens Settings: **Back up now** keeps your collection, the
  scanned cards and your decks as they are - do it before a big load. **Restore** puts all
  three back as they were; what you had at that moment is backed up first, so a restore can be
  taken back.

### Tips for reliable scans

- **Contrast**: a light, plain background (e.g. a white box) makes the card's dark border
  easy to find. White-bordered cards on a white background have no visible outline - use a
  darker background, or capture them with auto-detection turned off.
- **Keep the whole card in view** with a small margin. If an edge is cut off, the card isn't
  detected - and the foil marker in the bottom-left corner can't be read.
- **Focus**: click **Refocus** once with a card in the box. The scanner sweeps through the
  camera's focus range (~10 s), locks the sharpest position and remembers it for that camera.
  While you scan, every few cards it checks whether a slightly different focus is sharper and
  follows it; if a card stays blurry for 3 s it refocuses completely. A soft focus is the usual
  reason for cards going to the AI or to review instead of being read by OCR.
- **Camera sideways** gives more detail and a taller pile: set **Settings → Camera rotation**
  so the card shows upright.
- **Light** evenly from above, so reflective foils don't glare.
- **Keep your hand out of the picture** between drops: a card hidden for a moment counts as a
  new card when it is seen again.
- Cards printed before the ★/• convention (roughly before 2020) have no foil marker; for
  those, set the finish yourself when both versions exist.

## Configuration

Most settings are in the web interface and remembered on the server - per camera (focus,
rotation, fixed area, focus check interval, add automatically, location) or for everyone (AI
provider and model, OCR first, sound). `config.yaml` has the rest:

| Setting | Where | Default | Description |
|---|---|---|---|
| `camera.type` | server | `auto` | `remote`: no camera on the server (the Docker image sets it); `auto` / `usb` / `picamera`: the server opens a camera itself |
| `camera.usb_index` | station | | `/dev/videoN` number of the USB camera (or `SCANNER_CAMERA_INDEX`: a number or a `/dev/v4l/by-id/...` path) |
| `camera.resolution` / `fps` | station | `[2560, 1440]` / `20` | Capture resolution and frames processed per second |
| `camera.rotate` | station | `0` | Default rotation (0/90/180/270) until it is set in Settings |
| `detection.allow_landscape` | station | `false` | Accept cards lying sideways (a card's art box can look like a sideways card) |
| `auto_capture.delay` | station | `1.0` | Minimum seconds between automatic captures |
| `auto_capture.stability_frames` | station | `5` | Still, in-focus frames before a capture that waits for Add / Skip |
| `fast_scan.stability_frames` | station | `6` | Same, when adding automatically (~0.3 s, lets a dropped sleeved card stop sliding) |
| `auto_capture.min_sharpness` | station | `250` | Minimum sharpness for auto-capture; lower it if cards stay on *Focusing* |
| `auto_capture.refocus_every` | station | `10` | Default for *Check the focus every* (captures); `0` = off |
| `vision_ai.provider` | server | `gemini` | Default AI provider |
| `vision_ai.workers` | server | `3` | AI requests at the same time |
| `vision_ai.detect_foil` | server | `true` | Ask the AI for the ★/• marker when OCR couldn't read it |
| `vision_ai.image_size` | server | `1024` | Longest side of the card image sent to the AI (larger = slower, not more accurate) |
| `ocr.provider` | server | `auto` | Where light-ocr runs: `auto` (GPU when available), `cpu`, `webgpu` |
| `stations.token` | server | (none) | When set (or `SCANNER_STATION_TOKEN`), stations must send it |
| `flask.port` | server | `5000` | Web server port (with Docker: `SCANNER_PORT`) |
| `cleanup.enabled` / `days` | server | `true` / `7` | Delete uploaded capture images older than N days on startup |
| `database.update_after_days` | server | `7` | How old the card data may get before the update notice shows |

The station's settings are read from the `config.yaml` on the station's machine (or inside its
image).

## Maintenance

| Task | How |
|---|---|
| Update card data and prices | Click the Database counter when it shows a dot, or **Settings → Update card database**. Scanning keeps working during an update |
| Update the programs | `git pull`, then `docker compose up -d --build` again - on the server and on each station |
| Back up your cards | Collection page → Settings → **Back up now** (also made daily at startup) |
| Back up everything | Copy the server's `data/` folder while the container is stopped |
| See what a station is doing | Its page's Activity log; `docker compose -f docker-compose.client.yml logs -f` on the station |
| Server logs | `data/logs/`: `app.log` (web app, every station's scanner lines), `ai.log` (OCR and AI reads), `database.log`, `scanned_cards.log` (one CSV line per identified card), `ocr.log` |

Your collection lives in the same SQLite file as the card data (`data/cards_database.db`,
table `inventory`); updating the card data does not touch it.

## Troubleshooting

**A camera's page says "No camera station connected"** - the station's program isn't running
or can't reach the server. On the station: `docker compose -f docker-compose.client.yml logs`;
it retries by itself once the server is reachable.

**"No camera found" on a connected station** - the station doesn't see its webcam. Check
`v4l2-ctl --list-devices` there and `SCANNER_CAMERA_INDEX` / `camera.usb_index`. Only one
program can use a camera at a time. The station picks the camera up when it is plugged in.

**Auto scanning never captures** - the status tells you why: *Focusing* (image not sharp
enough - click **Refocus**, or lower `auto_capture.min_sharpness`), *Stabilizing* (card still
moving), or *Captured - drop the next card* (it's waiting for a new card to land).

**Cards go to review, or to the AI, more than they should** - usually focus: the small print
is too soft for OCR. Click **Refocus** with a card in the box.

**A card was added twice / one was skipped** - see *Tips*: a hand over the box between drops,
or an identical copy landing exactly on the previous one. Undo, or fix the quantity in the
scanned list.

**Card not detected** - make sure the whole card is visible with some margin and the
background contrasts with the border. A card without a clear outline can be captured with
auto-detection turned off, or with a fixed area.

**"Vision AI disabled"** - no API key was found for the selected provider. Enter one in
Settings, or switch to a local model.

**Local AI returns 404** - Ollama answers 404 when the requested model isn't installed. Pick
one of the models listed in Settings (they come from your server) or `ollama pull` it.

**Wrong printing** - check the set code and number that were read (shown in the Search bar
after a capture); correct them there and search again, or pick the printing from the grid.

## Project structure

```
app.py               Server: web pages, routes, events, one desk per camera, station captures
identification.py    Reading a card: OCR queue, AI queue, shared by every camera
card_ocr.py, ocr/    light-ocr reader (Node.js) and the parser for what it reads
card_identifier.py   Vision AI providers, card identification, foil marker check
prompts.py           AI prompts: built-in ones and those edited in Settings
stations.py          The stations and each one's settings (data/stations.json)
remote_scanner.py    The server's stand-in for a station's camera; the stations' connection
pending.py           Captures in the queues, kept across a restart
review.py            The review queue
database.py          Scryfall card data: download, schema, search, printings
games/               Card games: base.py (interface), mtg.py (Magic), mtg_decks.py
inventory.py         Inventory: add, merge, undo, edit, locations, tags, cameras, import / export
decks.py             Decks (lists of cards; the inventory says what is owned)
recommendations.py   Deck ideas from EDHREC, MTGJSON, Archidekt and Moxfield (cached)
backups.py           Backups of the collection, scanned cards and decks
station_client.py    Camera station: runs next to the camera, talks to the server
scanner.py           Camera capture thread, detection, stability, auto-capture, focus
object_detector.py   Card outline detection + perspective correction
config.yaml          Settings (loaded by config.py / config_loader.py)
templates/, static/  Web interface: the cameras, a camera's page, the collection
tests/               Automated tests (camera ownership, backups)
Dockerfile           Images: server, client
docker-compose*.yml  Server, server with GPU, camera station
scripts/             docker-entrypoint.sh; deploy.sh, start.sh, backup.sh for a setup without Docker
data/                Card data, collection, settings, backups, logs (created at runtime)
scanned_cards/       Capture images (created at runtime)
```

[PROGRAM_DOCUMENTATION.md](PROGRAM_DOCUMENTATION.md) explains how everything works inside
(stations, detection, auto-capture, identification, matching, database);
[INSTALL.md](INSTALL.md) covers installation and [DOCKER_GUIDE.md](DOCKER_GUIDE.md) is the
step-by-step version ([VIDEO_SCRIPT.md](VIDEO_SCRIPT.md): a script for filming it);
[TEST_CASES.md](TEST_CASES.md) lists what to check by hand.

## HTTP API

The web interface uses these endpoints, which you can also call directly. A scanner page adds
`?station=<id>` to say which camera it means. The inventory endpoints work on the collection;
add `?area=scan` for the cards scanned and not yet added to it, and `&camera=<id>` to narrow
those to one camera.

| Endpoint | Description |
|---|---|
| `GET /`, `GET /scan/<id>`, `GET /collection` | The cameras; a camera's scanner page; the collection |
| `GET /video_feed?station=<id>` | MJPEG stream of a camera's annotated view |
| `GET /api/detection_status?station=<id>` | Whether a card is detected and how stable it is, or why there is no camera |
| `GET /api/stats` | Card data, scanned cards, collection and review counts |
| `GET /api/stations` | The stations and their state |
| `PUT` / `DELETE /api/stations/<id>` | Rename a station or set its location; forget it (with its scanned cards and review items) |
| `POST /api/stations/<id>/captures` | A card captured by a station (multipart `image`; optional `foil_image`, `foil_is_image`, `name`, `wait`, `capture_id`): read, then added or queued for review - see PROGRAM_DOCUMENTATION.md, Stations |
| `GET /api/stations/<id>/captures/<n>`, `POST /api/stations/<id>/undo` | The outcome of a capture that was still pending; take back the station's last card |
| `GET /api/scan_settings`, `POST /api/scan_location`, `POST /api/sound` | A camera's scanning settings; its location for scanned cards; the sound switch and volume |
| `GET /api/games` | Supported card games (finishes, export formats) and the active one |
| `GET /api/inventory` | The active game's inventory (each entry has an `id` and its `captures`) |
| `GET /captures/<file>`, `GET /review_images/<file>` | A capture thumbnail of an inventory entry; the capture of a review item |
| `POST /api/inventory/update/<id>` | Update an entry (JSON: `quantity`, `condition`, `finish`, `location`, `tags`, `split_quantity`; `card_id`: another printing of the same card) |
| `GET /api/inventory/<id>/printings` | The printings an entry can be changed to |
| `POST /api/inventory/bulk` | One change to several entries (JSON: `ids`, `action`: `delete` / `condition` / `location` / `add_tag` / `remove_tag`, `value`) |
| `POST /api/inventory/delete/<id>` | Delete an inventory entry |
| `POST /api/clear_inventory` | Delete the active game's entries; with `?area=scan&camera=<id>` one camera's scanned cards and review items |
| `GET` / `POST /api/scan_inventory/to_collection` | Cards waiting and locations in use; move the scanned cards into the collection (JSON: `location`, `camera` - both optional) |
| `POST /api/inventory/remove_batch` | Take back the cards added at one time (JSON: `added_at`) |
| `GET /api/export_inventory/<format>` | Download the inventory: `moxfield` (Magic), `csv` (the app's own, every column) |
| `POST /api/import_inventory` | Import a collection CSV (multipart `file`, `replace_existing`): Moxfield, or the app's own columns |
| `GET /api/backups`, `POST /api/backups` | List the backups; make one (JSON: `note`) |
| `POST /api/backups/<id>/restore`, `DELETE /api/backups/<id>` | Put the collection, scanned cards and decks back as in a backup (the current state is backed up first); delete a backup |
| `GET /api/cards/search`, `GET /api/cards/printings?name=` | Deck builder card search; every printing of a card with the copies owned |
| `GET /api/decks`, `POST /api/decks` | List decks; create one (JSON: `name`, `format`, and optionally `text`, `url`, `precon`, `commander`) |
| `GET` / `PUT` / `DELETE /api/decks/<id>` | A deck with card data, copies owned and issues; rename / change format; delete |
| `POST /api/decks/<id>/cards`, `/import`, `/duplicate` | Change cards; add cards from a list or a deck address; copy a deck |
| `GET /api/decks/<id>/export/<text\|buylist>` | Download the decklist, or the cards not owned |
| `GET /api/decks/<id>/suggestions`, `GET /api/decks/popular` | EDHREC cards for the deck's commander; public decks on Archidekt / Moxfield |
| `GET` / `POST /api/decks/ideas/<commanders\|precons\|card>` | "What can I build?" searches: state / start a run / stop it |
| `GET /api/precons`, `POST /api/precons/<file>/own` | Preconstructed decks; add one's cards to the inventory and open it as a deck |
| `GET /api/inventory/suggested` | The owned cards EDHREC lists for the decks' commanders |
| `GET /api/ai_provider`, `/api/ai_models`, `/api/local_ai_models`, `/api/ai_credentials`, `/api/prompts` | The AI in use, model lists, masked keys, prompts |

Scanning, searching and settings on a camera's page go through Socket.IO events
(`capture_card`, `search_card`, `add_to_inventory`, `toggle_auto_capture`, ...), and a camera
station holds a Socket.IO connection of its own (`/station`); both are listed in
PROGRAM_DOCUMENTATION.md.

The web interface has no login: run it on a network you trust.

## How this project was built

This project was developed entirely with AI. The code, the tests and the documentation - this
README, the installation guides, the technical documentation - were written by an AI coding
assistant: Claude, by Anthropic, working through Claude Code. The commit history records it
(nearly every commit carries a `Co-Authored-By: Claude` line). The same goes for the
[Android app](https://github.com/filipesibinel/mtg-scanner-android).

The project's author did what the AI could not: decided what to build and how it should
behave, ran it on real hardware - webcams, a Raspberry Pi, a phone - with real cards, and
reported what happened. The thresholds and timings quoted in the documentation come from
those runs and from recorded sessions, and each says when and on what it was measured.

What that means if you use it:

- **It has been run, not audited.** The author uses it for their own collection, but no one
  has reviewed the code independently, and the automated tests cover one area (which camera
  scanned what, and backups). Where something was never tried, the documentation says so
  ("not tested").
- **It is made for a home network.** There is no login; see the note under
  [HTTP API](#http-api).
- **Keep backups** of a collection you care about - the server makes one a day, and
  *Back up now* is on the collection page.

## License

[MIT](LICENSE) - free to use, modify and share, including commercially, as long as the
copyright notice is kept.

## Acknowledgments

- Card data, prices and images: [Scryfall](https://scryfall.com)
- Text recognition: [light-ocr](https://github.com/arcships/light-ocr)
- Computer vision: [OpenCV](https://opencv.org)
- Web: [Flask](https://flask.palletsprojects.com) and
  [Flask-SocketIO](https://flask-socketio.readthedocs.io)

This is an unofficial fan project, not affiliated with or endorsed by Wizards of the Coast.
Magic: The Gathering is a trademark of Wizards of the Coast LLC.
