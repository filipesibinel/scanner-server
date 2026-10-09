#!/usr/bin/env python3
"""
Camera station: runs next to the camera, on any Linux machine. It finds the card, captures it
once it is still and keeps the focus (CardScanner - the same code and thresholds as ever), and
sends each captured card to the scanner server, which reads it, matches it and keeps the
collection. The server's scanner page shows this camera's live view and controls it.

    python station_client.py --server http://192.168.1.20:5000
    (or SCANNER_SERVER / SCANNER_STATION_ID / SCANNER_STATION_NAME / SCANNER_STATION_TOKEN)

Nothing is kept here: camera settings (focus, rotation, fixed area) are saved on the server,
and a captured image is deleted once the server has it. The camera itself is set in
config.yaml (camera.usb_index, resolution). See remote_scanner.py for the messages.
"""

import argparse
import logging
import os
import queue
import re
import socket
import threading
import time
import uuid
from pathlib import Path

import cv2
import requests
import socketio

from config import Config

NAMESPACE = '/station'
STATUS_INTERVAL = 0.2    # seconds between status reports
PREVIEW_INTERVAL = 0.08  # at most ~12 preview frames a second, while the page is watched
# What the server may call and set (remote_scanner.py: METHODS, DECIDED, SENT)
METHODS = ('set_rotation', 'set_refocus_every', 'set_fixed_area', 'set_detection_enabled',
           'set_continuous_autofocus', 'reset_focus', 'detected_area')
SETTABLE = ('fast_scan_mode', 'required_stable_frames', 'auto_capture_delay', 'debug_trace_enabled',
            'auto_capture_enabled', 'card_under_review', 'capture_pending', 'stable_frames')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('station')
# CardScanner's lines are logged here (send_log), not a second time by its own logger
logging.getLogger('scanner').propagate = False
logging.getLogger('scanner').addHandler(logging.NullHandler())


class ServerSettings:
    """The camera's settings as CardScanner uses them (get / set): kept by the server"""

    def __init__(self, values, on_change):
        self.values = dict(values)
        self.on_change = on_change

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value
        self.on_change(key, value)


class Station:
    def __init__(self, server, station_id, name, token):
        self.server = server.rstrip('/')
        self.id = station_id
        self.name = name
        self.token = token
        self.scanner = None
        self.capture_number = 0
        self.preview_wanted = False
        self.uploads = queue.Queue()
        self.sio = socketio.Client(reconnection=True, reconnection_delay=1, reconnection_delay_max=5)
        self.sio.on('connect', self.on_connect, namespace=NAMESPACE)
        self.sio.on('disconnect', self.on_disconnect, namespace=NAMESPACE)
        self.sio.on('command', self.on_command, namespace=NAMESPACE)
        self.sio.on('set', self.on_set, namespace=NAMESPACE)

    # ------------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------------

    def run(self):
        threading.Thread(target=self.upload_loop, daemon=True, name="Uploads").start()
        while True:
            try:
                self.sio.connect(self.server, namespaces=[NAMESPACE], wait_timeout=10,
                                 auth={'id': self.id, 'name': self.name, 'token': self.token})
                break
            except socketio.exceptions.ConnectionError as e:
                logger.warning(f"Server {self.server} not reachable ({e}) - trying again in 3 s")
                time.sleep(3)
        try:
            self.report_loop()
        except KeyboardInterrupt:
            pass
        finally:
            if self.scanner:
                self.scanner.cleanup()
            self.sio.disconnect()

    def connected(self):
        return self.sio.connected and NAMESPACE in self.sio.namespaces

    def emit(self, event, data, callback=None):
        if self.connected():
            try:
                self.sio.emit(event, data, namespace=NAMESPACE, callback=callback)
            except socketio.exceptions.SocketIOError:
                pass  # the connection just went: the next report will find out

    def on_connect(self):
        logger.info(f"Connected to {self.server} as {self.name} ({self.id})")
        # Not from this handler's thread: hello waits for the server's answer
        threading.Thread(target=self.hello, daemon=True).start()

    def on_disconnect(self, *_args):
        logger.warning("Connection to the server lost - scanning goes on, captures wait to be sent")
        self.preview_wanted = False

    def hello(self):
        """Take the camera's settings from the server; the first time, start the scanner with them"""
        try:
            answer = self.sio.call('hello', {}, namespace=NAMESPACE, timeout=10) or {}
        except Exception as e:
            logger.error(f"The server did not answer hello: {e}")
            return
        if self.scanner is None:
            from scanner import CardScanner  # opens the camera
            settings = ServerSettings(answer.get('settings') or {},
                                      lambda key, value: self.emit('setting', {'key': key, 'value': value}))
            scanner = CardScanner(log_callback=self.send_log, settings=settings)
            scanner.fast_scan_mode = True
            scanner.auto_capture_callback = self.auto_captured
            self.apply(scanner, answer.get('set') or {})
            self.scanner = scanner
        else:
            self.apply(self.scanner, answer.get('set') or {})

    @staticmethod
    def apply(scanner, values):
        for name, value in values.items():
            if name in SETTABLE:
                setattr(scanner, name, value)

    def send_log(self, message, level="info"):
        getattr(logger, 'info' if level == 'success' else level, logger.info)(message)
        self.emit('log', {'message': message, 'level': level})

    # ------------------------------------------------------------------------
    # What the server asks for
    # ------------------------------------------------------------------------

    def state(self):
        """What the server shows and decides by (RemoteScanner: REPORTED and SENT names, the camera settings)"""
        s = self.scanner
        return {
            'camera_error': s.camera_error, 'camera_type': s.camera_type,
            'auto_capture_enabled': bool(s.auto_capture_enabled), 'card_under_review': bool(s.card_under_review),
            'capture_pending': bool(s.capture_pending), 'rotation': s.rotation, 'refocus_every': s.refocus_every,
            'fixed_area': s.fixed_area, 'fixed_area_enabled': bool(s.fixed_area_enabled),
            'focus_locked_value': s.focus_locked_value, 'focus_sweep_running': bool(s.focus_sweep_running),
            'focus_probe_running': bool(s.focus_probe_running), 'enable_detection': bool(s.enable_detection),
        }

    def on_set(self, values):
        if self.scanner:
            self.apply(self.scanner, values or {})

    def on_command(self, data):
        """Run a CardScanner method for the server; the answer carries the state after it"""
        if self.scanner is None:
            return {'ok': False, 'error': 'The camera is still starting'}
        method = (data or {}).get('method')
        try:
            if method == 'capture':
                threading.Thread(target=self.manual_capture, daemon=True).start()
                result = True
            elif method in METHODS:
                result = getattr(self.scanner, method)(*data.get('args', []), **data.get('kwargs', {}))
            else:
                return {'ok': False, 'error': f'Unknown command {method}'}
        except (TypeError, ValueError) as e:
            return {'ok': False, 'error': str(e)}
        return {'ok': True, 'result': result, 'state': self.state()}

    def report_loop(self):
        """Status several times a second; the preview while the scanner page is watched"""
        last_status = last_preview = 0.0
        last_frame = -1
        while True:
            time.sleep(0.02)  # the two intervals below are checked 50 times a second
            scanner = self.scanner
            if scanner is None or not self.connected():
                continue  # still starting, or the server is away: scanning goes on without reports
            now = time.time()
            if now - last_status >= STATUS_INTERVAL:
                last_status = now
                self.emit('status', {'state': self.state(), 'detection': scanner.get_detection_status()},
                          callback=self.status_answered)
            if self.preview_wanted and now - last_preview >= PREVIEW_INTERVAL:
                frame_id, jpeg = scanner.get_stream_jpeg()
                if jpeg is not None and frame_id != last_frame:
                    last_frame, last_preview = frame_id, now
                    self.emit('preview', {'jpeg': jpeg})

    def status_answered(self, answer=None):
        self.preview_wanted = bool((answer or {}).get('preview'))

    # ------------------------------------------------------------------------
    # Captures
    # ------------------------------------------------------------------------

    def auto_captured(self):
        """CardScanner's auto-capture callback (its own thread): take the image, beep, send it"""
        scanner = self.scanner
        fast = bool(getattr(scanner, 'fast_scan_mode', True))  # as it is now, not when it is sent
        if not scanner.is_card_detected():
            logger.warning("Auto-capture triggered but no card detected")
            scanner.capture_pending = False
            scanner.card_under_review = False
            return
        self.capture_number += 1
        number = self.capture_number
        image_path, card_image, foil_image = scanner.capture_card_image_only(number, settle=0)
        if not image_path:
            scanner.capture_pending = False
            scanner.card_under_review = False
            return
        # The capture beep - the signal to drop the next card: once the image is taken and a
        # focus probe started by this capture (~1 s, every refocus_every cards) is done
        deadline = time.time() + 2.5
        while scanner.focus_probe_running and time.time() < deadline:
            time.sleep(0.05)
        scanner.capture_pending = False
        self.emit('captured', {'number': number})
        if fast:
            scanner.card_under_review = False  # ready for the next drop; else until Add / Skip on the page
        self.queue_upload(image_path, card_image, foil_image, 'auto' if fast else 'review')

    def manual_capture(self):
        """The page's Capture button: whatever is in view, shown on the page for Add / Skip"""
        scanner = self.scanner
        self.capture_number += 1
        image_path, card_image, foil_image = scanner.capture_card_image_only(self.capture_number)
        if image_path:
            self.queue_upload(image_path, card_image, foil_image, 'review')
        else:
            self.send_log("Failed to capture image", level="error")

    def queue_upload(self, image_path, card_image, foil_image, mode):
        foil_path = None
        if foil_image is not None and foil_image is not card_image:
            # Fixed area: the capture is the area, the foil check uses the outlined card
            foil_path = Path(image_path).with_name(f"{Path(image_path).stem}_foil.jpg")
            cv2.imwrite(str(foil_path), cv2.cvtColor(foil_image, cv2.COLOR_RGB2BGR))
        self.uploads.put({'image': Path(image_path), 'foil': foil_path, 'mode': mode,
                          'foil_is_image': foil_image is card_image, 'capture_id': uuid.uuid4().hex})

    def upload_loop(self):
        """Send captures in order; one that can't be sent is tried again until it is"""
        session = requests.Session()
        headers = {'X-Station-Token': self.token} if self.token else {}
        while True:
            item = self.uploads.get()
            delay = 1.0
            while True:
                try:
                    files = {'image': (item['image'].name, item['image'].read_bytes(), 'image/jpeg')}
                    if item['foil']:
                        files['foil_image'] = (item['foil'].name, item['foil'].read_bytes(), 'image/jpeg')
                    response = session.post(
                        f"{self.server}/api/stations/{self.id}/captures", files=files, headers=headers, timeout=90,
                        # wait 0: the outcome is shown on the station's page, nobody waits for it here.
                        # capture_id: this same capture sent again (no answer came) is still one card
                        data={'name': self.name, 'camera': '1', 'mode': item['mode'], 'wait': '0',
                              'capture_id': item['capture_id'], 'foil_is_image': '1' if item['foil_is_image'] else '0'})
                    if response.status_code < 500:
                        # The server has it - or refuses it for good (a wrong token, an unreadable
                        # picture): sending it again would change nothing
                        if response.status_code >= 400:
                            logger.error(f"The server refused {item['image'].name}: {response.text[:200]}")
                        break
                    logger.warning(f"Server error {response.status_code} for {item['image'].name} - trying again")
                except (requests.RequestException, OSError) as e:
                    logger.warning(f"Could not send {item['image'].name} ({type(e).__name__}) - trying again in {delay:.0f} s")
                time.sleep(delay)
                delay = min(delay * 2, 15.0)
            for path in (item['image'], item['foil']):
                if path:
                    path.unlink(missing_ok=True)


def main():
    # In a container the machine's name is the one mounted from the host (docker-compose.client.yml)
    host_file = Path('/etc/host_hostname')
    host = host_file.read_text().strip() if host_file.is_file() else socket.gethostname()
    host = re.sub(r'[^A-Za-z0-9_-]', '-', host)[:40] or 'station'
    parser = argparse.ArgumentParser(description="Camera station for the card scanner server")
    parser.add_argument('--server', default=os.getenv('SCANNER_SERVER'), help="e.g. http://192.168.1.20:5000")
    parser.add_argument('--id', default=os.getenv('SCANNER_STATION_ID') or host,
                        help="1-40 letters, digits, - or _ (default: this machine's name)")
    parser.add_argument('--name', default=os.getenv('SCANNER_STATION_NAME'), help="shown on the server (default: the id)")
    parser.add_argument('--token', default=os.getenv('SCANNER_STATION_TOKEN') or '',
                        help="the server's station token, if it has one")
    args = parser.parse_args()
    if not args.server:
        parser.error("--server (or SCANNER_SERVER) is needed, e.g. http://192.168.1.20:5000")
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', args.id):
        parser.error("--id must be 1-40 letters, digits, - or _")
    Config.create_directories()
    Station(args.server, args.id, args.name or args.id, args.token).run()


if __name__ == "__main__":
    main()
