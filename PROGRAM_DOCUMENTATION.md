# Card Scanner - Technical Documentation

How the scanner works inside. For installing and using it see [README.md](README.md); for
deployment see [INSTALL.md](INSTALL.md).

## Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Card detection](#card-detection)
4. [Auto-capture](#auto-capture)
5. [Identification (vision AI)](#identification-vision-ai)
6. [Matching the printing](#matching-the-printing)
7. [Foil and finish](#foil-and-finish)
8. [Database](#database)
9. [Focus](#focus)
10. [Collection page and decks](#collection-page-and-decks)
11. [Stations](#stations)
12. [Web interface](#web-interface)
13. [Configuration and files](#configuration-and-files)
14. [Performance](#performance)
15. [Known limitations](#known-limitations)

## Overview

The scanner turns a camera pointed into a box into a card-cataloguing station: cards are dropped
onto a pile, each new card is detected, captured once, identified down to the exact printing
and finish, and added to a local inventory.

Design choices:

- **Classic computer vision for detection.** A card is found by its outline, not by an object
  detection model: fast (milliseconds, fine on a Raspberry Pi), exact corners for a flat
  perspective-corrected crop, and unaffected by foil glare.
- **A vision AI only reads text.** It reads the name, collector number and set code from the
  crop, and the ★/• foil marker from a zoomed corner. Any provider works: Gemini, OpenAI,
  Anthropic, or a local Ollama model.
- **The local Scryfall database decides.** AI output is matched against 110,000+ printings;
  set code + collector number identify a printing exactly, and only confirmed matches are added
  without review.

## Architecture

### Modules

| Module | Responsibility |
|---|---|
| `app.py` | Flask + Socket.IO server: routes, events, capture orchestration, station captures |
| `identification.py` | Reading a card, shared by every camera: light-ocr, the vision AI, and the queue in front of each (`Identification`) |
| `stations.py` | Stations: cameras elsewhere that capture cards themselves and upload them (`data/stations.json`) |
| `backups.py` | Backups of the collection, the scanned cards and the decks, made and restored on the collection page |
| `scanner.py` | Camera (USB via OpenCV/V4L2 or Pi camera), capture thread, detection state, stability, auto-capture |
| `object_detector.py` | Outline detection (`find_card_outline`), perspective warp (`warp_card`) |
| `card_ocr.py`, `ocr/server.mjs` | light-ocr reader: the Node.js process that runs the OCR models (kept running, one request per line), and the parsers that pick name, number, set code and foil marker from the text lines |
| `card_identifier.py` | Vision AI providers (`_ask`), response parsing, foil marker check, model warm-up |
| `prompts.py` | Built-in prompts and the ones edited in Settings (`data/prompts.json`), per model |
| `database.py` | Scryfall download and import, schema/migrations, searches, printing lookup, match confidence |
| `games/` | Card games: `base.Game` (the interface the app uses), `mtg.Magic` (Scryfall data, matching, finishes, exports); `games.active()` is the game being scanned |
| `card_search.py` | Magic search helpers combining name, number, set and treatment |
| `inventory.py` | Inventory table for every game: schema + migration, add (merging duplicates), undo, edit/split, bulk edits, locations and tags, delete, stats, CSV import/export |
| `decks.py` | Decks (`decks`, `deck_cards`): lists of card names with a count and a board; never touches the inventory |
| `games/mtg_decks.py` | Magic deck formats, the deck checks (size, copies, color identity, legality) and text decklists |
| `recommendations.py` | Deck ideas from EDHREC, MTGJSON, Archidekt and Moxfield, cached in `web_cache` |
| `settings.py` | UI preferences persisted in `data/settings.json` |
| `config.py`, `config_loader.py` | Settings from `config.yaml` (+ environment variables) |
| `cleanup.py` | Deletes old scanned images (on startup and as a CLI) |
| `setup_database.py` | Downloads and builds the card database |
| `templates/scanner.html`, `static/` | The scanner page |
| `templates/collection.html`, `static/js/collection.js`, `static/css/collection.css` | The collection page (`/collection`): inventory management, deck builder, statistics |
| `static/js/common.js`, `templates/_icons.html`, `templates/_dialogs.html` | Shared by both pages: text helpers, in-page dialogs and notifications, the inventory edit dialog, the capture viewer, the icon sprite |

### Threads

| Thread | What it does |
|---|---|
| Main | Flask + Socket.IO (threading mode) - HTTP routes, Socket.IO events, MJPEG stream |
| Capture (`scanner._capture_frames`) | Reads frames, detects the card, tracks stability, draws the overlay, triggers auto-captures |
| Auto-capture callback | One short-lived thread per auto-capture: crops, saves and (in review mode) identifies the card |
| OCR worker (`Identification._ocr_worker`) | Reads queued captures with light-ocr, one at a time; passes on what it can't settle |
| AI workers (`Identification._ai_worker`, `vision_ai.workers`, default 3) | Ask the vision AI about the cards OCR passed on, several at once |
| Identified worker (`Identification._done_worker`) | Hands each read card back (lookup, add or review queue), one at a time |
| Background tasks | Card database update/rebuild, startup image cleanup, model warm-up, card data update check (10 s after startup, then daily) |

Frames and detection state are shared under `scanner.frame_lock`; the card database and
inventory use a re-entrant lock each around a shared SQLite connection (WAL mode).

**No camera.** The app starts without one (the collection page needs none):
`CardScanner._open_camera` logs one warning and sets `camera_error`, and the capture thread
tries again every `CAMERA_RETRY_SECONDS` (3 s), silently, until it opens - no restart. On Linux
a missing `/dev/videoN` is seen before OpenCV is asked (which would print its own errors at
each try); a device that opens but sends no first frame counts as no camera too. While
running, `CAMERA_LOST_AFTER` (20, ~2 s) failed reads in a row (`_read_failed`: unplugged)
close the camera, clear the frame and go back to waiting - one warning instead of an error
per frame. `camera_error` is part of `/api/detection_status`: the scanner page covers the
video with the reason ("No camera") and disables the capture button.

### Flow of a card

```
camera frame ──> outline detection ──> settled? ──> new card? ──> capture
                                                                     │
      flat, perspective-corrected card image <───────────────────────┘
                 │
                 ├──> light-ocr: name, collector number, set code, foil marker
                 │      confirmed printing? done (vision AI only for a missed foil marker)
                 ├──> otherwise vision AI: name, collector number, set code
                 └──> vision AI: foil marker (zoomed bottom-left corner)
                                    │
                    database: set + number (checked against the name)
                              → name + number → name → fuzzy name
                                    │
              confirmed printing ──> added to the inventory (with Undo)
              uncertain ──────────> shown for review, auto scanning paused
```

## Card detection

**Frames.** USB cameras deliver MJPEG; the scanner asks OpenCV for the raw JPEG
(`CAP_PROP_CONVERT_RGB = 0`) and `grab()`s every frame off the camera (so frames are never stale)
but decodes only `camera.fps` of them. For cameras of 1920 px and wider, live frames are decoded
at **half size** (2560 × 1440 → 1280 × 720): detection, stability, focus measurement and the
preview stream all work at that size, and a capture decodes the stored JPEG at full size and
scales the card's corners up (`get_detected_card`, `get_full_frame`). OpenCV runs with 2 threads
(`cv2.setNumThreads(2)`) - its default of one thread per core spent more CPU spin-waiting than
working. The preview stream encodes each new frame once, shared by all browser tabs
(`get_stream_jpeg`).

**Rotation** (`camera.rotate` or **Settings → Camera rotation**, saved as `camera_rotation`;
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
`scanner.log` says why, once a second (`_trace_waiting`): *no card outline found* (every 5 s), or
*card not ready (n/N)* with the frame's movement, drift, sharpness change and sharpness against
their limits (1%, 1%, 20%, `min_sharpness`). With **Settings → Debug trace** on (remembered: `debug_trace` in
`data/settings.json`), the scanner also
keeps the last 3 s of frames (640 px wide - what the outline detector works on) and saves them,
plus the next second, to `data/debug_frames/<time>_<reason>/` when a card waits over 2 s or a
new card is detected, or the card is "gone", less than 2 s after a capture (a likely duplicate:
a foil Gwen Stacy lost its outline right after its capture and was captured again - by its
look the pair differed 0.79, so appearance can't tell such a case from a new card); the newest 20 dumps
are kept. Replaying such frames through `CardScanner` with a fake camera reproduces the case.

**The capture beep is the signal to drop the next card.** `auto_capture_triggered` (beep +
flash) is sent by `app.handle_auto_capture` once the image is taken and a focus probe started by
that capture is done (`announce_capture`; `scanner.capture_pending` / status `capturing` until
then). Auto-captures take the image at once (`capture_card_image_only(settle=0)` - the card has
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
(`set_fixed_area`, saved as `fixed_area_enabled` / `fixed_area` in `data/settings.json`) judges
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

With **Add cards automatically** (default; internally `fast_scan_mode`, saved as `auto_add`):
the capture is queued, read in the background (`Identification.submit`: OCR, then the AI
workers - see [The two queues](#the-two-queues)), and a confirmed printing
is added immediately by the server (`add_automatically`: one Near Mint copy in
`Game.suggested_finish` - the same rule as the page's `suggestedFinish`), never through the
current card, so it can't replace a card being reviewed. It used to be the page that sent the
add; a tab still running an older script then sent every add without its card and all confirmed
cards ended in the review queue - and with no page open (phone asleep) nothing was added. Every
page is told (`inventory_updated` with `auto: true`) and shows the card with an **Undo** button
(`undo_last_add`).

Anything uncertain - printing not confirmed, name not found, no name read - goes to the
**review queue** (`review.py`, table `review_queue`, a copy of the capture in `data/review/`)
and scanning goes on (it used to pause until the card was reviewed). Items keep what the AI
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
it again when it reconnects. Per game; kept across restarts.

With the switch off, each capture is identified synchronously and waits for **Add** / **Skip**;
a card dropped meanwhile is captured right after.

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
`Identification.identify()` runs both stages in the caller's thread (manual capture, and auto
scanning that waits for Add / Skip). A capture of a game that is no longer the active one is
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
`data/logs/ocr.log`. It needs Node.js 22+ and `npm install` in `ocr/` (`deploy.sh` does it);
without them the switch is disabled and cards go to the AI. `ocr.provider` in `config.yaml`
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

**Test on last capture** runs the AI on the last captured card (`scanner.last_capture`) with the
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
get the `scan_location` setting (Settings → Scan into location). Inventories from before
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
`scanned_cards/` are deleted after `cleanup.days`), `captured_at`. The capture follows the card
from the AI worker to the add: `search_and_emit_card` puts it on the matched card
(`card['capture']`, so a queued automatic add can't take another card's photo), and
`pending_capture` keeps the one under review for a manual search (the automatic "not found"
dismissal keeps it, Skip drops it). Several finishes added at once arrive as one
`add_to_inventory` event (`items`) - separate events ran in parallel threads - and the photo
goes with the first. The photos follow the copies: undo removes that add's photo; moving
copies to another finish moves the newest photos with them; merging moves all; lowering a
quantity drops the newest photos (the usual reason is a card captured twice); deleting or
clearing entries deletes their files. Entries added before this, or imported, have none.
`/api/inventory` returns each entry's `captures` (newest first, URLs under `/captures/`).

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

**Imports never leave a half-filled table.** The download fills a staging table (`cards_import`), committing every 5,000 rows so the inventory can still write, and swap
it in at the end in one step (`CardDatabase.replace_table`, under the database lock, then the
indexes are rebuilt) - scanning keeps using the old data while an update runs.

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

- **Refocus** (button, `reset_focus` → `CardScanner.refocus`): switches autofocus off and runs
  `focus_sweep()` - a coarse pass over the camera's `focus_absolute` range (step 50), then a fine
  pass (step 10) around the best position, scoring each position by the sharpness of the card
  (or the image centre when there's no card); finally a parabola through the best position and
  its neighbours predicts the peak between the fine steps, which is measured and kept if sharper.
  On the C200 the sweep found 444 against a measured peak of 446 (without the parabola: 440, 4%
  less sharp). A lens move takes ~0.4 s to show up in the frames
  (lens + camera buffer), so each position waits 0.45 s; if re-measuring the chosen position
  doesn't confirm it, the sweep repeats with 0.8 s. About 10 s in total. The position is saved
  (`focus_value` in `data/settings.json`) and restored on startup.
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

**Scanned cards and the collection are two inventories.** The scanner page adds to
`scan_inventory` - a second `InventoryManager` on its own file, `data/scan_inventory.db`, with
the same tables - and its top bar counter, inventory window, edit / delete, Undo, export, import
and "Clear all" work on that one only (`?area=scan` on the `/api/inventory*` endpoints,
`inventory_area()` in `app.py`; `/api/stats` reports it as `inventory` and the collection as
`collection`). So a scanning session can be checked, corrected or thrown away without touching
the collection. **Add to collection** (`POST /api/scan_inventory/to_collection`,
`InventoryManager.take_from`) moves every scanned entry of the active game into the collection
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

### Backups

The gear button opens the page's settings drawer (`/collection#settings` opens it directly; the
scanner's drawer links there, and this one links to `/#settings` for camera, AI and sound).
**Back up now** (`POST /api/backups`, `backups.create`) writes `data/backups/<date_time>/`:
`backup.db` with plain copies of `inventory` + `inventory_captures` of the collection
(`collection_*`) and of the scanned cards (`scanned_*`), `decks`, `deck_cards` and an `info`
row (time, note, counts) - every game - and `captures/`, hard links to the thumbnails those
entries point at (no extra space; they survive the app deleting its own). It is written to a
`.tmp` folder and moved into place, under the three managers' locks.

**At startup** `backups.create_daily` (from `initialize_components`) makes the day's backup,
marked `daily` (note "Application start"): a later start the same day finds it and makes
none, an empty collection makes none, and the last `KEEP_DAILY` (7) are kept. A failure is
logged and does not stop the app. Backups made by hand are never deleted automatically.

**Restore** (`POST /api/backups/<id>/restore`, `backups.restore`) first makes an automatic
backup of the current state ("Before restoring ...", the last 5 are kept), then replaces the
rows of each table (the columns the backup has; row ids are kept) and links missing
thumbnails back. Collection, scanned cards and decks are committed one after the other - the
collection and the decks are two connections to one file - so a failure part way leaves the
earlier parts restored; the automatic backup has the state from before. Every page reloads its
inventory (`inventory_updated`). Card data, the review queue and settings are not part of
these backups: `scripts/backup.sh` archives all of `data/`.

`/collection` (`templates/collection.html`, `static/js/collection.js`) works on the active
game's inventory over the REST endpoints; it listens to `inventory_updated`, `inventory_undone`
and `inventory_prices_updated` to follow what is scanned meanwhile, and reloads on `game_changed`.

**Inventory tab.** `/api/inventory` adds to every row its `location`, `tags` and `details` from
`Game.card_details` (Magic: image, mana value, color identity). Filters (text,
color identity, type, rarity, set, finish, location, tag, price), sorts, the list / image grid
and the statistics are computed in the browser from that one response; rows render 200 at a
time. Selected entries get the bulk bar (`POST /api/inventory/bulk`). Rows also carry `decks`:
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

**Deck ideas from other sites** (`recommendations.py`). Every answer is cached in the
`web_cache` table (EDHREC and the MTGJSON list 7 days, precon lists 90 days, deck searches and
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
with a search; the ranking only adds the share owned. "Open as deck" creates a deck from the
list. "I own it" (`precon_own`) also adds the cards to the inventory: MTGJSON names the printing
in the box (`identifiers.scryfallId`, else set + number, else any printing of the name) and
whether it is foil, so the entries get the right set, finish (`Game.suggested_finish`) and
price; they are Near Mint at the location given (the deck's name by default), which is also how
to find them again - filter by that location to move or delete them. Undo does not cover it.

## Stations

A station is a camera somewhere else - a phone, a laptop with a webcam - that finds and
captures the card itself and sends the image to the server, which does everything after that.
The server needs no camera of its own.

`POST /api/stations/<id>/captures` (multipart; `app.py: station_capture`):

| Field | |
|---|---|
| `image` | The card as the station cut it out (JPEG as it is, or PNG) |
| `foil_image` | Optional: the perspective-corrected card, for the ★/• foil check by the AI |
| `name` | Optional: what the station calls itself (used until it is renamed on the server) |
| `wait` | Optional: seconds to wait for the outcome (default 30, at most 120; 0 answers at once) |

`<id>` is chosen and kept by the station (1-40 letters, digits, `-`, `_`); a new id creates the
station (`stations.py`, `data/stations.json`: name, location, capture count, last seen). The
image is saved in `scanned_cards/` as `<id>_<n>_<time>.jpg` and goes through the same two
queues as the scanner page's captures, always as when adding automatically: a confirmed
printing is added to the scanned cards, anything else goes to the review queue (its row keeps
the station id in `review_queue.station`). The answer:

```
{"capture": 12, "status": "added", "seconds": 0.31,
 "card": {"name": ..., "set": ..., "number": ..., "finish": ..., "quantity": 1},
 "read": {"name": ..., "number": ..., "set": ..., "foil": ..., "reader": "light-ocr"}}
{"capture": 13, "status": "review", "reason": "printing not confirmed" | "not found" | "not read" | "game switched", ...}
```

or `202` with `"status": "pending"` when the card isn't settled within `wait` (it still is,
afterwards). Errors: `400` (bad id, no readable picture), `401` (token), `413` (over 30 MB).

- **Location**: a station's cards are put in its own location when it has one
  (`PUT /api/stations/<id>` with `name` / `location`), else in the scanner page's.
- **Order**: a card the AI had to read is added after cards dropped later that OCR confirmed.
  The entry gets the time its capture arrived (`add_card(when=...)`), so the scanned list stays
  in dropping order.
- **Token**: with `stations.token` in `config.yaml` (or `SCANNER_STATION_TOKEN`) set, captures
  need the header `X-Station-Token`. Without it anyone on the network can send cards, like the
  web interface itself.
- `GET /api/stations` lists them; `DELETE /api/stations/<id>` forgets one (its cards stay).

Measured 2026-10-08 (server in Docker, OCR on an RTX 4070 Ti SUPER, a laptop on Wi-Fi sending
recorded captures): one station at a card every 2.5 s - 30 of 30 added, 29 by OCR in 0.34 s
(median, upload included) and 1 by the AI in 1.2 s; four stations at a card every 0.5 s each
(7.8 cards/s) - 100 of 100 added, OCR median 0.48 s, worst 1.0 s.

## Web interface

`templates/scanner.html` + `static/js/scanner.js` + `static/css/style.css` (dark/light theme via
CSS variables). Top bar with statistics; search bar and camera on the left, card panel on the
right, activity log below; settings in a slide-out drawer. The page polls
`/api/detection_status` every 500 ms for the status pill (and `camera_error`, the "No camera"
message over the video) and talks to the server over Socket.IO.

Socket.IO events:

| Client → server | Server → client |
|---|---|
| `capture_card`, `search_card`, `select_printing`, `add_to_inventory` (`finish` + `quantity`, or `items` for several finishes), `undo_last_add`, `dismiss_card` (`keep_capture` from the automatic "not found" dismissal) | `card_captured`, `card_found`, `card_printings`, `similar_cards`, `card_not_found`, `inventory_updated`, `inventory_prices_updated` (prices fetched after an add), `inventory_undone`, `card_dismissed` |
| `toggle_auto_capture`, `toggle_fast_scan` (add automatically), `toggle_detection`, `toggle_ocr` (read with OCR first), `toggle_debug_trace`, `toggle_debug_mode` (Flask's debug mode, for the next start), `reset_focus` (refocus + lock), `set_autofocus`, `set_fixed_area` (`enabled` / `area` / `use_detected`), `set_camera_rotation`, `set_refocus_every` (`captures`: focus probe interval) | `auto_capture_triggered` (image taken, focus probe done: drop the next card), `processing_queue_update`, `*_toggled`, `focus_reset`, `fixed_area_updated`, `camera_rotation_updated`, `refocus_every_updated` |
| `set_ai_provider`, `save_ai_credential`, `update_database` (the active game's data), `rebuild_database` | `ai_provider_set`, `ai_credential_saved`, `database_update_progress` / `_complete` / `_error`, `database_update_available` (update check found newer data), `database_rebuild_*`, `log`, `error` |
| `save_prompt` (scope `model` / `all`), `reset_prompt`, `test_prompt` | `prompts_updated`, `prompt_test_result` (sent only to the client that asked) |
| `review_open`, `review_skip`, `review_close` | `review_item` (the oldest item, or `id: null` when empty), `review_queue_update` (count; `queued: true` when a card was just queued - the page plays the queue alert) |
| `set_game` | `game_changed` (to every client; stops auto scanning; downloads the game's card data if it has none). Captures still waiting for the AI keep their game (`game` on the queue item, `game_id` in `route_identified`): they are not looked up as cards of the new game but go, unread, to their own game's review queue |

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

| Where | What |
|---|---|
| `config.yaml` | Camera, detection, auto-capture, vision AI defaults, web server, cleanup |
| `.env` | API keys (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`); `VISION_AI_PROVIDER` and `LOCAL_AI_ENDPOINT` override `config.yaml` |
| `data/api_keys.env` | Keys and local endpoint entered in Settings (`api_keys.py`, mode 600); overrides `.env`. The UI only ever receives masked keys (`/api/ai_credentials`) - the web interface has no login |
| `data/settings.json` | Choices made in the UI: AI provider/model, OCR first, add automatically, locked focus position, sound effects on/off and volume (`sound_enabled`, `sound_volume`; `POST /api/sound`), focus probe interval (`refocus_every`, overrides `auto_capture.refocus_every`), debug trace, debug mode (`debug_mode`: Flask's debugger, and every request in `data/logs/requests.log` - `enable_request_log`, since `setup_logging` silences Werkzeug; overrides `flask.debug`; taken when the server starts, always without the reloader - a second copy of the program could not open the camera) |
| `data/prompts.json` | Prompt instructions edited in Settings, per game / kind / model (`prompts.py`) |
| `data/review/` | Captures waiting in the review queue (deleted when resolved) |
| `data/captures/` | Thumbnails of the captures behind inventory entries (deleted with their entry) |
| `data/backups/` | Backups made on the collection page (`<date_time>/`, see Backups); copies of the inventory table made before a migration rebuilds it (`inventory_before_*.db`) |
| `data/scan_inventory.db` | Cards scanned and not yet added to the collection |
| `data/cards_database.db` | Card data (`cards`, `card_data_info`), inventory, decks (`decks`, `deck_cards`) and answers cached from other sites (`web_cache`) |
| `data/logs/` | `app.log`, `ai.log`, `scanner.log`, `database.log`, `scanned_cards.log` (one CSV line per identified card; the model column says `light-ocr` when OCR read it), `ocr.log` (errors of the OCR reader process) |
| `scanned_cards/` | Captured images (deleted after `cleanup.days`) |

## Performance

Measured on an x86-64 laptop with an Anker PowerConf C200 at 2560 × 1440 and a local
`qwen3.5:9b` on Ollama over the network:

| Step | Time |
|---|---|
| Camera | 27–29 fps at 2560 × 1440 (MJPEG); 20 fps processed (`camera.fps`) |
| Per processed frame | ~10 ms (half-size decode, detection, stability) - was ~24 ms at full size |
| App CPU while scanning | ~20–25% of one core - was ~120% (full-size decode, 8 OpenCV threads) |
| Outline detection | ~3 ms per frame |
| Card landed → capture | ~0.15–0.5 s (settling) |
| OCR identification | ~0.17 s (light-ocr on the GPU; ~0.8 s on the CPU) - 88% of cards need nothing more |
| AI identification | ~0.9 s (qwen3.5:9b, 1024 px image); the foil check runs in parallel (+~0.3 s with Ollama); ~10 s once if the model has to load |
| Set + number lookup | 0.1 ms; fuzzy name search ~90 ms |

Vision models compared on 90 scans (identification + foil check, before the prompt and image
size change - identification alone is now 0.9 s / 1.5 s, see *Prompts*):

| Model | Hardware | Time per card | Notes |
|---|---|---|---|
| `qwen3.5:9b` (Q4_K_M, 6.6 GB) | Ollama server on the network | 1.6 s | reference; says "unknown" when the ★/• is unreadable |
| `qwen3.5:4b` (Q4_K_M, 3.4 GB) | laptop RTX 3050 6 GB (fits entirely) | 3.4 s | image processing ~920 vs ~2,560 tokens/s on the server; called 3 regular cards foil |

The 9B doesn't fit in a 6 GB GPU (it would be split with the CPU); the 4B is a usable fallback.

## Known limitations

- **White-bordered cards on a white background** have no visible outline; use
  a darker background or capture them manually with detection off.
- **An identical copy landing within ~0.7 mm of the previous card** without the fall hiding the
  card for 6 frames isn't recognized as new - press Capture.
- **Cards without the ★/• marker** (older printings) get their finish from printing data only.
- **Undo** takes back only the most recent add; older adds are edited in the inventory.
- **One camera, one instance**: the camera can only be opened by one process.
