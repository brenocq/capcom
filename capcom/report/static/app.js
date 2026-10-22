// The frame: sidebar (navigation, session search, live sessions, index status,
// theme toggle) and a router from the URL path to a page.

import { h, s, api, stamp, sessionHref, errorBox, forget } from './lib.js';
import { renderOverview } from './overview.js';
import { renderSpend } from './spend.js';
import { renderSession } from './session.js';
import { renderDay } from './day.js';
import { renderSessions } from './sessions.js';
import { renderSpikes } from './spikes.js';
import { renderChecks } from './checks.js';
import { renderLimits } from './limits.js';
import { renderChanges, renderChange } from './changes.js';

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

function icon(d, size = 16) {
  return s('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.8, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' }, s('path', { d }));
}

function mark() {
  const st = { stroke: '#D97757' };
  return s('svg', { width: 22, height: 22, viewBox: '0 0 24 24', fill: 'none', 'stroke-width': 2, 'stroke-linecap': 'round', 'aria-hidden': 'true', style: 'stroke:#D97757' },
    s('circle', { cx: 12, cy: 12, r: 2.4, fill: '#D97757', stroke: 'none' }),
    ...['M8 8a5.7 5.7 0 0 0 0 8', 'M16 8a5.7 5.7 0 0 1 0 8', 'M5 5a9.9 9.9 0 0 0 0 14', 'M19 5a9.9 9.9 0 0 1 0 14'].map((d) => s('path', { d, ...st })));
}

function route() {
  const parts = location.pathname.replace(/^\/+|\/+$/g, '').split('/');
  const name = parts[0] || 'overview';
  return { name, arg: parts[1] ? decodeURIComponent(parts[1]) : null, query: new URLSearchParams(location.search) };
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
  const nav = document.getElementById('sidebar');
  const input = h('input', { id: 'capcom-find', type: 'search', placeholder: 'Project, title or agent type', value: r.name === 'sessions' ? r.query.get('q') || '' : '' });
  const form = h('form.find', { role: 'search', onsubmit: (e) => { e.preventDefault(); location.href = '/sessions?q=' + encodeURIComponent(input.value.trim()); } },
    h('label', { for: 'capcom-find', text: 'Find a session' }), input);
  nav.replaceChildren(
    h('a.brand', { href: '/' }, mark(), h('span', { text: 'capcom' })),
    form,
    h('div.nav', null, NAV.map(([id, href, label, soon]) => h('a', { href, 'aria-current': id === active ? 'page' : null },
      icon(ICON[id]), h('span', { text: label }), soon ? h('span.soon', { text: soon, title: `Coming in milestone ${soon.slice(1)}` }) : null,
      id === 'changes' && statusData && statusData.changes ? h('span.soon', { text: String(statusData.changes), title: `${statusData.changes} change${statusData.changes === 1 ? '' : 's'} marked as applied` }) : null))),
    liveList(),
    footer(),
  );
}

function liveList() {
  const box = h('div.live', null, h('div.side-head', null, h('span', { text: 'Live now' }), h('span', { text: '$/hour' })));
  if (!liveData) { box.append(h('span.note', { style: 'padding:0 10px', text: 'Loading…' })); return box; }
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
    const sub = sn.agents_active ? `main + ${sn.agents_active} agent${sn.agents_active > 1 ? 's' : ''}` : (sn.title ? sn.project : 'main');
    box.append(h('a', { href: sessionHref(sn.session_id), title: `Last record ${stamp(new Date(sn.last_seen))}` },
      h('span', { class: hot ? 'dot hot' : 'dot' }),
      h('span.name', null, h('span', { text: sn.title || sn.project }), h('span.note', { text: sub })),
      h('span', { class: hot ? 'rate hot' : 'rate', text: sn.usd_per_hour.toFixed(2) })));
  }
  return box;
}

function footer() {
  let status = 'Reading the index…';
  if (statusData && statusData.index) {
    const d = new Date(statusData.index.updated);
    status = `Index updated ${stamp(d)} · ${statusData.sessions.toLocaleString('en-US')} sessions · ${(statusData.index.size_bytes / 1e6).toFixed(0)} MB`;
  }
  const dark = isDark();
  return h('div.foot', null,
    h('div.row', null, icon('M8 11V8a4 4 0 0 1 8 0v3M5 11h14v10H5z', 14), h('span', { text: 'Local and read-only. No network.' })),
    h('span', { text: status }),
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

// ------------------------------------------------------------------ pages

const PAGES = {
  overview: renderOverview,
  spend: renderSpend,
  sessions: renderSessions,
  session: renderSession,
  day: renderDay,
  spikes: renderSpikes,
  checks: renderChecks,
  changes: (main, r, live) => (r.arg ? renderChange(main, r, live) : renderChanges(main, r, live)),
  limits: renderLimits,
};

let renderToken = 0;

async function renderPage() {
  const r = route();
  const main = document.getElementById('main');
  const page = PAGES[r.name];
  const token = ++renderToken;
  // Draw into a detached node so a redraw (theme switch) never flashes empty.
  const next = h('main', { id: 'main', class: 'panel', 'aria-live': 'polite' });
  if (!main.childElementCount) main.replaceChildren(h('p.loading', { text: 'Loading…' }));
  try {
    await page(next, r, () => token === renderToken);
  } catch (err) {
    next.replaceChildren(errorBox(err));
  }
  if (statusData && statusData.scanning) next.prepend(scanningNotice());
  if (token === renderToken) main.replaceWith(next);
}

window.addEventListener('capcom-route', () => { drawSidebar(); renderPage(); });
// a mark was written: redraw the page and the sidebar's count
window.addEventListener('capcom-changed', () => { renderPage(); refreshStatus(); });

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

refreshStatus();
refreshLive();
setInterval(refreshLive, 30e3);
