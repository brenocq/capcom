// The sessions table (Spend and the Sessions page), and the block shown for a
// part whose analysis module is not built yet.

import { h, api, usd, tokens, pct, duration, stamp, seg, empty, setQuery, sessionHref, sessionDot, ApiError, LOWER_TITLE, tipOn } from './lib.js';

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
        h(r.color ? 'span.sname' : 'span', null, sessionDot(r.color), h('span', null, r.title || 'Untitled session ' + r.session_id.slice(0, 8))),
        h('span', { text: r.project + (r.git_branch ? ' · ' + r.git_branch : ''), title: r.project_path || null }))),
      h('td.num', { text: String(r.subagents) }),
      h('td.num', { text: r.active_seconds ? duration(r.active_seconds) : '<1 min' }),
      h('td.num', { text: tokens(r.peak_context) }),
      h('td', null, h('div', { style: 'display:flex;align-items:center;gap:8px' },
        tipOn(h('div.track', { style: 'width:80px' }, h('div', { style: `width:${(r.cache_read_share * 100).toFixed(1)}%;background:var(--s1)` })),
          'Cache reads', [{ label: 'Share of tokens', value: pct(r.cache_read_share, 1), color: 'var(--s1)' }], { focus: false }),
        h('span.note.mono', { text: pct(r.cache_read_share) }))),
      h('td.num', { text: r.last ? stamp(new Date(r.last)) : '' }),
      h('td.cost', { text: (r.estimated_output ? '≥ ' : '') + usd(r.usd), title: r.estimated_output ? LOWER_TITLE : null }),
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
