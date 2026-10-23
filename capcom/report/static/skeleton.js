// Loading placeholders: each page's outline while its first reply computes (the
// same cards, chart areas and rows as the page, so nothing jumps when the data
// lands), the outline of the parts a page fills in later, and the small ones the
// sidebar and the heatmap's hour panel show.
//
// Text and number blocks (`.sk`) share one soft light sweeping across them all:
// a gradient positioned against the viewport (background-attachment: fixed) whose
// animation every block runs in the same phase (inPhase()), so the sweep runs
// through the page as one band. A chart's place shows a faint ghost of its kind: a
// cumulative curve that keeps drawing itself, bars rising in a slow wave, heatmap
// cells fading in a ripple, a wave of dots for the limits. When the reply lands,
// grow() lets the real chart grow out of where its ghost was. Reduced motion:
// still outlines (skeleton.css).

import { h } from './lib.js';
import { reducedMotion } from './loading.js';

// Every placeholder animation runs in phase with the page's clock: a block made
// at time t starts its cycles -(t mod PERIOD) into them (the --sk-t delay set on
// each placeholder's root), so blocks drawn at different times (the page
// outline, a tail added later, the sidebar) sweep and wave as one, and a redraw
// does not restart them. No script touches the animations after.
const PERIOD = 7200; // ms: a whole number of every cycle (2.4 s and 3.6 s)
export function inPhase(el) {
  const t = typeof performance === 'object' ? performance.now() : 0;
  el.style.setProperty('--sk-t', `${-(t % PERIOD).toFixed(0)}ms`);
  // the first frame paints the blocks plain, the sweep and the waves start on
  // the next: the first paint stays as quick as a still outline's
  el.setAttribute('data-sk-wait', '');
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => setTimeout(() => el.removeAttribute('data-sk-wait'), 0));
  return el;
}

// ------------------------------------------------------------------ blocks

// A text or number block, `w` wide (a % of its row, or a CSS length).
const W = (w) => (typeof w === 'number' ? `${w}%` : w);
export const line = (w, cls = '') => h('span.sk' + cls, { style: `width:${W(w)}` });
const label = (w = 40) => line(w, '.sk-label');
const note = (w = 90) => line(w, '.sk-note');
const big = (w = 45) => line(w, '.sk-big');
const notes = (...ws) => h('div.sk-lines', null, ws.map((w) => note(w)));
const pill = (w) => h('span.sk.sk-pill', { style: `width:${W(w)}` });
const round = (d) => h('span.sk.sk-round', { style: `width:${d}px;height:${d}px` });
const title = (text) => h('h2', { text });
const titleSmall = (text) => h('h2.small', { text });

// The page title and its line below; `controls`: pill widths (range buttons...).
// `under`: the controls sit below the titles, as on pages with a long line.
function head(text, subs = [320], controls = [], under = false) {
  return h('header.head', null,
    h('div.titles', { style: under ? 'flex:1 1 100%' : null }, text ? h('h1', { text }) : line('240px', '.sk-h1'), ...subs.map((w) => line(`${w}px`, '.sk-sub'))),
    controls.length ? h('div.controls.sk-controls', null, controls.map((w) => pill(w))) : null);
}

const story = () => h('div.sk-story', null, line('72%', '.sk-sub'));

// A legend: `n` short swatch-and-label pairs.
const legend = (n, w = 70) => h('div.sk-legend', null, Array.from({ length: n }, (_, i) => h('span', null, h('i.sk'), line(`${w - (i * 13) % 30}px`, '.sk-note'))));

// Rows of a bar list: a label and an amount over a track.
function barRows(n, widths = [92, 70, 44, 26, 18, 12]) {
  return h('div.blist.sk-blist', null, Array.from({ length: n }, (_, i) => h('div.sk-brow', null,
    h('div.sk-split', null, line(`${30 + (i * 17) % 35}%`, '.sk-note'), line('44px', '.sk-note')),
    h('div.track.sk-track', null, h('span.sk', { style: `width:${widths[i % widths.length]}%` })))));
}

// Rows of a table: a two-line title and `cols` numbers.
function tableRows(n, cols = 5) {
  return h('div.sk-table', null,
    h('div.sk-tr.sk-th', null, line('12%', '.sk-note'), Array.from({ length: cols }, () => line('7%', '.sk-note'))),
    Array.from({ length: n }, (_, i) => h('div.sk-tr', null,
      h('div.sk-cell1', null, line(`${48 + (i * 23) % 40}%`, '.sk-body'), line(`${22 + (i * 11) % 20}%`, '.sk-note')),
      Array.from({ length: cols }, (_, j) => line(`${5 + ((i + j) * 7) % 5}%`, '.sk-note')))));
}

// A list of items with a number disc on the left (spikes, why it jumped).
function discRows(n) {
  return h('div.sk-discs', null, Array.from({ length: n }, (_, i) => h('div.sk-disc', null, round(26),
    h('div.sk-lines', null, line('22%', '.sk-note'), line(`${70 - i * 9}%`, '.sk-note'), line(`${58 - i * 7}%`, '.sk-body'), line('34%', '.sk-note')))));
}

// ------------------------------------------------------------------ chart ghosts

// The cumulative curves (Overview, Spend, a day): three grid lines, y labels, a
// dim area and its curve drawing itself from the left, x labels below.
const CURVES = [
  'M0 296 C120 295 230 294 300 286 S390 240 450 214 S560 170 620 150 S760 104 840 78 S950 30 1000 22',
  'M0 296 C160 294 300 290 380 270 S470 200 520 188 S640 150 700 122 S820 80 880 70 S960 42 1000 34',
];
export function ghostLine(height = 300, variant = 0, xLabels = 7) {
  const d = CURVES[variant % CURVES.length];
  const svg = `<svg viewBox="0 0 1000 300" preserveAspectRatio="none" aria-hidden="true">`
    + `<path class="sk-area" d="${d} L1000 300 L0 300Z"/>`
    + `<path class="sk-curve" d="${d}" vector-effect="non-scaling-stroke"/></svg>`;
  const plot = h('div.sk-plot', { style: `height:${height}px` },
    [0, 50, 100].map((y) => h('div.gridline', { style: `top:${y}%` })),
    [0, 50, 100].map((y) => h('span.sk.sk-ylab', { style: `top:calc(${y}% - 5px)` })));
  const draw = h('div.sk-draw');
  draw.innerHTML = svg;
  plot.append(draw);
  return h('div.sk-chart', null, plot,
    h('div.sk-xaxis', null, Array.from({ length: xLabels }, () => line('34px', '.sk-note'))));
}

// Bars rising and settling in a slow wave (Spend's days, context sizes).
const PROFILE = [0.12, 0.08, 0.1, 0.14, 0.1, 0.2, 0.32, 0.45, 0.95, 0.66, 0.82, 0.86, 0.58, 0.16, 0.3, 0.5];
export function ghostBars(n = 14, height = 200, { pairs = false, labels = true } = {}) {
  const bars = h('div.sk-bars' + (pairs ? '.pairs' : ''), { style: `height:${height}px` },
    Array.from({ length: n }, (_, i) => {
      const f = pairs ? [0.15, 0.62, 1, 0.82, 0.16, 0.1][Math.floor(i / 2) % 6] * (i % 2 ? 0.8 : 1) : PROFILE[i % PROFILE.length];
      return h('i', { style: `--i:${i};height:${Math.max(6, f * 92).toFixed(0)}%` });
    }));
  return h('div.sk-chart', null, bars,
    labels ? h('div.sk-xaxis', null, Array.from({ length: pairs ? n / 2 : n }, () => line('24px', '.sk-note'))) : null);
}

// A weekday-by-hour grid whose cells fade in a ripple from the top left.
export function ghostHeat(rows = 7, cols = 24) {
  return h('div.scroll', null, h('div.heat.sk-heat', { 'aria-hidden': 'true' },
    Array.from({ length: rows }, (_, r) => h('div.hr', null, h('span', null, line('24px', '.sk-note')),
      Array.from({ length: cols }, (_, c) => h('span.cell', { style: `--r:${r};--c:${c}` })))),
    h('div.hx', null, ['', '', '', ''].map(() => h('span', null, line('14px', '.sk-note'))))));
}

// The limits' readings: a wave of dots over a rising line, with the grid of
// percentages and the now line.
export function ghostDots(height = 180, n = 44) {
  const plot = h('div.sk-plot.sk-dotplot', { style: `height:${height}px` },
    [0, 50, 100].map((y) => h('div.gridline', { style: `top:${y}%` })),
    [0, 50, 100].map((y) => h('span.sk.sk-ylab', { style: `top:calc(${y}% - 5px)` })));
  const dots = h('div.sk-dots');
  for (let i = 0; i < n; i++) {
    const x = i / (n - 1);
    const y = 8 + 70 * Math.pow(x, 1.4) + 4 * Math.sin(i * 0.9);
    dots.append(h('i', { style: `--i:${i};left:${(x * 100).toFixed(2)}%;bottom:${y.toFixed(1)}%` }));
  }
  plot.append(dots);
  return h('div.sk-chart', null, plot, h('div.sk-xaxis', null, Array.from({ length: 5 }, () => line('34px', '.sk-note'))));
}

// A gauge ring (a limit's share used) with a dim arc going round.
const ring = () => h('span.sk-ring');

// ------------------------------------------------------------------ cards

const card = (cls, ...kids) => h('section.card.sk-card' + cls, null, ...kids);

function limitCard(name) {
  return card('', h('div.sk-limtop', null, ring(), h('div.sk-lines', null, line(`${name.length * 7}px`, '.sk-label'), big(38))),
    notes(94, 52), ghostDots(130, 36), notes(96, 88, 40), line('34%', '.sk-note'));
}

function kpiCard(w) {
  return card('.tight', label(w), big(50), notes(92, 80, 54), notes(88, 50));
}

function checkRows(n) {
  return h('div.blist.sk-blist', null, Array.from({ length: n }, (_, i) => h('div.sk-split', null,
    h('div.sk-lines', { style: 'flex:1' }, line(`${62 - i * 8}%`, '.sk-body'), line(`${44 - i * 5}%`, '.sk-note')),
    line('80px', '.sk-note'))));
}

// ------------------------------------------------------------------ pages

function greeting(d) {
  const hr = d.getHours();
  return hr < 5 ? 'Good night' : hr < 12 ? 'Good morning' : hr < 18 ? 'Good afternoon' : 'Good evening';
}

// Each page: its header (`head`) and its body blocks in order; `later`: the index
// of the first body block the page draws after the rest (a reply it waits on
// after drawing the others), or 0 when it draws everything at once.
const PAGES = {
  overview: () => ({
    head: head(greeting(new Date()), [260], ['136px']),
    body: [
      story(),
      h('div.grid', { style: '--min:440px' }, limitCard('5-hour window'), limitCard('Weekly limit')),
      h('div.grid', { style: '--min:220px' }, kpiCard(62), kpiCard(40), kpiCard(46), kpiCard(50)),
      h('div.row-wrap', null,
        card('.grow2', h('div.sk-split', null, title("Today's spend, adding up"), pill('320px')), legend(5), ghostLine(300, 0, 6), notes(90, 40)),
        card('.grow1', title('Why it jumped'), discRows(2))),
      overviewBottom(),
    ],
    later: 4,
  }),
  spend: () => ({
    head: head('Spend', [340], ['258px', '272px'], true),
    body: [
      story(),
      card('', h('div.sk-split', null, big('180px'), legend(4)), ghostLine(290, 1, 7), note(70)),
      h('div.row-wrap', null,
        card('.grow3', h('div.sk-split', null, title('Per day'), line('170px', '.sk-note')), ghostBars(14, 200)),
        card('.grow2b', title('Tokens are not dollars'), legend(4), label(28), h('span.sk.sk-thick'), label(26), h('span.sk.sk-thick'), notes(96, 90, 70))),
      h('div.row-wrap', null,
        card('.grow3', h('div.sk-split', null, titleSmall('When you spend'), pill('180px')), note(64), ghostHeat()),
        card('.grow2b', titleSmall('Context size when you paid'), legend(2), ghostBars(12, 130, { pairs: true }), notes(92, 60))),
      card('', h('div.sk-split', null, titleSmall('Costliest sessions'), line('90px', '.sk-note')), tableRows(8, 6)),
    ],
  }),
  sessions: () => ({
    head: head('Sessions', [200], ['370px']),
    body: [card('', tableRows(14, 6))],
  }),
  spikes: () => ({
    head: head('Spikes', [420, 300], ['369px'], true),
    body: [
      h('div.sk-chips', null, [150, 120, 90, 130, 100, 120, 110, 70].map((w) => pill(`${w}px`))),
      h('div.row-wrap', null, card('.grow1', big('140px'), barRows(6)), card('.grow1', big('160px'), barRows(6, [70, 52, 40, 30, 22, 14]))),
      h('div.row-wrap', { style: 'align-items:flex-start' },
        h('div.sk-col', null, pill('200px'), card('', discRows(4))),
        card('.grow2', big('50%'), notes(80, 60), ghostLine(220, 1, 5), notes(94, 86, 40))),
    ],
  }),
  checks: () => ({
    head: head('Checks', [380, 240], ['228px']),
    body: [
      card('.tight', label(30), big(55), note(40)),
      notes(96, 70),
      card('', title('By kind'), note(50), legend(6), checkRows(5)),
    ],
  }),
  changes: () => ({
    head: head('Did it work?', [420, 260]),
    body: [
      h('div.row-wrap', null, card('.tight.grow1', label(40), big(35), notes(80)), card('.tight.grow1', label(30), big(25), notes(70))),
      h('div.sk-col', null, [0, 1, 2].map((i) => card('.tight', h('div.sk-split', null, line(`${50 - i * 8}%`, '.sk-body'), pill('90px')), notes(70 - i * 6)))),
    ],
  }),
  change: () => ({
    head: null,
    body: [
      line('110px', '.sk-note'),
      head(null, [360]),
      h('div.grid', { style: '--min:220px' }, kpiCard(50), kpiCard(40), kpiCard(46)),
      h('div.row-wrap', { style: 'align-items:flex-start' }, card('.grow1', big('40%'), ghostBars(16, 160)), card('.grow1', big('36%'), notes(90, 80, 60))),
    ],
  }),
  limits: () => ({
    head: head('Limits', [420, 330]),
    body: [limitsCard(180), limitsCard(210)],
  }),
  session: () => ({
    head: h('header.sk-sessionhead', null, line('120px', '.sk-note'), big('55%'), line('38%', '.sk-sub'), h('div.sk-chips', null, [90, 120, 80].map((w) => pill(`${w}px`)))),
    body: [
      h('div.grid', { style: '--min:180px' }, kpiCard(50), kpiCard(46), kpiCard(40), kpiCard(54)),
      card('', big('30%'), ghostLine(220, 0, 6)),
      h('div.row-wrap', { style: 'align-items:flex-start' }, card('.grow1', big('40%'), discRows(2)), card('.grow1', big('44%'), barRows(4))),
      card('', big('20%'), tableRows(5, 5)),
    ],
  }),
  day: () => ({
    head: head(null, [300]),
    body: [
      story(),
      card('', h('div.sk-split', null, title('The day, adding up'), pill('320px')), legend(4), ghostLine(260, 1, 7)),
      h('div.row-wrap', null, card('.grow1', titleSmall('Why it jumped'), discRows(2)), card('.grow1', titleSmall('Where it went'), barRows(5))),
      card('', big('16%'), tableRows(5, 6)),
    ],
  }),
};

function overviewBottom() {
  return h('div.grid', { style: '--min:300px' },
    card('', titleSmall('By project'), barRows(4)),
    card('', titleSmall('By agent type'), barRows(3, [100, 92, 3])),
    card('', h('div.sk-split', null, titleSmall('Worth changing'), line('64px', '.sk-note')), checkRows(3)));
}

function limitsCard(height) {
  return card('', h('div.sk-split', null, big('140px'), line('80px', '.sk-note')), note(60), ghostDots(height),
    notes(92, 60), line('24%', '.sk-body'), barRows(6), line('20%', '.sk-body'), legend(7), barRows(6));
}

const pageOf = (r) => (r.name === 'changes' && r.arg ? 'change' : r.name);

// The placeholder <main> for route `r`, in place of the page until it draws.
export function skeletonMain(r) {
  const p = (PAGES[pageOf(r)] || PAGES.sessions)();
  return inPhase(h('main', { id: 'main', class: 'panel', 'data-skeleton': '', 'aria-busy': 'true' }, p.head, ...p.body));
}

// The outline of what page `r` has still to draw, below the part it has drawn
// (a card whose reply is still computing).
export function skeletonTail(r) {
  const p = (PAGES[pageOf(r)] || PAGES.sessions)();
  return inPhase(h('div.skel-tail', { 'aria-hidden': 'true' }, ...p.body.slice(p.later || 0)));
}

// The sidebar's live list before its first reply: rows shaped like its sessions.
export function liveRows(n = 3) {
  return inPhase(h('div.sk-live', { 'aria-hidden': 'true' }, Array.from({ length: n }, (_, i) => h('div.sk-liverow', null,
    h('span.sk.sk-dot'), h('span.sk-lines', null, line(`${78 - i * 14}%`, '.sk-body'), line(`${46 - i * 8}%`, '.sk-note')), line('34px', '.sk-note')))));
}

// The heatmap's hour panel while its hour loads: the split by kind and two lists.
export function hourSkeleton() {
  return inPhase(h('div.sk-hour', { 'aria-hidden': 'true' }, h('span.sk.sk-thick'), legend(4),
    h('div.hm-cols', null, h('div', null, label(30), barRows(3)), h('div', null, label(36), barRows(3)))));
}

// ------------------------------------------------------------------ growing in

// The real charts in `root`, put in where ghosts were: lines and areas drawn in
// from the left, day bars rising in a wave, heatmap cells in a ripple, list bars
// growing from their start. Web Animations, so nothing stays on the elements.
export function grow(root) {
  if (reducedMotion() || !root || typeof root.querySelectorAll !== 'function') return;
  const ease = 'cubic-bezier(0.22, 0.8, 0.24, 1)';
  const anim = (el, frames, opts) => {
    if (el.closest('.skel-tail, .sk-chart, .sk-heat')) return;
    try { el.animate(frames, { duration: 650, easing: ease, fill: 'backwards', ...opts }); } catch (e) { /* no animations */ } };
  // a plot (its curves, marks and labels) uncovered from the left, its labels
  // outside the box included; a lone SVG chart (a sparkline) the same way
  const plots = [...root.querySelectorAll('.chart')];
  for (const svg of root.querySelectorAll('.card svg')) {
    if (!svg.closest('.chart, button, .legend') && svg.getBoundingClientRect().width >= 60) plots.push(svg);
  }
  for (const el of plots) {
    if (el.parentElement && el.parentElement.closest('.chart')) continue;
    anim(el, [{ clipPath: 'inset(-40px 100% -40px -80px)' }, { clipPath: 'inset(-40px -40px -40px -80px)' }], { duration: 900 });
  }
  root.querySelectorAll('.days .bar').forEach((bar, i) => {
    bar.style.transformOrigin = '50% 100%';
    anim(bar, [{ transform: 'scaleY(0)' }, { transform: 'scaleY(1)' }], { delay: i * 28 });
  });
  root.querySelectorAll('.heat .cell').forEach((cell) => {
    const row = cell.parentNode, r = [...row.parentNode.children].indexOf(row), c = [...row.children].indexOf(cell);
    anim(cell, [{ opacity: 0 }, { opacity: 1 }], { duration: 420, delay: (r + c) * 14 });
  });
  root.querySelectorAll('.track > div, .stackbar > *').forEach((bar, i) => {
    bar.style.transformOrigin = '0 50%';
    anim(bar, [{ transform: 'scaleX(0)' }, { transform: 'scaleX(1)' }], { delay: Math.min(i, 12) * 20 });
  });
}
