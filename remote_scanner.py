#!/usr/bin/env python3
"""
The scanner page's camera when it is at a station (camera.type: remote): station_client.py
runs the real CardScanner - camera, outline detection, auto-capture, focus - next to the
camera and connects here (Socket.IO namespace /station). RemoteScanner is what app.py holds
in its place: the same attributes and methods, answered from the state the client reports
several times a second, or passed on to the client and waited for.

  client -> server   hello (-> the camera's settings and what the server decides)
                     status (state + detection status; answered with whether the preview is watched)
                     preview (JPEG of the annotated live view, while watched)
                     setting (a camera setting the scanner changed: saved here)
                     log, captured (the capture beep)
  server -> client   command (a CardScanner method, waited for), set (attributes)

The captured cards themselves come in over HTTP (app.py: station_capture).
"""

import hmac
import logging
import threading
import time

from flask import request

from config import Config

logger = logging.getLogger('card_scanner')

NAMESPACE = '/station'

# Camera settings kept in the server's settings.json (the client stores nothing)
CAMERA_SETTINGS = ('focus_value', 'camera_rotation', 'refocus_every', 'fixed_area_enabled', 'fixed_area',
                   'debug_trace')
# CardScanner methods the server may call on the client
METHODS = ('set_rotation', 'set_refocus_every', 'set_fixed_area', 'set_detection_enabled',
           'set_continuous_autofocus', 'reset_focus', 'detected_area', 'capture')
# Attributes the server decides: sent when set, and again to a client that connects
DECIDED = {'fast_scan_mode': True, 'required_stable_frames': Config.FAST_SCAN_STABILITY_FRAMES,
           'auto_capture_delay': Config.AUTO_CAPTURE_DELAY, 'debug_trace_enabled': False}
# Attributes the server sets now and then; otherwise they are the client's own
SENT = {'auto_capture_enabled': False, 'card_under_review': False, 'capture_pending': False, 'stable_frames': 0}
# Read-only state the client reports
REPORTED = {'camera_type': None, 'focus_sweep_running': False, 'focus_probe_running': False,
            'enable_detection': True}
NOT_CONNECTED = "No camera station connected - start station_client.py next to the camera"


class RemoteScanner:
    def __init__(self, socketio, settings, log_callback=None, stations=None, on_captured=None,
                 on_auto_capture_changed=None):
        self.__dict__['_decided'] = dict(DECIDED, debug_trace_enabled=bool(settings.get('debug_trace', False)))
        self.__dict__['_state'] = {}       # what the client last reported
        self.socketio = socketio
        self.settings = settings
        self.log_callback = log_callback
        self.stations = stations
        self.on_captured = on_captured                            # on_captured(number): the capture beep
        self.on_auto_capture_changed = on_auto_capture_changed    # the client's auto-capture went on / off
        self.auto_capture_callback = None                         # captures arrive as uploads instead
        self.sid = None       # the connected client's Socket.IO session
        self.station = None   # {'id', 'name'} of the connected client
        self._detection = {}
        self._preview = (-1, None)
        self._watched = 0.0   # when the preview was last asked for
        self._lock = threading.Lock()
        self._register()

    # ------------------------------------------------------------------------
    # Attributes: as CardScanner's
    # ------------------------------------------------------------------------

    def __setattr__(self, name, value):
        if name in DECIDED or name in SENT:
            if name in DECIDED:
                self._decided[name] = value
            else:
                self._state[name] = value
            self._emit('set', {name: value})
        else:
            object.__setattr__(self, name, value)

    def __getattr__(self, name):  # only what isn't a real attribute
        if name in DECIDED:
            return self._decided[name]
        if name in SENT:
            return self._state.get(name, SENT[name])
        if name in REPORTED:
            return self._state.get(name, REPORTED[name])
        raise AttributeError(name)

    @property
    def connected(self):
        return self.sid is not None

    @property
    def camera_error(self):
        return self._state.get('camera_error') if self.connected else NOT_CONNECTED

    # With no client connected these come from the saved settings, so the page shows them
    @property
    def rotation(self):
        return self._state.get('rotation', int(self.settings.get('camera_rotation', Config.CAMERA_ROTATE) or 0))

    @property
    def refocus_every(self):
        return self._state.get('refocus_every',
                               int(self.settings.get('refocus_every', Config.AUTO_CAPTURE_REFOCUS_EVERY) or 0))

    @property
    def fixed_area(self):
        return self._state.get('fixed_area', self.settings.get('fixed_area'))

    @property
    def fixed_area_enabled(self):
        return self._state.get('fixed_area_enabled', bool(self.settings.get('fixed_area_enabled', False)))

    @property
    def focus_locked_value(self):
        return self._state.get('focus_locked_value', self.settings.get('focus_value'))

    # ------------------------------------------------------------------------
    # Methods: as CardScanner's
    # ------------------------------------------------------------------------

    def log(self, message, level="info"):
        getattr(logger, 'info' if level == 'success' else level, logger.info)(message)
        if self.log_callback:
            self.log_callback(message, level)

    def _emit(self, event, data):
        sid = self.sid
        if sid:
            self.socketio.emit(event, data, to=sid, namespace=NAMESPACE)

    def _call(self, method, *args, timeout=6, **kwargs):
        """Run a CardScanner method on the client and return its result (ValueError as there)"""
        sid = self.sid
        if not sid:
            raise ValueError("No camera connected")
        try:
            answer = self.socketio.call('command', {'method': method, 'args': list(args), 'kwargs': kwargs},
                                        to=sid, namespace=NAMESPACE, timeout=timeout)
        except Exception as e:  # timed out, or the client went away
            raise ValueError(f"The camera station did not answer ({type(e).__name__})") from e
        if not answer or not answer.get('ok'):
            raise ValueError((answer or {}).get('error') or 'The camera station could not do that')
        self._take_state(answer.get('state'))
        return answer.get('result')

    def set_rotation(self, degrees):
        return self._call('set_rotation', degrees)

    def set_refocus_every(self, captures):
        return self._call('set_refocus_every', captures)

    def set_fixed_area(self, enabled=None, area=None):
        return self._call('set_fixed_area', enabled=enabled, area=area)

    def detected_area(self):
        return self._call('detected_area') if self.connected else None

    def set_detection_enabled(self, enabled):
        if self.connected:
            self._call('set_detection_enabled', bool(enabled))

    def _focus(self, method, *args):
        try:
            return bool(self._call(method, *args))
        except ValueError:
            return False

    def set_continuous_autofocus(self, enabled):
        return self._focus('set_continuous_autofocus', bool(enabled))

    def reset_focus(self):
        return self._focus('reset_focus')

    def request_capture(self):
        """Manual capture: the client takes the picture and uploads it for review"""
        self._call('capture')

    def is_card_detected(self):
        return bool(self._detection.get('detected'))

    def get_detection_status(self):
        status = {'detected': False, 'stable_frames': 0, 'required_frames': self.required_stable_frames,
                  'is_stable': False, 'awaiting_new_card': False, 'in_focus': False, 'focusing': False,
                  'capturing': False}
        if self.connected:
            status.update(self._detection)
        status['camera_error'] = self.camera_error
        return status

    def get_stream_jpeg(self, max_height=720):
        """(frame id, JPEG) of the client's latest preview; asking keeps the preview coming"""
        self._watched = time.time()
        return self._preview

    def cleanup(self):
        pass

    # ------------------------------------------------------------------------
    # The client's side of the conversation
    # ------------------------------------------------------------------------

    def _take_state(self, state):
        if not state:
            return
        before = self._state.get('auto_capture_enabled')
        self._state.update(state)
        after = self._state.get('auto_capture_enabled')
        if before is not None and after != before and self.on_auto_capture_changed:
            self.on_auto_capture_changed(bool(after))

    def _is_client(self):
        return request.sid == self.sid

    def _register(self):
        socketio = self.socketio

        @socketio.on('connect', namespace=NAMESPACE)
        def station_connect(auth=None):
            auth = auth or {}
            if Config.STATION_TOKEN and not hmac.compare_digest(str(auth.get('token') or ''), Config.STATION_TOKEN):
                logger.warning("A camera station was refused: wrong or missing station token")
                return False
            station_id = str(auth.get('id') or '')
            if self.stations and not self.stations.valid_id(station_id):
                return False
            with self._lock:
                replaced = self.station if self.sid else None
                self.sid = request.sid
                self.station = (self.stations.seen(station_id, auth.get('name')) if self.stations
                                else {'id': station_id, 'name': auth.get('name') or station_id})
                self._state.clear()
                self._detection = {}
                self._preview = (-1, None)
            if replaced:
                self.log(f"Camera station {replaced['name']} replaced by {self.station['name']}", level="warning")
            self.log(f"Camera station connected: {self.station['name']}", level="success")

        @socketio.on('disconnect', namespace=NAMESPACE)
        def station_disconnect(*_args):
            if not self._is_client():
                return
            with self._lock:
                name = self.station['name'] if self.station else '?'
                self.sid = None
                self._state.clear()
                self._detection = {}
                self._preview = (-1, None)
            self.log(f"Camera station disconnected: {name}", level="warning")
            if self.on_auto_capture_changed:
                self.on_auto_capture_changed(False)

        @socketio.on('hello', namespace=NAMESPACE)
        def station_hello(_data=None):
            """The camera's saved settings, and the attributes the server decides"""
            if not self._is_client():
                return None
            return {'settings': {key: self.settings.get(key) for key in CAMERA_SETTINGS
                                 if self.settings.get(key) is not None},
                    'set': dict(self._decided)}

        @socketio.on('status', namespace=NAMESPACE)
        def station_status(data):
            if not self._is_client():
                return None
            self._detection = data.get('detection') or {}
            self._take_state(data.get('state'))
            return {'preview': time.time() - self._watched < 2.0}

        @socketio.on('preview', namespace=NAMESPACE)
        def station_preview(data):
            if self._is_client() and isinstance(data.get('jpeg'), (bytes, bytearray)):
                self._preview = (self._preview[0] + 1 if self._preview[0] >= 0 else 0, bytes(data['jpeg']))

        @socketio.on('setting', namespace=NAMESPACE)
        def station_setting(data):
            if self._is_client() and data.get('key') in CAMERA_SETTINGS:
                self.settings.set(data['key'], data.get('value'))

        @socketio.on('log', namespace=NAMESPACE)
        def station_log(data):
            if self._is_client():
                self.log(str(data.get('message', '')), str(data.get('level', 'info')))

        @socketio.on('captured', namespace=NAMESPACE)
        def station_captured(data):
            if self._is_client() and self.on_captured:
                self.on_captured(data.get('number'))
