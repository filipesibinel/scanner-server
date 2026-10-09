# Deployment Guide

The scanner is deployed with `scripts/deploy.sh`, run **on the machine that runs the scanner**
(a Raspberry Pi or any Linux PC). The same script installs, repairs and updates it, and can set
it up as a service that starts on boot.

## Raspberry Pi, from scratch

1. **Flash the SD card** with [Raspberry Pi Imager](https://www.raspberrypi.com/software/):
   *Raspberry Pi OS Lite (64-bit)*. In the imager's settings, set a hostname (e.g. `scanner`),
   your user and Wi-Fi, and enable SSH.
2. **Connect the camera** (USB webcam, or the camera module to the CSI port) and boot the Pi.
3. **Log in and install:**

   ```bash
   ssh <user>@scanner.local
   sudo apt install -y git
   git clone https://github.com/filipesibinel/scanner.git
   cd scanner
   ./scripts/deploy.sh                 # lists the cameras it finds
   ./scripts/deploy.sh --camera 0 --service      # USB camera /dev/video0, as a service
   # or: ./scripts/deploy.sh --picamera --service   # Raspberry Pi camera module
   ```

4. **Set up the vision AI** (see below), then restart: `sudo systemctl restart mtg-scanner`.
5. **Open** `http://scanner.local:5000` from any device on your network.

A Raspberry Pi 4 or 5 handles the camera, card detection and web interface easily. The card
identification runs on a cloud AI (Gemini, OpenAI or Anthropic) or on a local model server on a
more powerful computer (e.g. Ollama with a vision model on your desktop) - a Pi is too slow to
run a vision model itself.

## Any Linux PC

```bash
git clone https://github.com/filipesibinel/scanner.git
cd scanner
./scripts/deploy.sh                # add --service to start it on boot
venv/bin/python app.py             # if you didn't install the service
```

The script supports apt (Debian, Ubuntu, Raspberry Pi OS), pacman (Arch), dnf (Fedora) and
zypper (openSUSE) for the few system packages it needs.

## Server in Docker

The server (web app, card data, OCR reader, AI requests) runs as a container on any x86-64
Linux machine with Docker - no camera needed there. Files: `Dockerfile` (target `server`),
`docker-compose.yml`, `docker-compose.gpu.yml`, `scripts/docker-entrypoint.sh`.

```bash
git clone <this repository> scanner-server && cd scanner-server
mkdir -p data scanned_cards        # before the first start, so they belong to you, not root
docker compose up -d --build
docker compose logs -f             # the first start downloads the card database (a few minutes)
```

Then open `http://<server>:5000`. Stop with `docker compose stop` (the app closes its databases,
like Ctrl+C).

- **Your data** is in `data/` (collection, scanned cards, settings, API keys entered in
  Settings, backups, logs) and `scanned_cards/` (captured images), next to the compose file.
  To move an existing installation, stop both, copy its `data/` here and start the container.
- **Another user id**: the container runs as 1000:1000, the owner the two folders must have.
  Set `SCANNER_UID` / `SCANNER_GID` in `.env` if yours differ; `SCANNER_PORT` changes the port.
- **API keys**: in Settings, or in `.env` beside the compose file (see `.env.example`).
- **Ollama on the same machine**: the container reaches it as `host.docker.internal:11434`
  (the default `LOCAL_AI_ENDPOINT`). Ollama must listen on more than 127.0.0.1
  (`OLLAMA_HOST=0.0.0.0`) and a firewall must let the Docker network reach port 11434. For
  Ollama elsewhere, set `LOCAL_AI_ENDPOINT=http://<host>:11434/v1/chat/completions` in `.env`.
- **OCR on an NVIDIA GPU** (needs the NVIDIA Container Toolkit):

  ```bash
  docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
  ```

  `data/logs/ai.log` then says `light-ocr ready (webgpu)` instead of `(cpu)`. Measured on an
  RTX 4070 Ti SUPER: 0.10 s per card, against 0.67 s on 4 cores of a Ryzen 7 7800X3D.
- **Updating**: `git pull`, then the same `up -d --build` command.

## What the script does

Each step is skipped when it's already done, so it's safe to run again at any time:

1. **Update** (with `--update`): `git pull`, keeping your local changes (e.g. the camera
   number in `config.yaml`).
2. **System packages**: installs what's missing - Python venv support, `v4l-utils` (camera
   focus/zoom controls) and, with `--picamera`, `python3-picamera2`. Uses `sudo` only here and
   for the service.
3. **Python environment**: creates `venv/` with Python 3.10+ and installs `requirements.txt`
   (about 300 MB).
4. **OCR reader** (optional): if Node.js 22+ with npm is installed, installs
   [light-ocr](https://github.com/arcships/light-ocr) into `ocr/` (about 130 MB), which reads
   most cards without an AI request. Without Node.js this step is skipped and every card goes
   to the vision AI; install Node.js and run the script again to add it later.
5. **Configuration**: creates `.env` from `.env.example` (readable only by you), sets the camera
   with `--camera N`, and checks that an API key is set for your AI provider.
6. **Camera**: checks the configured camera and lists all cameras found.
7. **Card database**: downloads it from Scryfall if missing (`--refresh-cards` downloads the
   latest cards and prices). Your inventory is kept.
8. **Service** (with `--service`): installs `/etc/systemd/system/mtg-scanner.service` for your
   user and folder, enables it and (re)starts it. If the service is already installed, each run
   restarts it so new code and packages are used - except a plain `--refresh-cards`, since the
   running scanner reads new card data directly.

### Options

| Option | What it does |
|---|---|
| `--service` | Install and start the systemd service (starts on boot) |
| `--remove-service` | Stop and remove the service (your data is kept) |
| `--update` | Pull the latest code first, then update packages and restart the service |
| `--camera N` | Use USB camera `/dev/videoN` (sets `camera.usb_index` in `config.yaml`) |
| `--picamera` | Raspberry Pi camera module |
| `--skip-database` | Don't download the card database |
| `--refresh-cards` | Re-download the card database (latest cards and prices) |

## Vision AI

Pick the provider in the web interface (**Settings → Vision AI**); the choice is remembered.

- **Cloud**: pick the provider in Settings and paste the API key into the field that appears -
  it's saved to `data/api_keys.env` and used right away. (Or put `GEMINI_API_KEY=...`,
  `OPENAI_API_KEY=...` / `ANTHROPIC_API_KEY=...` in `.env` and restart.)
- **Local (Ollama)**: pick *Local*, enter your server's address, e.g.
  `http://192.168.1.20:11434/v1/chat/completions` (or set `vision_ai.local.endpoint` in
  `config.yaml`), and pick a vision model. On the
  Ollama machine, make it listen on the network (`OLLAMA_HOST=0.0.0.0`).

## Deploying from your computer

Everything can be driven over SSH (`-t` lets `sudo` ask for your password):

```bash
ssh -t <user>@scanner.local 'cd scanner && ./scripts/deploy.sh --update'
```

## Updating

```bash
./scripts/deploy.sh --update            # new code + packages, restarts the service
./scripts/deploy.sh --refresh-cards     # new cards and prices from Scryfall
```

The card database can also be refreshed from the web interface (**Settings → Update card
database**). To refresh it automatically every Monday at 4:00, add this with `crontab -e`:

```
0 4 * * 1 cd $HOME/scanner && ./scripts/deploy.sh --refresh-cards >> data/logs/refresh.log 2>&1
```

## Running the service

```bash
sudo systemctl status mtg-scanner       # is it running?
sudo systemctl restart mtg-scanner      # after changing config.yaml or .env
journalctl -u mtg-scanner -f            # live logs (the app also writes data/logs/)
```

The service runs as your user with a read-only view of the system and your home folder, except
`data/` and `scanned_cards/` in the project, and gets camera access through the `video` group.
It restarts after a crash, but not after a normal exit (for example when the card database is
missing - see the logs).

## Backups

For your cards alone - the collection, the scanned cards and the decks - the collection page's
Settings (gear button) makes and restores backups in `data/backups/`.

`scripts/backup.sh` archives `data/` (card database, inventory, settings), the scanned images
and `.env` to `~/scanner-backups/`, keeping the last 10. It can run while the app does (the
databases are archived as consistent snapshots); if anything fails it says so, makes no
backup and keeps the older ones. Your inventory lives in
`data/cards_database.db`.

## Uninstalling

```bash
./scripts/deploy.sh --remove-service    # if you installed the service
rm -rf ~/scanner                         # removes everything, including your inventory
```

## Troubleshooting

**The camera isn't found** - `v4l2-ctl --list-devices` lists the cameras; pick one with
`./scripts/deploy.sh --camera N`. Only one program can use the camera at a time, so stop other
instances (a manual `python app.py` and the service can't run together).

**Permission denied on /dev/video0 (manual runs)** - add your user to the `video` group:
`sudo usermod -aG video $USER`, then log out and in. The service has access either way.

**Port 5000 is in use** - another instance is running (`sudo systemctl stop mtg-scanner`), or
change `flask.port` in `config.yaml`.

**The service doesn't start** - `journalctl -u mtg-scanner -n 50` shows the error.

**"Vision AI disabled"** - no API key for the selected provider: check `.env`, or switch to a
local model in Settings.

**Package installation fails** - run the script again (it resumes).
