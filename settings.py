# ============================================================================
# FILE: settings.py
# Choices made in the web interface, kept in data/settings.json on the server:
# the ones that are everyone's (AI provider and model, OCR first, sound, game),
# and the camera settings of a camera on the server itself - which a station
# inherits until it has its own (stations.StationSettings).
# ============================================================================
import json
import os
import threading
from config import Config


class Settings:
    """Manage user preferences and settings"""

    SETTINGS_FILE = Config.DATA_DIR / 'settings.json'

    DEFAULT_SETTINGS = {
        'ai_provider': 'gemini',
        'ai_model': None,  # None means use provider's default
        'auto_capture_enabled': False,  # Controlled via UI button, not config
        'auto_add': True,  # Auto scanning adds confirmed cards to the inventory without review
        'ocr_first': True,  # light-ocr reads each card; the vision AI only when that isn't a confirmed match
        'debug_trace': False,  # Log filtered detections, save frames of slow or doubtful captures
        'sound_enabled': True,  # Sound effects on the scanner page
        'sound_volume': 30,  # 0-100
        'sound_capture': True,  # The capture beep (the signal to drop the next card)
        'sound_added': True,  # The ding when a card is added to the scanned cards
        'focus_value': None,  # Locked manual focus position (None = continuous autofocus)
        'detection_enabled': True,
        'fixed_area_enabled': False,  # Judge cards by a fixed area instead of their outline (sleeves)
        'fixed_area': None,  # [x1, y1, x2, y2] as fractions of the frame
        # 'camera_rotation' (0/90/180/270) overrides config.yaml camera.rotate once set in the UI
        # 'refocus_every' (captures between focus probes) overrides config.yaml auto_capture.refocus_every once set in the UI
        # 'debug_mode' (Flask's debugger and request log) overrides config.yaml flask.debug once set in the UI
    }

    def __init__(self):
        self._lock = threading.Lock()  # settings are saved from several threads
        self.settings = self._load_settings()

    def _load_settings(self):
        """Load settings from JSON file"""
        try:
            if self.SETTINGS_FILE.exists():
                with open(self.SETTINGS_FILE, 'r') as f:
                    loaded = json.load(f)
                    # Merge with defaults (in case new settings added)
                    settings = self.DEFAULT_SETTINGS.copy()
                    settings.update(loaded)
                    return settings
            else:
                # Create settings file with defaults
                self._save_settings(self.DEFAULT_SETTINGS)
                return self.DEFAULT_SETTINGS.copy()
        except Exception as e:
            print(f"Error loading settings: {e}")
            return self.DEFAULT_SETTINGS.copy()

    def _save_settings(self, settings):
        """Save settings to JSON file"""
        try:
            # Ensure data directory exists
            Config.DATA_DIR.mkdir(exist_ok=True)

            # Written beside the file and moved into place: a crash while writing must not
            # leave a cut-off file (which would load as the defaults)
            temporary = self.SETTINGS_FILE.with_suffix('.json.tmp')
            with self._lock:
                with open(temporary, 'w') as f:
                    json.dump(settings, f, indent=4)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary, self.SETTINGS_FILE)
        except Exception as e:
            print(f"Error saving settings: {e}")

    def get(self, key, default=None):
        """Get a setting value"""
        return self.settings.get(key, default)

    def set(self, key, value):
        """Set a setting value and save"""
        self.settings[key] = value
        self._save_settings(self.settings)

    def get_ai_provider(self):
        """Get saved AI provider"""
        return self.settings.get('ai_provider', 'gemini')

    def get_ai_model(self):
        """Get saved AI model (None means use provider's default)"""
        return self.settings.get('ai_model')

    def set_ai_provider(self, provider, model=None):
        """Save AI provider and model preference"""
        self.settings['ai_provider'] = provider
        self.settings['ai_model'] = model
        self._save_settings(self.settings)
