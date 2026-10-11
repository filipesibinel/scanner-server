// Collection page: inventory management, deck builder, statistics.
// Shared helpers (escapeHtml, dialogs, notify, the edit dialog, sorts) come from common.js.

const socket = io();
const $ = id => document.getElementById(id);
const money = value => '$' + (value || 0).toFixed(2);
const plural = (count, word, many = word + 's') => `${count} ${count === 1 ? word : many}`;
const entriesText = count => plural(count, 'entry', 'entries');
const totalQuantity = rows => rows.reduce((sum, row) => sum + row.quantity, 0);

async function api(path, options = {}) {
    // JSON request; shows the server's error and returns null when it fails
    if (options.body && typeof options.body !== 'string' && !(options.body instanceof FormData)) {
        options = {...options, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(options.body)};
    }
    try {
        const response = await fetch(path, options);
        const data = await response.json();
        if (data.success === false) {
            notify(data.error || 'Request failed', 'error');
            return null;
        }
        return data;
    } catch (error) {
        notify(`Request failed: ${error.message}`, 'error');
        return null;
    }
}

function remember(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* storage blocked */ }
}

function recall(key, fallback) {
    try { return localStorage.getItem(key) || fallback; } catch (e) { return fallback; }
}

function manaHtml(cost) {
    // "{2}{W}{U/B}" -> colored pips
    return (cost || '').replace(/\{([^}]+)\}/g, (_, symbol) => {
        // Hybrid symbols ("W/U") take the first color; numbers and X are neutral
        const color = [...'WUBRGC'].find(letter => symbol.includes(letter)) || 'N';
        const text = symbol.replace('/', '');
        return `<span class="mana mana-${color}${text.length > 1 ? ' is-long' : ''}">${escapeHtml(text)}</span>`;
    });
}

function closeModal(id) {
    $(id).classList.remove('show');
}

function optionsHtml(pairs) {
    // [[value, label]]
    return pairs.map(([value, label]) => `<option value="${escapeHtml(String(value))}">${escapeHtml(label)}</option>`).join('');
}

function searchWords(id) {
    // What is typed in a filter field: every word must be found
    return $(id).value.trim().toLowerCase().split(/\s+/).filter(Boolean);
}

function markSwitch(id, key, value) {
    // A row of buttons of which one is active (data-<key>)
    document.querySelectorAll(`#${id} button`).forEach(button => button.classList.toggle('is-active', button.dataset[key] === value));
}

// ============================================================================
// Tabs
// ============================================================================

let currentTab = 'inventory';

function showTab(tab) {
    currentTab = tab;
    document.querySelectorAll('.tab').forEach(button => button.classList.toggle('is-active', button.dataset.tab === tab));
    document.querySelectorAll('.tab-panel').forEach(panel => { panel.hidden = panel.id !== `tab-${tab}`; });
    remember('collectionTab', tab);
    if (tab === 'decks') {
        loadDecks();
        if (gameInfo.deck_formats.length) {
            loadPrecons();
            loadAroundCards();
        }
    }
    if (tab === 'stats') renderStats();
    if (tab === 'trades') loadTrades();
}

// ============================================================================
// Inventory
// ============================================================================

const COLORS = [['W', 'White'], ['U', 'Blue'], ['B', 'Black'], ['R', 'Red'], ['G', 'Green'], ['C', 'Colorless']];
const MTG_TYPES = ['Creature', 'Planeswalker', 'Instant', 'Sorcery', 'Artifact', 'Enchantment', 'Battle', 'Land'];
const CONDITIONS = ['Mint', 'Near Mint', 'Excellent', 'Good', 'Played', 'Poor'];
const PAGE_SIZES = [25, 50, 100, 200];
// The filters kept for the next visit. Not the "Added" batch (it comes with "Remove this
// batch") and not "No use in my decks" (it asks EDHREC before the list can show)
const FILTER_FIELDS = ['filter-text', 'filter-type', 'filter-rarity', 'filter-set', 'filter-finish', 'filter-location',
                       'filter-tag', 'filter-price-min', 'filter-price-max'];
const FILTER_TICKS = ['filter-text-not', 'filter-free'];
// ... and their names in the page's address (?set=...&rarity=rare), which can be kept as a bookmark
const FILTER_PARAMS = {'filter-text': 'q', 'filter-type': 'type', 'filter-rarity': 'rarity', 'filter-set': 'set',
                       'filter-finish': 'finish', 'filter-location': 'loc', 'filter-tag': 'tag', 'filter-price-min': 'min',
                       'filter-price-max': 'max', 'filter-text-not': 'not', 'filter-free': 'free'};
const UNDO_SECONDS = 6;

let inventory = [];
let shown = [];             // after filters and sort
let pageSize = PAGE_SIZES.includes(parseInt(recall('collectionPageSize', ''))) ? parseInt(recall('collectionPageSize', '')) : 100;
let page = 0;               // of shown, pageSize entries each
let selected = new Set();   // entry ids
let lastPicked = null;      // the entry ticked or unticked last: a shift-click reaches from it to the clicked one
let savedFilters = recallFilters();   // until the first load has filled the lists to choose from
let filterColors = new Set(savedFilters.colors);
let suggested = null;       // {card name: [decks whose commander it is played with]}, while "No use in my decks" is ticked
let inventoryView = recall('collectionView', 'list');

function recallFilters() {
    // The filters of the address when it names any (a bookmark), else the ones of the last visit
    let saved = {};
    const params = new URLSearchParams(window.location.search);
    if ([...Object.values(FILTER_PARAMS), 'colors'].some(name => params.has(name))) {
        [...FILTER_FIELDS, ...FILTER_TICKS].forEach(id => {
            const value = params.get(FILTER_PARAMS[id]);
            if (value) saved[id] = FILTER_TICKS.includes(id) ? true : value;
        });
        saved.colors = [...(params.get('colors') || '').toUpperCase()];
    } else {
        try { saved = JSON.parse(recall('collectionFilters', '{}')) || {}; } catch (e) { /* not ours */ }
    }
    return {...saved, colors: Array.isArray(saved.colors) ? saved.colors.filter(color => COLORS.some(([key]) => key === color)) : []};
}

function saveFilters() {
    const state = {colors: [...filterColors]};
    FILTER_FIELDS.forEach(id => { if ($(id).value) state[id] = $(id).value; });
    FILTER_TICKS.forEach(id => { if ($(id).checked) state[id] = true; });
    remember('collectionFilters', JSON.stringify(state));
    // The address says the same (the #settings part stays)
    const params = new URLSearchParams();
    [...FILTER_FIELDS, ...FILTER_TICKS].forEach(id => { if (state[id]) params.set(FILTER_PARAMS[id], state[id] === true ? '1' : state[id]); });
    if (state.colors.length) params.set('colors', state.colors.join(''));
    const query = params.toString();
    try {
        history.replaceState(null, '', window.location.pathname + (query ? '?' + query : '') + window.location.hash);
    } catch (e) { /* the list works without it */ }
}

function restoreFilters() {
    // Once, after the first load: a list takes only a value it offers, so a set or tag that
    // is gone from the collection is not filtered by
    FILTER_FIELDS.forEach(id => {
        $(id).value = typeof savedFilters[id] === 'string' ? savedFilters[id] : '';
        if ($(id).selectedIndex === -1) $(id).value = '';
    });
    FILTER_TICKS.forEach(id => { $(id).checked = savedFilters[id] === true; });
    savedFilters = null;
    saveFilters();  // what was taken, in the address too
}

function filtersChanged() {
    page = 0;
    saveFilters();
    applyFilters();
}

function inventoryChanged() {
    // After an edit or delete (common.js), a bulk action or a change made while scanning;
    // the promise resolves when the list is loaded again
    return loadInventory();
}

function identityOf(card) {
    return (card.details && card.details.identity) || null;
}

function mainType(card) {
    // Magic: the card type; other games: the first part of the type line
    const front = (card.type_line || '').split(' // ')[0];
    if (gameInfo.id === 'mtg') return MTG_TYPES.find(type => front.includes(type)) || 'Other';
    return front.split(' · ')[0] || 'Other';
}

function colorGroup(card) {
    const identity = identityOf(card);
    if (!identity) return card.color_identity || 'Unknown';
    if (!identity.length) return 'Colorless';
    return identity.length > 1 ? 'Multicolor' : COLORS.find(([key]) => key === identity[0])[1];
}

async function loadScanned() {
    // Cards scanned on the scanner page wait there until they are added to the collection
    const data = await api('/api/stats');
    const waiting = data && data.inventory ? data.inventory.total_cards : 0;
    $('scanned-notice').hidden = !waiting;
    $('scanned-text').textContent = `${plural(waiting, 'scanned card')} ${waiting === 1 ? 'is' : 'are'} waiting on the scanner page.`;
}

async function addScanned() {
    if (await addScannedToCollection()) loadInventory();
}

async function loadInventory() {
    loadScanned();
    const data = await api('/api/inventory');
    if (!data) {
        $('inventory-list').innerHTML = '<div class="empty-state is-error">Failed to load the inventory</div>';
        return;
    }
    inventory = data.cards;
    selected = new Set([...selected].filter(id => inventory.some(card => card.id === id)));
    fillFilterOptions();
    if (savedFilters) restoreFilters();
    if ($('filter-spare').checked) await loadSuggested();
    applyFilters();
    if (currentTab === 'stats') renderStats();
}

async function loadSuggested() {
    // The owned cards EDHREC lists for the decks' commanders (slow the first time: one request per deck)
    $('inventory-list').innerHTML = '<div class="hint">Asking EDHREC about your commanders...</div>';
    $('filter-spare').disabled = true;
    const data = await api('/api/inventory/suggested');
    $('filter-spare').disabled = false;
    suggested = data ? data.cards : null;
    if (!data) $('filter-spare').checked = false;
    else if (data.unknown.length) notify(`No EDHREC data for ${data.unknown.join(', ')} - not counted`, 'warning');
}

async function spareChanged() {
    if ($('filter-spare').checked) await loadSuggested();
    page = 0;
    applyFilters();
}

function fillSelect(id, label, values, labels = {}) {
    const select = $(id);
    const current = select.value;
    select.innerHTML = optionsHtml([['', label], ...values.map(value => [value, labels[value] || value])]);
    select.value = values.includes(current) ? current : '';
    select.hidden = values.length === 0;
}

function distinct(values) {
    return [...new Set(values.filter(Boolean))].sort(byText);
}

function fillFilterOptions() {
    fillSelect('filter-type', 'Any type', distinct(inventory.map(mainType)));
    fillSelect('filter-rarity', 'Any rarity', distinct(inventory.map(card => card.rarity))
        .sort((a, b) => (RARITY_ORDER[a] ?? 9) - (RARITY_ORDER[b] ?? 9)));
    fillSelect('filter-set', 'Any set', distinct(inventory.map(card => card.set_name)));
    fillSelect('filter-finish', 'Any finish', gameInfo.finishes.map(([key]) => key),
               Object.fromEntries(gameInfo.finishes));
    fillLocationFilter();
    fillSelect('filter-tag', 'Any tag', distinct(inventory.flatMap(card => card.tags)));
    // The last times cards came into the collection (one per "Add to collection"), newest first
    const batches = new Map();
    // added_quantity 0: the entry's batch was removed, its copies were there before
    inventory.filter(card => card.added_quantity).forEach(card => batches.set(card.added_at, (batches.get(card.added_at) || 0) + card.added_quantity));
    const times = [...batches.keys()].sort().reverse().slice(0, 20);
    fillSelect('filter-added', 'Added any time', times,
               Object.fromEntries(times.map(time => [time, `Added ${time.slice(0, 16)} (${plural(batches.get(time), 'card')})`])));
    // Color chips only where the card data has color identities (Magic)
    $('filter-free-label').hidden = !gameInfo.deck_formats.length;
    $('filter-spare-label').hidden = !gameInfo.deck_formats.some(([, , commander]) => commander);
    const hasColors = inventory.some(card => identityOf(card));
    $('filter-colors').innerHTML = hasColors ? colorChipsHtml(filterColors) : '';
}

function colorChipsHtml(active) {
    return COLORS.map(([key, label]) =>
        `<button class="color-chip mana-${key} ${active.has(key) ? 'is-active' : ''}" data-color="${key}" title="${label}">${key}</button>`).join('');
}

function fillLocationFilter() {
    // Boxes and binders first; the locations named after one of the decks under their own
    // heading at the bottom (a deck's cards are usually kept under the deck's name)
    const decks = new Set(deckList.map(deck => deck.name.toLowerCase()));
    const places = knownLocations();
    const inDecks = places.filter(place => decks.has(place.toLowerCase()));
    const select = $('filter-location');
    const current = select.value;
    select.innerHTML = optionsHtml([['', 'Any location'], ['(none)', '(none)'],
                                    ...places.filter(place => !inDecks.includes(place)).map(place => [place, place])])
        + (inDecks.length ? decksHeadingHtml(select, ['Any location', ...places]) + optionsHtml(inDecks.map(place => [place, place])) : '');
    select.value = current === '(none)' || places.includes(current) ? current : '';
    select.hidden = false;
}

function knownLocations() {
    return distinct(inventory.map(card => card.location));
}

// Search terms that say where to look: set:HOB, rarity:rare, loc:"Binder 2", -tag:trade.
// A set is its code, whole (HOB does not find the cards with "hob" in their name); typed
// without a known code, part of the set's name. Tags, locations, finishes and numbers are whole
const SEARCH_FIELDS = {
    set: (card, value, setCodes) => setCodes.has(value) ? (card.set_code || '').toLowerCase() === value
                                                        : (card.set_name || '').toLowerCase().includes(value),
    name: (card, value) => (card.name || '').toLowerCase().includes(value),
    type: (card, value) => (card.type_line || '').toLowerCase().includes(value),
    rarity: (card, value) => (card.rarity || '').toLowerCase().startsWith(value),
    tag: (card, value) => card.tags.some(tag => tag.toLowerCase() === value),
    loc: (card, value) => (card.location || '').toLowerCase() === value,
    finish: (card, value) => [card.finish, finishLabel(card.finish)].some(finish => (finish || '').toLowerCase() === value),
    number: (card, value) => String(card.number || '').toLowerCase() === value,
    // qty:>4, qty:<=2, qty:3 - how many copies the entry has
    qty: (card, value) => {
        const [, sign, number] = /^(>=|<=|>|<|=)?(\d+)$/.exec(value) || [];
        if (number === undefined) return false;
        const copies = card.quantity, wanted = parseInt(number);
        return sign === '>' ? copies > wanted : sign === '<' ? copies < wanted : sign === '>=' ? copies >= wanted
            : sign === '<=' ? copies <= wanted : copies === wanted;
    },
    // Set aside for a trade with this in its name; trade:"" = for any trade
    trade: (card, value) => (card.trades || []).some(trade => trade.name.toLowerCase().includes(value)),
};
SEARCH_FIELDS.location = SEARCH_FIELDS.loc;

function parseSearch(text) {
    // {terms: [{field, value, not}], phrase: what is left, looked for everywhere}. A word with
    // a colon that names no field stays text ("Circle of Protection: Red", "foo:bar")
    const terms = [];
    const phrase = text.toLowerCase().replace(/(^|\s)(-?)([a-z]+):(?:"([^"]*)"|(\S+))/g, (all, space, not, field, quoted, word) => {
        if (!SEARCH_FIELDS[field]) return all;
        terms.push({field, value: quoted !== undefined ? quoted.trim() : word, not: !!not});
        return ' ';
    });
    return {terms, phrase: phrase.replace(/\s+/g, ' ').trim()};
}

function activeFilters() {
    // [{text, clear}] of what narrows the list now
    const chips = [];
    const chosen = (id, label) => {
        if ($(id).value) chips.push({text: `${label}: ${$(id).selectedOptions[0].textContent}`, clear: () => { $(id).value = ''; }});
    };
    const text = $('filter-text').value.trim();
    if (text) {
        chips.push({text: `${$('filter-text-not').checked ? 'Not' : 'Search'}: ${text}`,
                    clear: () => { $('filter-text').value = ''; $('filter-text-not').checked = false; }});
    }
    if (filterColors.size) {
        chips.push({text: `Colors: ${[...filterColors].join(' ')}`, clear: () => { filterColors.clear(); fillFilterOptions(); }});
    }
    chosen('filter-type', 'Type');
    chosen('filter-rarity', 'Rarity');
    chosen('filter-set', 'Set');
    chosen('filter-finish', 'Finish');
    chosen('filter-location', 'Location');
    chosen('filter-tag', 'Tag');
    if ($('filter-added').value) chips.push({text: $('filter-added').selectedOptions[0].textContent, clear: () => { $('filter-added').value = ''; }});
    if ($('filter-free').checked) chips.push({text: 'Not in a deck', clear: () => { $('filter-free').checked = false; }});
    if ($('filter-spare').checked) chips.push({text: 'No use in my decks', clear: () => { $('filter-spare').checked = false; }});
    const min = $('filter-price-min').value, max = $('filter-price-max').value;
    if (min || max) {
        chips.push({text: 'Price: ' + (min && max ? `$${min} – $${max}` : min ? `from $${min}` : `up to $${max}`),
                    clear: () => { $('filter-price-min').value = ''; $('filter-price-max').value = ''; }});
    }
    return chips;
}

function filterByBadge(filter, value) {
    // A click on a row's badge shows only the cards with it; a second click takes that off again
    if (filter === 'trade') {
        const term = `trade:"${value.toLowerCase()}"`, field = $('filter-text');
        field.value = field.value.toLowerCase().includes(term)
            ? field.value.replace(new RegExp(term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i'), '').replace(/\s+/g, ' ').trim()
            : `${field.value.trim()} ${term}`.trim();
    } else {
        const select = $(filter);
        select.value = select.value === value ? '' : value;
        if (select.selectedIndex === -1) select.value = '';
    }
    filtersChanged();
    window.scrollTo(0, 0);
}

function applyFilters() {
    const {terms, phrase} = parseSearch($('filter-text').value);
    const setCodes = new Set(inventory.map(card => (card.set_code || '').toLowerCase()).filter(Boolean));
    const textMatches = card =>
        (!phrase || [card.name, card.set_name, card.set_code, card.type_line, card.rarity, card.location, ...card.tags]
            .some(value => (value || '').toLowerCase().includes(phrase)))
        && terms.every(term => SEARCH_FIELDS[term.field](card, term.value, setCodes) !== term.not);
    const textNot = $('filter-text-not').checked;
    const type = $('filter-type').value, rarity = $('filter-rarity').value, set = $('filter-set').value;
    const finish = $('filter-finish').value, location = $('filter-location').value, tag = $('filter-tag').value;
    const added = $('filter-added').value;
    const free = $('filter-free').checked;
    const spare = $('filter-spare').checked && suggested;
    const min = parseFloat($('filter-price-min').value), max = parseFloat($('filter-price-max').value);
    const rows = inventory.filter(card => {
        // "Not" turns the text search around: the cards that don't have the text anywhere
        if ((phrase || terms.length) && textMatches(card) === textNot) return false;
        if (type && mainType(card) !== type) return false;
        if (rarity && card.rarity !== rarity) return false;
        if (set && card.set_name !== set) return false;
        if (finish && card.finish !== finish) return false;
        if (location && card.location !== (location === '(none)' ? '' : location)) return false;
        if (tag && !card.tags.includes(tag)) return false;
        if (added && (card.added_at !== added || !card.added_quantity)) return false;
        if (free && card.decks.length) return false;
        if (spare && (card.decks.length || suggested[card.name])) return false;
        if (!isNaN(min) && card.price < min) return false;
        if (!isNaN(max) && card.price > max) return false;
        if (filterColors.size) {
            // Every chosen color must be in the card's identity; C = colorless
            const identity = identityOf(card) || [];
            if (filterColors.has('C') ? identity.length > 0 : ![...filterColors].every(color => identity.includes(color))) return false;
        }
        return true;
    });
    $('batch-remove').hidden = !added;
    // Filters come back on the next visit: show what narrows the list, each to be taken off
    const chips = activeFilters();
    $('filter-chips').innerHTML = chips.map((chip, index) =>
        `<button class="filter-chip" data-chip="${index}" title="Take this filter off">${escapeHtml(chip.text)} <span>×</span></button>`).join('');
    $('filter-chips').hidden = !chips.length;
    $('filter-clear').disabled = !chips.length;
    const sort = recall('collectionSort', 'added');
    $('inventory-sort').value = sort in INVENTORY_SORTS ? sort : 'added';
    shown = INVENTORY_SORTS[sort] ? [...rows].sort(INVENTORY_SORTS[sort]) : rows;
    // Only cards the filters show can be selected: a bulk action must not reach cards out of sight
    const visible = new Set(rows.map(card => card.id));
    selected = new Set([...selected].filter(id => visible.has(id)));
    renderInventory();
}

function badgesHtml(card) {
    const rarity = (card.rarity || '').toLowerCase();
    const only = (filter, value) => `data-filter="${filter}" data-value="${escapeHtml(value)}"`;  // filterByBadge
    return `
        ${rarity ? `<span class="inventory-badge ${escapeHtml(rarity)}" ${only('filter-rarity', card.rarity)} title="Rarity - click to show only these">${escapeHtml(rarity.toUpperCase())}</span>` : ''}
        ${card.finish !== defaultFinish() ? `<span class="inventory-badge ${escapeHtml(card.finish)}" ${only('filter-finish', card.finish)} title="Finish - click to show only these">${escapeHtml(finishLabel(card.finish).toUpperCase())}</span>` : ''}
        ${card.location ? `<span class="inventory-badge location" ${only('filter-location', card.location)} title="Location - click to show only these">${escapeHtml(card.location)}</span>` : ''}
        ${card.tags.map(tag => `<span class="inventory-badge tag" ${only('filter-tag', tag)} title="Tag - click to show only these">${escapeHtml(tag)}</span>`).join('')}
        ${(card.trades || []).map(trade => `<span class="inventory-badge trade" ${only('trade', trade.name)} title="Set aside for the trade &quot;${escapeHtml(trade.name)}&quot; (Trades tab) - click to show only these">Trade: ${
            escapeHtml(trade.name)}${trade.quantity < card.quantity ? ` ${trade.quantity}×` : ''}</span>`).join('')}
        ${card.decks.length ? `<span class="inventory-badge deck" title="Used in: ${escapeHtml(card.decks.join(', '))}">${
            escapeHtml(card.decks.length === 1 ? card.decks[0] : plural(card.decks.length, 'deck'))}</span>` : ''}`;
}

function waiting(card) {
    // Part of a change that is not sent yet (changeLater)
    return !!pendingChange && pendingChange.ids.has(card.id);
}

function gridCardHtml(card) {
    const image = (card.details && card.details.image_uri) || (card.captures[0] && card.captures[0].url);
    return `
        <div class="grid-card ${selected.has(card.id) ? 'is-selected' : ''} ${waiting(card) ? 'is-pending' : ''}" data-id="${card.id}">
            ${image ? `<img src="${escapeHtml(image)}" alt="${escapeHtml(card.name)}" loading="lazy">`
                    : `<div class="no-image">${escapeHtml(card.name)}</div>`}
            <input type="checkbox" class="row-check" ${selected.has(card.id) ? 'checked' : ''} aria-label="Select">
            ${card.quantity > 1 ? `<span class="grid-qty">${card.quantity}×</span>` : ''}
            <div class="grid-name" title="${escapeHtml(card.name)}">${escapeHtml(card.name)}</div>
            <div class="grid-meta"><span>${escapeHtml((card.set_code || card.set_name).toUpperCase())} ${escapeHtml(card.number)}</span><span>${money(card.price)}</span></div>
            <div class="inventory-card-meta">${badgesHtml(card)}</div>
        </div>`;
}

function listRowHtml(card) {
    // On the left what was captured (a click opens the captures); by the buttons the card's
    // downloaded picture, small - the pointer on it shows it large (showPreview)
    const picture = (card.details && card.details.image_uri) || '';
    const image = (card.captures[0] && card.captures[0].url) || picture;
    return `
        <div class="inventory-card ${selected.has(card.id) ? 'is-selected' : ''} ${waiting(card) ? 'is-pending' : ''}" data-id="${card.id}">
            <input type="checkbox" class="row-check" ${selected.has(card.id) ? 'checked' : ''} aria-label="Select">
            ${image ? `<button class="inventory-thumb" title="${card.captures.length ? plural(card.captures.length, 'capture') : 'Card image'}">
                           <img src="${escapeHtml(image)}" alt="" loading="lazy"></button>`
                    : '<div class="inventory-thumb empty" title="No image"></div>'}
            <div class="inventory-card-info">
                <div class="inventory-card-name">
                    ${card.quantity > 1 ? `<span class="inventory-qty">${card.quantity}×</span> ` : ''}${escapeHtml(card.name)}
                    ${manaHtml(card.mana_cost)}
                </div>
                <div class="inventory-card-details">
                    ${escapeHtml(card.set_name)} ${card.number ? '#' + escapeHtml(card.number) : ''}
                    ${card.type_line ? '· ' + escapeHtml(card.type_line) : ''}
                </div>
                <div class="inventory-card-meta">${badgesHtml(card)}<span>${escapeHtml(card.condition)}</span></div>
            </div>
            <div class="inventory-card-price">
                <div class="inventory-price-value">${money(card.price * card.quantity)}</div>
                ${card.quantity > 1 ? `<div class="inventory-price-each">${money(card.price)} each</div>` : ''}
                <div class="inventory-timestamp" title="Scanned ${escapeHtml(card.timestamp)}">Added ${card.added_quantity ? '' : 'before '}${escapeHtml(card.added_at.slice(0, 16))}${card.added_quantity && card.added_quantity < card.quantity ? ` (+${card.added_quantity})` : ''}</div>
            </div>
            <div class="inventory-card-actions">
                ${picture ? `<span class="card-peek" data-image="${escapeHtml(picture)}" title="The card's picture - rest the pointer here to see it large">
                                 <img src="${escapeHtml(picture.replace('/normal/', '/small/'))}" alt="" loading="lazy"></span>` : ''}
                <button class="btn-trade" title="Set aside for a trade: it stays in your collection until you confirm the trade"><svg class="icon"><use href="#i-swap"/></svg></button>
                <button class="btn-edit" title="Edit"><svg class="icon"><use href="#i-edit"/></svg></button>
                <button class="btn-delete" title="Delete"><svg class="icon"><use href="#i-trash"/></svg></button>
            </div>
        </div>`;
}

function renderInventory() {
    const list = $('inventory-list');
    const cards = totalQuantity(shown);
    const value = shown.reduce((sum, card) => sum + card.price * card.quantity, 0);
    $('inventory-summary').innerHTML = `<strong>${entriesText(shown.length)}</strong> · ${plural(cards, 'card')} · <strong>${money(value)}</strong>`
        + (shown.length !== inventory.length ? ` (of ${inventory.length})` : '');
    list.classList.toggle('is-grid', inventoryView === 'grid');
    markSwitch('view-switch', 'view', inventoryView);

    if (!shown.length) {
        list.classList.remove('is-grid');
        list.innerHTML = inventory.length
            ? '<div class="empty-state">No cards match the filters.</div>'
            : '<div class="empty-state">No cards in the inventory yet.<br>Scan some cards to build your collection.</div>';
    } else {
        // The page stays where it was when the list is loaded again (after an edit); a filter
        // that leaves fewer pages ends on the last one
        const pages = Math.ceil(shown.length / pageSize);
        page = Math.max(0, Math.min(page, pages - 1));
        const first = page * pageSize;
        list.innerHTML = shown.slice(first, first + pageSize).map(inventoryView === 'grid' ? gridCardHtml : listRowHtml).join('');
        $('page-text').textContent = `${(first + 1).toLocaleString()}–${Math.min(first + pageSize, shown.length).toLocaleString()} of ${shown.length.toLocaleString()} · page ${page + 1} of ${pages}`;
        $('page-first').disabled = $('page-prev').disabled = page === 0;
        $('page-next').disabled = $('page-last').disabled = page >= pages - 1;
    }
    $('inventory-pager').hidden = shown.length <= pageSize;
    $('inventory-page-size').value = pageSize;
    renderBulkBar();
}

function renderBulkBar() {
    $('bulk-bar').hidden = selected.size === 0;
    const copies = totalQuantity(inventory.filter(card => selected.has(card.id)));
    $('bulk-count').textContent = `${entriesText(selected.size)} selected (${plural(copies, 'card')})`;
    $('bulk-deck').hidden = !gameInfo.deck_formats.length;
    // "Select all": ticked when every card the filters show is selected, a dash when some are
    const chosen = shown.filter(card => selected.has(card.id)).length;
    $('select-all').checked = shown.length > 0 && chosen === shown.length;
    $('select-all').indeterminate = chosen > 0 && chosen < shown.length;
    $('select-all').disabled = !shown.length;
}

function rowCard(row) {
    // The entry of a list row or grid card (data-id)
    return row && inventory.find(entry => entry.id === parseInt(row.dataset.id));
}

function toggleSelected(id, on) {
    if (on) selected.add(id); else selected.delete(id);
    const row = document.querySelector(`#inventory-list [data-id="${id}"]`);
    if (row) {
        row.classList.toggle('is-selected', on);
        row.querySelector('.row-check').checked = on;
    }
    renderBulkBar();
}

function pick(id, on, range) {
    // A tick; with Shift held, every entry from the one ticked before to this one (in the
    // order shown, also across pages) is ticked or unticked like it
    const from = range ? shown.findIndex(card => card.id === lastPicked) : -1;
    const to = shown.findIndex(card => card.id === id);
    lastPicked = id;
    if (from < 0 || to < 0 || from === to) return toggleSelected(id, on);
    shown.slice(Math.min(from, to), Math.max(from, to) + 1).forEach(card => on ? selected.add(card.id) : selected.delete(card.id));
    renderInventory();
}

function inventoryKey(event) {
    // Keys of the Inventory tab, when no dialog is open and nothing is being typed:
    // / search, Esc clear the selection, arrows previous / next page, Ctrl+A select all shown,
    // Del delete the selection, Ctrl+Z undo
    if (currentTab !== 'inventory' || document.querySelector('.modal.show, .drawer.show')) return;
    const typing = event.target.matches('input, textarea, select') || event.target.isContentEditable;
    const command = event.ctrlKey || event.metaKey;
    if (event.key === 'Escape' && event.target === $('filter-text')) return $('filter-text').blur();
    if (typing || event.altKey) return;  // Ctrl+Z in a text field is the field's own
    if (command && event.key.toLowerCase() === 'z' && pendingChange) {
        event.preventDefault();
        undoPending();
    } else if (event.key === '/' && !command) {
        event.preventDefault();
        $('filter-text').focus();
        $('filter-text').select();
    } else if (event.key === 'Escape' && selected.size) {
        selected.clear();
        renderInventory();
    } else if (command && event.key.toLowerCase() === 'a' && shown.length) {
        event.preventDefault();
        shown.forEach(card => selected.add(card.id));
        renderInventory();
    } else if (event.key === 'Delete' && selected.size) {
        bulkAction('delete');
    } else if ((event.key === 'ArrowLeft' || event.key === 'ArrowRight') && !command && !event.shiftKey) {
        const next = page + (event.key === 'ArrowRight' ? 1 : -1);
        if (next >= 0 && next < Math.ceil(shown.length / pageSize)) showPage(next);
    }
}

function showPage(number) {
    page = number;
    renderInventory();
    // The buttons are below the list: back to its first row, when that is out of sight
    const topbar = document.querySelector('.topbar');
    const above = $('inventory-list').getBoundingClientRect().top - (topbar ? topbar.offsetHeight : 0) - 12;
    if (above < 0) window.scrollBy(0, above);
}

// -- One value for a bulk action ----------------------------------------------

let valueResolve = null;

function valueDialog({title, label, options = [], choices = null, value = ''}) {
    // Free text with suggestions (options), or one of choices ([[value, label]]); resolves
    // with the value, or null when cancelled
    return new Promise(resolve => {
        valueResolve = resolve;
        $('value-title').textContent = title;
        $('value-label').textContent = label;
        const input = $('value-input'), select = $('value-select');
        input.hidden = !!choices;
        select.hidden = !choices;
        if (choices) {
            select.innerHTML = optionsHtml(choices);
        } else {
            input.value = value;
            $('value-options').innerHTML = options.map(option => `<option value="${escapeHtml(option)}"></option>`).join('');
        }
        $('value-modal').classList.add('show');
        (choices ? select : input).focus();
    });
}

function closeValueDialog(value) {
    closeModal('value-modal');
    const resolve = valueResolve;
    valueResolve = null;
    if (resolve) resolve(value);
}

async function bulkAction(action) {
    const ids = [...selected];
    const entries = entriesText(ids.length);
    let value = '';
    if (action === 'delete') {
        const ok = await confirmDialog({title: `Delete ${entries}?`, confirmText: 'Delete', danger: true,
            message: `The selected cards are removed from your inventory. You have ${UNDO_SECONDS} seconds to undo it.`});
        if (!ok) return;
    } else if (action === 'location') {
        value = await valueDialog({title: `Move ${entries}`, label: 'Location (empty: none)', options: knownLocations()});
        if (value === null) return;
    } else if (action === 'add_tag' || action === 'remove_tag') {
        const tags = distinct(inventory.filter(card => action === 'add_tag' || selected.has(card.id)).flatMap(card => card.tags));
        value = await valueDialog({title: `${action === 'add_tag' ? 'Tag' : 'Remove a tag from'} ${entries}`, label: 'Tag', options: tags});
        if (!value) return;
    } else if (action === 'condition') {
        value = await valueDialog({title: `Condition of ${entries}`, label: 'Condition', choices: CONDITIONS.map(c => [c, c])});
        if (!value) return;
    } else if (action === 'deck') {
        return addSelectionToDeck();
    } else if (action === 'trade') {
        return setAsideForTrade();
    }
    const what = {delete: `Deleting ${entries}`, location: `Moving ${entries} to ${value || 'no location'}`,
                  add_tag: `Tagging ${entries} "${value}"`, remove_tag: `Taking the tag "${value}" off ${entries}`,
                  condition: `Setting ${entries} to ${value}`}[action];
    changeLater(ids, what, {ids, action, value});
}

// -- Undo: a change waits a few seconds before it is sent ---------------------

let pendingChange = null;            // {ids: Set of entry ids, body: for /api/inventory/bulk, timer}
let pendingSent = Promise.resolve(); // the change being sent
let changesAsked = Promise.resolve(); // changeLater, one call after the other
const plainFetch = window.fetch.bind(window);
// Whatever else writes (an edit, a trade, a backup) goes after the change that waits, never
// around it: it is sent first
window.fetch = async (input, options) => {
    if (((options && options.method) || 'GET').toUpperCase() !== 'GET') await sendPending();
    return plainFetch(input, options);
};

function changeLater(ids, text, body) {
    // A delete or a bulk change: its rows are greyed and the server hears of it when the time
    // to undo is over - or at once when something else is changed or the page is left.
    // One call at a time: two asked for while an earlier change was on its way both waited
    // for it, and the second then took the first one's place - which was never sent
    changesAsked = changesAsked.then(async () => {
        await sendPending();
        pendingChange = {ids: new Set(ids), body, timer: setTimeout(sendPending, UNDO_SECONDS * 1000)};
        $('undo-text').textContent = text;
        $('undo-bar').hidden = false;
        $('undo-time').style.animation = 'none';
        $('undo-time').offsetWidth;  // the bar starts to run out again
        $('undo-time').style.animation = `undoTime ${UNDO_SECONDS}s linear forwards`;
        // Done with these cards: left selected, they went along with the next "Select all" and
        // its move (59 cards put in one box ended up in another, 2026-10-06)
        selected.clear();
        renderInventory();
    });
    return changesAsked;
}

function sendPending() {
    const change = pendingChange;
    if (!change) return pendingSent;
    pendingChange = null;
    clearTimeout(change.timer);
    $('undo-bar').hidden = true;
    pendingSent = (async () => {
        try {
            // keepalive: the request is finished also when the page is left while it is on
            // its way (browsers take up to 64 KB that way - some 8,000 entries)
            const body = JSON.stringify(change.body);
            const response = await plainFetch('/api/inventory/bulk', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                                      body, keepalive: body.length < 60000});
            const data = await response.json();
            if (data.success === false) notify(data.error || 'Request failed', 'error');
            else notify(`${entriesText(data.changed)} ${change.body.action === 'delete' ? 'deleted' : 'changed'}`, 'success');
        } catch (error) {
            notify(`Request failed: ${error.message}`, 'error');
        }
        await loadInventory();
    })();
    return pendingSent;
}

function undoPending() {
    if (!pendingChange) return;
    clearTimeout(pendingChange.timer);
    pendingChange = null;
    $('undo-bar').hidden = true;
    notify('Undone - nothing was changed', 'info');
    renderInventory();
}

async function removeBatch() {
    // Undo an "Add to collection": only the copies that came with it go
    const added = $('filter-added').value;
    const batch = inventory.filter(card => card.added_at === added && card.added_quantity);
    const cards = batch.reduce((sum, card) => sum + card.added_quantity, 0);
    const kept = batch.filter(card => card.added_quantity < card.quantity).length;
    const ok = await confirmDialog({title: `Remove the ${plural(cards, 'card')} added ${added.slice(0, 16)}?`, confirmText: 'Remove', danger: true,
        message: `They are deleted from the collection (not moved back to the scanner).`
            + (kept ? ` ${entriesText(kept)} had copies before and keep${kept === 1 ? 's' : ''} those.` : '')
            + " This can't be undone."});
    if (!ok) return;
    const data = await api('/api/inventory/remove_batch', {method: 'POST', body: {added_at: added}});
    if (!data) return;
    notify(`${plural(data.cards, 'card')} removed`, 'success');
    $('filter-added').value = '';
    loadInventory();
}

async function addSelectionToDeck() {
    const data = await api('/api/decks');
    if (!data) return;
    const choice = await valueDialog({title: 'Add to a deck', label: 'Deck (one copy of each selected card)',
        choices: [...data.decks.map(deck => [deck.id, deck.name]), ['new', 'New deck...']]});
    if (!choice) return;
    const text = distinct(inventory.filter(card => selected.has(card.id)).map(card => card.name)).map(name => `1 ${name}`).join('\n');
    if (choice === 'new') {
        showTab('decks');
        return openDeckModal({text});
    }
    const result = await api(`/api/decks/${choice}/import`, {method: 'POST', body: {text}});
    if (result) notify(`${plural(result.added, 'card')} added to ${result.deck.name}`, 'success');
}

// -- Export / import -----------------------------------------------------------

function renderExportMenu() {
    // The sites / apps the collection can be exported for (Game.export_formats), and read from
    const select = $('export-select');
    select.innerHTML = '<option value="">Export…</option>' + gameInfo.exports.map(([format, label]) =>
        `<option value="${escapeHtml(format)}">${escapeHtml(label)}</option>`).join('');
    select.hidden = !gameInfo.exports.length;
    $('import-button').title = `Add the cards of a ${importFormats()} file to your collection`;
}

function importFormats() {
    return [...gameInfo.imports, 'Card Scanner'].join(' or ');
}

async function exportInventory() {
    // The server sends the file as a download; the menu goes back to "Export…"
    const select = $('export-select');
    const format = select.value;
    if (!format) return;
    const label = select.selectedOptions[0].textContent;
    select.value = '';
    // With cards ticked or the list filtered: which of them
    let part = 'all';
    if (selected.size || shown.length !== inventory.length) {
        // Short labels: three buttons share the dialog's one row
        part = await choiceDialog({title: `Export for ${label}`, message: 'Which cards go into the file?', choices: [
            {label: `All ${inventory.length.toLocaleString()}`, value: 'all'},
            ...(shown.length !== inventory.length ? [{label: `${shown.length.toLocaleString()} shown`, value: 'shown', style: selected.size ? undefined : 'primary'}] : []),
            ...(selected.size ? [{label: `${selected.size.toLocaleString()} selected`, value: 'selected', style: 'primary'}] : [])]});
        if (!part) return;
    }
    const link = document.createElement('a');
    if (part === 'all') {
        link.href = `/api/export_inventory/${encodeURIComponent(format)}`;
        link.download = '';
    } else {
        const ids = part === 'selected' ? [...selected] : shown.map(card => card.id);
        let response;
        try {
            response = await plainFetch(`/api/export_inventory/${encodeURIComponent(format)}`, {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ids})});
        } catch (error) {
            return notify(`Export failed: ${error.message}`, 'error');
        }
        if (!response.ok) return notify('Export failed', 'error');
        link.href = URL.createObjectURL(await response.blob());
        link.download = (/filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '') || [])[1] || 'export.csv';
        setTimeout(() => URL.revokeObjectURL(link.href), 60000);
    }
    document.body.appendChild(link);
    link.click();
    link.remove();
    notify(`${label} export started - check your downloads folder`
        + (format === 'moxfield' ? '. Import it at moxfield.com/account/collection'
            : format === 'csv' ? '. Every entry with its location and tags; Import reads it back' : ''), 'success');
}

async function importInventory() {
    const input = $('import-file-input');
    const file = input.files[0];
    if (!file) return;
    const mode = await choiceDialog({
        title: `Import ${file.name}`,
        message: `Reads a ${importFormats()} file. Add its cards to your collection (quantities of matching cards are added up), or replace your whole collection with this file?`,
        choices: [
            {label: 'Cancel', value: null},
            {label: 'Replace inventory', value: 'replace', style: 'danger'},
            {label: 'Add to inventory', value: 'merge', style: 'primary'}
        ]
    });
    if (mode) {
        const form = new FormData();
        form.append('file', file);
        form.append('replace_existing', mode === 'replace' ? 'true' : 'false');
        const data = await api('/api/import_inventory', {method: 'POST', body: form});
        if (data) {
            const stats = data.stats;
            const missing = stats.not_found.length
                ? ` Not found: ${stats.not_found.join(', ')}${stats.skipped > stats.not_found.length ? ', ...' : ''}.` : '';
            notify(`${stats.format} import: ${stats.added} added, ${stats.updated} added to cards you had`
                + (stats.by_name ? `, ${stats.by_name} matched by name only (check their printing)` : '')
                + (stats.skipped ? `, ${stats.skipped} skipped.` : '.') + (stats.errors ? ` ${stats.errors} errors.` : '') + missing,
                stats.skipped || stats.errors ? 'warning' : 'success');
            loadInventory();
        }
    }
    input.value = '';
}

// ============================================================================
// Statistics (from the loaded inventory)
// ============================================================================

let statsMeasure = 'value';

function barsHtml(rows, format, limit = 12) {
    // rows: [[label, number]] - largest first; the rest folds into "Other"
    rows = rows.filter(([, number]) => number > 0).sort((a, b) => b[1] - a[1]);
    if (rows.length > limit) {
        const rest = rows.slice(limit - 1);
        rows = [...rows.slice(0, limit - 1), [`Other (${rest.length})`, rest.reduce((sum, [, number]) => sum + number, 0)]];
    }
    const top = Math.max(...rows.map(([, number]) => number), 0);
    if (!rows.length) return '<div class="hint">Nothing to show</div>';
    return rows.map(([label, number]) => `
        <div class="bar-row" title="${escapeHtml(label)}: ${format(number)}">
            <span class="bar-label">${escapeHtml(label)}</span>
            <span class="bar-track"><div class="bar-fill" style="width: ${top ? (100 * number / top).toFixed(1) : 0}%"></div></span>
            <span class="bar-value">${format(number)}</span>
        </div>`).join('');
}

function groupTotals(rows, keyOf, measure) {
    const totals = new Map();
    rows.forEach(card => {
        const amount = measure === 'value' ? card.price * card.quantity : card.quantity;
        [].concat(keyOf(card)).forEach(key => totals.set(key, (totals.get(key) || 0) + amount));
    });
    return [...totals.entries()];
}

function renderStats() {
    const cards = totalQuantity(inventory);
    const value = inventory.reduce((sum, card) => sum + card.price * card.quantity, 0);
    const tile = (number, label) => `
        <div class="inventory-stat"><div class="inventory-stat-value">${number}</div><div class="inventory-stat-label">${label}</div></div>`;
    $('stats-tiles').innerHTML = tile(cards.toLocaleString(), 'Cards') + tile(inventory.length.toLocaleString(), 'Entries')
        + tile(new Set(inventory.map(card => card.name)).size.toLocaleString(), 'Different cards')
        + tile(money(value), 'Total value') + tile(money(cards ? value / cards : 0), 'Average per card');
    markSwitch('stats-measure', 'measure', statsMeasure);
    const format = statsMeasure === 'value' ? money : number => number.toLocaleString();
    const chart = (title, keyOf, limit) => `
        <div class="panel"><h3 class="section-title">${title}</h3>${barsHtml(groupTotals(inventory, keyOf, statsMeasure), format, limit)}</div>`;
    const valuable = [...inventory].sort((a, b) => b.price - a.price).slice(0, 10)
        .map(card => [`${card.name} (${(card.set_code || '').toUpperCase()}${card.finish !== defaultFinish() ? ', ' + finishLabel(card.finish) : ''})`, card.price]);
    $('stats-grid').innerHTML = chart('Color', colorGroup) + chart('Type', mainType)
        + chart('Rarity', card => card.rarity ? card.rarity[0].toUpperCase() + card.rarity.slice(1) : 'Unknown') + chart('Finish', card => finishLabel(card.finish))
        + chart('Set', card => card.set_name) + chart('Location', card => card.location || 'No location')
        + (inventory.some(card => card.tags.length) ? chart('Tag', card => card.tags.length ? card.tags : ['No tag']) : '')
        + `<div class="panel"><h3 class="section-title">Most valuable cards</h3>${barsHtml(valuable, money, 10)}</div>`;
}

// ============================================================================
// Decks: list
// ============================================================================

let deck = null;          // the deck open in the builder (server payload)
let deckList = [];

function deckFormat(format) {
    // [key, label, has a commander]
    return gameInfo.deck_formats.find(([key]) => key === format);
}

function formatLabel(format) {
    const found = deckFormat(format);
    return found ? found[1] : format;
}

function hasCommander(format) {
    const found = deckFormat(format);
    return !!(found && found[2]);
}

function isCommanderCard(card) {
    return /Legendary.*Creature|can be your commander/.test(card.type_line.split(' // ')[0] + card.oracle_text);
}

async function loadDecks() {
    if (!gameInfo.deck_formats.length) return;
    const data = await api('/api/decks');
    if (!data) return;
    deckList = data.decks;
    fillLocationFilter();
    $('deck-data-notice').hidden = data.has_deck_data;
    // The formats there are decks of; a choice that is gone falls back to all
    const formats = [...new Set(deckList.map(item => item.format))];
    const chosen = $('deck-filter-format').value;
    $('deck-filter-format').innerHTML = optionsHtml([['', 'Any format'], ...gameInfo.deck_formats.filter(([key]) => formats.includes(key))]);
    $('deck-filter-format').value = formats.includes(chosen) ? chosen : '';
    $('deck-filters').hidden = deckList.length < 2;
    renderDeckList();
}

const DECK_SORTS = {
    changed: (a, b) => b.updated_at.localeCompare(a.updated_at) || b.id - a.id,
    name: (a, b) => a.name.localeCompare(b.name),
    format: (a, b) => formatLabel(a.format).localeCompare(formatLabel(b.format)) || a.name.localeCompare(b.name),
    owned: (a, b) => b.owned_percent - a.owned_percent || a.name.localeCompare(b.name),
    missing: (a, b) => (a.cards - a.owned) - (b.cards - b.owned) || a.name.localeCompare(b.name),
    cards: (a, b) => b.cards - a.cards || a.name.localeCompare(b.name),
};

function renderDeckList() {
    const words = searchWords('deck-filter-text');
    const format = $('deck-filter-format').value;
    const shown = deckList.filter(item => {
        const text = `${item.name} ${item.commanders.join(' ')}`.toLowerCase();
        return (!format || item.format === format) && words.every(word => text.includes(word));
    }).sort(DECK_SORTS[$('deck-sort').value] || DECK_SORTS.changed);
    $('deck-list').innerHTML = !shown.length && deckList.length
        ? '<div class="empty-state">No deck matches.</div>'
        : deckList.length ? shown.map(item => `
        <button class="deck-tile" data-deck="${item.id}">
            <span class="deck-tile-name">${escapeHtml(item.name)}</span>
            <span class="deck-tile-meta">${escapeHtml(formatLabel(item.format))} · ${plural(item.cards, 'card')}${item.commanders.length ? ' · ' + escapeHtml(item.commanders.join(' + ')) : ''}</span>
            <span class="meter" title="${item.owned} of ${item.cards} owned"><span style="width: ${item.owned_percent}%"></span></span>
            <span class="deck-tile-meta">${item.owned_percent}% owned · changed ${escapeHtml(item.updated_at.slice(0, 10))}</span>
        </button>`).join('')
        : '<div class="empty-state">No decks yet. Create one, or see below what your collection can build.</div>';
}

function openDeckModal({text = '', importInto = null} = {}) {
    // New deck, or (importInto: a deck) add cards to an existing one
    $('deck-modal').dataset.importInto = importInto ? importInto.id : '';
    $('deck-modal-title').textContent = importInto ? `Import cards into ${importInto.name}` : 'New deck';
    $('deck-modal-name-group').hidden = $('deck-modal-format-group').hidden = !!importInto;
    $('deck-modal-replace').hidden = !importInto;
    $('deck-modal-save').textContent = importInto ? 'Import' : 'Create';
    $('new-deck-name').value = '';
    $('new-deck-url').value = '';
    $('new-deck-text').value = text;
    $('new-deck-replace').checked = false;
    $('new-deck-format').innerHTML = optionsHtml(gameInfo.deck_formats);
    $('deck-modal').classList.add('show');
    (importInto ? $('new-deck-url') : $('new-deck-name')).focus();
}

function reportUnknown(unknown) {
    if (unknown && unknown.length) {
        notify(`Not in the card data: ${unknown.slice(0, 6).join(', ')}${unknown.length > 6 ? ` and ${unknown.length - 6} more` : ''}`, 'warning');
    }
}

async function saveDeckModal() {
    const importInto = $('deck-modal').dataset.importInto;
    const body = {text: $('new-deck-text').value.trim() || undefined, url: $('new-deck-url').value.trim() || undefined};
    const button = $('deck-modal-save');
    button.disabled = true;
    let data;
    if (importInto) {
        if (!body.text && !body.url) {
            notify('Paste a decklist or a deck address', 'warning');
            button.disabled = false;
            return;
        }
        data = await api(`/api/decks/${importInto}/import`, {method: 'POST', body: {...body, replace: $('new-deck-replace').checked}});
    } else {
        // A deck from another site keeps that site's format unless the list is pasted
        data = await api('/api/decks', {method: 'POST', body: {...body, name: $('new-deck-name').value.trim() || undefined,
                                                              format: body.url ? undefined : $('new-deck-format').value}});
    }
    button.disabled = false;
    if (!data) return;
    closeModal('deck-modal');
    reportUnknown(data.unknown);
    openDeck(data.deck);
}

async function createDeck(body, message) {
    const data = await api('/api/decks', {method: 'POST', body});
    if (!data) return;
    reportUnknown(data.unknown);
    if (message) notify(message, 'success');
    openDeck(data.deck);
}

function copyDeckClicked(event) {
    // "Copy as deck" on a public deck's row
    const button = event.target.closest('[data-import-url]');
    if (button) createDeck({url: button.dataset.importUrl}, 'Deck copied');
}

// -- Preconstructed decks: browse, open as a deck, or add as owned ----------------

let precons = null;          // [{file, name, type, format, released, set}] newest first
let preconRank = {};         // file -> {owned, total, percent}, after "Rank by what I own"

async function loadPrecons() {
    if (precons) return renderPrecons();
    $('ideas-precons').innerHTML = '<div class="hint">Loading the list...</div>';
    const data = await api('/api/precons');
    if (!data) {
        $('ideas-precons').innerHTML = '<div class="hint">The list of preconstructed decks is not available right now.</div>';
        return;
    }
    precons = data.precons;
    renderPrecons();
}

function renderPrecons() {
    if (!precons) return;
    const words = searchWords('precon-search');
    const ranked = Object.keys(preconRank).length > 0;
    let rows = precons.filter(item => {
        const text = `${item.name} ${item.set} ${item.type} ${item.released.slice(0, 4)}`.toLowerCase();
        return words.every(word => text.includes(word));
    });
    // Ranked: the decks you own most of first; otherwise the newest first (as loaded)
    if (ranked) rows = [...rows].sort((a, b) => ((preconRank[b.file] || {}).percent || 0) - ((preconRank[a.file] || {}).percent || 0));
    const more = rows.length - 80;
    $('ideas-precons').innerHTML = rows.slice(0, 80).map(item => {
        const rank = preconRank[item.file];
        return `
        <div class="idea-row precon" data-file="${escapeHtml(item.file)}">
            <div><div class="idea-name">${escapeHtml(item.name)}</div>
                 <div class="idea-meta">${escapeHtml(item.type)} · ${escapeHtml(item.set)} · ${escapeHtml(item.released.slice(0, 7))}${rank ? ` · ${rank.owned} of ${rank.total} cards owned` : ''}</div></div>
            ${rank ? `<div class="idea-share"><strong>${rank.percent}%</strong><div class="meter" style="width: 70px"><span style="width: ${rank.percent}%"></span></div></div>` : '<span></span>'}
            <div class="idea-actions">
                <button class="btn btn-small" data-precon="view" title="See what is in it - nothing is saved">View cards</button>
                <button class="btn btn-small" data-precon="own" title="Add its cards to your inventory and open it as a deck">I own it</button>
            </div>
        </div>`;
    }).join('') + (more > 0 ? `<div class="hint">${more} more - type to narrow the list</div>` : '')
        || '<div class="hint">No preconstructed deck matches.</div>';
}

let viewedPrecon = null;     // the precon whose cards are shown

async function viewPrecon(item) {
    // Only a look: a deck is made by "Create deck" in the window (it once was made by opening)
    viewedPrecon = item;
    $('precon-title').textContent = item.name;
    $('precon-subtitle').textContent = `${item.type} · ${item.set} · ${item.released.slice(0, 7)}`;
    $('precon-cards').innerHTML = '<div class="hint">Loading the list...</div>';
    $('precon-create').disabled = $('precon-own').disabled = true;
    $('precon-modal').classList.add('show');
    const data = await api(`/api/precons/${encodeURIComponent(item.file)}`);
    if (viewedPrecon !== item) return;  // another one was opened meanwhile
    if (!data) return closeModal('precon-modal');
    const total = totalQuantity(data.cards);
    const have = data.cards.reduce((sum, card) => sum + Math.min(card.quantity, card.owned), 0);
    $('precon-subtitle').textContent += ` · ${plural(total, 'card')}, ${have} owned`;
    const boards = [['commander', 'Commander'], ['main', 'Main deck'], ['side', 'Sideboard']];
    $('precon-cards').innerHTML = boards.map(([board, label]) => {
        const cards = data.cards.filter(card => card.board === board).sort((a, b) => byText(a.name, b.name));
        return cards.length ? `<h4>${label} (${totalQuantity(cards)})</h4>` + cards.map(card => `
            <div class="precon-card"><span>${card.quantity}×</span><span>${escapeHtml(card.name)}</span>
                <span class="${card.owned ? '' : 'missing'}">${card.owned ? `${card.owned} owned` : 'not owned'}</span></div>`).join('') : '';
    }).join('');
    $('precon-create').disabled = $('precon-own').disabled = false;
}

async function ownPrecon(item) {
    const location = await valueDialog({
        title: `Add ${item.name} to your inventory`,
        label: 'Its cards are added as Near Mint, in the printings that come in the box. Location (empty: none)',
        options: knownLocations(), value: item.name.slice(0, 60)});
    if (location === null) return;
    const data = await api(`/api/precons/${encodeURIComponent(item.file)}/own`, {method: 'POST', body: {name: item.name, location}});
    if (!data) return;
    reportUnknown(data.unknown);
    notify(`${plural(data.added, 'card')} of ${item.name} added to your inventory`, 'success');
    preconRank = {};  // what is owned changed
    loadInventory();
    openDeck(data.deck);
}

// -- "What can I build?": searches that run on the server, polled while they go ----

const ideaTimers = {};

function renderIdeas(kind, state) {
    if (kind === 'card') return renderAround(state);
    if (kind === 'precons') {
        // The ranking fills in the browsable list
        state.items.forEach(item => { preconRank[item.file] = item; });
        $('precon-progress').innerHTML = (state.running ? `<div class="hint">Reading the lists... ${state.done} of ${state.total || '?'}</div>`
                : state.stopped ? `<div class="hint">Stopped after ${state.done} of ${state.total} lists - the rest is not ranked</div>` : '')
            + (state.error ? `<div class="callout warning">${escapeHtml(state.error)}</div>` : '');
        return renderPrecons();
    }
    const list = $(`ideas-${kind}`);
    const progress = state.running ? `<div class="hint">Looking... ${state.done} of ${state.total || '?'}</div>`
        : state.stopped ? `<div class="hint">Stopped after ${state.done} of ${state.total}</div>` : '';
    const error = state.error ? `<div class="callout warning">${escapeHtml(state.error)}</div>` : '';
    const rows = state.items.slice(0, 60).map(item => `
        <div class="idea-row" data-image="${escapeHtml(item.image_uri || '')}">
            <div><div class="idea-name">${escapeHtml(item.name)}</div>
                 <div class="idea-meta">${item.decks.toLocaleString()} decks on EDHREC · you own ${item.owned} of the ${item.total} cards played with it</div></div>
            <div class="idea-share"><strong>${item.fit}%</strong><div class="meter"><span style="width: ${item.fit}%"></span></div></div>
            <button class="btn btn-small" data-start-commander="${escapeHtml(item.name)}">Start deck</button>
        </div>`).join('');
    const empty = !state.running && !state.error && state.started && !state.items.length
        ? '<div class="hint">No legendary creatures in the inventory yet.</div>' : '';
    list.innerHTML = progress + error + rows + empty;
}

async function pollIdeas(kind, start = false, body = {}) {
    // start: begin a run (body: what to look for); otherwise ask how the current one is doing
    clearTimeout(ideaTimers[kind]);
    const state = await api(`/api/decks/ideas/${kind}`, start ? {method: 'POST', body} : {});
    if (!state) return;
    // The button that started it stops it while it runs
    const button = document.querySelector(`[data-ideas="${kind}"]`);
    if (button) {
        button.dataset.label = button.dataset.label || button.textContent;
        button.dataset.running = state.running ? '1' : '';
        button.textContent = state.running ? 'Stop' : button.dataset.label;
    }
    renderIdeas(kind, state);
    if (state.running) ideaTimers[kind] = setTimeout(() => pollIdeas(kind), 1500);
}

function stopIdeas(kind) {
    // What was found so far stays
    return pollIdeas(kind, true, {stop: true});
}

// -- Build around a card: your cards legal in a format -> public decks that play one ----

let aroundCard = null;   // the card on display (clicked in the list)
let aroundState = null;  // the last answer about the search for decks
let aroundRequest = 0;

async function loadAroundCards() {
    const request = ++aroundRequest;
    const format = $('around-format').value;
    const params = new URLSearchParams({owned: '1', format});
    if ($('around-search').value.trim()) params.set('q', $('around-search').value.trim());
    if ($('around-type').value.trim()) params.set('type', $('around-type').value.trim());
    if ($('around-free').checked) params.set('free', '1');
    // Only offered for a format that has a commander
    $('around-commanders-label').hidden = !hasCommander(format);
    if ($('around-commanders').checked && hasCommander(format)) params.set('commander', '1');
    const data = await api(`/api/cards/search?${params}`);
    if (!data || request !== aroundRequest) return;  // a later search already answered
    $('around-cards-title').textContent = `Your cards legal in ${formatLabel(format)}`;
    $('around-cards').innerHTML = data.cards.map(card => `
        <div class="result-row ${aroundCard && aroundCard.name === card.name ? 'is-chosen' : ''}" data-name="${escapeHtml(card.name)}" data-type="${escapeHtml(card.type_line)}"
             data-image="${escapeHtml(card.image_uri || '')}" data-commander="${isCommanderCard(card) ? '1' : ''}">
            <span class="result-main">
                <div><span class="result-name">${escapeHtml(card.name)}</span> ${manaHtml(card.mana_cost)}</div>
                <div class="result-sub">${escapeHtml(card.type_line)}</div>
            </span>
            <span class="result-numbers">${card.owned} owned</span>
        </div>`).join('') + (data.more ? '<div class="hint">More cards - type a name or type to narrow the list</div>' : '')
        || '<div class="hint">None of your cards match. Deck data missing? Update the card database.</div>';
}

function chooseAround(row) {
    // Shows the card; looking for decks waits for "Find decks" (it asks other sites)
    aroundCard = {name: row.dataset.name, commander: !!row.dataset.commander, format: $('around-format').value,
                  image: row.dataset.image, type: row.dataset.type};
    document.querySelectorAll('#around-cards .result-row').forEach(other => other.classList.toggle('is-chosen', other === row));
    renderAround();
}

function findAround() {
    if (!aroundCard) return;
    $('around-decks').innerHTML = '';
    pollIdeas('card', true, {card: aroundCard.name, format: aroundCard.format});
}

function renderAround(state = aroundState) {
    aroundState = state;
    const card = aroundCard;
    const found = state && state.started && state.card ? state : null;
    const running = !!(found && found.running);
    const mine = found && card && found.card === card.name && found.format === card.format;
    // A search that is running stays on display (and can be stopped) while another card is looked at
    const shown = running || mine || !card ? found : null;
    $('around-decks-title').textContent = shown ? `${formatLabel(shown.format)} decks with ${shown.card}` : 'Decks';
    const status = shown ? (running ? (shown.total ? `Reading the decks... ${shown.done} of ${shown.total}`
                : 'Asking Archidekt - for a much played card this can take half a minute...')
            : shown.stopped ? `Stopped after ${shown.done} of ${shown.total || '?'} decks`
            : `${plural(shown.items.length, 'deck')}, the ones you own most of first`)
        : card ? 'Find decks looks for public decks that play it.' : '';
    const buttons = (running ? '<button class="btn btn-small" id="around-stop">Stop</button>'
            : card ? `<button class="btn btn-small btn-primary" id="around-find">${mine ? 'Find again' : 'Find decks'}</button>` : '')
        + (card && card.commander && hasCommander(card.format)
            ? '<button class="btn btn-small" id="around-commander">Start a deck with it as commander</button>' : '');
    $('around-progress').innerHTML = (card ? `<div class="around-card">
            ${card.image ? `<img src="${escapeHtml(card.image)}" alt="${escapeHtml(card.name)}">` : ''}
            <div>
                <div class="idea-name">${escapeHtml(card.name)}</div>
                <div class="idea-meta">${escapeHtml(card.type || '')}</div>
                <div class="filter-row">${buttons}</div>
                <div class="hint">${status}</div>
            </div>
        </div>` : buttons || status ? `<div class="filter-row"><span class="hint">${status}</span><span class="spacer"></span>${buttons}</div>` : '')
        + (shown && shown.error ? `<div class="callout warning">${escapeHtml(shown.error)}</div>` : '');
    $('around-decks').innerHTML = !shown
        ? (card ? '' : '<div class="hint">Click one of your cards to see it, then Find decks to look for decks that play it.</div>')
        : shown.items.map(item => `
        <div class="idea-row precon">
            <div><div class="idea-name">${escapeHtml(item.name)}</div>
                 <div class="idea-meta">${escapeHtml(item.source)}${item.author ? ' · ' + escapeHtml(item.author) : ''} · ${item.views.toLocaleString()} views · ${item.owned} of ${item.total} cards owned</div></div>
            <div class="idea-share"><strong>${item.percent}%</strong><div class="meter" style="width: 70px"><span style="width: ${item.percent}%"></span></div></div>
            <div class="idea-actions">
                <a class="mini-btn" href="${escapeHtml(item.url)}" target="_blank" rel="noopener" title="Open on ${escapeHtml(item.source)}"><svg class="icon"><use href="#i-link"/></svg></a>
                <button class="btn btn-small" data-import-url="${escapeHtml(item.url)}" title="Open a copy as a new deck here">Copy as deck</button>
            </div>
        </div>`).join('') || (running || shown.error ? '' : '<div class="hint">No public decks found with this card in this format.</div>');
}

// ============================================================================
// Decks: builder
// ============================================================================

const BOARD_TITLES = {commander: 'Commander', main: 'Main deck', side: 'Sideboard'};
const TYPE_GROUPS = [['Creature', 'Creatures'], ['Planeswalker', 'Planeswalkers'], ['Instant', 'Instants'],
                     ['Sorcery', 'Sorceries'], ['Artifact', 'Artifacts'], ['Enchantment', 'Enchantments'],
                     ['Battle', 'Battles'], ['Land', 'Lands']];
let sidePanel = 'search';
let searchColors = new Set();
let searchOffset = 0;
let searchTimer = null;
let suggestions = null;    // {deckKey, data}: loaded per commander

function openDeck(payload) {
    deck = payload;
    suggestions = null;
    $('deck-home').hidden = true;
    $('deck-builder').hidden = false;
    $('deck-format').innerHTML = optionsHtml(gameInfo.deck_formats);
    renderDeck();
    showSide(hasCommander(deck.format) && commanders().length && deck.totals.cards < 30 ? 'suggestions' : 'search');
    window.scrollTo(0, 0);
}

function closeDeck() {
    deck = null;
    $('deck-builder').hidden = true;
    $('deck-home').hidden = false;
    loadDecks();
}

function commanders() {
    return deck.cards.filter(entry => entry.board === 'commander');
}

function commanderNames() {
    return commanders().map(entry => entry.name);
}

function updateDeck(payload) {
    // The deck after a change, and what is shown beside it
    deck = payload;
    renderDeck();
    refreshSide();
}

function sideTitle() {
    return hasCommander(deck.format) ? 'Considering (not in the deck)' : BOARD_TITLES.side;
}

function typeGroup(entry) {
    const front = ((entry.card && entry.card.type_line) || '').split(' // ')[0];
    // A creature that is also an artifact or enchantment is filed as a creature; a land always as a land
    if (front.includes('Land')) return 'Lands';
    const found = TYPE_GROUPS.find(([type]) => front.includes(type));
    return found ? found[1] : 'Other';
}

function ownHtml(entry, wanted) {
    // wanted: copies of the card this deck plays over all its boards
    const elsewhere = totalQuantity(entry.elsewhere);
    const others = entry.elsewhere.map(other => `${other.quantity} in ${other.deck}`).join(', ');
    if (entry.owned >= wanted + elsewhere) return `<span class="own have" title="You own ${entry.owned}${others ? ' - also ' + others : ''}">✓ owned</span>`;
    if (entry.owned >= wanted) return `<span class="own shared" title="You own ${entry.owned}, but other decks want it too: ${escapeHtml(others)}">⇄ shared</span>`;
    if (entry.owned > 0) return `<span class="own partly" title="You own ${entry.owned} of ${wanted}">${entry.owned} of ${wanted}</span>`;
    return '<span class="own none" title="Not in your inventory">✕ missing</span>';
}

function deckRowHtml(entry, wanted) {
    const moves = [];
    if (entry.board !== 'main') moves.push(['main', 'Main', 'Move to the main deck']);
    if (entry.board !== 'side') moves.push(['side', hasCommander(deck.format) ? 'Maybe' : 'Side', `Move to: ${sideTitle()}`]);
    const card = entry.card;
    if (canCommand(card) && entry.board !== 'commander') moves.push(['commander', 'Cmdr', 'Make it the commander']);
    return `
        <div class="deck-row" data-name="${escapeHtml(entry.name)}" data-board="${entry.board}" data-image="${escapeHtml((card && card.image_uri) || '')}">
            <span class="qty">
                <button class="mini-btn" data-change="-1" title="One less"><svg class="icon"><use href="#i-minus"/></svg></button>
                <strong>${entry.quantity}</strong>
                <button class="mini-btn" data-change="1" title="One more"><svg class="icon"><use href="#i-plus"/></svg></button>
            </span>
            <span class="result-main"><span class="result-name ${card ? '' : 'unknown'}">${escapeHtml(entry.name)}</span> ${card ? manaHtml(card.mana_cost) : '<span class="hint">not in the card data</span>'}</span>
            ${ownHtml(entry, wanted)}
            <span class="result-numbers">${card ? money(card.price * entry.quantity) : ''}</span>
            <span class="result-actions">${card ? `<button class="mini-btn printing-btn" data-printing title="Choose the printing shown">${
                escapeHtml(entry.printing.set_code ? `${entry.printing.set_code} ${entry.printing.number}` : 'Printing')}</button>` : ''}${moves.map(([board, label, title]) =>
                `<button class="mini-btn" data-move="${board}" title="${escapeHtml(title)}">${label}</button>`).join('')}</span>
        </div>`;
}

function renderDeck() {
    $('deck-name').value = deck.name;
    $('deck-format').value = deck.format;
    const totals = deck.totals;
    const considering = hasCommander(deck.format);
    $('deck-totals').innerHTML = `
        <span><strong>${totals.cards}</strong> cards${totals.side ? ` + ${totals.side} ${considering ? 'considered' : 'sideboard'}` : ''}</span>
        <span>Price <strong>${money(totals.price)}</strong></span>
        <span>${totals.missing ? `Missing <strong>${totals.missing}</strong> cards (${money(totals.missing_price)})` : totals.cards ? '<strong>You own every card</strong>' : ''}</span>`;
    $('deck-issues').innerHTML = deck.issues.length ? deck.issues.map(issue => `
        <div class="issue ${issue.level}">${issue.level === 'error' ? '✕' : '!'} ${escapeHtml(issue.message)}${issue.cards.length
            ? `<span class="issue-cards">: ${escapeHtml(issue.cards.slice(0, 12).join(', '))}${issue.cards.length > 12 ? ` and ${issue.cards.length - 12} more` : ''}</span>` : ''}</div>`).join('')
        : (totals.cards ? `<div class="issue ok">✓ Legal ${escapeHtml(formatLabel(deck.format))} deck</div>` : '');

    // Copies of each card over the boards that are played
    const played = deck.cards.filter(entry => !(considering && entry.board === 'side'));
    const wanted = {};
    played.forEach(entry => { wanted[entry.name] = (wanted[entry.name] || 0) + entry.quantity; });
    renderDeckCharts(played.filter(entry => entry.board !== 'side' && entry.card));

    const sections = [];
    const section = (title, entries) => {
        if (!entries.length) return;
        const count = totalQuantity(entries);
        sections.push(`<div class="category-title">${escapeHtml(title)} (${count})</div>`
            + entries.map(entry => deckRowHtml(entry, wanted[entry.name] || entry.quantity)).join(''));
    };
    section(BOARD_TITLES.commander, commanders());
    const main = deck.cards.filter(entry => entry.board === 'main');
    [...TYPE_GROUPS.map(([, title]) => title), 'Other'].forEach(title =>
        section(title, main.filter(entry => typeGroup(entry) === title)));
    section(sideTitle(), deck.cards.filter(entry => entry.board === 'side'));
    $('deck-cards').innerHTML = sections.join('')
        || '<div class="empty-state">No cards yet. Search on the left and click a card to add it.</div>';

    $('side-suggestions').hidden = !considering;
    $('search-identity-label').hidden = !considering;
}

function renderDeckCharts(entries) {
    if (!entries.length) {
        $('deck-charts').innerHTML = '';
        return;
    }
    // Mana curve: spells by mana value (lands have none)
    const curve = Array(8).fill(0);
    const pips = {W: 0, U: 0, B: 0, R: 0, G: 0};
    const types = new Map();
    entries.forEach(entry => {
        const group = typeGroup(entry);
        types.set(group, (types.get(group) || 0) + entry.quantity);
        if (group !== 'Lands') curve[Math.min(7, Math.floor(entry.card.cmc || 0))] += entry.quantity;
        ((entry.card.mana_cost || '').match(/\{[^}]+\}/g) || []).forEach(symbol =>
            Object.keys(pips).forEach(color => { if (symbol.includes(color)) pips[color] += entry.quantity; }));
    });
    const top = Math.max(...curve, 1);
    const colorNames = Object.fromEntries(COLORS);
    $('deck-charts').innerHTML = `
        <div><div class="chart-title">Mana curve</div>
            <div class="curve">${curve.map((count, cost) => `
                <div class="curve-col" title="Mana value ${cost}${cost === 7 ? ' or more' : ''}: ${plural(count, 'card')}">
                    <span>${count || ''}</span><div class="curve-bar" style="height: ${(72 * count / top).toFixed(0)}px"></div><span>${cost}${cost === 7 ? '+' : ''}</span>
                </div>`).join('')}</div></div>
        <div><div class="chart-title">Types</div>${barsHtml([...types.entries()], number => String(number), 9)}</div>
        <div><div class="chart-title">Mana symbols</div>${barsHtml(Object.entries(pips).map(([color, count]) => [colorNames[color], count]), number => String(number), 5)}</div>`;
}

// -- Printing of a deck card ------------------------------------------------------

let printingEntry = null;   // {name, board} of the entry whose printing is being chosen

async function openPrintings(name, board) {
    const entry = deck.cards.find(card => card.name === name && card.board === board);
    if (!entry) return;
    printingEntry = {name, board};
    $('printing-title').textContent = name;
    $('printing-grid').innerHTML = '<div class="hint">Loading the printings...</div>';
    $('printing-modal').classList.add('show');
    const data = await api(`/api/cards/printings?name=${encodeURIComponent(name)}`);
    if (!data) return closeModal('printing-modal');
    // The printings you own first, then the newest
    const printings = [...data.printings].sort((a, b) => (b.owned > 0) - (a.owned > 0));
    $('printing-grid').innerHTML = printings.map(printing => `
        <button class="printing ${printing.id === entry.printing.id ? 'is-current' : ''}" data-id="${escapeHtml(printing.id)}">
            ${printing.image_uri ? `<img src="${escapeHtml(printing.image_uri)}" alt="" loading="lazy">` : '<div class="no-image"></div>'}
            <span class="printing-set">${escapeHtml(printing.set)}</span>
            <span class="printing-meta">
                <span>${escapeHtml((printing.set_code || '').toUpperCase())} #${escapeHtml(printing.number)}</span>
                <span>${printing.price ? money(printing.price) : printing.price_foil ? money(printing.price_foil) + ' foil' : ''}</span>
                ${printing.owned ? `<span class="own have">✓ ${printing.owned} owned</span>` : ''}
            </span>
            ${printing.treatments.length ? `<span class="printing-meta">${escapeHtml(printing.treatments.join(', '))}</span>` : ''}
        </button>`).join('') || '<div class="hint">No printings found.</div>';
}

async function changeDeckCards(cards) {
    const data = await api(`/api/decks/${deck.id}/cards`, {method: 'POST', body: {cards}});
    if (!data) return;
    updateDeck(data.deck);
}

async function saveDeckInfo(body) {
    const data = await api(`/api/decks/${deck.id}`, {method: 'PUT', body});
    if (!data) return;
    updateDeck(data.deck);
}

// -- Left side: search, suggestions, popular decks -----------------------------

function showSide(panel) {
    sidePanel = panel;
    markSwitch('side-switch', 'side', panel);
    $('side-search').hidden = panel !== 'search';
    $('side-suggestions-panel').hidden = panel !== 'suggestions';
    $('side-popular-panel').hidden = panel !== 'popular';
    refreshSide(true);
}

function refreshSide(opened = false) {
    // After the deck changed: what is shown beside it depends on its cards
    if (sidePanel === 'search') { if (opened) runSearch(); else markResults(); }
    if (sidePanel === 'suggestions') loadSuggestions();
    if (sidePanel === 'popular' && opened) loadPopular();
}

function inDeck(name) {
    return totalQuantity(deck.cards.filter(entry => entry.name === name));
}

function addLabelHtml(count) {
    // On a result's add button: the copies already in the deck
    return count ? `${count} +` : '<svg class="icon"><use href="#i-plus"/></svg>';
}

function canCommand(card) {
    return !!card && hasCommander(deck.format) && isCommanderCard(card);
}

function resultRowHtml(card, numbers = '') {
    return `
        <div class="result-row" data-name="${escapeHtml(card.name)}" data-id="${escapeHtml(card.id)}" data-image="${escapeHtml(card.image_uri || '')}" title="${escapeHtml(card.oracle_text)}">
            <span class="result-main">
                <div><span class="result-name">${escapeHtml(card.name)}</span> ${manaHtml(card.mana_cost)}</div>
                <div class="result-sub">${escapeHtml(card.type_line)}</div>
            </span>
            <span class="result-numbers">${numbers}${card.owned ? `<span class="own have">✓ ${card.owned} owned</span>` : ''}<div>${money(card.price)}</div></span>
            <span class="result-actions">
                ${canCommand(card) ? '<button class="mini-btn" data-add="commander" title="Make it the commander">Cmdr</button>' : ''}
                <button class="mini-btn" data-add="main" title="Add to the deck">${addLabelHtml(inDeck(card.name))}</button>
            </span>
        </div>`;
}

async function runSearch(more = false) {
    if (!deck) return;
    searchOffset = more ? searchOffset : 0;
    const params = new URLSearchParams({offset: searchOffset});
    const add = (key, value) => { if (value) params.set(key, value); };
    add('q', $('search-text').value.trim());
    add('type', $('search-type').value.trim());
    add('text', $('search-oracle').value.trim());
    add('cmc', $('search-cmc').value);
    add('rarity', $('search-rarity').value);
    add('colors', [...searchColors].join(''));
    add('owned', $('search-owned').checked ? '1' : '');
    if ($('search-free').checked) {
        params.set('free', '1');
        params.set('deck_id', deck.id);
    }
    add('format', $('search-legal').checked ? deck.format : '');
    if (hasCommander(deck.format) && $('search-identity').checked && commanders().some(entry => entry.card)) {
        params.set('identity', [...new Set(commanders().flatMap(entry => (entry.card && entry.card.identity) || []))].join(''));
    }
    const data = await api(`/api/cards/search?${params}`);
    if (!data || !deck) return;
    const html = data.cards.map(card => resultRowHtml(card)).join('');
    const list = $('search-results');
    if (more) list.insertAdjacentHTML('beforeend', html);
    else list.innerHTML = html || '<div class="empty-state">No cards match.</div>';
    searchOffset += data.cards.length;
    $('search-more').hidden = !data.more;
}

function markResults() {
    // The counts on the add buttons follow the deck
    document.querySelectorAll('#search-results .result-row').forEach(row => {
        row.querySelector('[data-add="main"]').innerHTML = addLabelHtml(inDeck(row.dataset.name));
    });
}

async function loadSuggestions() {
    const key = `${deck.id}:${commanderNames().join('|')}`;
    if (!suggestions || suggestions.key !== key) {
        $('suggestions-list').innerHTML = '<div class="hint">Asking EDHREC...</div>';
        $('suggestions-source').textContent = '';
        const data = await api(`/api/decks/${deck.id}/suggestions`);
        if (!deck) return;
        suggestions = {key, data: data || {categories: [], message: 'Suggestions are not available right now'}};
    }
    const data = suggestions.data;
    const ownedOnly = $('suggestions-owned').checked;
    const freeOnly = $('suggestions-free').checked;
    $('suggestions-source').innerHTML = data.decks
        ? `Played with this commander in ${data.decks.toLocaleString()} decks - <a href="${escapeHtml(data.url)}" target="_blank" rel="noopener">EDHREC</a>` : '';
    const html = data.categories.map(category => {
        const cards = category.cards.filter(card => !inDeck(card.name) && (!ownedOnly || card.owned) && (!freeOnly || !card.elsewhere));
        return cards.length ? `<div class="category-title">${escapeHtml(category.title)}</div>` + cards.map(card => resultRowHtml(card,
            `<span title="In ${card.inclusion}% of this commander's decks; synergy ${card.synergy > 0 ? '+' : ''}${card.synergy}%">${card.inclusion}% </span>`)).join('') : '';
    }).join('');
    const average = !deck.totals.cards || deck.totals.cards <= 2
        ? '<button class="btn btn-small btn-block" id="suggestions-average">Start from EDHREC\'s average deck</button>' : '';
    $('suggestions-list').innerHTML = (data.categories.length ? average : '') + (html
        || `<div class="empty-state">${escapeHtml(data.message || (ownedOnly ? 'You own none of the suggested cards that are not in the deck already.' : 'No suggestions left.'))}</div>`);
}

async function startFromAverage() {
    const data = await api(`/api/decks/${deck.id}/import`, {method: 'POST',
        body: {average: commanderNames(), replace: true}});
    if (!data) return;
    reportUnknown(data.unknown);
    notify("Filled with EDHREC's average deck", 'success');
    updateDeck(data.deck);
}

async function loadPopular() {
    $('popular-list').innerHTML = '<div class="hint">Looking...</div>';
    const data = await api(`/api/decks/popular?deck_id=${deck.id}`);
    if (!data || !deck) return;
    $('popular-source').textContent = data.commander
        ? `Most viewed public decks with ${data.commander} (Archidekt)`
        : `Most viewed public ${formatLabel(deck.format)} decks (Archidekt, Moxfield)`;
    $('popular-list').innerHTML = data.problems.map(problem => `<div class="callout warning">${escapeHtml(problem)}</div>`).join('')
        + (data.decks.map(item => `
            <div class="result-row">
                <span class="result-main">
                    <div class="result-name">${escapeHtml(item.name)}</div>
                    <div class="result-sub">${escapeHtml(item.source)}${item.author ? ' · ' + escapeHtml(item.author) : ''} · ${item.views.toLocaleString()} views</div>
                </span>
                <span class="result-actions">
                    <a class="mini-btn" href="${escapeHtml(item.url)}" target="_blank" rel="noopener" title="Open on ${escapeHtml(item.source)}"><svg class="icon"><use href="#i-link"/></svg></a>
                    <button class="mini-btn" data-import-url="${escapeHtml(item.url)}" title="Open a copy as a new deck here">Copy</button>
                </span>
            </div>`).join('') || (data.problems.length ? '' : '<div class="empty-state">No decks found.</div>'));
}

// -- Card image beside the hovered row -----------------------------------------

let previewRow = null;   // the row whose card is shown large

function showPreview(row) {
    const preview = $('card-preview');
    const image = row && row.dataset.image;
    if (!image) {
        preview.hidden = true;
        previewRow = null;
        return;
    }
    if (preview.dataset.image !== image) {
        // Without its old picture: a browser keeps showing that until the new one has loaded
        // (the box itself is there at once, card-shaped)
        preview.removeAttribute('src');
        preview.src = image;
        preview.dataset.image = image;
    }
    preview.hidden = false;
    previewRow = row;
    placePreview();
}

function placePreview() {
    // Beside what the pointer is on (a deck row, the small picture of an inventory row), on
    // the side that has room
    if (!previewRow) return;
    const preview = $('card-preview');
    const height = 240 * 88 / 63;
    const rect = previewRow.getBoundingClientRect();
    const left = rect.right + 250 < window.innerWidth ? rect.right + 8 : rect.left - 248;
    preview.style.left = `${Math.max(8, left)}px`;
    preview.style.top = `${Math.max(8, Math.min(rect.top - 40, window.innerHeight - height - 8))}px`;
}

// ============================================================================
// Settings drawer: backups of the collection, the scanned cards and the decks
// ============================================================================

function openSettings() {
    $('settings-drawer').classList.add('show');
    loadBackups();
}

function closeSettings() {
    $('settings-drawer').classList.remove('show');
    if (window.location.hash === '#settings') history.replaceState(null, '', window.location.pathname);
}

async function loadBackups() {
    const data = await api('/api/backups');
    if (!data) return;
    renderBackups(data.backups);
    $('backup-every').value = String(data.schedule.every_hours);
    $('backup-keep').value = data.schedule.keep;
    $('backup-keep').disabled = !data.schedule.every_hours;
}

async function saveBackupSchedule(change) {
    // How often automatic backups are made, how many are kept (the server answers with what it took)
    const data = await api('/api/backups/schedule', {method: 'POST', body: change});
    if (!data) return loadBackups();
    $('backup-every').value = String(data.schedule.every_hours);
    $('backup-keep').value = data.schedule.keep;
    $('backup-keep').disabled = !data.schedule.every_hours;
    notify(data.schedule.every_hours ? 'Automatic backups saved' : 'Automatic backups switched off', 'success');
}

function renderBackups(backups) {
    $('backup-list').innerHTML = backups.map(backup => `
        <div class="backup-row" data-backup="${escapeHtml(backup.id)}" data-created="${escapeHtml(backup.created.slice(0, 16))}">
            <div>
                <div class="idea-name">${escapeHtml(backup.created.slice(0, 16))}${backup.automatic || backup.daily ? ' <span class="backup-auto">automatic</span>' : backup.uploaded ? ' <span class="backup-auto">uploaded</span>' : ''}</div>
                ${backup.note ? `<div class="idea-meta">${escapeHtml(backup.note)}</div>` : ''}
                <div class="idea-meta">${plural(backup.cards, 'card')} in the collection · ${backup.scanned} scanned · ${plural(backup.decks, 'deck')}</div>
            </div>
            <button class="btn btn-small" data-backup-action="restore">Restore</button>
            <a class="mini-btn" href="/api/backups/${encodeURIComponent(backup.id)}/download" download title="Download this backup as a zip file, to keep a copy elsewhere"><svg class="icon"><use href="#i-download"/></svg></a>
            <button class="mini-btn" data-backup-action="delete" title="Delete this backup"><svg class="icon"><use href="#i-trash"/></svg></button>
        </div>`).join('') || '<div class="hint">No backups yet.</div>';
}

async function createBackup() {
    const button = $('backup-create');
    button.disabled = true;
    const data = await api('/api/backups', {method: 'POST', body: {note: $('backup-note').value}});
    button.disabled = false;
    if (!data) return;
    $('backup-note').value = '';
    renderBackups(data.backups);
    notify(`Backup made: ${plural(data.backup.cards, 'card')}, ${data.backup.scanned} scanned, ${plural(data.backup.decks, 'deck')}`, 'success');
}

async function uploadBackup(file) {
    // A backup downloaded earlier joins the list; restoring it is a separate step
    const button = $('backup-upload');
    button.disabled = true;
    button.textContent = 'Uploading...';
    const form = new FormData();
    form.append('file', file);
    const data = await api('/api/backups/upload', {method: 'POST', body: form});
    button.disabled = false;
    button.textContent = 'Upload a backup file';
    if (!data) return;
    renderBackups(data.backups);
    notify(`Backup of ${data.backup.created.slice(0, 16)} added to the list: ${plural(data.backup.cards, 'card')}, ${plural(data.backup.decks, 'deck')}`, 'success');
}

async function backupAction(row, action) {
    const id = row.dataset.backup, created = row.dataset.created;
    if (action === 'restore') {
        const ok = await confirmDialog({title: `Restore the backup of ${created}?`, confirmText: 'Restore', danger: true,
            message: 'The collection, the scanned cards and the decks go back to how they were then - everything '
                + 'changed since is replaced. What you have now is backed up first, so you can go back to it.'});
        if (!ok) return;
        const data = await api(`/api/backups/${id}/restore`, {method: 'POST'});
        if (!data) return loadBackups();  // a backup of the state before may have been made
        renderBackups(data.backups);
        notify(`Backup of ${created} restored`, 'success');
        selected.clear();
        loadInventory();
        loadScanned();
        if (deck) closeDeck();  // the open deck may be gone or different
        else loadDecks();
    } else {
        const ok = await confirmDialog({title: `Delete the backup of ${created}?`, confirmText: 'Delete', danger: true,
            message: "Your cards are not changed. This can't be undone."});
        if (!ok) return;
        const data = await api(`/api/backups/${id}`, {method: 'DELETE'});
        if (data) renderBackups(data.backups);
    }
}

// ============================================================================
// Trades: cards set aside until a trade is confirmed
// ============================================================================

let trades = [];   // open ones first, then the confirmed ones (GET /api/trades)

async function loadTrades() {
    const data = await api('/api/trades');
    if (!data) return;
    trades = data.trades;
    renderTrades();
}

function tradeCardHtml(card, open) {
    const where = [`${(card.set_code || card.set_name).toUpperCase()} ${card.number ? '#' + card.number : ''}`.trim(),
                   card.finish !== defaultFinish() ? finishLabel(card.finish) : '', card.condition, card.location].filter(Boolean);
    return `
        <div class="trade-row" data-row="${card.row}">
            <span class="trade-qty"><strong>${card.quantity}×</strong>${open && card.entry_quantity > card.quantity ? ` <span class="hint">of ${card.entry_quantity}</span>` : ''}</span>
            <span class="trade-card"><span class="result-name">${escapeHtml(card.name)}</span> <span class="result-sub">${escapeHtml(where.join(' · '))}</span></span>
            <span class="summary">${money(card.price * card.quantity)}</span>
            ${open ? `<span class="trade-steps">
                <button class="mini-btn" data-step="-1" title="One copy less" ${card.quantity < 2 ? 'disabled' : ''}>−</button>
                <button class="mini-btn" data-step="1" title="One copy more" ${card.quantity >= card.most ? 'disabled' : ''}>+</button>
                <button class="mini-btn" data-step="0" title="Take it out of the trade - it stays in your collection">×</button>
            </span>` : ''}
        </div>`;
}

function tradeHtml(trade) {
    const open = trade.status === 'open';
    const summary = `${plural(trade.quantity, 'card')} · <strong>${money(trade.value)}</strong> · `
        + (open ? `since ${escapeHtml(trade.created_at.slice(0, 10))}` : `confirmed ${escapeHtml((trade.closed_at || '').slice(0, 10))}`);
    const exports = `<select class="select trade-export" title="Download this trade's cards as a file for another site or app">
        ${optionsHtml([['', 'Export…'], ...gameInfo.exports])}</select>`;
    const cards = trade.cards.map(card => tradeCardHtml(card, open)).join('')
        || '<div class="hint">No cards in this trade.</div>';
    if (!open) {
        return `
            <details class="panel trade" data-trade="${trade.id}">
                <summary><span class="trade-name">${escapeHtml(trade.name)}</span> <span class="summary">${summary}</span></summary>
                <div class="trade-cards">${cards}</div>
                <div class="filter-row trade-actions">
                    <span class="spacer"></span>
                    ${exports}
                    <button class="btn btn-small btn-ghost" data-trade-action="forget" title="Take this trade out of the list - the cards are long gone">Delete from history</button>
                </div>
            </details>`;
    }
    return `
        <div class="panel trade" data-trade="${trade.id}">
            <div class="filter-row">
                <span class="trade-name">${escapeHtml(trade.name)}</span>
                <span class="summary">${summary}</span>
                <span class="spacer"></span>
                ${exports}
                <button class="btn btn-small" data-trade-action="rename">Rename</button>
                <button class="btn btn-small" data-trade-action="cancel" title="The trade is off: its cards simply stay in your collection">Cancel trade</button>
                <button class="btn btn-small btn-primary" data-trade-action="confirm" title="The trade happened: remove its cards from your collection" ${trade.cards.length ? '' : 'disabled'}>Confirm trade</button>
            </div>
            <div class="trade-cards">${cards}</div>
        </div>`;
}

function renderTrades() {
    const open = trades.filter(trade => trade.status === 'open'), done = trades.filter(trade => trade.status !== 'open');
    // Confirmed trades that were unfolded stay so when the list is drawn again
    const unfolded = new Set([...document.querySelectorAll('#trades-done details[open]')].map(item => item.dataset.trade));
    $('trades-open').innerHTML = open.map(tradeHtml).join('')
        || '<div class="empty-state">No trade is open.</div>';
    $('trades-done').innerHTML = done.map(tradeHtml).join('');
    $('trades-done-title').hidden = !done.length;
    document.querySelectorAll('#trades-done details').forEach(item => { item.open = unfolded.has(item.dataset.trade); });
}

async function setAsideForTrade(card = null) {
    // Every free copy of the selected entries - or of one row's, from its own button; how many
    // of each is changed on the Trades tab
    const ids = card ? [card.id] : [...selected];
    const open = trades.filter(trade => trade.status === 'open').map(trade => trade.name);
    const name = await valueDialog({title: `Set ${card ? card.name : entriesText(ids.length)} aside for a trade`,
        label: open.length ? 'Trade (one of the open ones, or a new name)' : 'Name of the trade (who it is with)',
        options: open, value: open.length === 1 ? open[0] : ''});
    if (!name) return;
    const data = await api('/api/trades', {method: 'POST', body: {name, ids}});
    if (!data) return;
    notify(`${plural(data.cards, 'card')} set aside for "${data.name}"`
        + (data.skipped ? ` - ${entriesText(data.skipped)} had no copy left to give` : ''), data.cards ? 'success' : 'warning');
    if (!card) selected.clear();  // a row's button leaves what is ticked alone
    loadInventory();
    loadTrades();
}

async function tradeAction(trade, action) {
    let request = null;
    if (action === 'confirm') {
        const ok = await confirmDialog({title: `Confirm the trade "${trade.name}"?`, confirmText: 'Confirm trade', danger: true,
            message: `Its ${plural(trade.quantity, 'card')} (${money(trade.value)}) are removed from your collection. This can't be undone.`});
        if (ok) request = api(`/api/trades/${trade.id}/confirm`, {method: 'POST'});
    } else if (action === 'cancel') {
        // Not confirmDialog: its "Cancel" beside "Cancel trade" says nothing
        const ok = await choiceDialog({title: `Cancel the trade "${trade.name}"?`,
            message: `Its ${plural(trade.quantity, 'card')} stay in your collection and are free again.`,
            choices: [{label: 'Keep the trade', value: false}, {label: 'Cancel the trade', value: true, style: 'danger'}]});
        if (ok === true) request = api(`/api/trades/${trade.id}`, {method: 'DELETE'});
    } else if (action === 'forget') {
        const ok = await confirmDialog({title: `Delete "${trade.name}" from the history?`, confirmText: 'Delete', danger: true,
            message: 'Only the record of the trade goes; your collection is not changed.'});
        if (ok) request = api(`/api/trades/${trade.id}`, {method: 'DELETE'});
    } else if (action === 'rename') {
        const name = await valueDialog({title: 'Rename the trade', label: 'Name', value: trade.name});
        if (name) request = api(`/api/trades/${trade.id}`, {method: 'PUT', body: {name}});
    }
    if (!request) return;
    const data = await request;
    if (data && action === 'confirm') notify(`${plural(data.cards, 'card')} left your collection`, 'success');
    loadTrades();
    loadInventory();
}

function bindTradeEvents() {
    $('tab-trades').addEventListener('click', async event => {
        const panel = event.target.closest('[data-trade]');
        const trade = panel && trades.find(item => item.id === parseInt(panel.dataset.trade));
        if (!trade) return;
        const action = event.target.closest('[data-trade-action]');
        if (action) return tradeAction(trade, action.dataset.tradeAction);
        const step = event.target.closest('[data-step]');
        const row = event.target.closest('[data-row]');
        const card = row && trade.cards.find(item => item.row === parseInt(row.dataset.row));
        if (!step || !card) return;
        const by = parseInt(step.dataset.step);
        await api(`/api/trades/${trade.id}/cards/${card.row}`, {method: 'PUT', body: {quantity: by ? card.quantity + by : 0}});
        loadTrades();
        loadInventory();
    });
    $('tab-trades').addEventListener('change', event => {
        // The server sends the file as a download; the menu goes back to "Export…"
        const select = event.target.closest('.trade-export');
        if (!select || !select.value) return;
        const link = document.createElement('a');
        link.href = `/api/trades/${select.closest('[data-trade]').dataset.trade}/export/${encodeURIComponent(select.value)}`;
        link.download = '';
        document.body.appendChild(link);
        link.click();
        link.remove();
        select.value = '';
    });
}

// ============================================================================
// Events
// ============================================================================

function debounce(callback, delay = 250) {
    let timer = null;
    return () => {
        clearTimeout(timer);
        timer = setTimeout(callback, delay);
    };
}

function bindSwitch(id, chosen) {
    // A row of buttons of which one is active (markSwitch shows which)
    $(id).addEventListener('click', event => {
        const button = event.target.closest('button');
        if (button) chosen(button);
    });
}

function bindColorChips(id, colors, changed) {
    $(id).addEventListener('click', event => {
        const chip = event.target.closest('.color-chip');
        if (!chip) return;
        if (colors.has(chip.dataset.color)) colors.delete(chip.dataset.color); else colors.add(chip.dataset.color);
        chip.classList.toggle('is-active');
        changed();
    });
}

function bindInventoryEvents() {
    // Filters
    ['filter-type', 'filter-rarity', 'filter-set', 'filter-finish', 'filter-location', 'filter-tag', 'filter-added',
     'filter-free', 'filter-text-not'].forEach(id => $(id).addEventListener('change', filtersChanged));
    $('filter-spare').addEventListener('change', spareChanged);
    ['filter-text', 'filter-price-min', 'filter-price-max'].forEach(id => $(id).addEventListener('input', debounce(filtersChanged, 150)));
    bindColorChips('filter-colors', filterColors, filtersChanged);
    $('filter-clear').addEventListener('click', () => {
        ['filter-text', 'filter-type', 'filter-rarity', 'filter-set', 'filter-finish', 'filter-location', 'filter-tag',
         'filter-added', 'filter-price-min', 'filter-price-max'].forEach(id => { $(id).value = ''; });
        $('filter-free').checked = false;
        $('filter-text-not').checked = false;
        $('filter-spare').checked = false;
        filterColors.clear();
        fillFilterOptions();
        filtersChanged();
    });
    $('inventory-sort').addEventListener('change', event => {
        remember('collectionSort', event.target.value);
        page = 0;
        applyFilters();
    });
    $('inventory-page-size').innerHTML = optionsHtml(PAGE_SIZES.map(size => [size, `${size} per page`]));
    $('inventory-page-size').addEventListener('change', event => {
        // The card at the top of the page stays on the page
        const first = page * pageSize;
        pageSize = parseInt(event.target.value);
        remember('collectionPageSize', pageSize);
        page = Math.floor(first / pageSize);
        renderInventory();
    });
    $('page-first').addEventListener('click', () => showPage(0));
    $('page-prev').addEventListener('click', () => showPage(page - 1));
    $('page-next').addEventListener('click', () => showPage(page + 1));
    $('page-last').addEventListener('click', () => showPage(Infinity));
    bindSwitch('view-switch', button => {
        inventoryView = button.dataset.view;
        remember('collectionView', inventoryView);
        renderInventory();
    });
    $('import-file-input').addEventListener('change', importInventory);
    $('export-select').addEventListener('change', exportInventory);
    $('scanned-add').addEventListener('click', addScanned);
    $('batch-remove').addEventListener('click', removeBatch);

    // Inventory rows
    $('inventory-list').addEventListener('click', event => {
        const badge = event.target.closest('.inventory-badge[data-filter]');
        if (badge) return filterByBadge(badge.dataset.filter, badge.dataset.value);
        const row = event.target.closest('[data-id]');
        const card = rowCard(row);
        if (!card) return;
        if (event.target.closest('.btn-trade')) return setAsideForTrade(card);
        if (event.target.closest('.btn-edit')) return editCard(card, knownLocations(), () => shown);
        // No question first: it can be undone
        if (event.target.closest('.btn-delete')) return changeLater([card.id], `Deleting ${card.name}`, {ids: [card.id], action: 'delete', value: ''});
        if (event.target.closest('.inventory-thumb') && card.captures.length) return openCaptures(card);
        if (event.target.classList.contains('row-check')) return pick(card.id, event.target.checked, event.shiftKey);
        // Grid: a click on the card selects it, a double click edits it
        if (row.classList.contains('grid-card')) pick(card.id, !selected.has(card.id), event.shiftKey);
    });
    $('inventory-list').addEventListener('mousedown', event => {
        // A shift-click selects cards, not the text between them
        if (event.shiftKey) event.preventDefault();
    });
    $('inventory-list').addEventListener('dblclick', event => {
        const row = event.target.closest('.grid-card');
        const card = rowCard(row);
        if (card) {
            toggleSelected(card.id, false);
            editCard(card, knownLocations(), () => shown);
        }
    });
    $('filter-chips').addEventListener('click', event => {
        const chip = event.target.closest('[data-chip]');
        if (!chip) return;
        activeFilters()[parseInt(chip.dataset.chip)].clear();
        filtersChanged();
    });
    $('undo-button').addEventListener('click', undoPending);
    // Leaving the page is not an undo: what waits is sent on the way out
    window.addEventListener('pagehide', () => { sendPending(); });
    document.addEventListener('keydown', inventoryKey);
    $('bulk-bar').addEventListener('click', event => {
        const button = event.target.closest('[data-bulk]');
        if (button) bulkAction(button.dataset.bulk);
    });
    $('select-all').addEventListener('change', event => {
        // Every card the filters show (also the ones further down the list), or none of them
        shown.forEach(card => event.target.checked ? selected.add(card.id) : selected.delete(card.id));
        renderInventory();
    });
    $('bulk-all').addEventListener('click', () => {
        shown.forEach(card => selected.add(card.id));
        renderInventory();
    });
    $('bulk-none').addEventListener('click', () => {
        selected.clear();
        renderInventory();
    });
    $('value-ok').addEventListener('click', () =>
        closeValueDialog($('value-select').hidden ? $('value-input').value.trim() : $('value-select').value));
    $('value-input').addEventListener('keydown', event => { if (event.key === 'Enter') $('value-ok').click(); });
}

function bindDeckHomeEvents() {
    // Deck list
    $('deck-new').addEventListener('click', () => openDeckModal());
    $('deck-sort').value = recall('deckSort', 'changed');
    $('deck-sort').addEventListener('change', event => {
        remember('deckSort', event.target.value);
        renderDeckList();
    });
    $('deck-filter-format').addEventListener('change', renderDeckList);
    $('deck-filter-text').addEventListener('input', debounce(renderDeckList, 150));
    $('deck-modal-save').addEventListener('click', saveDeckModal);
    $('deck-list').addEventListener('click', async event => {
        const tile = event.target.closest('[data-deck]');
        if (!tile) return;
        const data = await api(`/api/decks/${tile.dataset.deck}`);
        if (data) openDeck(data.deck);
    });
    document.querySelectorAll('[data-ideas]').forEach(button => button.addEventListener('click', () =>
        button.dataset.running ? stopIdeas(button.dataset.ideas) : pollIdeas(button.dataset.ideas, true)));

    // Build around a card
    $('around-format').innerHTML = optionsHtml(gameInfo.deck_formats);
    $('around-format').addEventListener('change', () => {
        aroundCard = null;  // chosen for the other format
        renderAround();
        loadAroundCards();
    });
    ['around-search', 'around-type'].forEach(id => $(id).addEventListener('input', debounce(loadAroundCards)));
    $('around-free').addEventListener('change', loadAroundCards);
    $('around-commanders').addEventListener('change', loadAroundCards);
    $('around-cards').addEventListener('click', event => {
        const row = event.target.closest('.result-row');
        if (row) chooseAround(row);
    });
    $('around-progress').addEventListener('click', event => {
        if (event.target.id === 'around-find') findAround();
        if (event.target.id === 'around-stop') stopIdeas('card');
        if (event.target.id === 'around-commander') {
            createDeck({name: aroundCard.name, format: aroundCard.format, commander: aroundCard.name});
        }
    });
    $('around-decks').addEventListener('click', copyDeckClicked);
    $('ideas-commanders').addEventListener('click', event => {
        const button = event.target.closest('[data-start-commander]');
        if (button) createDeck({name: button.dataset.startCommander, format: 'commander', commander: button.dataset.startCommander});
    });
    $('precon-search').addEventListener('input', debounce(renderPrecons, 150));
    $('ideas-precons').addEventListener('click', event => {
        const button = event.target.closest('[data-precon]');
        const row = event.target.closest('[data-file]');
        const item = button && row && precons.find(precon => precon.file === row.dataset.file);
        if (!item) return;
        if (button.dataset.precon === 'own') ownPrecon(item);
        else viewPrecon(item);
    });
    $('precon-create').addEventListener('click', async () => {
        const item = viewedPrecon;
        // A second deck of the same name makes every card of the first count for two decks
        if (deckList.some(existing => existing.name.toLowerCase() === item.name.toLowerCase())
            && !await confirmDialog({title: `You already have a deck called ${item.name}`,
                                     message: 'Create another one with the same name?', confirmText: 'Create another'})) return;
        closeModal('precon-modal');
        createDeck({name: item.name, format: item.format, precon: item.file}, 'Deck created');
    });
    $('precon-own').addEventListener('click', () => {
        closeModal('precon-modal');
        ownPrecon(viewedPrecon);
    });
    $('deck-data-update').addEventListener('click', event => {
        event.target.disabled = true;
        $('deck-data-progress').textContent = 'Starting...';
        socket.emit('update_database');
    });
}

function bindBuilderEvents() {
    // Header
    $('deck-back').addEventListener('click', closeDeck);
    $('deck-name').addEventListener('change', event => saveDeckInfo({name: event.target.value}));
    $('deck-format').addEventListener('change', event => saveDeckInfo({format: event.target.value}));
    $('deck-import').addEventListener('click', () => openDeckModal({importInto: deck}));
    $('deck-export').addEventListener('click', () => { window.location = `/api/decks/${deck.id}/export/text`; });
    $('deck-buylist').addEventListener('click', () => {
        if (!deck.totals.missing) return notify('You own every card of this deck', 'success');
        window.location = `/api/decks/${deck.id}/export/buylist`;
    });
    $('deck-duplicate').addEventListener('click', async () => {
        const data = await api(`/api/decks/${deck.id}/duplicate`, {method: 'POST'});
        if (data) openDeck(data.deck);
    });
    $('deck-delete').addEventListener('click', async () => {
        const ok = await confirmDialog({title: `Delete "${deck.name}"?`, confirmText: 'Delete', danger: true,
            message: "The deck list is deleted. Your inventory is not touched."});
        if (ok && await api(`/api/decks/${deck.id}`, {method: 'DELETE'})) closeDeck();
    });

    // Deck cards
    $('deck-cards').addEventListener('click', event => {
        const row = event.target.closest('.deck-row');
        const button = event.target.closest('.mini-btn');
        if (!row || !button) return;
        const card = {name: row.dataset.name, board: row.dataset.board};
        if ('printing' in button.dataset) openPrintings(card.name, card.board);
        else if (button.dataset.change) changeDeckCards([{...card, change: parseInt(button.dataset.change)}]);
        else if (button.dataset.move) changeDeckCards([{...card, move_to: button.dataset.move}]);
    });

    $('printing-grid').addEventListener('click', event => {
        const button = event.target.closest('.printing');
        if (!button || !printingEntry) return;
        closeModal('printing-modal');
        changeDeckCards([{...printingEntry, printing: button.dataset.id}]);
    });

    // Search, suggestions, popular decks
    bindSwitch('side-switch', button => showSide(button.dataset.side));
    ['search-text', 'search-type', 'search-oracle'].forEach(id => $(id).addEventListener('input', debounce(() => runSearch())));
    ['search-cmc', 'search-rarity', 'search-owned', 'search-free', 'search-legal', 'search-identity']
        .forEach(id => $(id).addEventListener('change', () => runSearch()));
    $('search-colors').innerHTML = colorChipsHtml(searchColors);
    bindColorChips('search-colors', searchColors, () => runSearch());
    $('search-more').addEventListener('click', () => runSearch(true));
    const addFromRow = event => {
        const row = event.target.closest('.result-row[data-name]');
        if (!row || event.target.closest('a')) return;
        const button = event.target.closest('[data-add]');
        changeDeckCards([{name: row.dataset.name, card_id: row.dataset.id, board: button ? button.dataset.add : 'main', change: 1}]);
    };
    $('search-results').addEventListener('click', addFromRow);
    $('suggestions-list').addEventListener('click', event => {
        if (event.target.id === 'suggestions-average') return startFromAverage();
        addFromRow(event);
    });
    $('suggestions-owned').addEventListener('change', loadSuggestions);
    $('suggestions-free').addEventListener('change', loadSuggestions);
    $('popular-list').addEventListener('click', copyDeckClicked);
    $('popular-import').addEventListener('click', () => {
        const url = $('popular-url').value.trim();
        if (url) createDeck({url}, 'Deck copied');
    });
}

// Topmost first
const OVERLAYS = [
    ['dialog-modal', () => closeDialog(null)],
    ['value-modal', () => closeValueDialog(null)],
    ['capture-modal', closeCaptures],
    ['edit-card-modal', closeEditCard],
    ['printing-modal', () => closeModal('printing-modal')],
    ['precon-modal', () => closeModal('precon-modal')],
    ['settings-drawer', closeSettings],
    ['deck-modal', () => closeModal('deck-modal')],
];

function bindEvents() {
    $('tabs').addEventListener('click', event => {
        const tab = event.target.closest('.tab');
        if (tab) showTab(tab.dataset.tab);
    });
    bindInventoryEvents();
    bindSwitch('stats-measure', button => {
        statsMeasure = button.dataset.measure;
        renderStats();
    });
    bindDeckHomeEvents();
    bindBuilderEvents();
    bindTradeEvents();

    // Card image preview (devices with a mouse)
    if (window.matchMedia('(hover: hover)').matches) {
        document.addEventListener('mouseover', event => showPreview(event.target.closest('[data-image]')));
    }

    // Settings drawer (backups); /collection#settings opens it (the scanner's settings link here)
    $('settings-close').addEventListener('click', closeSettings);
    $('backup-create').addEventListener('click', createBackup);
    $('backup-upload').addEventListener('click', () => $('backup-upload-input').click());
    $('backup-upload-input').addEventListener('change', event => {
        if (event.target.files[0]) uploadBackup(event.target.files[0]);
        event.target.value = '';  // the same file can be chosen again
    });
    $('backup-every').addEventListener('change', event => saveBackupSchedule({every_hours: parseInt(event.target.value)}));
    $('backup-keep').addEventListener('change', event => saveBackupSchedule({keep: parseInt(event.target.value) || 1}));
    $('backup-note').addEventListener('keydown', event => { if (event.key === 'Enter') createBackup(); });
    $('backup-list').addEventListener('click', event => {
        const button = event.target.closest('[data-backup-action]');
        if (button) backupAction(button.closest('[data-backup]'), button.dataset.backupAction);
    });
    if (window.location.hash === '#settings') openSettings();

    // Overlays: Escape closes the topmost one open, a click outside the one clicked
    document.addEventListener('keydown', event => {
        if (event.key !== 'Escape') return;
        const overlay = OVERLAYS.find(([id]) => $(id).classList.contains('show'));
        if (overlay) overlay[1]();
    });
    window.addEventListener('click', event => {
        const overlay = OVERLAYS.find(([id]) => event.target === $(id));
        if (overlay) overlay[1]();
    });
}

// The inventory changes while scanning on another tab or device
// (collection_updated: a trade changed - only this page listens)
['inventory_updated', 'inventory_undone', 'inventory_prices_updated', 'collection_updated'].forEach(name =>
    socket.on(name, debounce(() => {
        loadInventory();
        if (currentTab === 'trades') loadTrades();
        if (deck) api(`/api/decks/${deck.id}`).then(data => { if (data && deck) { deck = data.deck; renderDeck(); } });
    }, 500)));
socket.on('game_changed', () => window.location.reload());
socket.on('database_update_progress', data => { $('deck-data-progress').textContent = data.message; });
socket.on('database_update_complete', () => {
    $('deck-data-progress').textContent = '';
    notify('Card database updated', 'success');
    loadInventory();
    loadDecks();
});
socket.on('database_update_error', data => {
    $('deck-data-progress').textContent = '';
    $('deck-data-update').disabled = false;
    notify(`Card database update failed: ${data.message}`, 'error');
});

document.addEventListener('DOMContentLoaded', async () => {
    const data = await api('/api/games');
    if (!data) return;
    gameInfo = data.games.find(game => game.id === data.active);
    $('game-label').textContent = data.games.length > 1 ? gameInfo.label : '';
    $('tab-button-decks').hidden = !gameInfo.deck_formats.length;
    renderExportMenu();
    bindEvents();
    await loadInventory();
    loadDecks();  // the location filter lists the decks' locations apart
    loadTrades();  // "Set aside for trade" offers the open ones
    const tab = recall('collectionTab', 'inventory');
    showTab(tab === 'decks' && !gameInfo.deck_formats.length ? 'inventory' : tab);
});
