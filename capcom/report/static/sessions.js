// The sessions table (Spend and the Sessions page), and the Spikes and Checks
// pages, which wait for milestones 3 and 4.

import { h, api, usd, tokens, pct, duration, stamp, seg, empty, setQuery, sessionHref, ApiError } from './lib.js';

export function sessionsTable(rows, sort, onSort) {
  const th = (key, label, cls) => {
    const inner = onSort && key ? h('button', { type: 'button', 'aria-pressed': String(sort === key), onclick: () => onSort(key) }, label) : label;
    return h('th', { scope: 'col', style: cls === 'num' ? 'text-align:right' : null }, inner);
  };
  return h('div.scroll', null, h('table.data', null,
    h('thead', null, h('tr', null,
      th(null, 'Session'), th('subagents', 'Subagents', 'num'), th('active_seconds', 'Active', 'num'),
      th('peak_context', 'Peak context', 'num'), th(null, 'Cache reads'), th('last', 'Last record', 'num'), th('usd', 'Cost', 'num'))),
    h('tbody', null, rows.map((r) => h('tr', null,
      h('td', null, h('a.title', { href: sessionHref(r.session_id) },
        h('span', { text: r.title || 'Untitled session ' + r.session_id.slice(0, 8) }),
        h('span', { text: r.project + (r.git_branch ? ' · ' + r.git_branch : '') }))),
      h('td.num', { text: String(r.subagents) }),
      h('td.num', { text: r.active_seconds ? duration(r.active_seconds) : '<1 min' }),
      h('td.num', { text: tokens(r.peak_context) }),
      h('td', null, h('div', { style: 'display:flex;align-items:center;gap:8px' },
        h('div.track', { style: 'width:80px' }, h('div', { style: `width:${(r.cache_read_share * 100).toFixed(1)}%;background:var(--s1)` })),
        h('span.note', { text: pct(r.cache_read_share) + ' of tokens' }))),
      h('td.num', { text: r.last ? stamp(new Date(r.last)) : '' }),
      h('td.cost', { text: (r.estimated_output ? '~' : '') + usd(r.usd), title: r.estimated_output ? 'Part of the output is estimated from content length' : null }),
    )))));
}

const RANGES = [[1, 'Today'], [7, '7 days'], [14, '14 days'], [30, '30 days'], [0, 'All']];

export async function renderSessions(main, r) {
  const days = r.query.has('days') ? Number(r.query.get('days')) : 14;
  const sort = r.query.get('sort') || 'usd';
  const q = r.query.get('q') || '';
  const params = new URLSearchParams({ days, sort, limit: 300 });
  if (q) params.set('q', q);
  const d = await api('sessions?' + params.toString());
  const shown = d.sessions.length < d.total ? `, the ${d.sessions.length} costliest shown` : '';
  main.append(h('header.head', null,
    h('div.titles', null, h('h1', { text: 'Sessions' }),
      h('p.sub', { text: `${d.total} session${d.total === 1 ? '' : 's'}${q ? ` matching “${q}”` : ''}${shown}` })),
    h('div.controls', null, seg('Range', RANGES, days, (v) => setQuery({ days: v })))));
  const card = h('section.card');
  if (q) card.append(h('div.split', null, h('span.note', { text: 'Filtered by project, title or agent type.' }), h('a.link', { href: `/sessions?days=${days}`, text: 'Clear the search' })));
  card.append(d.sessions.length ? sessionsTable(d.sessions, sort, (k) => setQuery({ sort: k })) : empty(null, 'No sessions match.'));
  main.append(card);
}

const PENDING = {
  spikes: { title: 'Spikes', milestone: 3, text: 'Jumps in spend, each with the cause the logs point to (an idle cache rebuild, a compaction, a fan-out of subagents, a large tool result).' },
  checks: { title: 'Checks', milestone: 4, text: 'Setup warnings drawn from your usage, ranked by the estimated weekly saving, each with its evidence and the change to make.' },
};

// A short block for a part that waits on a later milestone (shared with Overview).
export async function pendingBlock(kind, query = '') {
  try {
    const d = await api(kind + (query ? '?' + query : ''));
    return { data: d };
  } catch (err) {
    if (err instanceof ApiError && err.status === 501) {
      return { block: empty(`Coming in milestone ${err.body.milestone || PENDING[kind].milestone}`, PENDING[kind].text) };
    }
    throw err;
  }
}

export async function renderPending(main, kind) {
  const p = PENDING[kind];
  main.append(h('header.head', null, h('div.titles', null, h('h1', { text: p.title }), h('p.sub', { text: 'Last 14 days' }))));
  const { data, block } = await pendingBlock(kind, 'days=14');
  const card = h('section.card');
  if (block) card.append(block);
  else if (kind === 'checks') {
    card.append(data.findings.length ? h('div.blist', null, data.findings.map((f) => h('div', { style: 'display:flex;justify-content:space-between;gap:12px;border-top:0.5px solid var(--l12);padding-top:10px' },
      h('span', { style: 'display:flex;flex-direction:column;gap:2px' }, h('b', { text: f.title }), h('span.note', { text: f.summary })),
      h('span.mono', { style: 'color:var(--clay)', text: usd(f.saving_usd_per_week, 0) + '/wk' })))) : empty(null, 'No findings in this window.'));
  } else {
    const items = data.explanations.length ? data.explanations : data.spikes.map((sp) => ({ spike: sp, title: sp.kind.replace('_', ' '), excess_usd: sp.excess_usd }));
    card.append(items.length ? h('div.blist', null, items.map((e) => h('a', { href: e.spike.session_id ? sessionHref(e.spike.session_id) : '#', style: 'display:flex;justify-content:space-between;gap:12px;text-decoration:none;border-top:0.5px solid var(--l12);padding-top:10px' },
      h('span', { style: 'display:flex;flex-direction:column;gap:2px' }, h('b', { text: e.title }), h('span.note', { text: stamp(new Date(e.spike.start)) })),
      h('span.mono', { style: 'color:var(--clay)', text: '+' + usd(e.excess_usd) })))) : empty(null, 'No spikes in this window.'));
  }
  main.append(card);
}
