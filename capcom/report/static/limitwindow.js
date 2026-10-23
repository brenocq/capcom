// One usage window on its own page (/limits/window/<account>/<5-hour|weekly>/<start>),
// opened from a row of "What spent each window" on Limits: how its percentage rose
// (the readings, and the fitted percent of what was spent), what spent it, its
// costliest sessions and spikes, and its own dollars per 1%. Data from
// /api/limit-window.

import { h, api, usd, tokens, empty, monthDay, hm, barList, tableToggle, CATEGORY_LABELS, CATEGORY_ORDER } from './lib.js';
import { limitChart } from './limits.js';
import { sessionsTable } from './sessions.js';
import { pickSpikes, spikeList } from './spend.js';

const pctText = (x, digits = 0) => `${(+x).toFixed(digits)}%`;
const share = (x) => (x === null || x === undefined ? '' : `≈${pctText(x, x < 10 ? 1 : 0)}`);

// "Oct 22 18:20 → 23:20" (a weekly window names both days)
export function windowTitle(d) {
  const a = new Date(d.start), b = new Date(d.resets_at || d.until);
  const weekly = d.window === 'seven_day';
  const end = weekly || a.toDateString() !== b.toDateString() ? `${monthDay(b)} ${hm(b)}` : hm(b);
  return `${monthDay(a)} ${hm(a)} → ${d.current ? 'now' : end}`;
}

// The sentence on top: how full it got, on how much spend, and what 1% cost in it.
export function windowStory(d) {
  const parts = [];
  const spent = d.totals.usd;
  const peak = d.hit_limit ? `${pctText(d.peak)} (the limit was hit)` : pctText(d.peak);
  parts.push(`${d.current ? 'So far it has reached' : 'It reached'} ${peak} of the limit on ${usd(spent)} of API-equivalent spend.`);
  if (d.own_usd_per_percent !== null) {
    let line = `In this window 1% cost about ${usd(d.own_usd_per_percent)}`;
    if (d.fit && d.fit.usd_per_percent !== null) line += `, against ${usd(d.fit.usd_per_percent)} on average over every window`;
    parts.push(line + '.');
  } else if (d.fit && d.fit.usd_per_percent !== null) {
    parts.push(`Too small a rise to tell what 1% cost in this window alone; on average over every window it is ${usd(d.fit.usd_per_percent)}.`);
  }
  if (d.inferred_usd >= 0.005 && d.account.several) parts.push(`Of the spend, ${usd(d.inferred_usd)} came from sessions with no reading, put on this account by time.`);
  if (!d.start_known) parts.push('Its start was not seen, so the rise counts from the first reading.');
  return parts.join(' ');
}

// One "what spent it" list: bars with dollars, the share of the limit on hover,
// all of it in the table.
function spentList(title, rows) {
  return [h('h2.small', { style: 'margin-top:18px', text: title }),
    barList(rows.slice(0, 6), (r) => r.usd, (r) => (r.percent === null || r.percent === undefined ? '' : `${share(r.percent)} of the limit`))];
}

export async function renderLimitWindow(main, r) {
  const [, account, kind, start] = r.rest || [];
  const q = new URLSearchParams({ account: account || '', window: kind || '', start: start || '' });
  const d = await api('limit-window?' + q.toString());
  const weekly = d.window === 'seven_day';
  const x0 = new Date(d.start).getTime();
  const x1 = new Date(d.resets_at || d.until).getTime();
  const now = Date.now();

  const crumb = h('nav.crumb', { 'aria-label': 'Breadcrumb' }, h('a', { href: '/limits', text: 'Limits' }),
    h('span', { 'aria-hidden': 'true', text: '/' }),
    d.account.several ? h('span', { text: d.account.label, title: d.account.label, style: 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:40ch' }) : null,
    d.account.several ? h('span', { 'aria-hidden': 'true', text: '/' }) : null,
    h('span', { text: d.label[0].toUpperCase() + d.label.slice(1) }));
  const nav = h('div.controls', null,
    d.before ? h('a.link', { href: d.before, text: '← Window before' }) : null,
    d.after ? h('a.link', { href: d.after, text: 'Window after →' }) : null);
  const sessions = `${d.sessions_count} session${d.sessions_count === 1 ? '' : 's'}`;
  main.append(h('header.head', null,
    h('div.titles', null, crumb, h('h1', { text: windowTitle(d) }),
      h('p.sub', null, `${d.label[0].toUpperCase() + d.label.slice(1)} · peak `, h('strong', { text: pctText(d.peak) }),
        ` · ${usd(d.totals.usd)} API-equivalent · ${sessions}`)),
    nav));
  main.append(h('p.story', { text: windowStory(d) }));

  // how the percentage rose: the readings, and the fitted percent of the spend
  const card = h('section.card', null, h('div.split', null, h('h2', { text: 'How it filled up' }),
    h('div.legend', null,
      h('span', null, h('i', { style: 'background:var(--text)' }), 'Readings'),
      d.fit ? h('span', null, h('i', { style: 'background:var(--accent)' }), 'Fitted from the spend') : null)));
  const fitted = d.fitted.times.map((t, i) => [t, d.fitted.percent[i]]);
  card.append(limitChart({
    segments: [{ points: d.points, reset: false, start_known: d.start_known }],
    fitted: d.fit ? fitted : [], now: d.current ? now : null, x0, x1: Math.max(x1, x0 + 1), height: 220, weekly, kind: d.window,
  }));
  const notes = [];
  if (d.fit) notes.push(`The dashed line is the fit's percent of what was spent, adding up: ${d.fit.note}`);
  else notes.push('No fit of this window yet, so only the readings are drawn.');
  card.append(h('p.note', { text: notes.join(' ') }));
  main.append(card);

  if (!d.totals.requests) {
    main.append(h('section.card', null, empty(null, 'No spend from this account in this window.')));
    return;
  }

  // what spent it: by project, agent type, model and token type
  const cats = CATEGORY_ORDER.map((k) => ({ key: k, label: CATEGORY_LABELS[k], ...d.totals.categories[k] }))
    .filter((c) => c.usd > 0 || c.tokens > 0);
  const agents = d.by_agent.map((a) => ({ ...a, label: a.key === 'main' ? 'main thread' : a.label }));
  const models = d.by_model.map((m) => ({ ...m, label: m.label.replace(/^claude-/, '') }));
  const table = [];
  for (const [group, rows] of [['Project', d.by_project], ['Agent type', agents], ['Model', models], ['Token type', cats]]) {
    for (const x of rows) table.push([group, x.label, usd(x.usd), tokens(x.tokens), share(x.percent)]);
  }
  const left = h('section.card.grow2b', null, ...spentList('By project', d.by_project), ...spentList('By agent type', agents));
  left.firstChild.style.marginTop = '0';
  const right = h('section.card.grow2b', null, ...spentList('By model', models), ...spentList('By token type', cats));
  right.firstChild.style.marginTop = '0';
  main.append(h('div.split', null, h('h2.small', { text: 'What spent it' })),
    h('div.row-wrap', null, left, right),
    h('section.card', null, h('p.note', { style: 'margin:0', text: d.fit ? 'Each part\'s share of the limit is the fit\'s percent of its spend (on hover, and in the table).' : 'No fit yet, so no share of the limit.' }),
      tableToggle(['Group', 'Name', 'Spent', 'Tokens', 'Of the limit'], table)));

  // its spikes and costliest sessions
  const picked = pickSpikes(d.spikes, x0, Math.max(x1, now));
  main.append(h('section.card', null, h('h2.small', { text: 'Spikes in this window' }), ...spikeList(picked || d.spikes, undefined, 6)));
  main.append(h('section.card', null,
    h('div.split', null, h('h2.small', { text: 'Costliest sessions' }), h('span.note', { text: d.sessions_count > d.sessions.length ? `top ${d.sessions.length} of ${d.sessions_count}` : '' })),
    d.sessions.length ? sessionsTable(d.sessions) : empty(null, 'No sessions in this window.')));
}
