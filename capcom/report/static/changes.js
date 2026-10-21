// Did it work?: the changes the user marked, each measured before and after its
// date (/changes), and one change with its weeks (/changes/<id>).

import { h, s, api, pct, monthDay, localDate, empty, tipOn, ApiError } from './lib.js';
import { niceTicks } from './charts.js';
import {
  READ_ONLY, VERDICTS, VERDICT_ORDER, postChange, verdictChip, savingText, rateText, agoText, todayIso,
} from './marks.js';

const longDay = (iso) => { const d = localDate(iso); return `${monthDay(d)}, ${d.getFullYear()}`; };
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const lower = (text) => text.charAt(0).toLowerCase() + text.slice(1);
const scopeName = (c) => (c.scope === 'all' ? 'every project' : c.scope_label);

function missing(err) {
  if (err instanceof ApiError && err.status === 501) {
    return empty('Not available yet', 'capcom.changes, which keeps and measures the marks, is not part of this build.');
  }
  return null;
}

// "Applied Oct 22, 21 days ago." and where the mark came from
function appliedLine(c, long = false) {
  const parts = [`Applied ${long ? longDay(c.applied_on) : monthDay(localDate(c.applied_on))}, ${agoText(c.days_since)}.`];
  if (c.source === 'noticed') parts.push('Noticed by capcom; you confirmed it.');
  if (c.what === 'other') parts.push('Something else than the suggested change.');
  return parts.join(' ');
}

const isJudgement = (c) => c.quality && c.quality.length > 0;

// ------------------------------------------------------------------ the list

export async function renderChanges(main) {
  const titles = h('div.titles', null, h('h1', { text: 'Did it work?' }),
    h('p.sub', { text: 'The changes you marked, each measured on the logs before and after its date. Every measure is a rate (per 1,000 requests, per request, per run), so a busier week doesn’t count against you.' }));
  main.append(h('header.head', null, titles));
  let d;
  try {
    d = await api('changes');
  } catch (err) {
    const block = missing(err);
    if (!block) throw err;
    main.append(h('section.card', null, block));
    return;
  }
  if (!d.changes.length) {
    main.append(h('section.card.emptyexplain', null,
      h('h2', { text: 'Nothing marked yet' }),
      h('p.para', { text: 'Each finding on Checks says what to change and what it would save. Once you make a change, mark it as applied there with the day you made it: capcom then compares the finding’s own rate over the 4 weeks before with the days after, and says whether it worked, made no clear difference or made things worse, with the dollars it saved against what it expected.' }),
      h('p.para', { text: 'A verdict needs 7 days and enough requests in the same place. The marks stay on this machine, in one file next to capcom’s other data.' }),
      h('div', null, h('a.btn.primary', { href: '/checks', text: 'Open Checks' }))));
    return;
  }
  main.append(h('div.row-wrap', null, savingTile(d), countsTile(d)));
  main.append(h('section.changelist', { 'aria-label': 'Changes' }, d.changes.map(changeCard)));
  main.append(h('p.note.footer', { text: `Before is the 4 weeks before the date you gave; after starts the next day. A verdict needs 7 days and enough requests in the same place. Measured saving = the drop in the rate × the requests since × list prices, so it is API-equivalent money, not your bill. Your changes live in ${d.path}.` }));
}

function savingTile(d) {
  const w = d.with_verdict;
  const tile = h('div.card.tight.grow1', null, h('span.label', { text: 'Measured saving, changes with a verdict' }));
  if (!w.changes) {
    tile.append(h('span.big', { text: 'No verdict yet' }),
      h('span.note', { text: d.next_verdict ? `The first comes on ${monthDay(localDate(d.next_verdict))}, once a change has 7 days of requests.` : 'A verdict comes once a change has 7 days and enough requests.' }));
    return tile;
  }
  const m = w.measured_usd_per_week;
  tile.append(h('span.big', null, h('span', { class: m > 0 ? 'goodtext' : m < 0 ? 'badtext' : null, text: savingText(m) }), h('small', { text: ' a week' })));
  if (w.expected_usd_per_week !== null) {
    const these = w.changes === 1 ? 'this one' : `these ${w.changes}`;
    const share = w.share === null ? '' : w.share > 0 ? `: ${pct(w.share)} of it showed up` : '; none of it showed up';
    tile.append(h('span.note', { text: `capcom expected ${savingText(w.expected_usd_per_week)} a week for ${these}${share}.` }));
  }
  return tile;
}

function countsTile(d) {
  const c = d.counts;
  return h('div.card.tight.grow1', null,
    h('span.label', { text: `${plural(d.marks, 'change')} marked` }),
    h('div.chips', null, VERDICT_ORDER.filter((v) => c[v]).map((v) => verdictChip(v, `${c[v]} ${VERDICTS[v].label.toLowerCase()}`))),
    d.next_verdict ? h('span.note', { text: `The next verdict comes on ${monthDay(localDate(d.next_verdict))}.` }) : null);
}

// before and after as two bars on one scale
function pairBars(c) {
  const top = Math.max(c.before || 0, c.after || 0, 1e-12);
  const row = (label, v, cls) => h('div.pbar', null, h('span.pl', { text: label }),
    h('span.ptrack', null, v === null ? null : h(`span.${cls}`, { style: `width:${Math.max(1, v / top * 100).toFixed(1)}%` })),
    h('span.pv', { text: rateText(v, c.measure) }));
  return h('div.pairbars', { role: 'img', 'aria-label': `${c.measure}: ${rateText(c.before, c.measure)} before, ${rateText(c.after, c.measure)} after` },
    row('before', c.before, 'before'), row('after', c.after, 'after'));
}

function changeCard(c) {
  const early = c.verdict === 'too_early';
  const measured = c.measured_usd_per_week;
  const why = [c.reason];
  return h('a.card.change', { href: `/changes/${c.id}` },
    h('div.ctitle', null,
      h('div.tline', null, h('span.t', { text: c.title }), h('span.chip', { text: scopeName(c) }),
        isJudgement(c) ? h('span.tag.judge', { text: 'judgement call' }) : null),
      h('span.note', { text: appliedLine(c) }),
      c.note ? h('span.note', null, 'Your note: ', h('span.usernote', { text: `“${c.note}”` })) : null,
      h('span.note', { text: c.measure })),
    pairBars(c),
    h('div.money', null,
      h('div.verdict', null, verdictChip(c.verdict)),
      h('span.note', { text: 'measured / expected' }),
      h('span.serif.mid', { class: measured > 0 && !early ? 'goodtext' : measured < 0 && !early ? 'badtext' : null, text: measured === null ? (early ? 'not yet' : '—') : (early ? '≈ ' : '') + savingText(measured) + '/wk' }),
      h('span.note', { text: c.expected_usd_per_week === null ? 'no saving expected' : `of ${savingText(c.expected_usd_per_week)}/wk expected` })),
    h('div.why', null, why.filter(Boolean).join(' '),
      isJudgement(c) ? h('span.quality', null, 'Quality is yours to judge: ', c.quality.map((q, i) => [i ? '; ' : '', `${lower(q.label)} ${q.before} → ${q.after}`]), '.') : null));
}

// ------------------------------------------------------------------ one change

export async function renderChange(main, r) {
  main.append(h('a.back', { href: '/changes', text: '← Did it work?' }));
  let c;
  try {
    c = await api('changes/' + encodeURIComponent(r.arg));
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      main.append(h('section.card', null, empty('No such change', 'It may have been unmarked. The list has the ones that are marked now.'), h('a.link', { href: '/changes', text: 'Back to the list' })));
      return;
    }
    const block = missing(err);
    if (!block) throw err;
    main.append(h('section.card', null, block));
    return;
  }
  main.append(h('header.head.top', null,
    h('div.titles', null,
      h('div.chips', { style: 'align-items:center' }, h('span.chip', { text: scopeName(c) }),
        isJudgement(c) ? h('span.tag.judge', { text: 'judgement call' }) : null,
        h('span.note', { text: c.what === 'other' ? 'You made another change for this finding' : 'The finding’s suggested change' })),
      h('h1', { text: c.title }),
      h('p.sub', null, appliedLine(c, true), c.note ? [' Your note: ', h('span.usernote', { text: `“${c.note}”` }), '.'] : null)),
    verdictChip(c.verdict, null, { big: true })));
  main.append(h('div.grid.tiles3', null, measureTile(c), moneyTile(c), workloadTile(c)));
  main.append(h('div.row-wrap', { style: 'align-items:flex-start' },
    weekChart(c),
    h('aside.side', null, howTile(c), READ_ONLY ? null : editTile(c))));
  if (c.evidence.length || c.quality.length) main.append(evidenceCard(c));
}

function measureTile(c) {
  const tile = h('div.card.tight', null, h('span.label', { text: c.measure }));
  const cls = c.verdict === 'working' ? 'goodtext' : c.verdict === 'worse' ? 'badtext' : null;
  tile.append(h('span.big', null, rateText(c.before, c.measure), ' → ', h('span', { class: cls, text: rateText(c.after, c.measure) })));
  const notes = [];
  if (c.before && c.after !== null) notes.push(`${c.after >= c.before ? '+' : '−'}${pct(Math.abs(c.after - c.before) / c.before)}`);
  if (c.before_range) notes.push(`the before weeks ran ${rateText(c.before_range[0], c.measure)} to ${rateText(c.before_range[1], c.measure)}`);
  if (notes.length) tile.append(h('span.note', { text: notes.join(', ') }));
  return tile;
}

function moneyTile(c) {
  const m = c.measured_usd_per_week;
  const early = c.verdict === 'too_early';
  const tile = h('div.card.tight', null, h('span.label', { text: early ? 'Saving so far' : 'Saving, measured' }));
  tile.append(h('span.big', null, h('span', { class: m > 0 ? 'goodtext' : m < 0 ? 'badtext' : null, text: (early && m !== null ? '≈ ' : '') + savingText(m) }), m === null ? null : h('small', { text: ' a week' })));
  const e = c.expected_usd_per_week;
  if (e !== null) {
    const share = m === null || !(e > 0) || early ? '' : m > 0 ? `: ${pct(m / e)} of it showed up` : '; none of it showed up';
    tile.append(h('span.note', { text: `capcom expected ${savingText(e)}${share}` }));
  }
  return tile;
}

function workloadTile(c) {
  const b = c.requests_per_week_before, a = c.requests_per_week_after;
  const n = (x) => (x === null ? '—' : Math.round(x).toLocaleString('en-US'));
  const change = b && a ? ` (${a >= b ? '+' : '−'}${pct(Math.abs(a - b) / b)})` : '';
  return h('div.card.tight', null, h('span.label', { text: `Workload in ${scopeName(c)}` }),
    h('span.big', { text: `${n(b)} → ${n(a)}` }),
    h('span.note', { text: `requests a week${change}; the rate accounts for it` }));
}

function howTile(c) {
  const w = c.window;
  const lines = [
    `The finding’s own measure: ${lower(c.measure)} in ${scopeName(c)}.`,
    w.before_from ? `Before is ${monthDay(localDate(w.before_from))} to ${monthDay(localDate(w.before_to))}; after starts ${monthDay(localDate(w.after_from))}, the day after you applied it.` : `After starts ${monthDay(localDate(w.after_from))}, the day after you applied it.`,
    'Working: after at least 7 days and enough requests, the after days’ rate is lower than the before days’, by more than chance (95 %) and than their usual swing. Higher by as much: worse. Otherwise, no clear effect.',
    'Saving = (before − after) × the requests a week since × the average price of one in the before window, at list prices.',
  ];
  if (c.verdict === 'too_early' && c.verdict_due) lines.push(`The verdict comes on ${monthDay(localDate(c.verdict_due))}.`);
  return h('section.card.tight.how', { 'aria-labelledby': 'how' }, h('h2.small3', { id: 'how', text: 'How it’s measured' }),
    lines.map((t) => h('p.para', { text: t })));
}

// Change the date, edit the note, unmark: each opens inline under its button.
function editTile(c) {
  const box = h('section.card.tight.edits', { 'aria-label': 'Change this mark' });
  const err = h('p.formerr', { role: 'alert', hidden: true });
  let open = null;
  const run = async (body, btn) => {
    btn.disabled = true;
    err.hidden = true;
    try {
      await postChange(body);
      if (body.action === 'unmark') { history.pushState(null, '', '/changes'); window.dispatchEvent(new Event('capcom-route')); }
    } catch (ex) {
      err.textContent = 'Not saved: ' + (ex.message || String(ex));
      err.hidden = false;
      btn.disabled = false;
    }
  };
  const item = (label, build) => {
    const btn = h('button.btn.wide', { type: 'button', 'aria-expanded': 'false', text: label });
    const slot = h('div.editslot');
    btn.addEventListener('click', () => {
      const was = open;
      if (open) { open.slot.replaceChildren(); open.btn.setAttribute('aria-expanded', 'false'); open = null; }
      if (was && was.btn === btn) return;
      slot.replaceChildren(build());
      btn.setAttribute('aria-expanded', 'true');
      open = { btn, slot };
      const field = slot.querySelector('input, button');
      if (field) field.focus();
    });
    return [btn, slot];
  };
  box.append(
    ...item('Change the date', () => {
      const input = h('input.dateinput', { type: 'date', value: c.applied_on, max: todayIso(), 'aria-label': 'The day you made the change' });
      const save = h('button.btn.primary.small', { type: 'button', text: 'Save the date' });
      save.addEventListener('click', () => { if (input.value && input.value !== c.applied_on) run({ action: 'edit', id: c.id, applied_on: input.value }, save); });
      return h('div.inline', null, input, save);
    }),
    ...item('Edit the note', () => {
      const input = h('input.textinput', { type: 'text', maxlength: 500, 'aria-label': 'Your note', autocomplete: 'off' });
      input.value = c.note;
      const save = h('button.btn.primary.small', { type: 'button', text: 'Save the note' });
      save.addEventListener('click', () => run({ action: 'edit', id: c.id, note: input.value.trim() }, save));
      return h('div.inline', null, input, save);
    }),
    ...item('Unmark', () => {
      const yes = h('button.btn.danger.small', { type: 'button', text: 'Unmark it' });
      yes.addEventListener('click', () => run({ action: 'unmark', id: c.id }, yes));
      return h('div.confirm', null, h('p.para', { text: 'Unmark this change? capcom stops measuring it; the logs and your setup are untouched.' }),
        h('div.btnrow', null, yes, h('button.btn.small', { type: 'button', text: 'Keep it', onclick: () => { open.slot.replaceChildren(); open.btn.setAttribute('aria-expanded', 'false'); open.btn.focus(); open = null; } })));
    }),
    err);
  return box;
}

function evidenceCard(c) {
  const pairs = (rows) => h('div.evgrid', null, rows.map((e) => h('div.ev', null, h('span.note', { text: e.label }),
    h('span.mono', null, e.before, ' → ', h('b', { text: e.after })))));
  return h('section.card', { 'aria-labelledby': 'logs' },
    h('h2', { id: 'logs', text: 'What changed in the logs' }),
    c.evidence.length ? pairs(c.evidence) : null,
    c.quality.length ? [h('h3.small3', { text: 'Quality signals to judge by' }), pairs(c.quality),
      h('p.note', { style: 'margin:0', text: 'A cheaper model is a judgement call: capcom shows what it can see, the call on quality is yours.' })] : null,
    c.reason ? h('p.para', { text: c.reason }) : null);
}

// ------------------------------------------------------------------ the weekly chart

// Bars per week: before muted, after blue; dashed lines at the before and after
// rates with their labels; a line at the applied date. Hover a bar for its week.
export function weekBars(c) {
  const W = 620, H = 290, L = 48, R = 608, T = 24, B = 250;
  const weeks = c.weeks;
  const vals = weeks.map((w) => w.value).filter((v) => v !== null);
  const ticks = niceTicks(Math.max(...vals, c.before || 0, c.after || 0, 0) || 1, 4);
  const top = ticks[ticks.length - 1] || 1;
  const y = (v) => B - (v / top) * (B - T);
  const n = Math.max(weeks.length, 1);
  const slot = (R - L) / n, bw = Math.min(64, slot * 0.62);
  const cx = (i) => L + slot * (i + 0.5);
  const nb = weeks.filter((w) => !w.after).length;
  const split = L + slot * nb; // between the last before week and the first after one
  const label = (iso) => monthDay(localDate(iso));
  const svg = s('svg', { viewBox: `0 0 ${W} ${H}`, class: 'weeks', role: 'img', 'aria-label': `${c.measure} by week: ` + weeks.map((w) => `${label(w.week_start)} ${w.value === null ? 'no requests' : rateText(w.value, c.measure)}`).join(', ') });
  svg.append(s('g', { class: 'grid' }, ticks.map((t) => s('line', { x1: L, x2: R, y1: y(t), y2: y(t) }))),
    s('line', { class: 'axis', x1: L, x2: R, y1: B, y2: B }),
    s('g', { class: 'ylabs' }, ticks.map((t) => s('text', { x: L - 8, y: y(t) + 4, text: t === 0 ? '0' : rateText(t, c.measure) }))));
  weeks.forEach((w, i) => {
    const g = s('g', { class: w.after ? 'wk after' : 'wk before' });
    // a full-height target, so a short bar is easy to hover
    g.append(s('rect', { class: 'hit', x: cx(i) - slot / 2, y: T, width: slot, height: B - T }));
    if (w.value !== null) {
      const hgt = Math.max(1.5, B - y(w.value));
      g.append(s('rect', { class: 'bar', x: cx(i) - bw / 2, y: B - hgt, width: bw, height: hgt, rx: 4 }));
    } else {
      g.append(s('text', { class: 'none', x: cx(i), y: B - 8, text: 'none' }));
    }
    g.append(s('text', { class: 'xlab', x: cx(i), y: B + 22, text: label(w.week_start) }));
    tipOn(g, `Week of ${label(w.week_start)} (${w.after ? 'after' : 'before'})`, [
      { label: c.measure, value: w.value === null ? 'no requests' : rateText(w.value, c.measure) },
      { label: 'Requests', value: w.requests.toLocaleString('en-US') },
    ]);
    svg.append(g);
  });
  const mean = (v, x1, x2, text, anchorEnd) => {
    if (v === null || x2 <= x1) return [];
    const yy = y(v);
    const above = yy - 8 > T + 14;
    return [s('line', { class: 'mean', x1, x2, y1: yy, y2: yy }),
      s('text', { class: 'meanlab', x: anchorEnd ? x2 - 6 : x1 + 6, y: above ? yy - 8 : yy + 16, 'text-anchor': anchorEnd ? 'end' : 'start', text })];
  };
  svg.append(...mean(c.before, L, split, `before: ${rateText(c.before, c.measure)}`, false));
  if (weeks.length > nb) svg.append(...mean(c.after, split, R, `after: ${rateText(c.after, c.measure)}`, true));
  svg.append(s('line', { class: 'applied', x1: split, x2: split, y1: T - 6, y2: B }),
    s('text', { class: 'appliedlab', x: split + 6, y: T + 6, 'text-anchor': split > W * 0.75 ? 'end' : 'start', dx: split > W * 0.75 ? -12 : 0, text: `Applied ${label(c.applied_on)}` }));
  return svg;
}

function weekTable(c) {
  return h('div.scroll', null, h('table.data.narrow', null,
    h('thead', null, h('tr', null, h('th', { scope: 'col', text: 'Week of' }), h('th', { scope: 'col', text: '' }),
      h('th', { scope: 'col', style: 'text-align:right', text: 'Requests' }), h('th', { scope: 'col', style: 'text-align:right', text: c.measure }))),
    h('tbody', null, c.weeks.map((w) => h('tr', null,
      h('td', { text: monthDay(localDate(w.week_start)) }), h('td', { text: w.after ? 'after' : 'before' }),
      h('td.num', { text: w.requests.toLocaleString('en-US') }),
      h('td.num', { text: w.value === null ? 'no requests' : rateText(w.value, c.measure) }))))));
}

function weekChart(c) {
  const holder = h('div.weekholder', null, weekBars(c));
  let table = false;
  const toggle = h('button.linkbtn', { type: 'button', 'aria-pressed': 'false', text: 'Show as a table' });
  toggle.addEventListener('click', () => {
    table = !table;
    holder.replaceChildren(table ? weekTable(c) : weekBars(c));
    toggle.textContent = table ? 'Show as a chart' : 'Show as a table';
    toggle.setAttribute('aria-pressed', String(table));
  });
  return h('section.card.grow3', { 'aria-labelledby': 'weeks' },
    h('div.split', null, h('h2', { id: 'weeks', text: 'Week by week' }),
      h('div.legend', null, h('span', null, h('i.sq', { style: 'background:var(--bar-before)' }), 'the 4 weeks before'),
        h('span', null, h('i.sq', { style: 'background:var(--s1)' }), 'after'))),
    holder,
    h('div.split', null, h('span.note', { text: 'Weeks start on the date shown. Hover a bar for its requests and its rate.' }), toggle));
}
