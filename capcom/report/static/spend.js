// Spend: the window's cumulative spend split by a grouping, per-day bars, the
// hour-by-weekday heatmap, tokens against dollars, context size, costliest sessions.

import {
  h, api, usd, tokens, pct, seg, legend, empty, errorBox, setQuery, monthDay, localDate, dayName, hm,
  outputUsd, estimateNote, lowerMark, SERIES, OTHER_COLOR, CATEGORY_LABELS, CATEGORY_ORDER, categoryColor,
  spikeText, spikeHref, projectChip,
} from './lib.js';
import { bandChart, dayBars, heatmap, heatScale } from './charts.js';
import { sessionsTable } from './sessions.js';

const RANGES = [[7, '7 days'], [14, '14 days'], [30, '30 days'], [90, '90 days']];
const GROUPS = [['category', 'Category'], ['project', 'Project'], ['model', 'Model'], ['agent', 'Agent type']];
const TITLES = { 7: 'One week, adding up', 14: 'Two weeks, adding up', 30: 'Thirty days, adding up', 90: 'Ninety days, adding up' };

export function memberColor(m, i, group) {
  if (m.key === '(other)') return OTHER_COLOR;
  if (group === 'category' && CATEGORY_ORDER.includes(m.key)) return categoryColor(m.key);
  return SERIES[i] || OTHER_COLOR;
}

export function memberLabel(m, group) {
  if (group === 'category' && CATEGORY_LABELS[m.key]) return CATEGORY_LABELS[m.key];
  if (m.key === '(other)') return 'other';
  return m.label;
}

function spikeWhen(m) {
  const d = new Date(m.t);
  const thread = m.scope === 'session' ? 'session-wide' : m.agent_id ? 'subagent' : 'main thread';
  const where = [m.title || (m.session_id ? m.session_id.slice(0, 8) : null), thread].filter(Boolean);
  return [`${monthDay(d)} ${hm(d)}`, ...where].join(' · ');
}

// The markers that fit a chart from x0 to x1 (ms): the largest by excess first, at
// least `gap` of the axis apart, at most `max`, renumbered in time order. The list
// beside the chart uses the same set, so the numbers match.
export function pickSpikes(sp, x0, x1, gap = 0.04, max = 8) {
  if (!sp) return null;
  const span = Math.max(x1 - x0, 1);
  const chosen = [];
  for (const m of [...sp.marks].sort((a, b) => b.excess_usd - a.excess_usd)) {
    if (m.t < x0 || m.t > x1) continue;
    if (chosen.every((c) => Math.abs(c.t - m.t) / span >= gap)) chosen.push(m);
    if (chosen.length >= max) break;
  }
  chosen.sort((a, b) => a.t - b.t);
  return { ...sp, marks: chosen.map((m, i) => ({ ...m, n: i + 1 })) };
}

// chart markers from a picked spikes block; `days` picks the Spikes window
export function spikeMarks(sp, days) {
  if (!sp) return [];
  return sp.marks.map((m) => ({ n: m.n, t: m.t, href: spikeHref(m.key, m.t, days), aria: `Spike ${m.n}: ${spikeText(m).title}${spikeText(m).rest ? ` (${spikeText(m).rest})` : ''}, +${usd(m.excess_usd)} over the baseline${m.project ? `, in project ${m.project}` : ''}, ${spikeWhen(m)}` }));
}

// the numbered list next to a chart: the largest spikes first
export function spikeList(sp, days, limit = 4) {
  if (!sp) return [timelineMissing('Spike detection')];
  if (!sp.count) return [empty(null, 'No spikes: the spend rose at its usual pace.')];
  const rows = [...sp.marks].sort((a, b) => b.excess_usd - a.excess_usd).slice(0, limit);
  const out = rows.map((m) => h('a.spikerow', { href: spikeHref(m.key, m.t, days) },
    h('span.num', { text: String(m.n) }),
    h('span.body', null,
      h('span.meta', null, projectChip(m), h('span', { text: spikeWhen(m) })),
      h('b', { text: spikeText(m).title }),
      spikeText(m).rest ? h('span.rest', { text: spikeText(m).rest }) : null,
      h('span.tags', null, h('span.tag', { text: spikeText(m).tag }),
        h('span.plus', { text: `+${usd(m.excess_usd)}` }),
        h('span.note', { text: `${m.requests} request${m.requests === 1 ? '' : 's'}, ${usd(m.cost_usd)} in all` })))));
  const more = sp.count > rows.length ? `${sp.count} spike${sp.count === 1 ? '' : 's'} in all, +${usd(sp.excess_usd)} over their baselines; the largest are marked. ` : '';
  const why = sp.explained ? 'Each is matched to the cause the logs point to.' : 'Their causes are explained from milestone 3.';
  out.push(h('p.note', { style: 'margin:8px 0 0', text: `${more}${why}` }));
  return out;
}

export function timelineMissing(what) {
  return empty(`${what} needs the timeline module`, 'The spend series come from capcom.timeline, which is not merged yet. Totals below are exact; the chart appears once it lands.');
}

export async function renderSpend(main, r) {
  const days = Number(r.query.get('days')) || 14;
  const group = r.query.get('group') || 'category';
  const d = await api(`spend?days=${days}&group=${group}`);
  const since = new Date(d.window.since), until = new Date(d.window.until);
  const t = d.totals;

  const sub = h('p.sub', null, `${monthDay(since)} to ${monthDay(until)} · `, h('strong', { text: usd(t.usd) }), ' API-equivalent · ',
    `${d.sessions_count} sessions`, d.temporary_sessions ? `, ${d.temporary_sessions} more in /tmp` : '');
  main.append(h('header.head', null,
    h('div.titles', null, h('h1', { text: 'Spend' }), sub),
    h('div.controls', null,
      seg('Range', RANGES, days, (v) => setQuery({ days: v })),
      seg('Split by', GROUPS, group, (v) => setQuery({ group: v })))));

  // cumulative
  const cum = h('section.card', { style: 'padding-bottom:18px' });
  let picked = null;
  const head = h('div.split', null, h('h2', { text: TITLES[days] || 'Adding up' }));
  cum.append(head);
  if (d.cumulative && d.cumulative.times.length) {
    const c = d.cumulative;
    const members = c.members.map((m, i) => ({ ...m, label: memberLabel(m, group), color: memberColor(m, i, group), values: c.values[i] }));
    head.append(legend(members.map((m) => ({ label: m.label, color: m.color, value: (group === 'category' && m.key === 'output' ? lowerMark(t) : '') + usd(m.final, m.final >= 100 ? 0 : 2) })), lowerMark(t) + usd(t.usd, t.usd >= 100 ? 0 : 2)));
    picked = pickSpikes(d.spikes, since.getTime(), until.getTime());
    cum.append(bandChart({ times: c.times, members, total: c.total, start: since.getTime(), end: until.getTime(), height: 340, left: 52, marks: spikeMarks(picked, days) }));
  } else if (d.cumulative) {
    cum.append(empty(null, 'No spend in this window.'));
  } else {
    cum.append(timelineMissing('The cumulative chart'));
  }
  const note = estimateNote(t);
  const sp = d.spikes;
  const shown = picked ? picked.marks.length : 0;
  const spikeNote = !sp ? '' : sp.count ? `${sp.count} spike${sp.count === 1 ? '' : 's'}, +${usd(sp.excess_usd)} over their baselines; ${shown < sp.count ? `the ${shown} largest that fit are marked` : 'each is marked'}. ` : 'No spikes in this window. ';
  cum.append(h('p.note', { style: 'margin:0' }, spikeNote, note ? note + '.' : ''));
  main.append(cum);

  // per day + tokens vs dollars
  const perDay = h('section.card.grow3');
  if (d.daily) {
    const members = d.daily.members.map((m, i) => ({ label: memberLabel(m, group), color: memberColor(m, i, group) }));
    const rows = d.daily.days.map((x) => ({ ...x, iso: x.date, date: localDate(x.date) }));
    const best = rows.reduce((a, b) => (b.total > a.total ? b : a), rows[0]);
    perDay.append(h('div.split', null, h('h2.small', { text: 'Per day' }),
      best && best.total > 0 ? h('span.note', { text: `Highest: ${dayName(best.date)} ${monthDay(best.date)}, ${usd(best.total, 0)}` }) : null));
    perDay.append(dayBars(rows, members, rows[rows.length - 1].iso));
  } else {
    perDay.append(h('h2.small', { text: 'Per day' }), timelineMissing('The per-day chart'));
  }
  main.append(h('div.row-wrap', null, perDay, tokensVsDollars(t)));

  // heatmap + context
  const heat = h('section.card.grow3', null, h('div.split', null, h('h2.small', { text: 'When you spend' }), d.heatmap ? heatScale() : null));
  if (d.heatmap) {
    heat.append(heatmap(d.heatmap.cells, d.heatmap.max, d.heatmap.weekdays));
    heat.append(h('p.para', { text: heatSentence(d.heatmap) }));
  } else heat.append(timelineMissing('The heatmap'));
  main.append(h('div.row-wrap', null, heat, contextCard(d.context_buckets)));

  // sessions
  main.append(h('section.card', null,
    h('div.split', null, h('h2.small', { text: 'Costliest sessions' }),
      h('a.link', { href: `/sessions?days=${days}`, text: `All ${d.sessions_count + d.temporary_sessions} sessions` })),
    d.sessions.length ? sessionsTable(d.sessions) : empty(null, 'No sessions in this window.')));
}

function tokensVsDollars(t) {
  const card = h('section.card.grow2b', null, h('h2.small', { text: 'Tokens are not dollars' }));
  if (!t.requests) { card.append(empty(null, 'No requests in this window.')); return card; }
  const c = t.categories;
  const tokShare = CATEGORY_ORDER.map((k) => (t.tokens ? c[k].tokens / t.tokens : 0));
  const usdShare = CATEGORY_ORDER.map((k) => (t.usd ? c[k].usd / t.usd : 0));
  const bar = (shares, aria) => h('div.stackbar.tall', { role: 'img', 'aria-label': aria },
    shares.map((x, i) => (x > 0 ? h('span', { style: `width:${(x * 100).toFixed(2)}%;background:${categoryColor(CATEGORY_ORDER[i])}` }) : null)));
  const names = ['reads', 'writes', 'output', 'input'];
  const aria = (shares) => CATEGORY_ORDER.map((k, i) => `${pct(shares[i], 1)} ${CATEGORY_LABELS[k].toLowerCase()}`).join(', ');
  card.append(
    h('div', { style: 'display:flex;flex-direction:column;gap:6px' }, h('span.label', { text: 'Share of tokens' }), bar(tokShare, 'Tokens: ' + aria(tokShare))),
    h('div', { style: 'display:flex;flex-direction:column;gap:6px' }, h('span.label', { text: 'Share of cost' }), bar(usdShare, 'Cost: ' + aria(usdShare)),
      h('div', { style: 'display:flex;justify-content:space-between;font-size:11px;color:var(--muted)' },
        names.map((n, i) => h('span', { text: `${n} ${pct(usdShare[i])}` })))),
    h('p.para', { style: 'font-size:14px' }, `Output is ${pct(tokShare[2], 1)} of the tokens and ${pct(usdShare[2])} of the cost (${outputUsd(t)}). `,
      `Cache reads are ${pct(tokShare[0], 1)} of the tokens: ${tokens(c.cache_read.tokens)} of them, the same context reread on every turn, for ${pct(usdShare[0])} of the cost.`),
  );
  return card;
}

function contextCard(buckets) {
  const card = h('section.card.grow2b', null, h('h2.small', { text: 'Context size when you paid' }),
    h('div.legend', null, h('span', null, h('i.sw', { style: 'background:var(--gray)' }), 'requests'), h('span', null, h('i.sw', { style: 'background:var(--accent)' }), 'cost')));
  const nReq = buckets.reduce((a, b) => a + b.requests, 0), nUsd = buckets.reduce((a, b) => a + b.usd, 0);
  if (!nReq) { card.append(empty(null, 'No requests in this window.')); return card; }
  const shares = buckets.map((b) => [b.requests / nReq, nUsd ? b.usd / nUsd : 0]);
  const max = Math.max(...shares.flat(), 1e-9);
  card.append(
    h('div.pairs', null, buckets.map((b, i) => h('div', { title: `${b.label}: ${b.requests} requests (${pct(shares[i][0])}), ${usd(b.usd)} (${pct(shares[i][1])})` },
      h('div', { style: `height:${(shares[i][0] / max * 100).toFixed(1)}%;background:var(--gray)` }),
      h('div', { style: `height:${(shares[i][1] / max * 100).toFixed(1)}%;background:var(--accent)` })))),
    h('div.pairlabs', null, buckets.map((b) => h('span', { text: b.label.replace('-', '–') }))));
  const big = buckets.slice(4);
  const bigReq = big.reduce((a, b) => a + b.requests, 0) / nReq, bigUsd = nUsd ? big.reduce((a, b) => a + b.usd, 0) / nUsd : 0;
  card.append(h('p.para', { text: `Requests with more than 400k tokens of context are ${pct(bigReq)} of the requests and ${pct(bigUsd)} of the cost.` }));
  return card;
}

function heatSentence(heat) {
  let best = null, total = 0;
  heat.cells.forEach((row, r) => row.forEach((v, hr) => { total += v; if (!best || v > best.v) best = { v, r, hr }; }));
  if (!total) return 'No spend in this window.';
  const block = [0, 0, 0, 0];  // 00-06, 06-12, 12-18, 18-24
  heat.cells.forEach((row) => row.forEach((v, hr) => { block[Math.floor(hr / 6)] += v; }));
  const names = ['Nights (00 to 06)', 'Mornings (06 to 12)', 'Afternoons (12 to 18)', 'Evenings (18 to 24)'];
  const top = block.indexOf(Math.max(...block));
  const two = (n) => String(n).padStart(2, '0');
  return `${names[top]} carry ${pct(block[top] / total)} of the spend. The busiest hour was ${heat.weekdays[best.r]} ${two(best.hr)}:00, with ${usd(best.v)} over the window.`;
}

export { errorBox };
