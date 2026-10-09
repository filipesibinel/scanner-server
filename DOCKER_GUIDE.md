# Setting up the scanner with Docker - step by step

From nothing to scanning cards: one **server** and one **camera station**, both as Docker
containers. About 20 minutes, most of it waiting for downloads.

```
   camera station                          server
 ┌───────────────────┐                ┌──────────────────────────┐
 │ any Linux machine │  one picture   │ reads the cards (OCR, AI)│     your browser
 │ with a USB webcam │ ── per card ─> │ keeps the collection     │ <── http://<server>:5000
 │ (PC, laptop, Pi)  │                │                          │
 └───────────────────┘                └──────────────────────────┘
```

The two can be the same machine. [INSTALL.md](INSTALL.md) is the reference for everything
else (running without Docker, a phone as a station, a single machine with its own camera).

This project, including this guide, was written by an AI coding assistant (Claude) directed by
its author - see the [README](README.md#how-this-project-was-built). The last section says how
the steps below were checked.

## What you need

| | |
|---|---|
| **Server** | A Linux machine, x86-64, always on while you scan. 2 GB of free disk. No camera |
| **Camera station** | A Linux machine with a free USB port, x86-64 or ARM64 (a Raspberry Pi 5 works well). It can be the server itself |
| **Camera** | A USB webcam, mounted looking straight down into a box. One with manual focus control gives the sharpest text (most autofocus webcams have it) |
| **A box** | Light and plain inside, a little larger than a card - the card's dark border must stand out |
| **Network** | Both machines on the same network; Wi-Fi is enough |
| **Access to the code** | The repository is private: you need to be able to `git clone` it |

Optional: an NVIDIA GPU in the server (cards are read in 0.1 s instead of 0.7 s), and a vision
AI - an API key for Gemini, OpenAI or Anthropic, or an [Ollama](https://ollama.com) server -
for the cards plain text recognition can't read (full-art and borderless ones). Without an AI
those cards wait in a review list for you to look up by hand.

## Before you start: Docker

On **both** machines. Skip this if `docker compose version` already answers.

```bash
curl -fsSL https://get.docker.com | sudo sh        # Docker's own install script
sudo usermod -aG docker $USER                      # use docker without sudo
```

Log out and back in, then check:

```bash
docker compose version        # Docker Compose version v2.x or newer
```

## Part 1 - The server

**1. Get the code and make the two data folders.** They are made first so that they belong to
you and not to root.

```bash
git clone https://github.com/filipesibinel/scanner-server.git
cd scanner-server
mkdir -p data scanned_cards
```

**2. Start it.**

```bash
docker compose up -d --build
```

The first build takes a few minutes (it downloads Python, Node.js and the libraries; later
builds take seconds). It ends with:

```
 ✔ Container mtg-scanner-server  Started
```

**3. Watch the first start.** It downloads the card data once - about 75 MB, every Magic
printing with prices - into `data/`.

```bash
docker compose logs -f
```

Wait for these lines, then leave with Ctrl+C (the server keeps running):

```
✓ Magic: The Gathering: 112,771 cards
...
Web Interface Starting...
```

"Vision AI disabled" at this point is normal: no AI is chosen yet.

**4. Find the server's address** - you need it for the browser and for the camera station.

```bash
hostname -I        # the first address, e.g. 192.168.1.20
```

**5. Open it.** In a browser on any device on your network: `http://192.168.1.20:5000` (with
your address). You see **Cameras** - empty for now - and **Collection** in the top bar.

That is the whole server. Everything it must keep is in the `data/` folder.

## Part 2 - A camera station

On the machine with the webcam. If that is the server itself, see
[Both on one machine](#both-on-one-machine) first.

**1. Get the code.**

```bash
git clone https://github.com/filipesibinel/scanner-server.git
cd scanner-server
```

**2. Plug in the webcam and find its name.**

```bash
ls /dev/v4l/by-id/
```

```
usb-ACME_Webcam_HD_12345678-video-index0
usb-ACME_Webcam_HD_12345678-video-index1
```

Take the one ending in **`-video-index0`**. This name stays the same when the camera is
unplugged or the machine restarts, which `/dev/video0`, `/dev/video2`, ... do not.

**3. Write the station's three settings** into a file named `.env` in this folder - the
server's address from Part 1, your camera's name, and what to call this camera:

```bash
cat > .env <<'END'
SCANNER_SERVER=http://192.168.1.20:5000
SCANNER_CAMERA_INDEX=/dev/v4l/by-id/usb-ACME_Webcam_HD_12345678-video-index0
SCANNER_STATION_NAME=Desk camera
END
```

**4. Start it.**

```bash
docker compose -f docker-compose.client.yml up -d --build
docker compose -f docker-compose.client.yml logs -f
```

You should see (Ctrl+C to leave):

```
INFO Connected to http://192.168.1.20:5000 as Desk camera (your-machine-name)
INFO Camera settings: sharpness=50, zoom=100, continuous autofocus
INFO Resolution: 2560x1440 @ 20 FPS
INFO Camera initialized successfully (usb)
```

**5. Look at the server's page again.** *Desk camera* is now listed under Cameras. Click it:
you see the camera's live picture.

The station needs nothing else. It has no settings of its own to look after and keeps no
cards - it can be switched off, moved or reinstalled at any time.

### Both on one machine

Use the same `scanner-server` folder for both (skip the clone), and give the station the
server's **container name** instead of the machine's address:

```
SCANNER_SERVER=http://server:5000
```

The two containers share a private Docker network, where the server is called `server` and
always answers on port 5000. The machine's own network address often does not work from
inside a container on the same machine (a firewall blocks it - seen with `ufw`). Starting the
station prints a warning about "orphan containers": it only means the folder has two compose
files, and can be ignored.

## Part 3 - Scan your first cards

On the camera's page (`http://<server>:5000/scan/<its id>`, or click it on the start page):

**1. Choose how cards are read** (once, for all cameras). Open **Settings** (the button at the
top right) → **Vision AI**. Pick a provider and paste its API key, or pick *Local* for an
Ollama server and choose a vision model. Skip this to try it with text recognition alone.

**2. Put a card in the box.** It gets a green outline and the status says **Ready**. If it
says *Focusing* instead, or the small print at the bottom of the card looks soft:

**3. Click Refocus.** The camera tries its whole focus range for about ten seconds and locks
the sharpest position. Do this once, with a card in the box; it is remembered for this camera.

**4. Click Start auto scanning** and drop cards onto the pile, one at a time. **Wait for the
beep before dropping the next one** - it means the picture is taken. About two seconds per
card.

**5. Watch the top bar.** *Scanned* counts the cards added; *Review* counts the ones the
scanner was not sure about. Click **Review** to go through those: each shows the picture next
to the suggested card - **Add** it, correct it with the search, or **Skip**.

**6. Click Scanned** to see the list, fix anything wrong, then **Add to collection**. Your
cards are now under **Collection**, where you can sort them into locations, build decks and
export to Moxfield.

## Optional extras

**Read cards on an NVIDIA GPU.** Install the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
on the server, then start the server with both files - now and every time:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
grep "light-ocr ready" data/logs/ai.log | tail -1      # ... light-ocr ready (webgpu)
```

**Ollama on the server machine.** Nothing to configure: the server looks for it at the host's
port 11434. Ollama must accept connections from other than `127.0.0.1` (`OLLAMA_HOST=0.0.0.0`
in its service).

**A second camera.** Repeat Part 2 on another machine. Each camera gets its own page with its
own review list; the scanned cards are one list that can be narrowed to one camera.

**A phone as a camera.** The Android app, with *Send cards to a scanner server* turned on in
its Settings and the server's address.

**Lock out other devices.** Anyone on your network can open the pages. To at least require a
password from cameras, put `SCANNER_STATION_TOKEN=<something long>` in a `.env` file beside the
server's `docker-compose.yml`, run `docker compose up -d` again, and add the same line to
every station's `.env`.

## Day to day

| | On the server | On a camera station |
|---|---|---|
| Stop | `docker compose stop` | `docker compose -f docker-compose.client.yml stop` |
| Start | `docker compose up -d` | `docker compose -f docker-compose.client.yml up -d` |
| See what it is doing | `docker compose logs -f` | `docker compose -f docker-compose.client.yml logs -f` |
| Update to a new version | `git pull && docker compose up -d --build` | `git pull && docker compose -f docker-compose.client.yml up -d --build` |

- Both start again by themselves after a reboot.
- Update the server first; the stations reconnect on their own.
- With the GPU file, add `-f docker-compose.yml -f docker-compose.gpu.yml` to every server
  command.
- **Backup**: the collection page's Settings has *Back up now* (and makes one every day). For
  everything, stop the server and copy its `data/` folder.

## If something goes wrong

| What you see | What to do |
|---|---|
| The camera's page says **No camera station connected** | The station isn't running or can't reach the server. On the station: `docker compose -f docker-compose.client.yml logs`. "not reachable - trying again" means the address in `.env` is wrong or the server is down; it connects by itself once it can |
| The station's log says **No camera found** | The name in `SCANNER_CAMERA_INDEX` is wrong, or another program has the camera open (a video call, another scanner). Check `ls /dev/v4l/by-id/`, fix `.env`, then `docker compose -f docker-compose.client.yml up -d` |
| The card is not outlined | The whole card must be in view, on a background that contrasts with its border. For sleeved or borderless cards use **Fixed area** on the camera's page |
| Status stays on **Focusing** | Click **Refocus** with a card in the box |
| Many cards go to **Review** | Usually focus: the small print is too soft to read. **Refocus**. Full-art and borderless cards need a vision AI |
| **Vision AI disabled** | No key for the chosen provider: enter one in Settings → Vision AI |
| `permission denied ... docker.sock` | You are not in the `docker` group yet: log out and in after `usermod`, or use `sudo` |
| The browser can't open the page | Check the address and that the server is running (`docker compose ps`); a firewall on the server must allow port 5000 |
| Port 5000 is taken on the server | Put `SCANNER_PORT=5050` in a `.env` beside the server's compose file, restart it, and use that port in the browser and in the stations' `SCANNER_SERVER` |

## How this guide was checked

Parts 1 and 2 were run on 2026-10-09 from a clean copy of the repository, on an Ubuntu
machine with Docker 29: the server built, downloaded its card data and was serving its pages
40 seconds after `docker compose up` (with the image layers already cached - a first build
downloads more), and a station started from the same copy connected and appeared on the
server's page. That trial station had no camera attached; a station with a camera was run the
same way on a laptop and on a Raspberry Pi 5, where Part 3 was done with real cards. Both on
one machine was tried too: with the machine's network address the station could not connect
(the firewall), with `http://server:5000` it did. Installing Docker itself with the script
above was not part of the trial.
