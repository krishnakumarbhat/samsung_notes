'use strict';
/* Recall study workstation — self-contained app logic.
 * Contract (backend may 404 on new endpoints; degrade with clear error/empty states):
 *  GET  /api/state, /api/sources, /api/coverage, /api/history, /api/graph,
 *       /api/intelligence {days,strengths,weaknesses,recommended,errors},
 *       /api/learning/history {sessions:[...]} (optional),
 *       /source?path=urlencoded#page=N (PDF iframe drawer)
 *  POST /api/studio {action,question,sources[]} -> {answer,sources:[{source,page}]}
 *  POST /api/exam/start|answer|finish
 *  POST /api/tutor/step {session_id?,sources[],message} -> {session_id,explanation,question,sources,feedback?}
 * No auto Gemini call on boot; no fake data; all dynamic text escaped. */

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = (v, d = 0) => (Number.isFinite(+v) ? +v : d);

/* ---------- API ---------- */
class ApiError extends Error {
  constructor(msg, status) { super(msg); this.status = status || 0; }
}
async function api(path, body, method = 'POST') {
  const init = body !== undefined
    ? { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : { method };
  const r = await fetch(path, init);
  let j = null;
  try { j = await r.json(); } catch (_) { /* non-JSON body */ }
  if (!r.ok) throw new ApiError((j && (j.error || j.detail)) || `HTTP ${r.status}`, r.status);
  return j || {};
}
function isMissing(e) { return e instanceof ApiError && (e.status === 404 || e.status === 405); }
function errText(e) { return isMissing(e) ? 'This endpoint is not available on the running server yet.' : String(e && e.message || e); }

function toast(text) {
  const t = $('#toast'); if (!t) return;
  t.textContent = text; t.classList.add('show');
  clearTimeout(toast._h); toast._h = setTimeout(() => t.classList.remove('show'), 3200);
}
function emptyState(title, detail, retry) {
  const box = document.createElement('div');
  box.className = 'empty';
  const h = document.createElement('h2'); h.textContent = title; box.appendChild(h);
  if (detail) { const p = document.createElement('p'); p.textContent = detail; box.appendChild(p); }
  if (retry) {
    const b = document.createElement('button');
    b.className = 'pri'; b.style.marginTop = '14px'; b.textContent = 'Retry';
    b.addEventListener('click', retry); box.appendChild(b);
  }
  return box;
}
function errorState(container, title, error, retry) {
  container.replaceChildren();
  const box = emptyState(title, errText(error), retry);
  box.querySelectorAll('h2').forEach(h => { h.style.color = 'var(--red)'; });
  container.appendChild(box);
}

/* ---------- Tabs ---------- */
function showTab(name) {
  $$('nav button').forEach(b => b.classList.toggle('on', b.dataset.p === name));
  $$('section.pane').forEach(s => s.classList.toggle('on', s.id === 'p-' + name));
  if (name === 'progress') { if (GRAPH.alpha <= .03) { GRAPH.alpha = .04; startLoop(); } drawGraph(); }
  if (name === 'history') loadHistory();
}
$$('nav button').forEach(b => b.addEventListener('click', () => showTab(b.dataset.p)));

/* ---------- Sources & scope ---------- */
const state = { sources: [], folders: [], selected: new Set() };

function visibleSources() {
  const query = ($('#sourcefilter').value || '').trim().toLowerCase();
  const folder = $('#sourcefolder').value;
  return state.sources.filter(s => (!query || (s.source + ' ' + (s.folder || '')).toLowerCase().includes(query))
    && (!folder || s.source.startsWith(folder + '/')));
}
function updateSelection() {
  $('#selectioncount').textContent = state.selected.size + ' selected · ' + visibleSources().length + ' shown';
}
function selectVisible() { visibleSources().forEach(s => state.selected.add(s.source)); renderRail(); }
function clearSources() { state.selected.clear(); renderRail(); }

async function refreshSources() {
  try {
    const j = await api('/api/sources', undefined, 'GET');
    state.sources = Array.isArray(j.sources) ? j.sources : [];
    state.folders = Array.isArray(j.coverage && j.coverage.folders) ? j.coverage.folders : [];
    const folder = $('#sourcefolder'), previous = folder.value;
    folder.replaceChildren(new Option('All folders', ''));
    state.folders.forEach(f => folder.add(new Option(f.folder, f.folder_path || f.folder)));
    if ([...folder.options].some(o => o.value === previous)) folder.value = previous;
    renderRail();
    renderScopeSelect();
  } catch (e) {
    $('#srclist').replaceChildren();
    const p = document.createElement('div');
    p.style.cssText = 'color:var(--dim);font-size:12.5px;padding:6px 2px';
    p.textContent = 'Sources unavailable: ' + errText(e);
    $('#srclist').appendChild(p);
  }
}
function renderRail() {
  const list = $('#srclist'); list.replaceChildren();
  updateSelection();
  if (!state.sources.length) {
    const p = document.createElement('div');
    p.style.cssText = 'color:var(--dim);font-size:12.5px;padding:6px 2px';
    p.textContent = 'No local source PDFs indexed yet. Run the Desktop pipeline.';
    list.appendChild(p);
    return;
  }
  visibleSources().forEach(s => {
    const row = document.createElement('div');
    row.className = 'src' + (state.selected.has(s.source) ? ' sel' : '');
    row.title = [s.source, s.folder ? 'folder: ' + s.folder : '', s.status ? 'status: ' + s.status : '',
      s.pages ? s.pages + ' pages' : ''].filter(Boolean).join(' · ');
    const name = document.createElement('span'); name.textContent = s.source;
    const check = document.createElement('input'); check.type = 'checkbox'; check.checked = state.selected.has(s.source);
    check.setAttribute('aria-label', 'Select ' + s.source);
    check.addEventListener('click', ev => ev.stopPropagation());
    check.addEventListener('change', () => {
      if (check.checked) state.selected.add(s.source); else state.selected.delete(s.source);
      row.classList.toggle('sel', check.checked); updateSelection();
    });
    const meta = document.createElement('i'); meta.textContent = s.status === 'empty' ? 'no text' : (s.chunks ?? 0) + '¶';
    row.append(check, name, meta);
    row.addEventListener('click', ev => {
      if (ev.shiftKey) { openPdfDrawer(s.source); return; }
      if (state.selected.has(s.source)) state.selected.delete(s.source); else state.selected.add(s.source);
      row.classList.toggle('sel');
      check.checked = state.selected.has(s.source); updateSelection();
    });
    list.appendChild(row);
  });
}
function renderScopeSelect() {
  const sel = $('#quizscope'); const prev = sel.value;
  sel.replaceChildren();
  const opt = (v, label) => { const o = document.createElement('option'); o.value = v; o.textContent = label; sel.appendChild(o); };
  opt('Daily Adaptive Mix', 'Daily Adaptive Mix');
  state.folders.forEach(f => opt('folder:' + (f.folder_path || f.folder), 'Folder · ' + f.folder));
  state.sources.slice(0, 80).forEach(s => opt('note:' + s.source, 'Note · ' + s.source));
  if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
}
function scopeSources() {
  const targeted = $('#quizscope').value;
  if (targeted && targeted.startsWith('topic:')) return [...state.selected, targeted];
  if (state.selected.size) return [...state.selected];
  const v = $('#quizscope').value;
  if (v && v !== 'Daily Adaptive Mix') return [v];
  return $('#sourcefolder').value ? ['folder:' + $('#sourcefolder').value] : [];
}
function setTargetedScope(topic) {
  const sel = $('#quizscope');
  let found = [...sel.options].find(o => o.value === 'topic:' + topic);
  if (!found) {
    found = document.createElement('option');
    found.value = 'topic:' + topic; found.textContent = 'Concept · ' + topic;
    sel.insertBefore(found, sel.options[1] || null);
  }
  sel.value = found.value;
}

/* ---------- Uploads ---------- */
async function uploadFiles(list) {
  const ok = [...list].filter(f => /\.(pdf|md|txt)$/i.test(f.name) && f.size <= 15 * 1024 * 1024);
  if (!ok.length) { setStat('No supported PDF/markdown/txt files under the 15 MiB manual-upload limit.'); return; }
  setStat(`Indexing ${ok.length} file${ok.length > 1 ? 's' : ''}…`, true);
  let done = 0;
  for (const f of ok) {
    setStat(`Indexing ${f.name} (${++done}/${ok.length})…`, true);
    const b64 = btoa(await new Promise(res => { const r = new FileReader(); r.onload = () => res(r.result.split(',')[1]); r.readAsDataURL(f); }));
    try { await api('/api/upload', { files: [{ name: f.name, b64 }] }); } catch (e) { console.warn(f.name, e); }
  }
  setStat(`Indexed ${ok.length} files.`);
  refreshSources(); refreshState();
}
function setStat(t, busy) { const el = $('#ingeststat'); el.textContent = t; el.classList.toggle('busy', !!busy); }

/* ---------- State / intelligence ---------- */
async function refreshState() {
  try {
    const j = await api('/api/state', undefined, 'GET');
    const study = j.study || {};
    const pill = $('#activitypill');
    pill.style.display = 'inline-flex';
    pill.textContent = `${num(study.inactive_days)} inactive day(s) · cap ${num(study.daily_cap, 15)}/day`;
    renderTopics(j.stats || []);
    const due = j.due || [];
    const dp = $('#duepill');
    if (due.length) {
      dp.style.display = 'inline-flex';
      $('#duetxt').textContent = `${due.length}/${num(study.daily_cap, 15)} due: ` +
        due.slice(0, 2).map(d => d.topic).join(', ');
    } else dp.style.display = 'none';
    return j;
  } catch (e) {
    $('#topics').replaceChildren();
    $('#topics').appendChild(emptyState('State unavailable', errText(e), refreshState));
  }
}
function renderTopics(stats) {
  const el = $('#topics'); el.replaceChildren();
  if (!stats.length) {
    const p = document.createElement('p');
    p.style.cssText = 'color:var(--dim);font-size:13px';
    p.textContent = 'Take a few quizzes and your mastery map appears here.';
    el.appendChild(p); return;
  }
  stats.forEach(s => {
    const row = document.createElement('div'); row.className = 'trow';
    const name = document.createElement('span'); name.className = 'name'; name.title = s.topic; name.textContent = s.topic;
    const bar = document.createElement('div'); bar.className = 'bar';
    const fill = document.createElement('i');
    const full = num(s.total) > 0 && num(s.pct) >= 80;
    if (full) fill.classList.add('full');
    fill.style.width = (num(s.total) ? num(s.pct) : 0) + '%';
    bar.appendChild(fill);
    const pct = document.createElement('span'); pct.className = 'pct';
    pct.textContent = num(s.total) ? num(s.pct) + '%' : '—';
    row.append(name, bar, pct);
    el.appendChild(row);
  });
}
async function refreshIntelligence() {
  const el = $('#intelligence'); el.replaceChildren();
  let intel = null;
  try { intel = await api('/api/intelligence', undefined, 'GET'); }
  catch (e) {
    const p = document.createElement('p');
    p.textContent = isMissing(e) ? 'Learner intelligence endpoint not available on this server yet.'
      : 'Intelligence unavailable: ' + errText(e);
    el.appendChild(p); return;
  }
  const list = (title, items, cls) => {
    const h = document.createElement('h3'); h.textContent = title; el.appendChild(h);
    const ul = document.createElement('ul');
    (Array.isArray(items) ? items.slice(0, 8) : []).forEach(item => {
      const li = document.createElement('li'); if (cls) li.className = cls;
      li.textContent = fmtIntel(item);
      ul.appendChild(li);
    });
    if (!ul.children.length) { const li = document.createElement('li'); li.textContent = '—'; ul.appendChild(li); }
    el.appendChild(ul);
  };
  const h = document.createElement('h3');
  h.textContent = `Learner intelligence · last ${num(intel.days, 30)} days`;
  el.appendChild(h);
  if (num(intel.observations) === 0) {
    const p = document.createElement('p');
    p.textContent = 'No recorded study sessions in this window yet — nothing observed, so nothing is claimed.';
    el.appendChild(p);
    return;
  }
  list('Strengths', intel.strengths);
  list('Weaknesses', intel.weaknesses);
  list('Recommended', intel.recommended);
  list('Recent errors', intel.errors, 'err');
}
function fmtIntel(item) {
  if (typeof item === 'string') return item;
  if (item && typeof item === 'object') {
    if (item.topic) {
      const attempts = item.attempts === undefined ? '' : ` · ${num(item.attempts)} attempts`;
      return `${item.topic} · ${num(item.pct)}%${attempts}`;
    }
    if (item.tag) return `${item.tag} · ${num(item.count)}×`;
  }
  return String(item);
}

/* ---------- Coverage ---------- */
/* New contract: indexed == processed (status coverage); searchable is separate.
 * Empty/unreadable PDFs are reported as counts and never labeled searchable. */
async function refreshCoverage() {
  const el = $('#coverage'); el.replaceChildren();
  try {
    const j = await api('/api/coverage', undefined, 'GET');
    const folders = (j && j.folders) || [];
    const totalPdfs = num(j && (j.pdfs || j.total_pdfs));
    if (totalPdfs) {
      const sum = document.createElement('p');
      sum.className = 'coverage-summary';
      sum.textContent = `${totalPdfs} PDFs · ${num(j.processed_pct)}% processed (indexed) · ` +
        `${num(j.searchable_pct)}% searchable · ${num(j.empty)} empty · ${num(j.unreadable)} unreadable` +
        (folders.some(f => f.subsection) ? ' · subsection rows are included in their parent total' : '');
      el.appendChild(sum);
    }
    if (!folders.length) { el.appendChild(emptyState('No exported PDF folders found.')); return; }
    folders.forEach(f => {
      const item = document.createElement('div'); item.className = 'coverage-item';
      const pdfs = num(f.pdfs);
      const processed = num(f.indexed_processed, num(f.indexed));
      const searchable = num(f.searchable);
      const emptyCount = num(f.empty);
      const unreadable = num(f.unreadable);
      const concepts = num(f.concepts), mastered = num(f.mastered);

      const head = document.createElement('div'); head.className = 'coverage-head';
      const strong = document.createElement('strong');
      strong.textContent = f.folder + (f.subsection ? ' · sub' : '');
      if (f.subsection) {
        item.title = `${f.folder} is a subsection of ${f.parent || String(f.folder_path || '').split('/')[0]}; its PDFs are included in the parent total.`;
      }
      const meta = document.createElement('span');
      meta.textContent = `${num(f.extracted)}/${pdfs} extracted · ${processed} processed · ${mastered}/${concepts} mastered`;
      head.append(strong, meta);

      const meters = document.createElement('div'); meters.className = 'coverage-meters';
      const meter = (cls, value, label) => {
        const m = document.createElement('div'); m.className = 'meter ' + cls;
        m.title = `${label}: ${value}%`;
        const i = document.createElement('i'); i.style.width = value + '%'; m.appendChild(i);
        return m;
      };
      const ex = pdfs ? Math.round(100 * num(f.extracted) / pdfs) : 0;
      const px = num(f.indexed_pct, pdfs ? Math.round(100 * processed / pdfs) : 0);
      const ms = concepts ? num(f.mastery_pct) : 0;
      meters.append(meter('', ex, 'extracted'), meter('indexed', px, 'processed (indexed)'),
        meter('mastered', ms, 'mastered'));

      /* Searchable: has actual extractable text — separate from empty/unreadable. */
      const searchablePct = num(f.searchable_pct, pdfs ? Math.round(100 * searchable / pdfs) : 0);
      const row = document.createElement('div'); row.className = 'coverage-searchable';
      const label = document.createElement('span');
      label.textContent = `Searchable ${searchable}/${pdfs} · ${searchablePct}%`;
      const bar = document.createElement('div'); bar.className = 'meter searchable';
      bar.title = `searchable: ${searchablePct}%`;
      const fill = document.createElement('i'); fill.style.width = searchablePct + '%';
      bar.appendChild(fill);
      const counts = document.createElement('span'); counts.className = 'coverage-counts';
      counts.textContent = `empty ${emptyCount} · unreadable ${unreadable}`;
      row.append(label, bar, counts);

      item.append(head, meters, row);
      el.appendChild(item);
    });
    const legend = document.createElement('div'); legend.className = 'coverage-legend';
    ['Extracted', 'Processed (indexed)', 'Mastered'].forEach(t => {
      const s = document.createElement('span'); s.textContent = t; legend.appendChild(s);
    });
    el.appendChild(legend);
    if (j && j.explanation) {
      const note = document.createElement('p');
      note.className = 'coverage-note';
      note.textContent = String(j.explanation);
      el.appendChild(note);
    }
  } catch (e) {
    el.appendChild(emptyState('Coverage unavailable', errText(e), refreshCoverage));
  }
}

/* ---------- Studio ---------- */
let studioAction = 'chat';
function studio(action) {
  studioAction = action;
  $$('.studiobtn').forEach(x => x.classList.toggle('active', x.textContent.trim().toLowerCase() === action));
  runStudio();
}
async function runStudio() {
  const q = $('#studioq').value.trim() || (studioAction !== 'chat' ? studioAction : '');
  const out = $('#studioout');
  if (!q) { out.replaceChildren(emptyState('Ask something first', 'Type a question or topic, pick sources in the rail, then run an action.')); return; }
  out.replaceChildren(emptyState('Working…', 'Sending to the studio endpoint.'));
  try {
    const j = await api('/api/studio', { action: studioAction, question: q, sources: scopeSources() });
    out.replaceChildren();
    const box = document.createElement('div'); box.className = 'studio-result';
    richText(box, String(j.answer || ''));
    out.appendChild(box);
    const cites = (j.sources || []).filter(c => c && c.source);
    if (cites.length) out.appendChild(citationChips(cites));
  } catch (e) {
    errorState(out, 'Studio unavailable', e, runStudio);
  }
}
function citationChips(cites) {
  const wrap = document.createElement('div'); wrap.className = 'chips';
  cites.forEach(c => {
    if (!c || !c.source) return;
    wrap.appendChild(sourceAnchor(c.source, c.page,
      'chip', c.source + (c.page ? ' · p.' + c.page : '')));
  });
  return wrap;
}
/* Anchor whose left-click opens the probed drawer; href kept for open-in-new-tab. */
function sourceAnchor(source, page, cls, text) {
  const a = document.createElement('a');
  a.className = cls; a.target = '_blank'; a.rel = 'noopener';
  a.href = '/source?path=' + encodeURIComponent(source) + (page ? '#page=' + num(page, 1) : '');
  a.textContent = text;
  a.addEventListener('click', ev => { ev.preventDefault(); openPdfDrawer(source, page); });
  return a;
}

/* ---------- Tutor ---------- */
const tutor = { session: null, pending: false };
async function askTutor() {
  const input = $('#tutorq');
  const msg = input.value.trim();
  if (!msg || tutor.pending) return;
  const sourceScope = scopeSources(), fingerprint = [...sourceScope].sort().join('|');
  if (tutor.scope !== fingerprint) { tutor.session = null; tutor.scope = fingerprint; }
  input.value = '';
  appendTutor('you', msg);
  tutor.pending = true;
  $('#tutorsend').disabled = true;
  const wait = appendTutor('bot', 'Reading your notes…');
  try {
    const j = await api('/api/tutor/step', {
      session_id: tutor.session || undefined,
      sources: sourceScope,
      message: msg
    });
    if (j.session_id) tutor.session = j.session_id;
    wait.remove();
    const node = appendTutor('bot', j.explanation || '');
    if (j.feedback) {
      const fb = document.createElement('div');
      fb.className = 'explain'; fb.textContent = j.feedback;
      node.appendChild(fb);
    }
    if (j.question) {
      const q = document.createElement('div');
      q.className = 'qq'; q.style.fontSize = '16px'; q.textContent = j.question;
      node.appendChild(q);
      const form = document.createElement('form');
      form.className = 'verify-row';
      const box = document.createElement('input');
      box.type = 'text'; box.maxLength = 400; box.required = true;
      box.placeholder = 'Your short answer…';
      box.setAttribute('aria-label', 'Verification answer');
      const send = document.createElement('button');
      send.className = 'pri'; send.type = 'submit'; send.textContent = 'Check';
      form.append(box, send);
      form.addEventListener('submit', ev => {
        ev.preventDefault();
        const v = box.value.trim();
        if (!v) return;
        // Retire this turn's input so only one active verification box exists.
        form.querySelectorAll('input,button').forEach(el => { el.disabled = true; });
        input.value = v; askTutor();
      });
      node.appendChild(form);
    }
    if (Array.isArray(j.sources) && j.sources.length) {
      node.appendChild(citationChips(j.sources.map(s => typeof s === 'string' ? { source: s } : s)));
    }
    if (!j.explanation && !j.question && !j.feedback) {
      node.textContent = 'The tutor returned an empty step.';
    }
    renderMath(node);
  } catch (e) {
    wait.remove();
    appendTutor('bot', 'Tutor unavailable — ' + errText(e), true);
    const forms = $$('.verify-row');
    if (forms.length) forms[forms.length-1].querySelectorAll('input,button').forEach(el => { el.disabled = false; });
  } finally {
    tutor.pending = false;
    $('#tutorsend').disabled = false;
    input.focus();
  }
}
function appendTutor(cls, text, isError) {
  const d = document.createElement('div');
  d.className = 'msg ' + (cls === 'you' ? 'you' : '');
  if (isError) d.style.color = 'var(--red)';
  richText(d, text);
  $('#tutorchat').appendChild(d);
  $('#tutorchat').scrollTop = 1e9;
  return d;
}

/* ---------- Exam ---------- */
const exam = { id: null, questions: [], deadline: 0, mode: 'practice', index: 0, answers: {}, finished: false, timer: null };
let quizMode = 'practice';

function setMode(m) {
  quizMode = m === 'exam' ? 'exam' : 'practice';
  $$('.modepill').forEach(b => b.classList.toggle('active',
    b.textContent.trim().toLowerCase() === quizMode));
}

async function startQuiz() {
  if (exam.pending) return;
  exam.pending = true;
  stopTimer();
  const card = $('#quizcard');
  card.replaceChildren(emptyState('Building your session…'));
  try {
    const j = await api('/api/exam/start', {
      sources: scopeSources(),
      mode: quizMode,
      count: 25,
      duration_minutes: 45
    });
    exam.id = j.id;
    exam.questions = Array.isArray(j.questions) ? j.questions : [];
    exam.mode = j.mode === 'exam' ? 'exam' : 'practice';
    exam.deadline = parseDeadline(j.deadline);
    exam.index = 0; exam.answers = {}; exam.finished = false;
    if (!exam.questions.length) { card.replaceChildren(emptyState('No questions returned', 'The server returned an empty question set for this scope.')); return; }
    renderExam();
    startTimer();
  } catch (e) {
    card.replaceChildren(emptyState('Could not start session', errText(e), startQuiz));
  } finally {
    exam.pending = false;
  }
}
function parseDeadline(d) {
  if (typeof d === 'number') return d > 1e12 ? d : d * 1000;
  const t = Date.parse(d);
  return Number.isFinite(t) ? t : Date.now() + 45 * 60000;
}
function startTimer() {
  stopTimer();
  exam.timer = setInterval(() => {
    const left = exam.deadline - Date.now();
    const el = $('#examtimer');
    if (!el) { stopTimer(); return; }
    if (left <= 0) { stopTimer(); el.textContent = '00:00'; el.classList.add('warn'); finishExam(true); return; }
    const m = Math.floor(left / 60000), s = Math.floor(left / 1000) % 60;
    el.textContent = String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
    el.classList.toggle('warn', left < 60000);
  }, 500);
}
function stopTimer() { if (exam.timer) { clearInterval(exam.timer); exam.timer = null; } }

function renderExam() {
  const card = $('#quizcard'); card.replaceChildren();
  const wrap = document.createElement('div'); wrap.className = 'qcard';

  const head = document.createElement('div'); head.className = 'qtopic';
  const modeChip = document.createElement('span'); modeChip.className = 'modechip';
  modeChip.textContent = exam.mode === 'exam' ? 'Exam · silent until finish' : 'Practice · instant feedback';
  const timer = document.createElement('span'); timer.id = 'examtimer'; timer.className = 'timer';
  timer.textContent = '--:--';
  const finish = document.createElement('button');
  finish.className = 'ghost'; finish.textContent = 'Finish'; finish.style.marginLeft = 'auto';
  finish.addEventListener('click', () => finishExam(false));
  head.append(modeChip, timer, finish);

  const palette = document.createElement('div');
  palette.className = 'palette'; palette.id = 'palette';

  const q = document.createElement('div');
  q.className = 'qq'; q.id = 'examq';

  const opts = document.createElement('div'); opts.id = 'examopts';

  const nav = document.createElement('div'); nav.className = 'finishbar';
  const prev = document.createElement('button'); prev.className = 'ghost'; prev.textContent = '← Prev';
  const mark = document.createElement('button'); mark.className = 'ghost'; mark.textContent = 'Mark';
  const clear = document.createElement('button'); clear.className = 'ghost'; clear.textContent = 'Clear';
  const next = document.createElement('button'); next.className = 'ghost'; next.textContent = 'Next →';
  prev.addEventListener('click', () => jump(exam.index - 1));
  next.addEventListener('click', () => jump(exam.index + 1));
  mark.addEventListener('click', () => toggleMark());
  clear.addEventListener('click', () => clearAnswer());
  nav.append(prev, mark, clear, next);

  const fb = document.createElement('div'); fb.id = 'examfeedback';

  wrap.append(head, palette, q, opts, fb, nav);
  card.appendChild(wrap);
  renderQuestion();
  renderPalette();
  renderMath(wrap);
}
function renderPalette() {
  const pal = $('#palette'); if (!pal) return;
  pal.replaceChildren();
  exam.questions.forEach((qq, i) => {
    const b = document.createElement('button');
    b.className = 'palbtn'; b.textContent = String(i + 1);
    b.title = 'Question ' + (i + 1);
    const a = exam.answers[qq.id] || {};
    if (a.marked) b.classList.add('marked');
    if (a.choice !== undefined && a.choice !== null) {
      b.classList.add('answered');
      if (a.feedback) b.classList.add(a.feedback.correct ? 'done-good' : 'done-bad');
    }
    if (i === exam.index) b.classList.add('active');
    b.addEventListener('click', () => jump(i));
    pal.appendChild(b);
  });
}
function jump(i) {
  if (i < 0 || i >= exam.questions.length) return;
  exam.index = i;
  renderQuestion(); renderPalette();
}
function currentQ() { return exam.questions[exam.index]; }
function renderQuestion() {
  const q = $('#examq'), opts = $('#examopts'), fb = $('#examfeedback');
  if (!q || !opts || !fb) return;
  const item = currentQ();
  richText(q, (exam.index + 1) + '. ' + (item.question || ''));
  fb.replaceChildren();
  opts.replaceChildren();
  const a = exam.answers[item.id] || {};
  (item.options || []).forEach((text, idx) => {
    const b = document.createElement('button');
    b.className = 'opt';
    if (a.choice === idx) b.classList.add('selected');
    if (a.feedback) {
      if (idx === a.feedback.correct_index) b.classList.add('good');
      else if (a.choice === idx) b.classList.add('badd');
      b.disabled = true;
    }
    b.textContent = String.fromCharCode(65 + idx) + '. ' + text;
    b.addEventListener('click', () => answer(idx));
    opts.appendChild(b);
  });
  if (item.source && item.source.source) {
    const cite = document.createElement('span');
    cite.className = 'citation';
    cite.appendChild(sourceAnchor(item.source.source, item.source.page, '',
      item.source.source + (item.source.page ? ' · p.' + item.source.page : '')));
    opts.appendChild(cite);
  }
  if (a.feedback) showFeedback(a.feedback, a.choice);
  renderMath(opts);
}
function showFeedback(fb, choice) {
  const box = $('#examfeedback'); if (!box) return;
  const d = document.createElement('div');
  d.className = 'explain' + (fb.correct ? '' : ' no');
  let text = fb.correct ? 'Correct.' : (choice === null || choice === undefined ? 'Not answered.' : 'Incorrect.');
  if (fb.explanation) text += ' ' + fb.explanation;
  richText(d, text);
  box.appendChild(d);
}
async function answer(choice) {
  const item = currentQ();
  const prev = exam.answers[item.id] || {};
  const payload = { exam_id: exam.id, question_id: item.id, choice, marked: !!prev.marked };
  const optimistic = { ...prev, choice };
  exam.answers[item.id] = optimistic;
  renderQuestion(); renderPalette();
  try {
    const j = await api('/api/exam/answer', payload);
    if (j.feedback) {
      exam.answers[item.id] = { ...optimistic, feedback: j.feedback };
      renderQuestion(); renderPalette();
    }
    if (j.saved) toast('Answer saved');
  } catch (e) {
    toast('Save failed: ' + errText(e));
  }
}
async function toggleMark() {
  const item = currentQ();
  const prev = exam.answers[item.id] || {};
  const marked = !prev.marked;
  exam.answers[item.id] = { ...prev, marked };
  renderPalette();
  try {
    await api('/api/exam/answer', {
      exam_id: exam.id, question_id: item.id,
      choice: prev.choice === undefined ? null : prev.choice, marked
    });
    toast(marked ? 'Marked for review' : 'Unmarked');
  } catch (e) { toast('Mark failed: ' + errText(e)); }
}
async function clearAnswer() {
  const item = currentQ();
  const prev = exam.answers[item.id] || {};
  exam.answers[item.id] = { choice: null, marked: !!prev.marked };
  renderQuestion(); renderPalette();
  try {
    await api('/api/exam/answer', { exam_id: exam.id, question_id: item.id, choice: null, marked: !!prev.marked });
  } catch (e) { toast('Clear failed: ' + errText(e)); }
}
async function finishExam(auto) {
  if (exam.finished) return;
  if (!auto && !confirm('Finish this session and see analytics?')) return;
  exam.finished = true;
  stopTimer();
  try {
    const j = await api('/api/exam/finish', { exam_id: exam.id });
    renderResults(j);
  } catch (e) {
    exam.finished = false;
    const card = $('#quizcard');
    errorState(card, 'Could not finish session', e, startQuiz);
  }
}
function renderResults(j) {
  const card = $('#quizcard'); card.replaceChildren();
  const wrap = document.createElement('div'); wrap.className = 'qcard';

  const score = document.createElement('div');
  score.className = 'qq';
  score.textContent = `Score ${num(j.score)} / ${num(j.total)}`;
  wrap.appendChild(score);

  const results = Array.isArray(j.results) ? j.results : [];
  if (!results.length) {
    wrap.appendChild(emptyState('No results returned', 'The server returned an empty result set.'));
    card.appendChild(wrap); return;
  }
  results.forEach((r, i) => {
    const box = document.createElement('div'); box.className = 'result';
    const q = document.createElement('div'); q.className = 'q';
    richText(q, (i + 1) + '. ' + String(r.question || ''));
    const meta = document.createElement('div'); meta.className = 'meta';
    const sel = r.selected === null || r.selected === undefined ? 'not answered'
      : (r.options && r.options[r.selected]) || ('choice ' + r.selected);
    const correctText = (r.options && r.options[r.correct_index]) || ('choice ' + r.correct_index);
    richText(meta, `You: ${sel} · Correct: ${correctText}` + (r.error_tag ? ` · tag: ${r.error_tag}` : ''));
    box.append(q, meta);
    if (r.explanation) {
      const ex = document.createElement('div'); ex.className = 'explain';
      richText(ex, String(r.explanation)); box.appendChild(ex);
    }
    if (r.source) {
      const src = typeof r.source === 'string' ? { source: r.source } : r.source;
      if (src.source) box.appendChild(citationChips([src]));
    }
    wrap.appendChild(box);
  });
  const again = document.createElement('button');
  again.className = 'pri'; again.style.marginTop = '16px'; again.textContent = 'New session →';
  again.addEventListener('click', startQuiz);
  wrap.appendChild(again);
  card.appendChild(wrap);
}

/* ---------- History ---------- */
async function loadHistory() {
  const el = $('#histwrap'); el.replaceChildren();
  let hist = [], sessions = null;
  try {
    const j = await api('/api/history', undefined, 'GET');
    hist = Array.isArray(j.history) ? j.history : [];
  } catch (e) {
    errorState(el, 'History unavailable', e, loadHistory); return;
  }
  try {
    const lh = await api('/api/learning/history', undefined, 'GET');
    sessions = Array.isArray(lh.sessions) ? lh.sessions : null;
  } catch (_) { sessions = null; }
  let sessionSafe = true;
  try { if (sessions) JSON.stringify(sessions); } catch (_) { sessionSafe = false; }
  if (!sessionSafe) sessions = null;

  if (!hist.length && (!sessions || !sessions.length)) {
    el.appendChild(emptyState('No study sessions yet',
      'Every question you ask and every quiz you take is logged here by day.'));
    return;
  }
  const fmtDay = d => { const t = Date.parse(d + 'T12:00:00'); return Number.isFinite(t) ? new Date(t).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' }) : d; };
  hist.forEach(d => {
    const day = document.createElement('details'); day.className = 'day'; day.open = hist.length <= 3;
    const sum = document.createElement('summary'); sum.className = 'dayhead';
    const h2 = document.createElement('h2'); h2.textContent = fmtDay(d.date);
    const s = document.createElement('span'); s.className = 'sum';
    const nq = (d.asked || []).length, nz = (d.quizzes || []).length;
    s.textContent = `${nq} question${nq === 1 ? '' : 's'} asked · ${nz} quiz${nz === 1 ? '' : 'zes'} (${num(d.correct)} correct)`;
    sum.append(h2, s);
    day.appendChild(sum);
    (d.asked || []).forEach(a => day.appendChild(hItem('asked', a.question, a.topic, '', a)));
    (d.quizzes || []).forEach(z => {
      const grade = z.grade ? ['', 'Again', 'Hard', 'Good', 'Easy'][z.grade] || 'quiz' : 'quiz';
      const suffix = z.mistake_pattern ? ' · ' + z.mistake_pattern : '';
      day.appendChild(hItem(z.pending ? 'unanswered' : grade, (z.pending ? '' : z.correct ? '✓ ' : '✗ ') + z.question + suffix,
        z.topic, z.pending ? '' : z.correct ? 'ok' : 'no', z));
    });
    el.appendChild(day);
  });
  if (sessions && sessions.length) {
    const sec = document.createElement('details'); sec.className = 'day exam-section'; sec.open = true;
    const sum = document.createElement('summary'); sum.className = 'dayhead';
    const h2 = document.createElement('h2'); h2.textContent = 'Recorded sessions';
    sum.appendChild(h2); sec.appendChild(sum);
    sessions.forEach(s => {
      const data = (s && typeof s === 'object' && s.data && typeof s.data === 'object') ? s.data : {};
      const created = num(s.created);
      const when = created
        ? new Date(created * (created < 1e12 ? 1000 : 1)).toLocaleString(undefined,
          { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
        : '';
      const item = document.createElement('details'); item.className = 'history-session';
      const summary = document.createElement('summary'); summary.className = 'hitem';
      const mark = document.createElement('span'); mark.className = 'mark';
      mark.textContent = String(s.kind || 'session');
      const body = document.createElement('span'); body.className = 'qt';
      const bits = [];
      if (s.kind === 'exam') {
        const res = data.result;
        if (res && res.total !== undefined) bits.push(`score ${num(res.score)}/${num(res.total)}`);
        if (data.mode) bits.push(String(data.mode));
        bits.push(s.finished ? 'finished' : 'in progress — finish to reveal');
      } else if (s.kind === 'studio') {
        if (data.action) bits.push(String(data.action));
        if (data.question) bits.push(String(data.question).slice(0, 70));
      } else if (s.kind === 'tutor') {
        const turns = Array.isArray(data.turns) ? data.turns : [];
        bits.push(`${turns.length} turn${turns.length === 1 ? '' : 's'}`);
        const first = turns[0];
        const preview = first && (first.message || first.question);
        if (preview) bits.push(String(preview).slice(0, 60));
      }
      if (when) bits.push(when);
      body.textContent = bits.filter(Boolean).join(' · ') || String(s.id || 'session');
      body.title = String(s.id || '');
      summary.append(mark, body); item.appendChild(summary);
      if (s.kind === 'studio') {
        const content = document.createElement('div'); richText(content, data.answer || '');
        item.append(content, citationChips(data.sources || []));
      }
      if (s.kind === 'tutor') {
        (data.turns || []).forEach(turn => {
          const content = document.createElement('div'); content.className = 'history-turn';
          richText(content, `${turn.explanation || ''}\n\nVerification: ${turn.question || ''}\n\nYour input: ${turn.answer || ''}\n${turn.feedback || ''}`);
          if (turn.graded_previous) {
            const grade = document.createElement('p'), previous = turn.graded_previous;
            grade.textContent = `${previous.correct ? 'Correct' : 'Needs review'} · ${previous.error_tag || 'No error tag'}`
              + (previous.fsrs ? ` · FSRS stability ${previous.fsrs.stability ?? '—'} days · due ${previous.fsrs.due || '—'}` : '');
            content.appendChild(grade);
          }
          content.appendChild(citationChips(turn.sources || [])); item.appendChild(content);
        });
      }
      sec.appendChild(item);
      const results = data.result && Array.isArray(data.result.results) ? data.result.results : [];
      results.forEach(r => {
        const sub = document.createElement('details'); sub.className = 'history-question';
        const subSummary = document.createElement('summary'); subSummary.className = 'hitem';
        const m2 = document.createElement('span'); m2.className = 'mark';
        m2.textContent = r.correct ? '✓' : '✗';
        const t2 = document.createElement('span'); t2.className = 'qt ' + (r.correct ? 'ok' : 'no');
        const tag = r.error_tag ? ' · ' + r.error_tag : '';
        t2.textContent = String(r.question || '') + tag;
        renderMath(t2);
        subSummary.append(m2, t2); sub.appendChild(subSummary);
        const full = document.createElement('div'); full.className = 'history-full';
        richText(full, r.question || '');
        const options = document.createElement('ol'); options.type = 'A';
        (r.options || []).forEach((option,index) => {
          const line = document.createElement('li'); line.textContent = String(option)
            + (index === r.selected ? ' ← your choice' : '') + (index === r.correct_index ? ' ✓ correct' : '');
          renderMath(line);
          options.appendChild(line);
        });
        full.appendChild(options);
        const solution = document.createElement('div'); richText(solution, r.explanation || ''); full.appendChild(solution);
        const meta = document.createElement('p'); meta.textContent = r.error_tag ? 'Error tag: ' + r.error_tag : 'No error tag';
        if (r.fsrs) meta.textContent += ` · FSRS stability ${r.fsrs.stability_before ?? 'new'} → ${r.fsrs.stability ?? '—'} days · due ${r.fsrs.due || '—'}`;
        full.appendChild(meta);
        if (r.source) full.appendChild(citationChips([r.source]));
        sub.appendChild(full); item.appendChild(sub);
      });
    });
    el.appendChild(sec);
  }
}
function hItem(mark, text, topic, cls, record) {
  const item = document.createElement('details'); item.className = 'history-question';
  const summary = document.createElement('summary'); summary.className = 'hitem';
  const m = document.createElement('span'); m.className = 'mark'; m.textContent = mark;
  const t = document.createElement('span'); t.className = 'qt ' + (cls || ''); t.textContent = text; t.title = text;
  renderMath(t);
  const c = document.createElement('span'); c.className = 'chip'; c.textContent = topic || '';
  summary.append(m, t, c); item.appendChild(summary);
  const full = document.createElement('div'); full.className = 'history-full';
  richText(full, `${record?.question || text}\n\n${record?.answer || ''}\n\n${record?.feedback || ''}`);
  if (record?.source) full.appendChild(citationChips([{source: record.source, page: record.source_page}]));
  item.appendChild(full);
  return item;
}

/* ---------- Knowledge graph (self-contained force engine) ---------- */
const GRAPH = {
  nodes: [], edges: [], pos: new Map(), vel: new Map(),
  transform: { k: 1, x: 0, y: 0 },
  alpha: 0, raf: null, hover: null,
  // Configurable label/collision boxes (no perfect-overlap guarantee).
  box: { padX: 8, padY: 4, lineHeight: 13, maxChars: 16, extra: 10 }
};

async function loadGraph() {
  try {
    const j = await api('/api/graph', undefined, 'GET');
    GRAPH.nodes = (j.nodes || []).map(n => ({ topic: String(n.topic), pct: num(n.pct), total: num(n.total), concept_count: num(n.concept_count, 1) }));
    const known = new Set(GRAPH.nodes.map(n => n.topic));
    GRAPH.edges = (j.edges || []).filter(e => known.has(e.from) && known.has(e.to));
    initGraph();
    return true;
  } catch (e) {
    const hint = $('#graphhint');
    if (hint) hint.textContent = 'Graph unavailable: ' + errText(e);
    return false;
  }
}
function initGraph() {
  const n = GRAPH.nodes.length;
  GRAPH.pos = new Map(); GRAPH.vel = new Map();
  const cx = 0, cy = 0, R = Math.max(80, n * 12);
  GRAPH.nodes.forEach((node, i) => {
    const a = i / Math.max(1, n) * 2 * Math.PI;
    GRAPH.pos.set(node.topic, { x: cx + R * Math.cos(a), y: cy + R * Math.sin(a) });
    GRAPH.vel.set(node.topic, { x: 0, y: 0 });
  });
  GRAPH.alpha = 1;
  startLoop();
}
function nodeRadius(node) { return 8 + Math.min(22, Math.sqrt(node.concept_count || 1) * 3); }
function labelLines(node) {
  const words = node.topic.split(/\s+/);
  const lines = []; let line = '';
  for (const w of words) {
    if ((line + w).trim().length > GRAPH.box.maxChars) { if (line) lines.push(line.trim()); line = w + ' '; }
    else line += w + ' ';
  }
  if (line.trim()) lines.push(line.trim());
  return lines.slice(0, 3);
}
function nodeBox(node) {
  const p = GRAPH.pos.get(node.topic); if (!p) return null;
  const r = nodeRadius(node);
  const lines = labelLines(node);
  const widest = Math.max(...lines.map(l => l.length), 1);
  const w = Math.max(r * 2, widest * 6.6 + GRAPH.box.padX * 2);
  const labelH = lines.length * GRAPH.box.lineHeight + GRAPH.box.padY * 2;
  const top = p.y - r - GRAPH.box.padY;
  const bottom = p.y + r + 2 + labelH;
  return { x1: p.x - w / 2, x2: p.x + w / 2, y1: top, y2: bottom };
}
function stepGraph() {
  const nodes = GRAPH.nodes, pos = GRAPH.pos, vel = GRAPH.vel;
  const a = GRAPH.alpha;
  // Repulsion
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const A = nodes[i], B = nodes[j];
      const pa = pos.get(A.topic), pb = pos.get(B.topic);
      let dx = pa.x - pb.x, dy = pa.y - pb.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 1) { d2 = 1; dx = Math.random() - 0.5; dy = Math.random() - 0.5; }
      const d = Math.sqrt(d2);
      const minD = nodeRadius(A) + nodeRadius(B) + 46;
      const rep = (d < minD ? (minD - d) * 0.09 : 900 / d2) * a;
      const ux = dx / d, uy = dy / d;
      vel.get(A.topic).x += ux * rep; vel.get(A.topic).y += uy * rep;
      vel.get(B.topic).x -= ux * rep; vel.get(B.topic).y -= uy * rep;
    }
  }
  // Springs
  GRAPH.edges.forEach(e => {
    const pa = pos.get(e.from), pb = pos.get(e.to);
    if (!pa || !pb) return;
    const rest = 96;
    const dx = pb.x - pa.x, dy = pb.y - pa.y;
    const d = Math.max(1, Math.hypot(dx, dy));
    const f = (d - rest) * 0.02 * a;
    const ux = dx / d, uy = dy / d;
    vel.get(e.from).x += ux * f; vel.get(e.from).y += uy * f;
    vel.get(e.to).x -= ux * f; vel.get(e.to).y -= uy * f;
  });
  // Centering + integrate
  nodes.forEach(nd => {
    const p = pos.get(nd.topic), v = vel.get(nd.topic);
    v.x += -p.x * 0.006 * a; v.y += -p.y * 0.006 * a;
    v.x *= 0.86; v.y *= 0.86;
    p.x += v.x; p.y += v.y;
  });
  // Collision pass on bounding boxes (best-effort overlap reduction)
  for (let pass = 0; pass < 2; pass++) {
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const ba = nodeBox(nodes[i]), bb = nodeBox(nodes[j]);
        if (!ba || !bb) continue;
        const ox = Math.min(ba.x2, bb.x2) - Math.max(ba.x1, bb.x1);
        const oy = Math.min(ba.y2, bb.y2) - Math.max(ba.y1, bb.y1);
        if (ox > 0 && oy > 0) {
          const pa = pos.get(nodes[i].topic), pb = pos.get(nodes[j].topic);
          if (ox < oy) {
            const push = (ox / 2 + 0.5) * 0.6 * a;
            const dir = pa.x < pb.x ? -1 : 1;
            pa.x += push * dir; pb.x -= push * dir;
          } else {
            const push = (oy / 2 + 0.5) * 0.6 * a;
            const dir = pa.y < pb.y ? -1 : 1;
            pa.y += push * dir; pb.y -= push * dir;
          }
        }
      }
    }
  }
  GRAPH.alpha = Math.max(0.03, GRAPH.alpha * 0.98);
}
function startLoop() {
  if (GRAPH.raf) return;
  const tick = () => {
    GRAPH.raf = null;
    if (GRAPH.alpha > 0.03) { stepGraph(); drawGraph(); GRAPH.raf = requestAnimationFrame(tick); }
    else {
      // Final alpha-independent rectangular separation: cooling must not freeze
      // overlapping labels in place. Positions remain pannable/zoomable.
      for (let pass = 0; pass < 100; pass++) {
        let overlap = false;
        for (let i = 0; i < GRAPH.nodes.length; i++) for (let j = i + 1; j < GRAPH.nodes.length; j++) {
          const a = nodeBox(GRAPH.nodes[i]), b = nodeBox(GRAPH.nodes[j]);
          if (!a || !b) continue;
          const ox = Math.min(a.x2,b.x2) - Math.max(a.x1,b.x1);
          const oy = Math.min(a.y2,b.y2) - Math.max(a.y1,b.y1);
          if (ox <= 0 || oy <= 0) continue;
          overlap = true;
          const p = GRAPH.pos.get(GRAPH.nodes[i].topic), q = GRAPH.pos.get(GRAPH.nodes[j].topic);
          if (ox < oy) { const shift = (ox + 3) / 2, sign = p.x <= q.x ? -1 : 1; p.x += sign*shift; q.x -= sign*shift; }
          else { const shift = (oy + 3) / 2, sign = p.y <= q.y ? -1 : 1; p.y += sign*shift; q.y -= sign*shift; }
        }
        if (!overlap) break;
      }
      const boxes = GRAPH.nodes.map(nodeBox).filter(Boolean), canvas = $('#gcanvas');
      if (boxes.length && canvas.clientWidth) {
        const x1 = Math.min(...boxes.map(b=>b.x1)), x2 = Math.max(...boxes.map(b=>b.x2));
        const y1 = Math.min(...boxes.map(b=>b.y1)), y2 = Math.max(...boxes.map(b=>b.y2));
        const scale = Math.min(1, (canvas.clientWidth-35)/(x2-x1+1), (canvas.clientHeight-35)/(y2-y1+1));
        GRAPH.transform = {k:Math.max(.2,scale),x:-(x1+x2)/2*scale,y:-(y1+y2)/2*scale};
      }
      drawGraph();
    }
  };
  GRAPH.raf = requestAnimationFrame(tick);
}
function drawGraph() {
  const cv = $('#gcanvas'); if (!cv || !cv.clientWidth) return;
  const ctx = cv.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  if (cv.width !== Math.round(cv.clientWidth * dpr) || cv.height !== Math.round(cv.clientHeight * dpr)) {
    cv.width = Math.round(cv.clientWidth * dpr);
    cv.height = Math.round(cv.clientHeight * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const W = cv.clientWidth, H = cv.clientHeight;
  ctx.clearRect(0, 0, W, H);
  const { k, x, y } = GRAPH.transform;
  const sx = wx => wx * k + x + W / 2;
  const sy = wy => wy * k + y + H / 2;

  ctx.strokeStyle = '#435573'; ctx.lineWidth = 1;
  GRAPH.edges.forEach(e => {
    const pa = GRAPH.pos.get(e.from), pb = GRAPH.pos.get(e.to);
    if (!pa || !pb) return;
    ctx.beginPath(); ctx.moveTo(sx(pa.x), sy(pa.y)); ctx.lineTo(sx(pb.x), sy(pb.y)); ctx.stroke();
  });
  ctx.textAlign = 'center';
  GRAPH.nodes.forEach(nd => {
    const p = GRAPH.pos.get(nd.topic); if (!p) return;
    const r = nodeRadius(nd) * Math.min(k, 1.6);
    const X = sx(p.x), Y = sy(p.y);
    ctx.beginPath(); ctx.arc(X, Y, r, 0, Math.PI * 2);
    ctx.fillStyle = nd.total === 0 ? '#61728f' : nd.pct >= 80 ? '#56c998' : nd.pct >= 50 ? '#edbc60' : '#f57f93';
    ctx.fill();
    ctx.strokeStyle = GRAPH.hover === nd.topic ? '#a9cbff' : '#7185a2';
    ctx.lineWidth = GRAPH.hover === nd.topic ? 2 : 1;
    ctx.stroke();
    ctx.fillStyle = '#e7eefb';
    const labelScale = Math.min(k,1.6);
    ctx.font = (11*labelScale) + 'px Inter, system-ui, sans-serif';
    labelLines(nd).forEach((line, li) => {
      ctx.fillText(line, X, Y + r + 13*labelScale + li * GRAPH.box.lineHeight*labelScale);
    });
  });
}
function graphHit(ev) {
  const cv = $('#gcanvas'); if (!cv) return null;
  const rect = cv.getBoundingClientRect();
  const { k, x, y } = GRAPH.transform;
  const mx = ev.clientX - rect.left - rect.width / 2;
  const my = ev.clientY - rect.top - rect.height / 2;
  const wx = (mx - x) / k, wy = (my - y) / k;
  for (let i = GRAPH.nodes.length - 1; i >= 0; i--) {
    const nd = GRAPH.nodes[i];
    const b = nodeBox(nd);
    if (b && wx >= b.x1 && wx <= b.x2 && wy >= b.y1 && wy <= b.y2) return nd;
  }
  return null;
}
function bindGraph() {
  const cv = $('#gcanvas'); if (!cv) return;
  let dragging = false, moved = 0, last = null;
  cv.addEventListener('pointerdown', ev => {
    dragging = true; moved = 0; last = { x: ev.clientX, y: ev.clientY };
    cv.setPointerCapture(ev.pointerId);
  });
  cv.addEventListener('pointermove', ev => {
    if (dragging && last) {
      const dx = ev.clientX - last.x, dy = ev.clientY - last.y;
      moved += Math.abs(dx) + Math.abs(dy);
      GRAPH.transform.x += dx; GRAPH.transform.y += dy;
      last = { x: ev.clientX, y: ev.clientY };
      drawGraph();
    } else {
      const hit = graphHit(ev);
      const h = hit ? hit.topic : null;
      if (h !== GRAPH.hover) { GRAPH.hover = h; cv.style.cursor = h ? 'pointer' : 'grab'; drawGraph(); }
    }
  });
  cv.addEventListener('pointerup', ev => {
    dragging = false; last = null;
    if (moved < 5) {
      const hit = graphHit(ev);
      if (hit) launchScope(hit.topic);
    }
  });
  cv.addEventListener('pointerleave', () => { if (GRAPH.hover) { GRAPH.hover = null; drawGraph(); } });
  cv.addEventListener('wheel', ev => {
    ev.preventDefault();
    const factor = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
    const nk = Math.min(4, Math.max(0.3, GRAPH.transform.k * factor));
    const ratio = nk / GRAPH.transform.k;
    GRAPH.transform.x *= ratio; GRAPH.transform.y *= ratio;
    GRAPH.transform.k = nk;
    drawGraph();
  }, { passive: false });
  window.addEventListener('resize', drawGraph);
}
function launchScope(topic) {
  setTargetedScope(topic);
  showTab('quiz');
  toast('Scope set to ' + topic);
}

/* ---------- PDF drawer (split screen, no arbitrary HTML) ----------
 * The backend resolves only NOTES_DIR-rooted or basename-only sources, so
 * folder-qualified names are probed with cheap Range GETs (no full download). */
async function probeSource(path) {
  const seen = [];
  const add = p => { if (p && !seen.includes(p)) seen.push(p); };
  add(path);
  if (path && !path.startsWith('/')) {
    // Backend prefers the compressed tree for category-relative logical paths.
    add('compressed/' + path);
    add('original/' + path);
    const base = path.split('/').pop();
    if (base && base !== path) add(base);
  }
  let sawNotPdf = false;
  for (const candidate of seen) {
    try {
      const r = await fetch('/source?path=' + encodeURIComponent(candidate),
        { headers: { Range: 'bytes=0-16' } });
      if (r.body && r.body.cancel) r.body.cancel();
      if (r.ok) return { path: candidate };
      if (r.status === 415) sawNotPdf = true;
    } catch (_) { /* try the next candidate */ }
  }
  return sawNotPdf ? { notPdf: true } : null;
}
async function openPdfDrawer(path, page) {
  if (!path) {
    const pick = [...state.selected][0] || (state.sources[0] && state.sources[0].source);
    if (!pick) { toast('No source selected — pick a source in the rail first.'); return; }
    path = pick;
  }
  const hit = await probeSource(String(path));
  if (!hit) { toast('Source not found on disk: ' + path); return; }
  if (hit.notPdf) { toast('Only PDF sources can be opened in the viewer.'); return; }
  const url = '/source?path=' + encodeURIComponent(hit.path) + (page ? '#page=' + num(page, 1) : '');
  $('#pdftitle').textContent = path + (page ? ' · p.' + page : '');
  $('#pdfframe').src = url;
  $('#pdfdrawer').classList.add('open');
  $('#pdfdrawer').setAttribute('aria-hidden', 'false');
}
function closePdfDrawer() {
  $('#pdfdrawer').classList.remove('open');
  $('#pdfdrawer').setAttribute('aria-hidden', 'true');
  $('#pdfframe').src = 'about:blank';
}

/* ---------- KaTeX (optional, local vendor primary) ---------- */
function richText(root, text) {
  // Fenced code is built with text nodes; no model-controlled HTML is executed.
  root.replaceChildren();
  const value = String(text ?? '');
  const pattern = /```([^\n]*)\n([\s\S]*?)```/g;
  let last = 0, match;
  const plain = s => { if (!s) return; const node = document.createElement('div'); node.className = 'rich-plain'; node.style.whiteSpace = 'pre-wrap'; node.textContent = s; root.appendChild(node); };
  while ((match = pattern.exec(value)) !== null) {
    plain(value.slice(last, match.index));
    const pre = document.createElement('pre'), code = document.createElement('code');
    const language = match[1].trim().replace(/[^a-zA-Z0-9_-]/g, '');
    if (language) code.className = 'language-' + language;
    code.textContent = match[2]; pre.appendChild(code); root.appendChild(pre);
    if (window.hljs) { try { window.hljs.highlightElement(code); } catch (_) {} }
    last = pattern.lastIndex;
  }
  plain(value.slice(last)); renderMath(root);
}

function renderMath(root) {
  if (typeof window.renderMathInElement !== 'function') return;
  try {
    window.renderMathInElement(root, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '$', right: '$', display: false },
        { left: '\\[', right: '\\]', display: true },
        { left: '\\(', right: '\\)', display: false }
      ],
      throwOnError: false
    });
  } catch (_) { /* math rendering is optional */ }
}

/* ---------- Boot (GET only — no Gemini call) ---------- */
async function boot() {
  $('#sourcefilter').addEventListener('input', renderRail);
  $('#sourcefolder').addEventListener('change', renderRail);
  $$('.modepill').forEach(b => b.classList.toggle('active', b.textContent.trim().toLowerCase() === quizMode));
  $('#folder').addEventListener('change', e => uploadFiles(e.target.files));
  $('#files').addEventListener('change', e => uploadFiles(e.target.files));
  $('#tutorq').addEventListener('keydown', e => { if (e.key === 'Enter') askTutor(); });
  $('#studioq').addEventListener('keydown', e => { if (e.key === 'Enter') runStudio(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closePdfDrawer(); });

  $('#quizcard').replaceChildren(emptyState('Ready when you are',
    'Choose a scope, pick sources in the rail (Shift-click opens the PDF), and start a session.'));

  bindGraph();
  await Promise.allSettled([
    refreshState(), refreshSources(), refreshCoverage(), loadGraph(), refreshIntelligence()
  ]);
  const chunks = state.sources.reduce((n, s) => n + num(s.chunks), 0);
  setStat(`${chunks} chunks ready across ${state.sources.length} source(s).`);
}

/* Expose handlers used by inline markup. */
window.app = {
  showTab, setMode, startQuiz, askTutor, runStudio, studio,
  openPdfDrawer, closePdfDrawer, uploadFiles
  , selectVisible, clearSources
};

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
