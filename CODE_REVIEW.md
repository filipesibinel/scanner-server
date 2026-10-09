# Code review findings

Reviewed: 2026-10-09

The following three bugs were reproduced using temporary SQLite databases. No application data was changed during verification.

## 1. High: restoring a backup can delete the selected backup

**Location:** `backups.py`, `restore()` and automatic backup retention in `create()`.

`restore()` creates a safety backup before opening the selected backup. Creating that backup runs retention cleanup. If the selected backup is the oldest of five automatic backups, cleanup deletes it before restoration can read it.

### Reproduction

1. Create five automatic backups.
2. Restore the oldest automatic backup.
3. The safety backup triggers retention cleanup, deleting the selected backup.
4. Restoration fails with `sqlite3.OperationalError: unable to open database file`.

**Impact:** The requested restore fails and its source backup is permanently removed.

**Suggested fix:** Exclude the selected backup from retention cleanup until restoration completes. Ensure that a failed restore preserves its source backup.

**Regression test:** Restore the oldest of five automatic backups; assert restoration succeeds and the selected backup remains available during restoration.

## 2. High: backups omit station ownership

**Location:** `backups.py`, `INVENTORY_TABLES` and the backup/restore loops.

Backups include `inventory` and `inventory_captures`, but omit `inventory_sources`, which records how many copies each station contributed. Restoration replaces inventory rows while leaving the current ownership table in place.

### Reproduction

1. Add two scanned cards from station `desk-a`.
2. Create a backup.
3. Clear that station's scanned cards.
4. Restore the backup.
5. The overall inventory contains two cards, but the station-specific inventory contains none.

**Impact:** Restored cards can lose their camera assignments. Existing ownership records can also attach to restored rows with matching IDs, producing incorrect assignments. Camera-specific views, transfers, and clears become unreliable.

**Suggested fix:** Include `inventory_sources` in backup and restore. Define explicit behavior for older backups that lack this table rather than retaining unrelated current ownership records.

**Regression tests:** Restore station-owned cards after clearing them, and restore over a different ownership state. Verify both total quantities and station quantities.

## 3. Medium: inventory splits and merges lose station ownership

**Location:** `inventory.py`, `update_card()` and `_copy_with()`.

Splitting an entry moves copies and capture records without transferring the corresponding `inventory_sources` quantities. Merging entries similarly moves captures and deletes the original entry without transferring its station ownership.

### Reproduction

1. Add three regular copies of a card from station `desk-a`.
2. Change one copy to foil, splitting the entry.
3. The overall inventory contains two regular copies and one foil copy.
4. The station-specific inventory contains only the two regular copies.

**Impact:** Edited cards disappear from their station's inventory. Moving that station's scanned cards into the collection can leave those copies behind.

**Suggested fix:** Transfer source quantities alongside copies during splits and merges, within the same transaction. Define how ownership is allocated when an entry contains copies from multiple stations.

**Regression tests:** Split a station-owned entry, merge two station-owned entries, and split an entry shared by multiple stations. Verify station quantities remain consistent with the resulting inventory.

## Recommended improvement

Add executable regression tests for inventory edits, station ownership, and backup restoration. The review found manual test documentation but no executable test suite.

## Verification scope

- Reproduced the three bugs above using temporary databases.
- Python syntax checks passed for 29 files.
- JavaScript syntax checks passed for `static/js/scanner.js`, `static/js/collection.js`, and `static/js/common.js`.
- Camera hardware and external AI integrations were not exercised.
- This review is not a guarantee that the project contains no other defects.
