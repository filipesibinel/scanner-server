# Card scanner images (see INSTALL.md)
#   server - web app, card data, OCR reader and AI requests (docker-compose.yml)
#   client - a camera station: station_client.py next to the camera (docker-compose.client.yml)

# The OCR reader's packages (light-ocr: Node.js, x86-64 only)
FROM node:24-trixie-slim AS ocr
WORKDIR /ocr
COPY ocr/package.json ocr/package-lock.json ./
RUN npm ci --omit=dev


FROM python:3.12-slim-trixie AS server

# The OCR reader's GPU path is WebGPU through Vulkan: libvulkan1, and the X11 / GLVND libraries
# NVIDIA's Vulkan driver loads (measured: without them "Found no drivers" and OCR reads on the CPU)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 libatomic1 \
       libvulkan1 libx11-6 libxext6 libglvnd0 libgl1 libglx0 libegl1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ocr /usr/local/bin/node /usr/local/bin/node

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=ocr /ocr/node_modules ocr/node_modules

# data/ and scanned_cards/ are the volumes; the app runs as the user who owns them on the host
RUN mkdir -p data scanned_cards && chown 1000:1000 data scanned_cards
USER 1000:1000

EXPOSE 5000
ENTRYPOINT ["scripts/docker-entrypoint.sh"]


# The camera station: camera, card detection and focus only - x86-64 and ARM64
FROM python:3.12-slim-trixie AS client

# v4l2-ctl: the USB camera's focus and zoom controls
RUN apt-get update \
    && apt-get install -y --no-install-recommends v4l-utils \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements-client.txt .
RUN pip install --no-cache-dir -r requirements-client.txt

# Only what the station runs (station_client.py must not import the server's modules)
COPY station_client.py scanner.py object_detector.py settings.py config.py config_loader.py config.yaml ./

ENTRYPOINT ["python", "station_client.py"]
