// Checks: setup warnings drawn from real usage, ranked by the weekly saving, each
// with its evidence, a small chart from its numbers and the change to paste.

import { h, api, usd, pct, monthDay, hm, tokens, seg, empty, setQuery, sessionHref, maxOf, copyButton, daysText, entityColors, segBar, tableToggle, tipOn } from './lib.js';
import { pendingBlock } from './sessions.js';
import { READ_ONLY, markForm, markedChip, noticedBanners, summaryCard } from './marks.js';

// The marks the finding cards show: each mark's result by id (from /api/changes),
// and the Checks window a new mark answers.
let marked = { results: new Map(), days: 14 };

// /api/changes and its noticed list are extras on this page: a failure leaves them out.
const quiet = (path) => api(path).catch(() => null);

const RANGES = [[7, '7 days'], [14, '14 days'], [30, '30 days']];

export async function renderChecks(main, r) {
  const days = Number(r.query.get('days')) || 14;
  const titles = h('div.titles', null, h('h1', { text: 'Checks' }));
  const head = h('header.head', null, titles, h('div.controls', null, seg('Range', RANGES, days, (v) => setQuery({ days: v }))));
  main.append(head);
  const changesP = quiet('changes'), noticedP = quiet(`changes/noticed?days=${days}`);
  const { data: d, block } = await pendingBlock('checks', `days=${days}`);
  if (block) {
    main.append(h('section.card', null, block));
    return;
  }
  titles.append(h('p.sub', { text: `What your setup loads against what your sessions really used, over ${daysText(days)}. Ranked by what each change would save in a week at this pace.` }));
  if (!d.findings.length) {
    main.append(h('section.card', null, empty('Nothing worth changing', `No check found a saving in ${daysText(days)}: the tools, skills and agents you load were used, and no compaction, pause or model switch cost much. Checks run again on each visit.`)));
    return;
  }
  const t = d.total;
  if (t) {
    // the same line as `capcom checks` prints: a total only over findings that count separate dollars
    head.insertBefore(h('div.totalbox', { title: t.text },
      h('span.label', { text: totalLabel(t) }),
      h('span.big', null, 'up to ' + usd(t.usd_per_week, 0), h('small', { text: ' a week' })),
      // the label says what it adds; the note only what it leaves out
      t.judgement_calls ? h('span.note', { text: `Judgement calls, up to ${usd(t.judgement_calls.usd_per_week, 0)}/wk more, are listed apart and not in it.` }) : null), head.lastChild);
  }
  const [changes, noticed] = await Promise.all([changesP, noticedP]);
  marked = { results: new Map(((changes && changes.changes) || []).map((c) => [c.id, c])), days };
  if (changes) main.append(h('div.row-wrap', null, summaryCard(changes)));
  if (noticed) main.append(...noticedBanners(noticed.noticed, days));
  if (d.overlapping) {
    main.append(h('p.notice', { text: 'Some of these savings count the same dollars: a long context and the late compaction that ends it, say. Each saving is an “up to” on its own; the total counts each dollar once, over the findings that do not overlap.' }));
  }
  // one row per kind across its projects or agent types, largest first; a row
  // opens to its findings, one per scope
  const byN = new Map(d.findings.map((f) => [f.n, f]));
  const titleOf = new Map((d.rollups || []).map((r) => [r.kind, r.title]));
  const counted = (d.rollups || []).filter((r) => !r.judgement_call);
  const judged = (d.rollups || []).filter((r) => r.judgement_call);
  // one color per place on every row: the six with the most saving in all, the rest gray
  const bySaving = new Map();
  for (const r of d.rollups || []) for (const x of r.scopes) bySaving.set(x.scope_label, (bySaving.get(x.scope_label) || 0) + x.saving_usd_per_week);
  scopeColors = entityColors([...bySaving].sort((a, b) => b[1] - a[1]).slice(0, 6).map(([k]) => k));
  if (counted.length) {
    main.append(h('section.card.rollups', null, h('h2', { text: 'By kind' }),
      h('p.note', { style: 'margin:0 0 8px', text: 'Each kind across the places it was found, with the share of each (hover a bar for its numbers). Open a row for its findings.' }),
      scopeLegend(),
      counted.map((r) => rollupRow(r, byN, titleOf, false))));
  }
  if (judged.length) {
    const sum = judged.reduce((a, r) => a + r.saving_usd_per_week, 0);
    main.append(h('section.card.rollups.judged', null,
      h('div.split', null, h('h2', { text: 'Judgement calls' }), h('span.save.small', { text: 'up to ' + usd(sum, 2) + '/wk more' })),
      h('p.note', { style: 'margin:0 0 8px', text: 'A judgement call on quality, not in the total: such a change trades some quality for its saving, and would shrink the other savings on the same requests.' }),
      judged.map((r) => rollupRow(r, byN, titleOf, false))));
  }
  if (d.small && d.small.findings) {
    main.append(h('p.note', { style: 'margin:0', text: `${d.small.findings} more finding${d.small.findings === 1 ? '' : 's'} under ${usd(d.small.below_usd_per_week, 2)}/wk, ${usd(d.small.usd_per_week, 2)}/wk together: not listed, not in the total.` }));
  }
  if (d.footer) main.append(h('p.note.footer', { text: d.footer }));
}

// A roll-up row: the kind, its weekly saving, the scopes with their shares, what
// it overlaps; it opens to its findings.
let scopeColors = new Map();
const scopeLegend = () => (scopeColors.size > 1 ? h('div.legend', { style: 'margin:0 0 6px' },
  [...scopeColors].map(([k, c]) => h('span', null, h('i', { style: `background:${c}` }), k === '(other)' ? 'the rest' : k))) : null);
function rollupRow(r, byN, titleOf, open) {
  const findings = r.findings.map((n) => byN.get(n)).filter(Boolean);
  const many = r.scopes.length !== 1;
  const noun = r.scope_noun + (many ? 's' : '');
  const money = (x) => usd(x, x < 10 ? 2 : 0) + '/wk';
  const tags = [];
  // a judgement call's own section already says it is not in the total
  if (r.judgement_call) { /* no tag */ } else if (r.counted === 'none') tags.push(h('span.tag.over', { text: 'overlaps: not in the total', title: 'Its dollars are counted under the kind it overlaps.' }));
  else if (r.counted === 'some') tags.push(h('span.tag.over', { text: 'partly in the total', title: 'Some of its findings overlap a larger one and are left out of the total.' }));
  // the kinds that kept one of its findings out of the total (none: no chip); their
  // names on hover here, and in full once the row is open
  const near = r.overlaps.map((k) => titleOf.get(k) || k);
  const shares = near.length ? `may share dollars with ${near.join(', ')}` : '';
  if (near.length) {
    tags.push(tipOn(h('span.tag.over', { text: `shares dollars with ${near.length === 1 ? near[0] : near.length + ' kinds'}` }),
      'May share dollars with', near.map((k) => ({ label: k, value: '' })), { focus: false }));
  }
  const body = h('div.rfind');
  let where;
  if (r.scopes.length === 1 && r.scopes[0].scope === 'all') where = h('span.note.block', { text: 'across every project' });
  else if (!many) where = h('span.note.block', { text: `in ${r.scopes[0].scope_label}` });
  else {
    // a small bar of each scope's share: the five largest, then the rest together
    const sorted = [...r.scopes].sort((a, b) => b.share - a.share);
    const top = sorted.slice(0, 5), rest = sorted.slice(5);
    const colorOf = (k) => scopeColors.get(k) || scopeColors.get('(other)') || 'var(--gray)';
    const parts = top.map((x) => ({ color: colorOf(x.scope_label), share: x.share, title: x.scope_label,
      rows: [{ label: 'Share', value: pct(x.share) }, { label: 'Saving', value: 'up to ' + money(x.saving_usd_per_week) }] }));
    if (rest.length) {
      const share = rest.reduce((a, x) => a + x.share, 0);
      parts.push({ color: colorOf('(other)'), share, title: `${rest.length} more ${rest.length === 1 ? r.scope_noun : noun}`,
        rows: rest.slice(0, 6).map((x) => ({ label: x.scope_label, value: pct(x.share) }))
          .concat(rest.length > 6 ? [{ label: `${rest.length - 6} more`, value: '' }] : []) });
    }
    const lead = sorted[0];
    const text = `across ${r.scopes.length} ${noun}, most in ${lead.scope_label} (${pct(lead.share)})`;
    where = h('span.scopes', null, h('span.note', { text }),
      segBar(parts, { focus: false, aria: `across ${r.scopes.length} ${noun}: ` + sorted.map((x) => `${x.scope_label} ${pct(x.share)}`).join(', ') }));
    body.append(tableToggle([r.scope_noun[0].toUpperCase() + r.scope_noun.slice(1), 'Share', 'Saving up to'],
      sorted.map((x) => [x.scope_label, pct(x.share), money(x.saving_usd_per_week)]), `Each ${r.scope_noun} as a table`));
  }
  if (shares) body.append(h('p.note', { style: 'margin:0', text: shares[0].toUpperCase() + shares.slice(1) + '.' }));
  body.append(...findings.map((f) => card(f, true)));
  return h('details.rollup', { open: open || null },
    h('summary', null,
      h('span.mtext', null, h('b', { text: r.title }), where, tags.length ? h('span.tags', null, ...tags) : null),
      h('span.save', null, h('small', { text: 'up to ' }), money(r.saving_usd_per_week))),
    body);
}


// "across 4 findings that don't overlap (of 5)", worded as in `capcom checks`
export function totalLabel(t) {
  const n = `${t.findings} finding${t.findings === 1 ? '' : 's'}`;
  if (t.findings === 1 && t.all) return 'Across the one finding';
  return `Across ${n} that don't overlap` + (t.all ? '' : ` (of ${t.of})`);
}

// A finding; inside its kind's row (`scoped`) it is headed by its scope instead of
// the kind's title, which the row already says.
function card(f, scoped = false) {
  const scopeName = f.scope && f.scope !== 'all' ? (f.scope_label || f.scope) : 'Every project';
  const box = h('section.card.check');
  box.append(h('div.chead', null,
    h('span.n', { text: String(f.n) }),
    h('div.ctext', null,
      h('div', { style: 'display:flex;flex-wrap:wrap;gap:8px;align-items:baseline' }, h('h2', { text: scoped ? scopeName : f.title, title: scoped ? (f.scope_path || null) : null }), !scoped && f.scope && f.scope !== 'all' ? h('span.chip', { text: f.scope_label || f.scope, title: f.scope_path || null }) : null,
        f.counted ? null : h('span.tag.over', { text: f.metrics && f.metrics.judgement_call ? 'judgement call' : 'overlaps a larger finding', title: 'Not added to the total.' })),
      h('p.para', { text: f.summary })),
    h('span.save', { title: 'Estimated at this window’s pace' }, h('small', { text: 'up to ' }), usd(f.saving_usd_per_week, f.saving_usd_per_week < 10 ? 2 : 0) + '/wk'),
    markAction(f, box)));
  box.append(body(f));
  return box;
}

// A finding's mark: its chip, linked to the measure, or the button that opens the
// "Mark as applied" form under the card's head (none in read-only mode).
function markAction(f, box) {
  if (f.mark) return h('div.mark', null, markedChip(f.mark, marked.results.get(f.mark.id)));
  if (READ_ONLY || !('finding_id' in f)) return null;
  const btn = h('button.btn.small', { type: 'button', 'aria-expanded': 'false', text: 'Mark as applied' });
  let form = null;
  const close = () => { if (form) form.remove(); form = null; box.classList.remove('marking'); btn.setAttribute('aria-expanded', 'false'); btn.focus(); };
  btn.addEventListener('click', () => {
    if (form) { close(); return; }
    form = markForm(f, marked.days, close);
    box.querySelector('.chead').after(form);
    box.classList.add('marking');
    btn.setAttribute('aria-expanded', 'true');
    const first = form.querySelector('button');
    if (first) first.focus();
  });
  return h('div.mark', null, btn);
}

function body(f) {
  const left = h('div.cleft');
  const v = visual(f.visual);
  if (v) left.append(v);
  if (f.evidence.length) {
    left.append(h('div.facts', null, h('h3', { text: 'Evidence in the logs' }),
      h('ul.evidence', null, f.evidence.map((e) => h('li', null,
        e.session_id ? h('a', { href: sessionHref(e.session_id), text: e.label }) : h('span', { text: e.label }),
        e.timestamp ? h('span.note', { text: ' · ' + `${monthDay(new Date(e.timestamp))} ${hm(new Date(e.timestamp))}` }) : null)))));
  }
  const fix = h('div.fix', null, h('h3', { text: 'What to change' }), h('p', { text: f.advice }));
  if (f.change) fix.append(h('pre.change', { text: f.change }), h('div', null, copyButton(f.change)));
  return h('div.cbody', null, left, fix);
}

function visual(v) {
  if (!v) return null;
  const wrap = h('div.visual', null, h('span.label', { text: v.label }));
  if (v.type === 'squares' || v.type === 'split') {
    const caption = `${v.total} · ${v.filled} ${v.verb}`;
    if (v.type === 'squares') {
      const sq = h('div.squares', { role: 'img', 'aria-label': `${v.label}: ${caption}` });
      for (let i = 0; i < v.total; i++) sq.append(h('span', { class: i < v.filled ? 'on' : null }));
      wrap.append(sq);
    } else {
      wrap.append(h('div.stackbar.tall', { role: 'img', 'aria-label': `${v.label}: ${caption}` },
        h('span', { style: `width:${(v.filled / v.total * 100).toFixed(2)}%;background:var(--accent)` }),
        h('span', { style: 'flex:1;background:var(--subtle)' })));
    }
    wrap.append(h('span.note', { text: `${caption}. Filled: ${v.verb} at least once.` }));
  } else if (v.type === 'histogram') {
    const max = maxOf(v.buckets.map((b) => b.count), 1);
    wrap.append(h('div.hist', { role: 'img', 'aria-label': v.label + ': ' + v.buckets.map((b) => `${b.label} calls ${b.count}`).join(', ') },
      v.buckets.map((b) => h('div', null,
        h('span.note', { text: b.count ? String(b.count) : '' }),
        h('div', { style: `height:${(b.count / max * 100).toFixed(1)}%;background:${b.small ? 'var(--accent)' : 'var(--gray)'}` })))),
    h('div.histlabs', null, v.buckets.map((b) => h('span', { text: b.label }))),
    h('span.note', { text: 'Tool calls per subagent; in the accent, two or fewer.' }));
  } else if (v.type === 'dots') {
    const top = Math.max(1e6, maxOf(v.values, 0) * 1.05);
    const x = (n) => (n / top * 100).toFixed(2) + '%';
    const axis = h('div.dots', { role: 'img', 'aria-label': `${v.label}: ${v.values.map(tokens).join(', ')}` },
      h('div.thr', { style: `left:${x(v.threshold)}` }), h('span.thrlab', { style: `left:${x(v.threshold)}`, text: tokens(v.threshold) }),
      v.values.map((n) => h('span.dot', { style: `left:${x(n)}`, title: tokens(n) + ' tokens' })));
    wrap.append(axis, h('div.wx', null, h('span', { text: '0' }), h('span', { text: tokens(top) })),
      h('span.note', { text: v.values.slice(0, 8).map(tokens).join(', ') + (v.values.length > 8 ? ', …' : '') }));
  } else if (v.type === 'gauge') {
    const top = Math.max(v.value, v.limit) * 1.1;
    wrap.append(h('div.gauge', { role: 'img', 'aria-label': `${v.label}: ${tokens(v.value)} against ${tokens(v.limit)}` },
      h('div', { style: `width:${(v.value / top * 100).toFixed(1)}%` }),
      h('span', { style: `left:${(v.limit / top * 100).toFixed(1)}%` })),
    h('span.note', { text: `${tokens(v.value)} tokens; the line is ${tokens(v.limit)}.` }));
  } else {
    return null;
  }
  return wrap;
}
