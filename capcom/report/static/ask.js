// Ask Claude: a chat in the corner that explains the dense parts of a page. A
// round button (capcom's mascot, who waves now and then) opens a small panel; the
// small "Explain" marks on dense cards open it already asking about that card, and
// the "Ask Roger" button beside any text the user selects asks about that text.
//
// Answers come from the user's own `claude` CLI, which `capcom serve` starts per
// question (POST /api/ask, capcom/report/ask.py), streamed as they are written.
// Off until the user turns it on once in the panel; the choice is kept in this
// browser. What a question sends: the page's name, the part's label and its
// numbers and labels in capcom's own words (partText below keeps a table's
// numbers but leaves out evidence rows, session links and titles, notes the user
// wrote and pasted config), the
// earlier turns of this chat and the question. A selection sends its text without
// the private pieces it crosses (PRIVATE below: only what kind they were), where it
// sits and its card's text as partText builds it. Never a session's text.

import { h, s } from './lib.js';
import { freshToken } from './marks.js';

const meta = (name) => { const m = document.querySelector(`meta[name="${name}"]`); return m ? m.getAttribute('content') : null; };
export const ASK_OFF = meta('capcom-ask') === 'off';

const KEY = 'capcom-ask-on';
const MAX_PART = 2800;
const MAX_SELECTION = 1000;
const MAX_WHERE = 200;
const MAX_TURNS = 8;
const MAX_QUESTION = 600;
// the question an Explain mark asks (capcom/report/ask.py, EXPLAIN, adds how to answer it)
const EXPLAIN = 'What does this show, and what stands out?';
const EXPLAIN_SELECTION = 'Explain the text I selected, very shortly and simply.';
export const NOTICE = 'Ask Claude sends the numbers and labels on this part of the page, or the text you select, to Claude through your own claude CLI. No text from your logs is sent.';
// The mascot's name ("Roger that", on the radio): the greeting, the panel's
// title, the opt-in's heading and the round button's label say it. The opt-in
// notice's text and the sidebar say Claude, since Claude is what answers.
export const MASCOT_NAME = 'Roger';
const named = (yes, no) => (MASCOT_NAME ? yes : no);
// The first message of every chat, written by capcom (instant, and nothing sent).
export const GREETING = `Hi${named(`, I’m ${MASCOT_NAME}`, '')}! I explain what you see here in a few short, plain words. Tap “Explain” on a card, select any text and tap “${named(`Ask ${MASCOT_NAME}`, 'Ask Claude')}”, or ask me anything about this page.`;

// The sidebar's promise (app.js draws it): true with Ask Claude off, and saying
// what goes out while it is on in this browser.
export const NET_OFF = 'Local and read-only. No network.';
export const NET_ON = 'Local and read-only. Only Ask Claude uses the network, through your claude CLI.';
export function networkNote() { return !ASK_OFF && enabled() ? NET_ON : NET_OFF; }
function syncNote() { for (const el of document.querySelectorAll('.netnote')) el.textContent = networkNote(); }

const PAGE_NAMES = { overview: 'Overview', spend: 'Spend', sessions: 'Sessions', session: 'Session', day: 'Day', spikes: 'Spikes', checks: 'Checks', changes: 'Did it work?', limits: 'Limits' };

function enabled() { try { return localStorage.getItem(KEY) === '1'; } catch (e) { return false; } }
function setEnabled(on) { try { if (on) localStorage.setItem(KEY, '1'); else localStorage.removeItem(KEY); } catch (e) { /* storage off: asks again next visit */ } }
const reducedMotion = () => !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

function route() {
  const parts = location.pathname.replace(/^\/+|\/+$/g, '').split('/');
  return { name: parts[0] || 'overview', arg: parts[1] || null };
}
const pageName = () => PAGE_NAMES[route().name] || 'Overview';

// ------------------------------------------------------------------ the mascot

// capcom's mascot: a little capsule in mission-control headphones (CAPCOM is the
// one voice that talks to the crew). Its right arm waves. `mini`: the head only.
export function mascot(size, mini = false) {
  const ink = { class: 'mz-ink' };
  const parts = [];
  if (!mini) {
    parts.push(
      s('path', { class: 'mz-body-stroke', d: 'M17 42 Q11 46 12.5 51', 'stroke-width': 5, 'stroke-linecap': 'round', fill: 'none' }),
      s('g', { class: 'mz-arm' },
        s('path', { class: 'mz-body-stroke', d: 'M46 43 Q56 41 58.5 33', 'stroke-width': 5, 'stroke-linecap': 'round', fill: 'none' }),
        s('circle', { class: 'mz-body', cx: 59, cy: 30.5, r: 4.3 })));
  }
  parts.push(
    s('rect', { class: 'mz-body', x: 14, y: 16, width: 36, height: 38, rx: 16 }),
    s('rect', { class: 'mz-face', x: 19, y: 23, width: 26, height: 20, rx: 10 }),
    s('g', { class: 'mz-eyes' },
      s('ellipse', { ...ink, cx: 27, cy: 32, rx: 2.3, ry: 3 }),
      s('ellipse', { ...ink, cx: 37, cy: 32, rx: 2.3, ry: 3 }),
      s('circle', { class: 'mz-glint', cx: 27.8, cy: 30.8, r: 0.8 }),
      s('circle', { class: 'mz-glint', cx: 37.8, cy: 30.8, r: 0.8 })),
    s('circle', { class: 'mz-cheek', cx: 23.4, cy: 37, r: 2 }),
    s('circle', { class: 'mz-cheek', cx: 40.6, cy: 37, r: 2 }),
    s('path', { class: 'mz-ink-stroke', d: 'M29.6 37.6 Q32 40 34.4 37.6', 'stroke-width': 1.6, 'stroke-linecap': 'round', fill: 'none' }),
    s('path', { class: 'mz-band-stroke', d: 'M13 30 A19 19 0 0 1 51 30', 'stroke-width': 3, 'stroke-linecap': 'round', fill: 'none' }),
    s('rect', { class: 'mz-band', x: 9.5, y: 25, width: 7, height: 11, rx: 3.5 }),
    s('rect', { class: 'mz-band', x: 47.5, y: 25, width: 7, height: 11, rx: 3.5 }),
    s('path', { class: 'mz-band-stroke', d: 'M12.5 35 Q13 45 22.5 45.5', 'stroke-width': 2, 'stroke-linecap': 'round', fill: 'none' }),
    s('circle', { class: 'mz-band', cx: 24, cy: 45.5, r: 2.2 }));
  return s('svg', { class: mini ? 'mascot mini' : 'mascot', width: size, height: size, viewBox: mini ? '8 8 48 46' : '3 5 62 54', 'aria-hidden': 'true' }, parts);
}

// ------------------------------------------------------------------ what a part says

// What a part shows, as lines of text: its numbers and labels in capcom's own
// words, tables as lines (tableLines). Left out: controls, charts' drawings (their aria-labels, numbers and
// labels, are kept), evidence rows (they quote the logs), links to sessions
// (their titles come from the logs), the user's own notes, pasted config.
const DROP = [
  '.askmark', 'button', 'form', 'pre', 'svg', 'input', 'textarea', 'select', 'script', 'style',
  '.facts', '.evidence', 'details.astable', 'table', '.usernote', '.markform', '.tip', '[hidden]',
  '.detail .tags > .note', '.spikelist', '.spikerow .meta', '.lab .what', '.ylab', '.wx', '.histlabs', '.thrlab', '.legend',
].join(',');

// The private pieces of a page, by what they are: text from the logs (session
// titles, a subagent's spawn description, evidence rows) or the user's own (notes,
// pasted config, what is typed in a field). A selection never sends them, only
// their kind (capcom/report/ask.py, LEFT_OUT, takes only these names).
const PRIVATE = [
  ['a[href^="/session/"], a[href^="/session?"], .spikelist .meta, .spikerow .meta, .detail .tags > .note', 'session names'],
  ['.lab .what', 'agent names'],
  ['.facts, .evidence', 'evidence rows'],
  ['.usernote, .markform, input, textarea, select, [contenteditable]', 'your notes'],
  ['pre', 'pasted config'],
];
// a session's page has its title as its heading
const sessionTitle = (el) => (route().name === 'session' ? el.closest('#main h1') : null);
function privateOf(el) {
  const t = sessionTitle(el);
  if (t) return [t, 'session names'];
  for (const [sel, kind] of PRIVATE) { const p = el.closest(sel); if (p) return [p, kind]; }
  return null;
}
const BLOCK = /^(DIV|P|LI|UL|OL|H1|H2|H3|H4|SECTION|ASIDE|HEADER|FOOTER|DETAILS|SUMMARY|FIGURE|BR|DL|DT|DD)$/;

// A table, as lines: how many rows, then each row (the first ten) as its first
// cell, then the other cells after their column headers, such as "a
// session: Active 2h 10m, Peak context 312k, Cost $42.10". A cell holding a
// private piece (a session's title and link, a spawn description, evidence, a
// note) is said only by its kind: "a session", "an agent", "[left out]".
const MAX_ROWS = 10;
const CELL_DROP = '.tip, [hidden], script, style, svg, .askmark';
const PRIVATE_CELL = [
  ['a[href^="/session/"], a[href^="/session?"], .spikelist .meta, .spikerow .meta, .detail .tags > .note', 'a session'],
  ['.lab .what', 'an agent'],
  ['.facts, .evidence, .usernote, .markform, input, textarea, select, [contenteditable], pre', '[left out]'],
];
function cellText(c) {
  for (const [sel, say] of PRIVATE_CELL) if (c.matches(sel) || c.querySelector(sel)) return say;
  const x = c.cloneNode(true);
  for (const d of x.querySelectorAll(CELL_DROP)) d.remove();
  return x.textContent.replace(/\s+/g, ' ').trim();
}

export function tableLines(table) {
  const cells = (tr) => [...tr.children].filter((c) => c.matches('th, td'));
  const headRow = table.querySelector('thead tr');
  const head = headRow ? cells(headRow).map(cellText) : [];
  const rows = [...table.querySelectorAll('tr')].filter((tr) => tr !== headRow && !tr.closest('thead'));
  if (!rows.length) return [];
  const out = [`A table of ${rows.length} row${rows.length === 1 ? '' : 's'}${rows.length > MAX_ROWS ? `, the first ${MAX_ROWS}` : ''}:`];
  for (const tr of rows.slice(0, MAX_ROWS)) {
    const vals = cells(tr).map(cellText);
    const name = vals[0] || '';
    const rest = vals.slice(1).map((v, i) => (!v ? '' : head[i + 1] ? `${head[i + 1]} ${v}` : v)).filter(Boolean);
    out.push(rest.length ? `${name || 'a row'}: ${rest.join(', ')}` : name);
  }
  return out.filter(Boolean);
}

// Any container that partText drops, whatever is inside it (a table in an
// evidence block goes with it).
const DROP_BOX = DROP.split(',').filter((x) => x !== 'table' && x !== 'details.astable').join(',');

export function partText(el, extra = []) {
  const lines = [];
  for (const root of [el, ...extra]) {
    if (!root) continue;
    for (const img of root.querySelectorAll('[role="img"][aria-label]')) {
      if (!img.closest(DROP.replace(/\[hidden\],?/, ''))) lines.push(img.getAttribute('aria-label'));
    }
    const copy = root.cloneNode(true);
    // tables first, as lines of capcom's numbers (their private cells said by kind)
    for (const t of copy.querySelectorAll('table')) {
      const box = t.closest('details.astable') || t;
      if (box.parentElement && box.parentElement.closest(DROP_BOX)) continue; // dropped with its container
      const div = document.createElement('div');
      for (const l of tableLines(t)) { const p = document.createElement('p'); p.textContent = l; div.append(p); }
      box.replaceWith(div);
    }
    for (const a of copy.querySelectorAll('a[href^="/session/"], a[href^="/session?"]')) a.replaceWith(document.createTextNode('a session'));
    for (const x of copy.querySelectorAll(DROP)) x.remove();
    if (route().name === 'session') for (const x of copy.querySelectorAll('h1')) x.remove();
    let line = '';
    const flush = () => { const t = line.replace(/\s+/g, ' ').trim(); if (t) lines.push(t); line = ''; };
    const walk = (node) => {
      for (const c of node.childNodes) {
        if (c.nodeType === 3) line += c.data;
        else if (c.nodeType === 1) {
          const block = BLOCK.test(c.tagName) || /\b(note|label|big|para|chip|tag|save|excess|plus|rest|t|mid)\b/.test(c.className || '');
          if (block) flush();
          walk(c);
          if (block) flush(); else line += ' ';
        }
      }
    };
    walk(copy);
    flush();
  }
  const seen = new Set();
  const out = lines.filter((l) => (seen.has(l) ? false : (seen.add(l), true))).join('\n');
  return out.length > MAX_PART ? out.slice(0, MAX_PART - 1) + '…' : out;
}

const textOf = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
const headingText = (el) => { if (!el) return ''; const c = el.cloneNode(true); for (const m of c.querySelectorAll('.askmark')) m.remove(); return textOf(c); };

// The dense parts of each page that get a mark: where (the route; `arg`: true
// only on a page with an argument, false only on one without), which element,
// where the mark goes inside it, and its label (the part's own heading). A part
// whose text has no numbers beyond its label gets no mark (`worth`).
const PARTS = [
  { route: 'checks', sel: 'section.card.check', anchor: 'h2',
    label: (el) => { const kind = el.closest('details.rollup'); const k = kind ? headingText(kind.querySelector('summary b')) : ''; const t = headingText(el.querySelector('h2')); return k && k !== t ? `${k}, in ${t}` : t; } },
  { route: 'spikes', sel: 'section.card.detail', anchor: 'h2', label: (el) => headingText(el.querySelector('h2')) },
  { route: 'overview', sel: 'aside.card', anchor: 'h2', label: (el) => headingText(el.querySelector('h2')) },
  { route: 'overview', sel: 'section.card', anchor: '.label', has: 'a[href="/limits"]', label: (el) => headingText(el.querySelector('.label')) },
  // the Limits page: each window's card; a window's own page: each card under a heading
  { route: 'limits', arg: false, sel: 'section.card', anchor: ':scope > .split > h2', label: (el) => headingText(el.querySelector(':scope > .split > h2')) },
  // (a card with two lists, such as By project and By agent type, says both)
  { route: 'limits', arg: true, sel: 'section.card', anchor: ':scope > .split > h2, :scope > h2',
    label: (el) => [...el.querySelectorAll(':scope > .split > h2, :scope > h2')].map(headingText).filter(Boolean)
      .map((t, i) => (i ? t[0].toLowerCase() + t.slice(1) : t)).join(' and ') },
  { route: 'spend', sel: 'section.card.grow2b', anchor: 'h2', label: (el) => headingText(el.querySelector('h2')) },
  { route: 'spend', sel: 'section.card', has: 'table.data', anchor: ':scope > .split > h2', label: (el) => headingText(el.querySelector(':scope > .split > h2')) },
  { route: 'session', sel: 'details.calfold', anchor: 'summary > h2', label: () => 'Cost check of this session' },
  { route: 'changes', arg: true, sel: 'header.head.top', anchor: '.chips', label: (el) => `Did it work: ${headingText(el.querySelector('h1'))}`,
    extra: (el) => [el.parentElement && el.parentElement.querySelector('.tiles3')] },
];

const unworthy = new WeakMap(); // a part -> its text length when it had nothing to explain
function decorate() {
  const r = route();
  const main = document.getElementById('main');
  if (!main) return;
  for (const p of PARTS) {
    if (p.route !== r.name || (p.arg === true && !r.arg) || (p.arg === false && r.arg)) continue;
    for (const el of main.querySelectorAll(p.sel)) {
      if (p.has && !el.querySelector(p.has)) continue;
      const anchor = el.querySelector(p.anchor);
      if (!anchor || anchor.querySelector('.askmark') || el.querySelector('.askmark')) continue;
      // a part judged not worth a mark is judged again only once its text changes
      const size = el.textContent.length;
      if (unworthy.get(el) === size) continue;
      const label = p.label(el) || 'This part';
      if (!worth(partText(el, p.extra ? p.extra(el) : []), label)) { unworthy.set(el, size); continue; }
      anchor.append(h('button.askmark', {
        type: 'button', title: named(`Ask ${MASCOT_NAME} to explain this`, 'Ask Claude to explain this'),
        'aria-label': `${named(`Ask ${MASCOT_NAME}`, 'Ask Claude')} to explain: ${label}`,
        onclick: (e) => { e.preventDefault(); e.stopPropagation(); explain(el, p, label); },
      }, mascot(18, true), h('span', { 'aria-hidden': 'true', text: 'Explain' })));
    }
  }
}

// Whether a part's text says something beyond its label: a number somewhere.
export function worth(text, label = '') {
  return text.split('\n').some((l) => l !== label && /\d/.test(l) && !(label && label.includes(l)));
}

// The page's headline numbers, for a question asked without a mark.
function pageContext() {
  const r = route();
  const main = document.getElementById('main');
  if (!main) return null;
  const lines = [];
  if (r.name !== 'session') {
    const h1 = main.querySelector('h1');
    if (h1) lines.push(headingText(h1));
    const sub = main.querySelector('.head .sub');
    if (sub) lines.push(textOf(sub));
  }
  for (const tile of [...main.querySelectorAll('.totalbox, .card.tight')].slice(0, 8)) {
    if (tile.matches('a') || tile.closest('a')) continue;
    const t = partText(tile);
    if (t) lines.push(t.replace(/\n/g, ': '));
  }
  const text = lines.join('\n').slice(0, 1500);
  return text ? { label: `${pageName()} page`, text } : null;
}

// ------------------------------------------------------------------ state

const state = {
  on: enabled(),
  open: false,
  msgs: [], // {role: 'user'|'assistant', text, part?, suggestions?, error?, done?, hello?}
  topic: null, // {page, label, text}: the part the chat is about, sent with each question
  pending: null, // a question waiting for the opt-in
  ctrl: null, // the answer being streamed: its AbortController
};
let ui = null;
let token = null;

function explain(el, p, label) {
  const topic = { page: pageName(), label, text: partText(el, p.extra ? p.extra(el) : []) };
  openPanel();
  ask(EXPLAIN, topic);
}

// ------------------------------------------------------------------ the panel

const ICON = {
  close: 'M6 6l12 12M18 6L6 18',
  fresh: 'M12 5v14M5 12h14',
  send: 'M5 12h13M13 6l6 6-6 6',
  stop: 'M8 8h8v8H8z',
};
const icon = (d, size = 18) => s('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' }, s('path', { d }));

function build() {
  const bubble = h('button.askbtn', { type: 'button', 'aria-label': named(`Ask ${MASCOT_NAME}`, 'Ask Claude'), 'aria-expanded': 'false', 'aria-controls': 'askpanel', title: named(`Ask ${MASCOT_NAME} about this page`, 'Ask Claude about this page'), onclick: () => (state.open ? closePanel() : openPanel()) }, mascot(44));
  const log = h('div.asklog', { role: 'log', 'aria-live': 'polite', 'aria-label': 'Conversation' });
  const input = h('textarea.askinput', { rows: 1, maxlength: MAX_QUESTION, placeholder: 'Ask about this page…', 'aria-label': 'Your question' });
  const sendBtn = h('button.asksend', { type: 'submit', 'aria-label': 'Send' }, icon(ICON.send));
  const form = h('form.askform', null, input, sendBtn);
  const foot = h('div.askfoot', null, form,
    h('p.asknote', null, 'Haiku, through your claude CLI. ', h('button.asklink', { type: 'button', text: 'Turn off', onclick: turnOff })));
  const title = named(`Ask ${MASCOT_NAME}`, 'Ask Claude');
  const panel = h('section.askpanel', { id: 'askpanel', role: 'dialog', 'aria-label': title, hidden: true },
    h('header.askhead', null, mascot(34),
      h('div.asktitle', null, h('strong', { text: title }), h('span', { text: 'Short answers about your numbers' })),
      h('button.askicon', { type: 'button', 'aria-label': 'New chat', title: 'New chat', onclick: newChat }, icon(ICON.fresh)),
      h('button.askicon', { type: 'button', 'aria-label': 'Close', title: 'Close', onclick: () => closePanel() }, icon(ICON.close))),
    log, foot);
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (state.ctrl) { state.ctrl.abort(); return; }
    const q = input.value.trim();
    if (!q) return;
    input.value = '';
    grow();
    ask(q);
  });
  const grow = () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 96) + 'px'; };
  input.addEventListener('input', grow);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); } });
  panel.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); closePanel(); } });
  const root = h('div.ask', { id: 'ask', 'data-layout-ignore': '' }, panel, bubble);
  document.body.append(root);
  ui = { root, bubble, panel, log, input, sendBtn, foot };
}

function openPanel() {
  if (!ui) return;
  state.open = true;
  ui.panel.hidden = false;
  ui.bubble.setAttribute('aria-expanded', 'true');
  ui.root.classList.add('open');
  draw();
  const target = state.on ? ui.input : ui.panel.querySelector('.askoptin .btn.primary');
  if (target) target.focus({ preventScroll: true });
}

function closePanel() {
  if (!ui) return;
  state.open = false;
  ui.panel.hidden = true;
  ui.bubble.setAttribute('aria-expanded', 'false');
  ui.root.classList.remove('open');
  ui.bubble.focus({ preventScroll: true });
}

function newChat() {
  if (state.ctrl) state.ctrl.abort();
  state.msgs = [];
  state.topic = null;
  draw();
  ui.input.focus();
}

function turnOff() {
  if (state.ctrl) state.ctrl.abort();
  setEnabled(false);
  state.on = false;
  syncNote();
  state.msgs = [];
  state.topic = null;
  draw();
}

function turnOn() {
  setEnabled(true);
  state.on = true;
  syncNote();
  const p = state.pending;
  state.pending = null;
  draw();
  ui.input.focus({ preventScroll: true });
  if (p) ask(p.question, p.topic);
}

// Draw the conversation (or the opt-in notice); the answer being streamed is
// written into its bubble directly (see `ask`).
function draw() {
  if (!ui) return;
  ui.foot.hidden = !state.on;
  ui.sendBtn.replaceChildren(icon(state.ctrl ? ICON.stop : ICON.send));
  ui.sendBtn.setAttribute('aria-label', state.ctrl ? 'Stop' : 'Send');
  if (!state.on) {
    const topic = state.pending && state.pending.topic;
    const about = topic ? (topic.selection != null ? 'the text you selected' : topic.label) : null;
    ui.log.replaceChildren(h('div.askoptin', null,
      mascot(56),
      h('strong', { text: `Turn on ${named(`Ask ${MASCOT_NAME}`, 'Ask Claude')}?` }),
      h('p', { text: NOTICE }),
      h('p.note', { text: 'It runs your claude CLI with the Haiku model, so each question counts toward your Claude usage. You can turn it off again under the chat.' }),
      about ? h('p.note', null, `Then ${named(MASCOT_NAME, 'Claude')} explains `, h('b', { text: about }), '.') : null,
      h('div.btnrow', null,
        h('button.btn.primary', { type: 'button', text: 'Turn on', onclick: turnOn }),
        h('button.btn', { type: 'button', text: 'Not now', onclick: () => { state.pending = null; closePanel(); } }))));
    return;
  }
  greet();
  const last = state.msgs.length - 1;
  ui.log.replaceChildren(...state.msgs.map((m, i) => message(m, i === last)));
  ui.log.scrollTop = ui.log.scrollHeight;
}

const avatar = () => h('span.askav', null, mascot(26, true));

// A chat starts with the greeting, once: on a fresh panel, after the opt-in
// (before the question that waited for it) and after New chat.
function greet() {
  if (state.on && !(state.msgs.length && state.msgs[0].hello)) state.msgs.unshift({ role: 'assistant', text: GREETING, done: true, hello: true });
}

function starters() {
  const r = route().name;
  const first = { checks: 'Which change saves the most?', spikes: 'Why did my spend jump?', limits: 'Will I hit my limit?', spend: 'Where does my money go?', changes: 'Did my changes work?' }[r];
  return [first || 'What does this page show?', 'What should I look at first?'];
}

function message(m, last) {
  if (m.hello) {
    return h('div.askhello', null, h('div.askrow.bot', null, avatar(), h('div.askmsg.bot', { text: m.text })),
      last ? suggestions(starters(), true) : null);
  }
  if (m.role === 'user') {
    return h('div.askrow.me', null, h('div.askmsg.me', null,
      m.part ? h('span.askpart', { title: m.part }, 'About: ', h('b', { text: m.part })) : null,
      m.quote ? h('q.askquote', { text: m.quote }) : null,
      m.leftOut && m.leftOut.length ? h('span.askleft', { text: `(${listed(m.leftOut)} left out)` }) : null,
      h('span', { text: m.text })));
  }
  const body = h('div', { class: m.error ? 'askmsg bot err' : 'askmsg bot' });
  if (m.text) body.textContent = tidy(m.text);
  else if (!m.done) body.append(typing());
  if (m.error) body.append(m.text ? h('br') : null, h('span', { text: m.error }));
  const row = h('div.askrow.bot', null, avatar(), body);
  m.el = body;
  if (!last || !m.done) return row;
  const next = m.error ? [m.retry ? 'Try again' : null].filter(Boolean) : (m.suggestions || []);
  return next.length ? h('div', null, row, suggestions(next, false, m)) : row;
}

const listed = (xs) => (xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`);
const typing = () => h('span.asktyping', { 'aria-label': `${named(MASCOT_NAME, 'Claude')} is writing` }, h('i'), h('i'), h('i'));
const tidy = (t) => t.replace(/\*\*/g, '').trim();

function suggestions(list, start, m) {
  return h('div.asksugg', { role: 'group', 'aria-label': 'Suggested replies' }, list.map((q) => h('button.askchip', {
    type: 'button', text: q,
    onclick: () => { if (q === 'Try again' && m && m.retry) ask(m.retry.question, m.retry.topic, true); else ask(q); },
  })));
}

// ------------------------------------------------------------------ asking

async function post(body, signal) {
  if (token === null) token = meta('capcom-token') || '';
  const go = () => fetch('/api/ask', {
    method: 'POST', signal, credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/x-ndjson', 'X-Capcom-Token': token },
    body: JSON.stringify(body),
  });
  let r = await go();
  if (r.status === 403) {
    let data = null;
    try { data = await r.clone().json(); } catch (e) { /* not JSON */ }
    if (data && data.error === 'bad_token') {
      const fresh = await freshToken(); // capcom serve restarted: its token is new
      if (fresh && fresh !== token) { token = fresh; r = await go(); }
    }
  }
  return r;
}

// Ask a question (with a part, from a mark: a new topic). `again`: a retry of the
// last question, which is already in the chat.
export async function ask(question, topic = null, again = false) {
  if (!ui) return;
  question = String(question || '').slice(0, MAX_QUESTION);
  if (!state.on) {
    state.pending = { question, topic };
    openPanel();
    return;
  }
  if (state.ctrl) state.ctrl.abort();
  if (topic) state.topic = topic;
  greet();
  const history = state.msgs.filter((m) => m.text && !m.error && !m.hello).slice(-MAX_TURNS).map((m) => ({ role: m.role, text: m.text.slice(0, 1500) }));
  if (again) state.msgs.pop();
  else {
    const picked = topic && topic.selection != null;
    state.msgs.push({ role: 'user', text: question, part: topic ? (picked ? topic.where : topic.label) : null,
      quote: picked ? topic.selection : null, leftOut: picked ? topic.leftOut : null });
  }
  const reply = { role: 'assistant', text: '', done: false };
  state.msgs.push(reply);
  const ctrl = new AbortController();
  state.ctrl = ctrl;
  draw();
  const t = state.topic || pageContext();
  const body = { page: (state.topic && state.topic.page) || pageName(), part: t ? { label: t.label, text: t.text } : null, history: again ? history.slice(0, -1) : history, question };
  if (t && t.selection != null) Object.assign(body, { selection: t.selection, where: t.where, left_out: t.leftOut });
  const fail = (msg) => { reply.error = msg; reply.retry = { question, topic }; };
  try {
    const r = await post(body, ctrl.signal);
    if (!r.ok) {
      let data = null;
      try { data = await r.json(); } catch (e) { /* not JSON */ }
      fail(r.status === 405 ? 'Ask Claude is turned off on this capcom serve (--no-ask).'
        : (data && data.message) || `capcom serve answered ${r.status}.`);
    } else {
      await read(r, reply);
    }
  } catch (e) {
    if (e && e.name === 'AbortError') { reply.error = reply.text ? null : 'Stopped.'; } else fail('capcom serve is not answering. Start it again, then retry.');
  } finally {
    if (state.ctrl === ctrl) state.ctrl = null;
    if (reply.error && reply.error !== 'Stopped.' && !reply.retry) reply.retry = { question, topic };
    reply.done = true;
    draw();
  }
}

async function read(r, reply) {
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (value) buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i);
      buf = buf.slice(i + 1);
      if (line.trim()) handle(JSON.parse(line), reply);
    }
    if (done) break;
  }
}

function handle(ev, reply) {
  if (ev.type === 'text') {
    reply.text += ev.text;
    if (reply.el && reply.el.isConnected) {
      const log = ui.log;
      const near = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
      reply.el.textContent = tidy(reply.text);
      if (near) log.scrollTop = log.scrollHeight;
    }
  } else if (ev.type === 'done') {
    reply.suggestions = (ev.suggestions || []).slice(0, 3);
  } else if (ev.type === 'error') {
    reply.error = ev.message || 'Claude could not answer.';
  }
}

// ------------------------------------------------------------------ a selection

// What the user selected, as text: what they see of it, line by line, with each
// private piece it crosses replaced by "[left out]" and named in `leftOut`.
const SILENT = '#ask, .askmark, button.copy, .mark > button, script, style, noscript, [hidden], .tip';
const LEFT = '[left out]';

function blockOf(el) {
  for (let x = el; x && x !== document.body; x = x.parentElement) {
    const d = getComputedStyle(x).display;
    if (d !== 'inline' && d !== 'contents') return x;
  }
  return document.body;
}

// seen on the page: not hidden, and not in a closed <details> (laid out, not painted)
const shown = (el) => (el.checkVisibility ? el.checkVisibility({ visibilityProperty: true }) : el.getClientRects().length > 0);

export function pickedText(range) {
  const top = range.commonAncestorContainer;
  const base = top.nodeType === 1 ? top : top.parentNode;
  const walker = document.createTreeWalker(base, NodeFilter.SHOW_TEXT);
  const lines = [];
  const leftOut = [];
  let line = '';
  let block = null;
  let hidden = null; // the private element last left out, to say it once
  const flush = () => { const t = line.replace(/\s+/g, ' ').trim(); if (t) lines.push(t); line = ''; };
  let seen = 0;
  let size = 0; // past the limit, the rest would be cut anyway
  for (let t = walker.nextNode(); t && seen < 20000 && size <= MAX_SELECTION + 200; t = walker.nextNode(), seen++) {
    if (!range.intersectsNode(t)) continue;
    const el = t.parentElement;
    if (!el || el.closest(SILENT) || !shown(el)) continue;
    const b = blockOf(el);
    if (b !== block) { flush(); block = b; }
    const p = privateOf(el);
    if (p) {
      if (!leftOut.includes(p[1])) leftOut.push(p[1]);
      if (hidden !== p[0]) { line += ` ${LEFT} `; hidden = p[0]; }
      continue;
    }
    hidden = null;
    const from = t === range.startContainer ? range.startOffset : 0;
    const to = t === range.endContainer ? range.endOffset : t.data.length;
    line += t.data.slice(from, to);
    size += to - from;
  }
  flush();
  let text = lines.join('\n').replace(/\[left out\](?:\s*\[left out\])+/g, LEFT);
  if (text.length > MAX_SELECTION) text = text.slice(0, MAX_SELECTION - 1) + '…';
  const kinds = PRIVATE.map(([, k]) => k).filter((k) => leftOut.includes(k));
  return { text: text === LEFT ? '' : text, leftOut: kinds };
}

// Where a selection sits: the page's name, then the headings of the cards and
// sections around it, outermost first (never a private one, such as a session's title).
const HEADS = [':scope > summary b', ':scope > h1', ':scope > h2', ':scope > h3', ':scope > .split > h2', ':scope > .split > h3', ':scope > .chead h2',
  ':scope > .titles > h1', ':scope > header h2', ':scope > .label', ':scope > .tline > .t', ':scope > summary'];
const nodeEl = (n) => (n && n.nodeType === 1 ? n : n && n.parentElement);

function headings(el) {
  const bits = [];
  for (let x = el; x && x !== document.body; x = x.parentElement) {
    if (x.id === 'main') break;
    if (x.id === 'sidebar') { bits.unshift('Sidebar'); break; }
    for (const q of HEADS) {
      const hd = x.querySelector(q);
      if (!hd) continue;
      if (!privateOf(hd)) {
        const t = headingText(hd).slice(0, 80);
        if (t && !bits.includes(t)) bits.unshift(t);
      }
      break;
    }
  }
  return bits;
}

function whereOf(el) {
  let out = [pageName(), ...headings(el)].join(' › ');
  if (out.length > MAX_WHERE) out = out.slice(0, MAX_WHERE - 1) + '…';
  return out;
}

// The card or section a selection sits in: its text, as a mark would send it.
const CARD = 'section.card, aside.card, .card, header.head, details, section, aside';
function contextOf(el) {
  const main = document.getElementById('main');
  const card = el && main && main.contains(el) && el !== main ? el.closest(CARD) : null;
  if (card && main.contains(card)) {
    const label = headings(card).join(' › ').slice(0, 150);
    return { label: label || `${pageName()} page`, text: partText(card) };
  }
  return pageContext();
}

export function selectionTopic(range) {
  const el = nodeEl(range.commonAncestorContainer);
  const picked = pickedText(range);
  const ctx = contextOf(el);
  return {
    page: pageName(), label: ctx ? ctx.label : `${pageName()} page`, text: ctx ? ctx.text : '',
    selection: picked.text, where: whereOf(el), leftOut: picked.leftOut,
  };
}

// The "Ask Roger" button beside a selection: shown once the selection settles
// (a mouse button let go, or a pause while the keyboard or touch handles move
// it), placed under its end (above its start when there is no room), moved as the
// page scrolls, and gone when the selection is cleared or on Escape. Never for
// text in the chat itself or in a field.
const pick = { btn: null, range: null, timer: 0, down: false, touch: false, pressing: false };

function selectedRange() {
  const sel = document.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount || !sel.getRangeAt(0).toString().trim()) return null;
  for (const n of [sel.anchorNode, sel.focusNode]) {
    const el = nodeEl(n);
    if (!el || el.closest('#ask, input, textarea, select, [contenteditable]')) return null;
  }
  const active = document.activeElement;
  if (active && active.matches('input, textarea, select, [contenteditable]')) return null;
  return sel.getRangeAt(0).cloneRange();
}

function hidePick() {
  clearTimeout(pick.timer);
  pick.range = null;
  if (pick.btn) pick.btn.hidden = true;
}

function placePick() {
  const b = pick.btn;
  if (!b || !pick.range) return;
  const rects = [...pick.range.getClientRects()].filter((r) => r.width > 0 || r.height > 0);
  const vh = window.innerHeight, vw = document.documentElement.clientWidth;
  if (!rects.length) { hidePick(); return; }
  const first = rects[0], last = rects[rects.length - 1];
  // off screen: out of the way until it scrolls back
  b.style.visibility = last.bottom < 0 || first.top > vh ? 'hidden' : '';
  const w = b.offsetWidth, ht = b.offsetHeight;
  const gap = pick.touch ? 30 : 8; // on touch, below the selection's handles
  let top = last.bottom + gap;
  if (top + ht > vh - 8 && first.top - gap - ht >= 8) top = first.top - (pick.touch ? 8 : gap) - ht;
  const left = Math.max(8, Math.min(last.right - w / 2, vw - w - 8));
  b.style.top = `${Math.round(top)}px`;
  b.style.left = `${Math.round(left)}px`;
}

function showPick() {
  const r = selectedRange();
  if (!r) { hidePick(); return; }
  pick.range = r;
  pick.btn.hidden = false;
  placePick();
}

function askSelection() {
  const r = pick.range;
  hidePick();
  if (!r) return;
  openPanel();
  ask(EXPLAIN_SELECTION, selectionTopic(r));
}

function buildPick() {
  const b = h('button.asksel', { type: 'button', hidden: true, title: named(`Ask ${MASCOT_NAME} about the text you selected`, 'Ask Claude about the text you selected'), 'aria-label': named(`Ask ${MASCOT_NAME} about the selected text`, 'Ask Claude about the selected text') },
    mascot(18, true), h('span', { text: named(`Ask ${MASCOT_NAME}`, 'Ask Claude') }));
  // keep the selection: a press on the button must not clear it
  const hold = (e) => { e.preventDefault(); pick.pressing = true; };
  b.addEventListener('pointerdown', hold);
  b.addEventListener('mousedown', (e) => e.preventDefault());
  b.addEventListener('pointercancel', () => { pick.pressing = false; });
  b.addEventListener('click', (e) => { e.preventDefault(); pick.pressing = false; askSelection(); });
  ui.root.append(b);
  pick.btn = b;
  const later = (ms) => { clearTimeout(pick.timer); pick.timer = setTimeout(showPick, ms); };
  document.addEventListener('selectionchange', () => {
    if (pick.pressing) return;
    const sel = document.getSelection();
    if (!sel || sel.isCollapsed) { hidePick(); return; }
    if (pick.down) return; // a mouse still dragging: wait for it to let go
    later(pick.touch ? 350 : 250);
  });
  document.addEventListener('pointerdown', (e) => {
    if (b.contains(e.target)) return;
    pick.touch = e.pointerType !== 'mouse';
    pick.down = e.pointerType === 'mouse' && e.button === 0;
    if (pick.down) hidePick();
  }, true);
  document.addEventListener('pointerup', (e) => {
    if (pick.pressing && !b.contains(e.target)) pick.pressing = false; // pressed, then let go elsewhere
    if (!pick.down || b.contains(e.target)) return;
    pick.down = false;
    later(10);
  }, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !b.hidden) hidePick(); });
  const follow = () => { if (!b.hidden) requestAnimationFrame(placePick); };
  window.addEventListener('scroll', follow, { capture: true, passive: true });
  window.addEventListener('resize', follow, { passive: true });
}

// ------------------------------------------------------------------ the wave

function scheduleWave() {
  if (reducedMotion()) return;
  setTimeout(() => {
    const svg = ui && ui.bubble.querySelector('.mascot');
    if (svg && !state.open && !document.hidden && !reducedMotion()) {
      svg.classList.add('waving');
      setTimeout(() => svg.classList.remove('waving'), 2000);
    }
    scheduleWave();
  }, 9000 + Math.random() * 9000);
}

// ------------------------------------------------------------------ start

if (!ASK_OFF) {
  build();
  buildPick();
  draw();
  decorate();
  syncNote();
  // turned on or off in another tab: follow it
  window.addEventListener('storage', (e) => { if (e.key === KEY) { state.on = enabled(); syncNote(); draw(); } });
  new MutationObserver((records) => {
    if (records.some((r) => !ui.root.contains(r.target))) decorate();
  }).observe(document.body, { childList: true, subtree: true });
  window.addEventListener('pagehide', () => { if (state.ctrl) state.ctrl.abort(); });
  if (!reducedMotion()) {
    setTimeout(() => { const svg = ui.bubble.querySelector('.mascot'); svg.classList.add('waving'); setTimeout(() => svg.classList.remove('waving'), 2000); }, 1500);
  }
  scheduleWave();
  window.capcomAsk = { ask, open: openPanel, close: closePanel, state, pickedText, selectionTopic }; // for tests and the console
}
