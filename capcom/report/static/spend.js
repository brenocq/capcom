// Spend: the window's cumulative spend split by a grouping, per-day bars, the
// hour-by-weekday heatmap, tokens against dollars, context size, costliest sessions.

import {
  h, api, usd, tokens, pct, seg, legend, empty, errorBox, setQuery, monthDay, localDate, dayName, hm,
  outputUsd, estimateNote, lowerMark, OTHER_COLOR, CATEGORY_LABELS, CATEGORY_ORDER, categoryColor,
  spikeText, spikeHref, projectChip, storyLine, segBar, tableToggle, tipOn, entityColor, GROUPS, pickedGroup, rememberGroup, spendPath, groupQuery, sessionDot,
} from './lib.js';
import { bandChart, dayBars, hourBars } from './charts.js';
import { heatCard } from './heatmap.js';
import { sessionsTable } from './sessions.js';

const RANGES = [[1, 'Today'], [7, '7 days'], [14, '14 days'], [30, '30 days'], [90, '90 days']];
const TITLES = { 1: 'Today, adding up', 7: 'One week, adding up', 14: 'Two weeks, adding up', 30: 'Thirty days, adding up', 90: 'Ninety days, adding up' };

export function memberColor(m, group) {
  if (m.key === '(other)') return OTHER_COLOR;
  if (group === 'category' && CATEGORY_ORDER.includes(m.key)) return categoryColor(m.key);
  return entityColor(m.key, group);
}

// The members of a chart with their labels and colors: a color follows its entity
// (entityColor, one per project, agent type and model across capcom), never its rank.
export function withColors(members, group) {
  return members.map((m) => ({ ...m, label: memberLabel(m, group), color: memberColor(m, group) }));
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

// The same, with the session's color dot before its title.
function spikeWhenParts(m) {
  const [at, ...rest] = spikeWhen(m).split(' · ');
  return rest.length ? [`${at} · `, m.session_id ? sessionDot(m.color) : null, rest.join(' · ')] : [at];
}

// The markers that fit a chart from x0 to x1 (ms): the `keep` largest by excess
// always (the list beside the chart names them), then the next largest at least
// `gap` of the axis apart, at most `max`, renumbered in time order. The list
// beside the chart uses the same set, so the numbers match. Marker times come as
// ISO strings and leave as epoch ms.
export function pickSpikes(sp, x0, x1, gap = 0.04, max = 8, keep = 4) {
  if (!sp) return null;
  const span = Math.max(x1 - x0, 1);
  const chosen = [];
  const inRange = sp.marks.map((m) => ({ ...m, t: typeof m.t === 'number' ? m.t : Date.parse(m.t) }))
    .filter((m) => m.t >= x0 && m.t <= x1)
    .sort((a, b) => b.excess_usd - a.excess_usd);
  for (const m of inRange) {
    if (chosen.length < keep || chosen.every((c) => Math.abs(c.t - m.t) / span >= gap)) chosen.push(m);
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
      h('span.meta', null, projectChip(m), h('span', null, ...spikeWhenParts(m))),
      h('b', { text: spikeText(m).title }),
      spikeText(m).rest ? h('span.rest', { text: spikeText(m).rest }) : null,
      h('span.tags', null, spikeText(m).same ? null : h('span.tag', { text: spikeText(m).tag }),
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
  const group = pickedGroup(r.query);
  const d = await api(spendPath(r.query));
  const since = new Date(d.window.since), until = new Date(d.window.until);
  const t = d.totals;

  const sub = h('p.sub', null, `${monthDay(since)} to ${monthDay(until)} · `, h('strong', { text: usd(t.usd) }), ' API-equivalent · ',
    `${d.sessions_count} sessions`, d.temporary_sessions ? `, ${d.temporary_sessions} more in /tmp` : '');
  main.append(h('header.head', null,
    h('div.titles', null, h('h1', { text: 'Spend' }), sub),
    h('div.controls', null,
      seg('Range', RANGES, days, (v) => setQuery({ days: v })),
      seg('Split by', GROUPS, group, (v) => { rememberGroup(v); setQuery({ group: v }); }))));
  const story = storyLine(d.regime);
  if (story) main.append(story);

  // cumulative
  const cum = h('section.card', { style: 'padding-bottom:18px' });
  let picked = null;
  const head = h('div.split', null, h('h2', { text: TITLES[days] || 'Adding up' }));
  cum.append(head);
  if (d.cumulative && d.cumulative.times.length) {
    const c = d.cumulative;
    const members = withColors(c.members, group).map((m, i) => ({ ...m, values: c.values[i] }));
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

  // per day (per hour today) + tokens vs dollars
  const perDay = h('section.card.grow3');
  if (days === 1 && d.hourly) {
    const members = withColors(d.hourly.members, group);
    const dayHref = `/day/${d.window.since.slice(0, 10)}${groupQuery(group)}`;  // since: local midnight today
    perDay.append(h('div.split', null, h('h2.small', { text: 'Per hour' }),
      h('a.link', { href: dayHref, text: 'Today against a typical day' })));
    perDay.append(hourBars(d.hourly.hours, members, until.getTime(), dayHref));
  } else if (d.daily) {
    const members = withColors(d.daily.members, group);
    const rows = d.daily.days.map((x) => ({ ...x, iso: x.date, date: localDate(x.date) }));
    const best = rows.reduce((a, b) => (b.total > a.total ? b : a), rows[0]);
    perDay.append(h('div.split', null, h('h2.small', { text: 'Per day' }),
      best && best.total > 0 ? h('span.note', { text: `Highest: ${dayName(best.date)} ${monthDay(best.date)}, ${usd(best.total, 0)}` }) : null));
    perDay.append(dayBars(rows, members, rows[rows.length - 1].iso, groupQuery(group)));
  } else {
    perDay.append(h('h2.small', { text: days === 1 ? 'Per hour' : 'Per day' }), timelineMissing(days === 1 ? 'The per-hour chart' : 'The per-day chart'));
  }
  main.append(h('div.row-wrap', null, perDay, tokensVsDollars(t)));

  // heatmap + context; today has no week to lay out, so its spikes take the heatmap's place
  const heat = days === 1
    ? h('section.card.grow3', null, h('h2.small', { text: 'Spikes today' }), ...spikeList(picked || sp, days))
    : d.heatmap ? heatCard(d.heatmap, until.getTime(), heatSentence)
      : h('section.card.grow3', null, h('h2.small', { text: 'When you spend' }), timelineMissing('The heatmap'));
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
  const lower = (k) => (k === 'output' ? lowerMark(t) : '');
  const bar = (shares, what) => segBar(CATEGORY_ORDER.map((k, i) => ({
    color: categoryColor(k), share: shares[i], title: CATEGORY_LABELS[k],
    rows: [
      { label: `Share of ${what}`, value: pct(shares[i], 1), color: categoryColor(k) },
      { label: 'Tokens', value: tokens(c[k].tokens) }, { label: 'Cost', value: lower(k) + usd(c[k].usd) },
    ],
  })), { tall: true, aria: `Share of ${what} by kind` });
  const block = (label, el) => h('div', { style: 'display:flex;flex-direction:column;gap:6px' }, h('span.label', { text: label }), el);
  card.append(
    legend(CATEGORY_ORDER.map((k) => ({ label: CATEGORY_LABELS[k], color: categoryColor(k), value: '' }))),
    block('Share of tokens', bar(tokShare, 'tokens')),
    block('Share of cost', bar(usdShare, 'cost')),
    h('p.para', { style: 'font-size:14px' }, `Output is ${pct(tokShare[2], 1)} of the tokens and ${pct(usdShare[2])} of the cost (${outputUsd(t)}). `,
      `Cache reads are ${pct(tokShare[0], 1)} of the tokens: ${tokens(c.cache_read.tokens)} of them, the same context reread on every turn, for ${pct(usdShare[0])} of the cost.`),

    tableToggle(['Kind', 'Tokens', 'Share of tokens', 'Cost', 'Share of cost'],
      CATEGORY_ORDER.map((k, i) => [CATEGORY_LABELS[k], tokens(c[k].tokens), pct(tokShare[i], 1), lower(k) + usd(c[k].usd), pct(usdShare[i], 1)])),
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
    h('div.pairs.hl', null, buckets.map((b, i) => tipOn(h('div', null,
      h('div', { style: `height:${(shares[i][0] / max * 100).toFixed(1)}%;background:var(--gray)` }),
      h('div', { style: `height:${(shares[i][1] / max * 100).toFixed(1)}%;background:var(--accent)` })),
    `Context ${b.label.replace('-', '–')} tokens`, [
      { label: 'Requests', value: `${b.requests.toLocaleString('en-US')} (${pct(shares[i][0])})`, color: 'var(--gray)' },
      { label: 'Cost', value: `${usd(b.usd)} (${pct(shares[i][1])})`, color: 'var(--accent)' },
    ]))),
    h('div.pairlabs', null, buckets.map((b) => h('span', { text: b.label.replace('-', '–') }))));
  const big = buckets.slice(4);
  const bigReq = big.reduce((a, b) => a + b.requests, 0) / nReq, bigUsd = nUsd ? big.reduce((a, b) => a + b.usd, 0) / nUsd : 0;
  card.append(h('p.para', { text: `Requests with more than 400k tokens of context are ${pct(bigReq)} of the requests and ${pct(bigUsd)} of the cost.` }));
  card.append(tableToggle(['Context', 'Requests', 'Share', 'Cost', 'Share'],
    buckets.map((b, i) => [b.label.replace('-', '–'), b.requests.toLocaleString('en-US'), pct(shares[i][0]), usd(b.usd), pct(shares[i][1])])));
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
