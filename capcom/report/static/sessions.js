// The sessions table (Spend and the Sessions page), and the block shown for a
// part whose analysis module is not built yet.

import { h, api, usd, duration, stamp, seg, empty, setQuery, sessionHref, sessionDot, ApiError, LOWER_TITLE } from './lib.js';

// The sort a click on a column header leads to: a new column sorts descending,
// then the same column ascending, then no sort (the default order, costliest first).
// `sort` null is no sort; returns { sort, dir } with both null for no sort.
export function nextSort(sort, dir, key) {
  if (sort !== key) return { sort: key, dir: 'desc' };
  if (dir !== 'asc') return { sort: key, dir: 'asc' };
  return { sort: null, dir: null };
}

const SORT_NAMES = { desc: 'descending', asc: 'ascending' };

// `sort` is the active column (null for none) and `dir` its direction; without
// `onSort` the headers are plain text and the rows stay in the given order.
export function sessionsTable(rows, sort, onSort, dir = 'desc') {
  const th = (key, label, cls) => {
    const style = cls === 'num' ? 'text-align:right' : null;
    if (!onSort || !key) return h('th', { scope: 'col', style }, label);
    const on = sort === key ? (dir === 'asc' ? 'asc' : 'desc') : null;
    const next = nextSort(sort, on, key);
    const hint = next.sort ? `sort by ${label.toLowerCase()}, ${SORT_NAMES[next.dir]}` : 'back to the default order';
    return h('th', { scope: 'col', style, 'aria-sort': on ? SORT_NAMES[on] : 'none' },
      h('button', { type: 'button', title: 'Click to ' + hint, onclick: () => onSort(next.sort, next.dir) },
        label, h('span.sortarrow', { 'aria-hidden': 'true', text: on === 'asc' ? '↑' : on === 'desc' ? '↓' : '' })));
  };
  return h('div.scroll', null, h('table.data', null,
    h('thead', null, h('tr', null,
      th(null, 'Session'), th('subagents', 'Subagents', 'num'), th('active_seconds', 'Active', 'num'),
      th('prompts', 'Prompts', 'num'), th('usd_per_active_hour', 'Cost / active hour', 'num'), th('last', 'Last record', 'num'), th('usd', 'Cost', 'num'))),
    h('tbody', null, rows.map((r) => h('tr', null,
      h('td', null, h('a.title', { href: sessionHref(r.session_id) },
        h(r.color ? 'span.sname' : 'span', null, sessionDot(r.color), h('span', null, r.title || 'Untitled session ' + r.session_id.slice(0, 8))),
        h('span', { text: r.project + (r.git_branch ? ' · ' + r.git_branch : ''), title: r.project_path || null }))),
      h('td.num', { text: String(r.subagents) }),
      h('td.num', { text: r.active_seconds ? duration(r.active_seconds) : '<1 min' }),
      h('td.num', { text: r.prompts == null ? '–' : String(r.prompts) }),
      h('td.num', { text: r.usd_per_active_hour == null ? '–' : usd(r.usd_per_active_hour),
        title: r.usd_per_active_hour == null ? 'Under 5 minutes of active time: too short for a rate' : null }),
      h('td.num', { text: r.last ? stamp(new Date(r.last)) : '' }),
      h('td.cost', { text: (r.estimated_output ? '≥ ' : '') + usd(r.usd), title: r.estimated_output ? LOWER_TITLE : null }),
    )))));
}

const RANGES = [[1, 'Today'], [7, '7 days'], [14, '14 days'], [30, '30 days'], [0, 'All']];

export async function renderSessions(main, r) {
  const days = r.query.has('days') ? Number(r.query.get('days')) : 14;
  const sort = r.query.get('sort') || null;
  const dir = sort && r.query.get('dir') === 'asc' ? 'asc' : 'desc';
  const q = r.query.get('q') || '';
  const params = new URLSearchParams({ days, sort: sort || 'usd', limit: 300 });
  if (dir === 'asc') params.set('dir', 'asc');
  if (q) params.set('q', q);
  const d = await api('sessions?' + params.toString());
  const costliest = (sort || 'usd') === 'usd' && dir === 'desc';
  const shown = d.sessions.length < d.total ? `, the ${costliest ? `${d.sessions.length} costliest` : `first ${d.sessions.length}`} shown` : '';
  main.append(h('header.head', null,
    h('div.titles', null, h('h1', { text: 'Sessions' }),
      h('p.sub', { text: `${d.total} session${d.total === 1 ? '' : 's'}${q ? ` matching “${q}”` : ''}${shown}` })),
    h('div.controls', null, seg('Range', RANGES, days, (v) => setQuery({ days: v })))));
  const card = h('section.card');
  if (q) card.append(h('div.split', null, h('span.note', { text: 'Filtered by project, title or agent type.' }), h('a.link', { href: `/sessions?days=${days}`, text: 'Clear the search' })));
  card.append(d.sessions.length ? sessionsTable(d.sessions, sort, (k, v) => setQuery({ sort: k, dir: v === 'asc' ? 'asc' : null }), dir) : empty(null, 'No sessions match.'));
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
