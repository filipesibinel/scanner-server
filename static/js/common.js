// Shared by the scanner page and the collection page: text helpers, the active game's
// finishes, in-page dialogs and notifications, the inventory edit dialog and the capture viewer.
// Each page defines inventoryChanged() (reload what it shows after an edit).

function timeNow() {
    // 24-hour HH:MM:SS, matching timestamps sent by the server
    return new Date().toLocaleTimeString('en-GB', {hour12: false});
}

let gameInfo = null;  // {id, label, finishes: [[key, label]], exports: [[key, label]]}

function defaultFinish() {
    return gameInfo.finishes[0][0];
}

function finishLabel(key) {
    const finish = gameInfo.finishes.find(([k]) => k === key);
    return finish ? finish[1] : key;
}

function escapeHtml(text) {
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return text.replace(/[&<>"']/g, m => map[m]);
}

const RARITY_ORDER = {special: 0, bonus: 0, mythic: 1, rare: 2, uncommon: 3, common: 4};
const byText = (a, b) => (a || '').localeCompare(b || '', undefined, {numeric: true, sensitivity: 'base'});
const INVENTORY_SORTS = {
    newest: null,
    oldest: (a, b) => byText(a.timestamp, b.timestamp) || a.id - b.id,
    // When the entry came into the collection (one time per "Add to collection"); the cards of
    // one such batch by when they were scanned - their ids run the other way (the card scanned
    // last is moved first), which once put the first card scanned on top
    added: (a, b) => byText(b.added_at, a.added_at) || byText(b.timestamp, a.timestamp) || b.id - a.id,
    name: (a, b) => byText(a.name, b.name),
    price_desc: (a, b) => (b.price || 0) - (a.price || 0),
    price_asc: (a, b) => (a.price || 0) - (b.price || 0),
    value_desc: (a, b) => (b.price || 0) * b.quantity - (a.price || 0) * a.quantity,
    quantity_desc: (a, b) => b.quantity - a.quantity,
    rarity: (a, b) => (RARITY_ORDER[a.rarity] ?? 9) - (RARITY_ORDER[b.rarity] ?? 9),
    set: (a, b) => byText(a.set_name, b.set_name) || byText(a.number, b.number),
};

// ============================================================================
// Inventory edit dialog (templates/_dialogs.html)
// ============================================================================

let currentEditId = null;

// The entry as it was, to detect a split (another finish or location for part of a stack)
let originalFinish = null;
let originalLocation = '';
let originalQuantity = 0;
let originalPrinting = '';   // card id of the entry's printing ('' = not in the list)
let originalCondition = '', originalTags = '';
let editLocations = [];      // locations offered while typing
let editList = null;         // () => the entries of the list the card was opened from, in its order
let editSession = 0;         // changes whenever the dialog opens or closes (see stepEditCard)
let editPrintings = [];      // the printings offered in the Edit card dialog
let editScans = [], editScanIndex = 0;  // the entry's photos, and the one shown beside the printing

function areaQuery() {
    // The scanner page works on the scanned cards (it sets inventoryArea = 'scan'), the
    // collection page on the collection
    return typeof inventoryArea === 'string' ? `?area=${inventoryArea}` : '';
}

function logLine(level, message) {
    // The activity panel only exists on the scanner page
    if (typeof addLog === 'function') addLog(timeNow(), level, message);
}

function parseTags(text) {
    // "trade, Keep ,trade" -> ["trade", "Keep"]
    const seen = new Set();
    return (text || '').split(',').map(tag => tag.trim()).filter(tag => {
        const key = tag.toLowerCase();
        if (!tag || seen.has(key)) return false;
        seen.add(key);
        return true;
    });
}

function renderEditFinishes(selected) {
    document.getElementById('edit-finishes').innerHTML = gameInfo.finishes.map(([key, label]) => `
        <label class="radio-pill">
            <input type="radio" name="edit-finish" value="${escapeHtml(key)}" ${key === selected ? 'checked' : ''} onchange="updateSplitQuantityVisibility()">
            <span>${escapeHtml(label)}</span>
        </label>`).join('');
}

function editCard(card, locations = [], list = null) {
    // locations: the ones already in use, offered while typing; list: a function giving the
    // entries of the list the card is in, as shown - the arrows step through them
    editLocations = locations;
    editList = list;
    editSession++;
    currentEditId = card.id;
    originalQuantity = card.quantity;
    originalFinish = card.finish;
    originalLocation = card.location || '';
    originalCondition = card.condition;
    originalTags = (card.tags || []).join(', ');
    // Scanned cards get their location and tags in the collection: fewer fields while checking them
    const scanned = typeof inventoryArea === 'string' && inventoryArea === 'scan';
    document.getElementById('edit-location-group').hidden = scanned;
    document.getElementById('edit-tags-group').hidden = scanned;
    document.getElementById('edit-split-note').textContent = `Changing the printing${scanned ? ' or finish' : ', finish or location'}`
        + ' of several copies splits this entry - choose how many change.';

    document.getElementById('edit-card-name').textContent = card.name;
    document.getElementById('edit-quantity').value = card.quantity;
    document.getElementById('edit-condition').value = card.condition;
    document.getElementById('edit-location').value = originalLocation;
    document.getElementById('edit-location-options').innerHTML =
        locations.map(location => `<option value="${escapeHtml(location)}"></option>`).join('');
    document.getElementById('edit-tags').value = (card.tags || []).join(', ');
    renderEditFinishes(card.finish);

    editScans = card.captures || [];
    editScanIndex = 0;
    showEditScan();
    loadEditPrintings(card.id);

    const splitInput = document.getElementById('edit-split-quantity');
    splitInput.value = 1;
    splitInput.max = originalQuantity;
    splitInput.oninput = updateSplitPreview;
    document.getElementById('split-quantity-section').style.display = 'none';

    showEditPosition();
    document.getElementById('edit-card-modal').classList.add('show');
}

function loadEditPrintings(rowId) {
    // The printings the entry can be changed to, with the scan beside the one chosen. A card
    // with one printing shows it too (nothing to choose, but the scan can be compared); a game
    // without a list of printings shows the scan alone
    const group = document.getElementById('edit-printing-group'), select = document.getElementById('edit-printing');
    const label = document.getElementById('edit-printing-label');
    group.hidden = !editScans.length;
    label.textContent = 'Your scan';
    select.hidden = true;
    select.innerHTML = '';
    originalPrinting = '';
    editPrintings = [];
    showEditPrintingImage();  // no picture left over from the card edited before
    fetch(`/api/inventory/${rowId}/printings${areaQuery()}`)
        .then(response => response.json())
        .then(data => {
            if (currentEditId !== rowId || !data.success || !data.printings.length) return;
            const price = value => value ? `$${Number(value).toFixed(2)}` : '';
            select.innerHTML = data.printings.map(printing => {
                const prices = [price(printing.price), printing.price_foil ? `foil ${price(printing.price_foil)}` : ''].filter(Boolean).join(' / ');
                return `<option value="${escapeHtml(printing.id)}">${escapeHtml(printing.set)} (${escapeHtml((printing.set_code || '').toUpperCase())}) #${escapeHtml(printing.number)}${prices ? ' - ' + prices : ''}</option>`;
            }).join('');
            const current = data.printings.find(printing => printing.current);
            // An entry whose printing the card data doesn't have (an old import): no choice made yet
            if (!current) select.insertAdjacentHTML('afterbegin', '<option value="">Keep as it is</option>');
            select.value = originalPrinting = current ? current.id : '';
            editPrintings = data.printings;
            showEditPrintingImage();
            label.textContent = data.printings.length > 1 ? 'Printing' : 'Printing (the only one)';
            select.disabled = data.printings.length < 2;
            select.hidden = false;
            group.hidden = false;
        })
        .catch(() => {});  // the dialog works without the list
}

function showEditPrintingImage() {
    // The picture of the printing chosen, beside the scan
    const printing = editPrintings.find(item => item.id === document.getElementById('edit-printing').value);
    const figure = document.getElementById('edit-printing-figure');
    figure.hidden = !(printing && printing.image_uri);
    const image = document.getElementById('edit-printing-image');
    if (figure.hidden) image.removeAttribute('src'); else image.src = printing.image_uri;
}

function showEditScan() {
    // One of the photos taken of the entry's copies; a click shows the next one
    const figure = document.getElementById('edit-scan-figure');
    figure.hidden = !editScans.length;
    if (figure.hidden) return;
    const scan = editScans[editScanIndex];
    document.getElementById('edit-scan-image').src = scan.url;
    document.getElementById('edit-scan-image').classList.toggle('is-clickable', editScans.length > 1);
    document.getElementById('edit-scan-caption').textContent = editScans.length > 1
        ? `Your scan ${editScanIndex + 1} of ${editScans.length} - click for the next` : 'Your scan';
}

function nextEditScan() {
    if (editScans.length < 2) return;
    editScanIndex = (editScanIndex + 1) % editScans.length;
    showEditScan();
}

function editedPrinting() {
    // The printing chosen, '' when it is the one the entry already is
    const select = document.getElementById('edit-printing');
    return select.value !== originalPrinting ? select.value : '';
}

function stepNumber(id, delta) {
    // The - / + buttons beside a number field (.number-stepper): within its min and max,
    // and the field's own oninput runs as if the number had been typed
    const input = document.getElementById(id);
    const low = input.min === '' ? -Infinity : Number(input.min);
    const high = input.max === '' ? Infinity : Number(input.max);
    input.value = Math.max(low, Math.min(high, (parseInt(input.value) || 0) + delta));
    input.dispatchEvent(new Event('input', {bubbles: true}));
}

function closeEditCard() {
    document.getElementById('edit-card-modal').classList.remove('show');
    editSession++;
    currentEditId = null;
    originalFinish = null;
    originalLocation = '';
    originalQuantity = 0;
}

function editedFinish() {
    return document.querySelector('input[name="edit-finish"]:checked').value;
}

function editedLocation() {
    return document.getElementById('edit-location').value.trim();
}

function editSplits() {
    // Several copies and a new finish or location: ask how many get it
    return originalQuantity > 1 && (editedFinish() !== originalFinish || editedLocation() !== originalLocation || editedPrinting() !== '');
}

function updateSplitQuantityVisibility() {
    const splitSection = document.getElementById('split-quantity-section');
    if (editSplits()) {
        splitSection.style.display = 'block';
        updateSplitPreview();
    } else {
        splitSection.style.display = 'none';
    }
}

function updateSplitPreview() {
    const splitQuantity = parseInt(document.getElementById('edit-split-quantity').value) || 1;
    const remaining = originalQuantity - splitQuantity;
    const preview = document.getElementById('split-preview');
    if (!preview) return;
    const describe = (finish, location) => escapeHtml(finishLabel(finish) + (location ? `, ${location}` : ''));
    // "2 Foil, Box #1, other printing + 1 stay Regular"
    preview.innerHTML = `<strong>${splitQuantity}</strong> ${describe(editedFinish(), editedLocation())}`
        + (editedPrinting() ? ', other printing' : '')
        + ` + <strong>${remaining}</strong> stay ${describe(originalFinish, originalLocation)}`;
}

function updateSplitMaxQuantity() {
    const newQuantity = parseInt(document.getElementById('edit-quantity').value) || 1;
    const splitInput = document.getElementById('edit-split-quantity');

    if (splitInput) {
        // Update max to the new total quantity
        splitInput.max = newQuantity;

        // If current split value exceeds new max, adjust it
        if (parseInt(splitInput.value) > newQuantity) {
            splitInput.value = newQuantity;
        }

        // Update preview
        updateSplitPreview();
    }
}

function editRequest() {
    // What the dialog asks for, or null (after a notice) when a number is out of range
    const quantity = parseInt(document.getElementById('edit-quantity').value);
    if (isNaN(quantity) || quantity < 1 || quantity > 999) {
        notify('Please enter a quantity between 1 and 999', 'warning');
        return null;
    }
    const requestBody = {quantity: quantity, condition: document.getElementById('edit-condition').value, finish: editedFinish(),
                         location: editedLocation(), tags: parseTags(document.getElementById('edit-tags').value)};
    if (editedPrinting()) requestBody.card_id = editedPrinting();
    if (editSplits()) {
        const splitQuantity = parseInt(document.getElementById('edit-split-quantity').value) || 1;
        if (splitQuantity < 1 || splitQuantity > originalQuantity) {
            notify(`Split quantity must be between 1 and ${originalQuantity}`, 'warning');
            return null;
        }
        requestBody.split_quantity = splitQuantity;
    }
    return requestBody;
}

function editChanged() {
    // Has anything in the dialog been changed?
    return parseInt(document.getElementById('edit-quantity').value) !== originalQuantity
        || document.getElementById('edit-condition').value !== originalCondition
        || editedFinish() !== originalFinish || editedLocation() !== originalLocation
        || parseTags(document.getElementById('edit-tags').value).join(', ') !== parseTags(originalTags).join(', ')
        || editedPrinting() !== '';
}

async function sendEdit(rowId, cardName, requestBody) {
    // Resolves true when the entry was updated
    logLine('info', `Updating ${cardName}...`);
    try {
        const response = await fetch(`/api/inventory/update/${rowId}${areaQuery()}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(requestBody)
        });
        const data = await response.json();
        if (!data.success) throw new Error(data.error || data.message || 'Unknown error');
        logLine('success', `${cardName} updated` + (data.split ? ' (entry split)' : ''));
        return true;
    } catch (error) {
        notify('Failed to update card: ' + error.message, 'error');
        return false;
    }
}

async function saveEditCard() {
    if (editStepping) return;  // an arrow is saving this very card: a second request would repeat a split
    if (currentEditId === null) {
        notify('No card selected for editing', 'error');
        return;
    }
    const requestBody = editRequest();
    if (!requestBody) return;
    const cardName = document.getElementById('edit-card-name').textContent;
    const rowId = currentEditId;
    closeEditCard();
    if (await sendEdit(rowId, cardName, requestBody)) inventoryChanged();
}

function editEntries() {
    return editList ? editList() : [];
}

function showEditPosition() {
    // "3 of 19" and the two arrows; hidden when the card wasn't opened from a list of several
    const entries = editEntries();
    const index = entries.findIndex(entry => entry.id === currentEditId);
    document.getElementById('edit-nav').hidden = index < 0 || entries.length < 2;
    document.getElementById('edit-position').textContent = `${index + 1} of ${entries.length}`;
    document.getElementById('edit-previous').disabled = index <= 0;
    document.getElementById('edit-next').disabled = index >= entries.length - 1;
}

let editStepping = false;

async function stepEditCard(delta) {
    // The previous / next card of the list, without closing the dialog. Changes made to the
    // card in view are saved first (the list is then loaded again: an entry can split or merge)
    if (editStepping || currentEditId === null) return;
    let entries = editEntries();
    const index = entries.findIndex(entry => entry.id === currentEditId);
    const target = entries[index + delta];
    if (index < 0 || !target) return;
    editStepping = true;
    document.getElementById('edit-save').disabled = true;
    try {
        if (editChanged()) {
            const requestBody = editRequest();
            if (!requestBody) return;
            // Closing the dialog, or opening another card, while the save or the reload is under
            // way starts another session: the change is saved, but this step must not open
            // its target over whatever is shown by then
            const session = editSession;
            const saved = await sendEdit(currentEditId, document.getElementById('edit-card-name').textContent, requestBody);
            if (saved) await inventoryChanged();
            if (!saved || session !== editSession) return;
            entries = editEntries();
        }
        // The same entry after a reload, or the one now at its place in the list
        const next = entries.find(entry => entry.id === target.id)
            || entries[Math.min(index + Math.max(delta, 0), entries.length - 1)];
        if (next) editCard(next, editLocations, editList); else closeEditCard();
    } finally {
        editStepping = false;
        document.getElementById('edit-save').disabled = false;
    }
}

document.addEventListener('keydown', event => {
    // Left / right arrows step through the cards while the dialog is open (not while typing)
    if (!document.getElementById('edit-card-modal').classList.contains('show')) return;
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName)) return;
    if (document.getElementById('edit-nav').hidden) return;
    event.preventDefault();
    stepEditCard(event.key === 'ArrowLeft' ? -1 : 1);
});

async function deleteCard(rowId, cardName) {
    const ok = await confirmDialog({
        title: 'Delete card?',
        message: `Delete "${cardName}"? This can't be undone.`,
        confirmText: 'Delete',
        danger: true
    });
    if (!ok) return;

    logLine('info', `Deleting ${cardName}...`);

    fetch(`/api/inventory/delete/${rowId}${areaQuery()}`, {
        method: 'DELETE'
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            logLine('success', `${cardName} deleted`);
            inventoryChanged();
        } else {
            notify('Failed to delete card: ' + (data.error || 'Unknown error'), 'error');
        }
    })
    .catch(error => {
        console.error('Delete error:', error);
        notify('Failed to delete card: ' + error, 'error');
    });
}

// ============================================================================
// In-page dialogs and notifications
// Native confirm()/alert() can be silently blocked by the browser ("prevent this page from
// creating additional dialogs"), after which confirm() always returns false - so the app
// uses its own.
// ============================================================================

let dialogResolve = null;

function choiceDialog({title, message, choices}) {
    // choices: [{label, value, style: 'primary' | 'danger' | undefined}]; resolves with the
    // chosen value, or null when dismissed (Escape, click outside)
    return new Promise(resolve => {
        if (dialogResolve) dialogResolve(null);
        dialogResolve = resolve;
        document.getElementById('dialog-title').textContent = title;
        document.getElementById('dialog-message').textContent = message;
        const buttons = document.getElementById('dialog-buttons');
        buttons.innerHTML = '';
        choices.forEach(choice => {
            const button = document.createElement('button');
            button.className = 'btn' + (choice.style ? ` btn-${choice.style}` : '');
            button.textContent = choice.label;
            button.onclick = () => closeDialog(choice.value);
            buttons.appendChild(button);
        });
        document.getElementById('dialog-modal').classList.add('show');
        buttons.firstChild.focus();  // the safe choice (Cancel) is first
    });
}

function closeDialog(value = null) {
    document.getElementById('dialog-modal').classList.remove('show');
    const resolve = dialogResolve;
    dialogResolve = null;
    if (resolve) resolve(value);
}

function confirmDialog({title, message, confirmText = 'OK', danger = false}) {
    return choiceDialog({title, message, choices: [
        {label: 'Cancel', value: false},
        {label: confirmText, value: true, style: danger ? 'danger' : 'primary'}
    ]}).then(value => value === true);
}

// Scanned cards -> collection, from either page: asks for a location first

let toCollectionResolve = null;

function closeToCollection(location) {
    // location: what was typed ('' = none), or null when cancelled
    document.getElementById('to-collection-modal').classList.remove('show');
    const resolve = toCollectionResolve;
    toCollectionResolve = null;
    if (resolve) resolve(location === null ? null : location.trim());
}

function decksHeadingHtml(select, labels) {
    // The "Decks" heading of a location dropdown: a line as wide as the widest entry, so the
    // word sits in the middle of the list (browsers don't center an <optgroup> label, and
    // indent the entries under it)
    const context = document.createElement('canvas').getContext('2d');
    context.font = getComputedStyle(select).font;
    const width = text => context.measureText(text).width;
    const widest = Math.max(...labels.map(width));
    const dashes = Math.max(4, Math.floor((widest - width(' Decks ')) / (2 * width('─'))));
    return `<option disabled>${'─'.repeat(dashes)} Decks ${'─'.repeat(dashes)}</option>`;
}

async function addScannedToCollection(camera = '', cameraName = '') {
    // Resolves true when the cards were moved (the page then reloads its lists).
    // camera: a station id - only the cards that camera scanned ('' = every camera's)
    try {
        const waiting = await (await fetch('/api/scan_inventory/to_collection?camera=' + encodeURIComponent(camera))).json();
        if (!waiting.cards) {
            notify('No scanned cards to add', 'info');
            return false;
        }
        document.getElementById('to-collection-message').textContent =
            camera ? `The ${waiting.cards} card${waiting.cards > 1 ? 's' : ''} scanned by ${cameraName || camera} move to your collection (cards you already have there get the copies added). The other cameras' cards stay in the scanned list.`
                   : `The ${waiting.cards} scanned card${waiting.cards > 1 ? 's' : ''} move to your collection (cards you already have there get the copies added) and the scanned list is emptied.`;
        // The locations in use, to pick from; a new one is typed in the field below
        const used = document.getElementById('to-collection-used');
        // Boxes and binders first, the locations named after a deck under their own heading
        const decks = new Set((waiting.decks || []).map(name => name.toLowerCase()));
        const option = location => `<option value="${escapeHtml(location)}">${escapeHtml(location)}</option>`;
        const inDecks = waiting.locations.filter(location => decks.has(location.toLowerCase()));
        const heading = 'Locations you already use...';
        used.innerHTML = `<option value="">${heading}</option>` +
            waiting.locations.filter(location => !inDecks.includes(location)).map(option).join('') +
            (inDecks.length ? decksHeadingHtml(used, [heading, ...waiting.locations]) + inDecks.map(option).join('') : '');
        used.hidden = !waiting.locations.length;
        const input = document.getElementById('to-collection-location');
        input.value = '';
        const location = await new Promise(resolve => {
            if (toCollectionResolve) toCollectionResolve(null);
            toCollectionResolve = resolve;
            document.getElementById('to-collection-modal').classList.add('show');
            input.focus();
        });
        if (location === null) return false;
        const response = await fetch('/api/scan_inventory/to_collection', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({location: location, camera: camera})
        });
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Unknown error');
        notify(`${data.cards} card${data.cards === 1 ? '' : 's'} added to the collection${location ? ` in ${location}` : ''}`, 'success');
        return true;
    } catch (error) {
        notify('Could not add to the collection: ' + error.message, 'error');
        return false;
    }
}

function notify(message, level = 'info') {
    // Shows a short notification and records it in the activity log
    logLine(level, message);
    const toast = document.createElement('div');
    toast.className = `toast ${level}`;
    toast.textContent = message;
    document.getElementById('toast-container').appendChild(toast);
    setTimeout(() => toast.remove(), 5000);
}

const POPOVER_MAX = 8;

function captureGridHtml(card, max = Infinity) {
    const shown = card.captures.slice(0, max);
    const more = card.captures.length - shown.length;
    return shown.map(capture => `
        <figure>
            <img src="${escapeHtml(capture.url)}" alt="${escapeHtml(card.name)}" loading="lazy">
            <figcaption>${escapeHtml(capture.captured_at)}</figcaption>
        </figure>`).join('')
        + (more > 0 ? `<div class="capture-more">+${more} more<br><span>click to see all</span></div>` : '');
}

function showCapturePopover(card, row) {
    const popover = document.getElementById('capture-popover');
    popover.className = 'capture-popover capture-grid' + (card.captures.length === 1 ? ' single' : '');
    popover.innerHTML = captureGridHtml(card, POPOVER_MAX);
    popover.hidden = false;
    // Beside the row's thumbnail; above the row when there is no room below
    const rect = row.getBoundingClientRect();
    const box = popover.getBoundingClientRect();
    const left = Math.min(rect.left + 70, window.innerWidth - box.width - 16);
    const below = rect.bottom + 6;
    popover.style.left = `${Math.max(16, left)}px`;
    popover.style.top = `${below + box.height < window.innerHeight - 8 ? below : Math.max(8, rect.top - box.height - 6)}px`;
}

function hideCapturePopover() {
    document.getElementById('capture-popover').hidden = true;
}

function openCaptures(card) {
    hideCapturePopover();
    const count = card.captures.length;
    document.getElementById('capture-title').textContent =
        `${card.name} - ${count} capture${count > 1 ? 's' : ''} of ${card.quantity} ${card.quantity > 1 ? 'copies' : 'copy'}`;
    document.getElementById('capture-grid').innerHTML = captureGridHtml(card);
    document.getElementById('capture-modal').classList.add('show');
}

function closeCaptures() {
    document.getElementById('capture-modal').classList.remove('show');
}
