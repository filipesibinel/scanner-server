// Scanner page: camera status, capture and auto scanning, the card panel, the review queue,
// the scanned cards list and the settings drawer.
// Shared helpers (escapeHtml, dialogs, notify, the edit dialog, sorts) come from common.js.

const socket = io();
const $ = id => document.getElementById(id);

let cardNumber = 1;
let currentCard = null;
let lastDetectionStatus = {detected: false, stable_frames: 0, required_frames: 0, is_stable: false};  // from /api/detection_status
let detectionEnabled = true;
let autoScanningEnabled = false;
let fastScanMode = true;  // "Add cards automatically" - loaded from /api/scan_settings
let detectedFoilStatus = 'unknown';  // Foil marker read by the AI on the last capture: 'foil' | 'non-foil' | 'unknown'
let availableModels = {};  // provider -> model names
let currentProvider = 'gemini';
let currentModel = null;
let activeProvider = null;  // provider/model the server is actually using (not just selected)
let activeModel = null;

function isOpen(id) {
    return $(id).classList.contains('show');
}

function setCardPanel(html) {
    $('card-display').innerHTML = html;
}

function setButton(id, text, disabled = false) {
    const button = $(id);
    button.disabled = disabled;
    button.textContent = text;
}

// ============================================================================
// Connection and activity log
// ============================================================================

socket.on('connect', function() {
    audioManager.ensureAudioContext();
    loadStats();
    // The server closes a review when its page disconnects: open it again
    if (reviewItem) openReview();
    startDetectionPolling();
    loadScanSettings();
});

socket.on('log', function(data) {
    addLog(data.timestamp, data.level, data.message);
});

socket.on('error', function(data) {
    console.error('Server error:', data.message);
    audioManager.playError();

    if (isOpen('prompt-modal')) {
        notify(data.message, 'error');
    } else {
        addLog(timeNow(), 'error', data.message);
    }
});

function addLog(timestamp, level, message) {
    const logContainer = $('log-container');
    const logEntry = document.createElement('div');
    logEntry.className = 'log-entry';
    logEntry.innerHTML = `
        <span class="log-timestamp">[${timestamp}]</span>
        <span class="log-${level}">${escapeHtml(message)}</span>
    `;
    logContainer.appendChild(logEntry);
    logContainer.scrollTop = logContainer.scrollHeight;

    // Keep only last 100 log entries
    while (logContainer.children.length > 100) {
        logContainer.removeChild(logContainer.firstChild);
    }
}

// ============================================================================
// Detection Status
// ============================================================================

function detectionState(status) {
    // [status class, icon, text, capture allowed]
    if (status.camera_error) return ['status-stabilizing', '🚫', 'No camera', false];
    // Detection off: the capture button takes the full frame
    if (!detectionEnabled) return ['', '📷', 'Manual Mode - Click Capture', true];
    // Focus sweep in progress (Refocus button, or automatic when the card stays blurry)
    if (status.focusing) return ['status-stabilizing', '🎯', 'Focusing - finding the sharpest image...', false];
    // Image being taken (and, every few cards, the focus checked) - the beep says when to drop
    if (status.capturing) return ['status-stabilizing', '📸', 'Capturing - wait for the beep', false];
    // Auto scanning captured this card and waits for the next one
    if (status.awaiting_new_card) return ['status-locked', '✅', 'Captured - drop the next card', status.detected];
    if (!status.detected) return ['', '⚪', 'Waiting for card...', false];
    if (status.focus_locked) return ['status-locked', '🔵', 'LOCKED - Perfect!', true];
    if (status.is_stable) return ['status-ready', '🟢', 'Ready to Capture', true];
    return ['status-stabilizing', '🟠',
            status.in_focus === false ? 'Focusing...' : `Stabilizing ${status.stable_frames}/${status.required_frames}...`, true];
}

function updateDetectionStatus(status) {
    lastDetectionStatus = status;
    const [statusClass, icon, text, canCapture] = detectionState(status);
    const statusDiv = $('detection-status');
    statusDiv.classList.remove('status-detected', 'status-stabilizing', 'status-ready', 'status-locked');
    if (statusClass) statusDiv.classList.add(statusClass);
    statusDiv.querySelector('.status-icon').textContent = icon;
    statusDiv.querySelector('.status-text').textContent = text;
    $('capture-btn').disabled = !canCapture;
    // Without a camera the video box says why (the server keeps looking for it)
    $('camera-missing').hidden = !status.camera_error;
    $('camera-missing-reason').textContent = status.camera_error || '';
}

let detectionPolling = null;

function startDetectionPolling() {
    // Called on every (re)connect: one poll, every 500 ms
    if (detectionPolling) return;
    detectionPolling = setInterval(function() {
        fetch('/api/detection_status')
            .then(response => response.json())
            .then(updateDetectionStatus)
            .catch(error => console.error('Error polling detection status:', error));
    }, 500);
}

// ============================================================================
// Capture, auto scanning, focus
// ============================================================================

function captureFeedback() {
    // The beep and a flash on the video
    audioManager.playCapture().catch(error => console.error('Failed to play capture sound:', error));
    const videoContainer = document.querySelector('.video-container');
    videoContainer.classList.add('capture-flash');
    setTimeout(() => videoContainer.classList.remove('capture-flash'), 500);
}

function captureCard() {
    // With detection off the full frame is captured, whatever is in it
    if (detectionEnabled && !lastDetectionStatus.detected) {
        addLog(timeNow(), 'warning', 'No card detected. Please position card in frame.');
        return;
    }
    captureFeedback();
    socket.emit('capture_card', {card_number: cardNumber});
    cardNumber++;
}

socket.on('auto_capture_triggered', function(data) {
    // The image is taken: this beep is the signal to drop the next card
    captureFeedback();
    addLog(timeNow(), 'info', `📸 Card #${data.counter} captured${fastScanMode ? ' - drop the next card' : ''}`);
});

function autoScanHint() {
    return fastScanMode ? 'Auto scanning - adding cards automatically' : 'Auto scanning - confirm each card';
}

function toggleAutoScanning() {
    autoScanningEnabled = !autoScanningEnabled;

    const btn = $('toggle-auto-scanning-btn');
    const hint = $('auto-scan-hint');
    btn.classList.toggle('is-active', autoScanningEnabled);
    hint.classList.toggle('is-active', autoScanningEnabled);
    btn.innerHTML = `<svg class="icon"><use href="#i-play"/></svg> ${autoScanningEnabled ? 'Stop' : 'Start'} auto scanning`;
    hint.textContent = autoScanningEnabled ? autoScanHint() : 'Click to start automatic card scanning';

    socket.emit('toggle_auto_capture', {enabled: autoScanningEnabled});
    if (autoScanningEnabled) {
        addLog(timeNow(), 'success', `Auto scanning started${fastScanMode ? ' - adding cards automatically' : ' - confirm each card'}`);
    } else {
        addLog(timeNow(), 'info', 'Auto scanning stopped - captures in progress will complete');
    }
}

socket.on('processing_queue_update', function(data) {
    const queueCount = data.queue_count || 0;
    // Always shown (0 = nothing waiting); highlighted only when a backlog builds up
    $('processing-queue').textContent = queueCount;
    $('processing-queue-box').classList.toggle('is-busy', queueCount >= 3);
});

function resetFocus() {
    addLog(timeNow(), 'info', 'Resetting camera focus...');
    socket.emit('reset_focus');
}

socket.on('focus_reset', function(data) {
    addLog(timeNow(), 'info', data.message || 'Focusing...');
    // Refocusing locks the focus - reflect that in the settings switch
    $('toggle-autofocus').checked = false;
});

// ============================================================================
// Search and the card panel
// ============================================================================

function canSearch(name, setCode, number) {
    // A name, or only the set + number (a name the AI can't read: runes, another language)
    return Boolean(name || (number && (setCode || number.includes('/'))));
}

function clearSearchFields() {
    ['card-name', 'collector-number', 'set-code', 'card-treatment'].forEach(id => { $(id).value = ''; });
    $('search-btn').disabled = true;
}

function searchCard() {
    const cardName = $('card-name').value.trim();
    const collectorNumber = $('collector-number').value.trim();
    const setCode = $('set-code').value.trim().toUpperCase();
    const treatmentSelect = $('card-treatment');
    const treatment = treatmentSelect.value;

    if (!canSearch(cardName, setCode, collectorNumber)) {
        addLog(timeNow(), 'warning', 'Enter a card name, or the set and number');
        return;
    }

    const numberInfo = (setCode ? ` ${setCode}` : '') + (collectorNumber ? ` #${collectorNumber}` : '');
    const treatmentInfo = treatment ? ` (${treatmentSelect.options[treatmentSelect.selectedIndex].text})` : '';
    addLog(timeNow(), 'info', `Searching for: ${cardName}${numberInfo}${treatmentInfo}`);

    socket.emit('search_card', {
        card_name: cardName,
        collector_number: collectorNumber || null,
        set_code: setCode || null,
        treatment: treatment || null
    });
}

socket.on('card_captured', function(data) {
    // The capture sound is played at capture time, not here after the AI

    // Scanning goes on during a review: leave the review's fields alone
    if (reviewItem) return;

    detectedFoilStatus = data.foil || 'unknown';
    $('card-name').value = data.card_name;
    $('collector-number').value = data.collector_number || '';
    $('set-code').value = data.set_code || '';
    $('search-btn').disabled = false;
});

socket.on('card_found', function(data) {
    // Cards added automatically don't come here: the server adds them (inventory_updated)
    audioManager.playSuccess();
    currentCard = data.card;
    displayCard(data.card);
});

socket.on('card_not_found', function(data) {
    audioManager.playError();

    const message = data.message || `Card "${data.card_name}" not found in database.`;
    addLog(timeNow(), 'warning', message);
    setCardPanel(`
        <div class="empty-state is-error">
            ${escapeHtml(message)}<br>
            <span class="hint">${data.message ? 'Try a different treatment filter.' : 'Try a different name or check spelling.'}</span>
        </div>
    `);
    // Auto-dismiss after card not found to allow next auto-capture; the capture is kept for
    // a manual search
    setTimeout(function() {
        socket.emit('dismiss_card', {keep_capture: true});
    }, 2000);
});

socket.on('card_printings', function(data) {
    currentCard = null;
    displayPrintings(data.name, data.cards);
});

socket.on('similar_cards', function(data) {
    displaySimilarCards(data.cards);
});

socket.on('inventory_updated', function(data) {
    const added = data.added;
    loadStats();

    if (data.station) {
        // A card from a station (a camera elsewhere): it goes on in the background - the card
        // on this page stays, and the station makes its own sounds
        if (added) addLog(timeNow(), 'success', `[${data.station}] Added ${added.quantity}× ${added.name} (${added.finish})`);
        if (data.undone) addLog(timeNow(), 'warning', `[${data.station}] Removed ${data.undone} from the inventory (undo)`);
        if (isOpen('inventory-modal')) loadInventory();
        if ($('settings-drawer').classList.contains('show')) loadStations();
        return;
    }

    if (reviewItem) {
        // An automatic add while reviewing, or the reviewed card (the next item follows)
        if (added) addLog(timeNow(), 'success', `Added ${added.quantity}× ${added.name} (${added.finish})`);
        if (!data.auto) audioManager.playSuccess();
        return;
    }

    // The drop signal is the capture beep; adding (1-2 s later, after the AI) just dings
    audioManager.playSuccess();

    setCardPanel(`
        <div class="empty-state is-success">
            ✓ Added to inventory
            ${added ? `<div class="added-card">${added.quantity > 1 ? added.quantity + '× ' : ''}<strong>${escapeHtml(added.name)}</strong>
                <span class="hint">${escapeHtml(added.set)} · #${escapeHtml(added.number)} · ${added.finish}</span></div>
                <button class="btn btn-small" onclick="undoLastAdd()">Undo</button>` : ''}
            <span class="hint">Ready for the next card.</span>
        </div>
    `);
    clearSearchFields();
    detectedFoilStatus = 'unknown';
    currentCard = null;
});

// Prices fetched after an add (games with Game.fetches_prices): new totals, and the list if it is open
socket.on('inventory_prices_updated', function() {
    loadStats();
    if (isOpen('inventory-modal')) loadInventory();
});

socket.on('inventory_undone', function(data) {
    loadStats();
    addLog(timeNow(), 'warning', `Removed ${data.name} from the inventory (undo)`);
    setCardPanel(`
        <div class="empty-state">
            Removed <strong>${escapeHtml(data.name)}</strong> from the inventory.<br>
            <span class="hint">Ready for the next card.</span>
        </div>
    `);
});

function undoLastAdd() {
    socket.emit('undo_last_add');
}

function suggestedFinish(card, foilStatus = detectedFoilStatus) {
    // Same rule as Game.suggested_finish on the server (automatic adds)
    // Which finish the card in hand most likely is: 'regular' | 'foil' | 'surge', and why.
    // Printings that only exist in one finish are certain; otherwise use the ★/• marker
    // the AI read next to the set code on the last capture.
    if (!gameInfo || gameInfo.id !== 'mtg') {
        // Other games list the finishes the printing exists in; the plain one is the likely one
        const options = card.finish_options || [defaultFinish()];
        if (options.length === 1) return {finish: options[0], reason: `only printed as ${finishLabel(options[0]).toLowerCase()}`};
        return {finish: options.includes(defaultFinish()) ? defaultFinish() : options[0], reason: null};
    }
    const finishes = card.finishes || [];
    const hasFoil = finishes.includes('foil') || finishes.includes('etched');
    const hasNonfoil = finishes.includes('nonfoil');
    const foilKind = (card.treatments || []).includes('Surge Foil') ? 'surge' : 'foil';

    if (hasFoil && !hasNonfoil) return {finish: foilKind, reason: 'only printed in foil'};
    if (hasNonfoil && !hasFoil) return {finish: 'regular', reason: 'only printed non-foil'};
    if (foilStatus === 'foil') return {finish: foilKind, reason: '★ next to the set code'};
    if (foilStatus === 'non-foil') return {finish: 'regular', reason: '• next to the set code'};
    return {finish: 'regular', reason: null};
}

function cardFinishes(card) {
    // The game's finishes, only those the printing exists in when the game says
    return card.finish_options ? gameInfo.finishes.filter(([key]) => card.finish_options.includes(key)) : gameInfo.finishes;
}

function treatmentTagsHtml(treatments) {
    return '<span class="treatment-tags">' +
        treatments.map(t => `<span class="treatment-tag">${escapeHtml(t)}</span>`).join('') +
        '</span>';
}

function quantityCell(id, label, kind, value = 0) {
    return `
        <div class="qty-cell ${kind}">
            <span class="qty-label"><span class="qty-dot"></span>${label}</span>
            <div class="qty-stepper">
                <button onclick="adjustQtyInput('${id}', -1)" aria-label="Decrease ${label}">−</button>
                <input type="number" id="${id}" value="${value}" min="0" max="999" aria-label="${label} quantity">
                <button onclick="adjustQtyInput('${id}', 1)" aria-label="Increase ${label}">+</button>
            </div>
        </div>
    `;
}

function adjustQtyInput(inputId, delta) {
    const input = $(inputId);
    input.value = Math.max(0, Math.min(999, (parseInt(input.value) || 0) + delta));
}

function cardPricesHtml(card) {
    // Only prices that exist (e.g. foil-only printings have no regular price)
    const prices = [];
    if (card.prices) {
        // [[finish label, price]] (games other than Magic)
        card.prices.forEach(([label, price]) => prices.push(
            `<span class="price-label">${escapeHtml(label.toLowerCase())}</span> <span class="price">$${price.toFixed(2)}</span>`));
    } else {
        if (card.price > 0) {
            prices.push(`<span class="price">$${card.price.toFixed(2)}</span>`);
        }
        if (card.price_foil > 0) {
            prices.push(`<span class="price-label">foil</span> <span class="price">$${card.price_foil.toFixed(2)}</span>`);
        }
    }
    if (prices.length === 0) {
        prices.push('<span class="price-label">No price data</span>');
    }
    return prices.join('<span class="price-sep">·</span>');
}

function displayCard(card) {
    const suggestion = suggestedFinish(card);
    setCardPanel(`
        <div class="card-summary">
            ${card.image_uri ? `<img src="${escapeHtml(card.image_uri)}" alt="${escapeHtml(card.name)}" class="card-image">` : ''}
            <div class="card-facts">
                <div class="card-title">${escapeHtml(card.name)}</div>
                <div class="card-meta">${escapeHtml(card.set)} · #${escapeHtml(card.number)}</div>
                <div class="card-meta"><span class="card-rarity">${escapeHtml(card.rarity)}</span> · ${escapeHtml(card.type)}</div>
                ${card.treatments && card.treatments.length ? treatmentTagsHtml(card.treatments) : ''}
                ${card.confirmed === false ? '<div class="card-warning">Printing not confirmed - check the set and number</div>' : ''}
                <div class="card-prices">${cardPricesHtml(card)}</div>
            </div>
        </div>

        <div class="input-group">
            <label for="condition">Condition</label>
            <select id="condition">
                <option value="Mint">Mint (M)</option>
                <option value="Near Mint" selected>Near Mint (NM)</option>
                <option value="Excellent">Excellent (EX)</option>
                <option value="Good">Good (GD)</option>
                <option value="Played">Played (PL)</option>
                <option value="Poor">Poor (P)</option>
            </select>
        </div>

        <div class="input-group">
            <span class="field-label">Quantity</span>
            <div class="qty-grid">
                ${cardFinishes(card).map(([key, label]) => quantityCell(`qty-${key}`, label, key, suggestion.finish === key ? 1 : 0)).join('')}
            </div>
            ${suggestion.reason ? `<div class="finish-hint ${suggestion.finish}">${finishLabel(suggestion.finish)}: ${suggestion.reason}</div>` : ''}
        </div>

        <div class="card-actions">
            <button class="btn btn-success" onclick="addToInventoryBoth()">Add to inventory</button>
            <button class="btn" onclick="dismissCard()">Skip</button>
        </div>
    `);
}

function displayPrintings(cardName, cards) {
    const printings = cards.map(card => {
        // Scryfall's "small" image size keeps the grid light
        const thumb = card.thumb_uri || (card.image_uri ? card.image_uri.replace('/normal/', '/small/') : '');
        const price = card.price > 0 ? `$${card.price.toFixed(2)}` : (card.price_foil > 0 ? `$${card.price_foil.toFixed(2)} foil` : 'N/A');
        return `
            <div class="printing-card" onclick="selectPrinting('${escapeHtml(card.id)}')">
                ${thumb ? `<img src="${escapeHtml(thumb)}" alt="${escapeHtml(card.name)}" loading="lazy">` : ''}
                <strong>${escapeHtml(card.set)}</strong><br>
                <span class="meta">#${escapeHtml(card.number)} &middot; ${price}</span>
                ${card.treatments.length ? treatmentTagsHtml(card.treatments) : ''}
            </div>
        `;
    });
    setCardPanel(`<div class="similar-cards"><div class="list-heading">${escapeHtml(cardName)}</div>
        <div class="list-subheading">${cards.length} printings - pick the one you have</div>
        <div class="printing-grid">${printings.join('')}</div></div>`);
}

function selectPrinting(cardId) {
    socket.emit('select_printing', {id: cardId});
}

function displaySimilarCards(cards) {
    const similar = cards.map(card => `
        <div class="similar-card" onclick="selectSimilarCard('${escapeHtml(card.name.replace(/'/g, "\\'"))}')">
            <strong>${escapeHtml(card.name)}</strong><br>
            <small>${escapeHtml(card.set)} · ${card.price}</small>
        </div>
    `);
    setCardPanel('<div class="similar-cards"><div class="list-heading">No exact match</div><div class="list-subheading">Did you mean:</div>'
        + (similar.length ? similar.join('') : '<div class="empty-state">No similar cards found.</div>')
        + '</div>');
}

function selectSimilarCard(cardName) {
    if (reviewItem) {
        $('review-name').value = cardName;
        reviewSearch();
        return;
    }
    $('card-name').value = cardName;
    searchCard();
}

function addToInventoryBoth() {
    if (!currentCard) {
        addLog(timeNow(), 'error', 'No card selected');
        return;
    }

    // One entry per finish with a quantity, sent together
    const items = cardFinishes(currentCard)
        .map(([finish]) => ({finish, quantity: parseInt($(`qty-${finish}`).value) || 0}))
        .filter(item => item.quantity > 0);
    if (items.length === 0) {
        addLog(timeNow(), 'warning', 'Please set at least one quantity');
        return;
    }
    socket.emit('add_to_inventory', {condition: $('condition').value, items: items});
}

function dismissCard() {
    if (reviewItem) {
        socket.emit('review_skip');  // drops the item; the next one follows
        return;
    }
    currentCard = null;
    detectedFoilStatus = 'unknown';

    // The server re-enables auto-capture
    socket.emit('dismiss_card');

    setCardPanel(`
        <div class="empty-state">
            Card skipped.<br>
            Ready to scan the next card.
        </div>
    `);
    addLog(timeNow(), 'info', 'Card dismissed - ready for next card');
}

// ============================================================================
// Review queue: cards not added automatically, reviewed one by one at the end
// ============================================================================

let reviewItem = null;  // the item open in the card panel

function setReviewCount(count) {
    $('review-count').textContent = count;
    $('review-box').classList.toggle('has-items', count > 0);
}

function openReview() {
    socket.emit('review_open');
}

socket.on('review_queue_update', function(data) {
    setReviewCount(data.count);
    // A card just went to the review queue (not a review resolved): three beeps, as in the Android app
    if (data.queued) audioManager.playQueueAlert();
});

socket.on('review_item', function(data) {
    if (!data.id) {
        const wasReviewing = !!reviewItem;
        hideReviewPanel();
        if (wasReviewing) {
            setCardPanel('<div class="empty-state is-success">✓ Review queue done.<br><span class="hint">Every card was added or skipped.</span></div>');
        } else {
            notify('Nothing to review', 'info');
        }
        setReviewCount(0);
        return;
    }
    renderReview(data);
});

function renderReview(item) {
    reviewItem = item;
    setReviewCount(item.total);
    const ai = item.ai;
    const read = [ai.name || 'no name', ai.number ? '#' + ai.number : '', ai.set,
                  ai.foil === 'foil' ? '★' : ai.foil === 'non-foil' ? '•' : ''].filter(Boolean).join(' · ');
    const panel = $('review-panel');
    panel.innerHTML = `
        <div class="review-header">
            <strong>Review</strong> <span class="hint">1 of ${item.total}</span>
            <div class="review-actions">
                <button class="btn btn-small btn-danger" onclick="deleteReviewItem()" title="Remove this card from the queue without adding it">Delete</button>
                <button class="btn btn-small" onclick="closeReview()" title="Keep the rest for later">Close</button>
            </div>
        </div>
        <div class="review-body">
            ${item.image_url ? `<img class="review-capture" src="${escapeHtml(item.image_url)}" alt="Capture" onclick="zoomReviewCapture()">`
                             : '<div class="review-capture empty">No image</div>'}
            <div class="review-side">
                <div class="field-label">AI read</div>
                <div class="review-read">${escapeHtml(read)}</div>
                <div class="hint">${escapeHtml(item.captured_at)}${item.station ? ' · from ' + escapeHtml(item.station) : ''}</div>
                <div class="review-search">
                    <input type="text" id="review-name" placeholder="Card name" value="${escapeHtml(ai.name || '')}">
                    <input type="text" id="review-set" placeholder="Set" value="${escapeHtml(ai.set || '')}" maxlength="5">
                    <input type="text" id="review-number" placeholder="Number" value="${escapeHtml(ai.number || '')}">
                    <button class="btn btn-small" onclick="reviewSearch()">Search</button>
                </div>
            </div>
        </div>`;
    panel.hidden = false;
    panel.querySelectorAll('input').forEach(input =>
        input.addEventListener('keydown', e => { if (e.key === 'Enter') reviewSearch(); }));

    detectedFoilStatus = ai.foil || 'unknown';
    if (item.card) {
        currentCard = item.card;
        displayCard(item.card);
    } else if (ai.name) {
        reviewSearch();  // no match kept: show what a search finds (printings / similar)
    } else {
        currentCard = null;
        setCardPanel('<div class="empty-state">The AI couldn\'t read this card - search for it above, or Skip.' +
            '<div class="card-actions"><button class="btn" onclick="dismissCard()">Skip</button></div></div>');
    }
}

function reviewSearch() {
    const name = $('review-name').value.trim();
    const setCode = $('review-set').value.trim().toUpperCase();
    const number = $('review-number').value.trim();
    if (!canSearch(name, setCode, number)) {
        notify('Enter a name, or the set and number', 'info');
        return;
    }
    socket.emit('search_card', {card_name: name, set_code: setCode || null, collector_number: number || null});
}

function deleteReviewItem() {
    // Drops the item and its capture, then the next one opens (same as Skip)
    socket.emit('review_skip');
}

function zoomReviewCapture() {
    $('capture-title').textContent = 'Capture';
    $('capture-grid').innerHTML =
        `<figure><img src="${escapeHtml(reviewItem.image_url)}" alt="Capture"><figcaption>${escapeHtml(reviewItem.captured_at)}</figcaption></figure>`;
    $('capture-modal').classList.add('show');
}

function hideReviewPanel() {
    reviewItem = null;
    const panel = $('review-panel');
    panel.hidden = true;
    panel.innerHTML = '';
}

function closeReview() {
    socket.emit('review_close');
    hideReviewPanel();
    currentCard = null;
    setCardPanel('<div class="empty-state">Review closed - the rest stays in the queue.</div>');
}

// ============================================================================
// Scan settings remembered on the server
// ============================================================================

// "Read with OCR first": without the light-ocr package the switch does nothing, so say why
const OCR_DESC = $('ocr-desc').textContent;

function applyOcrState(enabled, installed) {
    const toggle = $('toggle-ocr');
    toggle.checked = Boolean(enabled) && installed;
    toggle.disabled = !installed;
    $('ocr-desc').textContent = installed ? OCR_DESC
        : 'Not installed: needs Node.js 22+ and "npm install" in the ocr folder (scripts/deploy.sh does it)';
}

socket.on('ocr_toggled', data => applyOcrState(data.enabled, data.installed));

// "Debug mode" is the web server's: the switch is what the next start will use
const DEBUG_MODE_DESC = $('debug-mode-desc').textContent;

function applyDebugMode(enabled, running) {
    $('toggle-debug-mode').checked = Boolean(enabled);
    $('debug-mode-desc').textContent = Boolean(enabled) === Boolean(running) ? DEBUG_MODE_DESC
        : `Now ${running ? 'on' : 'off'}: restart the scanner to turn it ${enabled ? 'on' : 'off'}`;
}

socket.on('debug_mode_toggled', data => applyDebugMode(data.enabled, data.running));

function loadScanSettings() {
    fetch('/api/scan_settings')
        .then(response => response.json())
        .then(data => {
            fastScanMode = data.auto_add;
            $('toggle-fast-scan').checked = data.auto_add;
            $('toggle-autofocus').checked = data.autofocus;
            applyOcrState(data.ocr_first, data.ocr_installed);
            $('toggle-debug-trace').checked = Boolean(data.debug_trace);
            applyDebugMode(data.debug_mode, data.debug_mode_running);
            addLog(timeNow(), 'info', `Debug trace: ${data.debug_trace ? 'on' : 'off'}, debug mode: ${data.debug_mode_running ? 'on' : 'off'}`);
            applyFixedArea({enabled: data.fixed_area_enabled, area: data.fixed_area});
            $('camera-rotation').value = String(data.camera_rotation || 0);
            $('refocus-every').value = String(data.refocus_every);
            applySound(data.sound);
            $('scan-location').value = data.scan_location || '';
            $('scan-location-options').innerHTML = (data.locations || [])
                .map(location => `<option value="${escapeHtml(location)}"></option>`).join('');
        })
        .catch(error => console.error('Error loading scan settings:', error));
}

function applySound(sound) {
    // The sound switch and volume as remembered on the server
    if (!sound) return;
    $('toggle-audio').checked = sound.enabled;
    $('audio-volume').value = sound.volume;
    $('volume-value').textContent = sound.volume + '%';
    audioManager.setEnabled(sound.enabled);
    audioManager.setVolume(sound.volume / 100);
}

function saveSound(change) {
    fetch('/api/sound', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(change)
    }).catch(error => console.error('Could not save the sound setting:', error));
}

function setScanLocation(location) {
    // Where scanned cards are put in the inventory (remembered on the server)
    fetch('/api/scan_location', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({location: location})
    })
    .then(response => response.json())
    .then(data => { $('scan-location').value = data.scan_location; })
    .catch(error => notify('Could not save the location: ' + error.message, 'error'));
}

// ============================================================================
// Fixed capture area (sleeved cards): drawn on the video, judged by image changes
// ============================================================================

let fixedArea = {enabled: false, area: null};
let areaDrag = null;

function applyFixedArea(state) {
    fixedArea = state;
    $('fixed-area-toggle').checked = Boolean(state.enabled);
}

socket.on('fixed_area_updated', function(state) {
    const wasEnabled = fixedArea.enabled;
    applyFixedArea(state);
    if (state.enabled !== wasEnabled) {
        notify(state.enabled ? 'Fixed area on - cards are judged by the drawn area' : 'Fixed area off - cards are found by their outline', 'info');
    }
});

socket.on('refocus_every_updated', function(data) {
    $('refocus-every').value = String(data.captures);
});

socket.on('camera_rotation_updated', function(data) {
    $('camera-rotation').value = String(data.rotation);
    notify(`Camera image rotated ${data.rotation}°` + (data.fixed_area_off ? ' - fixed area off, draw it again' : ''), 'info');
});

function imageContentRect() {
    // Where the video is drawn inside its box (object-fit: contain may leave bars)
    const img = $('video-feed');
    const box = img.getBoundingClientRect();
    const ratio = (img.naturalWidth && img.naturalHeight) ? img.naturalWidth / img.naturalHeight : 16 / 9;
    let width = box.width, height = box.width / ratio;
    if (height > box.height) { height = box.height; width = height * ratio; }
    const parent = img.parentElement.getBoundingClientRect();
    return {left: box.left - parent.left + (box.width - width) / 2, top: box.top - parent.top + (box.height - height) / 2, width, height};
}

function placeAreaLayer() {
    const rect = imageContentRect();
    Object.assign($('area-layer').style, {left: rect.left + 'px', top: rect.top + 'px', width: rect.width + 'px', height: rect.height + 'px'});
}

function updateVideoOrientation() {
    // A rotated camera gives a portrait image: show it upright instead of letterboxed
    const img = $('video-feed');
    if (img.naturalWidth && img.naturalHeight) {
        img.parentElement.classList.toggle('portrait', img.naturalHeight > img.naturalWidth);
    }
}

function isDrawingArea() {
    return $('area-layer').classList.contains('is-drawing');
}

function startDrawArea() {
    placeAreaLayer();
    $('area-layer').classList.add('is-drawing');
    $('area-hint').hidden = false;
}

function cancelDrawArea() {
    $('area-layer').classList.remove('is-drawing');
    $('area-hint').hidden = true;
    $('area-rect').hidden = true;
    areaDrag = null;
    $('fixed-area-toggle').checked = Boolean(fixedArea.enabled);
}

function useDetectedArea() {
    socket.emit('set_fixed_area', {use_detected: true, enabled: true});
    cancelDrawArea();
}

function areaPoint(event) {
    const box = $('area-layer').getBoundingClientRect();
    return {x: Math.min(1, Math.max(0, (event.clientX - box.left) / box.width)),
            y: Math.min(1, Math.max(0, (event.clientY - box.top) / box.height))};
}

function drawAreaRect(a, b) {
    const rect = $('area-rect');
    Object.assign(rect.style, {
        left: Math.min(a.x, b.x) * 100 + '%', top: Math.min(a.y, b.y) * 100 + '%',
        width: Math.abs(a.x - b.x) * 100 + '%', height: Math.abs(a.y - b.y) * 100 + '%'
    });
    rect.hidden = false;
}

function setupAreaDrawing() {
    const layer = $('area-layer');
    layer.addEventListener('pointerdown', event => {
        areaDrag = areaPoint(event);
        layer.setPointerCapture(event.pointerId);
        drawAreaRect(areaDrag, areaDrag);
    });
    layer.addEventListener('pointermove', event => {
        if (areaDrag) drawAreaRect(areaDrag, areaPoint(event));
    });
    layer.addEventListener('pointerup', event => {
        if (!areaDrag) return;
        const a = areaDrag, b = areaPoint(event);
        areaDrag = null;
        if (Math.abs(a.x - b.x) < 0.05 || Math.abs(a.y - b.y) < 0.05) {
            notify('Drag a rectangle around the card', 'warning');
            $('area-rect').hidden = true;
            return;
        }
        socket.emit('set_fixed_area', {
            area: [Math.min(a.x, b.x), Math.min(a.y, b.y), Math.max(a.x, b.x), Math.max(a.y, b.y)],
            enabled: true
        });
        cancelDrawArea();
    });
    $('fixed-area-toggle').addEventListener('change', event => {
        if (event.target.checked && !fixedArea.area) {
            startDrawArea();  // nothing to turn on yet: draw the area first
            return;
        }
        socket.emit('set_fixed_area', {enabled: event.target.checked});
    });
    window.addEventListener('resize', () => {
        if (isDrawingArea()) placeAreaLayer();
    });
}

// ============================================================================
// Top bar counters and card data updates
// ============================================================================

function loadStats() {
    fetch('/api/stats')
        .then(response => response.json())
        .then(data => {
            if (data.database) {
                $('db-cards').textContent = data.database.total_cards.toLocaleString();
                showDataUpdate(data.database.update);
            }
            setReviewCount(data.review || 0);
            if (data.inventory) {
                $('inv-cards').textContent = data.inventory.total_cards.toLocaleString();
                $('total-value').textContent = '$' + data.inventory.total_value.toFixed(2);
            }
        })
        // Not shown in the activity log - expected when the server is down
        .catch(error => console.error('Error loading stats:', error));
}

let dataUpdateNotified = false;

function showDataUpdate(message) {
    // Newer card data available (checked at startup and daily): a dot on the Database
    // counter, which then offers the update; one notification per page load
    const box = $('db-stat');
    box.classList.toggle('has-update', !!message);
    box.classList.toggle('clickable', !!message);
    box.title = message ? `${message} - click to update` : '';
    if (message && !dataUpdateNotified) {
        dataUpdateNotified = true;
        notify(`Card data update available: ${message}`, 'info');
    }
}

async function offerDataUpdate() {
    const box = $('db-stat');
    if (!box.classList.contains('has-update')) return;
    const ok = await confirmDialog({
        title: `Update the ${gameInfo.label} card data?`,
        message: `${box.title.replace(/ - click to update$/, '')}. Scanning keeps working while it downloads.`,
        confirmText: 'Update'
    });
    if (ok) updateDatabase();
}

socket.on('database_update_available', function(data) {
    if (gameInfo && data.game === gameInfo.id) showDataUpdate(data.message);
});

function updateDatabase() {
    setButton('update-database-btn', 'Updating...', true);
    addLog(timeNow(), 'info', `Starting the ${gameInfo.label} card data update from ${gameInfo.source}...`);
    socket.emit('update_database');
}

socket.on('database_update_progress', function(data) {
    addLog(timeNow(), 'info', data.message);
});

socket.on('database_update_complete', function(data) {
    audioManager.playQueueAlert();  // triple beep: a long operation finished
    setButton('update-database-btn', 'Update card database');
    addLog(timeNow(), 'success', `Database updated! ${data.total_cards.toLocaleString()} cards loaded.`);
    loadStats();
});

socket.on('database_update_error', function(data) {
    audioManager.playError();
    setButton('update-database-btn', 'Update card database');
    addLog(timeNow(), 'error', `Database update failed: ${data.message}`);
});

async function rebuildDatabase() {
    const ok = await confirmDialog({
        title: 'Rebuild database schema?',
        message: 'Reorders the card table and rebuilds its indexes (about 30 seconds). Your inventory is not touched.',
        confirmText: 'Rebuild'
    });
    if (!ok) return;

    setButton('rebuild-database-btn', 'Rebuilding...', true);
    addLog(timeNow(), 'info', 'Starting database schema rebuild...');
    addLog(timeNow(), 'info', 'This will optimize database structure and indexes (~30 seconds)');
    socket.emit('rebuild_database');
}

socket.on('database_rebuild_progress', function(data) {
    addLog(timeNow(), 'info', data.message);
});

socket.on('database_rebuild_complete', function(data) {
    audioManager.playQueueAlert();  // triple beep: a long operation finished
    setButton('rebuild-database-btn', 'Rebuild database schema');
    addLog(timeNow(), 'success', `Database rebuilt! ${data.cards_imported.toLocaleString()} cards migrated.`);
    addLog(timeNow(), 'success', `Schema optimized: ${data.schema_type} (with performance indexes)`);
    addLog(timeNow(), 'success', 'Database queries will now be faster!');
});

socket.on('database_rebuild_error', function(data) {
    audioManager.playError();
    setButton('rebuild-database-btn', 'Rebuild database schema');
    addLog(timeNow(), 'error', `Database rebuild failed: ${data.message}`);
});

// ============================================================================
// Vision AI: provider and model (Settings -> Vision AI)
// ============================================================================

function fetchAIModels() {
    return fetch('/api/ai_models')
        .then(response => response.json())
        .then(data => { availableModels = data.models; });
}

function loadAIModels() {
    return fetchAIModels().catch(error => console.error('Error loading AI models:', error));
}

function refreshAIModels() {
    const provider = currentProvider;
    setButton('refresh-models-btn', '⏳', true);
    $('ai-model').innerHTML = '<option value="">Refreshing models...</option>';
    addLog(timeNow(), 'info', `Refreshing ${provider} models...`);

    // Local models come live from Ollama; cloud providers have a static list
    const refreshed = provider === 'local' ? loadLocalModels(currentModel) : fetchAIModels()
        .then(() => {
            populateModelDropdown(provider, currentModel);
            addLog(timeNow(), 'success', `${provider} models refreshed`);
        })
        .catch(error => {
            console.error('Error refreshing models:', error);
            addLog(timeNow(), 'error', 'Failed to refresh models');
        });
    refreshed.finally(() => setButton('refresh-models-btn', '🔄'));
}

function populateModelDropdown(provider, selectedModel = null) {
    const modelSelect = $('ai-model');
    modelSelect.innerHTML = '';
    const models = availableModels[provider];
    if (!models) {
        modelSelect.add(new Option('No models available', ''));
        return;
    }
    models.forEach(model => modelSelect.add(new Option(model, model, false, model === selectedModel)));
}

function loadLocalModels(selectedModel = null) {
    $('ai-model').innerHTML = '<option value="">Loading models from Ollama...</option>';

    // Live models from Ollama; the static list when the server can't be reached
    return fetch('/api/local_ai_models')
        .then(response => response.json())
        .then(data => {
            if (data.success && data.models && data.models.length > 0) {
                availableModels['local'] = data.models;
                addLog(timeNow(), 'success', `Found ${data.models.length} local models`);
            } else {
                addLog(timeNow(), 'warning', 'Using static model list (local server not available)');
            }
        })
        .catch(error => {
            console.error('Error loading local models:', error);
            addLog(timeNow(), 'warning', 'Could not connect to local AI server');
        })
        .then(() => populateModelDropdown('local', selectedModel));
}

function setActiveModel(provider, model) {
    currentProvider = provider;
    currentModel = model;
    activeProvider = provider;
    activeModel = model;
}

function applyProvider(provider) {
    // Load the provider's models and switch the scanner to it; returning to the active
    // provider keeps its model instead of jumping to the first one in the list
    const keepModel = provider === activeProvider ? activeModel : null;
    const loaded = provider === 'local' ? loadLocalModels(keepModel) : Promise.resolve(populateModelDropdown(provider, keepModel));
    loaded.then(() => {
        socket.emit('set_ai_provider', {provider: provider, model: $('ai-model').value});
        addLog(timeNow(), 'info', `Changing AI provider to ${provider}...`);
    });
}

function loadAIProvider() {
    return fetch('/api/ai_provider')
        .then(response => response.json())
        .then(data => {
            if (!data.provider) return;
            $('ai-provider').value = data.provider;
            setActiveModel(data.provider, data.model);
            if (data.provider === 'local') {
                loadLocalModels(data.model);
            } else {
                populateModelDropdown(data.provider, data.model);
            }
        })
        .catch(error => console.error('Error loading AI provider:', error));
}

socket.on('ai_provider_set', function(data) {
    setActiveModel(data.provider, data.model);
    addLog(timeNow(), 'success', data.message);
    loadPrompts();
});

// ============================================================================
// API keys / local endpoint (Settings -> Vision AI)
// Keys are shown masked only: the server never sends a full key to the browser.
// ============================================================================

let aiCredentials = {};
const PROVIDER_INFO = {
    gemini: {name: 'Google Gemini', keyUrl: 'https://aistudio.google.com/app/apikey'},
    openai: {name: 'OpenAI', keyUrl: 'https://platform.openai.com/api-keys'},
    anthropic: {name: 'Anthropic', keyUrl: 'https://console.anthropic.com/settings/keys'},
    local: {name: 'Local AI'}
};

function providerInfo(provider) {
    return PROVIDER_INFO[provider] || {name: provider};
}

function loadCredentials() {
    return fetch('/api/ai_credentials')
        .then(response => response.json())
        .then(data => {
            aiCredentials = data;
            renderCredentialField($('ai-provider').value);
        })
        .catch(error => console.error('Error loading API key status:', error));
}

function hasCredential(provider) {
    return provider === 'local' || Boolean(aiCredentials[provider] && aiCredentials[provider].configured);
}

function renderCredentialField(provider) {
    const input = $('ai-credential');
    const label = $('ai-credential-label');
    const hint = $('ai-credential-hint');
    const info = providerInfo(provider);
    const status = aiCredentials[provider] || {};
    input.value = '';
    hint.classList.remove('is-missing');

    if (provider === 'local') {
        label.textContent = 'Server address';
        input.type = 'text';
        input.value = status.endpoint || '';
        input.placeholder = 'http://localhost:11434/v1/chat/completions';
        hint.textContent = 'Ollama or another OpenAI-compatible server';
        return;
    }

    label.textContent = `${info.name} API key`;
    input.type = 'password';
    if (status.configured) {
        input.placeholder = `Saved: ${status.masked} - type a new key to replace it`;
        hint.textContent = `Key configured (${status.masked})`;
    } else {
        input.placeholder = 'Paste your API key';
        hint.innerHTML = `No key yet - <a href="${info.keyUrl}" target="_blank" rel="noopener">get one from ${escapeHtml(info.name)}</a>`;
        hint.classList.add('is-missing');
    }
}

async function saveCredential() {
    const provider = $('ai-provider').value;
    const value = $('ai-credential').value.trim();

    if (!value) {
        if (provider === 'local' || !hasCredential(provider)) {
            notify(provider === 'local' ? 'Enter the server address' : 'Paste an API key first', 'warning');
            return;
        }
        const remove = await confirmDialog({
            title: `Remove the ${providerInfo(provider).name} key?`,
            message: 'Removes the key saved in the web interface. A key in the .env file (if any) is used again after a restart.',
            confirmText: 'Remove key',
            danger: true
        });
        if (!remove) return;
    }
    socket.emit('save_ai_credential', {provider: provider, value: value});
}

socket.on('ai_credential_saved', function(data) {
    aiCredentials[data.provider] = data.status;
    const info = providerInfo(data.provider);
    const selected = $('ai-provider').value;
    renderCredentialField(selected);

    if (data.provider === 'local') {
        notify('Local AI address saved', 'success');
    } else {
        notify(data.status.configured ? `${info.name} API key saved` : `${info.name} API key removed`, 'success');
    }
    // Use the new key / address right away for the selected provider
    if (data.provider === selected && hasCredential(selected)) {
        applyProvider(selected);
    }
});

// ============================================================================
// Prompt editor (Settings -> Vision AI -> Edit prompts)
// ============================================================================

let promptData = null;      // {provider, model, prompts: {kind: {label, instructions, source, ...}}}
let promptKind = 'identify';
let promptDrafts = {};      // kind -> edited text not saved yet

const PROMPT_SOURCES = {
    'built-in': 'Built-in prompt',
    'all': 'Saved for all models',
    'model': 'Saved for this model'
};

function loadPrompts() {
    return fetch('/api/prompts')
        .then(response => response.json())
        .then(data => {
            promptData = data;
            updatePromptSummary();
            if (isOpen('prompt-modal')) renderPromptEditor();
        })
        .catch(error => console.error('Error loading prompts:', error));
}

function updatePromptSummary() {
    const custom = promptData.order.map(kind => promptData.prompts[kind]).filter(p => p.source !== 'built-in');
    $('prompt-summary').textContent = custom.length
        ? custom.map(p => `${p.label}: ${PROMPT_SOURCES[p.source].toLowerCase()}`).join(' · ')
        : 'What the AI is asked to read on each card - adjustable per model';
}

function promptText(kind) {
    return kind in promptDrafts ? promptDrafts[kind] : promptData.prompts[kind].instructions;
}

function editedPromptText() {
    // The text in the editor, or null (with a notice) when it is empty
    const text = $('prompt-text').value.trim();
    if (!text) notify('The prompt is empty', 'warning');
    return text || null;
}

function openPromptEditor() {
    loadPrompts().then(() => {
        if (!promptData) return;
        $('prompt-test').hidden = true;
        $('prompt-modal').classList.add('show');
        renderPromptEditor();
    });
}

async function closePromptEditor() {
    const edited = Object.keys(promptDrafts).length > 0;
    if (edited && !await confirmDialog({
        title: 'Discard changes?',
        message: 'The edited prompt has not been saved.',
        confirmText: 'Discard',
        danger: true
    })) return;
    promptDrafts = {};
    $('prompt-modal').classList.remove('show');
}

function renderPromptEditor() {
    const kinds = promptData.prompts;
    if (!(promptKind in kinds)) promptKind = promptData.order[0];

    $('prompt-model').textContent = promptData.model
        ? `In use with ${promptData.provider} / ${promptData.model}`
        : 'No AI model active - prompts can only be saved for all models';

    $('prompt-kinds').innerHTML = promptData.order.map(kind => `
        <label class="radio-pill">
            <input type="radio" name="prompt-kind" value="${kind}" ${kind === promptKind ? 'checked' : ''} onchange="selectPromptKind('${kind}')">
            <span>${escapeHtml(kinds[kind].label)}${kind in promptDrafts ? ' *' : ''}</span>
        </label>`).join('');

    const textarea = $('prompt-text');
    if (textarea.value !== promptText(promptKind)) textarea.value = promptText(promptKind);
    $('prompt-format').textContent = kinds[promptKind].answer_format;
    $('prompt-save-model-btn').disabled = !promptData.model;
    renderPromptSource();
}

function renderPromptSource() {
    const prompt = promptData.prompts[promptKind];
    const edited = promptKind in promptDrafts;
    const tag = `<span class="source-tag ${prompt.source === 'built-in' ? '' : 'is-custom'}">${PROMPT_SOURCES[prompt.source]}</span>`;
    const note = edited ? '<span class="source-tag is-edited">Edited - not saved</span>'
        : prompt.source === 'model' && prompt.has_all_models ? 'overrides the prompt saved for all models' : '';
    $('prompt-source').innerHTML = tag + note;
    $('prompt-reset-btn').disabled = prompt.source === 'built-in' && !edited;
}

function selectPromptKind(kind) {
    promptKind = kind;
    $('prompt-test').hidden = true;
    renderPromptEditor();
}

function onPromptInput() {
    const text = $('prompt-text').value;
    const wasEdited = promptKind in promptDrafts;
    if (text === promptData.prompts[promptKind].instructions) {
        delete promptDrafts[promptKind];
    } else {
        promptDrafts[promptKind] = text;
    }
    // Redraw the tabs only when the unsaved-changes marker appears or disappears
    if (wasEdited !== (promptKind in promptDrafts)) renderPromptEditor();
    else renderPromptSource();
}

function savePrompt(scope) {
    const text = editedPromptText();
    if (text) socket.emit('save_prompt', {kind: promptKind, text: text, scope: scope});
}

async function resetPrompt() {
    const prompt = promptData.prompts[promptKind];
    if (prompt.source === 'built-in') {
        // Nothing saved - just discard the edits
        delete promptDrafts[promptKind];
        renderPromptEditor();
        return;
    }
    const fallback = prompt.source === 'model' && prompt.has_all_models ? 'the prompt saved for all models' : 'the built-in prompt';
    const message = prompt.source === 'model'
        ? `Removes the prompt saved for ${promptData.provider} / ${promptData.model}. It will use ${fallback}.`
        : 'Removes the prompt saved for all models. Models without their own prompt will use the built-in prompt.';
    if (!await confirmDialog({title: 'Restore default?', message: message, confirmText: 'Remove saved prompt', danger: true})) return;
    delete promptDrafts[promptKind];
    socket.emit('reset_prompt', {kind: promptKind});
}

socket.on('prompts_updated', function(data) {
    delete promptDrafts[promptKind];
    promptData = data;
    updatePromptSummary();
    if (isOpen('prompt-modal')) {
        $('prompt-text').value = '';  // force a refresh with the saved text
        renderPromptEditor();
    }
    notify(data.message, 'success');
});

function testPrompt() {
    const text = editedPromptText();
    if (!text) return;
    setButton('prompt-test-btn', 'Testing...', true);
    const panel = $('prompt-test');
    panel.hidden = false;
    panel.innerHTML = `Asking ${escapeHtml(promptData.model || 'the AI')} about the last captured card...`;
    socket.emit('test_prompt', {kind: promptKind, text: text});
}

function promptTestReadHtml(data) {
    // How the answer was parsed, and for a card the database match
    if (data.kind === 'foil') {
        const labels = {'foil': 'Foil (star)', 'non-foil': 'Not foil (dot)', 'unknown': 'Not recognized - answer must contain "star" or "dot"'};
        return `<div>${escapeHtml(labels[data.result.foil] || data.result.foil)}</div>`;
    }
    if (!data.result) return '<div class="is-error">No card name found in the answer</div>';

    const r = data.result;
    const m = data.match;
    return `<div>${escapeHtml(r.name)} · #${escapeHtml(r.collector_number || '?')} · ${escapeHtml(r.set_code || '?')}</div>`
        + '<div class="test-title">Database match</div>' + (!m
            ? '<div class="is-error">No card found in the database</div>'
            : `<div class="${m.confirmed ? 'is-confirmed' : 'is-review'}">${escapeHtml(m.name)} · ${escapeHtml(m.set_name || m.set)} (${escapeHtml(m.set)}) #${escapeHtml(m.number || '?')}
               - ${m.confirmed ? 'confirmed, would be added automatically' : `needs review (matched by ${escapeHtml(m.match || '?')})`}</div>`);
}

socket.on('prompt_test_result', function(data) {
    setButton('prompt-test-btn', 'Test on last capture');
    const panel = $('prompt-test');
    panel.hidden = false;

    if (data.error) {
        panel.innerHTML = `<span class="is-error">${escapeHtml(data.error)}</span>`;
        return;
    }
    panel.innerHTML = `
        <div class="test-title">Answer (${data.seconds} s)</div>
        <pre>${escapeHtml(data.raw || '(empty)')}</pre>
        <div class="test-title">Read as</div>
        ${promptTestReadHtml(data)}`;
});

// ============================================================================
// Card game being scanned (one at a time; the selector shows when there are several)
// ============================================================================

function applyGameFields() {
    // Manual search fields and the card data hint follow the game being scanned
    document.querySelector('.search-treatment').style.display = gameInfo.has_treatments ? '' : 'none';
    if (!gameInfo.has_treatments) $('card-treatment').value = '';
    $('set-code').placeholder = `e.g. ${gameInfo.set_example}`;
    $('collector-number').placeholder = `e.g. ${gameInfo.number_example}`;
    $('database-source-hint').textContent =
        `Downloads the latest ${gameInfo.label} card data${gameInfo.id === 'mtg' ? ' and prices' : ''} from ${gameInfo.source}`
        + (gameInfo.id === 'mtg' ? ' (a few minutes)' : ' (a few seconds)');
}

function loadGames() {
    return fetch('/api/games')
        .then(response => response.json())
        .then(data => {
            gameInfo = data.games.find(game => game.id === data.active);
            const select = $('game-select');
            select.innerHTML = data.games.map(game =>
                `<option value="${escapeHtml(game.id)}">${escapeHtml(game.label)}</option>`).join('');
            select.value = data.active;
            select.hidden = data.games.length < 2;
            applyGameFields();
        })
        .catch(error => console.error('Error loading games:', error));
}

socket.on('game_changed', function(data) {
    gameInfo = data;
    $('game-select').value = data.id;
    hideReviewPanel();
    applyGameFields();
    currentCard = null;
    setCardPanel(`<div class="empty-state">Scanning ${escapeHtml(data.label)}.</div>`);
    loadStats();
    loadPrompts();
    notify(data.card_count
        ? `Scanning ${data.label}`
        : `Scanning ${data.label} - downloading its card data`, data.card_count ? 'success' : 'info');
});

// ============================================================================
// Scanned cards (the list behind the top bar's counter)
// ============================================================================

// The scanner page lists the cards scanned and not yet moved to the collection (common.js: areaQuery)
const inventoryArea = 'scan';

let currentInventory = [];
let filteredInventory = [];

function inventoryChanged() {
    // After an edit or delete (common.js); the promise resolves when the list is loaded again
    loadStats();
    return loadInventory();
}

function showInventory() {
    $('inventory-modal').classList.add('show');
    loadInventory();
}

function closeInventory() {
    $('inventory-modal').classList.remove('show');
}

function loadInventory() {
    return fetch('/api/inventory?area=scan')
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                currentInventory = data.cards;
                filterInventory();
            } else {
                $('inventory-list').innerHTML = '<div class="empty-state is-error">Error loading inventory</div>';
            }
        })
        .catch(error => {
            console.error('Error loading inventory:', error);
            $('inventory-list').innerHTML = '<div class="empty-state is-error">Failed to load inventory</div>';
        });
}

function inventoryStatHtml(value, label) {
    return `
        <div class="inventory-stat">
            <div class="inventory-stat-value">${value}</div>
            <div class="inventory-stat-label">${label}</div>
        </div>`;
}

function inventoryRowHtml(card, index) {
    const rarity = (card.rarity || '').toLowerCase();
    const special = card.finish !== defaultFinish();
    return `
        <div class="inventory-card">
            <div class="inventory-card-number">#${index + 1}</div>
            ${card.captures && card.captures.length
                ? `<button class="inventory-thumb" data-id="${card.id}" title="${card.captures.length} capture${card.captures.length > 1 ? 's' : ''}">
                       <img src="${escapeHtml(card.captures[0].url)}" alt="" loading="lazy"></button>`
                : '<div class="inventory-thumb empty" title="No capture"></div>'}
            <div class="inventory-card-info">
                <div class="inventory-card-name">
                    ${card.quantity > 1 ? `<span class="inventory-qty">${card.quantity}×</span> ` : ''}${escapeHtml(card.name)}
                </div>
                <div class="inventory-card-details">
                    ${escapeHtml(card.set_name)} ${card.number ? '#' + escapeHtml(card.number) : ''}
                    ${card.type_line ? '· ' + escapeHtml(card.type_line) : ''}
                </div>
                <div class="inventory-card-meta">
                    ${rarity ? `<span class="inventory-badge ${escapeHtml(rarity)}">${escapeHtml(rarity.toUpperCase())}</span>` : ''}
                    ${special ? `<span class="inventory-badge ${escapeHtml(card.finish)}">${escapeHtml(finishLabel(card.finish).toUpperCase())}</span>` : ''}
                    ${card.color_identity ? `<span class="inventory-badge">${escapeHtml(card.color_identity)}</span>` : ''}
                    ${card.location ? `<span class="inventory-badge" title="Location">${escapeHtml(card.location)}</span>` : ''}
                    <span>${escapeHtml(card.condition)}</span>
                </div>
            </div>
            <div class="inventory-card-price">
                <div class="inventory-price-value">$${(card.price * card.quantity).toFixed(2)}</div>
                ${card.quantity > 1 ? `<div class="inventory-price-each">$${card.price.toFixed(2)} each</div>` : ''}
                <div class="inventory-timestamp">${escapeHtml(card.timestamp)}</div>
            </div>
            <div class="inventory-card-actions">
                <button class="btn-edit" data-id="${card.id}" title="Edit">
                    <svg class="icon"><use href="#i-edit"/></svg>
                </button>
                <button class="btn-delete" data-id="${card.id}" title="Delete">
                    <svg class="icon"><use href="#i-trash"/></svg>
                </button>
            </div>
        </div>
    `;
}

function renderInventory() {
    const statsDiv = $('inventory-stats');
    const listDiv = $('inventory-list');

    if (filteredInventory.length === 0) {
        statsDiv.innerHTML = '';
        listDiv.innerHTML = currentInventory.length
            ? '<div class="empty-state">No cards match the filter.</div>'
            : '<div class="empty-state">No scanned cards waiting.<br>Cards you scan are listed here until you add them to the collection.</div>';
        return;
    }

    const totalCards = filteredInventory.reduce((sum, card) => sum + card.quantity, 0);
    const totalValue = filteredInventory.reduce((sum, card) => sum + card.price * card.quantity, 0);
    // Copies in any finish other than the default one (foil, surge foil, holo, ...)
    const specialCount = filteredInventory.reduce((sum, card) => sum + (card.finish !== defaultFinish() ? card.quantity : 0), 0);

    statsDiv.innerHTML = inventoryStatHtml(totalCards, 'Total Cards')
        + inventoryStatHtml(`$${totalValue.toFixed(2)}`, 'Total Value')
        + inventoryStatHtml(specialCount, 'Foil Cards')
        + inventoryStatHtml(`$${(totalValue / totalCards).toFixed(2)}`, 'Avg. Value');
    listDiv.innerHTML = filteredInventory.map(inventoryRowHtml).join('');
}

// Inventory sort, remembered in this browser (the server sends rows newest first)
const INVENTORY_SORT_KEY = 'inventorySort';
function inventorySort() {
    try {
        const saved = localStorage.getItem(INVENTORY_SORT_KEY);
        if (saved in INVENTORY_SORTS) return saved;
    } catch (e) { /* storage blocked */ }
    return 'newest';
}

function setInventorySort(value) {
    try { localStorage.setItem(INVENTORY_SORT_KEY, value); } catch (e) { /* storage blocked */ }
    filterInventory();
}

function filterInventory() {
    const searchTerm = $('inventory-search').value.toLowerCase();
    const rows = !searchTerm ? currentInventory : currentInventory.filter(card =>
        [card.name, card.set_name, card.rarity, card.color_identity, card.type_line]
            .some(value => (value || '').toLowerCase().includes(searchTerm)));
    const sort = inventorySort();
    $('inventory-sort').value = sort;
    // Array.sort is stable: ties keep the server's newest-first order
    filteredInventory = INVENTORY_SORTS[sort] ? [...rows].sort(INVENTORY_SORTS[sort]) : rows;
    renderInventory();
}

function refreshInventory() {
    loadInventory();
    addLog(timeNow(), 'info', 'Scanned cards refreshed');
}

function inventoryEntry(element) {
    // The entry a row's button (data-id) belongs to
    return element && currentInventory.find(entry => entry.id === parseInt(element.dataset.id));
}

function setupInventoryList() {
    const list = $('inventory-list');

    list.addEventListener('click', function(event) {
        const button = event.target.closest('.btn-edit, .btn-delete, button.inventory-thumb');
        const card = inventoryEntry(button);
        if (!card) return;
        if (button.classList.contains('inventory-thumb')) {
            openCaptures(card);
        } else if (button.classList.contains('btn-delete')) {
            deleteCard(card.id, card.name);
        } else {
            editCard(card, [...new Set(currentInventory.map(entry => entry.location).filter(Boolean))].sort(byText),
                     () => filteredInventory);
        }
    });

    // Captures behind the entries: a grid of the copies on hover, all of them on click.
    // Hover preview only where there is a mouse; touch screens tap the thumbnail
    if (!window.matchMedia('(hover: hover)').matches) return;
    let hovered = null;
    list.addEventListener('mouseover', function(event) {
        const row = event.target.closest('.inventory-card');
        if (row === hovered) return;
        hovered = row;
        const card = inventoryEntry(row && row.querySelector('button.inventory-thumb'));
        if (card && card.captures.length) showCapturePopover(card, row); else hideCapturePopover();
    });
    list.addEventListener('mouseleave', function() {
        hovered = null;
        hideCapturePopover();
    });
    list.addEventListener('scroll', hideCapturePopover, {passive: true});
}

async function addToCollection() {
    // Move what was scanned into the collection (the Collection page); the list here empties
    if (await addScannedToCollection()) inventoryChanged();
}

async function clearInventory() {
    const count = $('inv-cards').textContent;
    const ok = await confirmDialog({
        title: 'Clear the scanned cards?',
        message: `This deletes the ${count} scanned cards that are not in the collection yet, to start over. Your collection is not touched.`,
        confirmText: 'Delete everything',
        danger: true
    });
    if (!ok) return;

    addLog(timeNow(), 'warning', 'Clearing inventory...');

    fetch('/api/clear_inventory?area=scan', {method: 'POST'})
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                notify(`Scanned cards cleared: ${data.deleted} entries removed`, 'success');
                inventoryChanged();
            } else {
                notify('Failed to clear inventory: ' + (data.error || 'Unknown error'), 'error');
            }
        })
        .catch(error => {
            console.error('Clear inventory error:', error);
            notify('Failed to clear inventory: ' + error, 'error');
        });
}

// ============================================================================
// Settings drawer and overlays
// ============================================================================

function openSettings() {
    $('settings-drawer').classList.add('show');
    loadStations();
}

// ============================================================================
// Stations: cameras elsewhere (a phone, a laptop) that capture cards and send them here
// ============================================================================

function loadStations() {
    fetch('/api/stations')
        .then(response => response.json())
        .then(data => renderStations(data.stations, data.token_required))
        .catch(error => console.error('Error loading stations:', error));
}

function renderStations(stations, tokenRequired) {
    $('stations-hint').textContent =
        `A station sends its cards to ${window.location.origin}/api/stations/<its id>/captures`
        + (tokenRequired ? ' - with the station token set on this server.' : '. It appears here with its first card.');
    $('stations-list').innerHTML = stations.length ? stations.map(station => {
        const id = escapeHtml(station.id);
        return `
        <div class="station" data-id="${id}">
            <div class="station-fields">
                <input type="text" class="text-input" value="${escapeHtml(station.name)}" maxlength="60" title="Name"
                       aria-label="Station name" onchange="saveStation('${id}', {name: this.value})">
                <input type="text" class="text-input" value="${escapeHtml(station.location || '')}" maxlength="60"
                       list="scan-location-options" placeholder="Location: as above" title="Its cards get this inventory location"
                       aria-label="Station location" autocomplete="off" onchange="saveStation('${id}', {location: this.value})">
            </div>
            <div class="station-meta">
                <span class="hint">${station.captures} card${station.captures === 1 ? '' : 's'} · last ${escapeHtml(station.last_seen || 'never')}</span>
                <button class="btn btn-small" onclick="undoStationAdd('${id}')" title="Take back the last card this station added">Undo last</button>
                <button class="btn btn-small btn-danger" onclick="forgetStation('${id}')" title="Remove it from this list - its cards stay">Forget</button>
            </div>
        </div>`;
    }).join('') : '<div class="hint">No station has sent a card yet.</div>';
}

function saveStation(id, change) {
    fetch('/api/stations/' + encodeURIComponent(id), {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(change)
    })
    .then(response => response.json())
    .then(data => {
        if (!data.success) throw new Error(data.message);
        loadStations();
    })
    .catch(error => notify('Could not save the station: ' + error.message, 'error'));
}

function undoStationAdd(id) {
    // Answered with inventory_updated (undone) or an error
    socket.emit('undo_last_add', {station: id});
}

async function forgetStation(id) {
    const ok = await confirmDialog({
        title: 'Forget this station?',
        message: 'It is removed from this list. The cards it sent stay, and it comes back if it sends another one.',
        confirmText: 'Forget',
        danger: true
    });
    if (!ok) return;
    fetch('/api/stations/' + encodeURIComponent(id), {method: 'DELETE'})
        .then(() => loadStations())
        .catch(error => notify('Could not forget the station: ' + error.message, 'error'));
}

function closeSettings() {
    $('settings-drawer').classList.remove('show');
    if (window.location.hash === '#settings') history.replaceState(null, '', window.location.pathname);
}

// The collection page's settings link here (/#settings)
if (window.location.hash === '#settings') openSettings();

// Topmost first: Escape closes the first one open, a click on a backdrop closes that one
const OVERLAYS = [
    ['capture-modal', closeCaptures],
    ['dialog-modal', () => closeDialog(null)],
    ['edit-card-modal', closeEditCard],
    ['prompt-modal', closePromptEditor],
    ['inventory-modal', closeInventory],
    ['settings-drawer', closeSettings],
];

document.addEventListener('keydown', function(event) {
    if (event.key !== 'Escape') return;
    if (isDrawingArea()) {
        cancelDrawArea();
        return;
    }
    const overlay = OVERLAYS.find(([id]) => isOpen(id));
    if (overlay) overlay[1](); else closeSettings();
});

window.addEventListener('click', function(event) {
    const overlay = OVERLAYS.find(([id]) => event.target === $(id));
    if (overlay) overlay[1]();
});

function testAudio() {
    addLog(timeNow(), 'info', 'Testing audio system...');

    audioManager.ensureAudioContext().then(() => {
        // All sounds in sequence
        audioManager.playCapture();
        addLog(timeNow(), 'info', '1/3: Capture sound');

        setTimeout(() => {
            audioManager.playSuccess();
            addLog(timeNow(), 'info', '2/3: Success sound');
        }, 400);

        setTimeout(() => {
            audioManager.playNextReady();
            addLog(timeNow(), 'success', '3/3: Next Ready sound - Check browser console');
        }, 1000);
    });
}

// ============================================================================
// Page setup
// ============================================================================

function onToggle(id, handler) {
    $(id).addEventListener('change', event => handler(event.target.checked));
}

function setupSearchFields() {
    const fields = ['card-name', 'set-code', 'collector-number'];
    fields.forEach(id => {
        $(id).addEventListener('keypress', event => {
            if (event.key === 'Enter') searchCard();
        });
        $(id).addEventListener('input', () => {
            const [name, setCode, number] = fields.map(field => $(field).value.trim());
            $('search-btn').disabled = !canSearch(name, setCode, number);
        });
    });
}

function setupScanToggles() {
    detectionEnabled = $('toggle-detection').checked;

    onToggle('toggle-detection', enabled => {
        detectionEnabled = enabled;
        socket.emit('toggle_detection', {enabled: enabled});  // the server logs the change

        // Shown right away (the next poll refreshes it from the server)
        updateDetectionStatus(lastDetectionStatus);

        // Auto scanning needs detection
        if (!enabled && autoScanningEnabled) toggleAutoScanning();
        $('toggle-auto-scanning-btn').disabled = !enabled;
    });

    onToggle('toggle-fast-scan', enabled => {
        fastScanMode = enabled;
        socket.emit('toggle_fast_scan', {enabled: enabled});  // the server logs the change
        if (autoScanningEnabled) $('auto-scan-hint').textContent = autoScanHint();
    });

    // Continuous autofocus on/off (off = find the sharpest focus and lock it)
    onToggle('toggle-autofocus', enabled => socket.emit('set_autofocus', {enabled: enabled}));
    // Read cards with light-ocr before asking the vision AI
    onToggle('toggle-ocr', enabled => socket.emit('toggle_ocr', {enabled: enabled}));
    onToggle('toggle-debug-mode', enabled => socket.emit('toggle_debug_mode', {enabled: enabled}));
    onToggle('toggle-debug-trace', enabled => {
        socket.emit('toggle_debug_trace', {enabled: enabled});
        addLog(timeNow(), 'info', `Debug trace ${enabled ? 'enabled' : 'disabled'}`);
    });

    $('refocus-every').addEventListener('change', function(e) {
        socket.emit('set_refocus_every', {captures: parseInt(e.target.value)});
    });
    $('camera-rotation').addEventListener('change', function(e) {
        socket.emit('set_camera_rotation', {rotation: parseInt(e.target.value)});
    });
    $('game-select').addEventListener('change', function(e) {
        socket.emit('set_game', {game: e.target.value});
    });
}

function setupAudioControls() {
    onToggle('toggle-audio', enabled => {
        audioManager.setEnabled(enabled);
        saveSound({enabled: enabled});
        addLog(timeNow(), 'info', `Sound effects ${enabled ? 'enabled' : 'disabled'}`);
        if (enabled) audioManager.playSuccess();  // a test sound
    });

    $('audio-volume').addEventListener('input', function(e) {
        audioManager.setVolume(e.target.value / 100);  // 0-100 -> 0.0-1.0
        $('volume-value').textContent = e.target.value + '%';
    });
    // Saved when the slider is let go, not on every step of the drag
    $('audio-volume').addEventListener('change', e => saveSound({volume: parseInt(e.target.value)}));
}

function setupAIControls() {
    // Change AI provider: show its key / address field; switch once it has a key
    $('ai-provider').addEventListener('change', function(e) {
        const provider = e.target.value;
        currentProvider = provider;
        renderCredentialField(provider);

        if (hasCredential(provider)) {
            applyProvider(provider);
        } else {
            populateModelDropdown(provider);
            $('ai-credential').focus();
            notify(`Enter your ${PROVIDER_INFO[provider].name} API key to use it`, 'warning');
        }
    });

    $('ai-model').addEventListener('change', function(e) {
        socket.emit('set_ai_provider', {provider: currentProvider, model: e.target.value});
        addLog(timeNow(), 'info', `Changing model to ${e.target.value}...`);
    });

    // Enter in the key field saves it
    $('ai-credential').addEventListener('keydown', function(e) {
        if (e.key === 'Enter') saveCredential();
    });

    $('prompt-text').addEventListener('input', onPromptInput);
}

document.addEventListener('DOMContentLoaded', function() {
    setupSearchFields();
    setupScanToggles();
    setupAudioControls();
    setupAIControls();
    setupAreaDrawing();
    setupInventoryList();

    loadGames();
    setInterval(updateVideoOrientation, 1000);

    loadStats();
    setInterval(loadStats, 5000);

    // The model list first (the provider's dropdown is filled from it); the key field needs
    // the current provider
    loadAIModels().then(loadAIProvider).then(loadCredentials).then(loadPrompts);
});
