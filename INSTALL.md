# Installation

The scanner has three parts. Install the server once, then as many cameras as you like:

| Part | Where | How |
|---|---|---|
| [Server](#1-the-server) | One Linux machine (it can be the one running Ollama) | Docker |
| [Camera station](#2-a-camera-station) | Each machine with a USB webcam: a PC, a laptop, a Raspberry Pi | Docker, or Python |
| [Phone](#3-a-phone-as-a-station) | An Android phone | The app |

There is also a way to run [everything on one machine](#everything-on-one-machine-without-docker)
without Docker, as the scanner this project grew from.

## 1. The server

The server (web pages, card data, OCR reader, AI requests, your collection) runs as a container
on an x86-64 Linux machine with Docker and the Compose plugin. It needs no camera.

```bash
git clone https://github.com/filipesibinel/scanner-server.git && cd scanner-server
mkdir -p data scanned_cards        # before the first start, so they belong to you, not root
docker compose up -d --build
docker compose logs -f             # the first start downloads the card data (about 75 MB)
```

Then open `http://<server>:5000`: an empty list of cameras, and **Collection**. Stop with
`docker compose stop` (the app closes its databases, like Ctrl+C).

Files: `Dockerfile` (target `server`), `docker-compose.yml`, `docker-compose.gpu.yml`,
`scripts/docker-entrypoint.sh`.

- **Your data** is in `data/` (card data, collection, scanned cards, decks, settings, the
  stations, API keys entered in Settings, backups, logs) and `scanned_cards/` (capture images as
  uploaded), next to the compose file. Nothing else needs keeping.
- **Another user id**: the container runs as 1000:1000, the owner the two folders must have.
  Set `SCANNER_UID` / `SCANNER_GID` in `.env` beside the compose file if yours differ;
  `SCANNER_PORT` changes the port.
- **Firewall**: the stations and your browser must reach the port (5000). Ports published by
  Docker usually bypass `ufw`.

### Vision AI

Open any camera's page (or, before a camera exists, enter the keys in `.env`) and choose the
provider in **Settings → Vision AI**. It is one choice for all cameras, remembered on the
server.

- **Cloud**: pick the provider and paste the API key into the field that appears - it is saved
  to `data/api_keys.env` and used right away. Or put `GEMINI_API_KEY=...`, `OPENAI_API_KEY=...`
  / `ANTHROPIC_API_KEY=...` in `.env` beside the compose file (see `.env.example`) and run
  `docker compose up -d` again.
- **Ollama on the same machine**: the container reaches it as `host.docker.internal:11434`
  (the default `LOCAL_AI_ENDPOINT`). Ollama must listen on more than 127.0.0.1
  (`OLLAMA_HOST=0.0.0.0`) and a firewall must let the Docker network reach port 11434. Pick
  *Local* and a vision model in Settings.
- **Ollama elsewhere**: set `LOCAL_AI_ENDPOINT=http://<host>:11434/v1/chat/completions` in
  `.env`.

Without any AI the server still reads most cards with OCR; the ones OCR can't confirm go to
the review queue.

### OCR on an NVIDIA GPU

Optional: OCR takes 0.10 s per card on a GPU instead of 0.67 s on the CPU (measured: RTX 4070
Ti SUPER against 4 cores of a Ryzen 7 7800X3D). It needs the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
on the host.

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

`data/logs/ai.log` then says `light-ocr ready (webgpu)` instead of `(cpu)`. Use the same two
`-f` options for every later `docker compose` command.

### Moving an existing collection in

From the single-machine scanner (or another server): stop both, copy its `data/` folder -
`cards_database.db`, `scan_inventory.db`, `settings.json`, `captures/`, `review/`, `backups/` -
into the server's `data/`, and start the server. It adds what is new to the database at
startup and makes a backup of the day. Check the card and deck counts on the collection page
against the old installation before deleting anything there.

From the Android app working on its own: its **CSV** export is the server's own format - import
it on the collection page.

## 2. A camera station

A camera station is a Linux machine with a USB webcam: x86-64 or ARM64, a Raspberry Pi 5 is
tested. It runs `station_client.py`, which finds, captures and focuses on the cards and sends
each one to the server. It keeps nothing - its settings (focus, rotation, fixed area) are
saved on the server - and it has no interface of its own: you use its page on the server.

A webcam with a manual focus control (`v4l2-ctl --list-ctrls` shows `focus_absolute`) lets the
scanner lock the focus; others keep their own autofocus.

### With Docker

```bash
git clone https://github.com/filipesibinel/scanner-server.git && cd scanner-server
ls /dev/v4l/by-id/                 # find your camera: ...-video-index0
cat > .env <<'END'
SCANNER_SERVER=http://<server>:5000
SCANNER_CAMERA_INDEX=/dev/v4l/by-id/<your camera>-video-index0
SCANNER_STATION_NAME=Desk camera
END
docker compose -f docker-compose.client.yml up -d --build
docker compose -f docker-compose.client.yml logs -f
```

The station appears on the server's start page; its page is `http://<server>:5000/scan/<id>`.
It starts again by itself after a reboot. [DOCKER_GUIDE.md](DOCKER_GUIDE.md) walks through the
server and a station step by step, with what each command should print.

**On the same machine as the server**: use the same folder and `SCANNER_SERVER=http://server:5000`
(the server's name on the Docker network the two share) - the machine's own address is often
blocked by its firewall from inside a container.

| Setting | |
|---|---|
| `SCANNER_SERVER` | The server's address (needed) |
| `SCANNER_CAMERA_INDEX` | The camera: `N` of `/dev/videoN`, or its `/dev/v4l/by-id/...` path - that one stays the same camera when the numbers change. Default: `camera.usb_index` in `config.yaml` |
| `SCANNER_STATION_ID` | 1-40 letters, digits, `-` or `_`. Default: the machine's name. It is how the server knows the station: keep it, or the station appears as a new one |
| `SCANNER_STATION_NAME` | Shown on the server (it can be renamed there) |
| `SCANNER_STATION_TOKEN` | The server's station token, when it has one |

- The container gets the host's `/dev` with access to video devices only, so the camera can
  be unplugged and plugged in again without restarting it. Unplugging while it runs has not
  been tested; restart the container if the camera does not come back.
- Nothing is kept in the container: it can be removed and rebuilt at any time.
- Files: `Dockerfile` (target `client`), `docker-compose.client.yml`,
  `requirements-client.txt`. The image is about 510 MB.

### Without Docker

```bash
git clone https://github.com/filipesibinel/scanner-server.git && cd scanner-server
python3 -m venv venv && venv/bin/pip install -r requirements-client.txt
sudo apt install v4l-utils         # v4l2-ctl: focus control (pacman -S v4l-utils, ...)
venv/bin/python station_client.py --server http://<server>:5000
```

Options `--id`, `--name`, `--token`, or the environment variables above. Your user needs
access to the camera (`sudo usermod -aG video $USER`, then log out and in).

### On a Raspberry Pi

Raspberry Pi OS (64-bit) with Docker, and the steps above. Measured on a Pi 5 (8 GB) with an
Anker PowerConf C200 on Wi-Fi: the image builds there in 37 s; about half a core idle and 70%
while scanning, 75 °C without throttling; 1.6 s per card over a pile.

A Raspberry Pi **camera module** is not supported in the container (its software comes from
Raspberry Pi OS packages) and has no focus control here; that path has not been run. Use a USB
webcam.

### When something is away

- **No server at startup**: the station waits for it (its camera settings come from there).
- **Server or network down while scanning**: the station keeps scanning; the captures wait
  and are sent when the server is back. A capture sent twice is one card.
- **No camera**: the station says so on its page and picks the camera up when it is plugged in.

### A station token

Without one, any program on your network can send cards to the server. To require a token,
set it on the server - `SCANNER_STATION_TOKEN=<something long>` in its `.env`, then
`docker compose up -d` - and give every station the same value (`SCANNER_STATION_TOKEN`, or
the field in the phone app's Settings).

## 3. A phone as a station

Install the Android app ([mtg-scanner-android](https://github.com/filipesibinel/mtg-scanner-android),
built from its repository). In its Settings turn on **Send cards to a scanner server** and
enter the server's address (`http://<server>:5000`); **Test connection** checks it. The phone
then finds and captures the cards and the server does the rest. The phone's name and token
are set in the same place.

The phone shows its own camera and each card's result; its page on the server
(*Cards and review on the server* in the app) has its scanned cards and review queue. Without
a connection it keeps capturing and sends the cards when it is back, also after the app was
closed. With the switch off the app works on its own, with its own AI settings and inventory.

## Updating

On the server, then on each camera station:

```bash
git pull
docker compose up -d --build                                # server (add the GPU file if you use it)
docker compose -f docker-compose.client.yml up -d --build   # camera station
```

Update the server first: a station reconnects by itself a few seconds after the server is
back, and scanning continues. Card data is updated from the web interface (**Settings → Update
card database**, or the Database counter when it shows a dot).

## Backups

- **Your cards** - the collection, the scanned cards and the decks: the collection page's
  Settings (gear button) makes and restores backups in `data/backups/`. One is made
  automatically the first time the server starts each day (the last 7 are kept).
- **Everything** - card data, settings, stations, API keys, review queue: stop the server and
  copy its `data/` folder.

## Everything on one machine, without Docker

The server can open a camera itself, as the single-machine scanner did. `scripts/deploy.sh`
installs that on a Linux PC (apt, pacman, dnf, zypper):

```bash
git clone https://github.com/filipesibinel/scanner-server.git && cd scanner-server
./scripts/deploy.sh                # packages, venv, .env, camera check, OCR reader, card data
./scripts/deploy.sh --camera 2     # use /dev/video2
venv/bin/python app.py             # or: ./scripts/deploy.sh --service (starts on boot)
```

`http://localhost:5000` is then that camera's scanner page, and other stations can still
connect to it. With `camera.type: remote` in `config.yaml` it runs as a server without a
camera, like the Docker image.

| Option | What it does |
|---|---|
| `--service` / `--remove-service` | Install and start / stop and remove the systemd service `mtg-scanner` (your data is kept) |
| `--update` | Pull the latest code first, then update packages and restart the service |
| `--camera N` | Use USB camera `/dev/videoN` (sets `camera.usb_index` in `config.yaml`) |
| `--picamera` | Raspberry Pi camera module (untested here) |
| `--skip-database` / `--refresh-cards` | Don't download the card data / download the latest |

The script is safe to run again: each step is skipped when it is already done. It installs
[light-ocr](https://github.com/arcships/light-ocr) into `ocr/` when Node.js 22+ with npm is
there (it needs glibc 2.38 or newer - Debian 13, Ubuntu 24.04); without it every card goes to
the vision AI. `scripts/backup.sh` archives `data/`, the capture images and `.env` to
`~/scanner-backups/`, and `scripts/start.sh` starts the app after checking the key and the
card data.

```bash
sudo systemctl status mtg-scanner       # is it running?
journalctl -u mtg-scanner -f            # live logs (the app also writes data/logs/)
```

## Troubleshooting

**The station's page says "No camera station connected"** - the station isn't running, or
can't reach the server: check `SCANNER_SERVER` and the log on the station
(`docker compose -f docker-compose.client.yml logs`). It keeps trying.

**The station is refused** - "wrong or missing station token" in the server's log: give the
station the server's token.

**"No camera found" on the station** - `v4l2-ctl --list-devices` lists the cameras; set
`SCANNER_CAMERA_INDEX`. Only one program can use a camera at a time.

**Permission denied on /dev/video0 (without Docker)** - add your user to the `video` group:
`sudo usermod -aG video $USER`, then log out and in.

**Port 5000 is in use on the server** - set `SCANNER_PORT` in `.env` (stations then use that
port in `SCANNER_SERVER`).

**OCR runs on the CPU although there is a GPU** - `data/logs/ai.log` says `light-ocr ready
(cpu)`: start the server with the GPU compose file, and check `nvidia-smi` works on the host
and `docker info` lists the `nvidia` runtime.

**OCR did not start** - `data/logs/ocr.log` has the reason. Outside Docker it is usually an
older system library (light-ocr needs glibc 2.38) or a missing `npm install` in `ocr/`.

**"Vision AI disabled"** - no API key for the selected provider: enter one in Settings, or
switch to a local model.

**The server doesn't start** - `docker compose logs` shows why. With an empty `data/` it
first downloads the card data; without internet it stops there.
