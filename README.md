# MTG Card Scanner

A camera-based scanner for Magic: The Gathering cards. Put a card under the camera, and the
scanner finds it in the video feed, identifies the exact printing with a vision AI, tells you
whether it's foil, and adds it to a local collection you can export to Moxfield.

It runs as a small web app (Flask + Socket.IO) on a Raspberry Pi or any Linux machine with a
USB webcam or Raspberry Pi camera, and is used from a browser on the same network.

## How it works

1. **Find the card** - each camera frame is searched for the card's outline: the largest
   four-sided shape with a card's 88×63 mm proportions. This takes a few milliseconds and
   isn't fooled by foil glare inside the card.
2. **Capture** - the card is cut out and perspective-corrected into a flat, upright image.
3. **Identify** - a vision AI reads the card name, collector number and set code from that image.
4. **Foil check** - modern cards print a star (`HOB★EN`) instead of a dot (`HOB•EN`) next to
   the set code on foil copies. The AI is shown a zoomed crop of that corner and asked which
   one it is.
5. **Match the printing** - set code + collector number (unique for every printing), checked
   against the name, are looked up in a local copy of
   [Scryfall](https://scryfall.com)'s card data, which also provides prices, images and
   which finishes each printing exists in.
6. **Add to inventory** - auto scanning adds it automatically (or you confirm it) and the card is
   stored with quantity, condition and finish.

## Features

- **Live camera view** with the detected card outlined; the status turns from
  *Stabilizing* to *Ready* when the card is still.
- **Manual or automatic capture** - capture on demand, or start auto scanning and just drop
  cards into the box: each one is identified in the background and added to the inventory
  automatically, with a one-click Undo.
- **OCR first** - with Node.js installed, [light-ocr](https://github.com/arcships/light-ocr)
  reads each Magic card on your computer in a fraction of a second; the vision AI is asked only
  for the cards OCR can't match with certainty (about one in eight in our tests).
- **Vision AI providers** - Google Gemini, OpenAI, Anthropic Claude, or a self-hosted model
  via Ollama (or another OpenAI-compatible server). Switch provider and model from the UI.
- **Exact printing identification** from the set code and collector number, with name
  matching that tolerates misreads, accents ("Fili" → "Fíli"), flavor names ("Bucklebury
  Ferry") and shortened legendary names ("Thanos" → "Thanos, the Mad Titan").
- **Foil detection** from the ★/• marker, combined with printing data: printings that only
  exist in foil (or only non-foil) are known for certain. The card panel pre-selects the
  finish and shows why.
- **Manual search with treatment filter** - borderless, showcase, extended art, full art,
  retro frame, etched, surge foil. When several printings match, pick the one you have from
  a grid of thumbnails.
- **Inventory** with automatic duplicate merging, condition, regular / foil / surge foil,
  prices, filtering, sorting, editing, and export to / import from
  [Moxfield](https://moxfield.com) on the collection page.
- Sound effects, and a dark/light interface that follows your system theme.
- **Housekeeping** - scanned images older than 7 days are cleaned up automatically. The app
  checks for newer card data when it starts (and daily) and marks the Database counter with
  a dot; click it to update.

## Requirements

- **Computer**: Raspberry Pi 4 or 5, or any Linux machine (developed on x86-64 Linux)
- **Camera**: USB webcam (autofocus strongly recommended) or Raspberry Pi Camera Module
- **Python**: 3.10 or newer (tested with 3.12 and 3.14)
- **Disk**: ~300 MB for dependencies, ~75 MB for the card database
- **AI**: an API key for Gemini, OpenAI or Anthropic, **or** a local Ollama server with a
  vision model
- **Node.js** (optional): 22 or newer, with npm, to read cards with OCR before the AI
- **Internet**: to download the card database, show card images, and reach cloud AI providers

## Installation

The deployment script installs everything and can set the scanner up as a service. Run it on
the machine that will run the scanner (a Raspberry Pi or any Linux PC):

```bash
git clone https://github.com/filipesibinel/scanner.git
cd scanner
./scripts/deploy.sh             # install: packages, venv, .env, camera check, card database
./scripts/deploy.sh --service   # optional: run as a service that starts on boot
```

Then add your AI key to `.env` (see below) and open `http://<device-ip>:5000`. Useful options:

| Option | What it does |
|---|---|
| `--camera N` | Use USB camera `/dev/videoN` (the script lists the cameras it finds) |
| `--picamera` | Raspberry Pi camera module |
| `--service` / `--remove-service` | Install / remove the systemd service |
| `--update` | Pull the latest code, update packages, restart the service |
| `--refresh-cards` | Re-download the card database (new cards and prices) |

The script is safe to run again at any time - it only does what is missing, and uses `sudo`
only to install missing system packages or the service. [INSTALL.md](INSTALL.md) has the
details: Raspberry Pi setup, remote deployment over SSH, updating and uninstalling.

### Manual installation

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python setup_database.py     # download the card database (a few minutes)
cp .env.example .env                  # then add your API key(s)
```

Set `camera.usb_index` in `config.yaml` to your camera's `/dev/videoN` number
(`v4l2-ctl --list-devices`, package `v4l-utils`). For the Raspberry Pi camera module, install
`python3-picamera2` with apt and create the venv with `--system-site-packages`.

## Choosing a vision AI provider

Enter API keys in **Settings → Vision AI**: pick a provider and a key field appears (keys you
saved before are shown masked, e.g. `AIza…3f9Q`, never in full). They are stored in
`data/api_keys.env` (readable only by you) and used immediately. Keys can also be put in `.env`;
keys saved in the web interface take precedence.

| Provider | Setting | Key / endpoint |
|---|---|---|
| Google Gemini | `gemini` | `GEMINI_API_KEY` ([get a key](https://aistudio.google.com/app/apikey)) - default model Flash-Lite: same reads as Flash on a test of 10 cards at ~1/4 of the cost (~$0.79 per 1000 cards, Sept 2026) |
| OpenAI | `openai` | `OPENAI_API_KEY` |
| Anthropic Claude | `anthropic` | `ANTHROPIC_API_KEY` |
| Local (Ollama, vLLM, LM Studio) | `local` | `vision_ai.local.endpoint` in `config.yaml`, or `LOCAL_AI_ENDPOINT` |

The default provider is set by `vision_ai.provider` in `config.yaml` (or `VISION_AI_PROVIDER`),
but you can switch provider and model at any time in **Settings → Vision AI**; the choice is
remembered in `data/settings.json`.

**Local models**: pick a vision-capable model in **Settings → Vision AI** - the list is loaded
from your server (or check with `curl http://<server>:11434/api/tags`). Thinking is switched off for Ollama
requests, so "thinking" models such as `qwen3.5` answer in about a second instead of
reasoning for tens of seconds. The model is preloaded when you start auto scanning and kept
loaded for 30 minutes, so the first card doesn't wait ~10 s for it to load.

`qwen3.5:9b` is the recommended model (about 1 s per card on a desktop GPU). It needs more
than 6 GB of GPU memory; on smaller GPUs `qwen3.5:4b` works, but it is slower on weak GPUs and
more often mistakes regular cards for foil - see PROGRAM_DOCUMENTATION.md for the comparison.

**Prompts**: **Settings → Vision AI → Edit prompts** shows what the AI is asked (card
identification and the foil marker check) and lets you change it - for all models or just the
one in use, since smaller local models sometimes need different wording. **Test on last
capture** runs the edited text on the last captured card before you save it, and shows the
answer and the printing it would match. **Restore default** goes back to the built-in prompt.
Only the instructions are editable; the answer format is added automatically. Edited prompts
are saved in `data/prompts.json`.

The foil check sends one extra small request per card. It is free with a local model; for
cloud providers you can turn it off with `vision_ai.detect_foil: false`.

## Running

If you installed the service, it's already running:

```bash
sudo systemctl status mtg-scanner      # or: restart, stop
journalctl -u mtg-scanner -f           # live logs
```

Otherwise start it by hand:

```bash
venv/bin/python app.py
```

Then open `http://localhost:5000` (or `http://<device-ip>:5000` from another device).
`scripts/start.sh` does the same but also loads `.env` and checks your API key and database
first.

## Using the scanner

### Scanning cards

1. Put a card in front of the camera. It gets an outline in the video and the status pill
   shows **Focusing** / **Stabilizing**, then **Ready** once the card is still and sharp.
2. Click **Capture card**, or **Start auto scanning** to capture every time a new card is
   ready.
3. The card panel shows the identified printing, its price and treatment. The finish
   (Regular / Foil / Surge foil) is pre-selected with the reason, e.g.
   *"Foil: ★ next to the set code"* or *"only printed in foil"*.
4. Adjust quantity or condition if needed and click **Add to inventory**, or **Skip**.

Auto scanning is made for dropping cards onto a pile in the box. Each card is captured once.
**Drop the next card when you hear the capture beep**: it sounds once the image is taken (every
few cards the scanner also checks the focus first, ~1 s, while the status shows *Capturing -
wait for the beep*); then the status shows **Captured - drop the next card**, and the next capture happens
when a new card has been dropped on top and has settled. The drop is recognized by the motion
(the card briefly vanishing or jumping, a hand) and by where the new card lands, so two
identical copies in a row are both captured.

**Sleeved cards:** turn on **Fixed area** in the camera panel. The first time, drag a rectangle
on the video around where the cards land (or click **Use detected card**); **Area** redraws it.
Cards are then judged by the image inside that area instead of their outline, which a pile of
sleeves confuses, and the photo sent to the AI is exactly that area - draw it around the whole
card. Turn it off again for cards without sleeves.

By default (**Settings → Add cards automatically**) cards are identified in the background and
added to the inventory without review, so you can keep dropping cards; the *Processing* counter
in the top bar shows how many are still being identified. After each add the card panel shows
what was added (name, set, finish) with an **Undo** button. Only cards whose exact printing is
confirmed (set code + number, or name + number) are added automatically. Anything less certain
(printing not confirmed, name not found, nothing readable) goes to the **Review** queue and
scanning goes on: the *Review* counter in the top bar shows how many are waiting. Click it when
you're done to go through them one by one - each shows the capture next to the suggested card
and what the AI read, with a search to correct it; **Add** adds the card shown (and opens the
next), **Skip** / **Delete** drop the capture, **Close** keeps the rest for later (the queue is
kept across restarts). A card whose name can't be read (runes, another language) is found by
its set code + number alone - leave the name empty.

Turn the switch off to confirm each card with **Add** / **Skip** instead; auto scanning then
waits for you before the next capture (a card dropped before you click Add is captured right
after). The setting is remembered.

### Searching manually

Type a name in **Search**, optionally with the **Set** code and **Number** from the card's
bottom-left corner (e.g. `HOB` and `14`) - together they go straight to the exact printing -
and a **Treatment** (e.g. Borderless). If more than one printing matches, choose yours from
the thumbnail grid. After a capture, the fields are filled with what the AI read, so you can
correct a misread and search again. Press Enter in any field to search.

### Scanned cards

What you scan is kept apart from your collection until you say so: the **Scanned** count in the
top bar opens the list. Check it, fix what is wrong, then click **Add to collection** (you can choose one of your locations or type a new one - *Box #1*, a binder - before they are added) - or
**Clear all** to throw the session away and start over, which never touches the collection.

Open it from the Scanned count in the top bar. Each entry shows a thumbnail of what was
captured: hover over an entry to see all its captured copies side by side, or click (tap) the
thumbnail to open them larger - handy to check what a scan actually added. You can filter, sort (newest / oldest, name,
price, total value, quantity, rarity, set and number - the choice is remembered in the browser),
edit quantity / condition / finish (changing the finish of part of a stack splits it; the price follows the finish), delete and clear. Exporting and importing
are on the collection page: the **Export…** menu lists the sites the collection can be written for
(Magic: Moxfield) and **Card Scanner (everything)** - the app's own CSV with every entry's
location, tags, finish and printing, which **Import** reads back exactly as it was (capture
thumbnails and decks are not in it - the backups have those). **Import** also adds a Moxfield
collection CSV to the collection or replaces it - each row is matched to its printing by set code and collector number
(by name when that fails, which is reported). Moxfield has no locations, so an import has none. Adding a card that is already in the inventory with the same
condition, finish and location increases its quantity instead of creating a duplicate.

### Collection page

**Collection** in the top bar (`http://localhost:5000/collection`) is the place to manage what
you have scanned and to build decks. It works on a phone too.

- **Inventory** - filter by name, color identity, type, rarity, set, finish, location, tag and
  price, or tick **Not in a deck** to see only the cards no deck uses yet, or **No use in my decks** to also leave out the cards EDHREC lists for your decks' commanders - what remains can be sold or given away (the deck builder's
  search and suggestions have the same tick, *Not in other decks*); switch between the list and a grid of card images; tick entries to move them to a
  **location** (a binder, a box), tag them (*trade*, *keep*), change their condition, add them
  to a deck or delete them in one step. Copies of the same printing can be in two locations -
  moving part of a stack splits it. *Settings → Scan into location* on the scanner page puts
  everything you scan next into a location.
- **Decks** (Magic) - Commander, Standard, Pioneer, Modern, Legacy, Vintage and Pauper. Search
  the whole card database (name, type, rules text, mana value, colors, "owned", legal in the
  format, within the commander's colors) and click to add. Every card shows whether you own it,
  are missing it, or need it in another deck too; the deck shows what is not legal or
  unfinished, its mana curve, its price and what completing it costs, and **Buy list**
  downloads the missing cards. The set code on a card's row (e.g. `MSC 1`) opens its
  printings to choose the one shown. A deck is only a list: it never changes the inventory. Import a
  pasted decklist or a Moxfield / Archidekt deck address; export as text. The list of your
  decks can be sorted (last changed, name, format, most owned, fewest cards missing, size)
  and filtered by format or by deck / commander name.
- **Deck ideas** - for a Commander deck, *Suggestions* lists the cards played with its
  commander (EDHREC, with how often) and can show only the ones you own; *Popular decks* lists
  public decks from Archidekt and Moxfield you can copy. *What can I build?* has three
  searches: **Build around a card** - choose a format, click one of your cards that is legal in
  it to see it (tick **Not in a deck** to leave out the cards your decks already use), then **Find decks** for public decks that play it, the ones you own most of
  first, to copy as a deck (the button becomes **Stop** while it looks);
  the legendary creatures you own, by how much of their usual deck you already have; and the
  preconstructed decks, by how much of each you own. A search that is running can be stopped. These read other sites, so they need
  internet; if a site doesn't answer, only that list is missing.
- **Preconstructed decks** - search the Commander and Challenger decks by name, set code or
  year. **Open as deck** makes a deck list to change as you like; **I own it** also adds its
  cards to your inventory (the printings and foils that come in the box, Near Mint, in a
  location named after the deck unless you change it) - then upgrade the deck from there.
- **Added by mistake?** The inventory is sorted by when cards were added to the collection, and
  the *Added* filter lists each "Add to collection" with its time. Choose one and click
  **Remove this batch** to take exactly those copies back out (copies you had before stay).
- **Statistics** - cards and value by color, type, rarity, finish, set, location and tag, and
  your most valuable cards.
- **Backups** - one is made automatically the first time the app starts each day (the last
  7 are kept). The gear button opens Settings: **Back up now** keeps your collection, the
  scanned cards and your decks as they are (with an optional note) - do it before a big load,
  like adding a pile of scanned cards or importing a file. **Restore** puts all three back as
  they were; what you had at that moment is backed up first, so a restore can be taken back.
  Card data and settings are not part of these backups (`scripts/backup.sh` archives
  everything).

Deck building needs card data downloaded with this version: the Decks tab offers
**Update card database** when it is missing.

### Tips for reliable scans

- **Contrast**: a light, plain background (e.g. a white box) makes the card's dark border
  easy to find. White-bordered cards on a white background have no visible outline - use
  a darker background, or capture them with auto-detection turned off.
- **Keep the whole card in view** with a small margin. If an edge is cut off, the card isn't
  detected - and the foil marker in the bottom-left corner can't be read.
- **Focus**: click **Refocus** once with a card in the box. The scanner sweeps through the
  camera's focus range (~10 s), locks the sharpest position and remembers it - no autofocus
  hunting when cards are dropped quickly. While you scan, every few cards it checks whether a
  slightly different focus is sharper and follows it (the best focus drifts as the pile grows
  and the camera warms up); if a card stays blurry for 3 s it refocuses completely. **Settings → Camera autofocus** switches back to the
  camera's continuous autofocus.
- **Camera sideways** gives more detail and a taller pile: the card's long side then runs along
  the image's long side. Set **Settings → Camera rotation** so the card shows upright.
- **Light** evenly from above, so reflective foils don't glare.
- Cards printed before the ★/• convention (roughly before 2020) have no foil marker; for
  those, set the finish yourself when both versions exist.

## Configuration

Settings live in `config.yaml`. The most useful ones:

| Setting | Default | Description |
|---|---|---|
| `camera.type` | `auto` | `auto`, `usb` or `picamera` |
| `camera.usb_index` | | `/dev/videoN` number of the USB camera |
| `camera.resolution` / `fps` | `[2560, 1440]` / `20` | Capture resolution and frame rate |
| `detection.allow_landscape` | `false` | Accept cards lying sideways (a card's art box can look like a sideways card) |
| `auto_capture.delay` | `1.0` | Minimum seconds between automatic captures |
| `auto_capture.stability_frames` | `5` | Still, in-focus frames required before capturing |
| `auto_capture.min_sharpness` | `250` | Minimum sharpness for auto-capture; lower it if cards stay on *Focusing* |
| `auto_capture.refocus_every` | `10` | Every this many captures, check if a focus one step away is sharper and follow it (the next card waits about a second longer); `0` = off. Also in **Settings**, which wins once used |
| `fast_scan.stability_frames` | `6` | Same, when adding cards automatically (~0.3 s, lets a dropped sleeved card stop sliding) |
| `camera.rotate` | `0` | Rotate the image (0/90/180/270) for a camera mounted sideways - also in Settings |
| `vision_ai.provider` | `gemini` | Default AI provider (see above) |
| `vision_ai.detect_foil` | `true` | Read the ★/• foil marker |
| `ocr.provider` | `auto` | Where light-ocr runs: `auto` (GPU when available), `cpu`, `webgpu`. Switch OCR on/off in Settings |
| `vision_ai.image_size` | `1024` | Longest side of the card image sent for identification (larger = slower, not more accurate) |
| `vision_ai.local.endpoint` | | Local AI server (the model is chosen in Settings) |
| `flask.host` / `port` | `0.0.0.0` / `5000` | Web server address |
| `flask.debug` | `false` | The web server's debug mode (debugger pages, and every request logged to `data/logs/requests.log`) - also **Settings → Debug mode**, which wins once used; applies after a restart. Anyone on your network can reach the debugger while it is on |
| `cleanup.enabled` / `days` | `true` / `7` | Delete scanned images older than N days on startup |

Environment variables `VISION_AI_PROVIDER` and `LOCAL_AI_ENDPOINT` override the matching
settings.

## Maintenance

| Task | How |
|---|---|
| Update card data and prices | Click the Database counter when it shows a dot, **Settings → Update card database** (the game being scanned), or `python3 setup_database.py` (Magic) |
| Scanned image statistics | `python3 cleanup.py --stats` |
| Delete old scanned images | `python3 cleanup.py --days 30` (add `--dry-run` to preview, `--all` for everything) |
| Back up database, inventory, settings, images and `.env` | `scripts/backup.sh` (can run while the app does) |

Your inventory lives in the same SQLite file as the card data (`data/cards_database.db`,
table `inventory`); updating the card database does not touch it, and scanning keeps working
during an update (the new data replaces the old in one step at the end). How old Magic data may
get before the update notice shows is `database.update_after_days` in `config.yaml` (7).

Logs are written to `data/logs/`:

| File | Contents |
|---|---|
| `app.log` | Web app, searches, inventory actions |
| `ai.log` | AI requests and responses |
| `scanner.log` | Camera, detection, captures |
| `database.log` | Database queries |
| `scanned_cards.log` | One CSV line per identified card |
| `requests.log` | Every request the web server answered - only with **Debug mode** on |

## Troubleshooting

**No camera found / black video** - check the device number with `v4l2-ctl --list-devices`
and set `camera.usb_index`. Only one program can use the camera at a time. Without a camera
the app still starts: the scanner page shows **No camera** with the reason and picks the camera
up when it is connected (no restart), and the collection page works as usual.

**Auto scanning never captures** - the status tells you why: *Focusing* (image not sharp
enough - click **Refocus**, or lower `auto_capture.min_sharpness`), *Stabilizing* (card still
moving), or *Captured - drop the next card* (it's waiting for a new card to land). The log
shows *New card detected* with the measured jump and image change for each drop.

**Card not detected** - make sure the whole card is visible with some margin and the
background contrasts with the border (see *Tips*). A card without a clear outline can be captured with auto-detection turned off.

**"Vision AI disabled"** - no API key was found for the selected provider. Check `.env`, or
switch provider in Settings.

**Local AI returns 404** - Ollama answers 404 when the requested model isn't installed. Pick
one of the models listed in Settings (they come from your server) or `ollama pull` it.

**Wrong printing** - check the set code and number the AI read (shown in the Search bar after
a capture); correct them there and search again, or pick the printing from the grid. If a model
keeps misreading the same thing, adjust its prompt in **Settings → Vision AI → Edit prompts**
and check it with **Test on last capture**.

**The service doesn't start** - `journalctl -u mtg-scanner -n 50` shows why (most often the
card database is missing or another program has the camera).

## Project structure

```
app.py               Flask + Socket.IO web app, routes and event handlers
scanner.py           Camera capture thread, detection, stability, auto-capture
object_detector.py   Card outline detection + perspective correction
card_ocr.py, ocr/    OCR first: light-ocr reader (Node.js) and the parser for what it reads
card_identifier.py   Vision AI providers, card identification, foil marker check
prompts.py           AI prompts: built-in ones and those edited in Settings (data/prompts.json)
database.py          Scryfall card database: download, schema, search, printings
backups.py           Backups of the collection, scanned cards and decks (collection page)
games/               Card games: base.py (interface), mtg.py (Magic)
card_search.py       Magic search helpers
inventory.py         Inventory storage, locations and tags, stats, import/export
decks.py             Decks (lists of cards; the inventory says what is owned)
recommendations.py   Deck ideas from EDHREC, MTGJSON, Archidekt and Moxfield (cached)
cleanup.py           Scanned image cleanup (also a CLI)
setup_database.py    Downloads and builds the card database
config.yaml          Settings (loaded by config.py / config_loader.py)
settings.py          UI preferences saved in data/settings.json
templates/, static/  Web interface
scripts/             deploy.sh (install/update), mtg-scanner.service (template), start.sh, backup.sh
requirements.txt     Python dependencies
data/                Card database, settings, logs (created at runtime)
scanned_cards/       Captured card images (created at runtime)
```

[PROGRAM_DOCUMENTATION.md](PROGRAM_DOCUMENTATION.md) explains how everything works inside (detection,
auto-capture, identification, matching, database); [INSTALL.md](INSTALL.md) covers deployment.

## HTTP API

The web interface uses these endpoints, which you can also call directly. The inventory
endpoints work on the collection; add `?area=scan` for the cards scanned and not yet added to it.

| Endpoint | Description |
|---|---|
| `GET /video_feed` | MJPEG stream of the annotated camera view |
| `GET /api/stats` | Database and inventory statistics |
| `GET /api/detection_status` | Whether a card is detected and how stable it is |
| `POST /api/stations/<id>/captures` | A card captured by a station (multipart `image`, optional `foil_image`, `name`, `wait`, `capture_id`): read, then added or queued for review - see PROGRAM_DOCUMENTATION.md, Stations |
| `GET /api/stations/<id>/captures/<n>`, `POST /api/stations/<id>/undo` | The outcome of a capture that was still pending; take back the station's last card |
| `GET /scan/<id>` | A station's scanner page (its camera, cards and review queue); `/` lists the stations when the server has no camera of its own |
| `GET /api/stations`, `PUT` / `DELETE /api/stations/<id>` | The stations; rename one or set its location; forget one |
| `GET /api/games` | Supported card games (finishes, export formats) and the active one |
| `GET /api/inventory` | The active game's inventory (each entry has an `id` and its `captures`) |
| `GET /captures/<file>` | Thumbnail of a capture kept with an inventory entry |
| `GET /review_images/<file>` | Capture of a review queue item |
| `POST /api/inventory/update/<id>` | Update an entry (JSON: `quantity`, `condition`, `finish`, `location`, `tags`, `split_quantity`; `card_id`: another printing of the same card) |
| `GET /api/inventory/<id>/printings` | The printings an entry can be changed to (the one it is marked `current`) |
| `POST /api/inventory/bulk` | One change to several entries (JSON: `ids`, `action`: `delete` / `condition` / `location` / `add_tag` / `remove_tag`, `value`) |
| `POST /api/scan_location` | Inventory location for the cards scanned next (JSON: `location`) |
| `POST /api/sound` | Remember the sound switch and volume (JSON: `enabled`, `volume` 0-100) |
| `GET /api/cards/search` | Deck builder card search (`q`, `type`, `text`, `cmc`, `rarity`, `colors`, `identity`, `format`, `owned`, `commander`, `offset`); a card you own comes in the printing you own |
| `GET /api/backups`, `POST /api/backups` | List the backups of the collection, scanned cards and decks; make one (JSON: `note`) |
| `POST /api/backups/<id>/restore`, `DELETE /api/backups/<id>` | Put all three back as in a backup (the current state is backed up first); delete a backup |
| `GET /api/decks`, `POST /api/decks` | List decks; create one (JSON: `name`, `format`, and optionally `text`, `url`, `precon`, `commander`) |
| `GET` / `PUT` / `DELETE /api/decks/<id>` | A deck with card data, copies owned and issues; rename / change format; delete |
| `POST /api/decks/<id>/cards` | Change cards (JSON: `cards`: `name`, `board`, and `change`, `quantity`, `move_to` or `printing`) |
| `GET /api/cards/printings?name=` | Every printing of a card, with the copies of each that are owned |
| `POST /api/decks/<id>/import`, `POST /api/decks/<id>/duplicate` | Add cards from a list, a deck address or EDHREC's average deck; copy a deck |
| `GET /api/decks/<id>/export/<text\|buylist>` | Download the decklist, or the cards not owned |
| `GET /api/decks/<id>/suggestions`, `GET /api/decks/popular` | EDHREC cards for the deck's commander; public decks on Archidekt / Moxfield |
| `GET` / `POST /api/decks/ideas/<commanders\|precons\|card>` | "What can I build?" searches: state / start a run (`card`: JSON `card`, `format`) / stop it (`{"stop": true}`) |
| `GET /api/precons`, `POST /api/precons/<file>/own` | Preconstructed decks; add one's cards to the inventory and open it as a deck (JSON: `name`, `location`) |
| `GET /api/inventory/suggested` | The owned cards EDHREC lists for the decks' commanders (`cards`: name → decks; `unknown`: decks without data) |
| `POST /api/inventory/delete/<id>` | Delete an inventory entry |
| `POST /api/import_inventory` | Import a collection CSV into the active game (multipart `file`, `replace_existing`): Moxfield (Magic), or the app's own columns |
| `POST /api/clear_inventory` | Delete the active game's inventory entries |
| `GET` / `POST /api/scan_inventory/to_collection` | Cards waiting and locations in use; move the scanned cards into the collection (JSON: `location`, optional - where they all go) |
| `POST /api/inventory/remove_batch` | Take back the cards added at one time (JSON: `added_at`) |
| `GET /api/export_inventory/<format>` | Download the inventory: `moxfield` (Magic), `csv` (the app's own, every column) |
| `GET /api/ai_provider` | Current AI provider and model |
| `GET /api/ai_models` | Built-in model lists for each provider |
| `GET /api/local_ai_models` | Models installed on the local AI server |

Scanning, searching and settings go through Socket.IO events (`capture_card`, `search_card`,
`select_printing`, `add_to_inventory`, `toggle_auto_capture`, ...); see `app.py`.

## License

[MIT](LICENSE) - free to use, modify and share, including commercially, as long as the
copyright notice is kept.

## Acknowledgments

- Card data, prices and images: [Scryfall](https://scryfall.com)
- Computer vision: [OpenCV](https://opencv.org)
- Web: [Flask](https://flask.palletsprojects.com) and
  [Flask-SocketIO](https://flask-socketio.readthedocs.io)

This is an unofficial fan project, not affiliated with or endorsed by Wizards of the Coast.
Magic: The Gathering is a trademark of Wizards of the Coast LLC.
