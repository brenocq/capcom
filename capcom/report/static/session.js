// Session view: cost and its check against Claude Code's own estimate, the
// per-request context and cost on an active-time axis, pauses, compactions, the
// main thread and the subagents as lanes, and the agents table.

import {
  h, s, api, usd, tokens, pct, duration, stamp, hm, monthDay, empty, tipOn, showTip, hideTip,
  outputUsd, estimateNote, CATEGORY_LABELS, CATEGORY_ORDER, categoryColor, SERIES, OTHER_COLOR,
} from './lib.js';
import { niceTicks, activeAxis, xAxis } from './charts.js';
import { pendingBlock } from './sessions.js';

export async function renderSession(main, r) {
  const d = await api('session/' + encodeURIComponent(r.arg || ''));
  const sn = d.session, t = d.totals;
  const first = sn.first_seen ? new Date(sn.first_seen) : null, last = sn.last_seen ? new Date(sn.last_seen) : null;

  const chips = [
    h('span.chip', { style: 'display:flex;align-items:center;gap:6px' }, h('span', { class: sn.running ? 'dot hot' : 'dot', style: sn.running ? null : 'background:var(--gray)' }), sn.running ? 'running' : 'ended'),
    sn.cwd ? h('span.chip.mono', { text: sn.cwd }) : null,
    sn.git_branch ? h('span.chip', { text: sn.git_branch }) : null,
    sn.models.length ? h('span.chip', { text: sn.models.slice(0, 2).join(', ') + (sn.efforts.length ? ' · ' + sn.efforts.join('/') + ' effort' : '') }) : null,
    first ? h('span.chip', { text: `${monthDay(first)} ${hm(first)} to ${sn.running ? 'now' : stamp(last)} · ${duration(d.active_seconds)} active` }) : null,
    h('span.chip', { text: `${d.agents.length} subagent${d.agents.length === 1 ? '' : 's'} · ${d.compactions.length} compaction${d.compactions.length === 1 ? '' : 's'}` }),
  ];
  main.append(h('header', { style: 'display:flex;flex-direction:column;gap:10px' },
    h('nav.crumb', { 'aria-label': 'Breadcrumb' }, h('a', { href: '/sessions', text: 'Sessions' }), h('span', { 'aria-hidden': 'true', text: '/' }), h('span', { text: sn.project })),
    h('div.head', null,
      h('div.titles', { style: 'gap:8px' }, h('h1', { text: sn.title || 'Untitled session ' + sn.session_id.slice(0, 8) }), h('div.chips', null, chips)),
      h('div.costbig', null, h('span.label', { text: 'API-equivalent cost' }), h('span', { text: (t.estimated_output.requests ? '~' : '') + usd(t.usd) })))));

  if (d.calibration) main.append(calibration(d.calibration, t));

  main.append(h('div.grid', { style: '--min:180px' }, CATEGORY_ORDER.map((k) => {
    const c = t.categories[k];
    const value = k === 'output' ? outputUsd(t) : usd(c.usd);
    return h('section.card.tight', null,
      h('span.label', { style: 'display:flex;align-items:center;gap:8px' }, h('span.sw', { style: `background:${categoryColor(k)}` }), CATEGORY_LABELS[k]),
      h('span.big', { style: 'font-size:30px', text: value }),
      h('span.note', { text: `${tokens(c.tokens)} tokens · ${pct(t.usd ? c.usd / t.usd : 0)} of the cost` }));
  })));

  main.append(unfolded(d));

  const why = h('section.card.grow3', null, h('h2', { text: 'Why it cost this much' }));
  const spikes = await pendingBlock('spikes', 'days=90');
  why.append(spikes.block || h('p.para', { text: 'See the Spikes page for this session’s spikes.' }));
  const helped = h('section.card.grow2b', null, h('h2', { text: 'What would have helped' }),
    empty('Coming in milestone 4', 'Setup checks will estimate what each change would have saved in this session.'));
  main.append(h('div.row-wrap', { style: 'align-items:flex-start' }, why, helped));

  main.append(agentsTable(d));
}

function calibration(c, t) {
  const ok = c.comparable && c.spread !== null && Math.abs(c.spread) <= 0.02;
  const mark = s('svg', { width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none', 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', style: `stroke:${ok ? 'var(--ok)' : 'var(--clay)'}` },
    s('path', { d: ok ? 'M20 6L9 17l-5-5' : 'M12 8v5M12 16.5v.5' }));
  const apart = c.spread === null ? '' : `, ${pct(Math.abs(c.spread), 1)} apart`;
  const lead = ok ? 'Totals checked.' : c.comparable ? 'Totals differ.' : 'Not directly comparable.';
  const why = c.comparable ? '' : ` Claude Code also counts ${c.reasons.join(', ') || 'calls the logs lack'}.`;
  const est = estimateNote(t);
  return h('section.card.banner', null, mark,
    h('span.msg', null, h('strong', { text: lead }), ` Claude Code's own estimate for this session is ${usd(c.claude_code_usd)}; capcom's, over the same window of the logs, is ${usd(c.capcom_usd)}${apart}.${why}${est ? ' ' + est + '.' : ''}`),
    h('div.vals', null, h('span', null, 'Claude Code', h('b', { text: usd(c.claude_code_usd) })), h('span', null, 'capcom', h('b', { text: usd(c.capcom_usd) }))));
}

function unfolded(d) {
  const card = h('section.card', null, h('div.split', null, h('h2', { text: 'How the session unfolded' }),
    h('span.note', { text: 'Active time; pauses over 30 minutes are squeezed to a mark. Hover for values.' })));
  const R = d.requests;
  if (!R.t.length) { card.append(empty(null, 'No model requests in this session.')); return card; }
  const start = R.t[0], end = Math.max(R.t[R.t.length - 1], start + 60e3);
  const x = activeAxis(start, end, d.pauses);
  const X = (tm) => (x(tm) * 1000).toFixed(1);
  const typeOf = (i) => (R.agent[i] < 0 ? 'main thread' : d.agents[R.agent[i]].agent_type);
  const nearest = (frac) => {
    let best = 0, bd = Infinity;
    for (let i = 0; i < R.t.length; i++) { const dd = Math.abs(x(R.t[i]) - frac); if (dd < bd) { bd = dd; best = i; } }
    return best;
  };
  const hover = (el) => {
    el.addEventListener('mousemove', (e) => {
      const rect = el.getBoundingClientRect();
      const i = nearest((e.clientX - rect.left) / rect.width);
      showTip(e, `${stamp(new Date(R.t[i]))} · ${typeOf(i)}`, [
        { label: 'Context', value: tokens(R.context[i]) },
        { label: 'Cost', value: usd(R.usd[i], R.usd[i] < 0.1 ? 3 : 2) },
        { label: 'Cache write', value: tokens(R.cache_write[i]) },
        { label: R.estimated[i] ? 'Output (estimated)' : 'Output', value: (R.estimated[i] ? '~' : '') + tokens(R.output[i]) },
      ]);
    });
    el.addEventListener('mouseleave', hideTip);
    return el;
  };

  // context per request: the main thread as a line, subagents as dots
  const main = R.t.map((_, i) => i).filter((i) => R.agent[i] < 0);
  const line = main.length ? main : R.t.map((_, i) => i);
  const ticks = niceTicks(Math.max(...R.context, 1), 3);
  const cmax = ticks[ticks.length - 1];
  const H = 230;
  const Y = (v) => (H - v / cmax * H).toFixed(1);
  const path = line.map((i, k) => (k ? 'L' : 'M') + X(R.t[i]) + ' ' + Y(R.context[i])).join('');
  const area = path + `L${X(R.t[line[line.length - 1]])} ${H}L${X(R.t[line[0]])} ${H}Z`;
  const ctx = h('div.chart', { style: `height:${H}px` });
  for (const v of ticks.slice(1)) {
    const top = ((1 - v / cmax) * 100).toFixed(2) + '%';
    ctx.append(h('div.gridline', { style: `top:${top}` }), h('span.ylab', { style: `right:4px;top:${top};transform:translateY(-120%)`, text: tokens(v) }));
  }
  const svg = s('svg', { viewBox: `0 0 1000 ${H}`, preserveAspectRatio: 'none', 'aria-hidden': 'true' },
    s('defs', null, s('linearGradient', { id: 'ctx-fill', x1: 0, y1: 0, x2: 0, y2: 1 },
      s('stop', { offset: 0, style: 'stop-color:var(--s1);stop-opacity:0.30' }), s('stop', { offset: 1, style: 'stop-color:var(--s1);stop-opacity:0.02' }))),
    s('path', { d: area, fill: 'url(#ctx-fill)' }),
    s('path', { d: path, fill: 'none', 'stroke-width': 2, 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke', style: 'stroke:var(--s1)' }));
  const dots = R.t.map((_, i) => i).filter((i) => R.agent[i] >= 0 && main.length);
  if (dots.length) {
    svg.append(s('path', { d: dots.map((i) => `M${X(R.t[i])} ${Y(R.context[i])}l0 0.01`).join(''), fill: 'none', 'stroke-width': 3, 'stroke-linecap': 'round', 'vector-effect': 'non-scaling-stroke', style: 'stroke:var(--s2);opacity:0.55' }));
  }
  ctx.append(svg);
  for (const c of d.compactions) {
    const left = (x(c.t) * 100).toFixed(2) + '%';
    const mark = h('div', { style: `position:absolute;top:0;bottom:0;left:${left};border-left:1px dashed var(--clay)` });
    ctx.append(tipOn(mark, `Compaction at ${stamp(new Date(c.t))}`, [
      { label: 'Trigger', value: c.trigger || 'unknown' },
      { label: 'Before', value: c.pre_tokens != null ? tokens(c.pre_tokens) : '?' },
      { label: 'After', value: c.post_tokens != null ? tokens(c.post_tokens) : '?' },
    ]));
    mark.style.width = '6px'; mark.style.marginLeft = '-3px'; mark.style.cursor = 'default';
  }
  hover(svg.parentNode || ctx);

  // cost per request
  const cmaxUsd = Math.max(...R.usd, 1e-6);
  const bars = (sel) => R.t.map((_, i) => i).filter(sel).map((i) => `M${X(R.t[i])} 80L${X(R.t[i])} ${(80 - R.usd[i] / cmaxUsd * 76).toFixed(1)}`).join('');
  const cost = hover(h('div.chart', { style: 'height:80px;border-bottom:1px solid var(--l15)' },
    s('svg', { viewBox: '0 0 1000 80', preserveAspectRatio: 'none', 'aria-hidden': 'true' },
      s('path', { d: bars((i) => R.agent[i] < 0), fill: 'none', 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke', style: 'stroke:var(--muted)' }),
      s('path', { d: bars((i) => R.agent[i] >= 0), fill: 'none', 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke', style: 'stroke:var(--s2)' }))));

  // pauses
  const pauses = h('div', { style: 'position:relative;height:26px' }, d.pauses.map((p) => {
    const mid = (x(p.start) + x(p.end)) / 2;
    const left = (mid * 100).toFixed(2) + '%';
    const shift = mid > 0.94 ? '-100%' : mid < 0.06 ? '0' : '-50%';
    const longp = p.minutes >= 30;
    const el = longp
      ? h('span', { class: p.minutes >= 60 ? 'pausechip warn' : 'pausechip', style: `left:${left};transform:translateX(${shift})`, text: duration(p.minutes * 60) })
      : h('span.pausetick', { style: `left:${left}` });
    return tipOn(el, `Pause of ${duration(p.minutes * 60)}, ${hm(new Date(p.start))} to ${hm(new Date(p.end))}`, [
      { label: 'Next request', value: usd(p.next_usd) },
      { label: 'Cache written', value: tokens(p.next_cache_write) },
    ]);
  }));

  // lanes: main thread, then one lane per agent type
  const lanes = [];
  const gapMarks = x.gaps.map((g) => h('div.gapmark', { style: `left:${(x(g.start) * 100).toFixed(2)}%;width:${((x(g.end) - x(g.start)) * 100).toFixed(2)}%;border-left:0;background:var(--card)` }));
  if (d.main) {
    const l = (x(d.main.first) * 100), w = Math.max((x(d.main.last) - x(d.main.first)) * 100, 0.3);
    lanes.push(h('div.lab', null, h('b', { text: 'Main thread' })),
      h('div.lane', { style: 'background:transparent' }, tipOn(h('span', { style: `left:${l.toFixed(2)}%;width:${w.toFixed(2)}%;background:var(--s1)` }),
        'Main thread', [{ label: 'Requests', value: String(d.main.requests) }, { label: 'Cost', value: usd(d.main.usd) }]), gapMarks));
  }
  const byType = new Map();
  d.agents.forEach((a) => { if (!byType.has(a.agent_type)) byType.set(a.agent_type, []); byType.get(a.agent_type).push(a); });
  const types = [...byType.entries()].sort((a, b) => b[1].reduce((s1, z) => s1 + z.usd, 0) - a[1].reduce((s1, z) => s1 + z.usd, 0));
  types.forEach(([type, list], k) => {
    const color = SERIES[(k + 1) % SERIES.length] || OTHER_COLOR;
    const maxUsd = Math.max(...list.map((a) => a.usd), 1e-9);
    lanes.push(h('div.lab', null, h('span', null, h('b', { text: type }), ` ×${list.length}`)),
      h('div.lane', null, list.map((a) => {
        const l = x(a.first) * 100, w = Math.max((x(a.last) - x(a.first)) * 100, 0.3);
        return tipOn(h('span', { style: `left:${l.toFixed(2)}%;width:${w.toFixed(2)}%;background:${color};opacity:${(0.45 + 0.55 * a.usd / maxUsd).toFixed(2)}` }),
          `${type} · ${hm(new Date(a.first))} to ${hm(new Date(a.last))}`, [
            { label: 'Requests', value: String(a.requests) }, { label: 'Tool calls', value: String(a.tool_calls) },
            { label: 'Peak context', value: tokens(a.peak_context) }, { label: 'Cost', value: usd(a.usd) }]);
      })));
  });

  // x labels: the start, each resume after a squeezed pause, the end
  const marks = [{ t: start, label: stamp(new Date(start)) }];
  for (const g of x.gaps) marks.push({ t: g.end, label: stamp(new Date(g.end)) });
  marks.push({ t: end, label: stamp(new Date(end)) });
  const labels = marks.filter((m, i) => i === 0 || x(m.t) - x(marks[i - 1].t) > 0.08 || i === marks.length - 1);
  if (labels.length > 1 && x(labels[labels.length - 1].t) - x(labels[labels.length - 2].t) < 0.08) labels.splice(labels.length - 2, 1);
  if (!x.gaps.length) {
    for (let k = 1; k < 4; k++) labels.splice(k, 0, { t: start + (end - start) * k / 4, label: hm(new Date(start + (end - start) * k / 4)) });
    labels.sort((a, b) => a.t - b.t);
  }

  card.append(h('div.scroll', null, h('div.tl', null,
    h('div.lab', { style: 'justify-content:space-between' }, h('b', { text: 'Context per request' }), h('span', { text: main.length && dots.length ? 'tokens; dots are subagents' : 'tokens' })), ctx,
    h('div.lab', null, h('b', { text: 'Cost per request' }), h('span', { text: `scale $0 to ${usd(cmaxUsd)}` })), cost,
    h('div.lab', null, h('b', { text: 'Pauses' })), pauses,
    lanes,
    h('div'), xAxis(labels, start, end, 0, x))));
  return card;
}

function agentsTable(d) {
  const card = h('section.card', null, h('h2', { text: 'Agents in this session' }));
  if (!d.agent_types.length) { card.append(empty(null, 'No requests.')); return card; }
  card.append(h('div.scroll', null, h('table.data', null,
    h('thead', null, h('tr', null, ['Agent', 'Spawns', 'Tool calls (median)', 'Peak context (average)', 'Share of cost', 'Cost'].map((c, i) =>
      h('th', { scope: 'col', style: i && i !== 4 ? 'text-align:right' : null, text: c })))),
    h('tbody', null, d.agent_types.map((a, i) => {
      const color = a.agent_type === 'main' ? 'var(--s1)' : SERIES[i % SERIES.length];
      return h('tr', null,
        h('td', null, h('span', { style: 'display:flex;align-items:center;gap:8px;font-weight:500' }, h('span.sw', { style: `background:${color}` }), a.agent_type === 'main' ? 'main thread' : a.agent_type)),
        h('td.num', { text: a.agent_type === 'main' ? '' : String(a.spawns) }),
        h('td.num', { text: String(a.median_tool_calls), style: a.agent_type !== 'main' && a.median_tool_calls <= 2 ? 'color:var(--clay)' : null }),
        h('td.num', { text: tokens(a.average_peak_context) }),
        h('td', null, h('div.track', { style: 'width:140px' }, h('div', { style: `width:${(a.share * 100).toFixed(1)}%;background:${color}` }))),
        h('td.cost', { text: usd(a.usd) }));
    })))));
  return card;
}
