// Day: one local day's spend against a typical day, and where it went: the day
// adding up with its spikes and their causes, by project, agent type and model, and the
// sessions. Reached from the per-day bars on Spend.

import {
  h, api, usd, tokens, seg, legend, empty, setQuery, longDate, localDate, monthDay, barList, lowerMark, estimateNote,
  CATEGORY_LABELS, CATEGORY_ORDER, GROUPS, pickedGroup, rememberGroup, dayPath, groupQuery,
} from './lib.js';
import { bandChart } from './charts.js';
import { sessionsTable } from './sessions.js';
import { withColors, pickSpikes, spikeMarks, spikeList, timelineMissing } from './spend.js';

const isoOf = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const shift = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };

// The sentence that answers "why was it expensive": the day against the typical
// one, then the project and the cost category that rose most above their own.
function whyLine(d) {
  const t = d.totals, typ = d.typical;
  if (!t.requests) return 'No spend on this day.';
  if (!typ) return 'No day with spend in the two weeks before to compare it with.';
  const typical = `a typical day (${usd(typ.usd)}, the median of the ${typ.days} day${typ.days === 1 ? '' : 's'} with spend in the ${typ.of_days} before)`;
  const extra = t.usd - typ.usd;
  if (extra <= 0) return `No more than ${typical}.`;
  const parts = [`${usd(extra)} more than ${typical}.`];
  const rise = (now, was) => now - (was || 0);
  const proj = [...d.by_project].sort((a, b) => rise(b.usd, b.typical_usd) - rise(a.usd, a.typical_usd))[0];
  if (proj && rise(proj.usd, proj.typical_usd) > 0) {
    parts.push(`Most of it was ${proj.label}: ${usd(proj.usd)}, against ${usd(proj.typical_usd || 0)} on a typical day.`);
  }
  const cats = CATEGORY_ORDER.map((k) => ({ k, rise: rise(t.categories[k].usd, typ.categories[k]) })).sort((a, b) => b.rise - a.rise);
  if (cats[0] && cats[0].rise > 0) {
    const k = cats[0].k;
    parts.push(`By kind, ${CATEGORY_LABELS[k].toLowerCase()} rose most: ${usd(t.categories[k].usd)} against ${usd(typ.categories[k] || 0)}.`);
    // cache reads are the context reread on every request: its size is the cause
    const top = d.sessions[0];
    if (k === 'cache_read' && top && top.peak_context >= 200e3) {
      parts.push(`Each request rereads the whole context, and in the costliest session it reached ${tokens(top.peak_context)} tokens.`);
    }
  }
  return parts.join(' ');
}

export async function renderDay(main, r) {
  const group = pickedGroup(r.query);
  const d = await api(dayPath(r.arg || '', r.query));
  const date = localDate(d.day);
  const t = d.totals;
  const since = new Date(d.window.since).getTime(), until = new Date(d.window.until).getTime();

  // the day before and after keep the split
  const prev = shift(date, -1), next = shift(date, 1);
  const keep = groupQuery(group);
  const nav = h('div.controls', null,
    h('a.link', { href: `/day/${isoOf(prev)}${keep}`, text: `← ${monthDay(prev)}` }),
    d.today ? null : h('a.link', { href: `/day/${isoOf(next)}${keep}`, text: `${monthDay(next)} →` }));
  const year = date.getFullYear() !== new Date().getFullYear() ? `, ${date.getFullYear()}` : '';
  main.append(h('header.head', null,
    h('div.titles', null, h('h1', { text: longDate(date) + year }),
      h('p.sub', null, h('strong', { text: usd(t.usd) }), ' API-equivalent · ',
        `${d.sessions_count} session${d.sessions_count === 1 ? '' : 's'}`, d.temporary_sessions ? `, ${d.temporary_sessions} more in /tmp` : '',
        d.today ? ' · so far today' : '')),
    nav));
  main.append(h('p.story', { text: whyLine(d) }));
  if (!t.requests) return;

  // the day adding up, with its spikes marked
  const cum = h('section.card', { style: 'padding-bottom:18px' });
  cum.append(h('div.split', null, h('h2', { text: 'The day, adding up' }),
    seg('Split by', GROUPS, group, (v) => { rememberGroup(v); setQuery({ group: v === 'category' ? null : v }); })));
  const head = h('div');
  cum.append(head);
  let picked = null;
  if (d.cumulative && d.cumulative.times.length) {
    const c = d.cumulative;
    const members = withColors(c.members, group).map((m, i) => ({ ...m, values: c.values[i] }));
    head.append(legend(members.map((m) => ({ label: m.label, color: m.color, value: (group === 'category' && m.key === 'output' ? lowerMark(t) : '') + usd(m.final, m.final >= 100 ? 0 : 2) })), lowerMark(t) + usd(t.usd, t.usd >= 100 ? 0 : 2)));
    picked = pickSpikes(d.spikes, since, until);
    cum.append(bandChart({ times: c.times, members, total: c.total, start: since, end: until, height: 300, left: 52, marks: spikeMarks(picked) }));
  } else if (d.cumulative) {
    cum.append(empty(null, 'No spend on this day.'));
  } else {
    cum.append(timelineMissing('The cumulative chart'));
  }
  const note = estimateNote(t);
  if (note) cum.append(h('p.note', { style: 'margin:0', text: note + '.' }));
  main.append(cum);

  // the spikes and their causes, beside where the money went
  const spikes = h('section.card.grow3', null, h('h2.small', { text: 'Spikes that day' }), ...spikeList(picked || d.spikes, undefined, 6));
  const where = h('section.card.grow2b', null,
    h('h2.small', { text: 'By project' }),
    barList(d.by_project.slice(0, 6), (p) => p.usd, (p) => (p.typical_usd != null ? `typical ${usd(p.typical_usd)}` : ''), 'project'),
    h('h2.small', { style: 'margin-top:18px', text: 'By agent type' }),
    barList(d.by_agent.slice(0, 6).map((a) => ({ ...a, label: a.key === 'main' ? 'main thread' : a.label })), (a) => a.usd, (a) => `${a.count} ${a.key === 'main' ? 'session' : 'agent'}${a.count === 1 ? '' : 's'}`, 'agent'),
    h('h2.small', { style: 'margin-top:18px', text: 'By model' }),
    barList(d.by_model.slice(0, 6), (m) => m.usd, (m) => `${m.count} session${m.count === 1 ? '' : 's'}`, 'model'));
  main.append(h('div.row-wrap', null, spikes, where));

  main.append(h('section.card', null,
    h('div.split', null, h('h2.small', { text: 'Sessions that day' })),
    d.sessions.length ? sessionsTable(d.sessions) : empty(null, 'No sessions on this day.')));
}
