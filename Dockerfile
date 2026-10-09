# Card scanner images (docker-compose.yml builds them; see INSTALL.md)
#   server - web app, card data, OCR reader and AI requests

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
