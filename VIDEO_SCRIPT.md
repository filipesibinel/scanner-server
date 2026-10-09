# Video script - "From zero to scanning: the card scanner with Docker"

A script for a walkthrough video of [DOCKER_GUIDE.md](DOCKER_GUIDE.md): what to show, what to
say, what to type. About **6 minutes**. A 60-second cut is at the end.

Each scene lists the **picture** (what is on screen), the **voice** (to read or adapt) and what
is **typed**. Times are a guide; the waiting parts are meant to be sped up or cut.

## Before recording

**Have ready**

- A server machine and a camera machine, both with Docker installed and tested, and the
  webcam mounted over the box. A second camera (or the phone) if you want scene 9.
- A pile of 15-20 cards that scan well, plus two "interesting" ones: a foil and a full-art or
  borderless card (it goes to the AI, or to review - good to show).
- A vision AI ready to choose in Settings (an API key in the clipboard, or Ollama running).

**Use a demo server, not your real collection.** On the server machine, a second copy of the
folder with its own empty `data/` and another port shows a truly fresh start and keeps your
cards and addresses out of the video:

```bash
git clone https://github.com/filipesibinel/scanner-server.git scanner-demo && cd scanner-demo
mkdir -p data scanned_cards && echo "SCANNER_PORT=5050" > .env
# for the recording only: another container name, so it runs beside the real server
printf 'services:\n  server:\n    container_name: scanner-demo\n' > demo.yml
docker compose -f docker-compose.yml -f demo.yml up -d --build
```

Delete the folder afterwards. If you record on the real server instead, don't open the
Collection page with your cards in it unless you mean to show them.

**Make the terminal filmable**

- Large font (18-20 pt), a short prompt (`PS1='$ '`), a clean window, dark or light to match
  the web pages.
- Build both images once before recording, so the builds in the video take seconds; say so in
  the voice-over (scene 3) rather than pretending.
- Hide what you don't want public: your network addresses are harmless on a home network, but
  API keys are not - paste the key off screen or blur the field.
- Record the screen at 1080p or more; record the box with a second camera or a phone on a
  tripod, close enough to see the cards land.

**Shots to collect separately (b-roll)**

- The box and the camera from the side; a hand dropping cards in rhythm with the beep.
- Close-up of the live view with the green outline appearing and the status turning *Ready*.
- The *Scanned* counter going up.
- A finished Collection page: the grid of card images, a deck.

## The script

| # | Time | Picture | Voice | Typed / clicked |
|---|---|---|---|---|
| 1 | 0:00-0:25 | **Cold open.** B-roll: cards dropped one after another into the box, a beep each time; cut to the screen where the Scanned counter climbs and card names appear. | "This is a pile of Magic cards going into a collection - the exact printing, the price, foil or not - at about two seconds a card. No typing. In this video we set the whole thing up from nothing, with Docker." | - |
| 2 | 0:25-0:55 | **The idea.** The diagram from the guide (camera station → server → browser), parts lighting up as they are named. | "There are two parts. A camera station is any small Linux computer with a webcam - a laptop, a Raspberry Pi. It finds the card and takes one picture of it. The server reads that picture, works out which card it is, and keeps your collection. You use it from a browser. One server, as many cameras as you like." | - |
| 3 | 0:55-1:40 | **Terminal on the server.** Type the three commands; cut or speed up the build. | "First the server. Get the code, make two folders for its data, and start it. The first build downloads everything it needs, which takes a few minutes - I've done that once already, so here it takes seconds." | `git clone https://github.com/filipesibinel/scanner-server.git`<br>`cd scanner-server`<br>`mkdir -p data scanned_cards`<br>`docker compose up -d --build` |
| 4 | 1:40-2:10 | **The log.** The download progress, then the lines `✓ Magic: The Gathering: 112,771 cards` and `Web Interface Starting...`. Highlight them. | "On its first start it downloads the card data: every Magic printing, with prices, about 75 megabytes. When you see the card count, it's ready." | `docker compose logs -f` then Ctrl+C |
| 5 | 2:10-2:30 | **Browser.** Type the address; the Cameras page appears, empty. Click *Collection*: empty too. | "Open the server's address in a browser, port five thousand. No cameras yet, and an empty collection. That's the server done - everything it keeps is in that one data folder." | `ip route get 1.1.1.1 \| grep -o 'src [0-9.]*'` (show the address), then the browser |
| 6 | 2:30-3:20 | **Terminal on the camera machine.** Show the webcam being plugged in (b-roll, 2 s). List the cameras, highlight the `-video-index0` line. Copy the example settings to `.env` and open it in an editor, so the three lines are readable as they are changed. | "Now a camera. On the machine with the webcam: get the same code, and find the camera's name - the line ending in video-index-zero. Then three settings go in a small file: where the server is, which camera, and what to call it." | `ls /dev/v4l/by-id/`<br>`cp .env.station.example .env`<br>`nano .env`: `SCANNER_SERVER`, `SCANNER_CAMERA_INDEX`, `SCANNER_STATION_NAME` |
| 7 | 3:20-3:50 | **Start the station.** The log: *Connected to ...*, *Camera initialized successfully*. Cut to the browser: the camera has appeared on the Cameras page. Click it - the live view. | "Start it. It connects to the server by itself - and there it is. Click it, and you're looking through the camera. The station has no screen and keeps nothing: you do everything from this page." | `docker compose -f docker-compose.client.yml up -d --build`<br>`docker compose -f docker-compose.client.yml logs -f` |
| 8 | 3:50-4:20 | **Settings.** Open Settings → Vision AI, choose the provider (key pasted off screen). Close. Put a card in the box: green outline, *Ready*. Click **Refocus**: the picture goes soft and sharp, then "Focus locked". | "Two things before scanning. Choose how the hard cards are read: a vision AI of your choice. Most cards never need it - they are read as plain text, in a tenth of a second. And with a card in the box, click Refocus once: the camera finds its sharpest position and keeps it." | Settings → Vision AI; **Refocus** |
| 9 | 4:20-5:10 | **Scanning.** Split screen: the box from above, and the page. Click **Start auto scanning**. Drop 8-10 cards, one per beep. The card panel shows each name; *Scanned* climbs. Include the foil (it says Foil) and the full-art card (a moment longer, or the *Review* counter lights up). Optional: a second camera or the phone scanning at the same time, each with its own page. | "Start auto scanning, and drop the cards. Wait for the beep - that's the picture taken - then drop the next one. Each card is matched to its exact printing by its set code and collector number. This one is a foil: it reads the little star next to the set code. And this one has no text box to read, so it took the AI a second longer." | **Start auto scanning** |
| 10 | 5:10-5:35 | **Review.** Click the *Review* counter: the photo next to the suggested card. Click **Add**. | "Anything the scanner isn't sure about waits here, with its photo, and scanning carries on. You check it at the end: add it, correct it, or skip it." | **Review** → **Add** |
| 11 | 5:35-6:00 | **Collection.** Click *Scanned* → **Add to collection**; then the Collection page: the list, switch to the image grid, a glimpse of a deck and the statistics. | "When the pile is done, one click moves it into your collection - where you can sort cards into boxes and binders, build decks, see what a deck is still missing, and export to Moxfield." | **Scanned** → **Add to collection** → *Collection* |
| 12 | 6:00-6:20 | **Close.** The diagram again, now with a second camera and a phone added. The repository address on screen, and the caption *Built with AI: code and documentation written by Claude, directed by the author*. | "That's one server and one camera. Add more cameras the same way, or use a phone. One more thing: this whole project - the code and the guide you just followed - was written by an AI assistant. I told it what I wanted and tested it with real cards. The step-by-step guide and everything else is in the repository." | - |

## Lines for the things that go wrong on camera

Keep one or two in if they happen - they make the video more useful.

| If | Say | Do |
|---|---|---|
| The page says *No camera station connected* | "The station can't reach the server - almost always the address in that settings file." | Fix `SCANNER_SERVER` in `.env`, `up -d` again |
| The station log says *No camera found* | "It can't see the webcam: check the name, and that nothing else has it open." | `ls /dev/v4l/by-id/` |
| A card isn't outlined | "The whole card has to be in view, and its border has to stand out from the box." | Move the card; mention *Fixed area* for sleeves |
| Cards keep going to Review | "The small print is too soft to read - refocus." | **Refocus** |

## A 60-second cut

| Time | Picture | Voice |
|---|---|---|
| 0:00-0:10 | Cards dropping, beeps, the counter climbing | "Scanning a Magic collection at two seconds a card. Here's the whole setup." |
| 0:10-0:25 | Server terminal: the four commands, sped up; the card count line | "On one machine: get the code, start the server. It downloads the card data once." |
| 0:25-0:40 | Camera machine: `ls /dev/v4l/by-id/`, the three-line `.env`, `up -d`; the camera appears in the browser | "On the machine with the webcam: tell it where the server is and which camera to use, and start it. It shows up by itself." |
| 0:40-0:55 | Refocus, Start auto scanning, cards dropping, names appearing | "Refocus once, start scanning, and drop cards on the beep. Each one is matched to its exact printing." |
| 0:55-1:00 | The Collection grid; the repository address | "Then it's in your collection. Guide in the description." |

## Notes for whoever records it

- **Say that it was built with AI** (scene 12, and the caption): the project, this script
  included, was written by an AI coding assistant (Claude) directed by its author. Put the same
  line in the video's description.

- The numbers said out loud were measured on this project's own setup: about 1.7 s per card
  over a pile, 0.1 s to read a card as text on a GPU (0.7 s without one), about 1 s for the
  AI, 75 MB of card data, 112,771 printings on 2026-10-09. The card count grows with every
  set - say "over a hundred thousand" if you would rather not date the video.
- Everything shown in scenes 3-7 was run from a clean copy of the repository as written in
  the guide. Scenes 8-11 follow what was done with real cards on a laptop and a Raspberry Pi
  station; the exact clicks have not been rehearsed against this script, so do a dry run.
- If server and camera are the same machine, the station's address is `http://server:5000`
  (see the guide) - worth a caption, since viewers will try that first.
