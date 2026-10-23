// Ask Claude: a chat in the corner that explains the dense parts of a page. A
// round button (capcom's mascot, who waves now and then) opens a small panel; the
// small "Explain" marks on dense cards open it already asking about that card.
//
// Answers come from the user's own `claude` CLI, which `capcom serve` starts per
// question (POST /api/ask, capcom/report/ask.py), streamed as they are written.
// Off until the user turns it on once in the panel; the choice is kept in this
// browser. What a question sends: the page's name, the part's label and its
// numbers and labels in capcom's own words (partText below leaves out evidence
// rows, session links and titles, notes the user wrote and pasted config), the
// earlier turns of this chat and the question. Never a session's text.

import { h, s } from './lib.js';
import { freshToken } from './marks.js';

const meta = (name) => { const m = document.querySelector(`meta[name="${name}"]`); return m ? m.getAttribute('content') : null; };
export const ASK_OFF = meta('capcom-ask') === 'off';

const KEY = 'capcom-ask-on';
const MAX_PART = 2800;
const MAX_TURNS = 8;
const MAX_QUESTION = 600;
const EXPLAIN = 'Explain this part to me very shortly and simply.';
export const NOTICE = 'Ask Claude sends the numbers and labels on this part of the page to Claude through your own claude CLI. No text from your logs is sent.';

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
// words. Left out: controls, charts' drawings (their aria-labels, numbers and
// labels, are kept), evidence rows (they quote the logs), links to sessions
// (their titles come from the logs), the user's own notes, pasted config.
const DROP = [
  '.askmark', 'button', 'form', 'pre', 'svg', 'input', 'textarea', 'select', 'script', 'style',
  '.facts', '.evidence', 'details.astable', 'table', '.usernote', '.markform', '.tip', '[hidden]',
  '.detail .tags > .note', '.spikelist', '.ylab', '.wx', '.histlabs', '.thrlab', '.legend',
].join(',');
const BLOCK = /^(DIV|P|LI|UL|OL|H1|H2|H3|H4|SECTION|ASIDE|HEADER|FOOTER|DETAILS|SUMMARY|FIGURE|BR|DL|DT|DD)$/;

export function partText(el, extra = []) {
  const lines = [];
  for (const root of [el, ...extra]) {
    if (!root) continue;
    for (const img of root.querySelectorAll('[role="img"][aria-label]')) {
      if (!img.closest(DROP.replace(/\[hidden\],?/, ''))) lines.push(img.getAttribute('aria-label'));
    }
    const copy = root.cloneNode(true);
    for (const a of copy.querySelectorAll('a[href^="/session/"], a[href^="/session?"]')) a.replaceWith(document.createTextNode('a session'));
    for (const x of copy.querySelectorAll(DROP)) x.remove();
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

// The dense parts of each page that get a mark: where (the route), which element,
// where the mark goes inside it, and its label.
const PARTS = [
  { route: 'checks', sel: 'section.card.check', anchor: 'h2',
    label: (el) => { const kind = el.closest('details.rollup'); const k = kind ? headingText(kind.querySelector('summary b')) : ''; const t = headingText(el.querySelector('h2')); return k && k !== t ? `${k}, in ${t}` : t; } },
  { route: 'spikes', sel: 'section.card.detail', anchor: 'h2', label: (el) => headingText(el.querySelector('h2')) },
  { route: 'overview', sel: 'aside.card', anchor: 'h2', label: (el) => headingText(el.querySelector('h2')) },
  { route: 'overview', sel: 'section.card', anchor: '.label', has: 'a[href="/limits"]', label: (el) => `${headingText(el.querySelector('.label'))} forecast` },
  { route: 'limits', sel: 'section.card', anchor: '.split > h2', label: (el) => `${headingText(el.querySelector('h2'))} forecast` },
  { route: 'spend', sel: 'section.card.grow2b', anchor: 'h2', label: (el) => headingText(el.querySelector('h2')) },
  { route: 'session', sel: 'section.card.banner', anchor: '.msg > strong', label: () => 'Cost check of this session' },
  { route: 'changes', arg: true, sel: 'header.head.top', anchor: '.chips', label: (el) => `Did it work: ${headingText(el.querySelector('h1'))}`,
    extra: (el) => [el.parentElement && el.parentElement.querySelector('.tiles3')] },
];

function decorate() {
  const r = route();
  const main = document.getElementById('main');
  if (!main) return;
  for (const p of PARTS) {
    if (p.route !== r.name || (p.arg && !r.arg)) continue;
    for (const el of main.querySelectorAll(p.sel)) {
      if (p.has && !el.querySelector(p.has)) continue;
      const anchor = el.querySelector(p.anchor);
      if (!anchor || anchor.querySelector('.askmark')) continue;
      const label = p.label(el) || 'This part';
      anchor.append(h('button.askmark', {
        type: 'button', title: 'Ask Claude to explain this', 'aria-label': `Ask Claude to explain: ${label}`,
        onclick: (e) => { e.preventDefault(); e.stopPropagation(); explain(el, p, label); },
      }, mascot(18, true), h('span', { 'aria-hidden': 'true', text: 'Explain' })));
    }
  }
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
  msgs: [], // {role: 'user'|'assistant', text, part?, suggestions?, error?, done?}
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
  const bubble = h('button.askbtn', { type: 'button', 'aria-label': 'Ask Claude', 'aria-expanded': 'false', 'aria-controls': 'askpanel', title: 'Ask Claude about this page', onclick: () => (state.open ? closePanel() : openPanel()) }, mascot(44));
  const log = h('div.asklog', { role: 'log', 'aria-live': 'polite', 'aria-label': 'Conversation' });
  const input = h('textarea.askinput', { rows: 1, maxlength: MAX_QUESTION, placeholder: 'Ask about this page…', 'aria-label': 'Your question' });
  const sendBtn = h('button.asksend', { type: 'submit', 'aria-label': 'Send' }, icon(ICON.send));
  const form = h('form.askform', null, input, sendBtn);
  const foot = h('div.askfoot', null, form,
    h('p.asknote', null, 'Haiku, through your claude CLI. ', h('button.asklink', { type: 'button', text: 'Turn off', onclick: turnOff })));
  const panel = h('section.askpanel', { id: 'askpanel', role: 'dialog', 'aria-label': 'Ask Claude', hidden: true },
    h('header.askhead', null, mascot(34),
      h('div.asktitle', null, h('strong', { text: 'Ask Claude' }), h('span', { text: 'Short answers about your numbers' })),
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
  state.msgs = [];
  state.topic = null;
  draw();
}

function turnOn() {
  setEnabled(true);
  state.on = true;
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
    const about = state.pending && state.pending.topic ? state.pending.topic.label : null;
    ui.log.replaceChildren(h('div.askoptin', null,
      mascot(56),
      h('strong', { text: 'Turn on Ask Claude?' }),
      h('p', { text: NOTICE }),
      h('p.note', { text: 'It runs your claude CLI with the Haiku model, so each question counts toward your Claude usage. You can turn it off again under the chat.' }),
      about ? h('p.note', null, 'Then Claude explains ', h('b', { text: about }), '.') : null,
      h('div.btnrow', null,
        h('button.btn.primary', { type: 'button', text: 'Turn on', onclick: turnOn }),
        h('button.btn', { type: 'button', text: 'Not now', onclick: () => { state.pending = null; closePanel(); } }))));
    return;
  }
  if (!state.msgs.length) {
    ui.log.replaceChildren(h('div.askhello', null,
      h('div.askrow.bot', null, avatar(), h('div.askmsg.bot', { text: 'Hi! I explain what you see here in a few simple words. Tap “Explain” on a card, or ask me anything about this page.' })),
      suggestions(starters(), true)));
    return;
  }
  const last = state.msgs.length - 1;
  ui.log.replaceChildren(...state.msgs.map((m, i) => message(m, i === last)));
  ui.log.scrollTop = ui.log.scrollHeight;
}

const avatar = () => h('span.askav', null, mascot(26, true));

function starters() {
  const r = route().name;
  const first = { checks: 'Which change saves the most?', spikes: 'Why did my spend jump?', limits: 'Will I hit my limit?', spend: 'Where does my money go?', changes: 'Did my changes work?' }[r];
  return [first || 'What does this page show?', 'What should I look at first?'];
}

function message(m, last) {
  if (m.role === 'user') {
    return h('div.askrow.me', null, h('div.askmsg.me', null,
      m.part ? h('span.askpart', { title: m.part }, 'About: ', h('b', { text: m.part })) : null,
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

const typing = () => h('span.asktyping', { 'aria-label': 'Claude is writing' }, h('i'), h('i'), h('i'));
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
  const history = state.msgs.filter((m) => m.text && !m.error).slice(-MAX_TURNS).map((m) => ({ role: m.role, text: m.text.slice(0, 1500) }));
  if (again) state.msgs.pop();
  else state.msgs.push({ role: 'user', text: question, part: topic ? topic.label : null });
  const reply = { role: 'assistant', text: '', done: false };
  state.msgs.push(reply);
  const ctrl = new AbortController();
  state.ctrl = ctrl;
  draw();
  const t = state.topic || pageContext();
  const body = { page: (state.topic && state.topic.page) || pageName(), part: t ? { label: t.label, text: t.text } : null, history: again ? history.slice(0, -1) : history, question };
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
  draw();
  decorate();
  new MutationObserver((records) => {
    if (records.some((r) => !ui.root.contains(r.target))) decorate();
  }).observe(document.body, { childList: true, subtree: true });
  window.addEventListener('pagehide', () => { if (state.ctrl) state.ctrl.abort(); });
  if (!reducedMotion()) {
    setTimeout(() => { const svg = ui.bubble.querySelector('.mascot'); svg.classList.add('waving'); setTimeout(() => svg.classList.remove('waving'), 2000); }, 1500);
  }
  scheduleWave();
  window.capcomAsk = { ask, open: openPanel, close: closePanel, state }; // for tests and the console
}
