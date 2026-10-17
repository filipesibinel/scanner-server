# ============================================================================
# FILE: server/pages.py
# The pages themselves, the camera's live view, thumbnails and the header's numbers.
# ============================================================================
from flask import Response, abort, jsonify, render_template, send_from_directory, url_for
import time

from card_scanner.config import Config
from card_scanner import games

from card_scanner.server.state import app, database, inventory, review, stations
from card_scanner.server.desks import desk, desk_for
from card_scanner.server.captures import scan_stats
from card_scanner.server.card_data import data_update_errors, data_update_notices, data_updates_running


@app.route('/favicon.ico')
def favicon():
    """
    The pages' icon, at the address a browser asks for by itself: when it opens something
    that is not one of the pages (a picture, an /api/ answer) it has no <link> to tell it
    where the icon is and tries /favicon.ico. The pages themselves name the file in static/
    (templates/_favicon.html); this is the same file, so that the answer is not a 404.
    """
    return send_from_directory(app.static_folder, 'favicon.ico', max_age=86400)


@app.route('/')
def index():
    """The cameras (stations), each with a link to its page"""
    return render_template('stations.html')


@app.route('/scan/<station_id>')
def scan_page(station_id):
    """A station's scanner page: its camera, its cards, its review queue"""
    if desk_for(station_id) is None:
        abort(404)
    return render_template('scanner.html', station=stations.get(station_id))


@app.route('/settings')
def settings_page():
    """Every setting, in categories (cameras, sounds, card reading, collection and data, server)"""
    return render_template('settings.html')


@app.context_processor
def page_links():
    """settings_url: the settings page (since 2026-10-15 one page for all of them)"""
    return {'settings_url': '/settings'}


@app.route('/collection')
def collection():
    """Inventory management, trades, statistics, backups"""
    return render_template('collection.html')


@app.route('/decks')
@app.route('/decks/<int:deck_id>')
def decks(deck_id=None):
    """
    The decks: the list (with what the collection can build, and the preconstructed decks)
    at /decks, one deck at /decks/<id>. Both addresses give the same page - its script shows
    what the address says (static/js/decks.js: showWhatTheAddressSays) - so that opening a
    deck from the list, or going back, does not load the page again. A deck that does not
    exist is the script's to report: it shows the list and says so.
    """
    return render_template('decks.html')


def generate_frames(camera):
    """MJPEG stream of a camera's annotated live view (each new frame once; encoding is shared)"""
    last_id = -1
    while True:
        frame_id, jpeg = camera.get_stream_jpeg() if camera else (-1, None)
        if jpeg is None or frame_id == last_id:
            time.sleep(0.01)
            continue
        last_id = frame_id
        yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + jpeg + b'\r\n'


@app.route('/captures/<path:name>')
def capture_thumbnail(name):
    """Thumbnail of a capture kept with an inventory entry"""
    from card_scanner.core.inventory import CAPTURES_DIR
    return send_from_directory(CAPTURES_DIR, name, max_age=86400)


@app.route('/capture_images/<path:name>')
def capture_image(name):
    """A picture a station sent (the scanner page of a camera-less station shows the last one)"""
    return send_from_directory(Config.IMAGES_DIR, name, max_age=3600)


@app.route('/api/last_capture')
def last_capture():
    """The picture the current desk's station sent last ({'url': None} when none since the server started)"""
    name = desk().last_capture
    return jsonify({'url': url_for('capture_image', name=name) if name else None})


@app.route('/video_feed')
def video_feed():
    """Video streaming route"""
    return Response(
        generate_frames(desk().scanner),  # ?station= names the camera
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )


@app.route('/api/stats')
def get_stats():
    """Get database and inventory statistics"""
    if database and inventory:
        game = games.active()
        return jsonify({
            'database': {'total_cards': game.card_count(), 'update': data_update_notices.get(game.id),
                         'updating': game.id in data_updates_running, 'error': data_update_errors.get(game.id)},
            'review': review.count(game.id, desk().review_filter) if review else 0,
            # The scanner page's counters: what was scanned and not moved to the collection yet
            'inventory': scan_stats(game.id),
            'collection': inventory.get_stats(game.id)
        })

    return jsonify({'error': 'Components not initialized'}), 500
