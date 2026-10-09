#!/usr/bin/env python3
"""
Stations: the cameras that send captured cards to this server (a phone, a laptop with a
webcam, ...). A station finds and captures the card itself and uploads the image
(app.py: station_capture); everything after that happens here.

A station is known by an id it chooses and keeps (it appears on its first capture) and has a
name and the inventory location its cards are put in. Kept in data/stations.json.
"""

import json
import os
import re
import threading
from datetime import datetime

from config import Config

STATIONS_FILE = Config.DATA_DIR / 'stations.json'
# Ids are part of capture file names and URLs
VALID_ID = re.compile(r'^[A-Za-z0-9_-]{1,40}$')


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


class Stations:
    def __init__(self, file=None):
        self.file = file or STATIONS_FILE
        self._lock = threading.RLock()
        self._stations = {}
        try:
            if self.file.exists():
                with open(self.file) as f:
                    self._stations = json.load(f)
        except (OSError, ValueError) as e:
            print(f"Error loading stations: {e}")

    def _save(self):
        # Written beside the file and moved into place, like settings.json
        temporary = self.file.with_suffix('.json.tmp')
        with open(temporary, 'w') as f:
            json.dump(self._stations, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, self.file)

    @staticmethod
    def valid_id(station_id):
        return bool(VALID_ID.match(station_id or ''))

    def _public(self, station_id):
        return {'id': station_id, **self._stations[station_id]}

    def get(self, station_id):
        with self._lock:
            return self._public(station_id) if station_id in self._stations else None

    def all(self):
        with self._lock:
            return [self._public(station_id) for station_id in self._stations]

    def capture(self, station_id, name=None):
        """
        A capture arrived from this station: it is created if new and its count goes up.
        name: what the station calls itself - used until it is renamed here.
        Returns the station with 'captures' being this capture's number.
        """
        with self._lock:
            station = self._stations.get(station_id)
            if station is None:
                station = self._stations[station_id] = {
                    'name': (name or '').strip()[:60] or station_id, 'renamed': False,
                    'location': None, 'created': now(), 'captures': 0}
            elif name and name.strip() and not station.get('renamed'):
                station['name'] = name.strip()[:60]
            station['captures'] += 1
            station['last_seen'] = now()
            self._save()
            return self._public(station_id)

    def update(self, station_id, name=None, location=None):
        """Rename a station / set where its cards are put (location None: as the scanner page's)"""
        with self._lock:
            station = self._stations.get(station_id)
            if station is None:
                return None
            if name is not None and name.strip():
                station['name'] = name.strip()[:60]
                station['renamed'] = True
            if location is not None:
                station['location'] = location.strip() or None
            self._save()
            return self._public(station_id)

    def remove(self, station_id):
        with self._lock:
            if self._stations.pop(station_id, None) is None:
                return False
            self._save()
            return True
