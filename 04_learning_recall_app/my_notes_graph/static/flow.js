'use strict';
/* Recall — offline question bank, flow-state ratings and mobile sync.
 *
 * The bank is deliberately decoupled from the exam engine in workstation.js:
 * questions are pre-generated on the host, cached in IndexedDB, and answered
 * either online (POST /api/sync/push_reviews) or entirely offline (the review
 * waits in IndexedDB and is uploaded on the next connection).
 *
 * Public surface (used by inline markup in index.html):
 *   bank.activate()          load the queue when the tab becomes visible
 *   bank.toggleTravel()      switch the commute filter
 *   bank.openSync()          open the sync modal
 *   bank.closeSync()
 *   bank.generate(n)         ask the host factory for more questions
 *   bank.syncMobile()        pull a deck into IndexedDB
 *   bank.downloadDeck()      save a deck file for manual transfer
 *   bank.uploadProgress(f)   import reviews (or a deck) from a file
 */
const bank = (() => {
  const DB_NAME = 'recall-offline';
  const DB_VERSION = 2;
  const GRADE = { 1: 4, 2: 3, 3: 3, 4: 2, 5: 1 };
  // Mirrors engine.fsrs_bridge.grade_for_review: the outcome decides the grade.
  function gradeFor(flowRating, correct) {
    return correct ? Math.max(2, GRADE[flowRating]) : 1;
  }
  const RATING_LABELS = {
    1: ['😴', 'Too Easy'], 2: ['😌', 'Comfortable'], 3: ['⚡', 'Flow State'],
    4: ['🧗', 'Challenging'], 5: ['🤯', 'Overwhelmed']
  };
  const TYPE_LABEL = {
    INTUITION: 'Intuition', CALCULATION_STEP: 'Calculation', ADVERSARIAL_TRAP: 'Trap',
    SYSTEM_APPLICATION: 'System'
  };
  const TYPE_ORDER = ['INTUITION', 'CALCULATION_STEP', 'ADVERSARIAL_TRAP', 'SYSTEM_APPLICATION'];

  const B = {
    mode: readTravel() ? 'travel' : 'all',
    session: localStorage.getItem('recall.session') || 'web-' + shortId(),
    queue: [], pos: 0, current: null, answered: false,
    askedAt: 0, busy: false, offline: false, lastStats: null, pollTimer: null, savedAt: 0,
    order: [0, 1, 2, 3], elo: null
  };
  // Keep one session id per device so momentum and the daily seed survive reloads.
  try { localStorage.setItem('recall.session', B.session); } catch (_) { /* private mode */ }

  /* ---------- tiny helpers ---------- */
  function $(selector) { return document.querySelector(selector); }
  function shortId() {
    try { return crypto.randomUUID().slice(0, 12); } catch (_) {
      return Math.random().toString(36).slice(2, 14);
    }
  }
  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g,
      c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  /* Escape first, then re-introduce a deliberately small markup subset. */
  function rich(text) {
    return esc(text)
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\n{2,}/g, '</p><p>')
      .replace(/\n/g, '<br>');
  }
  function math(root) {
    if (typeof window.renderMath === 'function') window.renderMath(root);
  }
  function notice(text, bad) {
    const el = $('#banknotice');
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('bad', !!bad);
  }
  function toast(text) {
    const el = $('#toast');
    if (!el) return;
    el.textContent = text;
    el.classList.add('show');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => el.classList.remove('show'), 3200);
  }

  async function request(path, body, method) {
    const init = body === undefined
      ? { method: method || 'GET' }
      : { method: method || 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    const response = await fetch(path, init);
    let payload = null;
    try { payload = await response.json(); } catch (_) { /* non-JSON */ }
    if (response.status === 401) {
      notice('This device is not paired with the laptop. Open /pair and enter the pairing code.', true);
    }
    if (!response.ok) {
      const detail = payload && (payload.detail || payload.error);
      throw new Error(typeof detail === 'string' ? detail : `HTTP ${response.status}`);
    }
    return payload || {};
  }

  /* ---------- IndexedDB ---------- */
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

  async function idbWrite(store, rows, clearFirst) {
    const database = await db();
    return new Promise((resolve, reject) => {
      const tx = database.transaction(store, 'readwrite');
      const target = tx.objectStore(store);
      if (clearFirst) target.clear();
      for (const row of rows) target.put(row);
      tx.oncomplete = () => { database.close(); resolve(rows.length); };
      tx.onerror = () => { database.close(); reject(tx.error); };
    });
  }

  async function idbRead(store) {
    const database = await db();
    return new Promise((resolve, reject) => {
      const tx = database.transaction(store, 'readonly');
      const target = tx.objectStore(store);
      const all = target.getAll();
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

  async function remember(key, value) {
    try { await idbWrite('meta', [{ key, value }]); } catch (_) { /* cache is best effort */ }
    try { localStorage.setItem('recall.' + key, JSON.stringify(value)); } catch (_) { /* quota */ }
  }

  async function recall(key, fallback) {
    try {
      const rows = await idbRead('meta');
      const hit = rows.find(row => row.key === key);
      if (hit) return hit.value;
    } catch (_) { /* fall through */ }
    try {
      const raw = localStorage.getItem('recall.' + key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (_) { return fallback; }
  }

  /* ---------- travel mode ---------- */
  function readTravel() {
    try {
      if (new URLSearchParams(location.search).get('travel') === '1') return true;
      return localStorage.getItem('recall.travel') === '1';
    } catch (_) { return false; }
  }

  function syncTravelButton() {
    const button = $('#traveltoggle');
    if (!button) return;
    button.setAttribute('aria-pressed', B.mode === 'travel' ? 'true' : 'false');
    button.classList.toggle('on', B.mode === 'travel');
    document.body.classList.toggle('travel-mode', B.mode === 'travel');
    const label = $('#traveltxt');
    if (label) label.textContent = B.mode === 'travel' ? 'Travel mode' : 'Travel / Commute';
  }

  async function toggleTravel() {
    B.mode = B.mode === 'travel' ? 'all' : 'travel';
    try { localStorage.setItem('recall.travel', B.mode === 'travel' ? '1' : '0'); } catch (_) { /* private mode */ }
    syncTravelButton();
    B.queue = []; B.pos = 0;
    if ($('#p-bank') && !$('#p-bank').classList.contains('on')) showTab('bank');
    await loadQueue();
    toast(B.mode === 'travel'
      ? 'Travel mode on — paper calculations postponed 24 h (no memory penalty)'
      : 'Desk mode on — full question bank');
  }

  /* ---------- randomisation ---------- */
  function daySalt() {
    return Number(new Date().toISOString().slice(0, 10).replace(/-/g, ''));
  }
  function mix(value, salt) {
    // 32-bit integer hash: a stable per-day shuffle key for offline ordering.
    let h = (value ^ salt) >>> 0;
    h = Math.imul(h ^ (h >>> 16), 0x45d9f3b) >>> 0;
    h = Math.imul(h ^ (h >>> 16), 0x45d9f3b) >>> 0;
    return (h ^ (h >>> 16)) >>> 0;
  }
  function shuffledOrder(count) {
    // Fisher-Yates: option positions change on every showing, so the place of
    // the right answer can never be memorised.
    const order = Array.from({ length: count }, (_, index) => index);
    for (let i = order.length - 1; i > 0; i -= 1) {
      const j = Math.floor(Math.random() * (i + 1));
      [order[i], order[j]] = [order[j], order[i]];
    }
    return order;
  }

  /* ---------- Elo panel ---------- */
  function renderElo(snapshot) {
    const box = $('#elopanel');
    if (!box || !snapshot || !snapshot.overall) return;
    B.elo = snapshot;
    try { localStorage.setItem('recall.elo', JSON.stringify(snapshot)); } catch (_) { /* quota */ }
    const o = snapshot.overall;
    const change = o.change_today;
    const trend = change === null || change === undefined ? ''
      : `<span class="elodelta ${change >= 0 ? 'up' : 'down'}">${change >= 0 ? '▲' : '▼'} ${Math.abs(change).toFixed(0)} today</span>`;
    const warn = o.lost_to_decay >= 1
      ? `<span class="elowarn">−${o.lost_to_decay.toFixed(0)} lost to idleness</span>`
      : (o.drop_if_idle_tomorrow >= 1
        ? `<span class="elohint">−${o.drop_if_idle_tomorrow.toFixed(0)} tomorrow if you skip</span>` : '');
    const chips = (snapshot.domains || []).slice(0, 6).map(d =>
      `<span class="elochip${d.lost_to_decay >= 5 ? ' stale' : ''}" title="${esc(d.games)} answers · idle ${esc(d.idle_days)} d">`
      + `${esc(d.key.split('/').pop())} <b>${Math.round(d.rating)}</b>`
      + (d.lost_to_decay >= 1 ? ` <i>−${d.lost_to_decay.toFixed(0)}</i>` : '') + '</span>').join('');
    box.innerHTML = `<div class="elomain"><span class="elolabel">Elo</span>`
      + `<strong>${Math.round(o.rating)}</strong>${trend}${warn}</div>`
      + (chips ? `<div class="elochips">${chips}</div>` : '');
  }
  async function refreshElo() {
    try {
      renderElo(await request('/api/elo', undefined, 'GET'));
    } catch (_) {
      try { renderElo(JSON.parse(localStorage.getItem('recall.elo') || 'null')); } catch (__) { /* none cached */ }
    }
  }
  async function reconcile() {
    // Drop questions another device already answered; let due ones come back.
    try {
      const state = await request('/api/sync/state', undefined, 'GET');
      const recycled = new Set(state.recycled_ids || []);
      const gone = (state.answered_ids || []).filter(id => !recycled.has(id))
        .concat(state.postponed_ids || []);
      if (gone.length) await idbDelete('deck', gone).catch(() => {});
      if (recycled.size) {
        const local = ((await recall('answered', [])) || []).filter(id => !recycled.has(id));
        await remember('answered', local);
      }
      renderElo(state.elo);
      return state;
    } catch (_) { return null; }
  }

  /* ---------- queue ---------- */
  function offlineInterleave(items, limit) {
    const buckets = {};
    TYPE_ORDER.forEach(name => { buckets[name] = []; });
    items.forEach(item => {
      (buckets[item.cognitive_type] = buckets[item.cognitive_type] || []).push(item);
    });
    const queue = [];
    let position = 0;
    let last = '';
    while (queue.length < limit && Object.keys(buckets).some(k => buckets[k].length)) {
      let choice = '';
      for (let step = 0; step < TYPE_ORDER.length; step += 1) {
        const name = TYPE_ORDER[(position + step) % TYPE_ORDER.length];
        if (name !== last && buckets[name] && buckets[name].length) {
          choice = name;
          position = (position + step + 1) % TYPE_ORDER.length;
          break;
        }
      }
      if (!choice) {
        choice = TYPE_ORDER.find(name => buckets[name] && buckets[name].length);
        position = TYPE_ORDER.indexOf(choice) + 1;
      }
      queue.push(buckets[choice].shift());
      last = choice;
    }
    return queue;
  }

  async function cachedQueue(limit) {
    const deck = await idbRead('deck');
    const answered = new Set((await recall('answered', [])) || []);
    const ordered = deck
      .filter(item => !answered.has(item.id))
      .filter(item => B.mode !== 'travel' || !item.requires_scratchpad)
      .sort((a, b) => (a.difficulty_tier - b.difficulty_tier)
        || (mix(a.id, daySalt()) - mix(b.id, daySalt())) || (a.id - b.id));
    return offlineInterleave(ordered, limit);
  }

  async function loadQueue() {
    const card = $('#bankcard');
    if (!card) return;
    card.replaceChildren(skeleton('Loading your pool…'));
    let questions = [];
    try {
      const payload = await request(`/api/quiz/queue?limit=25&mode=${B.mode}&session_id=${encodeURIComponent(B.session)}`, undefined, 'GET');
      questions = Array.isArray(payload.questions) ? payload.questions : [];
      B.offline = false;
      await flushReviews();
      await reconcile();
      const answerSet = await recall('answered', []);
      const done = new Set(Array.isArray(answerSet) ? answerSet : []);
      const pending = new Set((await idbRead('reviews').catch(() => [])).map(r => r.quiz_id));
      // A locally answered question may still be STAGED on the host until the
      // queued review is flushed, so filter it out of the served queue.  A
      // question the host says is due again (recycled) is allowed back.
      questions = questions.filter(item => !pending.has(item.id)
        && (item.recycled || !done.has(item.id)));
      if (questions.length) {
        await idbWrite('deck', questions.map(slim));
      }
      notice('');
    } catch (error) {
      B.offline = true;
      questions = await cachedQueue(25);
      notice(questions.length
        ? `Offline — ${questions.length} cached question(s) ready. Reviews will upload when you reconnect.`
        : 'Offline and no cached deck. Connect once and use “Sync Mobile”.', !questions.length);
    }
    B.queue = questions;
    B.pos = 0;
    render();
    refreshStats();
  }

  function slim(item) {
    return {
      id: item.id, concept_id: item.concept_id, topic_folder: item.topic_folder,
      source_pdf: item.source_pdf, page_num: item.page_num,
      cognitive_type: item.cognitive_type, requires_scratchpad: item.requires_scratchpad,
      difficulty_tier: item.difficulty_tier, question_md: item.question_md,
      options: Array.isArray(item.options) ? item.options : JSON.parse(item.options_json || '[]'),
      correct_index: item.correct_index, explanation_md: item.explanation_md,
      recycled: item.recycled ? 1 : 0
    };
  }

  /* ---------- rendering ---------- */
  function skeleton(text) {
    const box = document.createElement('div');
    box.className = 'empty';
    const title = document.createElement('h2');
    title.textContent = text;
    box.appendChild(title);
    return box;
  }

  function badge(text, className) {
    const span = document.createElement('span');
    span.className = 'badge ' + (className || '');
    span.textContent = text;
    return span;
  }

  function render() {
    const card = $('#bankcard');
    if (!card) return;
    if (!B.queue.length) {
      const empty = skeleton(B.mode === 'travel' ? 'No desk-free questions staged' : 'The pool is empty');
      const detail = document.createElement('p');
      detail.textContent = B.mode === 'travel'
        ? 'Travel mode hides every question that needs paper. Generate more questions or switch back to Desk mode.'
        : 'Open Sync and press “Generate 50 More Questions” to fill the bank.';
      empty.appendChild(detail);
      const action = document.createElement('button');
      action.className = 'pri';
      action.textContent = 'Open sync';
      action.addEventListener('click', openSync);
      empty.appendChild(action);
      card.replaceChildren(empty);
      return;
    }
    const item = B.queue[B.pos % B.queue.length];
    B.current = item;
    B.answered = false;
    B.rated = false;
    B.askedAt = Date.now();

    const root = document.createElement('article');
    root.className = 'qcard bankcard';
    root.dataset.quiz = item.id;

    const meta = document.createElement('div');
    meta.className = 'qtopic';
    const concept = document.createElement('span');
    concept.textContent = item.concept_id || 'unscoped';
    meta.appendChild(concept);
    meta.appendChild(badge(TYPE_LABEL[item.cognitive_type] || item.cognitive_type, 'type-' + item.cognitive_type));
    meta.appendChild(badge('Tier ' + item.difficulty_tier, 'tierchip'));
    if (item.remediation === 'prerequisite') meta.appendChild(badge('⬅ Prerequisite', 'reinforcechip'));
    else if (item.remediation === 'intuition') meta.appendChild(badge('💡 Simpler card', 'reinforcechip'));
    else if (item.reinforcement) meta.appendChild(badge('🔁 Reinforcement', 'reinforcechip'));
    if (item.recycled) meta.appendChild(badge('↻ Due again', 'reinforcechip'));
    if (typeof item.predicted_success === 'number') {
      meta.appendChild(badge(`${Math.round(item.predicted_success * 100)}% odds`, 'tierchip'));
    }
    meta.appendChild(badge(item.requires_scratchpad ? '✎ paper' : '🧠 mental',
      item.requires_scratchpad ? 'paperchip' : 'mentalchip'));
    meta.appendChild(badge(`${B.pos + 1}/${B.queue.length}`, 'counter'));
    root.appendChild(meta);

    const question = document.createElement('div');
    question.className = 'qq rich';
    question.innerHTML = rich(item.question_md);
    root.appendChild(question);

    const options = document.createElement('div');
    options.className = 'optlist';
    const choices = Array.isArray(item.options) ? item.options
      : (() => { try { return JSON.parse(item.options_json || '[]'); } catch (_) { return []; } })();
    // Display order is shuffled; ``data-index`` and choose() keep the stored
    // index, so grading and the uploaded review are unaffected.
    B.order = shuffledOrder(choices.length);
    B.order.forEach((index, slot) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'opt';
      button.dataset.index = String(index);
      button.innerHTML = `<span class="optkey">${'ABCD'[slot]}</span><span class="opttext rich">${rich(choices[index])}</span>`;
      button.addEventListener('click', () => choose(index));
      options.appendChild(button);
    });
    root.appendChild(options);

    const actions = document.createElement('div');
    actions.className = 'bankactions';
    const postpone = document.createElement('button');
    postpone.type = 'button';
    postpone.className = 'ghost postpone';
    postpone.textContent = '⏰ Postpone (Solve Later at Desk)';
    postpone.addEventListener('click', () => postponeCurrent());
    actions.appendChild(postpone);
    const skip = document.createElement('button');
    skip.type = 'button';
    skip.className = 'ghost';
    skip.textContent = 'Skip →';
    skip.addEventListener('click', next);
    actions.appendChild(skip);
    const hint = document.createElement('span');
    hint.className = 'bankhint';
    hint.textContent = 'Keys: 1–4 answer · P postpone · Space/Enter next';
    actions.appendChild(hint);
    root.appendChild(actions);

    card.replaceChildren(root);
    math(root);
    const first = root.querySelector('.opt');
    if (first) first.focus({ preventScroll: true });
  }

  /* ---------- answering ---------- */
  function choose(index) {
    if (!B.current || B.answered) return;
    B.answered = true;
    B.selection = index;
    const answeredCard = document.querySelector('#bankcard .bankcard');
    if (answeredCard) answeredCard.classList.add('answered');
    const item = B.current;
    const root = $('#bankcard');
    const buttons = [...root.querySelectorAll('.opt')];
    buttons.forEach(button => {
      const position = Number(button.dataset.index);
      button.disabled = true;
      if (position === item.correct_index) button.classList.add('good');
      if (position === index && index !== item.correct_index) button.classList.add('badd');
      if (position === index) button.classList.add('selected');
    });
    const correct = index === item.correct_index;

    const explanation = document.createElement('div');
    explanation.className = 'explain ' + (correct ? 'ok' : 'no');
    explanation.innerHTML = `<div class="verdict">${correct ? '✓ Correct' : '✗ Not quite — correct answer is ' + 'ABCD'[Math.max(0, B.order.indexOf(item.correct_index))]}</div>`
      + `<div class="rich">${rich(item.explanation_md || '')}</div>`;
    root.appendChild(explanation);

    const selector = document.createElement('div');
    selector.className = 'flowwrap';
    const prompt = document.createElement('div');
    prompt.className = 'flowprompt';
    prompt.textContent = 'How did that feel? Rate the challenge to set the next difficulty. Keys 1–5, or Space for Flow State.';
    selector.appendChild(prompt);
    const row = document.createElement('div');
    row.className = 'flow-row';
    Object.entries(RATING_LABELS).forEach(([rating, [emoji, label]]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'flowbtn';
      button.dataset.flow = rating;
      button.innerHTML = `<span class="flowemoji">${emoji}</span><span>${label}</span>`;
      button.addEventListener('click', () => rate(Number(rating), index));
      row.appendChild(button);
    });
    selector.appendChild(row);
    root.appendChild(selector);

    math(explanation);
    selector.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  async function rate(flowRating, selection) {
    // Persist the grade instantly, even if the network is down.
    if (!B.current || B.rated) return;
    B.rated = true;
    B.selection = selection;
    const item = B.current;
    const correct = selection === item.correct_index;
    const root = $('#bankcard');
    root.querySelectorAll('.flowbtn').forEach(button => {
      button.disabled = true;
      button.classList.toggle('chosen', Number(button.dataset.flow) === flowRating);
    });
    const review = {
      quiz_id: item.id,
      user_selection: selection,
      time_spent_seconds: Math.max(0, Math.round((Date.now() - B.askedAt) / 1000)),
      flow_rating: flowRating,
      fsrs_grade: gradeFor(flowRating, correct),
      context_mode: B.mode === 'travel' ? 'COMMUTE_TRAVEL' : 'DESK_STANDARD',
      error_category: selection === item.correct_index ? 'NONE' : null,
      client_id: B.session + '-' + item.id + '-' + Date.now(),
      reviewed_at: new Date().toISOString()
    };
    await localComplete(item.id);
    let session = null;
    let rating = null;
    let delivered = false;
    if (!B.offline) {
      try {
        const response = await request('/api/sync/push_reviews', { reviews: [review], session_id: B.session });
        delivered = (response.accepted || 0) > 0 || (response.duplicates || 0) > 0;
        session = response.session || null;
        rating = response.elo || null;
      } catch (error) {
        delivered = false;
      }
    }
    if (!delivered) {
      try { await idbWrite('reviews', [review]); } catch (_) { /* nothing else we can do */ }
      notice('Review saved on this device. It will upload when the host is reachable.');
    }
    const note = document.createElement('div');
    note.className = 'srsnote';
    const grade = gradeFor(flowRating, correct);
    const pace = rating && rating.speed_factor > 1.01 ? ` (fast ×${rating.speed_factor.toFixed(2)})`
      : rating && rating.speed_factor < 0.99 ? ` (slow ×${rating.speed_factor.toFixed(2)})` : '';
    const moved = rating && typeof rating.overall_delta === 'number'
      ? ` · Elo ${rating.overall_delta >= 0 ? '+' : ''}${rating.overall_delta.toFixed(1)}${pace} → ${Math.round(rating.overall)}` : '';
    note.textContent = delivered
      ? `FSRS grade ${grade} applied · ${session ? session.directive : 'momentum updated'}${moved}`
      : `Queued offline · FSRS grade ${grade}`;
    if (delivered) refreshElo();
    const selector = root.querySelector('.flowwrap');
    if (selector) selector.appendChild(note);
    await scheduleReinforcement(item, correct, flowRating);
    const nextButton = document.createElement('button');
    nextButton.type = 'button';
    nextButton.className = 'pri nextq';
    nextButton.textContent = B.pos + 1 < B.queue.length ? 'Next question →' : 'Next batch →';
    nextButton.addEventListener('click', next);
    nextButton.title = 'Space or Enter';
    if (selector) selector.appendChild(nextButton);
    nextButton.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    refreshStats();
  }

  async function scheduleReinforcement(item, correct, flowRating) {
    // Missed or effortful questions return as a same-concept variant 8-12 slots
    // later.  A variant is itself a second pass, so it never re-schedules.
    // A missed variant is a second failure in the session: the server then steps
    // back to the prerequisite (or a simpler card for a leech).  Prerequisite and
    // intuition cards themselves never re-schedule.
    if (item.remediation === 'prerequisite' || item.remediation === 'intuition') return;
    if (item.reinforcement && correct) return;
    if (correct && flowRating < 4) return;
    const secondPass = !!item.reinforcement;
    let question = null;
    let gap = 8 + Math.floor(Math.random() * 5);
    if (!B.offline) {
      try {
        const payload = await request('/api/quiz/weakness',
          { quiz_id: item.id, session_id: B.session, position: B.pos, correct: !!correct }, 'POST');
        question = payload && payload.question ? payload.question : null;
        if (payload && payload.insert_after) gap = payload.insert_after;
        if (question && payload.remediation) question.remediation = payload.remediation;
        // After a missed variant only a step back helps; another variant would loop.
        if (secondPass && question && question.remediation === 'variant') question = null;
      } catch (error) {
        question = null;
      }
    }
    if (!question && !secondPass) {
      const deck = await idbRead('deck').catch(() => []);
      const queued = new Set(B.queue.map(entry => entry.id));
      question = (deck || []).find(entry => entry.id !== item.id
        && entry.concept_id === item.concept_id
        && entry.cognitive_type !== item.cognitive_type
        && !queued.has(entry.id)) || null;
    }
    if (!question || B.queue.some(entry => entry.id === question.id)) return;
    question.reinforcement = true;
    const at = Math.min(B.queue.length, B.pos + 1 + gap);
    B.queue.splice(at, 0, question);
    if (question.remediation === 'prerequisite') {
      toast(`Missed twice: stepping back to “${question.concept_id}” first`);
    } else if (question.remediation === 'intuition') {
      toast(`“${item.concept_id}” keeps slipping: a simpler card is next`);
    } else {
      toast(`Weak concept “${question.concept_id}” returns in ${gap} questions`);
    }
  }

  async function localComplete(quizId) {    const answered = new Set((await recall('answered', [])) || []);
    answered.add(quizId);
    // Keep the local ledger bounded; a synced question leaves the pool anyway.
    const recent = [...answered].slice(-500);
    await remember('answered', recent);
    try { await idbDelete('deck', [quizId]); } catch (_) { /* cache only */ }
  }

  async function next() {
    B.pos += 1;
    if (B.pos >= B.queue.length) {
      await loadQueue();
      return;
    }
    render();
  }

  async function postponeCurrent() {
    if (!B.current) return;
    const item = B.current;
    const button = $('#bankcard .postpone');
    if (button) { button.disabled = true; button.textContent = 'Postponing…'; }
    try {
      await request('/api/quiz/postpone', { quiz_id: item.id, hours: 24 });
      toast('Postponed 24 h — no lapse recorded');
    } catch (error) {
      toast('Saved locally; will postpone on reconnect');
    }
    await localComplete(item.id);
    await next();
  }

  /* ---------- stats + sync modal ---------- */
  async function refreshStats() {
    const target = $('#bankstat');
    try {
      const stats = await request('/api/quiz/stats', undefined, 'GET');
      B.lastStats = stats;
      if (target) {
        target.textContent = `${stats.available} available · ${stats.synced_to_device} on device · ${stats.completed} completed`;
      }
      if ($('#syncstats')) renderStats(stats);
      return stats;
    } catch (error) {
      const deck = await idbRead('deck').catch(() => []);
      const answered = (await recall('answered', [])) || [];
      if (target) target.textContent = `offline · ${deck.length} cached · ${answered.length} answered`;
      return null;
    }
  }

  function renderStats(stats) {
    const box = $('#syncstats');
    if (!box) return;
    const rows = [
      ['Available in local pool', stats.available],
      ['Synced to device', stats.synced_to_device],
      ['Completed', stats.completed],
      ['Postponed', stats.postponed],
      ['Reviews logged (today)', `${stats.reviews_today} / ${stats.reviews_logged}`],
      ['Travel-ready (no paper)', stats.travel_ready],
      ['Pool total', stats.bank_total]
    ];
    box.replaceChildren();
    rows.forEach(([label, value]) => {
      const cell = document.createElement('div');
      cell.className = 'syncstat';
      cell.innerHTML = `<span>${esc(label)}</span><strong>${esc(value)}</strong>`;
      box.appendChild(cell);
    });
    const types = document.createElement('div');
    types.className = 'synctypes';
    types.textContent = Object.entries(stats.by_type || {})
      .map(([name, count]) => `${TYPE_LABEL[name] || name}: ${count}`).join(' · ');
    box.appendChild(types);
  }

  function openSync() {
    const modal = $('#syncmodal');
    if (!modal) return;
    modal.classList.add('show');
    modal.setAttribute('aria-hidden', 'false');
    refreshStats();
    loadNetworkInfo();
  }

  async function loadNetworkInfo() {
    const box = $('#networkinfo');
    if (!box) return;
    box.replaceChildren();
    const label = document.createElement('div');
    label.className = 'networklabel';
    label.textContent = 'Phone / tablet connection';
    box.appendChild(label);
    try {
      const info = await request('/api/network/info', undefined, 'GET');
      const primary = document.createElement('code');
      primary.textContent = info.primary;
      box.appendChild(primary);
      const copy = document.createElement('button');
      copy.type = 'button';
      copy.className = 'ghost small';
      copy.textContent = 'Copy URL';
      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(info.primary);
          toast('LAN URL copied');
        } catch (_) {
          toast(info.primary);
        }
      });
      box.appendChild(copy);
      if ((info.urls || []).length > 1) {
        const more = document.createElement('div');
        more.className = 'networkalt';
        more.textContent = 'Also: ' + info.urls.slice(1).join(' · ');
        box.appendChild(more);
      }
      const note = document.createElement('div');
      note.className = 'networknote';
      note.textContent = info.mdns && info.mdns.advertised
        ? `mDNS: ${info.mdns.name}`
        : `mDNS unavailable${info.mdns && info.mdns.error ? ' (' + info.mdns.error + ')' : ''} — use the IP above or USB (adb reverse).`;
      box.appendChild(note);
      const secure = document.createElement('div');
      secure.className = 'networknote';
      secure.textContent = info.secure_context_note || '';
      box.appendChild(secure);
      if (info.pairing_code) {
        // Only the laptop itself is told the code; a phone needs it once.
        const pair = document.createElement('div');
        pair.className = 'networknote';
        pair.textContent = 'Pairing code for a new phone/tablet: ';
        const code = document.createElement('code');
        code.textContent = info.pairing_code;
        pair.appendChild(code);
        box.appendChild(pair);
        const link = (info.pair_urls || [])[0];
        if (link) {
          const copyPair = document.createElement('button');
          copyPair.type = 'button';
          copyPair.className = 'ghost small';
          copyPair.textContent = 'Copy pairing link';
          copyPair.addEventListener('click', async () => {
            try { await navigator.clipboard.writeText(link); toast('Pairing link copied'); }
            catch (_) { toast(link); }
          });
          box.appendChild(copyPair);
        }
      }
    } catch (error) {
      const offline = document.createElement('div');
      offline.className = 'networknote';
      offline.textContent = 'LAN info unavailable (host offline).';
      box.appendChild(offline);
    }
  }

  function closeSync() {
    const modal = $('#syncmodal');
    if (!modal) return;
    modal.classList.remove('show');
    modal.setAttribute('aria-hidden', 'true');
  }

  async function generate(count) {
    const progress = $('#syncprogress');
    try {
      await request('/api/quiz/generate', { count, per_batch: 5 });
      if (progress) progress.textContent = `Generating ${count} questions in micro-batches of 5…`;
      pollGeneration(count);
    } catch (error) {
      if (progress) progress.textContent = 'Generation unavailable: ' + error.message;
    }
  }

  function pollGeneration(requested) {
    clearInterval(B.pollTimer);
    B.pollTimer = setInterval(async () => {
      const progress = $('#syncprogress');
      try {
        const status = await request('/api/quiz/generate/status', undefined, 'GET');
        if (status.running) {
          if (progress) progress.textContent = 'Generating… the host is calling Gemini in batches.';
          return;
        }
        clearInterval(B.pollTimer);
        const last = status.last || {};
        if (last.error) {
          if (progress) progress.textContent = 'Generation failed: ' + last.error;
        } else {
          if (progress) {
            progress.textContent = `Generated ${last.inserted || 0} question(s) in ${last.batches || 0} batch(es) · pool now ${last.finished_bank || '?'}`;
          }
          refreshStats();
          loadQueue();
        }
      } catch (error) {
        clearInterval(B.pollTimer);
        if (progress) progress.textContent = 'Could not read generation status.';
      }
    }, 3000);
  }

  async function syncMobile() {
    const progress = $('#syncprogress');
    try {
      const payload = await request(`/api/sync/pull_deck?limit=100&mode=${B.mode}&include_synced=1`, undefined, 'GET');
      const questions = (payload.questions || []).map(slim);
      if (questions.length) await idbWrite('deck', questions, false);
      await remember('lastSync', { at: new Date().toISOString(), count: questions.length, mode: B.mode, deck_id: payload.deck_id });
      if (progress) {
        progress.textContent = `Cached ${questions.length} question(s) (${Math.round((payload.bytes || 0) / 1024)} KB) for offline study.`;
      }
      refreshStats();
      loadQueue();
    } catch (error) {
      if (progress) progress.textContent = 'Sync failed: ' + error.message;
    }
  }

  async function downloadDeck() {
    const progress = $('#syncprogress');
    try {
      const response = await fetch(`/api/sync/export_deck?limit=200&mode=${B.mode}&download=1`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      const stamp = new Date().toISOString().slice(0, 10);
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `recall-deck-${B.mode}-${stamp}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 4000);
      if (progress) progress.textContent = 'Deck file downloaded — it can be opened on the phone without a network.';
    } catch (error) {
      if (progress) progress.textContent = 'Download failed: ' + error.message;
    }
  }

  async function uploadProgress(file) {
    const progress = $('#syncprogress');
    if (!file) return;
    try {
      const payload = JSON.parse(await file.text());
      const reviews = Array.isArray(payload) ? payload
        : Array.isArray(payload.reviews) ? payload.reviews : null;
      if (reviews) {
        await idbWrite('reviews', reviews, false);
        const result = await flushReviews();
        if (progress) progress.textContent = `Imported ${reviews.length} review(s) · ${result.accepted} accepted, ${result.pending} still queued.`;
        refreshStats();
        loadQueue();
        return;
      }
      if (Array.isArray(payload.questions)) {
        await idbWrite('deck', payload.questions.map(slim), false);
        if (progress) progress.textContent = `Imported a deck of ${payload.questions.length} question(s) into offline storage.`;
        loadQueue();
        return;
      }
      if (progress) progress.textContent = 'That file has neither “reviews” nor “questions”.';
    } catch (error) {
      if (progress) progress.textContent = 'Import failed: ' + error.message;
    }
  }

  async function flushReviews() {
    const queued = await idbRead('reviews').catch(() => []);
    if (!queued.length) return { accepted: 0, pending: 0 };
    let accepted = 0;
    const delivered = [];
    for (let start = 0; start < queued.length; start += 50) {
      const slice = queued.slice(start, start + 50);
      try {
        const response = await request('/api/sync/push_reviews', { reviews: slice, session_id: B.session });
        accepted += response.accepted || 0;
        // Only drop what the host has settled; a "retry" stays queued.
        const settled = Array.isArray(response.results)
          ? new Set(response.results.filter(r => r.status !== 'retry').map(r => r.client_id))
          : null;
        delivered.push(...slice.map(review => review.client_id)
          .filter(id => !settled || settled.has(id)));
      } catch (error) {
        break;
      }
    }
    if (delivered.length) await idbDelete('reviews', delivered).catch(() => {});
    return { accepted, pending: queued.length - delivered.length };
  }

  /* ---------- service worker ---------- */
  function registerWorker() {
    if (!('serviceWorker' in navigator)) return;
    const secure = location.protocol === 'https:'
      || ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
    if (!secure) return;
    navigator.serviceWorker.register('/sw.js').catch(() => { /* http on a LAN IP */ });
  }

  /* ---------- boot ---------- */
  function bind() {
    const navButton = document.querySelector('nav button[data-p="bank"]');
    if (navButton) navButton.addEventListener('click', activate);
    const modal = $('#syncmodal');
    if (modal) modal.addEventListener('click', event => { if (event.target === modal) closeSync(); });
    const file = $('#progressfile');
    if (file) file.addEventListener('change', event => uploadProgress(event.target.files[0]));
    document.addEventListener('keydown', event => {
      if (!$('#p-bank') || !$('#p-bank').classList.contains('on')) return;
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === 'Escape') { closeSync(); return; }
      if (!B.current) return;
      if (!B.answered && /^[1-4]$/.test(event.key)) {
        const picked = B.order[Number(event.key) - 1];
        if (picked !== undefined) choose(picked);
      }
      else if (!B.answered && event.key.toLowerCase() === 'p') { postponeCurrent(); }
      else if (B.answered && !B.rated && /^[1-5]$/.test(event.key)) {
        event.preventDefault();
        rate(Number(event.key), B.selection);
      }
      else if (B.answered && (event.key === ' ' || event.key === 'Enter')) {
        // Space / Enter: accept "Flow State" if not rated yet, otherwise go on.
        if (event.target.tagName === 'BUTTON' && !event.target.disabled && event.key === 'Enter') return;
        event.preventDefault();
        const nextButton = document.querySelector('#bankcard .nextq');
        if (nextButton) nextButton.click();
        else if (!B.rated) rate(3, B.selection);
      }
    });
    window.addEventListener('online', async () => {
      B.offline = false;
      const result = await flushReviews();
      if (result.accepted) toast(`Uploaded ${result.accepted} offline review(s)`);
      refreshStats();
      loadQueue();
    });
    window.addEventListener('offline', () => { B.offline = true; notice('Offline — serving the cached deck.'); });
    // navigator.onLine stays true on a LAN without the laptop, so "online"
    // never fires when the host comes back: retry on a timer and on focus.
    const retry = async () => {
      const queued = await idbRead('reviews').catch(() => []);
      if (!queued.length) return;
      const result = await flushReviews();
      if (result.accepted) {
        B.offline = false;
        toast(`Uploaded ${result.accepted} offline review(s)`);
        refreshStats();
        refreshElo();
      }
    };
    setInterval(retry, 60000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) retry(); });
    syncTravelButton();
  }

  let booted = false;
  async function activate() {
    if (!booted) { booted = true; }
    await loadQueue();
  }

  function boot() {
    bind();
    registerWorker();
    flushReviews().then(() => refreshStats());
    refreshElo();
    if ($('#p-bank') && $('#p-bank').classList.contains('on')) activate();
    if (new URLSearchParams(location.search).get('sync') === '1') openSync();
    syncTravelButton();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  return {
    activate, toggleTravel, openSync, closeSync, generate, syncMobile,
    downloadDeck, uploadProgress, loadQueue, refreshStats, flushReviews
  };
})();
