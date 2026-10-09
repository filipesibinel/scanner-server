#!/usr/bin/env python3
"""
Scanner Module - Updated with AI-based Object Detection
Camera and card scanning logic with real-time object detection.
"""

import cv2
import numpy as np
import os
import sys
import time
import threading
import subprocess
import shutil
from collections import deque
import logging
import re
from datetime import datetime
from config import Config
from object_detector import ObjectDetector, warp_card
from settings import Settings

# Create scanner logger
logger = logging.getLogger('scanner')

# OpenCV spreads each small operation over all CPU cores and its idle workers spin-wait:
# with 8 threads the capture loop used 121% of a core, with 2 it uses 59% at the same speed
cv2.setNumThreads(2)

# A camera that is not there (not plugged in, taken by another program, unplugged while
# running) is said once and then looked for quietly - the rest of the app needs no camera
CAMERA_RETRY_SECONDS = 3
CAMERA_LOST_AFTER = 20  # failed reads in a row (~2 s) before the camera counts as gone


def focus_sweep(set_focus, measure_sharpness, low, high, coarse_step=50, fine_step=10, settle=0.2):
    """
    Find the sharpest manual focus position: a coarse pass over the whole range, then a
    fine pass around the best coarse position.

    Args:
        set_focus: callable(position) that moves the lens
        measure_sharpness: callable() -> sharpness of the current image
        low, high: focus range of the camera
        settle: seconds to wait after moving the lens before measuring

    Returns:
        tuple: (best_position, best_sharpness, {position: sharpness})
    """
    scores = {}

    def score(position):
        set_focus(position)
        time.sleep(settle)
        scores[position] = measure_sharpness()

    for position in list(range(low, high + 1, coarse_step)) + [high]:
        if position not in scores:
            score(position)
    best = max(scores, key=scores.get)
    for position in range(max(low, best - coarse_step + fine_step), min(high, best + coarse_step - fine_step) + 1, fine_step):
        if position not in scores:
            score(position)
    best = max(scores, key=scores.get)

    # The sharpness curve is smooth around its peak: a parabola through the best position
    # and its neighbours predicts the peak between the fine steps - measure it, keep the better
    left, right = scores.get(best - fine_step), scores.get(best + fine_step)
    if left is not None and right is not None:
        curvature = left - 2 * scores[best] + right
        if curvature < 0:
            offset = fine_step * (left - right) / (2 * curvature)
            peak = int(round(best + max(-fine_step, min(fine_step, offset))))
            if peak not in scores and low <= peak <= high:
                score(peak)
                best = max(scores, key=scores.get)
    return best, scores[best], scores


class CardScanner:
    """Handles camera operations and card scanning"""
    
    def __init__(self, log_callback=None, settings=None):
        self.log_callback = log_callback
        self.camera = None
        self.camera_type = None
        self.camera_error = None  # Why there is no camera image (shown on the scanner page), or None
        self.failed_reads = 0  # Frames in a row that could not be read
        self.current_frame = None  # Live frame (RGB) - half resolution with a raw-JPEG camera
        self.current_raw = None  # The camera's JPEG of current_frame (full-resolution source), or None
        self.raw_mjpeg = False  # Camera delivers raw JPEG: decode at half size live, full size on capture
        self.half_size_decode = False
        self.next_retrieve = 0.0  # When the next raw frame is due for decoding (CAMERA_FPS pacing)
        self.frame_id = 0  # Increments with every processed frame (stream skips repeats)
        self._stream_cache = (-1, None)
        self.annotated_frame = None  # Frame with card detection overlay
        self.detected_card = None  # (frame, bbox, corners) of the detected card - see get_detected_card()
        self.detected_card_name = "" # Name of the detected card
        self.card_detected = False
        self.frame_lock = threading.Lock()
        self.capture_thread = None
        self.running = False
        self.object_detector = ObjectDetector(allow_landscape=Config.DETECTION_ALLOW_LANDSCAPE)

        # Frame stability tracking (for auto-capture)
        self.stable_frames = 0
        self.required_stable_frames = 5  # Require 5 stable frames before capture
        self.frames_since_card_lost = 0  # Track frames without card detection
        self.previous_card_points = None  # Card corners in the previous frame (stillness check)
        self.tracked_outline = None  # Corners of the last outline detection, followed by the detector
        self.previous_sharpness = None  # Card sharpness in the previous frame (autofocus check)
        self.card_in_focus = False  # Card sharpness above auto_capture.min_sharpness
        self.previous_thumbnail = None  # Tiny normalized card image of the previous frame
        self.captured_thumbnail = None  # Tiny normalized card image at the last capture
        self.card_disturbed = False  # Latest frame showed a jump (a card being dropped, a hand)
        self.missing_frames = 0  # Consecutive frames without a real detection
        # Cards are dropped onto a stack, so the view never empties: after a capture,
        # auto-capture waits for the next drop (see _new_card_arrived) so the same card
        # is never captured twice
        self.awaiting_new_card = False

        # User settings
        self.settings = settings or Settings()

        # Detection settings
        self.enable_detection = True  # Toggle auto-detection
        self.debug_trace_enabled = bool(self.settings.get('debug_trace', False))  # Toggle debug trace logging
        # Debug trace also records problem moments: the last 3 s of frames (640 px, what the
        # outline detector works on) are saved to data/debug_frames/ when a card waits > 2 s
        # or a "new card" shows up < 2 s after a capture
        self.debug_frames = deque(maxlen=60)
        self.debug_dump = None  # (folder, frames still to save after the trigger)

        # Card size detection - keep box visible longer for card-sized objects
        self.last_card_detection = None  # Store last valid card detection (bbox, time)
        self.card_display_duration = 6.0  # Keep detection box visible for 6 seconds (increased from 3.0 for stability)
        # Frames without a card that always count as a change of card (the detector can
        # miss 1-2 frames of a card lying still; shorter gaps are judged by where the
        # card reappears, see _new_card_arrived)
        self.missing_frames_for_new_card = 6
        self.log(f"Debug trace: {'on' if self.debug_trace_enabled else 'off'} (Settings; frames of slow or doubtful captures go to data/debug_frames)", level="info")

        # Bounding box smoothing to eliminate flicker
        self.smoothed_bbox = None  # Smoothed bounding box coordinates
        self.bbox_smoothing_alpha = 0.3  # Smoothing factor (0.3 = 30% new, 70% old)
        self.bbox_movement_threshold = 5  # Minimum pixel movement to update (reduces jitter)

        # Focus: continuous autofocus, or a manual focus found by a sweep and locked
        # (the camera-to-card distance is fixed, and continuous autofocus can hunt and
        # settle on a blurry position when cards are dropped quickly)
        self.focus_range = None  # (min, max) of the camera's focus_absolute control
        self.focus_locked_value = self.settings.get('focus_value')  # None = continuous autofocus
        self.focus_sweep_running = False
        self.last_focus_sweep = 0.0
        self.out_of_focus_since = None
        self.captures_since_focus = 0  # Captures since the last focus sweep / probe (refocus_every)
        # Probe the focus every this many captures (0 = never): Settings, else config.yaml
        self.refocus_every = int(self.settings.get('refocus_every', Config.AUTO_CAPTURE_REFOCUS_EVERY) or 0)
        self.focus_probe_running = False  # Short focus probe between two drops
        self.focus_probe_direction = 1  # Probe up (+1) or down (-1) next
        self.disturbed_during_focus = False  # A card was dropped while the focus was moving
        self.last_focus_move = 0.0  # When the last sweep / probe ended

        # Auto-capture settings (enabled state controlled via UI button)
        self.auto_capture_enabled = False  # Disabled by default, enabled via UI button
        self.auto_capture_delay = Config.AUTO_CAPTURE_DELAY
        self.last_auto_capture_time = 0
        self.auto_capture_callback = None
        self.card_under_review = False  # Prevent auto-capture while card is being reviewed
        self.capture_pending = False  # Auto-capture triggered, image / focus probe not done yet
        # Image rotation (degrees clockwise), applied as each frame is decoded - live and full size
        self.rotation = int(self.settings.get('camera_rotation', Config.CAMERA_ROTATE) or 0)

        # Fixed capture area (sleeved cards): stillness and new cards are judged by how the image
        # inside a drawn area changes, not by the card's outline (on a sleeved pile the outline
        # flips between the top card and the pile). Area = (x1, y1, x2, y2) as fractions of the frame
        self.fixed_area_enabled = bool(self.settings.get('fixed_area_enabled', False))
        self.fixed_area = self.settings.get('fixed_area')
        self.area_thumb = None  # This frame's area thumbnail (brightness removed)
        self.area_previous = None
        self.area_anchor = None  # Thumbnail when the current still streak began
        self.area_captured = None  # Thumbnail of the last captured card
        self.area_metrics = (0.0, 0.0, 0.0)  # (change since previous frame, drift, sharpness)
        self.area_big_changes = 0  # Frames in a row with a change > AREA_DROP

        self.initialize_camera()
    
    def log(self, message, level="info"):
        """Send log message to both file logger and UI callback"""
        # Log to file
        log_method = getattr(logger, level, logger.info)
        log_method(message)

        # Send to UI callback if provided
        if self.log_callback:
            self.log_callback(message, level)

    def smooth_bounding_box(self, new_bbox):
        """
        Apply exponential moving average smoothing to bounding box coordinates
        This eliminates flickering when card is physically still

        Args:
            new_bbox: Tuple of (x1, y1, x2, y2) of the detected card

        Returns:
            Smoothed bounding box tuple (x1, y1, x2, y2)
        """
        if new_bbox is None:
            # No detection - clear smoothed box after a delay
            return self.smoothed_bbox

        x1_new, y1_new, x2_new, y2_new = new_bbox

        # Initialize smoothed box on first detection
        if self.smoothed_bbox is None:
            self.smoothed_bbox = new_bbox
            return new_bbox

        x1_old, y1_old, x2_old, y2_old = self.smoothed_bbox

        # Calculate movement distance (center point)
        center_x_new = (x1_new + x2_new) / 2
        center_y_new = (y1_new + y2_new) / 2
        center_x_old = (x1_old + x2_old) / 2
        center_y_old = (y1_old + y2_old) / 2

        movement = ((center_x_new - center_x_old)**2 + (center_y_new - center_y_old)**2)**0.5

        # If movement is very small (below threshold), keep old box (reduces jitter)
        if movement < self.bbox_movement_threshold:
            return self.smoothed_bbox

        # Apply exponential moving average
        # alpha = smoothing factor (0.3 = 30% new, 70% old)
        alpha = self.bbox_smoothing_alpha

        x1_smooth = int(alpha * x1_new + (1 - alpha) * x1_old)
        y1_smooth = int(alpha * y1_new + (1 - alpha) * y1_old)
        x2_smooth = int(alpha * x2_new + (1 - alpha) * x2_old)
        y2_smooth = int(alpha * y2_new + (1 - alpha) * y2_old)

        self.smoothed_bbox = (x1_smooth, y1_smooth, x2_smooth, y2_smooth)
        return self.smoothed_bbox

    def _is_card_settled(self, frame, points):
        """
        True if the card is still and in focus: its corners moved less than ~1% of the
        card size since the previous frame and since the still streak began, its sharpness
        changed by less than 20%
        (autofocus still adjusting changes it a lot between frames), and the image is
        sharp enough to read (a camera that hasn't focused yet is steady but blurry).
        """
        points = np.asarray(points, dtype=np.float32)
        x1, y1 = np.maximum(points.min(axis=0).astype(int), 0)
        x2, y2 = points.max(axis=0).astype(int)
        region = frame[y1:y2, x1:x2]
        if region.size == 0:
            return False
        small = cv2.resize(region, (160, max(1, int(160 * region.shape[0] / region.shape[1]))), interpolation=cv2.INTER_AREA)
        sharpness = cv2.Laplacian(cv2.cvtColor(small, cv2.COLOR_RGB2GRAY), cv2.CV_64F).var()

        self.card_in_focus = bool(sharpness >= Config.AUTO_CAPTURE_MIN_SHARPNESS)  # plain bool: sent as JSON
        thumbnail = self._card_thumbnail(frame, points)

        previous_points, previous_sharpness = self.previous_card_points, self.previous_sharpness
        previous_thumbnail = self.previous_thumbnail
        self.previous_card_points, self.previous_sharpness, self.previous_thumbnail = points, sharpness, thumbnail
        if previous_points is None or previous_sharpness is None:
            self.card_disturbed = True
            return False

        card_size = np.linalg.norm(points[2] - points[0])
        movement = np.linalg.norm(points - previous_points, axis=1).max() / card_size
        # Drift since the still streak began: a sleeved card sliding slowly moves less than 1%
        # per frame, passes the frame-to-frame test and used to be captured mid-slide (blurred,
        # then captured again once it stopped)
        if self.stable_frames == 0 or getattr(self, 'settle_anchor', None) is None:
            self.settle_anchor = points
        drift = np.linalg.norm(points - self.settle_anchor, axis=1).max() / card_size
        sharpness_change = abs(sharpness - previous_sharpness) / max(previous_sharpness, 1e-6)
        image_change = self._thumbnail_difference(thumbnail, previous_thumbnail)

        # A drop (or a hand) makes the card jump or change far beyond camera noise
        # (measured on a card lying still: movement <= 0.4%, image change <= 0.07)
        self.card_disturbed = movement > 0.03 or image_change > 0.3
        self.last_frame_change = (movement, image_change)
        self.settle_metrics = (movement, drift, sharpness_change, sharpness)
        settled = movement < 0.01 and drift < 0.01 and sharpness_change < 0.2 and self.card_in_focus
        if not settled:
            self.settle_anchor = points
        return settled

    @staticmethod
    def _card_thumbnail(frame, points):
        """Tiny, brightness-normalized, perspective-corrected card image for comparisons"""
        card = warp_card(frame, points, out_h=180)
        gray = cv2.cvtColor(cv2.resize(card, (32, 45), interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY).astype(np.float32)
        return (gray - gray.mean()) / (gray.std() + 1e-6)

    @staticmethod
    def _thumbnail_difference(a, b):
        """Mean difference between two card thumbnails (0 = identical)"""
        if a is None or b is None or a.shape != b.shape:
            return 0.0
        return float(np.abs(a - b).mean())

    def _new_card_arrived(self, gap):
        """
        After a capture: has the next card been dropped onto the pile?
        - the card jumped or its image changed sharply (the drop itself, a hand), or
        - it reappears after a short gap in a different spot: a falling card usually
          can't be detected for a few frames, and a dropped card never lands exactly
          where the previous one lay (a detector hiccup leaves it within ~0.3%), or
        - the card on the pile looks different from the captured one.
        Identical copies are caught by the drop, not by their looks.
        gap: frames without a detection just before this one
        """
        self.new_card_gap_only = False
        if self.card_disturbed:
            return True
        if self._thumbnail_difference(self.previous_thumbnail, self.captured_thumbnail) > 0.3:
            return True
        movement, _ = getattr(self, 'last_frame_change', (0.0, 0.0))
        if gap >= 1 and movement > 0.008:
            # Only this weak sign: the outline flickered and came back a little shifted. The
            # capture gate checks it doesn't settle as the very same image (see _outline_flicker)
            self.new_card_gap_only = True
            return True
        return False

    def _trace_waiting(self, detected):
        """
        Log file only: while auto scanning waits for a card to become ready for more than 2 s,
        say why once a second - not detected, or which stillness / sharpness test fails
        (limits: movement 1%, drift 1%, sharpness change 20%, sharpness min_sharpness)
        """
        waiting = (self.auto_capture_enabled and not self.awaiting_new_card and not self.card_under_review
                   and not self.capture_pending and not self._focus_moving()
                   and self.stable_frames < self.required_stable_frames)
        now = time.time()
        if not waiting:
            self.waiting_since = None
            return
        if getattr(self, 'waiting_since', None) is None:
            self.waiting_since, self.last_wait_log, self.wait_dumped = now, now, False
            return
        if now - self.waiting_since >= 2.0 and not self.wait_dumped:
            self.wait_dumped = True
            self._start_debug_dump('waiting')
        if now - self.waiting_since < 2.0 or now - self.last_wait_log < (1.0 if detected else 5.0):
            return
        self.last_wait_log = now
        if self._fixed_area_active():
            change, drift, sharpness = self.area_metrics
            logger.info(f"Waiting {now - self.waiting_since:.0f} s: fixed area not ready ({self.stable_frames}/{self.required_stable_frames}) - "
                        f"change {change:.1f} (still < {self.AREA_STILL}), drift {drift:.1f} (< {self.AREA_DRIFT}), "
                        f"sharpness {sharpness:.0f} (card >= {Config.AUTO_CAPTURE_MIN_SHARPNESS})")
            return
        if not detected:
            logger.info(f"Waiting {now - self.waiting_since:.0f} s: no card outline found")
            return
        movement, drift, change, sharpness = getattr(self, 'settle_metrics', (0, 0, 0, 0))
        logger.info(f"Waiting {now - self.waiting_since:.0f} s: card not ready ({self.stable_frames}/{self.required_stable_frames}) - "
                    f"movement {movement:.1%}, drift {drift:.1%}, sharpness change {change:.0%}, "
                    f"sharpness {sharpness:.0f} (min {Config.AUTO_CAPTURE_MIN_SHARPNESS})")

    def _record_debug_frame(self, frame):
        """Keep the frame (640 px wide, as the outline detector sees it); continue a dump"""
        small = cv2.resize(frame, (640, int(640 * frame.shape[0] / frame.shape[1])), interpolation=cv2.INTER_AREA)
        self.debug_frames.append((time.time(), small, self.stable_frames))
        if self.debug_dump:
            folder, remaining = self.debug_dump
            self._save_debug_frame(folder, self.debug_frames[-1])
            self.debug_dump = (folder, remaining - 1) if remaining > 1 else None

    def _start_debug_dump(self, reason, after=20, keep=20):
        """Save the buffered frames and the next `after` ones; keep the newest `keep` dumps"""
        if not self.debug_trace_enabled or self.debug_dump:
            return
        root = Config.DATA_DIR / 'debug_frames'
        folder = root / f"{time.strftime('%Y%m%d_%H%M%S')}_{reason}"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            for item in list(self.debug_frames):
                self._save_debug_frame(folder, item)
            self.debug_dump = (folder, after)
            for old in sorted(p for p in root.iterdir() if p.is_dir())[:-keep]:
                shutil.rmtree(old, ignore_errors=True)
            logger.info(f"Debug frames saved to {folder}")
        except OSError as e:
            logger.warning(f"Could not save debug frames: {e}")

    @staticmethod
    def _save_debug_frame(folder, item):
        stamp, small, stable = item
        cv2.imwrite(str(folder / f"{stamp:.3f}_s{stable}.jpg"), cv2.cvtColor(small, cv2.COLOR_RGB2BGR),
                    [cv2.IMWRITE_JPEG_QUALITY, 95])

    def _outline_flicker(self):
        """
        A "new card" seen only because the outline vanished for a frame and came back shifted
        (a hand approaching), which settled as the very image just captured: not a new card.
        In a 67-card lot a card was captured twice this way (thumbnail difference 0.02, while
        real copies of a card differed 0.07-0.50 and came with bigger signs).
        """
        same = getattr(self, 'new_card_gap_only', False) and \
            self._thumbnail_difference(self.previous_thumbnail, self.captured_thumbnail) < 0.05
        self.new_card_gap_only = False
        return same

    def _rebaseline_after_focus(self, focus_moving):
        """
        When the lens has finished moving (probe or sweep) and no drop was seen meanwhile, the
        card in view is still the captured one: take it as the new reference. After a probe a
        foil's glare and the outline can change enough to look like another card (a foil Part
        in Friendship was captured twice this way).
        """
        if getattr(self, 'was_focus_moving', False) and not focus_moving and self.awaiting_new_card \
                and not self.disturbed_during_focus:
            self.captured_thumbnail = self.previous_thumbnail
            # (in fixed-area mode only if the area still looks like the capture - a card that
            # landed unseen during the probe differs ~21 and must stay new)
            if self._area_difference(self.area_thumb, self.area_captured) <= self.AREA_DIFFERENT:
                self.area_captured = self.area_thumb
        self.was_focus_moving = focus_moving

    def _focus_moving(self):
        """A focus sweep or probe is running, or ended less than 0.6 s ago"""
        return self.focus_sweep_running or self.focus_probe_running or time.time() - self.last_focus_move < 0.6

    def _check_focus_drift(self):
        """
        With a locked focus, refocus automatically when a still card stays blurry for
        3 s (the pile grows towards the camera as cards are added).
        """
        if self.focus_locked_value is None or self.focus_sweep_running or self.focus_probe_running:
            return
        movement, _ = getattr(self, 'last_frame_change', (1.0, 0.0))
        if self.card_in_focus or movement >= 0.01:
            self.out_of_focus_since = None
            return
        now = time.time()
        if self.out_of_focus_since is None:
            self.out_of_focus_since = now
        elif now - self.out_of_focus_since > 3.0 and now - self.last_focus_sweep > 15.0:
            self.refocus("card out of focus")

    def _trigger_auto_capture(self):
        """Start an auto-capture (the callback takes the image and identifies the card)"""
        # Double-check auto_capture_enabled before triggering (avoid race condition)
        if self.auto_capture_enabled and self.auto_capture_callback:
            self.log(f"Auto-capturing card (stable={self.stable_frames}/{self.required_stable_frames})", level="info")
            self.last_auto_capture_time = time.time()
            self.card_under_review = True  # Set flag to prevent further auto-captures
            self._mark_captured()  # Next auto-capture needs a new card
            self.capture_pending = True  # Until the image is taken (app.announce_capture)
            threading.Thread(target=self.auto_capture_callback).start()

    # ------------------------------------------------------------------------
    # Fixed capture area
    # ------------------------------------------------------------------------

    # Mean difference of the area's 48x64 thumbnail (brightness removed), measured on recorded
    # sleeved piles: still card <= 2.4 frame to frame (<= 5.4 over 1 s with the light changing),
    # a hand's shadow <= 0.6, a card falling in 10-38 over several frames in a row, a different
    # card settled ~21. A sleeved card settling after its capture jumped 13 in a single frame -
    # so a drop needs AREA_DROP in 2 frames in a row.
    AREA_STILL = 3.0
    AREA_DRIFT = 4.0
    AREA_DROP = 8.0
    AREA_DIFFERENT = 10.0

    ROTATIONS = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}

    def _rotate(self, image):
        code = self.ROTATIONS.get(self.rotation)
        return image if code is None or image is None else cv2.rotate(image, code)

    def set_rotation(self, degrees):
        """
        Rotate the camera image (0/90/180/270 clockwise). Detection starts afresh; a fixed area
        drawn for the old orientation is switched off (it has to be drawn again).
        """
        degrees = int(degrees) % 360
        if degrees not in (0, 90, 180, 270):
            raise ValueError("Rotation must be 0, 90, 180 or 270")
        self.rotation = degrees
        self.settings.set('camera_rotation', degrees)
        with self.frame_lock:
            self.detected_card = None
            self.card_detected = False
        self.tracked_outline = self.previous_card_points = self.previous_sharpness = None
        self.smoothed_bbox = self.last_card_detection = None
        self.stable_frames = 0
        fixed_area_off = self.fixed_area_enabled
        if fixed_area_off:
            self.set_fixed_area(enabled=False)
        self.log(f"Camera image rotated {degrees}°")
        return fixed_area_off

    def _fixed_area_active(self):
        return self.fixed_area_enabled and self.fixed_area is not None and self.enable_detection

    def set_fixed_area(self, enabled=None, area=None):
        """Turn the fixed area on/off and/or set it (fractions of the frame); saved in settings"""
        if area is not None:
            self.fixed_area = [float(v) for v in area]
            self.settings.set('fixed_area', self.fixed_area)
        if enabled is not None:
            self.fixed_area_enabled = bool(enabled)
            self.settings.set('fixed_area_enabled', self.fixed_area_enabled)
        # Start the area's judgement afresh; a card already captured stays captured
        self.area_previous = self.area_anchor = None
        self.area_captured = self.area_thumb if self.awaiting_new_card else None
        self.stable_frames = 0
        return {'enabled': self.fixed_area_enabled, 'area': self.fixed_area}

    def detected_area(self, margin=0.05):
        """The detected card's bounding box plus a margin, as fractions of the frame, or None"""
        with self.frame_lock:
            detected, frame = self.detected_card, self.current_frame
        if detected is None or frame is None:
            return None
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = detected[1]
        dx, dy = (x2 - x1) * margin, (y2 - y1) * margin
        return [max(0.0, (x1 - dx) / width), max(0.0, (y1 - dy) / height),
                min(1.0, (x2 + dx) / width), min(1.0, (y2 + dy) / height)]

    @staticmethod
    def _area_thumbnail(region):
        gray = cv2.cvtColor(cv2.resize(region, (48, 64), interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY).astype(np.float32)
        return gray - gray.mean()  # a uniform brightness change (e.g. a shadow) is not a change

    @staticmethod
    def _area_difference(a, b):
        return float(np.abs(a - b).mean()) if a is not None and b is not None else 0.0

    def _fixed_area_step(self, frame, raw, annotated):
        """
        One frame in fixed-area mode: a card is present when the area is sharp (an empty box
        measured ~30 against >= 250 for a card), still when its image hardly changes (frame to
        frame and since the still streak began - a sliding card drifts), and new after a
        capture when the area changed like a card falling in, or settled looking different.
        The photo is the area as drawn. The outline detector still runs: an outline inside the
        area gives the flat card for the ★/• foil check (get_detected_card).
        """
        height, width = frame.shape[:2]
        fx1, fy1, fx2, fy2 = self.fixed_area
        x1, y1 = int(fx1 * width), int(fy1 * height)
        x2, y2 = max(x1 + 8, int(fx2 * width)), max(y1 + 8, int(fy2 * height))
        region = frame[y1:y2, x1:x2]
        small = cv2.resize(region, (160, max(1, int(160 * region.shape[0] / region.shape[1]))), interpolation=cv2.INTER_AREA)
        sharpness = cv2.Laplacian(cv2.cvtColor(small, cv2.COLOR_RGB2GRAY), cv2.CV_64F).var()
        present = bool(sharpness >= Config.AUTO_CAPTURE_MIN_SHARPNESS)

        self.area_thumb = thumb = self._area_thumbnail(region)
        change = self._area_difference(thumb, self.area_previous) if self.area_previous is not None else 0.0
        self.area_previous = thumb
        if self.stable_frames == 0 or self.area_anchor is None:
            self.area_anchor = thumb
        drift = self._area_difference(thumb, self.area_anchor)
        self.area_metrics = (change, drift, sharpness)

        focus_moving = self._focus_moving()
        settled = present and not focus_moving and change < self.AREA_STILL and drift < self.AREA_DRIFT
        self.stable_frames = min(self.stable_frames + 1, self.required_stable_frames) if settled else 0
        if not settled:
            self.area_anchor = thumb

        self.area_big_changes = self.area_big_changes + 1 if change > self.AREA_DROP else 0
        # A card falling in while a focus probe moves the lens (only slightly blurred): it counts
        # as new once the focus is done (and the probe's measurement is discarded)
        if focus_moving and not self.focus_sweep_running and self.area_big_changes >= 2:
            self.disturbed_during_focus = True
        self._rebaseline_after_focus(focus_moving)
        if self.awaiting_new_card and not focus_moving and self.disturbed_during_focus:
            self.disturbed_during_focus = False
            self.awaiting_new_card = False
            self.stable_frames = 0
            if self.auto_capture_enabled:
                self.log("New card detected (dropped while focusing)")
        elif self.awaiting_new_card and not focus_moving:
            different = self.area_captured is not None and \
                self._area_difference(thumb, self.area_captured) > self.AREA_DIFFERENT
            if self.area_big_changes >= 2 or (settled and different):
                self.awaiting_new_card = False
                self.stable_frames = 0
                if self.auto_capture_enabled:
                    self.log(f"New card detected (area change {change:.1f})")

        # Outline inside the area -> flat, tight crop for the photo
        _, _, _, corners = self.object_detector.detect(
            frame, previous=self.tracked_outline if self.missing_frames <= 2 else None)
        if corners is not None:
            self.tracked_outline, self.missing_frames = corners, 0
            cx1, cy1 = corners.min(axis=0)
            cx2, cy2 = corners.max(axis=0)
            if cx1 < x1 - 0.03 * width or cy1 < y1 - 0.03 * height or cx2 > x2 + 0.03 * width or cy2 > y2 + 0.03 * height:
                corners = None  # an outline outside the area (e.g. the whole pile)
        else:
            self.missing_frames += 1
        with self.frame_lock:
            self.card_detected = present
            self.card_in_focus = present
            self.detected_card = (frame, (x1, y1, x2, y2), corners, raw) if present else None
            self.detected_card_name = "Card" if present else ""

        # Overlay: the area (the photo); the outline, used only for the foil check, isn't drawn
        if not present:
            color, label = (160, 160, 160), "Fixed area - no card"
        elif self.awaiting_new_card and self.auto_capture_enabled:
            color, label = (100, 200, 255), "Fixed area [Captured - drop next card]"
        elif self.stable_frames < self.required_stable_frames:
            color, label = (255, 165, 0), f"Fixed area [Stabilizing {self.stable_frames}/{self.required_stable_frames}]"
        else:
            color, label = (0, 255, 0), "Fixed area [Ready]"
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
        text_scale = max(0.7, width / 1280 * 0.7)
        cv2.putText(annotated, label, (x1, max(int(30 * text_scale), y1 - 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, text_scale, color, max(2, int(2 * text_scale)))

        self._trace_waiting(detected=present)
        if (self.auto_capture_enabled and present and not self.card_under_review and not self.awaiting_new_card
                and not self.focus_sweep_running and not self.focus_probe_running
                and self.stable_frames >= self.required_stable_frames
                and time.time() - self.last_auto_capture_time >= self.auto_capture_delay):
            self._trigger_auto_capture()

    def _mark_captured(self):
        """Remember the captured card; auto-capture waits for the next one"""
        self.area_captured = self.area_thumb
        self.awaiting_new_card = True
        self.captured_thumbnail = self.previous_thumbnail
        # A drop seen during an earlier focus move was this card's: when it was taken as new by
        # another sign ("Card gone"), the flag used to survive and made this very card "new"
        # right after its capture (Arwen captured twice, 2026-09-25; also 2026-09-24 19:09)
        self.disturbed_during_focus = False

    def detect_camera_type(self):
        """Auto-detect available camera"""
        camera_type = Config.CAMERA_TYPE.lower()
        
        if camera_type == 'usb':
            return 'usb'
        elif camera_type == 'picamera':
            return 'picamera'
        elif camera_type == 'auto':
            if self._test_usb_camera():
                return 'usb'
            elif self._test_picamera():
                return 'picamera'
            else:
                raise Exception(f"No camera found (USB camera {Config.USB_CAMERA_INDEX} or a Pi camera)")
        else:
            raise Exception(f"Unknown camera type: {camera_type}")
    
    def _usb_camera_present(self):
        """False when the USB camera's device is missing (opening it would only make OpenCV complain)"""
        index = Config.USB_CAMERA_INDEX
        if not sys.platform.startswith('linux') or not isinstance(index, int):
            return True
        return os.path.exists(f'/dev/video{index}')

    def _test_usb_camera(self):
        """Test if USB camera is available"""
        if not self._usb_camera_present():
            return False
        try:
            cap = cv2.VideoCapture(Config.USB_CAMERA_INDEX)
            if cap.isOpened():
                ret, _ = cap.read()
                cap.release()
                return ret
            return False
        except:
            return False
    
    def _test_picamera(self):
        """Test if Picamera is available"""
        try:
            from picamera2 import Picamera2
            picam = Picamera2()
            picam.close()
            return True
        except:
            return False
    
    def initialize_camera(self):
        """
        Open the camera and start the capture thread. Without a camera the app still starts
        (the collection pages need none): the thread keeps looking for it.
        """
        self._open_camera()
        self.running = True
        self.capture_thread = threading.Thread(target=self._capture_frames, daemon=True)
        self.capture_thread.start()

    def _open_camera(self):
        """Open the configured camera. False, with camera_error set, when it is not available"""
        try:
            self.camera_type = self.detect_camera_type()

            if self.camera_type == 'usb':
                self._initialize_usb_camera()
            elif self.camera_type == 'picamera':
                self._initialize_picamera()

            self.failed_reads = 0
            self.camera_error = None
            self.log(f"Camera initialized successfully ({self.camera_type})", level="success")
            return True
        except Exception as e:
            self._close_camera()
            if self.camera_error is None:  # said once, not at every retry
                self.log(f"No camera: {e} - the scanner waits for it; the collection works without it",
                         level="warning")
            self.camera_error = str(e)
            return False

    def _close_camera(self):
        camera, self.camera = self.camera, None
        if camera is None:
            return
        try:
            if self.camera_type == 'usb':
                camera.release()
            elif self.camera_type == 'picamera':
                camera.stop()
                camera.close()
        except Exception as e:
            logger.debug(f"Closing the camera: {e}")

    def _read_failed(self, reason="The camera stopped sending images (unplugged?)"):
        """
        A frame could not be read. One can be a hiccup; CAMERA_LOST_AFTER in a row and the
        camera is closed and looked for again (instead of an error per frame in the log).
        """
        self.failed_reads += 1
        if self.failed_reads < CAMERA_LOST_AFTER:
            time.sleep(0.1)
            return
        self.log(f"{reason} - waiting for the camera to come back", level="warning")
        self._close_camera()
        with self.frame_lock:
            self.camera_error = reason
            self.current_frame = self.current_raw = self.annotated_frame = None
            self.card_detected = False
            self.stable_frames = 0

    def _wait_for_camera(self):
        """No camera: try to open it again every CAMERA_RETRY_SECONDS"""
        deadline = time.time() + CAMERA_RETRY_SECONDS
        while self.running and time.time() < deadline:
            time.sleep(0.1)
        if self.running:
            self._open_camera()

    def _initialize_usb_camera(self):
        """Initialize USB camera using OpenCV, setting high resolution and focus settings"""
        # CRITICAL: Use V4L2 backend directly instead of GStreamer
        # GStreamer has issues with format changes on Raspberry Pi
        if not self._usb_camera_present():
            raise Exception(f"USB camera {Config.USB_CAMERA_INDEX} is not connected "
                            f"(/dev/video{Config.USB_CAMERA_INDEX} not found)")
        self.camera = cv2.VideoCapture(Config.USB_CAMERA_INDEX, cv2.CAP_V4L2)

        if not self.camera.isOpened():
            raise Exception(f"Failed to open USB camera at index {Config.USB_CAMERA_INDEX} "
                            f"(in use by another program?)")

        width = Config.CAMERA_RESOLUTION[0]
        height = Config.CAMERA_RESOLUTION[1]
        fps = Config.CAMERA_FPS

        # CRITICAL: Set MJPEG codec BEFORE setting resolution
        # USB cameras require MJPEG for high resolutions (2560x1440)
        # YUYV only supports up to 640x480
        self.camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))

        self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.camera.set(cv2.CAP_PROP_FPS, fps)

        # CRITICAL: Reset camera to optimal settings on every startup
        # These settings persist in camera hardware across runs
        self._run_v4l2_command('-c', 'sharpness=50')  # Default sharpness
        self._run_v4l2_command('-c', 'zoom_absolute=100')  # Minimum zoom (widest field of view)

        self.focus_range = self._query_focus_range()
        if self.focus_locked_value is not None and self.focus_range:
            self._set_manual_focus(self.focus_locked_value)
            focus = f"focus locked at {self.focus_locked_value}"
        else:
            self._run_v4l2_command('-c', 'focus_automatic_continuous=1')
            focus = "continuous autofocus"

        # Give camera time to initialize
        time.sleep(2)

        # Ask OpenCV for the camera's raw JPEG instead of decoded frames: live frames are
        # then decoded at half size (detection, preview) and full size only for captures
        self.raw_mjpeg = self.half_size_decode = False
        self.camera.set(cv2.CAP_PROP_CONVERT_RGB, 0)
        ret, raw = self.camera.read()
        if not ret:
            # Not the scanner's camera (another video device at this index), or it is busy
            raise Exception(f"USB camera at index {Config.USB_CAMERA_INDEX} opened but sends no images")
        self.log(f"Camera settings: sharpness=50, zoom=100, {focus}")
        self.log(f"Resolution: {width}x{height} @ {fps} FPS")
        if ret and raw is not None and raw.ndim == 2 and raw.size > 2 and bytes(raw.ravel()[:2]) == b'\xff\xd8':
            full = cv2.imdecode(raw.ravel(), cv2.IMREAD_COLOR)
            self.raw_mjpeg = full is not None
            self.half_size_decode = self.raw_mjpeg and full.shape[1] >= 1920
        if not self.raw_mjpeg:
            self.camera.set(cv2.CAP_PROP_CONVERT_RGB, 1)
        self.log("Frames: " + ("raw JPEG, live view decoded at half size" if self.half_size_decode
                               else "raw JPEG" if self.raw_mjpeg else "decoded by OpenCV"))

        self.log(f"USB camera initialized (index: {Config.USB_CAMERA_INDEX})")
    
    def _initialize_picamera(self):
        """Initialize Raspberry Pi Camera using Picamera2"""
        from picamera2 import Picamera2
        
        self.camera = Picamera2()
        
        config = self.camera.create_video_configuration(
            main={"size": Config.CAMERA_RESOLUTION},
            lores={"size": Config.CAMERA_PREVIEW_RESOLUTION},
        )
        self.camera.configure(config)
        self.camera.start()
        
        time.sleep(2)
        
        self.log("Picamera initialized")
    
    def _capture_frames(self):
        """Continuously capture frames and perform object detection"""
        while self.running:
            if self.camera_error:
                self._wait_for_camera()
                continue
            try:
                raw = None
                if self.camera_type == 'usb' and self.raw_mjpeg:
                    # grab() takes every frame off the camera (so the next one is fresh, no
                    # stale buffered frames) without decoding; only CAMERA_FPS are decoded
                    if not self.camera.grab():
                        self._read_failed()
                        continue
                    self.failed_reads = 0
                    now = time.time()
                    if now < self.next_retrieve:
                        continue
                    # Keep a steady CAMERA_FPS schedule (e.g. 2 of every 3 frames of a 30 fps camera)
                    self.next_retrieve = max(self.next_retrieve + 1.0 / Config.CAMERA_FPS, now - 1.0 / Config.CAMERA_FPS)
                    ret, raw = self.camera.retrieve()
                    if not ret or raw is None:
                        continue
                    raw = raw.ravel()
                    frame = cv2.imdecode(raw, cv2.IMREAD_REDUCED_COLOR_2 if self.half_size_decode else cv2.IMREAD_COLOR)
                    if frame is None:
                        continue  # corrupt JPEG from the camera
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                elif self.camera_type == 'usb':
                    ret, frame = self.camera.read()
                    if not ret:
                        self._read_failed()
                        continue
                    self.failed_reads = 0
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                elif self.camera_type == 'picamera':
                    try:
                        frame = self.camera.capture_array()
                    except Exception as e:
                        self._read_failed(f"The camera stopped sending images ({e})")
                        continue
                    self.failed_reads = 0
                    if len(frame.shape) == 3 and frame.shape[2] == 4:
                        frame = cv2.cvtColor(frame, cv2.COLOR_RGBA2RGB)
                    elif len(frame.shape) == 3 and frame.shape[2] == 3:
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                frame = self._rotate(frame)
                annotated = frame.copy()

                # Track which detection to display (current or cached)
                display_bbox = None
                display_corners = None
                is_cached_detection = False

                if self._fixed_area_active():
                    self._fixed_area_step(frame, raw, annotated)

                elif not self.enable_detection:
                    # Detection is disabled - clear all detection state
                    self.last_card_detection = None
                    self.stable_frames = 0
                    with self.frame_lock:
                        self.detected_card = None
                        self.detected_card_name = ""
                        self.card_detected = False

                elif self.enable_detection:
                    # Run detection on raw frame for better performance
                    # Follow the card of the previous frame (or of the last one, across a detector
                    # hiccup of up to 2 frames)
                    bounding_box, card_name, confidence, corners = self.object_detector.detect(
                        frame,
                        previous=self.tracked_outline if self.missing_frames <= 2 else None
                    )
                    self.tracked_outline = corners if bounding_box else self.tracked_outline

                    if bounding_box:
                        gap = self.missing_frames  # frames without a card just before this one
                        self.missing_frames = 0
                        # Measure movement on the raw detection, before smoothing
                        raw_points = corners

                        # Apply smoothing to eliminate flicker
                        smoothed_box = self.smooth_bounding_box(bounding_box)

                        # Use smoothed bounding box for all subsequent operations
                        if smoothed_box:
                            bounding_box = smoothed_box
                            x1, y1, x2, y2 = bounding_box
                            width = x2 - x1
                            height = y2 - y1

                            # Store this as a valid card detection
                            self.last_card_detection = {
                                'bbox': bounding_box,
                                'corners': corners,
                                'name': card_name,
                                'confidence': confidence,
                                'time': time.time(),
                                'frame': frame,
                                'raw': raw
                            }
                            display_bbox = bounding_box
                            display_corners = corners

                            # Card is detected - reset lost frames counter
                            self.frames_since_card_lost = 0

                            # Count consecutive frames where the card is still and in focus
                            if self._is_card_settled(frame, raw_points):
                                self.stable_frames = min(self.stable_frames + 1, self.required_stable_frames)
                            else:
                                self.stable_frames = 0
                            self._trace_waiting(detected=True)

                            self._check_focus_drift()

                            # While the lens moves, the blur can hide the card for a few frames and
                            # shift its outline (a probe once looked like a 1.9% jump and caused a
                            # second capture of the same card): no new-card rules until 0.6 s after.
                            # A real drop then is still seen by its big jump - the card counts as new
                            # once the focus is done (and spoils a probe's measurement)
                            focus_moving = self._focus_moving()
                            self._rebaseline_after_focus(focus_moving)
                            movement, _ = getattr(self, 'last_frame_change', (0.0, 0.0))
                            if focus_moving and not self.focus_sweep_running and movement > 0.03:
                                self.disturbed_during_focus = True
                            if self.awaiting_new_card and not focus_moving and self.disturbed_during_focus:
                                self.disturbed_during_focus = False
                                self.awaiting_new_card = False
                                self.tracked_outline = None
                                self.stable_frames = 0
                                if self.auto_capture_enabled:
                                    self.log("New card detected (dropped while focusing)")
                            elif self.awaiting_new_card and not focus_moving and self._new_card_arrived(gap):
                                self.awaiting_new_card = False
                                self.stable_frames = 0  # the new card must settle first
                                # Find the new card afresh: following the old outline could latch
                                # onto edges of the new card that happen to lie where the old one's
                                # were (a foil's inner frame - the photo then cut off its set line)
                                self.tracked_outline = None
                                if self.auto_capture_enabled:
                                    movement, image_change = getattr(self, 'last_frame_change', (0, 0))
                                    self.log(f"New card detected (jump {movement:.1%}, image change {image_change:.2f})")
                                    if time.time() - self.last_auto_capture_time < 2.0:
                                        self._start_debug_dump('new_card_soon_after_capture')
                            # Keep the card for capture
                            if display_bbox:
                                with self.frame_lock:
                                    self.detected_card = (frame, display_bbox, corners, raw)
                                    self.detected_card_name = card_name
                                    self.card_detected = True
                            else:
                                with self.frame_lock:
                                    self.detected_card = None
                                    self.detected_card_name = ""
                                    self.card_detected = False
                    else:
                        # No current detection - signal to smoothing algorithm
                        self.smooth_bounding_box(None)

                        # A cached (held) detection is not a still card: the next card
                        # must settle from scratch
                        self.stable_frames = 0

                        self._trace_waiting(detected=False)

                        # Card gone for more than a detector hiccup: treat as a change of card
                        # (not while the lens moves - the blur can hide the card)
                        self.missing_frames = 0 if self._focus_moving() else self.missing_frames + 1
                        if self.awaiting_new_card and self.missing_frames >= self.missing_frames_for_new_card:
                            self.awaiting_new_card = False
                            if self.auto_capture_enabled:
                                self.log("Card gone - ready for the next card")
                                # A foil Gwen Stacy vanished right after its capture and came back
                                # as "new" (2026-09-25): keep the frames of such cases (debug trace)
                                if time.time() - self.last_auto_capture_time < 2.0:
                                    self._start_debug_dump('card_gone_soon_after_capture')

                        # Check if we have a recent cached card detection
                        if self.last_card_detection:
                            time_since_detection = time.time() - self.last_card_detection['time']
                            if time_since_detection < self.card_display_duration:
                                # Show cached card detection
                                display_bbox = self.last_card_detection['bbox']
                                display_corners = self.last_card_detection['corners']
                                bounding_box = display_bbox
                                card_name = self.last_card_detection['name']
                                confidence = self.last_card_detection['confidence']
                                is_cached_detection = True

                                # Make the cached card available for capture
                                with self.frame_lock:
                                    self.detected_card = (self.last_card_detection['frame'], display_bbox, display_corners,
                                                          self.last_card_detection['raw'])
                                    self.detected_card_name = card_name
                                    self.card_detected = True

                                # Don't increment lost frames counter yet - we're showing cached detection
                            else:
                                # Cache expired, clear it
                                self.last_card_detection = None
                                self.frames_since_card_lost += 1
                        else:
                            # No cached detection available
                            self.frames_since_card_lost += 1

                        # Reset smoothed bounding box after losing card for several frames
                        if not display_bbox and self.frames_since_card_lost > 10:
                            self.smoothed_bbox = None

                        # Update detection state
                        if not display_bbox:
                            with self.frame_lock:
                                self.detected_card = None
                                self.detected_card_name = ""
                                self.card_detected = False

                    # Draw the detection box (current or cached)
                    if display_bbox:
                        x1, y1, x2, y2 = display_bbox

                        # Change box color based on stability
                        if is_cached_detection:
                            box_color = (100, 200, 255)  # Light blue for cached detection
                        elif self.stable_frames >= self.required_stable_frames:
                            box_color = (0, 255, 0)  # Green when stable
                        else:
                            box_color = (255, 165, 0)  # Orange when stabilizing

                        # Draw the card outline (the fixed area has a box without one)
                        if display_corners is not None:
                            cv2.polylines(annotated, [display_corners.astype(np.int32)], True, box_color, 3)
                        else:
                            cv2.rectangle(annotated, (x1, y1), (x2, y2), box_color, 3)

                        # Create label
                        label = f"{card_name} ({confidence:.2f})"
                        if is_cached_detection:
                            time_remaining = self.card_display_duration - (time.time() - self.last_card_detection['time'])
                            label += f" [HOLD {time_remaining:.1f}s]"
                        elif self.awaiting_new_card and self.auto_capture_enabled:
                            label += " [Captured - drop next card]"
                        elif not self.card_in_focus:
                            label += " [Focusing]"
                        elif self.stable_frames < self.required_stable_frames:
                            label += f" [Stabilizing {self.stable_frames}/{self.required_stable_frames}]"
                        else:
                            label += " [Ready]"

                        # Scale the label with the frame so it stays readable in the (downscaled) stream
                        text_scale = max(0.7, frame.shape[1] / 1280 * 0.7)
                        cv2.putText(annotated, label, (x1, max(int(30 * text_scale), y1 - 10)),
                                    cv2.FONT_HERSHEY_SIMPLEX, text_scale, box_color, max(2, int(2 * text_scale)))

                        # Auto-capture logic: Trigger when card-sized object is stable and ready
                        # Re-check auto_capture_enabled right before triggering to avoid race conditions
                        if (self.enable_detection and  # Detection must be enabled
                            self.auto_capture_enabled and
                            not is_cached_detection and  # Only auto-capture current detections, not cached ones
                            not self.card_under_review and  # Don't auto-capture if card is being reviewed
                            not self.awaiting_new_card and  # Still the card from the last capture
                            not self.focus_sweep_running and  # Frames are blurry while the lens moves
                            not self.focus_probe_running and
                            self.stable_frames >= self.required_stable_frames):

                            # Check if enough time has passed since last auto-capture
                            time_since_last_capture = time.time() - self.last_auto_capture_time

                            # Debug logging for timing
                            if time_since_last_capture < self.auto_capture_delay:
                                # Only log occasionally to avoid spam (every 20 frames = ~1 second at 20 FPS)
                                if self.stable_frames % 20 == 0:
                                    remaining = self.auto_capture_delay - time_since_last_capture
                                    self.log(f"Auto-capture ready, waiting for cooldown: {remaining:.1f}s remaining", level="info")

                            if time_since_last_capture >= self.auto_capture_delay and self._outline_flicker():
                                self.awaiting_new_card = True
                                self.log("Same card as the last capture (its outline flickered) - not captured again")
                            elif time_since_last_capture >= self.auto_capture_delay:
                                self._trigger_auto_capture()

                if self.debug_trace_enabled:
                    self._record_debug_frame(frame)

                with self.frame_lock:
                    self.current_frame = frame
                    self.current_raw = raw
                    self.annotated_frame = annotated
                    self.frame_id += 1

                if raw is None:  # grab() already paces raw-JPEG cameras
                    time.sleep(1.0 / Config.CAMERA_FPS)
            except Exception as e:
                self.log(f"Error capturing frame: {e}", level="error")
                time.sleep(0.1)
    
    def get_full_frame(self):
        """Current frame at full camera resolution (RGB) - for captures"""
        with self.frame_lock:
            frame, raw = self.current_frame, self.current_raw
        if raw is not None and self.half_size_decode:
            return self._rotate(cv2.cvtColor(cv2.imdecode(raw, cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB))
        return None if frame is None else frame.copy()

    def get_stream_jpeg(self, max_height=720):
        """
        (frame_id, JPEG bytes) of the annotated live view. Each frame is encoded once, no
        matter how many browser tabs are watching; callers skip ids they already sent.
        """
        with self.frame_lock:
            frame_id, annotated, card_detected = self.frame_id, self.annotated_frame, self.card_detected
            cached_id, cached_jpeg = self._stream_cache
        if annotated is None:
            return -1, None
        if cached_id == frame_id:
            return frame_id, cached_jpeg
        if annotated.shape[0] > max_height:
            width = int(annotated.shape[1] * max_height / annotated.shape[0])
            annotated = cv2.resize(annotated, (width, max_height), interpolation=cv2.INTER_AREA)
        # Higher quality while a card is in view
        ok, buffer = cv2.imencode('.jpg', cv2.cvtColor(annotated, cv2.COLOR_RGB2BGR),
                                  [int(cv2.IMWRITE_JPEG_QUALITY), 75 if card_detected else 65])
        jpeg = buffer.tobytes() if ok else None
        with self.frame_lock:
            self._stream_cache = (frame_id, jpeg)
        return frame_id, jpeg
    
    def get_detected_card(self):
        """
        Get the currently detected card image

        Returns:
            tuple: (image, card_name, foil_image) - foil_image is the perspective-corrected card
            from outline detection, where the ★/• corner is at a known position (None without an
            outline); (None, "", None) if no card. In fixed area mode the image is always the
            area as drawn (an outline can cut the card - a holo streak once passed for its top
            edge); the outline only serves the foil check.
        """
        with self.frame_lock:
            detected, card_name = self.detected_card, self.detected_card_name
        if detected is None:
            return None, "", None

        # Frames are never modified after capture, so cropping outside the lock is safe
        frame, bbox, corners, raw = detected
        if raw is not None and self.half_size_decode:
            # Detection ran on the half-size frame: cut the card from the full-size image
            full = self._rotate(cv2.cvtColor(cv2.imdecode(raw, cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB))
            scale_x, scale_y = full.shape[1] / frame.shape[1], full.shape[0] / frame.shape[0]
            frame = full
            if corners is not None:
                corners = corners * np.array([scale_x, scale_y], dtype=np.float32)
            bbox = (int(bbox[0] * scale_x), int(bbox[1] * scale_y), int(bbox[2] * scale_x), int(bbox[3] * scale_y))
        warped = warp_card(frame, corners) if corners is not None else None
        if warped is not None and not self._fixed_area_active():
            return warped, card_name, warped
        x1, y1, x2, y2 = bbox
        return frame[y1:y2, x1:x2].copy(), card_name, warped
    
    def is_card_detected(self):
        """Check if a card is currently detected"""
        with self.frame_lock:
            return self.card_detected

    def get_detection_status(self):
        """Get detailed detection status for enhanced UI feedback"""
        with self.frame_lock:
            return {
                'detected': self.card_detected,
                'stable_frames': self.stable_frames,
                'required_frames': self.required_stable_frames,
                'is_stable': self.stable_frames >= self.required_stable_frames,
                'awaiting_new_card': self.awaiting_new_card and self.auto_capture_enabled,
                'in_focus': self.card_in_focus,
                'focusing': self.focus_sweep_running,
                'capturing': self.capture_pending,
                'camera_error': self.camera_error
            }

    def capture_card_image_only(self, card_number, settle=0.3):
        """
        Capture and save a card image WITHOUT AI processing.
        settle: wait before taking the image - auto-captures pass 0 (the card has already been
            still for stability_frames)
        Returns: (image_path, card_image_rgb, foil_image) or (None, None, None) on failure
        """
        self.log(f"Capturing card #{card_number}...")
        if settle:
            time.sleep(settle)  # Let the image steady (manual captures)

        card_image, card_name, foil_image = self.get_detected_card()
        self._mark_captured()  # don't auto-capture this card again
        if card_image is not None:
            self._count_capture()

        if card_image is None:
            # No card detected - capture full frame
            self.log("No card detected, capturing full frame", level="info")
            frame = self.get_full_frame()
            if frame is None:
                self.log("Failed to capture frame", level="error")
                return None, None, None
            card_image = frame
        else:
            self.log(f"Using detected card crop ({card_image.shape[1]}x{card_image.shape[0]}px)", level="info")

        # Save image
        timestamp = int(time.time())
        image_path = Config.IMAGES_DIR / f"card_{card_number}_{timestamp}.jpg"

        card_bgr = cv2.cvtColor(card_image, cv2.COLOR_RGB2BGR)
        cv2.imwrite(str(image_path), card_bgr)

        self.log(f"Card captured: {image_path.name}")
        return image_path, card_image, foil_image

    def _run_v4l2_command(self, *args):
        """Run a v4l2-ctl command"""
        try:
            video_device = f'/dev/video{Config.USB_CAMERA_INDEX}'
            cmd = ['v4l2-ctl', '-d', video_device] + list(args)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            if result.returncode != 0:
                self.log(f"v4l2-ctl error: {result.stderr}", level="warning")
                return False
            return True
        except subprocess.TimeoutExpired:
            self.log("v4l2-ctl command timed out", level="warning")
            return False
        except Exception as e:
            self.log(f"v4l2-ctl command failed: {e}", level="warning")
            return False

    def _query_focus_range(self):
        """(min, max) of the camera's manual focus control, or None if it has none"""
        try:
            video_device = f'/dev/video{Config.USB_CAMERA_INDEX}'
            output = subprocess.run(['v4l2-ctl', '-d', video_device, '--list-ctrls'],
                                    capture_output=True, text=True, timeout=2).stdout
        except Exception:
            return None
        for line in output.splitlines():
            if line.strip().startswith('focus_absolute'):
                low, high = re.search(r'min=(-?\d+)', line), re.search(r'max=(-?\d+)', line)
                if low and high:
                    return int(low.group(1)), int(high.group(1))
        return None

    def _set_manual_focus(self, position):
        """Switch off autofocus and move the lens to `position`"""
        self._run_v4l2_command('-c', 'focus_automatic_continuous=0')
        self._move_focus(position)

    def _move_focus(self, position, from_below=True, approach=30):
        """
        Move the lens to `position`. The lens has play: the same position reached from above
        measured up to 5x blurrier than from below, so sweeps measure while moving up and
        the final position is approached from `approach` below too.
        """
        position = int(position)
        if from_below and self.focus_range:
            self._run_v4l2_command('-c', f'focus_absolute={max(self.focus_range[0], position - approach)}')
            time.sleep(0.15)
        self._run_v4l2_command('-c', f'focus_absolute={position}')

    def _measure_focus_sharpness(self, samples=3):
        """Median sharpness of the card (or the image centre when there is no card) over a few frames"""
        values = []
        last_id = None
        for _ in range(samples):
            # Each sample from a new frame
            deadline = time.time() + 0.3
            while self.frame_id == last_id and time.time() < deadline:
                time.sleep(0.01)
            with self.frame_lock:
                frame = self.current_frame
                detected = self.detected_card
                last_id = self.frame_id
            if frame is None:
                time.sleep(0.05)
                continue
            if detected is not None:
                x1, y1, x2, y2 = detected[1]
                region = frame[y1:y2, x1:x2]
            else:
                h, w = frame.shape[:2]
                region = frame[h // 4:3 * h // 4, w // 4:3 * w // 4]
            if region.size:
                small = cv2.resize(region, (320, max(1, int(320 * region.shape[0] / region.shape[1]))), interpolation=cv2.INTER_AREA)
                values.append(cv2.Laplacian(cv2.cvtColor(small, cv2.COLOR_RGB2GRAY), cv2.CV_64F).var())
        return float(np.median(values)) if values else 0.0

    def refocus(self, reason="manual"):
        """
        Find the sharpest focus with a sweep and lock it (runs in the background, ~7 s).

        Returns:
            bool: False if the camera has no manual focus or a sweep is already running
        """
        if self.camera_error:
            self.log("No camera to focus", level="warning")
            return False
        if self.camera_type != 'usb' or not self.focus_range:
            self.log("This camera has no manual focus control", level="warning")
            return False
        if self.focus_sweep_running or self.focus_probe_running:
            return False
        self.focus_sweep_running = True
        threading.Thread(target=self._run_focus_sweep, args=(reason,), daemon=True, name="Focus-Sweep").start()
        return True

    def _run_focus_sweep(self, reason):
        try:
            self.log(f"Focusing ({reason}) - finding the sharpest image...")
            self._run_v4l2_command('-c', 'focus_automatic_continuous=0')
            set_focus = lambda position: self._run_v4l2_command('-c', f'focus_absolute={position}')
            low, high = self.focus_range
            best = sharpness = None
            # Lens + camera buffer need ~0.4 s before a move shows up in the image; if the
            # result doesn't hold up, the sweep is repeated with a longer wait
            for settle in (0.45, 0.8):
                best, sharpness, _ = focus_sweep(set_focus, self._measure_focus_sharpness, low, high, settle=settle)
                self._move_focus(best)
                time.sleep(settle + 0.2)
                if self._measure_focus_sharpness() >= 0.7 * sharpness:
                    break
            self.focus_locked_value = best
            self.settings.set('focus_value', best)
            self.captures_since_focus = 0
            self.log(f"Focus locked at {best}", level="success")
        except Exception as e:
            self.log(f"Focus sweep failed: {e}", level="error")
        finally:
            self.last_focus_move = self.last_focus_sweep = time.time()
            self.focus_sweep_running = False
            self.out_of_focus_since = None

    def set_refocus_every(self, captures):
        """Probe the focus every this many captures while scanning (0 = never); remembered"""
        captures = int(captures)
        if not 0 <= captures <= 100:
            raise ValueError("The focus check interval must be 0 to 100 captures")
        self.refocus_every = captures
        self.captures_since_focus = 0
        self.settings.set('refocus_every', captures)
        self.log(f"Focus check while scanning: every {captures} captures" if captures else "Focus check while scanning: off")

    def _count_capture(self):
        """After a capture (image already taken): probe the focus every refocus_every captures"""
        self.captures_since_focus += 1
        every = self.refocus_every
        if (every and self.captures_since_focus >= every and self.focus_locked_value is not None
                and self.camera_type == 'usb' and self.focus_range
                and not self.focus_sweep_running and not self.focus_probe_running):
            self.captures_since_focus = 0
            self.focus_probe_running = True
            threading.Thread(target=self._run_focus_probe, daemon=True, name="Focus-Probe").start()

    def _run_focus_probe(self, step=10, settle=0.5, margin=1.05):
        """
        Follow the best focus while cards are being dropped (it drifts as the pile grows and
        the lens warms up): in the gap after a capture (~1 s), compare the card's sharpness
        here and one step away, and keep the sharper position. An improvement keeps the
        direction for the next probe; otherwise the next probe tries the other way. A card
        dropped meanwhile spoils the comparison - the probe is then dropped.
        """
        low, high = self.focus_range
        start = self.focus_locked_value
        direction = self.focus_probe_direction
        if not low <= start + direction * step <= high:
            direction = -direction
        probe = start + direction * step
        try:
            self.disturbed_during_focus = False
            here = self._measure_focus_sharpness()
            # Approaching from only 15 below keeps the card sharp enough to stay detected
            self._move_focus(probe, approach=15)
            time.sleep(settle)
            there = self._measure_focus_sharpness()
            if not self.disturbed_during_focus and there > here * margin:
                self.focus_locked_value = probe
                self.settings.set('focus_value', probe)
                self.focus_probe_direction = direction
                self.log(f"Focus adjusted {start} -> {probe} ({there / max(here, 1e-6):.2f}x sharper)")
            else:
                self._move_focus(start, approach=15)
                if not self.disturbed_during_focus:
                    self.focus_probe_direction = -direction
                # Log file only - every few cards would clutter the activity log
                logger.info(f"Focus probe: {start} kept ({probe}: {there / max(here, 1e-6):.2f}x"
                            + (", card dropped meanwhile)" if self.disturbed_during_focus else ")"))
        except Exception as e:
            self.log(f"Focus probe failed: {e}", level="warning")
        finally:
            self.last_focus_move = time.time()
            self.focus_probe_running = False

    def set_continuous_autofocus(self, enabled):
        """Continuous autofocus on, or find and lock the best focus"""
        if enabled:
            self.focus_locked_value = None
            self.settings.set('focus_value', None)
            self._run_v4l2_command('-c', 'focus_automatic_continuous=1')
            self.log("Continuous autofocus on")
            return True
        return self.refocus("locking focus")

    def reset_focus(self):
        """Reset focus button: find the sharpest focus and lock it"""
        if self.camera_type != 'usb':
            self.log("Focus reset only available for USB cameras", level="warning")
            return False
        return self.refocus("manual")

    def set_detection_enabled(self, enabled):
        """Enable or disable card detection"""
        self.enable_detection = enabled
        self.log(f"Card detection {'enabled' if enabled else 'disabled'}")

    def cleanup(self):
        """Clean up camera resources"""
        self.running = False
        if self.capture_thread:
            self.capture_thread.join(timeout=2)

        self._close_camera()
        self.log("Camera stopped")
