# ============================================================================
# FILE: server/scan_events.py
# The scanner page's Socket.IO events: capture, search, add, review, undo.
# ============================================================================
from datetime import datetime
from flask import request, send_from_directory
from flask_socketio import emit, join_room

from card_scanner import games
from card_scanner.core.review import REVIEW_DIR

from card_scanner.server.state import app, database, inventory, maintenance_barrier, review, scan_inventory, socketio, stations
from card_scanner.server.logs import logger
from card_scanner.server.desks import GENERAL_ROOM, desk, desk_for, emit_desk, log_to_client, page_stations, scanner
from card_scanner.server.captures import added_payload, scan_location, scan_stats, set_pending_capture, start_price_update
from card_scanner.server.stations_api import undo_station_add


@socketio.on('connect')
def handle_connect():
    """A page connected. A scanner page says which station it shows (?station=): it gets that desk's events"""
    station_id = request.args.get('station')
    target = desk_for(station_id) if station_id else None
    if target is not None:
        page_stations[request.sid] = station_id
        join_room(target.room)
    else:
        join_room(GENERAL_ROOM)
    logger.info(f"Client connected to SocketIO{' for station ' + station_id if target else ''}")
    emit('log', {
        'timestamp': datetime.now().strftime('%H:%M:%S'),
        'level': 'info',
        'message': 'Connected to card scanner'
    })


@socketio.on('capture_card')
def handle_capture(data):
    """Handle card capture request"""

    if not scanner:
        emit('error', {'message': 'Scanner not initialized'})
        return

    if scanner.camera_error:
        emit('error', {'message': f'No camera: {scanner.camera_error}'})
        return

    try:
        # The station takes the picture - whatever is in view, also without a detected card -
        # and sends it; it arrives like every capture (stations_api.station_capture)
        scanner.request_capture()

    except Exception as e:
        logger.exception(f"Exception in handle_capture: {e}")
        log_to_client(f"Capture error: {e}", level="error")
        emit('error', {'message': str(e)})


@socketio.on('search_card')
def handle_search(data):
    """Handle manual card search - lists printings so the user can pick the exact one"""

    logger.info(f"Search card request received: {data}")

    if not database:
        emit('error', {'message': 'Card database not initialized'})
        return
    game = games.active()

    card_name = (data.get('card_name') or '').strip()
    collector_number = (data.get('collector_number') or '').strip() or None
    set_code = (data.get('set_code') or '').strip() or None
    treatment = (data.get('treatment') or '').strip() or None

    # Without a name: set code + number, or a number with the set total ("199/165": games that print one)
    if not card_name and not (collector_number and (set_code or '/' in collector_number)):
        emit('error', {'message': 'Enter a card name, or the set code and number'})
        return

    try:
        resolved_name, printings = game.find_printings(card_name, collector_number, treatment, set_code)

        if len(printings) == 1:
            desk().current_card_info = printings[0]
            emit('card_found', {'card': game.card_payload(desk().current_card_info)})
        elif printings:
            # Several printings - let the user pick the one in hand
            desk().current_card_info = None
            emit('card_printings', {
                'name': resolved_name,
                'treatment': treatment,
                'cards': [game.card_payload(card) for card in printings]
            })
        elif resolved_name:
            # Card exists, but no printing has the requested treatment
            emit('card_not_found', {
                'card_name': resolved_name,
                'message': f'No printings of "{resolved_name}" match the selected treatment.'
            })
        else:
            logger.info(f"No match for '{card_name}', searching for similar cards...")
            similar = game.similar(card_name, limit=5) if card_name else []
            if similar:
                emit('similar_cards', {'cards': similar})
            else:
                emit('card_not_found', {'card_name': card_name or f"{(set_code or '').upper()} #{collector_number}".strip()})

    except Exception as e:
        logger.exception(f"Exception in handle_search: {e}")
        log_to_client(f"Search error: {e}", level="error")
        emit('error', {'message': str(e)})


@socketio.on('select_printing')
def handle_select_printing(data):
    """User picked a specific printing from the printing list"""

    game = games.active()
    card = game.get_card(data.get('id')) if database else None
    if not card:
        emit('error', {'message': 'Printing not found'})
        return

    desk().current_card_info = card
    logger.info(f"Printing selected: {card['name']} ({card['set']} #{card['number']})")
    emit('card_found', {'card': game.card_payload(card)})


@socketio.on('add_to_inventory')
def handle_add_inventory(data):
    """Handle add to inventory request"""
    # One at a time with a station removing a capture (stations_api.py: station_capture_sync,
    # which holds the same barrier): the card on the page may be a review item, and between
    # "which item is open" and "its card is in the scanned cards" the station must not be able
    # to remove that item and be told it is gone - the card would then be added after all.
    # Everything the add reads about the page (the card, the open item) is read in here: a
    # removal that came first has cleared it, and there is then no card to add.
    with maintenance_barrier:
        add_selected_card(data)


def add_selected_card(data):
    """The card on the page goes into the scanned cards (handle_add_inventory, under its barrier)"""
    card = desk().current_card_info
    if not scan_inventory or not card:
        logger.warning("Add to inventory requested but no card selected")
        emit('error', {'message': 'No card selected'})
        return

    try:
        game = games.active()
        condition = data.get('condition', 'Near Mint')
        # Several finishes of the card come in one event ("items") - separate events would
        # be handled in parallel threads, in any order
        items = data.get('items') or [{'finish': data.get('finish'), 'quantity': data.get('quantity', 1)}]
        for item in items:
            if (item.get('finish') or game.default_finish) not in game.finishes:
                emit('error', {'message': f"Unknown finish: {item.get('finish')}"})
                return

        # The card's own capture, else the one under review; it goes with the first finish added
        capture = card.pop('capture', None) or desk().pending_capture
        if capture == desk().pending_capture:
            set_pending_capture(None)
        added_rows = []
        # Added from the review queue: the card is the copy of the capture that waited there,
        # and takes its identity along (review.py: capture_key) - the phone that scanned it
        # then finds it in the scanned cards, and can still remove exactly this copy. It goes
        # with the first finish added, like the photo.
        reviewed = review.get(desk().current_review_id) if desk().current_review_id is not None else None
        if desk().current_review_id is not None and reviewed is None:
            # The item open here is not in the queue any more (resolved somewhere else): its
            # card is not added a second time, or after its station took it back
            desk().current_review_id = desk().review_sid = desk().current_card_info = None
            emit('error', {'message': 'This card is no longer in the review queue'})
            emit_desk('review_queue_update', {'count': review.count(game.id, desk().review_filter)})
            handle_review_open()
            return
        sync_key = reviewed.get('capture_key') if reviewed else None
        # Whose card it is: the station whose capture was reviewed, also when the review is
        # worked through on a page that shows every station's items (the page without a
        # station) - its camera scanned it, it counts as that camera's, and the phone that sent
        # it can still ask about it and remove it. A card added by hand is the page's own.
        source = reviewed['station'] if reviewed and reviewed.get('station') else desk().id

        for item in items:
            finish = item.get('finish') or game.default_finish
            try:
                quantity = max(1, int(item.get('quantity', 1)))
            except (ValueError, TypeError):
                quantity = 1
            logger.info(f"Adding card to inventory: {quantity}x {card['name']} ({condition}, {finish})")
            added_rows.append(scan_inventory.add_card(game.inventory_fields(card, finish), game.id, finish,
                                                      condition, quantity, capture=capture,
                                                      location=scan_location(), source=source,
                                                      capture_key=sync_key))
            capture = None
            sync_key = None

            # Send updated stats and what was added (the page offers an Undo)
            emit('inventory_updated', {'auto': False, 'stats': scan_stats(game.id),
                                       'added': added_payload(game, card, finish, quantity)})

        logger.info("Card added to inventory successfully")
        start_price_update(game, card, added_rows)
        desk().current_card_info = None
        if desk().current_review_id is not None:
            # Reviewed: resolve the item and open the next one
            review.remove(desk().current_review_id)
            desk().current_review_id = None
            emit_desk('review_queue_update', {'count': review.count(game.id, desk().review_filter)})
            handle_review_open()

        # Clear the review flag to allow next auto-capture
        if scanner:
            scanner.card_under_review = False
            logger.info("Card review completed - auto-capture re-enabled")

    except Exception as e:
        logger.exception(f"Exception in handle_add_inventory: {e}")
        log_to_client(f"Add to inventory error: {e}", level="error")
        emit('error', {'message': str(e)})
        # Clear flag even on error to prevent getting stuck
        if scanner:
            scanner.card_under_review = False


@socketio.on('review_open')
def handle_review_open(data=None):
    """Open the oldest item of the review queue (or say it is empty)"""
    game = games.active()
    item, total = review.first(game.id, desk().review_filter)
    desk().current_review_id = item['id'] if item else None
    desk().review_sid = request.sid if item else None
    if not item:
        desk().current_card_info = None
        set_pending_capture(None)
        emit('review_item', {'id': None, 'total': 0})
        return
    card = game.get_card(item['card_id']) if item['card_id'] else None
    if card:
        card['match'] = item['match']
    desk().current_card_info = card
    set_pending_capture(review.image_path(item))  # goes with whatever card is added for this item
    emit('review_item', {
        'id': item['id'],
        'total': total,
        'image_url': f"/review_images/{item['file']}" if item['file'] else None,
        'ai': {'name': item['ai_name'], 'number': item['ai_number'], 'set': item['ai_set'], 'foil': item['foil']},
        'captured_at': item['created_at'],
        'station': (stations.get(item['station']) or {'name': item['station']})['name'] if item.get('station') else None,
        'card': game.card_payload(card) if card else None,
    })


@socketio.on('review_skip')
def handle_review_skip(data=None):
    """Drop the open review item without adding anything"""
    if desk().current_review_id is not None:
        review.remove(desk().current_review_id)
    desk().current_review_id = desk().current_card_info = desk().review_sid = None
    set_pending_capture(None)
    emit_desk('review_queue_update', {'count': review.count(games.active_id(), desk().review_filter)})
    handle_review_open()  # the next one


@socketio.on('review_close')
def handle_review_close(data=None):
    """Leave the review; the open item stays in the queue"""
    desk().current_review_id = desk().current_card_info = desk().review_sid = None
    set_pending_capture(None)


@socketio.on('disconnect')
def handle_disconnect(*_args):
    """The reviewing page went away (reload, closed, asleep): the review is closed, so later
    captures aren't sent to the queue and an Add can't resolve the item unseen (the page
    opens it again when it reconnects)"""
    if desk().review_sid is not None and request.sid == desk().review_sid:
        logger.info("Reviewing page disconnected - review closed")
        handle_review_close()
    page_stations.pop(request.sid, None)


@app.route('/review_images/<path:name>')
def review_image(name):
    """Capture of a review queue item"""
    return send_from_directory(REVIEW_DIR, name)


@socketio.on('undo_last_add')
def handle_undo_last_add(data=None):
    """Take back the most recent add to the inventory - with data['station'], that station's"""
    if not inventory:
        emit('error', {'message': 'Inventory not initialized'})
        return

    station_id = (data or {}).get('station')
    if station_id:
        station = stations.get(station_id)
        if not (station and undo_station_add(station)):
            emit('error', {'message': 'Nothing to undo for this station'})
        return

    undone = scan_inventory.undo_last_add(source=desk().id)
    if undone:
        emit('inventory_undone', {'name': undone, 'stats': scan_stats(games.active().id)})
    else:
        emit('error', {'message': 'Nothing to undo'})


@socketio.on('dismiss_card')
def handle_dismiss_card(data=None):
    """Handle card dismissal - user cancels current card review"""

    logger.info("Card dismissed by user")
    desk().current_card_info = None
    # "Not found" dismisses itself to let auto scanning go on; the capture stays for a
    # manual search. Skip drops it
    if not (data or {}).get('keep_capture'):
        set_pending_capture(None)

    # Clear the review flag to allow next auto-capture
    if scanner:
        scanner.card_under_review = False
        logger.info("Card review dismissed - auto-capture re-enabled")

    emit('card_dismissed', {'message': 'Card dismissed'})


@socketio.on('toggle_detection')
def handle_toggle_detection(data):
    """Toggle card detection on/off"""

    if not scanner:
        logger.error("Toggle detection requested but scanner not initialized")
        emit('error', {'message': 'Scanner not initialized'})
        return

    enabled = data.get('enabled', True)
    try:
        scanner.set_detection_enabled(enabled)
    except ValueError as e:  # the camera station did not answer
        emit('error', {'message': str(e)})
        return
    emit('detection_toggled', {'enabled': enabled})
    logger.info(f"Detection toggled: {enabled}")
    log_to_client(f"Card detection {('enabled' if enabled else 'disabled')}", level="info")
