# ============================================================================
# FILE: config.py
# Configuration settings for the card scanner
# All settings are loaded from config.yaml
# ============================================================================
import os
from pathlib import Path
from config_loader import get_config_loader

# Load YAML configuration
config = get_config_loader()


class Config:
    """Application configuration - All values loaded from config.yaml"""

    # Paths
    BASE_DIR = Path(__file__).parent
    DATA_DIR = BASE_DIR / 'data'
    IMAGES_DIR = BASE_DIR / 'scanned_cards'
    TEMPLATES_DIR = BASE_DIR / 'templates'
    STATIC_DIR = BASE_DIR / 'static'

    # Database
    DATABASE_FILE = DATA_DIR / config.get('database', 'file', default='cards_database.db')
    DATABASE_UPDATE_AFTER_DAYS = config.get('database', 'update_after_days', default=7)

    # API URLs
    SCRYFALL_BULK_URL = config.get('api', 'scryfall_bulk', default="https://api.scryfall.com/bulk-data/default-cards")

    # Camera settings
    CAMERA_TYPE = config.get('camera', 'type', default='auto')
    USB_CAMERA_INDEX = config.get('camera', 'usb_index', default=0)
    CAMERA_RESOLUTION = tuple(config.get('camera', 'resolution', default=[2560, 1440]))
    CAMERA_PREVIEW_RESOLUTION = tuple(config.get('camera', 'preview_resolution', default=[640, 480]))
    CAMERA_FPS = config.get('camera', 'fps', default=20)
    CAMERA_ROTATE = int(config.get('camera', 'rotate', default=0))

    # Object detection settings
    DETECTION_ALLOW_LANDSCAPE = config.get('detection', 'allow_landscape', default=False)

    # Focus settings

    # Auto-capture settings (enabled state now controlled via UI button)
    AUTO_CAPTURE_DELAY = config.get('auto_capture', 'delay', default=1.0)
    AUTO_CAPTURE_STABILITY_FRAMES = config.get('auto_capture', 'stability_frames', default=5)
    AUTO_CAPTURE_MIN_SHARPNESS = config.get('auto_capture', 'min_sharpness', default=250)
    AUTO_CAPTURE_REFOCUS_EVERY = int(config.get('auto_capture', 'refocus_every', default=10))

    # Fast scan mode settings
    FAST_SCAN_STABILITY_FRAMES = config.get('fast_scan', 'stability_frames', default=6)

    # Flask settings
    SECRET_KEY = config.get('flask', 'secret_key', default='card_scanner_secret_key_change_in_production')
    HOST = config.get('flask', 'host', default='0.0.0.0')
    PORT = config.get('flask', 'port', default=5000)
    DEBUG = config.get('flask', 'debug', default=False)

    # Vision AI settings - Environment variables take precedence
    VISION_AI_PROVIDER = os.getenv('VISION_AI_PROVIDER') or config.get('vision_ai', 'provider', default='gemini')
    VISION_AI_ENABLED = config.get('vision_ai', 'enabled', default=True)
    # Ask the AI about the star (foil) / dot marker on outline-detected captures
    VISION_AI_DETECT_FOIL = config.get('vision_ai', 'detect_foil', default=True)
    VISION_AI_IMAGE_SIZE = int(config.get('vision_ai', 'image_size', default=1024))
    # AI requests running at the same time (identification.py)
    VISION_AI_WORKERS = int(config.get('vision_ai', 'workers', default=3))

    # Stations (stations.py): when set, captures are only accepted with this token
    STATION_TOKEN = os.getenv('SCANNER_STATION_TOKEN') or config.get('stations', 'token', default='') or ''

    # light-ocr reader (card_ocr.py): auto (GPU when available), cpu or webgpu
    OCR_PROVIDER = config.get('ocr', 'provider', default='auto')

    # Local AI settings
    LOCAL_AI_ENDPOINT = os.getenv('LOCAL_AI_ENDPOINT') or config.get('vision_ai', 'local', 'endpoint', default='http://192.168.51.60:11434/v1/chat/completions')
    LOCAL_AI_MODEL = os.getenv('LOCAL_AI_MODEL') or config.get('vision_ai', 'local', 'model', default='llava:13b')

    # Cleanup settings
    CLEANUP_ENABLED = config.get('cleanup', 'enabled', default=True)
    CLEANUP_DAYS = config.get('cleanup', 'days', default=7)

    @staticmethod
    def create_directories():
        """Create required directories if they don't exist"""
        Config.DATA_DIR.mkdir(exist_ok=True)
        Config.IMAGES_DIR.mkdir(exist_ok=True)
