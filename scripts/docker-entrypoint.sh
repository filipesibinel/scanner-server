#!/bin/sh
# Starts the scanner server in its container. On the first start with an empty data volume
# the card database is downloaded (~150 MB, a few minutes) before the web app comes up.
set -e

if ! python -c "
import sys
from config import Config
f = Config.DATABASE_FILE
sys.exit(0 if f.exists() and f.stat().st_size > 0 else 1)"; then
    python setup_database.py
fi

exec python app.py
