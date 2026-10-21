// Overview: today (or the last 7 days) at a glance.

import {
  h, s, api, usd, tokens, pct, seg, legend, empty, setQuery, longDate, barList, hm,
  outputUsd, estimateNote, lowerMark, LOWER_TITLE, CATEGORY_LABELS, CATEGORY_ORDER, categoryColor,
} from './lib.js';
import { bandChart, sparkline } from './charts.js';
import { pendingBlock } from './sessions.js';
import { limitCard } from './limits.js';
import { memberColor, memberLabel, timelineMissing, spikeMarks, spikeList, pickSpikes } from './spend.js';

function greeting(d) {
  const hr = d.getHours();
  return hr < 5 ? 'Good night' : hr < 12 ? 'Good morning' : hr < 18 ? 'Good afternoon' : 'Good evening';
}

export async function renderOverview(main, r) {
  const range = r.query.get('range') === '7d' ? '7d' : 'today';
  const d = await api('overview');
  const now = new Date(d.now);
  const block = range === 'today' ? d.today : d.week;
  const t = block.totals;

  main.append(h('header.head', null,
    h('div.titles', null, h('h1', { text: greeting(now) }),
      h('p.sub', { text: `${longDate(now)} · ${d.running_sessions} session${d.running_sessions === 1 ? '' : 's'} running` })),
    seg('Range', [['today', 'Today'], ['7d', '7 days']], range, (v) => setQuery({ range: v === 'today' ? null : v }))));

  const lim = d.limits || {};
  const wins = lim.windows || {};
  main.append(h('div.grid', { style: '--min:440px' }, limitCard('5-hour window', wins.five_hour, lim), limitCard('Weekly limit', wins.seven_day, lim)));

  // KPI cards
  // today so far against the previous days with history up to the same time of day
  // (once that baseline is $1 or an hour of today has passed); the week over the
  // days elapsed since its start or the first request
  const prev = d.previous_7_days;
  const avg = prev.same_hour_average_usd;
  let costNote = 'Not billed on your plan.';
  if (range === 'today' && prev.comparable && avg > 0) {
    const diff = t.usd / avg - 1;
    const over = prev.days === 1 ? 'the previous day' : `an average day of the previous ${prev.days}`;
    costNote = `${pct(Math.abs(diff))} ${diff >= 0 ? 'above' : 'below'} the ${usd(avg)} you spent by ${prev.same_hour} on ${over}. Not billed on your plan.`;
  } else if (range === '7d') {
    const days = d.week.elapsed_days || 1;
    const full = d.week.full_days;
    const parts = full ? `${full} full day${full === 1 ? '' : 's'} with history and today so far` : 'today so far';
    costNote = `${usd(t.usd / days)} a day on average, over ${days.toFixed(1)} days (${parts}). Not billed on your plan.`;
  }
  const lower = estimateNote(t);
  const lowerNote = lower ? h('span.note', { title: LOWER_TITLE, text: 'A lower bound: part of the output is estimated from content length.' }) : null;
  const spark = d.spending_now.spark;
  const cats = t.categories;
  const readShare = t.tokens ? cats.cache_read.tokens / t.tokens : 0;
  const sub = block.subagents;
  const when = range === 'today' ? 'today' : 'in 7 days';
  main.append(h('div.grid', { style: '--min:220px' },
    h('section.card.tight', null, h('span.label', { text: range === 'today' ? 'API-equivalent cost today' : 'API-equivalent cost, 7 days' }),
      h('span.big', { text: lowerMark(t) + usd(t.usd), title: lower ? LOWER_TITLE : null }), h('span.note', { text: costNote }), lowerNote),
    h('section.card.tight', null, h('span.label', { text: 'Spending now' }),
      h('div', { style: 'display:flex;align-items:flex-end;justify-content:space-between;gap:12px' },
        h('span.big', null, usd(d.spending_now.usd_last_hour), h('small', { text: ' in the last hour' })),
        spark && spark.values.some((v) => v > 0) ? sparkline(spark.values) : null),
      h('span.note', { text: spark ? 'Last two hours shown, in 5-minute steps.' : 'The sparkline arrives with the timeline module.' })),
    h('section.card.tight', null, h('span.label', { text: `Tokens ${when}` }), h('span.big', { text: lowerMark(t) + tokens(t.tokens), title: lower ? LOWER_TITLE : null }),
      h('div.stackbar', { 'aria-hidden': 'true' }, CATEGORY_ORDER.map((k) => (cats[k].tokens ? h('span', { style: `width:${(cats[k].tokens / t.tokens * 100).toFixed(2)}%;background:${categoryColor(k)}` }) : null))),
      h('span.note', { text: t.tokens ? `${pct(readShare)} are cache reads: the same context, reread every turn.` : 'No requests yet.' }), lowerNote && lowerNote.cloneNode(true)),
    h('section.card.tight', null, h('span.label', { text: `Subagents ${when}` }), h('span.big', { text: String(sub.count) }),
      h('span.note', { text: sub.count ? `They cost ${usd(sub.usd)} of ${usd(t.usd)}. ${sub.two_tool_calls_or_fewer} made two tool calls or fewer.` : 'No subagents ran.' }))));

  // today's cumulative chart and spikes
  const chart = h('section.card.grow2');
  const head = h('div.split', null, h('h2', { text: "Today's spend, adding up" }));
  chart.append(head);
  const c = d.cumulative_today;
  let picked = pickSpikes(d.spikes_today, now.getTime() - 864e5, now.getTime());
  if (c && c.times.length && c.total[c.total.length - 1] > 0) {
    const members = c.members.map((m, i) => ({ ...m, label: memberLabel(m, 'category'), color: memberColor(m, i, 'category'), values: c.values[i] }));
    head.append(legend(members.map((m) => ({ label: m.label, color: m.color, value: (m.key === 'output' ? outputUsd(d.today.totals) : usd(m.final)) })), lowerMark(d.today.totals) + usd(d.today.totals.usd)));
    // start the axis at the first hour with spend; end at the next full hour after now
    const first = c.total.findIndex((v) => v > 0);
    const start = new Date(c.times[Math.max(0, first - 1)] - c.step_s * 1000); start.setMinutes(0, 0, 0);
    const end = new Date(now); end.setHours(end.getHours() + 1, 0, 0, 0);
    const keep = c.times.map((tm, i) => i).filter((i) => c.times[i] >= start.getTime());
    picked = pickSpikes(d.spikes_today, start.getTime(), end.getTime());
    chart.append(bandChart({
      times: keep.map((i) => c.times[i]), total: keep.map((i) => c.total[i]),
      members: members.map((m) => ({ ...m, values: keep.map((i) => m.values[i]) })),
      start: start.getTime(), end: end.getTime(), now: now.getTime(), height: 300, left: 44,
      timeLabel: (tm) => hm(new Date(tm)),
      marks: spikeMarks(picked, 1),
    }));
  } else if (c) {
    chart.append(empty(null, 'Nothing spent yet today.'));
  } else {
    chart.append(timelineMissing("Today's chart"));
  }
  const est = estimateNote(d.today.totals);
  if (est) chart.append(h('p.note', { style: 'margin:0', text: est + '.' }));
  const why = h('aside.card.grow1', null, h('h2', { text: 'Why it jumped' }));
  why.append(...spikeList(picked, 1));
  main.append(h('div.row-wrap', null, chart, why));

  // by project, by agent type, checks
  const label = range === 'today' ? 'today' : 'the last 7 days';
  const projects = h('section.card', null, h('h2.small', { text: 'By project' }),
    barList(top(block.by_project), (x) => x.usd, (x) => `${x.count} session${x.count === 1 ? '' : 's'}`));
  const agents = h('section.card', null, h('h2.small', { text: 'By agent type' }),
    barList(top(block.by_agent.map((a) => (a.key === 'main' ? { ...a, label: 'main threads' } : a))), (x) => x.usd,
      (x) => (x.key === 'main' ? `${x.count} session${x.count === 1 ? '' : 's'}` : `${x.count} spawn${x.count === 1 ? '' : 's'}`)));
  const checks = h('section.card', null, h('div.split', null, h('h2.small', { text: 'Worth changing' }), h('a.link', { href: '/checks', text: 'All checks' })));
  const found = await pendingBlock('checks', 'days=14');
  checks.append(found.block || (found.data.findings.length
    ? h('div.blist', null, found.data.findings.slice(0, 3).map((f) => h('a', { href: '/checks', style: 'display:flex;justify-content:space-between;gap:12px;text-decoration:none' },
      h('span', { style: 'display:flex;flex-direction:column;gap:2px' }, h('b', { text: f.title }), h('span.note', { text: f.summary })),
      h('span.mono', { style: 'color:var(--clay);white-space:nowrap', text: 'up to ' + usd(f.saving_usd_per_week, 0) + '/wk' }))))
    : empty(null, 'Nothing to change in the last 14 days.')));
  main.append(h('div.grid', { style: '--min:300px' }, projects, agents, checks));
  main.append(h('p.note', { style: 'margin:0', text: `Totals for ${label}, at list prices (API-equivalent). ${t.inexact_price_requests ? `${t.inexact_price_requests} requests priced as the closest model family.` : ''}` }));
}

function top(rows, n = 6) {
  if (rows.length <= n) return rows;
  const rest = rows.slice(n - 1);
  const other = { key: '(other)', label: `other (${rest.length})`, usd: rest.reduce((a, x) => a + x.usd, 0), count: rest.reduce((a, x) => a + x.count, 0) };
  return [...rows.slice(0, n - 1), other];
}

export { CATEGORY_LABELS };
