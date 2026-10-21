// Shared helpers: DOM building (text only, never HTML from data), formatting,
// fetching the API, the tooltip.

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

// ------------------------------------------------------------------ API

const memo = new Map();

// Drop the kept results, so the next draw fetches fresh data.
export function forget() { memo.clear(); }

export class ApiError extends Error {
  constructor(status, body) {
    super(body && body.message ? body.message : `HTTP ${status}`);
    this.status = status;
    this.body = body || {};
  }
}

// GET /api/<path>; results are kept for the page's life (a theme switch redraws
// without fetching again). `fresh` skips the memo (the live list).
export function api(path, fresh = false) {
  if (!fresh && memo.has(path)) return memo.get(path);
  const p = fetch('/api/' + path, { headers: { Accept: 'application/json' } }).then(async (r) => {
    let body = null;
    try { body = await r.json(); } catch (e) { body = null; }
    if (!r.ok) throw new ApiError(r.status, body);
    return body;
  });
  if (!fresh) {
    memo.set(path, p);
    p.catch(() => memo.delete(path));
  }
  return p;
}

// ------------------------------------------------------------------ tooltip

const tip = () => document.getElementById('tip');

// rows: [{label, value, color}], title: string
export function showTip(event, title, rows) {
  const el = tip();
  el.replaceChildren(
    title ? h('div.t', null, title) : null,
    ...rows.map((r) => h('div.r', null,
      h('span', null, r.color ? h('i', { style: `background:${r.color}` }) : null, r.label),
      h('span', null, r.value))),
  );
  el.hidden = false;
  const pad = 14, w = el.offsetWidth, hgt = el.offsetHeight;
  let x = event.clientX + pad, y = event.clientY + pad;
  if (x + w > window.innerWidth - 4) x = event.clientX - w - pad;
  if (y + hgt > window.innerHeight - 4) y = event.clientY - hgt - pad;
  el.style.left = Math.max(4, x) + 'px';
  el.style.top = Math.max(4, y) + 'px';
}

export function hideTip() { tip().hidden = true; }

// Attach a simple tooltip to an element.
export function tipOn(el, title, rows) {
  el.addEventListener('mousemove', (e) => showTip(e, title, typeof rows === 'function' ? rows() : rows));
  el.addEventListener('mouseleave', hideTip);
  return el;
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

export function legend(items, total) {
  return h('div.legend', null,
    total ? h('span.total', null, h('i', { style: 'background:var(--text)' }), 'Total ', h('em', null, total)) : null,
    items.map((it) => h('span', null, h('i', { style: `background:${it.color}` }), it.label, ' ', h('em', null, it.value))));
}

export function barList(rows, valueOf, noteOf) {
  if (!rows.length) return empty(null, 'Nothing in this window.');
  const max = Math.max(...rows.map(valueOf), 1e-9);
  return h('div.blist', null, rows.map((r, i) => h('div.bitem', null,
    h('div.top', null,
      h('span', { title: r.path ? `${r.label}: ${r.path}` : r.label }, r.label, ' ', h('span.est', null, noteOf ? noteOf(r) : '')),
      h('span.mono', null, usd(valueOf(r)))),
    h('div.track', null, h('div', { style: `width:${(valueOf(r) / max * 100).toFixed(1)}%;background:${SERIES[i] || OTHER_COLOR}` })))));
}

export function sessionHref(id) { return '/session/' + encodeURIComponent(id); }

// Change the page's query without a reload; the frame redraws the page.
export function setQuery(params) {
  const q = new URLSearchParams(location.search);
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === '') q.delete(k); else q.set(k, v);
  }
  const qs = q.toString();
  history.replaceState(null, '', location.pathname + (qs ? '?' + qs : ''));
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
  if (!onPick) return h('span.tag.proj', { text: it.project, title });
  return h('button.tag.proj', { type: 'button', text: it.project, title: `${title}: show its spikes only`, onclick: (e) => { e.preventDefault(); e.stopPropagation(); onPick(it.project); } });
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
