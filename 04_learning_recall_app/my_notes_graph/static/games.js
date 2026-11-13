'use strict';
/* Recall — topic-personalised study games.
 *
 * Three games read the same question bank the quiz uses:
 *   Sudoku        digits 1-9 are mapped onto nine concepts from one topic
 *   Trap Hunt     a 60-second combo round of desk-free adversarial questions
 *   Concept Match flip cards pairing a concept with its explanation
 *
 * Everything is computed on the host, so this file only renders.  A finished
 * round is posted to /api/games/score; if the host is unreachable the round
 * waits in IndexedDB and uploads on the next connection, exactly like the
 * review queue in flow.js.
 *
 * Public surface (used by inline markup in index.html):
 *   games.activate()        load topics, bests and the hub
 *   games.start(name)       open a game ("sudoku" | "trap" | "match")
 *   games.refreshTopics()   re-read folders from the bank
 *   games.back()            return to the hub
 *   games.flushScores()     upload queued rounds (called on reconnect)
 */
const games = (() => {
  const DB_NAME = 'recall-offline';
  const DB_VERSION = 2;              // must match flow.js
  const BASE = { easy: 500, medium: 900, hard: 1400 };
  const DIFFICULTY = { easy: 'Easy', medium: 'Medium', hard: 'Hard' };
  const TRAP_SECONDS = 60;

  const G = {
    topic: '',
    topics: [],
    scores: {},
    sudoku: null,
    trap: null,
    match: null,
    timers: { sudoku: null, trap: null, match: null }
  };

  /* ---------- tiny helpers ---------- */
  function $(selector) { return document.querySelector(selector); }
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function shortId() {
    try { return crypto.randomUUID().slice(0, 12); }
    catch (_) { return Math.random().toString(36).slice(2, 14); }
  }
  function clock(seconds) {
    const whole = Math.max(0, Math.round(seconds));
    return Math.floor(whole / 60) + ':' + String(whole % 60).padStart(2, '0');
  }
  function toast(text) {
    const node = $('#toast');
    if (!node) return;
    node.textContent = text;
    node.classList.add('show');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => node.classList.remove('show'), 3400);
  }
  function notice(text, bad) {
    const node = $('#gamesnotice');
    if (!node) return;
    node.textContent = text || '';
    node.classList.toggle('bad', !!bad);
  }
  async function api(path, body) {
    const init = body === undefined
      ? { method: 'GET' }
      : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    const response = await fetch(path, init);
    let payload = null;
    try { payload = await response.json(); } catch (_) { /* non-JSON */ }
    if (!response.ok) {
      const detail = payload && (payload.detail || payload.error);
      throw new Error(typeof detail === 'string' ? detail : `HTTP ${response.status}`);
    }
    return payload || {};
  }

  /* ---------- IndexedDB (shared with flow.js) ---------- */
  function db() {
    return new Promise((resolve, reject) => {
      if (!('indexedDB' in window)) { reject(new Error('IndexedDB unavailable')); return; }
      const open = indexedDB.open(DB_NAME, DB_VERSION);
      open.onupgradeneeded = () => {
        const database = open.result;
        if (!database.objectStoreNames.contains('deck')) database.createObjectStore('deck', { keyPath: 'id' });
        if (!database.objectStoreNames.contains('reviews')) database.createObjectStore('reviews', { keyPath: 'client_id' });
        if (!database.objectStoreNames.contains('meta')) database.createObjectStore('meta', { keyPath: 'key' });
        if (!database.objectStoreNames.contains('game_scores')) database.createObjectStore('game_scores', { keyPath: 'client_id' });
      };
      open.onsuccess = () => resolve(open.result);
      open.onerror = () => reject(open.error);
    });
  }

  async function idbWrite(store, rows) {
    const database = await db();
    return new Promise((resolve, reject) => {
      const tx = database.transaction(store, 'readwrite');
      const target = tx.objectStore(store);
      rows.forEach(row => target.put(row));
      tx.oncomplete = () => { database.close(); resolve(rows.length); };
      tx.onerror = () => { database.close(); reject(tx.error); };
    });
  }

  async function idbRead(store) {
    const database = await db();
    return new Promise((resolve, reject) => {
      const tx = database.transaction(store, 'readonly');
      const all = tx.objectStore(store).getAll();
      all.onsuccess = () => { database.close(); resolve(all.result || []); };
      all.onerror = () => { database.close(); reject(all.error); };
    });
  }

  async function idbDelete(store, keys) {
    const database = await db();
    return new Promise((resolve, reject) => {
      const tx = database.transaction(store, 'readwrite');
      const target = tx.objectStore(store);
      keys.forEach(key => target.delete(key));
      tx.oncomplete = () => { database.close(); resolve(); };
      tx.onerror = () => { database.close(); reject(tx.error); };
    });
  }

  function local(key, value) {
    try {
      if (value === undefined) {
        const raw = localStorage.getItem('recall.' + key);
        return raw ? JSON.parse(raw) : null;
      }
      localStorage.setItem('recall.' + key, JSON.stringify(value));
    } catch (_) { /* private mode */ }
    return null;
  }

  /* ---------- topic + bests ---------- */
  function topicLabel(entry) {
    return entry.topic + (entry.questions ? ` (${entry.questions})` : '');
  }

  function renderTopics() {
    const select = $('#gametopic');
    if (!select) return;
    select.replaceChildren();
    const all = el('option', '', 'All topics');
    all.value = '';
    select.appendChild(all);
    G.topics.forEach(entry => {
      const option = el('option', '', topicLabel(entry));
      option.value = entry.topic;
      select.appendChild(option);
    });
    if (G.topic && !G.topics.some(entry => entry.topic === G.topic)) G.topic = '';
    select.value = G.topic;
  }

  async function loadTopics() {
    try {
      const payload = await api('/api/games/topics');
      G.topics = Array.isArray(payload.topics) ? payload.topics : [];
      local('gametopics', G.topics);
    } catch (_) {
      G.topics = local('gametopics') || [];
      if (G.topics.length) notice('Offline — using the topics cached on this device.');
    }
    renderTopics();
    renderBest();
  }

  async function loadScores() {
    try {
      const payload = await api(`/api/games/scores?topic=${encodeURIComponent(G.topic)}`);
      G.scores = payload.best || {};
      local('gamebest', { topic: G.topic, best: G.scores });
    } catch (_) {
      const cached = local('gamebest');
      G.scores = cached && cached.topic === G.topic ? (cached.best || {}) : {};
    }
    renderBest();
  }

  function renderBest() {
    const line = $('#gamebest');
    if (!line) return;
    const parts = ['sudoku', 'trap_hunt', 'match']
      .map(game => {
        const best = G.scores[game] && G.scores[game].best;
        return best ? `${gameName(game)} ${best}` : '';
      })
      .filter(Boolean);
    line.textContent = parts.length ? 'Best: ' + parts.join(' · ') : 'No rounds yet — play one to set a best.';
  }

  function gameName(name) {
    return name === 'trap_hunt' ? 'Trap Hunt' : name === 'match' ? 'Concept Match' : 'Sudoku';
  }

  /* ---------- hub ---------- */
  function renderHub() {
    clearTimers();
    if (G.trap && G.trap.done) G.trap = null;
    if (G.match && G.match.done) G.match = null;
    const hub = $('#gameshub');
    const out = $('#gameout');
    const pane = $('#p-games');
    if (pane) pane.classList.remove('game-active');
    if (hub) hub.hidden = false;
    if (out) { out.hidden = true; out.replaceChildren(); }
    if (!hub) return;

    const cards = [
      {
        game: 'sudoku', icon: '🧩', title: 'Topic Sudoku',
        text: 'Nine of your concepts become the digits 1–9. Place them without repeating a term in any row, column or box.',
        action: hasSavedSudoku() ? 'Resume board' : 'Play Sudoku'
      },
      {
        game: 'trap_hunt', icon: '🎯', title: 'Trap Hunt',
        text: 'Sixty seconds of adversarial traps that need no paper. Chain correct answers for a combo multiplier.',
        action: 'Start 60-second round'
      },
      {
        game: 'match', icon: '🃏', title: 'Concept Match',
        text: 'Flip cards to pair each concept with the explanation from your notes. Fewer moves and faster wins.',
        action: G.match && !G.match.done ? 'Resume match' : 'Play Concept Match'
      }
    ];

    hub.replaceChildren();
    cards.forEach(card => {
      const box = el('article', 'gamecard');
      const head = el('div', 'gamecardhead');
      head.appendChild(el('span', 'gameicon', card.icon));
      head.appendChild(el('h2', '', card.title));
      box.appendChild(head);
      box.appendChild(el('p', 'gamecardtext', card.text));
      const best = G.scores[card.game] && G.scores[card.game].best;
      box.appendChild(el('div', 'gamecardbest', best ? `Personal best ${best}` : 'No best yet'));
      const button = el('button', 'pri', card.action);
      button.type = 'button';
      button.addEventListener('click', () => {
        if (card.game === 'sudoku' && hasSavedSudoku()) resumeSudoku();
        else if (card.game === 'match' && G.match && !G.match.done) resumeMatch();
        else start(card.game);
      });
      box.appendChild(button);
      hub.appendChild(box);
    });

    const scope = el('p', 'hint');
    scope.textContent = G.topic
      ? `Boards are personalised with “${G.topic}”. Change the topic above to switch folders.`
      : 'Pick a topic above to map Sudoku digits onto that folder’s concepts.';
    hub.appendChild(scope);
  }

  function openStage() {
    const hub = $('#gameshub');
    const out = $('#gameout');
    const pane = $('#p-games');
    if (pane) pane.classList.add('game-active');
    if (hub) hub.hidden = true;
    if (out) { out.hidden = false; out.replaceChildren(); }
    return out;
  }

  function stageHeader(title, subtitle) {
    const header = el('div', 'gamestagehead');
    const backButton = el('button', 'ghost', '← Games');
    backButton.type = 'button';
    backButton.addEventListener('click', () => back());
    header.appendChild(backButton);
    const names = el('div', 'gamestagename');
    names.appendChild(el('h2', '', title));
    if (subtitle) names.appendChild(el('div', 'gamestagesub', subtitle));
    header.appendChild(names);
    return header;
  }

  /* =================================================================== sudoku */
  function savedSudoku() {
    const saved = local('sudokuprogress');
    if (!saved || saved.done || saved.topic !== G.topic || !saved.board
        || !Array.isArray(saved.board.puzzle) || saved.board.puzzle.length !== 9
        || !Array.isArray(saved.board.solution) || saved.board.solution.length !== 9
        || !Array.isArray(saved.board.legend) || saved.board.legend.length !== 9
        || !Array.isArray(saved.values) || saved.values.length !== 81) return null;
    return saved;
  }

  function hasSavedSudoku() {
    return !!(G.sudoku && !G.sudoku.done && G.sudoku.topic === G.topic)
      || !!savedSudoku();
  }

  function persistSudoku() {
    const state = G.sudoku;
    if (!state || state.done) return;
    local('sudokuprogress', {
      board: state.board,
      topic: state.topic,
      difficulty: state.difficulty,
      values: state.values,
      selected: state.selected,
      mistakes: state.mistakes,
      hints: state.hints,
      hintedCells: [...state.hintedCells],
      terms: state.terms,
      highlight: state.highlight,
      startedAt: state.startedAt,
      elapsed: state.elapsed
    });
  }

  function restoreSudoku(saved) {
    const values = saved.values.map(Number);
    G.sudoku = {
      board: saved.board,
      difficulty: saved.difficulty || saved.board.difficulty || 'medium',
      topic: saved.topic,
      values,
      given: saved.board.puzzle.flat().map(value => Number(value) > 0),
      selected: Number.isInteger(saved.selected) ? saved.selected : -1,
      mistakes: Math.max(0, Number(saved.mistakes) || 0),
      hints: Math.max(0, Number(saved.hints) || 0),
      hintedCells: new Set(Array.isArray(saved.hintedCells) ? saved.hintedCells : []),
      wrong: -1,
      terms: saved.terms === true,
      highlight: Number(saved.highlight) || 0,
      startedAt: Number(saved.startedAt) || Date.now(),
      elapsed: Math.max(0, Number(saved.elapsed) || 0),
      done: false,
      focus: false
    };
    renderSudoku();
    startSudokuTimer();
  }

  function startSudoku(difficulty, forceNew = false) {
    clearTimers();
    const saved = !forceNew && savedSudoku();
    if (saved && saved.difficulty === difficulty) {
      restoreSudoku(saved);
      notice('Resumed your saved Sudoku board.');
      return;
    }
    local('sudokuprogress', null);
    const query = new URLSearchParams({ topic: G.topic, difficulty });
    notice('Building a board from your notes…');
    api('/api/games/sudoku?' + query.toString()).then(board => {
      local('sudoku', board);
      mountSudoku(board, difficulty);
      notice('');
    }).catch(() => {
      const cached = local('sudoku');
      if (cached && cached.topic === G.topic && cached.puzzle && cached.legend) {
        mountSudoku(cached, cached.difficulty || difficulty);
        notice('Offline — replaying the last board generated on this device.');
      } else {
        notice('Sudoku needs the host once to build a board. Reconnect and try again.', true);
      }
    });
  }

  function resumeSudoku() {
    if (!G.sudoku || G.sudoku.topic !== G.topic || G.sudoku.done) {
      const saved = savedSudoku();
      if (saved) { restoreSudoku(saved); return; }
      startSudoku('medium', true);
      return;
    }
    renderSudoku();
    startSudokuTimer();
  }

  function mountSudoku(board, difficulty) {
    const values = board.puzzle.flat().map(Number);
    G.sudoku = {
      board, difficulty: board.difficulty || difficulty,
      topic: G.topic,
      values,
      given: values.map(value => value > 0),
      selected: -1,
      mistakes: 0, hints: 0,
      hintedCells: new Set(),
      wrong: -1,
      terms: local('sudokuterms') === true,
      highlight: 0,
      startedAt: Date.now(),
      elapsed: 0,
      done: false,
      focus: false
    };
    renderSudoku();
    startSudokuTimer();
  }

  function startSudokuTimer() {
    clearInterval(G.timers.sudoku);
    const state = G.sudoku;
    if (!state || state.done) return;
    const tick = () => {
      state.elapsed = Math.floor((Date.now() - state.startedAt) / 1000);
      const node = $('#sudokutimer');
      if (node) node.textContent = clock(state.elapsed);
    };
    tick();
    G.timers.sudoku = setInterval(tick, 1000);
  }

  function renderSudoku() {
    const state = G.sudoku;
    if (!state) return;
    const out = openStage();
    const board = state.board;
    out.appendChild(stageHeader('Topic Sudoku',
      `${DIFFICULTY[state.difficulty] || state.difficulty} · ${board.givens} givens · ${state.topic || 'all topics'}`));

    const bar = el('div', 'sudokutop');
    const diffLabel = el('label', 'sudokudiff');
    diffLabel.appendChild(el('span', '', 'Difficulty'));
    const select = el('select');
    Object.entries(DIFFICULTY).forEach(([value, label]) => {
      const option = el('option', '', label);
      option.value = value;
      if (value === state.difficulty) option.selected = true;
      select.appendChild(option);
    });
    select.addEventListener('change', () => startSudoku(select.value, true));
    diffLabel.appendChild(select);
    bar.appendChild(diffLabel);

    const terms = el('button', 'ghost', state.terms ? 'Show digits' : 'Show terms');
    terms.type = 'button';
    terms.addEventListener('click', () => {
      state.terms = !state.terms;
      local('sudokuterms', state.terms);
      terms.textContent = state.terms ? 'Show digits' : 'Show terms';
      paintSudoku();
    });
    bar.appendChild(terms);

    const fresh = el('button', 'ghost', 'New board');
    fresh.type = 'button';
    fresh.addEventListener('click', () => startSudoku(state.difficulty, true));
    bar.appendChild(fresh);

    const stats = el('span', 'sudokustats');
    stats.innerHTML = `<span id="sudokutimer">${clock(state.elapsed)}</span> · ` +
      `<span id="sudokumistakes">${state.mistakes}</span> mistakes · ` +
      `<span id="sudokuhints">${state.hints}</span> hints`;
    bar.appendChild(stats);

    const check = el('button', 'ghost', 'Check');
    check.type = 'button';
    check.addEventListener('click', checkSudoku);
    bar.appendChild(check);

    const hint = el('button', 'pri', 'Hint');
    hint.type = 'button';
    hint.addEventListener('click', hintSudoku);
    bar.appendChild(hint);
    out.appendChild(bar);

    const layout = el('div', 'sudokulayout');
    const grid = el('div', 'sudokugrid');
    grid.id = 'sudokugrid';
    grid.setAttribute('role', 'grid');
    layout.appendChild(grid);

    const panel = el('aside', 'sudokupanel');
    panel.appendChild(el('div', 'raillabel', 'Your topic legend'));
    const legend = el('ol', 'sudokulegend');
    legend.id = 'sudokulegend';
    panel.appendChild(legend);
    if (board.mixed && state.topic) {
      panel.appendChild(el('p', 'sudokumixnote',
        `Only ${board.from_topic} concept${board.from_topic === 1 ? '' : 's'} in “${state.topic}” — the remaining digits come from your other notes. Generate more questions to personalise all nine.`));
    }
    const hintline = el('p', 'sudokuhintline');
    hintline.id = 'sudokuhintline';
    panel.appendChild(hintline);
    panel.appendChild(el('p', 'hint', 'Tap a term to highlight every matching cell.'));
    layout.appendChild(panel);
    out.appendChild(layout);

    const pad = el('div', 'sudokupad');
    for (let digit = 1; digit <= 9; digit += 1) {
      const button = el('button', 'sudokupadbtn');
      button.type = 'button';
      button.dataset.digit = String(digit);
      button.addEventListener('click', () => sudokuInput(digit));
      pad.appendChild(button);
    }
    const erase = el('button', 'sudokupadbtn erase', '⌫');
    erase.type = 'button';
    erase.addEventListener('click', () => sudokuInput(0));
    pad.appendChild(erase);
    out.appendChild(pad);

    const result = el('div', 'sudokuresult');
    result.id = 'sudokuresult';
    out.appendChild(result);
    paintSudoku();
  }

  function valueCounts(values) {
    const counts = new Array(10).fill(0);
    values.forEach(value => { if (value) counts[value] += 1; });
    return counts;
  }

  function paintSudoku() {
    const state = G.sudoku;
    const grid = $('#sudokugrid');
    if (!state || !grid) return;
    const counts = valueCounts(state.values);
    const selected = state.selected;
    const selectedRow = selected >= 0 ? Math.floor(selected / 9) : -1;
    const selectedCol = selected >= 0 ? selected % 9 : -1;
    grid.replaceChildren();

    for (let index = 0; index < 81; index += 1) {
      const value = state.values[index];
      const row = Math.floor(index / 9);
      const col = index % 9;
      const cell = el('button', 'sudokucell');
      cell.type = 'button';
      cell.dataset.index = String(index);
      if (state.given[index]) cell.classList.add('given');
      if (index === selected) cell.classList.add('selected');
      if (selected >= 0 && index !== selected) {
        const sameBox = Math.floor(row / 3) === Math.floor(selectedRow / 3)
          && Math.floor(col / 3) === Math.floor(selectedCol / 3);
        if (row === selectedRow || col === selectedCol || sameBox) cell.classList.add('peer');
      }
      if (value && counts[value] > 1) cell.classList.add('conflict');
      if (value && value === state.highlight) cell.classList.add('hl');
      if (index === state.wrong) cell.classList.add('wrong');
      if (state.hintedCells.has(index)) cell.classList.add('hinted');
      if (col % 3 === 2 && col !== 8) cell.classList.add('boxright');
      if (row % 3 === 2 && row !== 8) cell.classList.add('boxbottom');
      if (state.terms) cell.classList.add('termcell');
      if (value) {
        const entry = state.board.legend[value - 1];
        cell.textContent = state.terms ? entry.short : String(value);
        cell.title = entry.term + (entry.hint ? ' — ' + entry.hint : '');
        cell.setAttribute('aria-label',
          `Row ${row + 1}, column ${col + 1}: ${entry.term}`);
      } else {
        cell.setAttribute('aria-label', `Row ${row + 1}, column ${col + 1}: empty`);
      }
      cell.addEventListener('click', () => {
        state.selected = index;
        state.focus = true;
        paintSudoku();
      });
      grid.appendChild(cell);
    }

    const mistakes = $('#sudokumistakes');
    if (mistakes) mistakes.textContent = String(state.mistakes);
    const hints = $('#sudokuhints');
    if (hints) hints.textContent = String(state.hints);

    const pad = $('.sudokupad');
    if (pad) {
      pad.querySelectorAll('.sudokupadbtn[data-digit]').forEach(button => {
        const digit = Number(button.dataset.digit);
        const entry = state.board.legend[digit - 1];
        button.innerHTML = state.terms
          ? `<span class="padnum">${digit}</span><span class="padshort">${esc(entry.short)}</span>`
          : `<span class="padnum">${digit}</span>`;
        button.setAttribute('aria-label', `Digit ${digit}: ${entry.term}`);
      });
    }

    paintLegend(state);
    if (state.focus && selected >= 0) {
      state.focus = false;
      const target = grid.querySelector(`[data-index="${selected}"]`);
      if (target) target.focus({ preventScroll: true });
    }
    persistSudoku();
  }

  function paintLegend(state) {
    const list = $('#sudokulegend');
    if (!list) return;
    list.replaceChildren();
    state.board.legend.forEach(entry => {
      const row = el('li', 'legendrow' + (state.highlight === entry.digit ? ' on' : ''));
      row.appendChild(el('span', 'legenddigit', String(entry.digit)));
      row.appendChild(el('span', 'legendshort', entry.short));
      row.appendChild(el('span', 'legendterm', entry.term));
      row.title = entry.hint || entry.term;
      row.tabIndex = 0;
      row.setAttribute('role', 'button');
      row.setAttribute('aria-pressed', state.highlight === entry.digit ? 'true' : 'false');
      const toggle = () => {
        state.highlight = state.highlight === entry.digit ? 0 : entry.digit;
        paintSudoku();
      };
      row.addEventListener('click', toggle);
      row.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggle(); }
      });
      list.appendChild(row);
    });
    const line = $('#sudokuhintline');
    if (line) {
      const entry = state.board.legend[(state.highlight || 0) - 1];
      line.textContent = entry ? (entry.hint || entry.term)
        : 'Tap a term to highlight it on the board.';
    }
  }

  function sudokuInput(digit) {
    const state = G.sudoku;
    if (!state || state.done) return;
    if (state.selected < 0) { notice('Pick a cell first.'); return; }
    if (state.given[state.selected]) { notice('That cell is a given — choose an empty one.'); return; }
    const index = state.selected;
    const expected = state.board.solution[Math.floor(index / 9)][index % 9];
    if (digit === 0) {
      state.values[index] = 0;
      state.focus = true;
      paintSudoku();
      return;
    }
    if (digit === expected) {
      state.values[index] = digit;
      state.wrong = -1;
      state.focus = true;
      paintSudoku();
      if (isSudokuComplete(state)) finishSudoku();
    } else {
      state.mistakes += 1;
      state.wrong = index;
      state.focus = true;
      paintSudoku();
      clearTimeout(sudokuInput._timer);
      sudokuInput._timer = setTimeout(() => {
        if (G.sudoku !== state || state.done) return;
        state.wrong = -1;
        paintSudoku();
      }, 750);
    }
  }

  function isSudokuComplete(state) {
    return state.values.every((value, index) => {
      const row = Math.floor(index / 9);
      return value === state.board.solution[row][index % 9];
    });
  }

  function checkSudoku() {
    const state = G.sudoku;
    if (!state || state.done) return;
    const empty = state.values.filter(value => !value).length;
    const counts = valueCounts(state.values);
    const conflicts = state.values.filter(value => value && counts[value] > 1).length;
    if (!empty && isSudokuComplete(state)) { finishSudoku(); return; }
    if (conflicts) notice(`${conflicts} cell(s) repeat a digit in their row, column or box.`, true);
    else if (empty) notice(`No conflicts — ${empty} cell(s) still empty.`);
    else notice('Every cell is filled but something is wrong. Try a hint.', true);
  }

  function hintSudoku() {
    const state = G.sudoku;
    if (!state || state.done) return;
    let index = state.selected;
    if (index < 0 || state.given[index] || state.values[index]) {
      index = state.values.findIndex(value => !value);
    }
    if (index < 0) { notice('The board is already full — press Check.'); return; }
    const row = Math.floor(index / 9);
    state.values[index] = state.board.solution[row][index % 9];
    state.hintedCells.add(index);
    state.hints += 1;
    state.selected = index;
    state.focus = true;
    paintSudoku();
    notice(`Hint ${state.hints} — ${state.board.legend[state.values[index] - 1].term} placed.`);
    if (isSudokuComplete(state)) finishSudoku();
  }

  function finishSudoku() {
    const state = G.sudoku;
    if (!state || state.done) return;
    state.done = true;
    local('sudokuprogress', null);
    clearInterval(G.timers.sudoku);
    state.elapsed = Math.floor((Date.now() - state.startedAt) / 1000);
    const placements = 81 - state.board.givens;
    const accuracy = Math.min(1, placements / Math.max(1, placements + state.mistakes));
    const score = Math.max(100,
      (BASE[state.difficulty] || BASE.medium) - state.mistakes * 30
      - state.hints * 60 - state.elapsed * 2);
    const box = $('#sudokuresult');
    if (box) {
      box.classList.add('show');
      box.replaceChildren();
      box.appendChild(el('h3', '', 'Board solved'));
      box.appendChild(el('p', '', `${clock(state.elapsed)} · ${state.mistakes} mistake(s) · ${state.hints} hint(s) · score ${score}`));
      const again = el('button', 'pri', 'New board');
      again.type = 'button';
      again.addEventListener('click', () => startSudoku(state.difficulty));
      box.appendChild(again);
    }
    notice('Solved! Score saved.');
    submit({
      game: 'sudoku', topic: state.topic, score, accuracy,
      duration: state.elapsed,
      detail: {
        difficulty: state.difficulty, givens: state.board.givens,
        hints: state.hints, mistakes: state.mistakes, seed: state.board.seed
      }
    });
  }

  /* ================================================================ trap hunt */
  function startTrap() {
    clearTimers();
    G.trap = null;
    notice('Loading traps…');
    api(`/api/games/trap_hunt?topic=${encodeURIComponent(G.topic)}&limit=12`)
      .then(payload => {
        if (!payload.count) {
          notice('No desk-free traps in this topic yet. Generate more questions or pick another topic.', true);
          renderTrapEmpty();
          return;
        }
        notice('');
        G.trap = {
          questions: payload.questions,
          mixed: !!payload.mixed,
          index: 0,
          score: 0,
          combo: 1,
          bestCombo: 1,
          correct: 0,
          answered: 0,
          endsAt: Date.now() + TRAP_SECONDS * 1000,
          locked: false,
          done: false,
          topic: G.topic
        };
        renderTrap();
        G.timers.trap = setInterval(tickTrap, 200);
      })
      .catch(() => {
        notice('Trap Hunt needs the host. Reconnect and try again.', true);
        renderTrapEmpty();
      });
  }

  function renderTrapEmpty() {
    const out = openStage();
    out.appendChild(stageHeader('Trap Hunt', '60 seconds · desk-free traps only'));
    out.appendChild(el('p', 'gameempty', 'Nothing to play yet. Generate more questions from Sync, then come back.'));
  }

  function renderTrap() {
    const state = G.trap;
    if (!state) return;
    const out = openStage();
    out.appendChild(stageHeader('Trap Hunt',
      `${TRAP_SECONDS} seconds · combo up to ×5${state.mixed ? ' · topped up from other topics' : ''}`));

    const bar = el('div', 'trapbar');
    bar.appendChild(el('span', 'trapclock', `${TRAP_SECONDS}s`));
    const track = el('div', 'trapbar-track');
    const fill = el('i');
    fill.id = 'trapfill';
    track.appendChild(fill);
    bar.appendChild(track);
    const combo = el('span', 'trapcombo', '×1');
    combo.id = 'trapcombo';
    bar.appendChild(combo);
    const score = el('span', 'trapscore', '0 pts');
    score.id = 'trapscore';
    bar.appendChild(score);
    out.appendChild(bar);

    const holder = el('div', 'trapcard');
    holder.id = 'trapcard';
    out.appendChild(holder);
    renderTrapQuestion();
  }

  function tickTrap() {
    const state = G.trap;
    if (!state || state.done) return;
    const remaining = Math.max(0, Math.ceil((state.endsAt - Date.now()) / 1000));
    const clockNode = $('.trapclock');
    if (clockNode) clockNode.textContent = `${remaining}s`;
    const fill = $('#trapfill');
    if (fill) fill.style.width = `${(remaining / TRAP_SECONDS) * 100}%`;
    if (remaining <= 0) finishTrap();
  }

  function renderTrapQuestion() {
    const state = G.trap;
    const holder = $('#trapcard');
    if (!state || !holder) return;
    if (state.index >= state.questions.length) { finishTrap(); return; }
    const item = state.questions[state.index];
    holder.replaceChildren();

    const meta = el('div', 'trapmeta');
    meta.appendChild(el('span', '', `${state.index + 1}/${state.questions.length}`));
    meta.appendChild(el('span', '', item.concept_id || 'unscoped'));
    meta.appendChild(el('span', '', `Tier ${item.difficulty_tier}`));
    holder.appendChild(meta);
    holder.appendChild(el('div', 'trapquestion rich', item.question_md));

    const options = el('div', 'trapoptions');
    item.options.forEach((text, index) => {
      const button = el('button', 'opt trapopt');
      button.type = 'button';
      button.dataset.index = String(index);
      button.innerHTML = `<span class="optkey">${'ABCD'[index]}</span><span class="opttext rich">${esc(text)}</span>`;
      button.addEventListener('click', () => answerTrap(index));
      options.appendChild(button);
    });
    holder.appendChild(options);

    const explain = el('div', 'trapexplain');
    explain.id = 'trapexplain';
    holder.appendChild(explain);
    if (typeof window.renderMath === 'function') window.renderMath(holder);
  }

  function answerTrap(choice) {
    const state = G.trap;
    const holder = $('#trapcard');
    if (!state || state.done || state.locked || !holder) return;
    const item = state.questions[state.index];
    state.locked = true;
    state.answered += 1;
    const buttons = [...holder.querySelectorAll('.trapopt')];
    buttons.forEach((button, index) => {
      button.disabled = true;
      if (index === item.correct_index) button.classList.add('good');
      if (index === choice && choice !== item.correct_index) button.classList.add('badd');
    });
    const correct = choice === item.correct_index;
    if (correct) {
      state.correct += 1;
      state.score += 10 * state.combo;
      state.combo = Math.min(5, state.combo + 1);
      state.bestCombo = Math.max(state.bestCombo, state.combo);
    } else {
      state.combo = 1;
    }
    const comboNode = $('#trapcombo');
    if (comboNode) comboNode.textContent = `×${state.combo}`;
    const scoreNode = $('#trapscore');
    if (scoreNode) scoreNode.textContent = `${state.score} pts`;

    const explain = $('#trapexplain');
    if (explain) {
      explain.className = 'trapexplain ' + (correct ? 'ok' : 'no');
      explain.innerHTML = `<div class="verdict">${correct ? '✓ Trap avoided' : '✗ Trap triggered'}</div>` +
        `<div class="rich">${esc(item.explanation_md || '')}</div>`;
      if (typeof window.renderMath === 'function') window.renderMath(explain);
    }
    setTimeout(() => {
      if (!G.trap || G.trap !== state || state.done) return;
      state.locked = false;
      state.index += 1;
      if (state.index >= state.questions.length) finishTrap();
      else renderTrapQuestion();
    }, correct ? 800 : 1700);
  }

  function finishTrap() {
    const state = G.trap;
    if (!state || state.done) return;
    state.done = true;
    clearInterval(G.timers.trap);
    const accuracy = state.answered ? state.correct / state.answered : 0;
    const score = state.score;
    const out = openStage();
    out.appendChild(stageHeader('Trap Hunt', 'Round complete'));
    const box = el('div', 'gamefinish');
    box.appendChild(el('h3', '', `${score} points`));
    box.appendChild(el('p', '', `${state.correct}/${state.answered} traps avoided · best combo ×${state.bestCombo} · ${Math.round(accuracy * 100)}% accuracy`));
    const again = el('button', 'pri', 'Play again');
    again.type = 'button';
    again.addEventListener('click', startTrap);
    box.appendChild(again);
    const hub = el('button', 'ghost', 'Back to games');
    hub.type = 'button';
    hub.addEventListener('click', back);
    box.appendChild(hub);
    out.appendChild(box);
    submit({
      game: 'trap_hunt', topic: state.topic, score, accuracy,
      duration: Math.min(TRAP_SECONDS,
        Math.round((Date.now() - (state.endsAt - TRAP_SECONDS * 1000)) / 1000)),
      detail: { answered: state.answered, correct: state.correct, best_combo: state.bestCombo }
    });
  }

  /* ============================================================ concept match */
  function startMatch() {
    clearTimers();
    G.match = null;
    notice('Dealing cards from your notes…');
    api(`/api/games/match?topic=${encodeURIComponent(G.topic)}&pairs=6`)
      .then(payload => {
        if (!payload.pairs) {
          notice('No concept/explanation pairs in this topic yet.', true);
          const out = openStage();
          out.appendChild(stageHeader('Concept Match', 'Flip and pair'));
          out.appendChild(el('p', 'gameempty', 'Generate more questions from Sync, then come back.'));
          return;
        }
        notice('');
        G.match = {
          pairs: payload.pairs,
          mixed: !!payload.mixed,
          topic: G.topic,
          cards: payload.cards.map((card, index) => ({ ...card, index, up: false, matched: false })),
          first: null,
          locked: false,
          moves: 0,
          startedAt: Date.now(),
          elapsed: 0,
          done: false
        };
        renderMatch();
        G.timers.match = setInterval(() => {
          const state = G.match;
          if (!state || state.done) return;
          state.elapsed = Math.floor((Date.now() - state.startedAt) / 1000);
          const node = $('#matchclock');
          if (node) node.textContent = clock(state.elapsed);
        }, 1000);
      })
      .catch(() => {
        notice('Concept Match needs the host. Reconnect and try again.', true);
      });
  }

  function resumeMatch() {
    if (!G.match) { startMatch(); return; }
    renderMatch();
    clearInterval(G.timers.match);
    G.timers.match = setInterval(() => {
      const state = G.match;
      if (!state || state.done) return;
      state.elapsed = Math.floor((Date.now() - state.startedAt) / 1000);
      const node = $('#matchclock');
      if (node) node.textContent = clock(state.elapsed);
    }, 1000);
  }

  function renderMatch() {
    const state = G.match;
    if (!state) return;
    const out = openStage();
    out.appendChild(stageHeader('Concept Match',
      `${state.pairs} pairs · ${state.topic || 'all topics'}${state.mixed ? ' + other topics' : ''}`));

    const bar = el('div', 'matchbar');
    bar.appendChild(el('span', 'matchclock', clock(state.elapsed)));
    bar.querySelector('.matchclock').id = 'matchclock';
    bar.appendChild(el('span', '', `${state.moves} moves`));
    const matched = el('span', '', `${state.cards.filter(card => card.matched).length / 2}/${state.pairs} matched`);
    bar.appendChild(matched);
    out.appendChild(bar);

    const grid = el('div', 'matchgrid');
    state.cards.forEach(card => {
      const button = el('button', 'matchcard');
      button.type = 'button';
      button.dataset.index = String(card.index);
      if (card.up || card.matched) button.classList.add('up');
      if (card.matched) button.classList.add('matched');
      if (card.up || card.matched) {
        button.innerHTML = `<span class="matchkind">${card.kind === 'concept' ? 'concept' : 'clue'}</span>` +
          `<span class="matchtext">${esc(card.text)}</span>`;
      } else {
        button.innerHTML = '<span class="matchback">?</span>';
      }
      button.addEventListener('click', () => flipMatch(card.index));
      grid.appendChild(button);
    });
    out.appendChild(grid);
    out.appendChild(el('p', 'hint', 'Match each concept with the explanation that belongs to it.'));
  }

  function flipMatch(index) {
    const state = G.match;
    if (!state || state.done || state.locked) return;
    const card = state.cards[index];
    if (!card || card.up || card.matched) return;
    card.up = true;

    if (state.first === null) {
      state.first = index;
      renderMatch();
      return;
    }
    const first = state.cards[state.first];
    state.moves += 1;
    state.locked = true;
    renderMatch();

    if (first.pair === card.pair && first.kind !== card.kind) {
      first.matched = true;
      card.matched = true;
      state.first = null;
      state.locked = false;
      renderMatch();
      if (state.cards.every(entry => entry.matched)) finishMatch();
      return;
    }

    setTimeout(() => {
      if (!G.match || G.match !== state || state.done) return;
      first.up = false;
      card.up = false;
      state.first = null;
      state.locked = false;
      const out = $('#gameout');
      if (out && !out.hidden) renderMatch();
    }, 800);
  }

  function finishMatch() {
    const state = G.match;
    if (!state || state.done) return;
    state.done = true;
    clearInterval(G.timers.match);
    state.elapsed = Math.floor((Date.now() - state.startedAt) / 1000);
    const accuracy = Math.min(1, state.pairs / Math.max(1, state.moves));
    const score = Math.max(100, state.pairs * 120 - state.moves * 8 - state.elapsed * 2);
    const out = openStage();
    out.appendChild(stageHeader('Concept Match', 'Board cleared'));
    const box = el('div', 'gamefinish');
    box.appendChild(el('h3', '', `${score} points`));
    box.appendChild(el('p', '', `${state.pairs} pairs in ${state.moves} moves · ${clock(state.elapsed)} · ${Math.round(accuracy * 100)}% accuracy`));
    const again = el('button', 'pri', 'Deal again');
    again.type = 'button';
    again.addEventListener('click', startMatch);
    box.appendChild(again);
    const hub = el('button', 'ghost', 'Back to games');
    hub.type = 'button';
    hub.addEventListener('click', back);
    box.appendChild(hub);
    out.appendChild(box);
    submit({
      game: 'match', topic: state.topic, score, accuracy,
      duration: state.elapsed,
      detail: { pairs: state.pairs, moves: state.moves }
    });
  }

  /* ================================================================ scoring */
  async function submit(round) {
    const payload = {
      game: round.game,
      topic: round.topic || '',
      score: Math.round(round.score),
      accuracy: Math.min(1, Math.max(0, Number(round.accuracy) || 0)),
      duration_seconds: Math.max(0, Math.round(round.duration || 0)),
      detail: round.detail || {},
      client_id: 'game-' + shortId()
    };
    try {
      const result = await api('/api/games/score', payload);
      const current = G.scores[payload.game] || { best: 0, rounds: 0 };
      G.scores[payload.game] = {
        best: Math.max(result.best || 0, payload.score),
        rounds: (current.rounds || 0) + 1
      };
      local('gamebest', { topic: G.topic, best: G.scores });
      renderBest();
      toast(`Score ${payload.score} saved · best ${G.scores[payload.game].best}`);
    } catch (_) {
      try { await idbWrite('game_scores', [payload]); } catch (_) { /* cache best effort */ }
      toast('Round saved on this device — it will upload when the host is reachable.');
    }
  }

  async function flushScores() {
    const queued = await idbRead('game_scores').catch(() => []);
    let uploaded = 0;
    for (const row of queued) {
      try {
        await api('/api/games/score', row);
        await idbDelete('game_scores', [row.client_id]);
        uploaded += 1;
      } catch (_) { break; }
    }
    if (uploaded) {
      toast(`Uploaded ${uploaded} offline game score(s)`);
      loadScores();
    }
    return uploaded;
  }

  /* ------------------------------------------------------------------- boot */
  function clearTimers() {
    Object.keys(G.timers).forEach(key => {
      clearInterval(G.timers[key]);
      G.timers[key] = null;
    });
  }

  function back() {
    clearTimers();
    G.trap = null;
    renderHub();
    loadScores();
  }

  function start(name) {
    if (name === 'sudoku') {
      if (hasSavedSudoku()) resumeSudoku();
      else startSudoku('medium', true);
    }
    else if (name === 'trap_hunt' || name === 'trap') startTrap();
    else if (name === 'match') startMatch();
  }

  function bind() {
    const nav = document.querySelector('nav button[data-p="games"]');
    if (nav) nav.addEventListener('click', activate);
    const select = $('#gametopic');
    if (select) {
      G.topic = local('gametopic') || '';
      select.addEventListener('change', () => {
        G.topic = select.value;
        local('gametopic', G.topic);
        loadScores();
        if (!G.sudoku && !G.trap && !G.match) renderHub();
      });
    }
    document.addEventListener('keydown', event => {
      if (!$('#p-games') || !$('#p-games').classList.contains('on')) return;
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const state = G.sudoku;
      if (!state || state.done) return;
      if (/^[1-9]$/.test(event.key)) { sudokuInput(Number(event.key)); event.preventDefault(); return; }
      if (event.key === 'Backspace' || event.key === 'Delete' || event.key === '0') {
        sudokuInput(0); event.preventDefault(); return;
      }
      if (event.key.toLowerCase() === 'h') { hintSudoku(); return; }
      const moves = { ArrowUp: -9, ArrowDown: 9, ArrowLeft: -1, ArrowRight: 1 };
      if (moves[event.key] !== undefined) {
        event.preventDefault();
        const current = state.selected < 0 ? 0 : state.selected;
        const row = Math.floor(current / 9);
        const col = current % 9;
        let nextRow = row;
        let nextCol = col;
        if (event.key === 'ArrowUp') nextRow = Math.max(0, row - 1);
        if (event.key === 'ArrowDown') nextRow = Math.min(8, row + 1);
        if (event.key === 'ArrowLeft') nextCol = Math.max(0, col - 1);
        if (event.key === 'ArrowRight') nextCol = Math.min(8, col + 1);
        state.selected = nextRow * 9 + nextCol;
        state.focus = true;
        paintSudoku();
      }
    });
    window.addEventListener('online', () => { flushScores(); });
  }

  let booted = false;
  async function activate() {
    if (!booted) booted = true;
    await loadTopics();
    await loadScores();
    flushScores();
    if (!G.sudoku && !G.trap && !G.match) renderHub();
  }

  function boot() {
    bind();
    if ($('#p-games') && $('#p-games').classList.contains('on')) activate();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  const surface = {
    activate, start, back, refreshTopics: loadTopics, flushScores, renderHub
  };
  window.games = surface;
  return surface;
})();
