// The frame's loading: links followed without a reload (history.pushState, back
// and forward restore the scroll), a page drawn the moment it is asked for, from
// the first data there is, and drawn again as the server's job sends newer
// partial replies (capcom/report/API.md, "Progressive replies"), each redraw
// swapped in with its numbers tweening (morph.js). While data loads: the old
// content dimmed (same page) or skeleton cards (another page), a thin bar at the
// top of each loading card, and a status line ("Loading 30 days… 62 %").

import { h, apiHooks, apiState, stopLoads, prefetch, preload, renew, errorBox, overviewPath, spendPath } from './lib.js';
import { swapIn } from './morph.js';

const GRACE_MS = 80; // a page drawn within this needs no loading state
const POLL_MS = 200; // how often the frame looks at the replies still loading
const REDRAW_MS = 350; // at most one redraw per this while partial replies come

const LABELS = { overview: 'Overview', spend: 'Spend', sessions: 'Sessions', spikes: 'Spikes', checks: 'Checks', changes: 'Did it work?', limits: 'Limits', session: 'Session', day: 'Day' };
const PAGE_PATH = /^\/(?:(?:spend|sessions|spikes|checks|changes|limits)\/?|(?:session|day|changes)\/[^/]+)?$/;

let cfg = null; // {pages, route, decorate, onNavigate}
let view = null;
let seq = 0;

// ------------------------------------------------------------------ status line

let statusEl = null;
function status(text, said) {
  if (!statusEl) {
    statusEl = h('div.loadstatus', { hidden: true },
      h('span.spin', { 'aria-hidden': 'true' }), h('span.txt', { 'aria-hidden': 'true' }),
      h('span.sr', { role: 'status', 'aria-live': 'polite' }));
    document.body.append(statusEl);
  }
  statusEl.hidden = !text;
  if (text) statusEl.children[1].textContent = text;
  if (said !== undefined && statusEl.children[2].textContent !== said) statusEl.children[2].textContent = said;
}

function what(r) {
  if (r.query.has('days') && ['spend', 'sessions', 'spikes', 'checks', 'limits'].includes(r.name)) {
    const d = Number(r.query.get('days'));
    return d === 1 ? 'today' : d === 0 ? 'all sessions' : `${d} days`;
  }
  return LABELS[r.name] || 'the page';
}

// Performance marks ("capcom:paint", "capcom:loaded"...) for measuring a click
// to its first and last draw.
const mark = (name) => { try { performance.mark('capcom:' + name); } catch (e) { /* no timing */ } };

// ------------------------------------------------------------------ one draw

// The pass's hook into api(): it records each path the page read and how far it
// had come, and hands the page the first data there is.
function makePass(v, node) {
  const p = { used: new Map(), mark: null, node };
  const begin = () => { if (p.mark === null) p.mark = node.childElementCount; };
  p.use = (path, e) => {
    if (e.done) { p.used.set(path, 'final'); return e.promise; }
    if (e.error) { p.used.set(path, 'error'); return e.promise; }
    begin();
    if (e.partial) { p.used.set(path, e.partial); return Promise.resolve(e.partial); }
    p.used.set(path, null);
    if (view === v) waiting(v, p);
    return new Promise((resolve, reject) => {
      const w = () => {
        if (!(e.done || e.error || e.partial)) return;
        e.waiters.delete(w);
        if (e.error) { p.used.set(path, 'error'); reject(e.error); } else if (e.done) { p.used.set(path, 'final'); resolve(e.data); } else { p.used.set(path, e.partial); resolve(e.partial); }
      };
      e.waiters.add(w);
    });
  };
  return p;
}

async function draw(v) {
  if (v.running) { v.dirty = true; return; }
  v.running = true;
  v.dirty = false;
  v.last = performance.now();
  const next = h('main', { id: 'main', class: 'panel' });
  const p = makePass(v, next);
  v.pass = p;
  apiHooks.pass = p;
  try {
    await cfg.pages[v.r.name](next, v.r, () => view === v);
  } catch (err) {
    if (view === v) next.replaceChildren(errorBox(err));
  }
  if (apiHooks.pass === p) apiHooks.pass = null;
  v.running = false;
  if (view !== v) return;
  v.used = p.used;
  v.mark = p.mark;
  if (cfg.decorate) cfg.decorate(next);
  // a redraw waits for a press to end: swapped in between, the release would land
  // on a new element and the click would be lost
  afterPointer(() => {
    if (view !== v) return;
    show(v, next);
    if (v.dirty) schedule(v);
  });
}

// Run `fn` now, or once the pointer that is down comes up (after its click).
let pressed = false;
const queued = [];
export function afterPointer(fn) { if (pressed) queued.push(fn); else fn(); }
function release() {
  setTimeout(() => {
    pressed = false;
    for (const fn of queued.splice(0)) fn();
  }, 0);
}

// The page is waiting for its first data on a path: the old content dims (same
// page), or what the page has drawn so far goes in with skeleton cards below.
function waiting(v, p) {
  const live = document.getElementById('main');
  if (live === p.node) return;
  if (live.hasAttribute('data-skeleton') && p.node.childElementCount) {
    clearTimeout(v.grace);
    p.node.append(skeletonCards('skel-tail'));
    show(v, p.node, true);
  }
}

function busyCards(v, node, loading) {
  const cards = [];
  if (loading) {
    const kids = [...node.children].slice(v.mark ?? node.children.length);
    for (const k of kids) {
      if (k.classList.contains('card')) cards.push(k);
      cards.push(...k.querySelectorAll('.card'));
    }
  }
  for (const c of node.querySelectorAll('.card[aria-busy]')) if (!cards.includes(c)) c.removeAttribute('aria-busy');
  for (const c of cards) c.setAttribute('aria-busy', 'true');
}

// Put `next` on screen in place of whatever is there.
function show(v, next, provisional = false) {
  const live = document.getElementById('main');
  clearTimeout(v.grace);
  const fromOld = live !== next && !live.hasAttribute('data-skeleton') && live.childElementCount > 0;
  if (live !== next) swapIn(live, next, { animate: fromOld || live.hasAttribute('data-skeleton') });
  if (!provisional) for (const t of next.querySelectorAll(':scope > .skel-tail')) t.remove();
  next.removeAttribute('data-stale');
  if (!v.shown) arrive(v, next);
  v.shown = true;
  mark(provisional ? 'paint-part' : 'paint');
  tick(v);
}

// The first time the view's content shows: scroll and focus as a navigation wants.
function arrive(v, node) {
  if (v.scroll != null) window.scrollTo(0, v.scroll);
  if (v.focus) {
    const h1 = node.querySelector('h1');
    if (h1 && !node.contains(document.activeElement)) {
      h1.setAttribute('tabindex', '-1');
      h1.focus({ preventScroll: true });
    }
  }
}

// ------------------------------------------------------------------ following the loads

function tick(v) {
  if (view !== v) return;
  const used = v.running && v.pass ? v.pass.used : v.used;
  let changed = false, sum = 0, n = 0;
  for (const [path, was] of used) {
    const st = apiState(path);
    if (!st) { if (!v.running) changed = true; continue; }
    if (st.error) { if (was !== 'error' && !v.running) changed = true; continue; }
    if (st.done) { if (was !== 'final' && !v.running) changed = true; continue; }
    n++;
    sum += st.progress;
    if (!v.running && st.partial && st.partial !== was) changed = true;
  }
  const loading = n > 0;
  const live = document.getElementById('main');
  if (loading) {
    const frac = sum / n;
    live.style.setProperty('--progress', `${Math.max(2, frac * 100).toFixed(1)}%`);
    live.toggleAttribute('data-indet', frac <= 0);
    if (v.shown && !v.running) busyCards(v, live, true);
    status(`Loading ${what(v.r)}…${frac > 0 ? ` ${Math.round(frac * 100)} %` : ''}`, `Loading ${what(v.r)}`);
    if (!v.timer) v.timer = setInterval(() => tick(v), POLL_MS);
  } else if (!v.running && v.shown && !changed) {
    if (!v.loaded) { v.loaded = true; mark('loaded'); }
    busyCards(v, live, false);
    live.removeAttribute('data-indet');
    if (v.timer) { clearInterval(v.timer); v.timer = 0; }
    status(null, v.wasLoading ? `${what(v.r)[0].toUpperCase() + what(v.r).slice(1)} loaded` : undefined);
  }
  v.wasLoading = v.wasLoading || loading;
  if (changed) schedule(v);
}

function schedule(v) {
  if (v.running) { v.dirty = true; return; }
  if (v.pending) return;
  v.pending = setTimeout(() => { v.pending = 0; if (view === v) draw(v); }, Math.max(0, REDRAW_MS - (performance.now() - v.last)));
}

// ------------------------------------------------------------------ live refresh

// The data behind the page changed on the server (app.js asks /api/version every
// 30 s): fetch the view's replies again in the background and, once all are in,
// draw the page from them and swap it in place, numbers tweening and charts
// gliding, with no dimming, no scroll jump and the focus kept; the sidebar's
// "Index updated" is the only note of it. Nothing happens while the view is
// still loading, or if the user moved on meanwhile.
export async function refreshView() {
  const v = view;
  if (!v || !v.loaded || v.running || v.timer) return false;
  const paths = [...v.used.keys()];
  if (!paths.length || !(await renew(paths)) || view !== v) return false;
  draw(v);
  return true;
}

// ------------------------------------------------------------------ placeholders

function skeletonCards(cls) {
  const line = (w) => h('span.skel-line', { style: `width:${w}%` });
  return h(`div.${cls}`, { 'aria-hidden': 'true' },
    h('div.row-wrap', null, [0, 1, 2].map(() => h('div.card.skel', null, line(40), h('span.skel-line.big', { style: 'width:60%' })))),
    h('div.card.skel.tall', null, line(30), line(90), line(75), line(82)));
}

function skeleton(r) {
  return h('main', { id: 'main', class: 'panel', 'data-skeleton': '', 'aria-busy': 'true' },
    h('header.head', null, h('div.titles', null, h('h1', { text: LABELS[r.name] || '' }), h('span.skel-line', { style: 'width:220px' }))),
    skeletonCards('skel-body'));
}

// Nothing drawn yet after GRACE_MS: dim the old content of the same page, or
// show the target's skeleton.
function placeholder(v, samePage) {
  if (view !== v || v.shown) return;
  mark('placeholder');
  const live = document.getElementById('main');
  if (samePage && live.childElementCount && !live.hasAttribute('data-skeleton')) {
    live.setAttribute('data-stale', '');
    v.mark = 0;
    busyCards(v, live, true);
    live.setAttribute('data-indet', '');
  } else {
    const sk = skeleton(v.r);
    live.replaceWith(sk);
    arrive(v, sk);
  }
  tick(v);
}

// ------------------------------------------------------------------ views

// Draw the page of the current URL. `nav`: a navigation (push, pop or a new
// query), so the old view's loads stop; `scroll`/`focus`: what to do when its
// content first shows.
export function render({ scroll = null, focus = false } = {}) {
  const r = cfg.route();
  const key = location.pathname + location.search;
  const prev = view;
  const changed = !prev || prev.key !== key;
  if (prev) stop(prev);
  const v = { key, r, id: ++seq, used: new Map(), mark: null, shown: false, running: false, dirty: false, last: 0, timer: 0, pending: 0, scroll, focus };
  view = v;
  if (changed) {
    // the old view's loads stop, except the ones this page asks for first
    const first = new Set(apiFor(new URL(location.href)));
    stopLoads(first);
    for (const path of first) preload(path);
  }
  if (changed) {
    const samePage = prev && prev.r.name === r.name;
    const live = document.getElementById('main');
    if (!live.childElementCount) placeholder(v, false);
    else v.grace = setTimeout(() => placeholder(v, samePage), GRACE_MS);
  }
  draw(v);
}

function stop(v) {
  clearTimeout(v.grace);
  clearTimeout(v.pending);
  if (v.timer) clearInterval(v.timer);
  v.timer = 0;
}

export function go(href, { replace = false } = {}) {
  try { history.replaceState({ ...(history.state || {}), scrollY: window.scrollY }, ''); } catch (e) { /* state too large */ }
  if (replace) history.replaceState({ scrollY: 0 }, '', href); else history.pushState({ scrollY: 0 }, '', href);
  if (cfg.onNavigate) cfg.onNavigate();
  render({ scroll: 0, focus: true });
}

// The API paths a link's page asks for first, to prefetch on hover and to start
// at once on a click. Overview's and Spend's follow the page's query and the
// group picked last, as the pages do: another path would start a second job,
// which the page never shows, beside the one it waits for.
export function apiFor(url) {
  const p = url.pathname.replace(/\/+$/, '') || '/';
  const m = /^\/(session|day|changes)\/([^/]+)$/.exec(p);
  if (m) return [`${m[1]}/${encodeURIComponent(decodeURIComponent(m[2]))}`];
  if (p === '/') return [overviewPath(url.searchParams), 'checks?days=14'];
  if (p === '/spend') return [spendPath(url.searchParams)];
  if (url.search) return [];
  return {
    '/sessions': ['sessions?days=14&sort=usd&limit=300'], '/spikes': ['spikes?days=14'],
    '/checks': ['checks?days=14', 'changes', 'changes/noticed?days=14'], '/changes': ['changes'], '/limits': ['limits?days=14'],
  }[p] || [];
}

function linkOf(e) {
  const a = e.target instanceof Element ? e.target.closest('a[href]') : null;
  if (!a || (a.target && a.target !== '_self') || a.hasAttribute('download')) return null;
  const url = new URL(a.href, location.href);
  if (url.origin !== location.origin || !PAGE_PATH.test(url.pathname)) return null;
  return url;
}

export function startRouter(options) {
  cfg = options;
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
  // a release outside the window never comes: let go after 3 s at most
  let safety = 0;
  document.addEventListener('pointerdown', () => { pressed = true; clearTimeout(safety); safety = setTimeout(release, 3000); }, true);
  window.addEventListener('blur', release);
  document.addEventListener('pointerup', release, true);
  document.addEventListener('pointercancel', release, true);
  document.addEventListener('click', (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const url = linkOf(e);
    if (!url) return;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return; // an anchor
    e.preventDefault();
    go(url.pathname + url.search + url.hash);
  });
  // a range or filter button shows as picked at once, before its page redraws
  document.addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest('#main .seg button') : null;
    if (!b || b.getAttribute('aria-disabled') === 'true') return;
    for (const x of b.parentNode.children) if (x.tagName === 'BUTTON') x.setAttribute('aria-pressed', String(x === b));
  }, true);
  window.addEventListener('popstate', (e) => {
    if (cfg.onNavigate) cfg.onNavigate();
    render({ scroll: e.state && typeof e.state.scrollY === 'number' ? e.state.scrollY : 0 });
  });
  // prefetch a link's page after a short hover, or at once on focus
  let hover = 0;
  const ahead = (e) => {
    const url = linkOf(e);
    if (url) for (const path of apiFor(url)) prefetch(path);
  };
  document.addEventListener('mouseover', (e) => {
    clearTimeout(hover);
    if (e.target instanceof Element && e.target.closest('#sidebar a[href]')) hover = setTimeout(() => ahead(e), 150);
  });
  document.addEventListener('mouseout', () => clearTimeout(hover));
  document.addEventListener('focusin', (e) => { if (e.target instanceof Element && e.target.closest('#sidebar a[href]')) ahead(e); });
}
