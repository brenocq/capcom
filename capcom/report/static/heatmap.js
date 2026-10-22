// The "When you spend" card on Spend: the spend by weekday and hour over the
// window ("Typical week") or hour by hour over the past seven dates ("Past 7
// days"). Each cell is a button: hover or focus shows its dollars and costliest
// project, a click (or Enter) opens a panel under the grid that says why the hour
// cost what it did (/api/hour). A typical-week cell first lists the dates behind
// it. The view and the open hour live in the URL (`heat=week`, `hour=<ISO>`).

import {
  h, api, usd, pct, two, monthDay, barList, sessionHref, spikeHref, spikeText, projectChip,
  showTip, hideTip, errorBox, CATEGORY_LABELS, CATEGORY_ORDER, categoryColor,
} from './lib.js';
import { heatColor } from './charts.js';

const HOUR = 3600e3;
const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const LONG = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const VIEWS = [['typical', 'Typical week'], ['week', 'Past 7 days']];

// ------------------------------------------------------------------ grids (pure)

const isoDay = (d) => `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}`;
export const rowLabel = (d) => `${SHORT[d.getDay()]} ${monthDay(d)}`;
const weekdayOf = (d) => (d.getDay() + 6) % 7;  // Mon = 0

// The past `days` local dates, oldest first, each a row of 24 clock hours. A cell
// holds the absolute hours that read that clock hour on that date (`starts`, ms):
// none on the night clocks spring forward, two on the night they fall back. Rows
// start at local midnight (built from the calendar date, never by adding 24 h), so
// a daylight-saving change in the week shifts no hour. Hours after `now` are
// `future`; the hour holding `now` is `partial`. `hours` is /api/spend's
// heatmap.hours: [{start: ISO, usd, project}].
export function pastWeekGrid(hours, now, days = 7) {
  const spent = new Map(hours.map((x) => [Date.parse(x.start), x]));
  const today = new Date(now);
  const day = (k) => new Date(today.getFullYear(), today.getMonth(), today.getDate() + k);
  const rows = [];
  for (let i = 0; i < days; i++) {
    const date = day(i - days + 1);
    rows.push({
      date, label: rowLabel(date), today: i === days - 1,
      cells: Array.from({ length: 24 }, (_, hour) => ({ hour, starts: [], usd: 0, project: null, top: 0, future: false, partial: false, missing: false })),
    });
  }
  const rowOf = new Map(rows.map((r, i) => [isoDay(r.date), i]));
  const first = rows[0].date.getTime(), end = day(1).getTime();
  for (let t = first; t < end; t += HOUR) {
    const local = new Date(t);
    const r = rowOf.get(isoDay(local));
    if (r === undefined) continue;
    const cell = rows[r].cells[local.getHours()];
    cell.starts.push(t);
    const x = spent.get(t);
    if (x && x.usd > 0) {
      cell.usd += x.usd;
      if (x.usd > cell.top) { cell.top = x.usd; cell.project = x.project || null; }
    }
    if (t <= now && now < t + HOUR) cell.partial = true;
  }
  let max = 0, total = 0;
  for (const row of rows) {
    for (const c of row.cells) {
      c.missing = c.starts.length === 0;
      c.future = !c.missing && c.starts.every((t) => t > now);
      max = Math.max(max, c.usd);
      total += c.usd;
    }
  }
  return { rows, max, total };
}

// The hours with spend behind a typical-week cell (weekday Mon = 0, clock hour),
// newest first: [{start: ms, usd, project}].
export function datesBehind(hours, weekday, hour) {
  return hours.map((x) => ({ ...x, start: Date.parse(x.start) }))
    .filter((x) => { const d = new Date(x.start); return weekdayOf(d) === weekday && d.getHours() === hour; })
    .sort((a, b) => b.start - a.start);
}

// ------------------------------------------------------------------ tooltip adapter

// A minimal local tooltip (lib.js's), behind one function so the card can move to
// a shared hover tooltip later: `at` is a pointer event, or the element itself on
// keyboard focus.
const tooltip = {
  show(at, title, rows) {
    if (at instanceof Element) {
      const r = at.getBoundingClientRect();
      showTip({ clientX: r.left + r.width / 2, clientY: r.bottom }, title, rows);
    } else showTip(at, title, rows);
  },
  hide: hideTip,
};

// ------------------------------------------------------------------ the card

const hourText = (d) => `${two(d.getHours())}:00–${two((d.getHours() + 1) % 24)}:00`;
const isoOf = (ms) => {
  // the hour's start with its local UTC offset, as the API writes it
  const d = new Date(ms), off = -d.getTimezoneOffset();
  const sign = off >= 0 ? '+' : '-', a = Math.abs(off);
  return `${isoDay(d)}T${two(d.getHours())}:00:00${sign}${two(Math.floor(a / 60))}:${two(a % 60)}`;
};

function readUrl() {
  const q = new URLSearchParams(location.search);
  const hour = q.get('hour');
  return { view: q.get('heat') === 'week' ? 'week' : 'typical', hour: hour ? Date.parse(hour) : null };
}

function writeUrl(params) {
  const q = new URLSearchParams(location.search);
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === '') q.delete(k); else q.set(k, v);
  }
  const qs = q.toString();
  history.replaceState(history.state, '', location.pathname + (qs ? '?' + qs : ''));
}

function scale(max, week) {
  const sw = (cls, style, label) => h('span.hm-key', null, h('i' + cls, { style }), label);
  return h('span.scale.hm-scale', null,
    sw('', 'background:var(--subtle)', '$0'),
    h('span.hm-ramp', { title: `From a cent to ${usd(max)} an hour` },
      [0.02, 0.12, 0.3, 0.6, 1].map((x) => h('i', { style: `background:${heatColor(x)}` }))),
    h('span', { text: usd(max, max >= 100 ? 0 : 2) }),
    week ? sw('.hm-future', null, 'not yet') : null);
}

// heat: /api/spend's heatmap; now: the reply's window.until (ms); sentence(heat):
// the typical week's line under the grid.
export function heatCard(heat, now, sentence) {
  const card = h('section.card.grow3.hm-card');
  const state = readUrl();
  const hours = heat.hours || null;
  if (!hours) state.view = 'typical';  // an older server: no hour list
  const draw = () => {
    tooltip.hide();
    const week = state.view === 'week';
    const grid = week ? pastWeekGrid(hours, now) : typicalGrid(heat);
    const views = hours ? h('div.seg', { role: 'group', 'aria-label': 'Heatmap view' },
      VIEWS.map(([v, text]) => h('button', {
        type: 'button', text, 'aria-pressed': String(v === state.view),
        // a new view starts with nothing picked: the other view's open hour (its panel and
        // its cell's outline) would otherwise carry over
        onclick: () => { state.view = v; state.hour = null; writeUrl({ heat: v === 'week' ? 'week' : null, hour: null }); draw(); },
      }))) : null;
    const panel = h('div.hm-panel', { 'aria-live': 'polite' });
    const open = (cell, row) => {
      if (week) openHour(cell.starts.find((t) => t <= now) ?? cell.starts[0]);
      else openDates(cell, row);
    };
    const openHour = (start, fromUrl = false) => {
      state.hour = start;
      writeUrl({ hour: isoOf(start) });
      mark();
      hourPanel(panel, start, () => { state.hour = null; writeUrl({ hour: null }); mark(); panel.replaceChildren(); })
        .then(() => { if (!fromUrl) panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); });
    };
    const openDates = (cell, row) => {
      state.hour = null;
      writeUrl({ hour: null });
      datesPanel(panel, cell, row, hours, openHour, () => panel.replaceChildren());
    };
    const { el, cells } = gridEl(grid, week, open);
    const mark = () => cells.forEach(({ btn, cell }) => btn.classList.toggle('on', state.hour !== null && cell.starts.includes(state.hour)));
    card.replaceChildren(
      h('div.split', null, h('h2.small', { text: 'When you spend' }), views),
      h('div.split.hm-sub', null,
        h('span.note', { text: week ? 'Hour by hour, the past seven days. Pick an hour to see why it cost what it did.' : 'Each weekday and hour, summed over the window. Pick one to see the dates behind it.' }),
        scale(grid.max, week)),
      el,
      h('p.para', { text: week ? weekSentence(grid) : sentence(heat) }),
      panel);
    mark();
    if (state.hour !== null && !Number.isNaN(state.hour)) openHour(state.hour, first);
    first = false;
  };
  let first = true;
  draw();
  return card;
}

// The typical week in the past-week grid's shape: one cell per weekday and hour.
function typicalGrid(heat) {
  const total = heat.cells.flat().reduce((a, b) => a + b, 0);
  const rows = heat.cells.map((row, r) => ({
    label: heat.weekdays[r] || WEEKDAYS[r], weekday: r,
    cells: row.map((v, hour) => ({
      hour, usd: v, project: heat.top_projects ? heat.top_projects[r][hour] : null,
      starts: heat.hours ? datesBehind(heat.hours, r, hour).map((x) => x.start) : [],
    })),
  }));
  return { rows, max: heat.max, total };
}

// The grid of buttons, with one tab stop: the arrow keys move between cells.
function gridEl(grid, week, open) {
  const cells = [];
  const btns = grid.rows.map(() => []);
  let stop = null;
  const rows = grid.rows.map((row, r) => h('div.hr', null,
    h('span', { text: row.label, class: row.today ? 'hm-today' : null }),
    row.cells.map((cell, hr) => {
      if (cell.missing) {
        const gap = h('span.cell.hm-missing', { title: 'No such hour: the clocks moved forward' });
        btns[r][hr] = null;
        return gap;
      }
      const when = week ? `${LONG[row.date.getDay()]} ${monthDay(row.date)}` : `${LONG[(row.weekday + 1) % 7]}s`;
      const span = `${two(hr)}:00 to ${two((hr + 1) % 24)}:00`;
      let aria, style = '';
      if (cell.future) {
        aria = `${when}, ${span}: not yet`;
      } else {
        const share = grid.total ? `, ${pct(cell.usd / grid.total, 1)} of the ${week ? 'week' : 'window'}` : '';
        aria = `${when}, ${span}: ${usd(cell.usd)}${share}${cell.partial ? ', so far' : ''}${cell.project ? `, mostly ${cell.project}` : ''}`;
        style = cell.usd > 0 ? `background:${heatColor(cell.usd / grid.max)}` : '';
      }
      const cls = ['cell', cell.future ? 'hm-future' : '', cell.usd > 0 ? '' : 'hm-zero', cell.partial ? 'hm-now' : ''].filter(Boolean).join(' ');
      const btn = h('button', { type: 'button', class: cls, style, 'aria-label': aria, tabindex: '-1', 'aria-disabled': cell.future ? 'true' : null });
      const title = week ? `${rowLabel(row.date)}, ${span}` : `${row.label} ${span}`;
      const rowsOf = () => (cell.future ? [{ label: 'Not yet', value: '' }] : [
        { label: cell.partial ? 'So far' : 'Spent', value: usd(cell.usd) },
        cell.project ? { label: 'Top project', value: cell.project } : null,
        !week && cell.starts.length ? { label: 'Dates with spend', value: String(cell.starts.length) } : null,
      ].filter(Boolean));
      btn.addEventListener('mousemove', (e) => tooltip.show(e, title, rowsOf()));
      btn.addEventListener('mouseleave', tooltip.hide);
      btn.addEventListener('focus', () => tooltip.show(btn, title, rowsOf()));
      btn.addEventListener('blur', tooltip.hide);
      btn.addEventListener('click', () => { if (!cell.future) open(cell, row); });
      btns[r][hr] = btn;
      cells.push({ btn, cell });
      if (!stop || cell.usd > stop.usd) stop = { btn, usd: cell.usd };
      return btn;
    })));
  if (stop) stop.btn.setAttribute('tabindex', '0');
  const el = h('div.scroll', null, h('div.heat.hm-grid', {
    role: 'group', 'aria-label': week ? 'Spend by hour, the past seven days' : 'Spend by weekday and hour',
    onkeydown: (e) => {
      const moves = { ArrowLeft: [0, -1], ArrowRight: [0, 1], ArrowUp: [-1, 0], ArrowDown: [1, 0] };
      const mv = moves[e.key];
      if (!mv) return;
      let r = btns.findIndex((row) => row.includes(e.target));
      if (r < 0) return;
      let c = btns[r].indexOf(e.target);
      for (;;) {  // skip the hours that do not exist
        r += mv[0]; c += mv[1];
        if (r < 0 || r >= btns.length || c < 0 || c > 23) return;
        const next = btns[r][c];
        if (next) {
          e.preventDefault();
          e.target.setAttribute('tabindex', '-1');
          next.setAttribute('tabindex', '0');
          next.focus();
          return;
        }
      }
    },
  }, rows, h('div.hx', { 'aria-hidden': 'true' }, ['00', '06', '12', '18'].map((t) => h('span', { text: t })))));
  return { el, cells };
}

function weekSentence(grid) {
  if (!grid.total) return 'No spend in the past seven days.';
  let best = null;
  grid.rows.forEach((row) => row.cells.forEach((c) => { if (!best || c.usd > best.c.usd) best = { c, row }; }));
  const where = best.c.project ? `, mostly ${best.c.project}` : '';
  return `The past seven days cost ${usd(grid.total)}. The costliest hour was ${rowLabel(best.row.date)} ${two(best.c.hour)}:00, with ${usd(best.c.usd)}${where}.`;
}

// ------------------------------------------------------------------ panels

function closeButton(onClose) {
  return h('button.hm-close', { type: 'button', 'aria-label': 'Close', text: '×', onclick: onClose });
}

// A typical-week cell: the dates behind it, to pick one.
function datesPanel(panel, cell, row, hours, openHour, onClose) {
  const list = datesBehind(hours, row.weekday, cell.hour);
  const head = `${LONG[(row.weekday + 1) % 7]}s, ${two(cell.hour)}:00–${two((cell.hour + 1) % 24)}:00: ${usd(cell.usd)}`;
  panel.replaceChildren(
    h('div.split', null, h('h3.hm-title', { text: head }), closeButton(onClose)),
    list.length ? h('p.note', { text: `${list.length} date${list.length === 1 ? '' : 's'} with spend at this hour. Pick one.` }) : h('p.note', { text: 'No date with spend at this hour.' }),
    h('div.hm-dates', null, list.map((x) => {
      const d = new Date(x.start);
      return h('button.hm-date', { type: 'button', onclick: () => openHour(x.start) },
        h('span', { text: rowLabel(d) }), h('span.mono', { text: usd(x.usd) }), x.project ? h('span.note', { text: x.project }) : null);
    })));
  const first = panel.querySelector('.hm-date');
  if (first) first.focus();
}

// The one-line reason from capcom.explain: the hour's largest named cause.
export function causeLine(d) {
  if (d.causes && d.causes.length) {
    const n = (c) => (c.spikes === 1 ? 'one spike' : `${c.spikes} spikes`);
    const [c, next] = d.causes;
    const more = next ? `; ${next.cause_label.toLowerCase()}: ${usd(next.excess_usd)}, in ${n(next)}` : '';
    return `${c.cause_label}: ${usd(c.excess_usd)} over the usual pace, in ${n(c)}${more}.`;
  }
  if (d.spikes && !d.spikes.length) return 'No spike in this hour: the spend ran at its usual pace.';
  if (d.spikes && d.spikes.length) return 'No rule names a cause for its spikes.';
  return null;
}

async function hourPanel(panel, start, onClose) {
  const d0 = new Date(start);
  const when = `${rowLabel(d0)}, ${hourText(d0)}`;
  panel.replaceChildren(h('div.split', null, h('h3.hm-title', { text: when }), closeButton(onClose)), h('p.note', { text: 'Loading…' }));
  let d;
  try {
    d = await api('hour?start=' + encodeURIComponent(isoOf(start)));
  } catch (err) {
    panel.replaceChildren(h('div.split', null, h('h3.hm-title', { text: when }), closeButton(onClose)), errorBox(err));
    return;
  }
  const t = d.totals;
  const title = `${when}: ${usd(t.usd)}${d.partial ? ' so far' : ''}`;
  const out = [h('div.split', null, h('h3.hm-title', { text: title }), closeButton(onClose))];
  if (!t.requests) {
    out.push(h('p.note', { text: 'No spend in this hour.' }));
    panel.replaceChildren(...out);
    return;
  }
  const why = causeLine(d);
  if (why) out.push(h('p.story.hm-why', { text: why }));

  // by token kind
  const cats = CATEGORY_ORDER.map((k) => ({ k, usd: t.categories[k].usd }));
  out.push(h('div.hm-kinds', null,
    h('div.stackbar.tall', { role: 'img', 'aria-label': 'By token kind: ' + cats.map((c) => `${CATEGORY_LABELS[c.k].toLowerCase()} ${usd(c.usd)}`).join(', ') },
      cats.map((c) => (c.usd > 0 ? h('span', { style: `width:${(c.usd / t.usd * 100).toFixed(2)}%;background:${categoryColor(c.k)}` }) : null))),
    h('div.legend', null, cats.map((c) => h('span', null, h('i', { style: `background:${categoryColor(c.k)}` }), CATEGORY_LABELS[c.k], ' ', h('em', null, usd(c.usd)))))));

  // where it went
  out.push(h('div.hm-cols', null,
    h('div', null, h('h4.label', { text: 'By project' }), barList(d.by_project.slice(0, 5), (p) => p.usd)),
    h('div', null, h('h4.label', { text: 'By agent type' }),
      barList(d.by_agent.slice(0, 5).map((a) => ({ ...a, label: a.key === 'main' ? 'main thread' : a.label })), (a) => a.usd))));

  // spikes and sessions
  if (d.spikes && d.spikes.length) {
    out.push(h('h4.label', { text: 'Spikes in this hour' }), h('div.hm-list', null, d.spikes.map((sp) => {
      const tx = spikeText(sp);
      const s0 = new Date(sp.start);
      return h('a.hm-row', { href: spikeHref(sp.key, s0.getTime()) },
        h('span.hm-main', null, projectChip(sp), h('b', { text: tx.title }), tx.rest ? h('span.note', { text: ` (${tx.rest})` }) : null),
        h('span.note', { text: `${two(s0.getHours())}:${two(s0.getMinutes())}` }),
        h('span.mono', { text: `+${usd(sp.excess_usd)}` }));
    })));
  }
  if (d.sessions.length) {
    out.push(h('h4.label', { text: d.sessions_count > d.sessions.length ? `Top sessions (${d.sessions.length} of ${d.sessions_count})` : 'Sessions' }),
      h('div.hm-list', null, d.sessions.map((s) => h('a.hm-row', { href: sessionHref(s.session_id) },
        h('span.hm-main', null, s.project ? h('span.tag.proj', { text: s.project }) : null, h('span', { text: s.title || s.session_id.slice(0, 8) })),
        h('span.note', { text: `${s.requests} request${s.requests === 1 ? '' : 's'}` }),
        h('span.mono', { text: usd(s.usd) })))));
  }
  panel.replaceChildren(...out);
}

// the card's own styles, loaded once
if (typeof document !== 'undefined' && document.head && !document.getElementById('hm-css')) {
  document.head.append(h('link', { id: 'hm-css', rel: 'stylesheet', href: '/static/heatmap.css' }));
}
