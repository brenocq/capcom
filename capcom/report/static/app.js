// The frame: sidebar (navigation, session search, live sessions, index status,
// theme toggle) and a router from the URL path to a page.

import { h, s, api, stamp, sessionHref, sessionColor, forget, useColors } from './lib.js';
import { startRouter, render, go, afterPointer, refreshView } from './router.js';
import { swapIn } from './morph.js';
import { liveRows, line, inPhase } from './skeleton.js';
import { renderOverview } from './overview.js';
import { renderSpend } from './spend.js';
import { renderSession } from './session.js';
import { renderAgent } from './agent.js';
import { renderDay } from './day.js';
import { renderSessions } from './sessions.js';
import { renderSpikes } from './spikes.js';
import { renderChecks } from './checks.js';
import { renderLimits } from './limits.js';
import { renderLimitWindow } from './limitwindow.js';
import { renderChanges, renderChange } from './changes.js';
import { freshToken } from './marks.js';
import { networkNote, mascot } from './ask.js';

const ICON = {
  overview: 'M3 12h4l3-8 4 16 3-8h4',
  spend: 'M4 20V10M10 20V4M16 20v-7M22 20H2',
  sessions: 'M4 6h16M4 12h10M4 18h13',
  spikes: 'M3 18l5-5 4 3 8-10',
  checks: 'M9 11l3 3 8-8M20 12v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9',
  limits: 'M4 18l4-8 4 5 4-9v12M3 20h18',
  changes: 'M21 12a9 9 0 1 1-4.5-7.8M21 4.5L12 13.5l-3-3',
};
const NAV = [
  ['overview', '/', 'Overview'],
  ['spend', '/spend', 'Spend'],
  ['sessions', '/sessions', 'Sessions'],
  ['spikes', '/spikes', 'Spikes'],
  ['checks', '/checks', 'Checks'],
  ['changes', '/changes', 'Did it work?'],
  ['limits', '/limits', 'Limits'],
];

// The sidebar's links from Limits carry its range (`days`) to the pages that take the
// same value: Spend has every range Limits has, Sessions all but 90 days.
const SAME_DAYS = { spend: [1, 7, 14, 30, 90], sessions: [1, 7, 14, 30] };
function navHref(id, href, r) {
  const days = Number(r.query.get('days'));
  return r.name === 'limits' && (SAME_DAYS[id] || []).includes(days) ? `${href}?days=${days}` : href;
}

function icon(d, size = 16) {
  return s('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.8, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' }, s('path', { d }));
}

function route() {
  const parts = location.pathname.replace(/^\/+|\/+$/g, '').split('/');
  const name = parts[0] || 'overview';
  return { name, arg: parts[1] ? decodeURIComponent(parts[1]) : null, rest: parts.slice(1).map(decodeURIComponent), query: new URLSearchParams(location.search) };
}

// ------------------------------------------------------------------ theme

const isDark = () => document.documentElement.dataset.theme === 'dark';

function toggleTheme() {
  const next = isDark() ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('capcom-theme', next); } catch (e) { /* storage off */ }
  drawSidebar();
  renderPage();
}

if (window.matchMedia) {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
    let saved = null;
    try { saved = localStorage.getItem('capcom-theme'); } catch (err) { /* storage off */ }
    if (saved) return;
    document.documentElement.dataset.theme = e.matches ? 'dark' : 'light';
    drawSidebar();
    renderPage();
  });
}

// ------------------------------------------------------------------ sidebar

let liveData = null, statusData = null;

function drawSidebar() {
  const r = route();
  const active = { session: 'sessions', day: 'spend' }[r.name] || r.name;
  const input = h('input', { id: 'capcom-find', type: 'search', placeholder: 'Project, title or agent type', value: r.name === 'sessions' ? r.query.get('q') || '' : '' });
  const form = h('form.find', { role: 'search', onsubmit: (e) => { e.preventDefault(); go('/sessions?q=' + encodeURIComponent(input.value.trim())); } },
    h('label', { for: 'capcom-find', text: 'Find a session' }), input);
  // drawn anew and swapped in, so the live rates tween and new sessions fade in
  const fresh = h('nav', { id: 'sidebar', class: 'sidebar', 'aria-label': 'capcom' },
    h('a.brand', { href: '/' }, mascot(26, true), h('span', { text: 'capcom' })),
    form,
    h('div.nav', null, NAV.map(([id, href, label, soon]) => h('a', { href: navHref(id, href, r), 'aria-current': id === active ? 'page' : null },
      icon(ICON[id]), h('span', { text: label }), soon ? h('span.soon', { text: soon, title: `Coming in milestone ${soon.slice(1)}` }) : null,
      id === 'changes' && statusData && statusData.changes ? h('span.soon', { text: String(statusData.changes), title: `${statusData.changes} change${statusData.changes === 1 ? '' : 's'} marked as applied` }) : null))),
    liveList(),
    footer(),
  );
  afterPointer(() => { const live = document.getElementById('sidebar'); swapIn(live, fresh, { animate: live.childElementCount > 0 }); });
  drawTopbar(r, active);
}

function liveList() {
  const box = h('div.live', null, h('div.side-head', null, h('span', { text: 'Live now' }), h('span', { text: '$/hour' })));
  if (!liveData) { box.append(liveRows()); return box; }
  if (liveData.watching === false) {
    // this server never scans, so the index stops at its last update: say that, not "nothing ran"
    const at = liveData.index_updated ? new Date(liveData.index_updated).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : null;
    box.append(h('span.note', { style: 'padding:0 10px', text: `Paused: this server doesn't scan${at ? `; the index is from ${at}` : ''}.` }));
    return box;
  }
  if (!liveData.sessions.length) { box.append(h('span.note', { style: 'padding:0 10px', text: 'No session wrote in the last 10 minutes.' })); return box; }
  const top = liveData.sessions[0].usd_per_hour;
  for (const sn of liveData.sessions.slice(0, 8)) {
    const hot = sn.usd_per_hour > 0 && sn.usd_per_hour === top;
    // the folder (the name already is it for an untitled session), then the subagents running
    const agents = sn.agents_active ? `${sn.agents_active} agent${sn.agents_active > 1 ? 's' : ''}` : null;
    const sub = [sn.title ? sn.project : null, agents].filter(Boolean).join(' · ');
    box.append(h('a', { href: sessionHref(sn.session_id), title: `${sn.title || sn.project}: last record ${stamp(new Date(sn.last_seen))}` },
      // the session's own color (/color) when it has one, else green, orange for the top spender
      h('span', { class: hot ? 'dot hot' : 'dot', style: sessionColor(sn.color) ? `background:${sessionColor(sn.color)}` : null }),
      h('span.name', null, h('span', { text: sn.title || sn.project }), sub ? h('span.note', { text: sub }) : null),
      h('span', { class: hot ? 'rate hot' : 'rate', text: sn.usd_per_hour.toFixed(2) })));
  }
  return box;
}

function footer() {
  // before the status is in: lines in place of the two notes to come
  let status = statusData ? h('span', { text: 'Reading the index…' }) : inPhase(h('span.sk-lines', { 'aria-hidden': 'true' }, line('92%', '.sk-note'), line('58%', '.sk-note'), line('86%', '.sk-note'), line('64%', '.sk-note')));
  if (statusData && statusData.index) {
    const d = new Date(statusData.index.updated);
    status = h('span', { text: `Index updated ${stamp(d)} · ${statusData.sessions.toLocaleString('en-US')} sessions · ${(statusData.index.size_bytes / 1e6).toFixed(0)} MB` });
  }
  const dark = isDark();
  return h('div.foot', null,
    h('div.row', null, icon('M8 11V8a4 4 0 0 1 8 0v3M5 11h14v10H5z', 14), h('span.netnote', { text: networkNote() })),
    status,
    statusData ? h('span', { text: `Prices checked ${statusData.prices_checked}. API-equivalent cost, not your bill.` }) : null,
    h('button.toggle', { type: 'button', onclick: toggleTheme },
      icon(dark ? 'M12 8a4 4 0 1 0 0 8a4 4 0 1 0 0-8M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4' : 'M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z'),
      h('span', { text: dark ? 'Light theme' : 'Dark theme' })));
}

async function refreshLive() {
  try {
    liveData = await api('live', true);
  } catch (e) {
    liveData = { sessions: [] };
  }
  drawSidebar();
}

// ------------------------------------------------------------------ responsive shell

// Below 900 px (style.css, "responsive") the sidebar leaves the page: a top bar
// shows the logo, the page's name and a menu button that opens the sidebar as a
// sheet under the bar. The sheet closes on Escape, a click outside it and on
// navigation; while it is open the focus stays in it (and on the button), and it
// goes back to the button when it closes.

const NARROW = window.matchMedia ? window.matchMedia('(max-width: 899.98px)') : null;
const BURGER = 'M4 7h16M4 12h16M4 17h16';
const CLOSE = 'M6 6l12 12M18 6L6 18';
const FOCUSABLE = 'a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])';
let menuOpen = false;

function topbar() {
  let bar = document.getElementById('topbar');
  if (bar) return bar;
  const menu = h('button.menu', { type: 'button', 'aria-controls': 'sidebar', 'aria-expanded': 'false', 'aria-label': 'Menu', onclick: () => setMenu(!menuOpen) }, icon(BURGER, 20));
  bar = h('header.topbar', { id: 'topbar' }, h('a.brand', { href: '/' }, mascot(26, true), h('span', { text: 'capcom' })), h('span.where'), menu);
  document.querySelector('.frame').before(bar);
  document.body.append(h('div.scrim', { 'aria-hidden': 'true', onclick: () => setMenu(false) }));
  // following a link in the sheet closes it (on the document: the sidebar node is
  // swapped for a fresh one on each redraw)
  document.addEventListener('click', (e) => { if (e.target instanceof Element && e.target.closest('#sidebar a[href]')) setMenu(false, false); });
  document.addEventListener('keydown', trapKeys);
  window.addEventListener('capcom-route', () => setMenu(false, false));
  if (NARROW) NARROW.addEventListener('change', () => { if (!NARROW.matches) setMenu(false, false); });
  return bar;
}

function drawTopbar(r, active) {
  const item = NAV.find(([id]) => id === active);
  topbar().querySelector('.where').textContent = item ? item[2] : r.name.charAt(0).toUpperCase() + r.name.slice(1);
}

function setMenu(open, refocus = true) {
  if (open && !(NARROW && NARROW.matches)) open = false;
  const was = menuOpen;
  menuOpen = open;
  const button = topbar().querySelector('.menu');
  button.setAttribute('aria-expanded', String(open));
  button.setAttribute('aria-label', open ? 'Close the menu' : 'Menu');
  button.replaceChildren(icon(open ? CLOSE : BURGER, 20));
  document.body.classList.toggle('menu-open', open);
  const main = document.getElementById('main');
  if (main) main.inert = open;
  const nav = document.getElementById('sidebar');
  if (open && !was) (nav.querySelector('[aria-current="page"]') || nav.querySelector(FOCUSABLE)).focus();
  if (!open && was && refocus) button.focus();
}

function trapKeys(e) {
  if (!menuOpen) return;
  if (e.key === 'Escape') { e.preventDefault(); setMenu(false); return; }
  if (e.key !== 'Tab') return;
  const items = [...document.getElementById('sidebar').querySelectorAll(FOCUSABLE)].filter((el) => el.getClientRects().length);
  items.push(topbar().querySelector('.menu'));
  const i = items.indexOf(document.activeElement);
  const next = e.shiftKey ? (i <= 0 ? items.length - 1 : i - 1) : (i < 0 || i === items.length - 1 ? 0 : i + 1);
  e.preventDefault();
  items[next].focus();
}

// ------------------------------------------------------------------ pages

const PAGES = {
  overview: renderOverview,
  spend: renderSpend,
  sessions: renderSessions,
  session: (main, r) => (r.rest[1] === 'agent' ? renderAgent(main, r) : renderSession(main, r)),
  day: renderDay,
  spikes: renderSpikes,
  checks: renderChecks,
  changes: (main, r, live) => (r.arg ? renderChange(main, r, live) : renderChanges(main, r, live)),
  limits: (main, r) => (r.arg === 'window' ? renderLimitWindow(main, r) : renderLimits(main, r)),
};

// Draw the page of the URL (router.js: at once, from the first data there is,
// then again as the server's partial replies come).
function renderPage() { render(); }

window.addEventListener('capcom-route', () => { drawSidebar(); renderPage(); });
// a mark was written: redraw the page and the sidebar's count
window.addEventListener('capcom-changed', () => { renderPage(); refreshStatus(); });

startRouter({
  pages: PAGES,
  route,
  onNavigate: () => { setMenu(false, false); drawSidebar(); },
  decorate: (next) => {
    if (statusData && statusData.scanning) next.prepend(scanningNotice());
    next.inert = menuOpen; // behind the open menu sheet
  },
});
drawSidebar();
renderPage();
// While serve's first scan runs, the totals are partial: say so, and redraw once
// the scan is done.
function scanningNotice() {
  return h('p.notice', { role: 'status' }, h('strong', { text: 'Reading the logs. ' }),
    'capcom serve is reading the logs into the index now, so the totals below are partial. This page redraws when the first scan is done.');
}

// A failed /api/status is retried, 2 s doubling up to 30 s, so a server that
// was busy or restarting still gets its scanning notice shown and cleared.
let statusRetry = 2e3;
async function refreshStatus() {
  const was = statusData && statusData.scanning;
  try {
    statusData = await api('status', true);
    statusRetry = 2e3;
  } catch (e) { // keep the last one, and ask again soon
    setTimeout(refreshStatus, statusRetry);
    statusRetry = Math.min(30e3, statusRetry * 2);
    return;
  }
  drawSidebar();
  if (statusData && statusData.scanning) {
    if (!was) renderPage();
    setTimeout(refreshStatus, 10e3);
  } else if (was) {
    forget();
    renderPage();
  }
}

// ------------------------------------------------------------------ colors

// One color per project, agent type and model (/api/colors): asked for at start and
// whenever the index changed; the page is drawn again when the assignment did.
async function refreshColors() {
  let data;
  try { data = await api('colors', true); } catch (e) { return; } // keep the last one
  if (useColors(data)) renderPage();
}

// ------------------------------------------------------------------ live pages

// Every 10 s while the tab is visible (and at once when it shows again): the live
// list, and /api/version, whose parts say what changed on the server. When a part
// the current view reads changed, the view is fetched again and swapped in place
// (router.js refreshView), and the sidebar's "Index updated" moves on. A new
// `launch` is a restarted server: its write token is new too (marks.js).
const READS = { overview: ['index', 'samples', 'marks'], limits: ['index', 'samples'], checks: ['index', 'marks'], changes: ['index', 'marks'] };
let seen = null;

async function watchData() {
  if (document.hidden) return;
  let v;
  try { v = await api('version', true); } catch (e) { return; }
  const before = seen;
  seen = v;
  if (!before) return;
  if (v.launch !== before.launch) freshToken();
  if (v.updated && statusData && statusData.index && statusData.index.updated !== v.updated) {
    statusData.index.updated = v.updated;
    drawSidebar();
  }
  if (before.index !== v.index) refreshColors();
  const parts = READS[route().name] || ['index'];
  if (parts.some((k) => before[k] !== v[k])) {
    if (before.marks !== v.marks) forget(); // a mark shows on several pages' replies
    refreshView();
  }
}

// A page left open past midnight: every kept reply counted its days up to the old
// date ("Today", the ranges' first day; the server keys its cache by the date too),
// so drop them and fetch the page again for the new day.
const localDay = () => new Date().toDateString();
let day = localDay();
function watchDay() {
  if (localDay() === day) return;
  day = localDay();
  forget();
  refreshView();
}

function tickLive() { if (!document.hidden) { watchDay(); refreshLive(); watchData(); } }

refreshStatus();
refreshColors();
refreshLive();
watchData();
setInterval(tickLive, 10e3);
document.addEventListener('visibilitychange', () => { if (!document.hidden) tickLive(); });
