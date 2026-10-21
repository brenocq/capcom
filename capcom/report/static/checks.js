// Checks: setup warnings drawn from real usage, ranked by the weekly saving, each
// with its evidence, a small chart from its numbers and the change to paste.

import { h, usd, tokens, seg, empty, setQuery, sessionHref, stamp, maxOf, copyButton, daysText } from './lib.js';
import { pendingBlock } from './sessions.js';

const RANGES = [[7, '7 days'], [14, '14 days'], [30, '30 days']];

export async function renderChecks(main, r) {
  const days = Number(r.query.get('days')) || 14;
  const titles = h('div.titles', null, h('h1', { text: 'Checks' }));
  const head = h('header.head', null, titles, h('div.controls', null, seg('Range', RANGES, days, (v) => setQuery({ days: v }))));
  main.append(head);
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
      h('span.big', null, 'up to ' + usd(t.usd_per_week, 0), h('small', { text: ' a week' }))), head.lastChild);
  }
  if (d.overlapping) {
    main.append(h('p.notice', { text: 'Some of these savings count the same dollars: a long context and the late compaction that ends it, say. Each saving is an “up to” on its own; the total counts each dollar once, over the findings that do not overlap.' }));
  }
  // the largest in full; the small ones as rows that open to the same content
  const big = d.findings.filter((f, i) => i < FULL && f.saving_usd_per_week >= 1);
  for (const f of big) main.append(card(f));
  const rest = d.findings.slice(big.length);
  if (rest.length) {
    main.append(h('section.card.minor', null,
      h('h2.small', { text: `${rest.length} smaller finding${rest.length === 1 ? '' : 's'}` }),
      rest.map((f) => h('details', null,
        h('summary', null,
          h('span.n', { text: String(f.n) }),
          h('span.mtext', null, h('b', { text: f.title }), f.scope && f.scope !== 'all' ? h('span.note', { text: ' · ' + (f.scope_label || f.scope), title: f.scope_path || null }) : null,
            h('span.note.block', { text: f.summary })),
          h('span.save.small', { text: 'up to ' + usd(f.saving_usd_per_week, 2) + '/wk' })),
        body(f)))));
  }
}

const FULL = 8;  // findings shown in full; the rest as rows

// "across 4 findings that don't overlap (of 5)", worded as in `capcom checks`
export function totalLabel(t) {
  const n = `${t.findings} finding${t.findings === 1 ? '' : 's'}`;
  if (t.findings === 1 && t.all) return 'Across the one finding';
  return `Across ${n} that don't overlap` + (t.all ? '' : ` (of ${t.of})`);
}

function card(f) {
  const box = h('section.card.check');
  box.append(h('div.chead', null,
    h('span.n', { text: String(f.n) }),
    h('div.ctext', null,
      h('div', { style: 'display:flex;flex-wrap:wrap;gap:8px;align-items:baseline' }, h('h2', { text: f.title }), f.scope && f.scope !== 'all' ? h('span.chip', { text: f.scope_label || f.scope, title: f.scope_path || null }) : null),
      h('p.para', { text: f.summary })),
    h('span.save', { title: 'Estimated at this window’s pace' }, h('small', { text: 'up to ' }), usd(f.saving_usd_per_week, f.saving_usd_per_week < 10 ? 2 : 0) + '/wk')));
  box.append(body(f));
  return box;
}

function body(f) {
  const left = h('div.cleft');
  const v = visual(f.visual);
  if (v) left.append(v);
  if (f.evidence.length) {
    left.append(h('div.facts', null, h('h3', { text: 'Evidence in the logs' }),
      h('ul.evidence', null, f.evidence.map((e) => h('li', null,
        e.session_id ? h('a', { href: sessionHref(e.session_id), text: e.label }) : h('span', { text: e.label }),
        e.timestamp ? h('span.note', { text: ' · ' + stamp(new Date(e.timestamp)) }) : null)))));
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
