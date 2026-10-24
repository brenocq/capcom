// Shared helpers: DOM building (text only, never HTML from data), formatting,
// fetching the API, the tooltip.

import { getProgressive } from './loading.js';

const SVGNS = 'http://www.w3.org/2000/svg';

// h('div.card', {style: '...'}, child, 'text', ...): an element. Children that
// are strings become text nodes, so data from the logs is never parsed as HTML.
export function h(spec, attrs, ...children) {
  const [tag, ...classes] = spec.split('.');
  const el = document.createElement(tag || 'div');
  if (classes.length) el.className = classes.join(' ');
  setAttrs(el, attrs);
  append(el, children);
  return el;
}

export function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVGNS, tag);
  setAttrs(el, attrs);
  append(el, children);
  return el;
}

function setAttrs(el, attrs) {
  if (!attrs) return;
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'text') el.textContent = v;
    else el.setAttribute(k, v === true ? '' : String(v));
  }
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

// ------------------------------------------------------------------ formatting

export function usd(x, digits) {
  const d = digits ?? (Math.abs(x) >= 1000 ? 0 : 2);
  return '$' + x.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function usdShort(x) {
  if (x >= 1000) return '$' + (x / 1000).toFixed(x >= 10000 ? 0 : 1) + 'k';
  if (x >= 100) return '$' + Math.round(x);
  if (x >= 10) return '$' + x.toFixed(0);
  return '$' + x.toFixed(x >= 1 ? 1 : 2);
}

export function tokens(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e4) return Math.round(n / 1e3) + 'k';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}

export function pct(x, digits = 0) {
  if (x > 0 && x < 0.01 && digits === 0) return '<1%';
  return (x * 100).toFixed(digits) + '%';
}

export function duration(seconds) {
  const m = Math.round(seconds / 60);
  if (m < 60) return m + ' min';
  const hrs = Math.floor(m / 60), rest = m % 60;
  if (hrs < 48) return rest ? `${hrs} h ${String(rest).padStart(2, '0')}` : `${hrs} h`;
  return Math.round(hrs / 24) + ' days';
}

const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const MONTHS_LONG = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
  'September', 'October', 'November', 'December'];

export const two = (n) => String(n).padStart(2, '0');
export const hm = (d) => `${two(d.getHours())}:${two(d.getMinutes())}`;
export const monthDay = (d) => `${MONTHS[d.getMonth()]} ${d.getDate()}`;
export const longDate = (d) => `${DAYS[d.getDay()]}, ${MONTHS_LONG[d.getMonth()]} ${d.getDate()}`;
export const dayName = (d) => DAYS[d.getDay()];
export function stamp(d) {
  const now = new Date();
  const same = d.toDateString() === now.toDateString();
  return same ? hm(d) : `${monthDay(d)} ${hm(d)}`;
}
// "2026-10-21" as a local date
export const localDate = (iso) => { const [y, m, d] = iso.split('-').map(Number); return new Date(y, m - 1, d); };

// Estimated output undercounts (thinking is not in the log), so any total that
// includes it is a lower bound: shown with "≥" and said in a note.
export const LOWER_TITLE = 'At least this much: part of the output is estimated from content length, which undercounts it';
export function lowerMark(totals) { return totals.estimated_output.requests ? '≥ ' : ''; }
export function outputUsd(totals) {
  return lowerMark(totals) + usd(totals.categories.output.usd);
}
export function estimateNote(totals) {
  const est = totals.estimated_output;
  if (!est.requests) return null;
  return `Output and the totals that include it are lower bounds (≥): ${pct(est.share, est.share < 0.1 ? 1 : 0)} of output tokens estimated from content length (${est.requests} request${est.requests === 1 ? "" : "s"}); the true output can be higher than this estimate`;
}

export const SERIES = ['var(--s1)', 'var(--s2)', 'var(--s3)', 'var(--s4)', 'var(--s5)', 'var(--s6)'];
export const OTHER_COLOR = 'var(--gray)';
export const CATEGORY_LABELS = {
  cache_read: 'Cache reads', cache_write: 'Cache writes', output: 'Output', input: 'Uncached input', web: 'Web tools',
};
export const CATEGORY_ORDER = ['cache_read', 'cache_write', 'output', 'input'];
export const categoryColor = (key) => SERIES[Math.max(0, CATEGORY_ORDER.indexOf(key))];

// Colors that follow the entity (a project, an agent type, a model), never its rank:
// the server assigns one per entity over every entity in the index (/api/colors,
// capcom/report/colors.py: the user's Claude Code color first), so the same project
// has the same color in every chart, list and page. The assignment is kept in
// localStorage, so a reload paints with it at once; app.js asks for a fresh one
// and redraws the page when it changed. A key the server does not know (new since)
// takes a stable slot by its name. "(other)" is always the gray.
const COLOR_KINDS = ['project', 'agent', 'model'];
const COLORS_KEPT = 'capcom-colors';
let assigned = null; // the /api/colors reply in use

function nameHash(k) {
  let hsh = 0;
  for (let i = 0; i < k.length; i++) hsh = (hsh * 31 + k.charCodeAt(i)) >>> 0;
  return hsh;
}

// Use an /api/colors reply: its palette as CSS variables (--ec0, --ec1, ...: a light
// and a dark value each, so a theme switch restyles every mark) and its maps.
// Returns whether anything changed.
export function useColors(data, keep = true) {
  if (!data || !Array.isArray(data.palette)) return false;
  const text = JSON.stringify(data);
  if (assigned && assigned.text === text) return false;
  assigned = { ...data, text };
  if (typeof document !== 'undefined' && document.head) {
    let el = document.getElementById('entity-colors');
    if (!el) { el = document.createElement('style'); el.id = 'entity-colors'; document.head.append(el); }
    const vars = (j) => data.palette.map((c, i) => `--ec${i}:${c[j]}`).join(';');
    el.textContent = `:root,[data-theme="light"]{${vars(0)}}\n[data-theme="dark"]{${vars(1)}}`;
  }
  if (keep) { try { localStorage.setItem(COLORS_KEPT, text); } catch (e) { /* storage off */ } }
  return true;
}
try { useColors(JSON.parse(localStorage.getItem(COLORS_KEPT)), false); } catch (e) { /* none kept, or storage off */ }

// The color of one entity, as a CSS value. `kind` ('project', 'agent', 'model': the
// "Split by" group) narrows the lookup; without it every kind is tried in turn.
export function entityColor(key, kind) {
  if (key == null || key === '(other)') return OTHER_COLOR;
  const k = String(key);
  if (assigned) {
    for (const c of COLOR_KINDS.includes(kind) ? [kind] : COLOR_KINDS) {
      const i = assigned[c] && assigned[c][k];
      if (i != null) return `var(--ec${i})`;
    }
    return `var(--ec${nameHash(k) % (assigned.general || 1)})`;
  }
  return SERIES[nameHash(k) % SERIES.length];
}

// The colors of `keys` (a Map), each its entity's (entityColor).
export function entityColors(keys, kind) {
  const out = new Map();
  for (const k of keys) out.set(k, entityColor(k, kind));
  out.set('(other)', OTHER_COLOR);
  return out;
}

// The "Split by" choice shared by Overview, Spend, Day and Limits: the URL's ?group= first, then
// the last one picked (kept in localStorage), else by category.
export const GROUPS = [['category', 'Category'], ['project', 'Project'], ['model', 'Model'], ['agent', 'Agent type']];
export function pickedGroup(query) {
  const ok = (g) => GROUPS.some(([v]) => v === g);
  const q = query.get('group');
  if (ok(q)) return q;
  let saved = null;
  try { saved = localStorage.getItem('capcom-group'); } catch (e) { /* storage off */ }
  return ok(saved) ? saved : 'category';
}
export function rememberGroup(g) {
  try { localStorage.setItem('capcom-group', g); } catch (e) { /* storage off */ }
}

// The API path Overview, Spend, Day and Limits ask for first, for a page's query (a
// URLSearchParams): the pages and the router's preload use the same one, so a
// click never starts a second job for a view the page does not show.
export function overviewPath(query) {
  const group = pickedGroup(query);
  return group === 'category' ? 'overview' : `overview?group=${group}`;
}
// Spend's window from a page query: a range of days ending today (`days`), one local
// day (`day=2026-10-23`) or one local clock hour (`hour=2026-10-24T08`, with the UTC
// offset only for the second 01:00 of a night the clocks fall back). A drilled-in
// day or hour keeps `days`, the range it was opened from, for the way back up.
export const SPEND_DAYS = [1, 7, 14, 30, 90];
export function spendScope(query) {
  const back = SPEND_DAYS.includes(Number(query.get('days'))) ? Number(query.get('days')) : 14;
  const hour = query.get('hour'), day = query.get('day');
  if (hour) return { scale: 'hour', hour, day: hour.slice(0, 10), days: back };
  if (day) return { scale: 'day', day, days: back };
  return { scale: 'range', days: Number(query.get('days')) || 14 };
}
export function spendPath(query) {
  const sc = spendScope(query), group = pickedGroup(query);
  if (sc.scale === 'hour') return `spend?hour=${encodeURIComponent(sc.hour)}&group=${group}`;
  if (sc.scale === 'day') return `spend?day=${encodeURIComponent(sc.day)}&group=${group}`;
  return `spend?days=${sc.days}&group=${group}`;
}
// A local date as YYYY-MM-DD, and a local clock hour (its start, ms) as Spend's
// `hour` key: the wall hour, with the UTC offset only when the wall time alone
// reads as another hour (the second 01:00 of a night the clocks fall back; a
// local time that happens twice reads as the first).
export const isoDay = (d) => `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}`;
export function hourKey(t) {
  const d = new Date(t);
  const key = `${isoDay(d)}T${two(d.getHours())}`;
  if (new Date(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours()).getTime() === t) return key;
  const off = -d.getTimezoneOffset(), a = Math.abs(off);
  return `${key}${off >= 0 ? '+' : '-'}${two(Math.floor(a / 60))}:${two(a % 60)}`;
}
// The per-hour chart's gray "typical day" bars: on or off, from the URL's
// ?typical=1|0 first, then the viewer's last choice (kept in localStorage).
export function pickedTypical(query) {
  const q = query.get('typical');
  if (q === '1' || q === '0') return q === '1';
  try { return localStorage.getItem('capcom-typical') === '1'; } catch (e) { return false; }
}
export function rememberTypical(on) {
  try { localStorage.setItem('capcom-typical', on ? '1' : '0'); } catch (e) { /* storage off */ }
}
// A link to Spend for a range (`{days}`), a day (`{day}`) or an hour (`{hour}`): the
// split (unless by category, the default) and, drilled in, the range it came from
// (unless 14 days, the default) ride along.
export function spendHref(to, group, back = 14) {
  const q = new URLSearchParams();
  if (to.hour) q.set('hour', to.hour);
  else if (to.day) q.set('day', to.day);
  const drilled = Boolean(to.hour || to.day);
  if (!drilled) q.set('days', to.days);
  else if (back !== 14) q.set('days', back);
  if (group && group !== 'category') q.set('group', group);
  return '/spend?' + q.toString().replace(/%3A/g, ':');
}
export const LIMIT_DAYS = [7, 14, 30, 90];
export function limitsPath(query) {
  const days = LIMIT_DAYS.includes(Number(query.get('days'))) ? Number(query.get('days')) : 14;
  return `limits?days=${days}&group=${pickedGroup(query)}`;
}
// a window's own page (/limits/window/<account>/<kind>/<start>), split by the group
export function limitWindowPath(account, kind, start, query) {
  const q = new URLSearchParams({ account: account || '', window: kind || '', start: start || '', group: pickedGroup(query) });
  return `limit-window?${q}`;
}
export function dayPath(day, query) {
  return `day/${encodeURIComponent(day)}${groupQuery(pickedGroup(query))}`;
}
// The query a link to a Day page carries to keep a split ('' by category, the default).
export function groupQuery(group) {
  return group === 'category' ? '' : `?group=${group}`;
}

// ------------------------------------------------------------------ API

// One entry per API path: the final reply's promise (kept for the page's life, so
// a theme switch redraws without fetching again), and while the server computes it
// as a job, the newest partial reply and the progress (capcom/report/API.md,
// "Progressive replies").
const store = new Map();
// the newest partial of a path whose poll was stopped, to start from next time
const partials = new Map();
// replies in so far: a kept reply's `seq` says which came in after which
let arrivals = 0;
// `onPartial(path, partial, progress, source)` sees each 202; `pass`, set by the
// frame while it draws a page, takes the page's api() calls (router.js).
export const apiHooks = { onPartial: null, pass: null };

// Drop the kept results, so the next draw fetches fresh data. A load still in
// flight is not dropped for its waiters: it starts again in place (the data it was
// fetching may predate what changed), so a page awaiting it never sees an abort.
export function forget() {
  for (const [path, e] of [...store]) {
    if (e.done) store.delete(path);
    else if (!e.error) { e.ctrl.abort(); e.restart(); }
  }
  partials.clear();
}

export class ApiError extends Error {
  constructor(status, body) {
    super(body && body.message ? body.message : `HTTP ${status}`);
    this.status = status;
    this.body = body || {};
  }
}

function load(path, low = false) {
  // a partial kept from a stopped poll is from before: its source says so ('kept')
  const kept = partials.get(path) || null;
  const e = { path, ctrl: null, low, done: false, error: null, data: null, partial: kept, progress: 0, source: kept ? 'kept' : null, seq: 0, waiters: new Set(), run: 0 };
  const wake = () => { for (const w of e.waiters) w(); };
  let settle;
  e.promise = new Promise((resolve, reject) => { settle = { resolve, reject }; });
  e.promise.catch(() => {}); // the pages see the error; no unhandled rejection
  // One fetch of the entry; `forget()` starts another in its place, and the result
  // of the one it aborted is ignored (`run` says which is current).
  const start = () => {
    const ctrl = new AbortController();
    const run = ++e.run;
    e.ctrl = ctrl;
    getProgressive(path, {
      signal: ctrl.signal,
      headers: low ? { 'X-Capcom-Priority': 'low' } : null,
      onPartial: (partial, progress, source) => {
        if (run !== e.run) return;
        e.progress = Math.max(e.progress, progress || 0);
        if (partial) { e.partial = partial; e.source = source; partials.set(path, partial); }
        wake();
        if (apiHooks.onPartial) apiHooks.onPartial(path, partial, progress, source);
      },
    }).then((data) => {
      if (run !== e.run) return;
      Object.assign(e, { done: true, data, progress: 1, seq: ++arrivals });
      partials.delete(path);
      wake();
      settle.resolve(data);
    }, (err) => {
      if (run !== e.run) return;
      // kept until the next navigation (stopLoads), so the redraws of this view
      // show it instead of asking again and again
      e.error = err.status ? new ApiError(err.status, err.body) : err;
      wake();
      settle.reject(e.error);
    });
  };
  e.restart = () => { e.partial = null; e.source = null; e.progress = 0; start(); };
  start();
  store.set(path, e);
  return e;
}

// GET /api/<path>, following a server job to its end. While the frame draws a
// page (`apiHooks.pass`), the page gets the first data there is instead: the
// final reply, or else the newest partial one (the frame draws again as newer
// ones come). `fresh` skips the kept results (the live list).
export function api(path, fresh = false) {
  if (fresh) {
    return getProgressive(path).catch((err) => { throw err.status ? new ApiError(err.status, err.body) : err; });
  }
  const e = wanted(path);
  return apiHooks.pass ? apiHooks.pass.use(path, e) : e.promise;
}

// The kept load of `path` for a page, started now if there is none. A prefetch
// still loading runs at low priority on the server: a page that needs it asks
// again at a page's priority (the server lifts the same job; nothing is lost).
function wanted(path) {
  const e = store.get(path);
  if (e && !(e.low && !e.done && !e.error)) return e;
  if (e) { e.ctrl.abort(); store.delete(path); }
  return load(path);
}

// Start loading `path` now, for a page that will ask for it after another reply
// (Overview asks for checks once its own data is in): the two then load side by side.
export function preload(path) { wanted(path); }

// Where the reply of `path` stands: {done, error, partial, progress}, or null
// when nothing is loading or kept for it.
export function apiState(path) {
  const e = store.get(path);
  return e ? { done: e.done, error: e.error, partial: e.partial, progress: e.progress, source: e.source, promise: e.promise, entry: e } : null;
}

// Stop following every reply still computing except `keep` (paths), and drop the
// errors: the server's jobs go on at low priority and keep their result, so
// asking again is cheap.
export function stopLoads(keep = new Set()) {
  for (const [path, e] of store) {
    if (e.done || keep.has(path)) continue;
    store.delete(path);
    if (!e.error) e.ctrl.abort();
  }
}

// Fetch `paths` again in the background (the data changed on the server) and, once
// every one is in, keep the new replies in place of the old: the next draw shows
// them at once, with no loading state. Resolves to false if one failed.
export async function renew(paths) {
  let fresh;
  try {
    fresh = await Promise.all(paths.map((p) => getProgressive(p)));
  } catch (e) { return false; }
  paths.forEach((path, i) => {
    const old = store.get(path);
    if (old && !old.done) return; // a newer load started meanwhile: it wins
    const data = fresh[i];
    store.set(path, { path, ctrl: new AbortController(), low: false, done: true, error: null, data, partial: null, progress: 1, source: null, seq: ++arrivals, waiters: new Set(), promise: Promise.resolve(data) });
  });
  return true;
}

// Drop the kept replies of `paths` that came in before the newest of `shown` (the
// paths whose replies are on screen), so the next draw fetches them again instead
// of showing them: a reply kept from before the data changed would show older
// totals than the page already does (Overview's group switch, from a split loaded
// before the index changed). Asked again, the server answers from its cache, or
// makes the reply from the one on screen (server.py REGROUP), at the same moment.
export function outdate(paths, shown) {
  let newest = 0;
  for (const path of shown) {
    const e = store.get(path);
    if (e && e.done) newest = Math.max(newest, e.seq);
  }
  for (const path of paths) {
    const e = store.get(path);
    if (e && e.done && !shown.has(path) && e.seq < newest) store.delete(path);
  }
}

// Start the server on `path` ahead of a click (a hover on a link to its page):
// the reply is followed like any other, so the click finds it loading or done.
export function prefetch(path) { if (!store.has(path)) load(path, true); }

// ------------------------------------------------------------------ tooltip

// One shared tooltip (#tip in index.html). With a mouse it follows the pointer;
// on keyboard focus it is anchored to the mark; a tap shows it and a tap elsewhere
// hides it; Escape closes it. rows: [{label, value, color, note}], all text
// (inserted as text nodes); a color is a swatch beside the label, never the text's.

const tip = () => document.getElementById('tip');
let tipOwner = null;

// Where the tooltip's top-left corner goes beside the point (x, y): below and to the
// right by `pad`, flipped to the other side at the viewport's right or bottom edge,
// and clamped inside it either way.
export function placeTip(x, y, w, hgt, vw, vh, pad = 14) {
  let left = x + pad, top = y + pad;
  const flipX = left + w > vw - 4, flipY = top + hgt > vh - 4;
  if (flipX) left = x - w - pad;
  if (flipY) top = y - hgt - pad;
  return { left: clamp(left, 4, vw - w - 4), top: clamp(top, 4, vh - hgt - 4), flipX, flipY };
}

// Anchored to a box (a focused mark): centered above it, below when there is no room.
export function placeTipAt(rect, w, hgt, vw, vh, gap = 8) {
  const above = rect.top - hgt - gap;
  const flipY = above < 4;
  const top = flipY ? rect.bottom + gap : above;
  const left = rect.left + rect.width / 2 - w / 2;
  return { left: clamp(left, 4, vw - w - 4), top: clamp(top, 4, vh - hgt - 4), flipX: false, flipY };
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(v, Math.max(lo, hi))); }

// The tooltip's text on one line: the accessible name of a mark that has one.
export function tipText(title, rows) {
  const parts = (rows || []).map((r) => [r.label, r.value].filter((x) => x !== undefined && x !== null && x !== '').join(' ')
    + (r.note ? (r.label || r.value ? ', ' : '') + r.note : ''));
  return [title, parts.filter(Boolean).join('; ')].filter(Boolean).join(': ');
}

function fillTip(title, rows) {
  const el = tip();
  el.replaceChildren(...[
    title ? h('div.t', null, title) : null,
    ...rows.filter((r) => r.label || r.value).map((r) => h('div.r', null,
      h('span', null, r.color ? h('i', { style: `background:${r.color}` }) : null, r.label || ''),
      h('b', null, r.value ?? ''))),
    ...rows.filter((r) => r.note).map((r) => h('div.n', null, r.note)),
  ].filter(Boolean));
  el.hidden = false;
  return el;
}

function put(el, pos) {
  el.style.left = pos.left + 'px';
  el.style.top = pos.top + 'px';
}

// At the pointer of `event` (mousemove, pointermove).
export function showTip(event, title, rows) {
  const el = fillTip(title, rows);
  put(el, placeTip(event.clientX, event.clientY, el.offsetWidth, el.offsetHeight, window.innerWidth, window.innerHeight));
}

// Anchored to an element (keyboard focus, a tap).
export function showTipAt(anchor, title, rows) {
  const el = fillTip(title, rows);
  put(el, placeTipAt(anchor.getBoundingClientRect(), el.offsetWidth, el.offsetHeight, window.innerWidth, window.innerHeight));
}

export function hideTip() {
  const el = tip();
  if (el) el.hidden = true;
  tipOwner = null;
}

let tipGlobal = false;
function tipGlobals() {
  if (tipGlobal || typeof document.addEventListener !== 'function') return;
  tipGlobal = true;
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hideTip(); });
  // a tap elsewhere hides a tapped tooltip
  document.addEventListener('pointerdown', (e) => { if (tipOwner && !(tipOwner.contains && tipOwner.contains(e.target))) hideTip(); });
  if (typeof window !== 'undefined' && window.addEventListener) {
    window.addEventListener('capcom-route', hideTip);
    // an anchored tooltip moves with its mark (a focused mark scrolled into view)
    window.addEventListener('scroll', () => {
      const el = tip();
      if (tipOwner && el && !el.hidden) put(el, placeTipAt(tipOwner.getBoundingClientRect(), el.offsetWidth, el.offsetHeight, window.innerWidth, window.innerHeight));
    }, { passive: true, capture: true });
  }
}

// Give a mark its tooltip: on hover (following the pointer), on keyboard focus and
// on a tap (anchored to it). Unless `opts.focus` is false the mark joins the tab
// order with the same text as its accessible name (`opts.aria` overrides it, for a
// link or a button that says what it does). `rows` may be a function, read on show.
export function tipOn(el, title, rows, opts = {}) {
  tipGlobals();
  const get = () => (typeof rows === 'function' ? rows() : rows);
  if (opts.focus !== false) {
    if (!opts.native) el.setAttribute('tabindex', '0');
    el.setAttribute('aria-label', opts.aria || tipText(title, get()));
  }
  if (el.classList) el.classList.add('mk');
  el.addEventListener('pointermove', (e) => {
    if (e.pointerType === 'touch') return;
    tipOwner = null;
    showTip(e, title, get());
  });
  el.addEventListener('pointerleave', (e) => { if (e.pointerType !== 'touch' && !tipOwner) hideTip(); });
  el.addEventListener('pointerdown', (e) => {
    if (e.pointerType !== 'touch') return;
    showTipAt(el, title, get());
    tipOwner = el;
  });
  el.addEventListener('focus', () => {
    // a mouse click focuses too: keep the pointer's tooltip then
    if (el.matches && !el.matches(':focus-visible')) return;
    showTipAt(el, title, get());
    tipOwner = el;
  });
  el.addEventListener('blur', () => { if (tipOwner === el) hideTip(); });
  return el;
}

// A bar split into colored parts, each with its details on hover, focus or tap.
// parts: [{color, share (0..1 of the bar), title, rows}]; the parts sit 2 px apart
// on the surface, and hovering one dims the others.
// With `opts.focus` false (a bar inside a control, such as a row's summary) the parts
// show their details on hover and tap only, and the bar is one image named by
// `opts.aria`: the same numbers must then be readable elsewhere.
export function segBar(parts, opts = {}) {
  const focus = opts.focus !== false;
  return h('div.stackbar.hl' + (opts.tall ? '.tall' : ''), { role: focus ? 'group' : 'img', 'aria-label': opts.aria || null },
    parts.filter((p) => p.share > 0).map((p) => tipOn(
      h('span', { style: `flex:${p.share.toFixed(5)} 1 0px;background:${p.color}` }), p.title, p.rows, { focus })));
}

// A table of the numbers a chart shows, behind a "Show as a table" toggle, so
// nothing is only on hover. head: [text], rows: [[cell text]]; cells after the
// first are right-aligned numbers.
export function tableToggle(head, rows, label = 'Show as a table') {
  return h('details.astable', null, h('summary', { text: label }),
    h('div.scroll', null, h('table.data.small', null,
      h('thead', null, h('tr', null, head.map((x, i) => h('th', { class: i ? 'num' : null, text: x })))),
      h('tbody', null, rows.map((r) => h('tr', null, r.map((x, i) => h('td', { class: i ? 'num' : null, text: x }))))))));
}

// ------------------------------------------------------------------ page parts

export function empty(title, text) {
  return h('div.empty', null, title ? h('strong', null, title) : null, text ? h('span', null, text) : null);
}

export function errorBox(err) {
  if (err instanceof ApiError && err.status === 503) {
    return h('div.error', null, err.message || 'The index cannot be read. Run capcom scan first.');
  }
  return h('div.error', null, 'Could not load this part: ' + (err.message || String(err)));
}

// A segmented control: options [[value, label]], onPick(value).
// options: [value, text, why it is off (optional: the button is shown disabled with it as tooltip)]
export function seg(label, options, current, onPick) {
  return h('div.seg', { role: 'group', 'aria-label': label },
    options.map(([v, text, off]) => h('button', {
      type: 'button', 'aria-pressed': String(String(v) === String(current)), onclick: off ? null : () => onPick(v),
      'aria-disabled': off ? 'true' : null, title: off || null,
    }, text)));
}

// A label short enough for a legend, a bar or a chip: past `max` characters it
// keeps the start and the last folder (or the end) around a middle "…", so a
// deep path still reads as where it starts and which folder it is.
export function clipLabel(text, max = 40) {
  if (text.length <= max) return text;
  const slash = text.lastIndexOf('/', text.length - 2);
  const last = slash >= 0 ? text.slice(slash) : '';
  const tail = last && last.length <= max - 12 ? last : text.slice(-Math.floor((max - 1) / 2));
  return text.slice(0, max - 1 - tail.length) + '…' + tail;
}

// A label as a span, shortened by clipLabel, with the full text as its title
// when it was shortened.
export function labelSpan(text, max = 40) {
  const shown = clipLabel(text, max);
  return h('span.lbl', { text: shown, title: shown === text ? null : text });
}

export function legend(items, total) {
  return h('div.legend', null,
    total ? h('span.total', null, h('i', { style: 'background:var(--text)' }), 'Total ', h('em', null, total)) : null,
    items.map((it) => h('span', null, h('i', { style: `background:${it.color}` }), labelSpan(it.label), ' ', h('em', null, it.value))));
}

// Bars, one per row: the label and the amount stay in sight; the row's note
// (its count, its typical day), its share and its folder are on hover and focus.
// `kind` names what the rows are ('project', 'agent', 'model'), for their colors.
export function barList(rows, valueOf, noteOf, kind) {
  if (!rows.length) return empty(null, 'Nothing in this window.');
  const max = Math.max(...rows.map(valueOf), 1e-9);
  const sum = rows.reduce((a, r) => a + valueOf(r), 0);
  return h('div.blist.hl', null, rows.map((r) => {
    const color = r.rest ? OTHER_COLOR : entityColor(r.key ?? r.label, kind);
    const v = valueOf(r);
    const note = noteOf ? noteOf(r) : '';
    const tipRows = [
      { label: 'Cost', value: usd(v), color },
      sum > 0 && rows.length > 1 ? { label: 'Share of these', value: pct(v / sum) } : null,
      note ? { note } : null,
      r.path && r.path !== r.label ? { note: r.path } : null,
    ].filter(Boolean);
    return tipOn(h('div.bitem', null,
      h('div.top', null, labelSpan(r.label), h('span.mono', null, usd(v))),
      h('div.track', null, h('div', { style: `width:${(v / max * 100).toFixed(1)}%;background:${color}` }))), r.label, tipRows);
  }));
}

export function sessionHref(id) { return '/session/' + encodeURIComponent(id); }

// The color a session was given in Claude Code (`/color`), as a dot before its name:
// a known name maps to a theme token (light and dark), anything else draws nothing.
const SESSION_COLORS = new Set(['red', 'orange', 'yellow', 'green', 'cyan', 'blue', 'purple', 'pink']);
export function sessionDot(color) {
  if (!color || !SESSION_COLORS.has(color)) return null;
  return h('span.sdot', { style: `background:var(--sc-${color})`, title: `Session color: ${color}`, 'aria-hidden': 'true' });
}

// The same color as a CSS value, for a dot drawn elsewhere (the live list's activity
// dot takes it), or null.
export function sessionColor(color) {
  return color && SESSION_COLORS.has(color) ? `var(--sc-${color})` : null;
}

// Change the page's query without a reload; the frame redraws the page. A new
// range (`days`, Overview's `range`) is its own step in the history, so Back goes
// 30 -> 14 -> 7 days before it leaves the page; other changes (a sort, a filter,
// a picked spike) replace the current step.
const RANGE_KEYS = ['days', 'range', 'day', 'hour'];

export function setQuery(params) {
  const q = new URLSearchParams(location.search);
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === '') q.delete(k); else q.set(k, v);
  }
  const qs = q.toString();
  const href = location.pathname + (qs ? '?' + qs : '');
  const before = new URLSearchParams(location.search);
  const step = RANGE_KEYS.some((k) => k in params && before.get(k) !== q.get(k));
  if (step) {
    // the step left keeps its scroll, for Back; the new one starts where we are
    try { history.replaceState({ ...(history.state || {}), scrollY: window.scrollY }, ''); } catch (e) { /* state too large */ }
    history.pushState({ scrollY: window.scrollY }, '', href);
  } else {
    history.replaceState(history.state, '', href);
  }
  window.dispatchEvent(new Event('capcom-route'));
}

// The largest value of an array, by a loop (spreading a big array into Math.max
// overflows the call stack).
export function maxOf(values, floor = -Infinity) {
  let m = floor;
  for (const v of values) if (v > m) m = v;
  return m;
}

// A button that copies `text` to the clipboard and says so.
export function copyButton(text, label = 'Copy') {
  const btn = h('button.copy', { type: 'button', text: label });
  btn.addEventListener('click', async () => {
    let ok = false;
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch (e) {
      const area = h('textarea', { style: 'position:fixed;left:-9999px', 'aria-hidden': 'true' });
      area.value = text;
      document.body.append(area);
      area.select();
      try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
      area.remove();
    }
    btn.textContent = ok ? 'Copied' : 'Select and copy by hand';
    setTimeout(() => { btn.textContent = label; }, 1800);
  });
  return btn;
}

// "today" or "the last N days"
export function daysText(days) { return days === 1 ? 'today' : `the last ${days} days`; }

// The cause labels and confidence names of capcom.explain (the API sends labels too).
export const CONFIDENCE = { clear: 'clear cause', likely: 'likely cause', unexplained: 'unexplained' };

// A spike's kind in words: [headline, tag].
export const SPIKE_KINDS = {
  single_request: ['One request far above its recent median', 'Single request'],
  slope_change: ['The spend rate rose and stayed up', 'Slope change'],
};

// A spike's headline and tag: its largest named cause when capcom.explain gave
// one, else what capcom.timeline saw; `rest` is a muted note when the unexplained
// rest is more than half the excess.
export function spikeText(m) {
  const kind = SPIKE_KINDS[m.kind] || [m.kind, m.kind];
  const rest = m.mostly_unexplained && m.cause !== 'unexplained' ? `mostly unexplained: ${usd(m.unexplained_usd)} of ${usd(m.excess_usd)}` : null;
  const title = m.headline || kind[0], tag = m.cause_label || kind[1];
  // `same`: the title already starts with the tag's words, so the tag would repeat it
  const same = title.toLowerCase().startsWith(tag.toLowerCase());
  return { title, tag, same, unexplained: m.cause === 'unexplained', rest };
}

// The Spikes page for one spike: a window of whole days that starts on or before it.
export function spikeHref(key, startMs, days) {
  // Local calendar days from the spike's day to today, inclusive: a spike at
  // 23:30 seen at 00:30 needs days=2, not 1.
  const day0 = new Date(startMs);
  day0.setHours(0, 0, 0, 0);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const back = Math.round((today - day0) / 864e5) + 1;
  const d = days || [1, 7, 14, 30, 90].find((x) => x >= back) || Math.min(366, back + 1);
  return `/spikes?days=${d}` + (key ? '&spike=' + encodeURIComponent(key) : '');
}

// A spike's project as a chip: the shared label, its full folder on hover; null
// when the spike has no session. `onPick`, when given, makes it a filter button.
export function projectChip(it, onPick) {
  if (!it || !it.project) return null;
  const title = it.project_path && it.project_path !== it.project ? `Project ${it.project_path}` : `Project ${it.project}`;
  const text = clipLabel(it.project);
  if (!onPick) return h('span.tag.proj', { text, title });
  return h('button.tag.proj', { type: 'button', text, title: `${title}: show its spikes only`, onclick: (e) => { e.preventDefault(); e.stopPropagation(); onPick(it.project); } });
}

// The "what changed" line (/api/overview `regime`): the level the daily spend rose
// to, from when, and the sessions behind it (each linked to its page) and the
// agent type that rose most. Null without a change.
export function storyLine(reg) {
  if (!reg) return null;
  const day = new Date(reg.day + 'T12:00:00');
  const times = reg.ratio == null ? 'from nothing' : `×${reg.ratio.toFixed(1)}`;
  const money = (x) => usd(x, x >= 10 ? 0 : 2);
  const parts = [`Spend rose ${times} from ${monthDay(day)} (${money(reg.before_usd_per_day)} to ${money(reg.after_usd_per_day)} a day)`];
  const named = reg.sessions.filter((x) => x.in_text);
  const kids = [];
  named.forEach((x, i) => {
    if (i) kids.push(i === named.length - 1 ? ' and ' : ', ');
    const label = `${x.project ? x.project + ' ' : ''}${x.key.slice(0, 8)}`;
    kids.push(h('a', { href: sessionHref(x.key), text: label, title: x.title || x.key }), ` (${pct(x.share)})`);
  });
  const agent = reg.agent_types.find((a) => !a.rest);
  const agentText = agent ? `${agent.key === 'main' ? 'main threads' : agent.key + ' subagents'} ${pct(agent.share)}` : null;
  return h('p.story', { title: `Mean daily spend in the ${reg.after_days} days from ${monthDay(day)} against the 7 days before` },
    parts[0], named.length || agentText ? ': ' : '', ...kids, named.length && agentText ? '; ' : '', agentText || '', '.');
}
