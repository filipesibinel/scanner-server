#!/usr/bin/env python3
"""
Card identification, shared by every camera: light-ocr reads each card first, and the vision
AI is asked only when that read isn't a confirmed match (measured on 747 scans: OCR alone
confirmed 82% in 0.16 s each, the AI 95% in ~1 s; OCR first with the AI behind it 95%).

Two stages, each with its own queue (submit): one OCR worker - the reader takes one request at
a time, ~0.1 s per card on a GPU - then several AI workers for what OCR couldn't settle. A card
OCR confirms never waits behind a slow AI answer (full-art cards, which only the AI reads, take
the whole AI stage). What was read is handed back by one more thread, one card at a time, so
looking a card up and adding it holds up neither stage. identify() runs both stages in the
caller's thread.
"""

import logging
import queue
import threading
import time

import games
import prompts
from card_identifier import CardIdentifier
from card_ocr import CardOcr
from config import Config

logger = logging.getLogger('ai')

# What the AI stage is asked for a card
IDENTIFY, FOIL = 'identify', 'foil'


class Job:
    """A capture on its way through the two stages"""

    def __init__(self, image, foil_image, game_id, on_done):
        self.image = image
        self.foil_image = foil_image
        self.game_id = game_id  # the game being scanned at capture: not read as another game's card
        self.on_done = on_done  # on_done(card_info or None, seconds since submit)
        self.submitted = time.time()
        self.ocr_info = None
        self.task = None  # IDENTIFY or FOIL, once OCR has passed it on


class Identification:
    """The OCR reader, the vision AI and the queues in front of them"""

    def __init__(self, settings, log_callback=None, ai_workers=None):
        self.settings = settings
        self.log_callback = log_callback
        self.ai_workers = max(1, int(ai_workers or Config.VISION_AI_WORKERS))
        self.last_capture = None  # (card image RGB, foil image or None) last given - prompt editor tests

        self.card_identifier = None
        if Config.VISION_AI_ENABLED:
            # Load saved provider and model from settings
            saved_provider = settings.get_ai_provider()
            saved_model = settings.get_ai_model()
            try:
                self.card_identifier = CardIdentifier(provider=saved_provider, model=saved_model,
                                                      log_callback=log_callback)
                self.log(f"Initialized AI with saved settings: {saved_provider} ({saved_model or 'default'})")
            except Exception as e:
                # Fallback to config default if saved settings fail
                self.log(f"Failed to load saved AI settings, using config defaults: {e}", level="warning")
                try:
                    self.card_identifier = CardIdentifier(provider=Config.VISION_AI_PROVIDER, log_callback=log_callback)
                    self.log(f"Vision AI enabled ({Config.VISION_AI_PROVIDER})", level="success")
                except Exception as e:
                    self.log(f"Vision AI initialization failed: {e}", level="warning")
                    self.log("Continuing without AI identification", level="warning")

        self.card_ocr = CardOcr(log_callback=log_callback)
        self.ocr_enabled = bool(settings.get('ocr_first', True))
        if self.ocr_enabled:
            self.card_ocr.warm_up()

        self._ocr_queue = queue.Queue()
        self._ai_queue = queue.Queue()
        self._done_queue = queue.Queue()
        self._threads = []
        self._running = False

    def log(self, message, level="info"):
        """Send log message to both file logger and UI callback"""
        getattr(logger, 'info' if level == 'success' else level, logger.info)(message)
        if self.log_callback:
            self.log_callback(message, level)

    def ai_info(self):
        """'provider/model' of the vision AI, or None without one"""
        identifier = self.card_identifier
        return f"{identifier.provider}/{identifier.model}" if identifier else None

    def set_ai_provider(self, provider, model=None):
        """
        Dynamically change the AI provider and/or model for card identification

        Args:
            provider: 'gemini', 'openai', 'anthropic' or 'local'
            model: Specific model to use (optional, uses default if not specified)

        Returns:
            dict: {'success': bool, 'message': str, 'model': str}
        """
        provider = provider.lower()
        valid_providers = list(CardIdentifier.AVAILABLE_MODELS.keys())

        if provider not in valid_providers:
            return {
                'success': False,
                'message': f"Invalid provider '{provider}'. Must be one of: {', '.join(valid_providers)}"
            }

        try:
            new_identifier = CardIdentifier(provider=provider, model=model, log_callback=self.log_callback)

            # If successful, replace the old identifier
            self.card_identifier = new_identifier
            new_identifier.warm_up()

            # Save the selection to settings for persistence
            self.settings.set_ai_provider(provider, model)

            # Update the config for consistency (optional, doesn't persist across restarts)
            Config.VISION_AI_PROVIDER = provider

            message = f"AI provider changed to {provider} ({new_identifier.model})"
            self.log(message, level="success")
            return {'success': True, 'message': message, 'model': new_identifier.model}
        except ValueError as e:
            # API key not found or invalid model
            self.log(f"Failed to change AI provider: {e}", level="error")
            return {'success': False, 'message': str(e)}
        except Exception as e:
            self.log(f"Error changing AI provider: {e}", level="error")
            return {'success': False, 'message': f"Error: {str(e)}"}

    def set_ocr_enabled(self, enabled):
        """Read cards with light-ocr before asking the vision AI (remembered)"""
        self.ocr_enabled = enabled
        self.settings.set('ocr_first', enabled)
        if enabled:
            self.card_ocr.warm_up()
        else:
            self.card_ocr.stop()
        self.log(f"Read cards with OCR first: {'on' if enabled else 'off'}")

    # ------------------------------------------------------------------------
    # The two stages
    # ------------------------------------------------------------------------

    def _ai_foil_check(self, foil_image):
        """
        Whether the AI can be asked about the star/dot foil marker: only on a perspective-
        corrected card (the marker's corner is at a known position)
        """
        return bool(self.card_identifier and foil_image is not None and Config.VISION_AI_DETECT_FOIL
                    and prompts.has('foil', games.active_id()))

    @staticmethod
    def _said_by_ocr(ocr_info):
        return f"✓ Card identified by OCR: {ocr_info['name']} #{ocr_info['collector_number']}"

    def _read_with_ocr(self, image, foil_image):
        """
        OCR stage. Returns (ocr_info or None, what the AI is still needed for): None when the
        card is settled - OCR's read is a confirmed match, or there is no vision AI to ask.
        """
        ocr_info = None
        if self.ocr_enabled:
            ocr_info = self.card_ocr.read_card(image, games.active_id())
            if ocr_info and games.active().confirmed_read(ocr_info['name'], ocr_info['collector_number'],
                                                         ocr_info['set_code']):
                # OCR misses the marker on about a third of the foils: ask the AI about those
                if ocr_info['foil'] == 'unknown' and self._ai_foil_check(foil_image):
                    return ocr_info, FOIL
                self.log(self._said_by_ocr(ocr_info), level="success")
                return ocr_info, None
            if ocr_info and self.card_identifier:
                self.log("OCR read is not a confirmed match - asking the vision AI")

        if self.card_identifier:
            return ocr_info, IDENTIFY
        if not ocr_info:
            self.log("⚠ Vision AI not enabled - set API key to enable automatic identification", level="warning")
        return ocr_info, None

    def _ask_ai(self, task, image, foil_image, ocr_info):
        """AI stage: the foil marker of a card OCR confirmed, or the whole identification"""
        identifier = self.card_identifier  # a provider change swaps it; this card stays with one
        if identifier is None:
            return ocr_info

        if task == FOIL:
            ocr_info['foil'] = identifier.read_foil_symbol(foil_image)
            self.log(self._said_by_ocr(ocr_info), level="success")
            return ocr_info

        self.log("Identifying card with Vision AI...")
        # The foil check runs alongside the identification (saves ~0.3 s per card with
        # Ollama, more with cloud providers that serve requests in parallel)
        foil_result = {}
        foil_thread = None
        if self._ai_foil_check(foil_image) and (not ocr_info or ocr_info['foil'] == 'unknown'):
            foil_thread = threading.Thread(
                target=lambda: foil_result.update(foil=identifier.read_foil_symbol(foil_image)),
                daemon=True)
            foil_thread.start()
        card_info = identifier.identify_card(image)
        if foil_thread:
            foil_thread.join(timeout=60)
        # A marker OCR did read stands (it agreed with the AI on 480 of 481 scans)
        foil = foil_result.get('foil') or (ocr_info or {}).get('foil') or 'unknown'

        if card_info and card_info.get('name'):
            name = card_info['name']
            number = card_info.get('collector_number', '')
            if number:
                self.log(f"✓ Card identified: {name} #{number}", level="success")
            else:
                self.log(f"✓ Card identified: {name} (no collector number)", level="success")
            card_info['foil'] = foil
            return card_info

        self.log("Vision AI could not identify card", level="warning")
        if ocr_info:
            # Better than nothing: the name OCR read still finds the card for review
            ocr_info['foil'] = foil
            return ocr_info
        return card_info

    def identify(self, image, foil_image=None):
        """
        Identify a card from a preprocessed RGB image, in the caller's thread.
        foil_image: perspective-corrected card to read the star/dot foil marker from (its
        corner is at a known position); no AI foil check without it.
        Returns: card_info dict with name, collector_number, set_code, foil
        ('foil'|'non-foil'|'unknown'), and 'reader' when it wasn't the vision AI that read the
        card - or None
        """
        self.last_capture = (image, foil_image)
        ocr_info, task = self._read_with_ocr(image, foil_image)
        return self._ask_ai(task, image, foil_image, ocr_info) if task else ocr_info

    # ------------------------------------------------------------------------
    # Queues
    # ------------------------------------------------------------------------

    def start(self):
        """Start the OCR worker and the AI workers"""
        if self._running:
            return
        self._running = True
        self._threads = [threading.Thread(target=self._ocr_worker, daemon=True, name="OCR-Worker")]
        self._threads += [threading.Thread(target=self._ai_worker, daemon=True, name=f"AI-Worker-{n + 1}")
                          for n in range(self.ai_workers)]
        self._threads.append(threading.Thread(target=self._done_worker, daemon=True, name="Identified-Worker"))
        for thread in self._threads:
            thread.start()
        logger.info(f"Identification started: 1 OCR worker, {self.ai_workers} AI workers")

    def stop(self):
        self._running = False
        self.card_ocr.stop()

    def submit(self, image, foil_image, game_id, on_done):
        """
        Queue a capture. on_done(card_info or None, seconds) is called exactly once, from the
        one thread that hands results back - in the order cards are settled, not submitted.
        """
        self.last_capture = (image, foil_image)
        self._ocr_queue.put(Job(image, foil_image, game_id, on_done))

    def waiting(self):
        """(captures waiting for OCR, captures waiting for the AI) - not counting those being read"""
        return self._ocr_queue.qsize(), self._ai_queue.qsize()

    def _finish(self, job, card_info):
        self._done_queue.put((job, card_info, time.time() - job.submitted))

    def _done_worker(self):
        while self._running:
            done = self._next(self._done_queue)
            if done is None:
                continue
            job, card_info, seconds = done
            try:
                job.on_done(card_info, seconds)
            except Exception as e:
                logger.exception(f"Handling an identified card failed: {e}")

    def _next(self, jobs):
        """The next job of a queue, or None once a second (so the worker sees a stop)"""
        try:
            return jobs.get(timeout=1.0)
        except queue.Empty:
            return None

    def _ocr_worker(self):
        while self._running:
            job = self._next(self._ocr_queue)
            if job is None:
                continue
            try:
                if job.game_id != games.active_id():
                    # The game was switched while this capture waited: not read as a card of
                    # the other game (the caller keeps it for its own game's review)
                    self._finish(job, None)
                    continue
                job.ocr_info, job.task = self._read_with_ocr(job.image, job.foil_image)
                if job.task:
                    self._ai_queue.put(job)
                else:
                    self._finish(job, job.ocr_info)
            except Exception as e:
                logger.exception(f"OCR stage failed: {e}")
                self.log(f"OCR error: {e} - asking the vision AI", level="warning")
                job.ocr_info, job.task = None, IDENTIFY
                self._ai_queue.put(job)

    def _ai_worker(self):
        while self._running:
            job = self._next(self._ai_queue)
            if job is None:
                continue
            card_info = None
            try:
                if job.game_id == games.active_id():
                    card_info = self._ask_ai(job.task, job.image, job.foil_image, job.ocr_info)
            except Exception as e:
                logger.exception(f"AI stage failed: {e}")
                self.log(f"AI processing error: {e}", level="error")
                card_info = job.ocr_info  # what OCR read still stands (a confirmed card, or a name for review)
            self._finish(job, card_info)
