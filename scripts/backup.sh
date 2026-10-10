#!/bin/bash
# MTG Card Scanner - Backup Script
# Creates a backup of user data (inventory, database, scanned images)

set -e

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Configuration
SCANNER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"  # the project this script belongs to
BACKUP_DIR="$HOME/scanner-backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="scanner-backup-$TIMESTAMP.tar.gz"
KEEP_BACKUPS=10  # Number of backups to keep

# Print messages
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# Change to scanner directory
if [ ! -d "$SCANNER_DIR" ]; then
    echo "Error: Scanner directory not found: $SCANNER_DIR"
    exit 1
fi

cd "$SCANNER_DIR"

# Create backup directory
mkdir -p "$BACKUP_DIR"

print_info "Creating backup..."

if [ -e "$BACKUP_DIR/$BACKUP_FILE" ]; then
    echo "Error: $BACKUP_DIR/$BACKUP_FILE exists already" >&2
    exit 1
fi

STAGING=$(mktemp -d)
SNAPSHOT_ALSO=()
trap 'rm -rf "$STAGING"' EXIT

fail() {
    echo "Error: $1 - no backup was made, the older backups are kept" >&2
    rm -f "$BACKUP_DIR/$BACKUP_FILE" "$BACKUP_DIR/${BACKUP_FILE%.gz}"
    exit 1
}

# The databases may be in use (the app writes them in WAL mode). While the server runs it is
# asked for a full backup: every database file and the pictures their rows point at, taken at
# one moment (a card being moved or a capture being added is in none or all of them). Without a
# server - stopped, or one that does not know the request - each file is snapshot on its own,
# which is the same thing when nothing is running.
mkdir -p "$STAGING/data"
SERVER_URL="${SCANNER_URL:-http://localhost:5000}"
ANSWER=$(curl -fsS -m 300 -X POST "$SERVER_URL/api/backups/full" 2>/dev/null || true)
FULL=$(printf '%s' "$ANSWER" | python3 -c 'import json, sys; print(json.load(sys.stdin)["backup"]["id"])' 2>/dev/null || true)
if [ -n "$FULL" ] && [ -d "data/backups/full/$FULL" ]; then
    print_info "The running server took a coordinated snapshot ($FULL)"
    MISSING=$(printf '%s' "$ANSWER" | python3 -c 'import json, sys; print(len(json.load(sys.stdin)["backup"]["missing"]))' 2>/dev/null || echo 0)
    [ "$MISSING" = "0" ] || print_warn "$MISSING pictures the databases name are not on disk (listed in data/full_backup_manifest.json)"
    cp "data/backups/full/$FULL"/*.db "$STAGING/data/" || fail "could not read the server's snapshot"
    cp "data/backups/full/$FULL/manifest.json" "$STAGING/data/full_backup_manifest.json"
    for db in data/*.db; do   # files the server does not snapshot (the web cache: fetched again when missing)
        if [ -f "$db" ] && [ ! -f "$STAGING/$db" ]; then SNAPSHOT_ALSO+=("$db"); fi
    done
else
    print_warn "No running server at $SERVER_URL: each database file is snapshot on its own"
    for db in data/*.db; do
        if [ -f "$db" ]; then SNAPSHOT_ALSO+=("$db"); fi
    done
fi
for db in "${SNAPSHOT_ALSO[@]}"; do
    python3 - "$db" "$STAGING/$db" <<'PYTHON' || fail "could not snapshot $db"
import sqlite3, sys
source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
target = sqlite3.connect(sys.argv[2])
source.backup(target)
if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
    sys.exit(1)
PYTHON
done

# What exists of: data/ (without the live database files and logs), the scanned images and
# the API keys - then the snapshots in the databases' place
PARTS=()
[ -d data ] && PARTS+=(data)
[ -d scanned_cards ] && PARTS+=(scanned_cards)
[ -f .env ] && PARTS+=(.env)
[ ${#PARTS[@]} -gt 0 ] || fail "nothing to back up in $SCANNER_DIR"

ARCHIVE="$BACKUP_DIR/${BACKUP_FILE%.gz}"
trap 'rm -rf "$STAGING" "$ARCHIVE"' EXIT
tar -cf "$ARCHIVE" \
    --exclude='data/logs' \
    --exclude='data/backups/full' \
    --exclude='data/backups/migrations' \
    --exclude='data/*.db' \
    --exclude='data/*.db-wal' \
    --exclude='data/*.db-shm' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    "${PARTS[@]}" || fail "tar failed"
tar -rf "$ARCHIVE" -C "$STAGING" data || fail "tar failed (database snapshots)"
gzip "$ARCHIVE" || fail "compressing the archive failed"
tar -tzf "$BACKUP_DIR/$BACKUP_FILE" > /dev/null || fail "the archive cannot be read back"

# Get backup size
BACKUP_SIZE=$(du -h "$BACKUP_DIR/$BACKUP_FILE" | cut -f1)

print_info "Backup created: $BACKUP_DIR/$BACKUP_FILE"
print_info "Backup size: $BACKUP_SIZE"

# List backup contents
print_info "Backup contains:"
tar -tzf "$BACKUP_DIR/$BACKUP_FILE" | head -10
TOTAL_FILES=$(tar -tzf "$BACKUP_DIR/$BACKUP_FILE" | wc -l)
echo "  ... ($TOTAL_FILES files total)"

# Clean up old backups
cd "$BACKUP_DIR"
BACKUP_COUNT=$(ls -1 scanner-backup-*.tar.gz 2>/dev/null | wc -l)

if [ "$BACKUP_COUNT" -gt "$KEEP_BACKUPS" ]; then
    print_info "Cleaning up old backups (keeping $KEEP_BACKUPS most recent)..."
    ls -t scanner-backup-*.tar.gz | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm
    REMOVED=$((BACKUP_COUNT - KEEP_BACKUPS))
    print_info "Removed $REMOVED old backup(s)"
fi

# Display current backups
echo ""
print_info "Current backups:"
ls -lth scanner-backup-*.tar.gz 2>/dev/null | head -5 || echo "  No backups found"

# Calculate total backup size
TOTAL_SIZE=$(du -sh . | cut -f1)
print_info "Total backup directory size: $TOTAL_SIZE"

echo ""
print_info "Backup complete!"
echo ""
echo "To restore this backup on another system:"
echo "  tar -xzf $BACKUP_DIR/$BACKUP_FILE -C $SCANNER_DIR/"
echo ""
