# capcom serve: the JSON API

`capcom serve` listens on `127.0.0.1` only (default port 8787). Every endpoint is a
`GET` (`HEAD` gives the same headers without the body); other methods get 405. There are no CORS headers, and a request whose `Host`
header is not `127.0.0.1` or `localhost` gets 403 (DNS rebinding). Each request opens
the index read-only and closes it before answering.

The server keeps the index fresh itself: an incremental scan (`run_scan`, quiet) at
start and then a minute after each scan ends, in one background thread; a lock keeps
two scans from overlapping, and `--no-scan` turns it off. The handlers never write.

Conventions:

- Money is API-equivalent USD at list prices (`capcom.cost`), not the bill.
- Times are ISO 8601 strings in the machine's local time zone, except series and the
  session's per-request columns, which use epoch milliseconds (`ms`).
- "Today" and day windows start at local midnight; a window of `days` holds that many
  whole local days, today included.
- Placeholder replies (`<synthetic>` models) are left out, as in `capcom top`.
- Payloads carry titles, project folders, paths, model, tool and agent names, counts,
  times and dollars; never prompt, reply or spawn-description text.

## Errors

Errors are JSON: `{"error": code, "message": text, ...}`.

| Status | `error` | When |
| --- | --- | --- |
| 400 | `bad_query` | A query parameter is out of range or unknown. |
| 404 | `not_found` | Unknown endpoint or session. |
| 501 | `unavailable` | The analysis module behind it is not built yet; adds `module` and `milestone`. |
| 503 | `no_index` | No index, or one written by another capcom version: run `capcom scan`. |
| 503 | `index_busy` | SQLite could not read the index. |

Anything else is a plain-text `500 internal error` that says nothing more; the
traceback goes to the terminal only. A static name that cannot be a file (a NUL byte,
a name too long) or that resolves outside `static/` is a 404.
Page paths (`/`, `/spend`, `/sessions`, `/session/<id>`, `/spikes`, `/checks`) serve
`static/index.html`, or a 503 page saying to run `capcom scan` when the index is
missing or old. Static files are under `/static/`.

## Shared shapes

**Totals** (a `Tally`):

```
{requests, usd, tokens,
 categories: {cache_read|cache_write|output|input: {tokens, usd}},   # output includes web tools
 estimated_output: {requests, tokens, share},   # share of output tokens that are estimates ("~")
 inexact_price_requests}
```

**Group**: Totals plus `key`, `label`, `count` (sessions for projects, models and the
main thread; spawned agents for an agent type).

**Session row**: `session_id, title, project, project_dir, temporary, git_branch,
subagents, active_seconds, first, last, peak_context, cache_read_share, usd, requests,
estimated_output`. `temporary` is true for a folder under /tmp. Active time sums the gaps under 5 minutes between consecutive
requests of the session.

**Cumulative**: `{step_s, times: [ms], members: [{key, label, final}], values:
[[member values per time]], total: [per time]}`. Point *i* is the running total at the
end of step *i*, so the last total is the window's total. At most six members plus
`(other)`, which sums the rest; members come largest first, `(other)` last (from
`capcom.timeline.cumulative`). By category, web tools are counted with output.

**Spikes**: `{count, excess_usd, marks: [{n, t, end, kind, session_id, title, agent_id,
requests, cost_usd, excess_usd}]}` from `capcom.timeline.find_spikes` over the
window: `count` and `excess_usd` cover them all, `marks` the 12 largest by excess,
numbered 1.. in time order (`t`, `end` in ms). The charts draw them as numbered
markers linking to `/spikes`.

`unavailable` lists the analysis modules that are not built yet; the parts that need
them are `null`.

**Projects**: a project is labeled by the last part of a working folder seen in it.
The OpenClaw state dir is `openclaw`. Every folder under /tmp is temporary: Claude
Code's scratchpads (`/tmp/claude-<uid>/<project folder>/<session id>/...`) are
grouped as `temporary (<project>)`, other temporary folders as `(temporary)`; never by
the cwd's last part. The chart series (`capcom.timeline`) group all temporary folders
as one member, labeled `temporary folders`.

## Endpoints

### `GET /api/status`

`{now, sessions, prices_checked, index: {updated, size_bytes}}`. `updated` is the
later modification time of `index.sqlite` and `index.sqlite-wal`.

### `GET /api/overview`

```
{now, running_sessions,
 today: Block, week: Block + {days: 7},
 previous_7_days: {since, daily_average_usd},   # the 7 days before today
 spending_now: {usd_last_hour, spark: {step_s, times, values} | null},
 cumulative_today: Cumulative (by category, 5-minute steps) | null,
 limits: {available: false, message},           # no limit data until milestone 5
 unavailable}
Block = {since, totals, sessions, by_project: [Group], by_agent: [Group],
         by_model: [Group], subagents: {count, usd, two_tool_calls_or_fewer}}
```

`spikes_today: Spikes | null` marks today's chart. `by_agent` keys are `main` for
main threads, else the agent type; projects as in "Projects" above.

### `GET /api/spend?days=14&group=category`

`days` is one of 7, 14, 30, 90; `group` one of `category`, `project`, `model`, `agent`,
`session`.

```
{window: {days, since, until, group}, totals, sessions_count, temporary_sessions,
 cumulative: Cumulative | null,       # steps: 30 min (7 d), 1 h (14 d), 2 h (30 d), 6 h (90 d)
 spikes: Spikes | null,
 daily: {members: [{key, label}], days: [{date, values, total}]} | null,
 heatmap: {weekdays: [Mon..Sun], cells: [7][24] USD, max} | null,
 context_buckets: [{label, requests, usd}],   # <50k, 50-100k, ..., >600k tokens of context
 sessions: [Session row] (10 costliest),
 unavailable}
```

`daily` and `heatmap` come from `capcom.timeline.per_step` in 1-hour steps from local
midnight, folded into the same members as `cumulative`, then summed by local date and
by local weekday and hour. The window's dates count back from today's local date, and
each starts at its own local midnight, so a daylight-saving change inside the window
moves no request to the wrong day.

### `GET /api/sessions?days=14&q=&sort=usd&limit=200`

`days` 0 means all time. `q` matches the title, the project label or folder, or an
agent type of the session (case-insensitive). `sort`: `usd`, `last`, `peak_context`,
`active_seconds`, `subagents`, `requests` (descending, ties by cost; zeros sort as
numbers, missing values last).
`{window: {days, since, until}, total, sessions: [Session row]}`.

### `GET /api/session/<session_id>`

```
{session: {session_id, title, project, project_dir, cwd, git_branch, version,
           entrypoint, first_seen, last_seen, running, models, efforts},
 totals, active_seconds,
 calibration: {claude_code_usd, capcom_usd, spread, comparable, reasons,
               reason_labels} | null,
 requests: {t, agent, context, usd, output, cache_write, estimated},   # columns, time order
 thinned: {requests, kept, spikes} | null,
 main: {first, last, requests, usd, tool_calls, peak_context} | null,
 agents: [{agent_id, agent_type, first, last, requests, usd, tool_calls,
           peak_context, finished}],
 agent_types: [{agent_type, spawns, median_tool_calls, average_peak_context, usd, share}],
 pauses: [{start, end, minutes, next_usd, next_cache_write}],    # gaps of 5 minutes or more
 compactions: [{t, agent_id, trigger, pre_tokens, post_tokens}]}
```

`requests.agent` is an index into `agents`, or -1 for the main thread. A session of
more than 4,000 requests has its columns thinned for the charts (`thinned` says how
many were kept): each thread's requests, in time order, are cut into runs of
`ceil(3 n / b)`, where `b` is 4,000 less the spike requests and two per thread (at
least 1,000), and a run keeps its costliest request and its largest and
smallest context; every thread keeps its first and last request, and every request
of a spike (`find_spikes` on the session) stays. Totals, `main`, `agents` and
`pauses` count every request. `reason_labels` are the `reasons` in words (the ids
come from `capcom.cost.calibrate`). A session whose replies are all placeholders has
0 active seconds. A session is
`running` when it wrote within the last 10 minutes.

### `GET /api/live`

Sessions with a record in the last 10 minutes, fastest first:
`{window_s: 600, sessions: [{session_id, title, project, agents_active,
usd_last_10_min, usd_per_hour, last_seen}]}`. The rate is the last 10 minutes' spend
times six.

### `GET /api/spikes?days=14` and `GET /api/checks?days=14`

501 until `capcom.timeline.find_spikes` and `capcom.explain` (milestone 3) or
`capcom.checks` (milestone 4) are built; then `{days, spikes, explanations}` and
`{days, findings}`, the `capcom.types` dataclasses as JSON.
