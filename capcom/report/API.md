# capcom serve: the JSON API

`capcom serve` listens on `127.0.0.1` only (default port 8787). Every endpoint is a
`GET` (`HEAD` gives the same headers without the body) but two: `POST /api/changes`
(the marks of "Did it work?", below) and `POST /api/ask` (Ask Roger, at the end);
other methods and paths get 405. There are no CORS headers, and a request whose `Host`
header is not `127.0.0.1` or `localhost` gets 403 (DNS rebinding). Each request opens
the index read-only and closes it before answering.

Replies are cached per scan generation: the key holds the size and modification time
of `index.sqlite` and its `-wal`, the background scanner's count of finished scans,
the local date, the endpoint and its query, so the first call after a scan computes
again and a repeat call is answered from memory. Entries live at most 5 minutes (some
numbers, such as "the last hour", move with the clock alone) and the cache holds at
most 32 MB of encoded replies, least recently used out first. `status` and `live` are
never cached; errors are never cached.

### Progressive replies

A reply that is not cached is computed as a job, off the request's thread, keyed like
the cache (two requests for the same view share one job). When the job is done within
0.3 s the request gets the normal `200`. Otherwise it gets `202 Accepted`:

```
{"status": "computing", "job": id, "progress": 0..1,
 "source": "chunks" | "stale" | null, "partial": <reply so far> | null}
```

`partial` has the same shape as the final reply, so a page can draw either. With
`source: "chunks"` it is this job's reply so far (`spikes`: the whole window's spikes,
count, excess and projects, with the causes and listed spikes of the sessions explained
so far, the sessions of the largest spikes first); with `"stale"` it is the newest reply
of the same view from before the index changed (or from an earlier day). A page
that switches a view of the page it shows (a group or range) draws no stand-in from
before over it, nor a reply it kept that came in before the one on screen: those
could show older totals than the page already does. The client
polls the same URL with `&job=<id>`; a poll waits on the server (at most 0.5 s) until
the job has a newer partial reply or ends, and gets `202` with newer progress and
partial replies, then the final `200` (or the endpoint's error). `progress` never goes
back; between the job's own steps it is estimated from how long the same view took last
time. A `job` the server no longer knows is ignored: the request is answered as if it
were the first. When the index's generation changes, a background job is dropped,
while a page's job finishes the reply its page polls for (its `job` keeps being
followed; a request without it starts a new job on the new data). A job stops after 120 s (the poll gets `504 {"error": "timeout"}`), and
runs at low priority once no page polled it for 2.5 s. `status` and `live` are always
answered at once.

A request with the header `X-Capcom-Priority: low` (a page's prefetch, on a hover) is
background work: its job runs at low priority and its polls do not lift it; the same
request without the header (the click) lifts the same job to a page's priority.

At most 3 jobs compute at once. Background work (low-priority jobs, and the warmer)
waits while any page's request or job is in flight, and for 0.5 s after the last one,
so a click never shares the CPU with it. The warmer computes the views pages asked for
in the last 30 minutes (newest first) and then every page's default view (Overview's,
Spend's and Limits' split by the group a page asked for last), after startup
and whenever the index changes, in a child process of its own (`capcom.report.worker`),
which is stopped (SIGSTOP) while a page needs the CPU and continued after; it rests
three times as long as each reply took (about a quarter of a core at most), and it
stops after 30 minutes without a page request.

The server keeps the index fresh itself: an incremental scan (`run_scan`, quiet) at
start and then a minute after each scan ends, in one background thread; a lock keeps
two scans from overlapping, and `--no-scan` turns it off. The handlers never write.
The scan prints nothing but the files that failed; when Ctrl+C stops the server in the
middle of a scan, the terminal says so (the next scan reads the rest).

Conventions:

- Money is API-equivalent USD at list prices (`capcom.cost`), not the bill.
- Times are ISO 8601 strings in the machine's local time zone, with their offset
  (spike starts and ends, chart markers, pauses, compactions, threads' first and
  last request included). The only epoch milliseconds (`ms`) are the x values of
  charts: series `times`, the session's per-request column `requests.t`, and the
  limits' point arrays
  (`fan`, `history`, `points`). Dates without a time (`regime.day`) are `YYYY-MM-DD`.
- "Today" and day windows start at local midnight; a window of `days` holds that many
  whole local days, today included.
- Placeholder replies (`<synthetic>` models) are left out, as in `capcom top`.
- Payloads carry titles, project folders, paths, model, tool and agent names, counts,
  times and dollars; never prompt or reply text. The session detail labels each
  subagent with its spawn description, the few words Claude Code's Agent call names
  the task with; no other endpoint carries it.
- Session titles are Claude Code's own generated titles (the `ai-title` record it
  derives from the conversation). capcom shows them as labels and stores nothing
  else of the conversation.
- Estimated output undercounts (content length misses thinking), so output and any
  total that includes it is a lower bound when `estimated_output.requests` > 0; the
  pages show it with "≥".

## Errors

Errors are JSON: `{"error": code, "message": text, ...}`.

| Status | `error` | When |
| --- | --- | --- |
| 400 | `bad_query` | A query parameter is out of range or unknown. |
| 400 | `bad_request`, `bad_json` | `POST /api/changes` or `/api/ask`: a body outside the schema, or not JSON. |
| 403 | `bad_token`, `cross_origin` | `POST /api/changes` or `/api/ask` without this launch's token, or from another origin. |
| 404 | `not_found` | Unknown endpoint, session or change. |
| 405 | | A method other than GET and HEAD, except `POST /api/changes` and `/api/ask`; the first under `--read-only` too, the second under `--no-ask`. |
| 409 | `already_marked` | `POST /api/changes`: the finding already has an open mark. |
| 411, 413, 415 | `no_length`, `too_large`, `bad_type` | `POST /api/changes` or `/api/ask`: no Content-Length, a body over 4 KB (20 KB for `/api/ask`, or one of its fields past its limit), not `application/json`. |
| 429 | `busy` | `POST /api/ask`: two answers are being written already. |
| 503 | `no_claude` | `POST /api/ask`: no `claude` command on the PATH of `capcom serve`. |
| 501 | `unavailable` | The analysis module behind it is not built yet; adds `module` and `milestone`. |
| 503 | `no_index` | No index, or one written by another capcom version: run `capcom scan`. Adds `scanning`: true when serve's scan is running (the message then says the logs are being read). |
| 503 | `index_busy` | SQLite could not read the index. |
| 504 | `timeout` | A job ran past its time limit (see Progressive replies). |

Anything else is a plain-text `500 internal error` that says nothing more; the
traceback goes to the terminal only. A static name that cannot be a file (a NUL byte,
a name too long) or that resolves outside `static/` is a 404.
Page paths (`/`, `/spend`, `/sessions`, `/session/<id>`, `/spikes`, `/checks`, `/changes`,
`/changes/<id>`, `/limits`, `/limits/window/<id>/<kind>/<start>`) serve `static/index.html` with this launch's write token in
`<meta name="capcom-token">` (and `<meta name="capcom-read-only" content="1">` under
`--read-only`, `<meta name="capcom-ask" content="off">` under `--no-ask`), or a 503 page saying to run `capcom scan` when the index is
missing or old. Static files are under `/static/`.

## Shared shapes

**Totals** (a `Tally`):

```
{requests, usd, tokens,
 categories: {cache_read|cache_write|output|input: {tokens, usd}},   # output includes web tools
 estimated_output: {requests, tokens, share},   # share of output tokens that are estimates ("≥")
 inexact_price_requests}
```

**Group**: Totals plus `key`, `label`, `count` (sessions for projects, models and the
main thread; spawned agents for an agent type); project groups add `path`, the full
folder (`~` for home) for a tooltip. A model is named by its price-table id (a session's
`models` too), as the charts split by model, so a model logged under its alias and its
dated id is one group.

**Session row**: `session_id, title, project, project_path, project_dir, temporary, git_branch,
subagents, active_seconds, first, last, peak_context, cache_read_share, usd, requests,
estimated_output`. `temporary` is true for a folder under a temporary root (/tmp; on macOS also /private/tmp and $TMPDIR under /var/folders). Active time sums the gaps under 5 minutes between consecutive
requests of the session.

**Cumulative**: `{step_s, times: [ms], members: [{key, label, final}], values:
[[member values per time]], total: [per time]}`. Point *i* is the running total at the
end of step *i*, so the last total is the window's total. At most six members plus
`(other)`, which sums the rest; members come largest first, `(other)` last (from
`capcom.timeline.cumulative`). By category, web tools are counted with output.

**Spikes**: `{count, excess_usd, explained, marks: [{n, key, t, end, kind, scope,
session_id, title, project, project_path, agent_id, requests, cost_usd, excess_usd, cause, cause_label,
confidence, headline}]}` from `capcom.timeline.find_spikes` over the window: `count`
and `excess_usd` cover them all, `marks` the 12 largest by excess, numbered 1.. in time
order (`t`, `end` ISO). The charts draw them as numbered markers linking to
`/spikes?spike=<key>`. `cause`, `cause_label`, `confidence` and `headline` (the title)
are the main cause from `capcom.explain` (its largest share of the excess), null when
there is none; `project` is the session's project label (`project_path` its full
folder), null without a session; `explained` is false while `capcom.explain` is not available.

`unavailable` lists the analysis modules that are not built yet; the parts that need
them are `null`.

**Regime** ("what changed", `capcom.report.story` over `capcom.timeline.regime_changes`):

```
{day, before_usd_per_day, after_usd_per_day, after_days, ratio, rise_usd_per_day,
 sessions: [{key, label, title, project, rise_usd, share, rest, in_text}],
 agent_types: [{key, label, rise_usd, share, rest}],
 text}
```

The latest day of the window where the mean daily spend rose to a new level: `after`
is the mean of up to 7 days from `day`, `before` of the 7 days before it (which may
precede the window), `ratio` their quotient (null when nothing was spent before).
Only whole local days count. `sessions` and `agent_types` split the rise
(`rise_usd_per_day`): the top 3 by their own rise (`share` of it), then one
`rest: true` entry (`key` null) so they add up; an agent type's `key` is `main` for
main threads. `in_text` marks the sessions `text` names: "Spend rose ×11.9 from Oct
16 ($10 to $119 a day): improved d3f68339 (52%) and toy-brain bcbbaac4 (35%); coder
subagents 53%." (`capcom top` and `capcom spikes` print the same line). Null when the
spend kept its level.

**Projects**: one display rule, `capcom.timeline.groups.ProjectNames`, shared with
`capcom top --by project`. A project folder's path is a session's cwd, or the ancestor
of that cwd whose folder form is the project folder (a session that moved into a
subfolder still names its repo); the label is that path's last part, the full path
when two folders share it, and the folder name itself when no cwd explains it. The
OpenClaw state dir is `openclaw` and the folders under it `openclaw (<path under
it>)`, e.g. `openclaw (workspace)`, each its own row. Every folder under /tmp (on
macOS also /private/tmp and $TMPDIR under /var/folders) is temporary: Claude Code's
scratchpads (`<tmp>/claude-<uid>/<project folder>/<session
id>/...`) are grouped as `temporary (<project label>)`, other temporary folders as
`(temporary)`. The chart series (`capcom.timeline`) group all temporary folders as one
member, labeled `temporary folders`, and all OpenClaw folders as one, labeled
`openclaw (all folders)`.

## Endpoints

### `GET /api/status`

`{now, sessions, prices_checked, scanning, index: {updated, size_bytes}}`. `updated` is
the later modification time of `index.sqlite` and `index.sqlite-wal`. `scanning` is
true while serve's first scan has not finished: totals may be partial until then.

### `GET /api/version`

`{index, samples, marks, updated, launch}`: short digests of the index's generation
(which, once it moved, holds a minute while the scan keeps writing), the usage samples' stamp and the marks file's stamp, when the index was last written, and a
random id of this launch of `capcom serve` (not the write token). The pages
ask for it every 10 s while the tab is visible and redraw the view they show when a part
it reads changed (the index for every view; the samples for Overview and Limits; the
marks for Checks and Did it work?); a new `launch` is a restarted server, so the page
reads its new write token from a fresh `/`. It opens no index and is never a job: a few
`stat` calls and one `PRAGMA data_version`.

### `GET /api/colors`

`{palette, general, project, agent, model}`: one color per project, agent type and
model, the same on every page (`colors.py`). `palette` is every color in use as
`[light, dark]` hex; its first `general` entries are the palette for entities without
a Claude Code color, which the pages also use, by a hash of the name, for a key the
reply does not hold. `project`, `agent` and `model` map every key and label the other
replies name an entity by (a project's folder key and its display label, OpenClaw's
and the temporary folders' merged series, `main` and the unknown agent type, a
model's price-table id) to its index in `palette`. A project takes the Claude Code
`/color` most of its last ten colored sessions use, an agent type its definition's
`color:`; another entity of the same kind with the same color takes a shade of it,
and the rest the first free general color, the earliest seen first. Cached per index
generation like the other replies.

### `GET /api/overview?group=category|project|model|agent`

`group` (default `category`, the same names as `/api/spend`) splits today's
cumulative chart only. `capcom serve` makes the reply for one group from its cached
reply for another (same index, same usage samples): only that chart is computed again,
up to the cached reply's `now`.

```
{now, running_sessions,
 today: Block, week: Block + {days: 7, full_days: 6, elapsed_days},
 previous_7_days: {since, daily_average_usd,     # the 7 days before today
                   same_hour_average_usd, same_hour},
 spending_now: {usd_last_hour, spark: {step_s, times, values} | null},
 cumulative_today: Cumulative (by `group`, 5-minute steps, top 6) | null,
 group,
 spikes_today: Spikes | null,
 regime: Regime | null,                          # a change in the last 14 days
 limits: Limits without segments,                # see /api/limits
 unavailable}
Block = {since, totals, sessions, by_project: [Group], by_agent: [Group],
         by_model: [Group], subagents: {count, usd, two_tool_calls_or_fewer}}
```

`spikes_today: Spikes | null` marks today's chart. Today so far is compared with
`same_hour_average_usd`: the spend of each of the previous 7 days up to the same local
time of day (`same_hour`, "HH:MM"), averaged; `daily_average_usd` is over the full
days. The week holds 6 full days and today so far; its daily average divides by
`elapsed_days` (about 6.4 in the afternoon), not by 7. `by_agent` keys are `main` for
main threads, else the agent type; projects as in "Projects" above.

### `GET /api/spend?days=14&group=category`

`days` is one of 7, 14, 30, 90; `group` one of `category`, `project`, `model`, `agent`
(also accepted as `agent_type`), `session`.

```
{window: {days, since, until, group}, totals, sessions_count, temporary_sessions,
 cumulative: Cumulative | null,       # steps: 30 min (7 d), 1 h (14 d), 2 h (30 d), 6 h (90 d)
 spikes: Spikes | null,
 regime: Regime | null,               # a change dated inside the window
 daily: {members: [{key, label}], days: [{date, values, total}]} | null,
 heatmap: {weekdays: [Mon..Sun], cells: [7][24] USD, max,
           hours: [{start, usd, project}],   # each local hour with spend, in time order
           top_projects: [7][24] label | null} | null,
 context_buckets: [{label, requests, usd}],   # <50k, 50-100k, ..., >600k tokens of context
 sessions: [Session row] (10 costliest),
 unavailable}
```

`daily` and `heatmap` come from `capcom.timeline.per_step` in 1-hour steps from local
midnight, folded into the same members as `cumulative`, then summed by local date and
by local weekday and hour. The window's dates count back from today's local date, and
each starts at its own local midnight, so a daylight-saving change inside the window
moves no request to the wrong day.

`heatmap.hours` lists the same hourly steps one by one (only those with spend): `start`
is the local hour's start as ISO 8601 with its UTC offset (`2026-10-21T05:00:00+02:00`),
`usd` its spend over every member, `project` its costliest project's label. A page lays
out the past days by date from it and finds the dates behind a weekday-and-hour cell;
its `usd` add up to the cells. `top_projects` is the costliest project of each cell
over the window, null for a cell without spend.

`sessions_count` leaves out the sessions in temporary folders (/tmp), counted apart in
`temporary_sessions`; together they are `/api/sessions`' `total` for the same `days`
(whose `temporary` counts the same temporary sessions).

### `GET /api/day/<YYYY-MM-DD>?group=category|project|model|agent`

One local day, the page a bar of Spend's per-day chart opens: what it cost against a
typical day, and where it went. 400 for a malformed date or an unknown `group`, 404 for
a day not yet come. `group` (default `category`, the same names as `/api/spend`) splits
the cumulative chart only; as for Overview, `capcom serve` makes the reply for one
group from its cached reply for another, computing only that chart again over the
cached reply's own window.

```
{day, today, window: {since, until},  # until: the next midnight, or now for today
 totals, sessions_count, temporary_sessions,
 typical: {days, of_days, usd, categories: {cache_read, cache_write, output, input}} | null,
 cumulative: Cumulative | null,       # by `group`, 10-minute steps, top 6
 group,
 spikes: Spikes | null,
 by_project: [Group + {typical_usd}], by_agent: [Group], by_model: [Group],
 sessions: [Session row] (10 costliest),
 unavailable}
```

`typical` is the median of the days with spend among the 14 before the day (`days` of
`of_days`), in all and by cost category; each project's `typical_usd` is its median on
those same days. Days without spend do not count: days off would pull the median
toward zero and make any working day look expensive. `typical` is null when none of
the 14 days had spend.

### `GET /api/hour?start=<ISO 8601>`

One local clock hour, the panel a cell of Spend's heatmap opens: why it cost what it
did. `start` is the hour's start as ISO 8601, with a UTC offset (`Z` or `+02:00`; a
`+` must be escaped as `%2B`, though one left unescaped is read back) or without one
(a wall time in the server's zone), as in `heatmap.hours[].start`. 400 for a
malformed time or one that is not the start of a local hour, 404 for an hour not yet
come. Replies are cached per index generation like the others, keyed by `start`.

```
{window: {start, end, until},         # ISO 8601 with the local offset; until: end, or now
 partial,                             # true for the current hour (until < end)
 totals,                              # Totals: its `categories` split the hour by token kind
 by_project: [Group], by_agent: [Group],
 spikes: [Spike item] | null,         # those overlapping the hour, largest excess first, at most 8
 causes: [{cause, cause_label, cause_color, excess_usd, spikes, title}] | null,
 sessions_count, sessions: [Session row] (5 costliest),
 unavailable}
```

The hour is the absolute span [start, start + 1 h), so on the night clocks fall back
the two hours read 02:00 are two hours with two starts (`+02:00` and `+01:00`).
`by_project`, `by_agent` and `totals.categories` each add up to `totals.usd`. `spikes`
are the spikes of `/api/spikes` (same shape, with their `explanations`) that start
before the hour ends and end after it starts, including one that began up to 12 hours
before. `causes` sums their named causes (never the unexplained rest) by cause, largest
first; `title` is the headline of the largest. `spikes` is null without
`capcom.timeline`, `causes` without `capcom.explain` (both listed in `unavailable`).

### `GET /api/sessions?days=14&q=&sort=usd&limit=200`

`days` 0 means all time. `q` matches the title, the project label or folder, or an
agent type of the session (case-insensitive). `sort`: `usd`, `last`, `peak_context`,
`active_seconds`, `subagents`, `requests` (descending, ties by cost; zeros sort as
numbers, missing values last).
`{window: {days, since, until}, total, temporary, sessions: [Session row]}`: `total`
counts every matching session, temporary folders included (`temporary` of them).

### `GET /api/session/<session_id>`

```
{session: {session_id, title, project, project_path, project_dir, cwd, git_branch, version,
           entrypoint, first_seen, last_seen, running, models, efforts,
           effort_counts},   # effort_counts: {effort: requests}, most used first
 totals, active_seconds,
 calibration: {claude_code_usd, capcom_usd, spread, comparable, reasons, reason_labels,
               part: {since, until, first, last, process, processes, whole_session, text},
               session_usd, outside_usd, output_above_usd, output_range, unlogged_usd,
               unlogged_text, unlogged_mostly, logged_more_usd, logged_more_text,
               logged_more_mostly, rest_usd, note} | null,
 requests: {t, agent, context, usd, output, cache_write, estimated},   # columns, time order
 thinned: {requests, kept, spikes} | null,
 main: {first, last, requests, usd, tool_calls, peak_context} | null,
 agents: [{agent_id, agent_type, description, first, last, requests, usd, tool_calls,
           peak_context, finished}],   # time order; description null when not logged
 agent_types: [{agent_type, spawns, tool_calls, median_tool_calls, average_peak_context,
                usd, share}],   # main: spawns and median_tool_calls null (one thread)
 pauses: [{start, end, minutes, next_usd, next_cache_write}],    # gaps of 5 minutes or more
 compactions: [{t, agent_id, trigger, pre_tokens, post_tokens}],
 spikes: {count, excess_usd, explained, items: [Spike item]} | null}
```

`spikes` are the session's (`find_spikes` on it), the 8 largest by excess in `items`
(as in `/api/spikes`, without `n` and `window`; `explanations` is null when
`capcom.explain` is not available); null when `capcom.timeline` is not.

`requests.agent` is an index into `agents`, or -1 for the main thread. A session of
more than 4,000 requests has its columns thinned for the charts (`thinned` says how
many were kept), to about 4,000 points whatever the number of threads: the spike
requests (`find_spikes` on the session) get up to half of them, all when they fit,
else cut into runs that each keep their costliest request and their largest and
smallest context; the other requests get the rest the same way, in the shortest runs
that fit, a thread with at least a run's worth of requests in runs of its own (the
main thread keeps its line) and the smaller threads' requests together. The
session's first and last request stay. Totals, `main`, `agents` and
`pauses` count every request. `reason_labels` are the `reasons` in words (the ids
come from `capcom.cost.calibrate`). Claude Code's cost-state counts the processes
that wrote one, step by step (a resumed process carries the totals on, and the growth
of `totalDuration` dates its step): `part` is the stretch of the session they compare,
from the first step's start (`since`) to the last step's end (`until`, null while
open), with its first and last request; `process` is the number of steps compared
and `processes` adds one per gap between steps (or before the first, after the last)
that holds requests, each counted as one process that wrote no cost-state (or has
not yet). `whole_session` is false
when the session has requests outside the steps. `capcom_usd` is the compared part;
`session_usd` is every request, as `totals.usd`; `outside_usd` the spend in no step
(capcom right, never a mismatch). `output_above_usd` is cost-state's output above
capcom's estimate, priced, where capcom estimated output, and `output_range` says
"≥ $X, up to $Y" (null when there is none); `unlogged_usd` prices cost-state's other
tokens above the log's (calls the log does not record), `unlogged_text` names them,
`logged_more_usd` / `logged_more_text` the log's tokens above cost-state's, priced
the same way (all three inferred from token counts), and `rest_usd` is what is left
unexplained. `note` says all of it in a few short
sentences, else null. `capcom calibrate` prints the same part per session (`--days`, `--project`). A session whose replies are all placeholders has
0 active seconds. A session is
`running` when it wrote within the last 10 minutes.

### `GET /api/live`

Sessions with a record in the last 10 minutes, fastest first:
`{window_s: 600, sessions: [{session_id, title, project, agents_active,
usd_last_10_min, usd_per_hour, last_seen}], watching, index_updated?}`. The rate is the
last 10 minutes' spend times six. `watching` is false when this server never scans
(`serve --no-scan`): the index then stops at `index_updated` (ISO), so an empty list
means nothing new was read, not that no session ran.

### `GET /api/spikes?days=14&project=<label>&cause=<cause>`

501 (`module` `timeline` or `explain`, milestone 3) until both are built. `days` is
1 to 366. `project` (optional) is a project label, its full folder or its project
folder; a project no session has is a 400 `bad_query`. `cause` (optional) is a cause id
(`capcom.types.CauseKind`, e.g. `tool_wait_expiry`); an unknown one is a 400.

```
{days, since, until, count, excess_usd,
 explained,                                     # spikes with a cause other than unexplained
 largest,                                       # key of the largest spike: the default pick
 cause,                                         # the cause filter, null without one
 project, project_path,                         # the filter, null without one
 by_project: [{project, project_path, excess_usd, spikes}],   # largest first
 causes: [{cause, label, color, excess_usd, explanations}],  # largest first, unexplained last
 spikes: [Spike item + {n, window}]}
Spike item = {key, kind, scope, start, end, session_id, agent_id, agent_type, title,
              project, project_path, requests, cost_usd, baseline_usd, excess_usd,
              cause, cause_label, cause_color, confidence, headline,   # the main cause
              explanations: [{cause, cause_label, confidence, confidence_label,
                              excess_usd, title, summary, advice, evidence: [Evidence]}]}
Evidence = {label, session_id, agent_id, timestamp, message_id, event_key}
window = {start, step_s, usual: [usd], extra: [usd], spike_start, spike_end}   # ISO times
```

`causes` add up the explanations of every spike of the window (of the project's
spikes with `?project=`, which also narrows `count`, `excess_usd`, `explained` and
`spikes`); `by_project` adds up the excess of every spike of the window per project,
whatever the filter (`project` null for spikes without a session), so its sums equal
the unfiltered `excess_usd`. With `?cause=`, `count`, `excess_usd`, `explained` and
`spikes` keep the spikes with an explanation of that cause, while `causes` still
lists every cause (the page's filter chips). A cause's `color` is a CSS value: the
validated palette (`--s1`..`--s6`, in order) for tool-wait expiries, parallel
subagents' work, idle rewrites, context growth, late-resumed subagents and fan-out
(the largest causes on real data, in about that order), gray for
the others and the unexplained rest; it is the same in every window. `spikes` lists the 40
largest by excess, numbered in time order. `scope` is `thread` (one agent thread,
`agent_id` null for the main thread) or `session` (all threads of the session). The
`window` is about 36 bars around the spike (30 minutes to 3 hours each side) of the
spend of its scope: `usual` is what the requests cost at the baseline pace (the
spike's baseline shared among its requests, plus every other request), `extra` what
the spike's requests cost above it, adding up to its excess.

### `GET /api/limits?days=14&group=category`

The subscription's 5-hour and weekly windows from `capcom.limits`: the samples
`capcom record-limits` appends to `limits.jsonl` in the state folder
(`$XDG_STATE_HOME/capcom`, else `~/.local/state/capcom`: no scan rebuilds them, so they
stay out of the cache), and the seed from the index's rate-limit errors. `days` is one
of 7, 14, 30, 90 and bounds `segments`. `group` (default `category`; also accepted as
`agent_type` for `agent`) is one of `category`, `project`, `model`, `agent` and splits
each segment's spend (`by`); a split by session is a 400, as is an unknown group.
The reply's cache key also holds the size and modification time of `limits.jsonl`
(and of the recorder's account files beside it), since the recorder writes them
without a scan.

```
Limits = {available, recording, message, install, samples, last_sample, recording_since, days, group,
          windows: {five_hour: Window | null, seven_day: Window | null},  # the current account's
          accounts: [Account],          # the current one first; absent without samples
          combined: Combined | null}    # null with fewer than two accounts
Account = {id, email, label, current, samples,
           windows: {five_hour: Window | null, seven_day: Window | null}}
Combined = {usd_left, pace_usd_per_hour, pace_hours, runs_out_at,
            reset_first: {id, resets_at} | null,
            accounts: [{id, label, percent, usd_per_percent, usd_left, resets_at}]}
Window = {window, label, samples, current,
          reading: {percent, time, resets_at, source} | null,
          fit: {method, note, percent_per_usd, usd_per_percent, intervals, readings, rough,
                rate_factor, mean_abs_error, r2,
                weights: [{model, category, percent_per_mtok}],
                costs: Costs | null} | null,   # per_type fits only
          forecast: {reading, now, median, low, high, resets_at, runs_out_at,
                     runs_out_early, runs_out_late, rough, readings, rate_factor, line,
                     bucket_minutes, buckets, fan: [[ms, median, low, high]]} | null,
          reason,                       # why there is no forecast (it needs two readings, ...)
          history: [[ms, percent]],     # the current window's line, from its start at 0 %
          start, start_known,           # start_known: history's first point is the reset, not a reading
          segments: [{start, start_known, resets_at, peak, hit_limit, reset,
                      points: [[ms, percent]], usd, inferred_usd,
                      by: [{key, usd, percent}] | null,   # split by `group`
                      path}]}           # newest first; path: the window's own page
Costs = {rows: [{model, category, label, tokens, tokens_low, tokens_high,
                 usd, usd_low, usd_high, rough}],   # tokens (and dollars) per 1 %
         merged: {kinds: [label], share, usd, usd_low, usd_high, rough} | null,
         left_out: [label], min_share, rough_ratio}
```

`available` is false without any sample (`windows` both null); `recording` is true
once the status line recorder wrote a sample, and `message` is the empty state's
text until then ("Not recording yet: run `capcom limits --install` to set it
up."). A segment is one window between resets; within it the percentage only
rises. `fit.method` is `per_type` (weights per model and token category, in percent
per million tokens, by non-negative least squares) once there are enough intervals,
else `per_usd` (one percent per API-equivalent dollar); `note` says it in plain words
("About $17 of API-equivalent spend per 1% of your weekly limit, from 4 rises
(rough)."); null before two readings with a rise between them. `rate_factor` is the
fit's 80 % range on its rate (divided to multiplied by it): a t-interval on the rises'
residuals pooled with a wide prior (2.5 from a single rise), narrowing as rises come;
`rough` is fewer than 6 rises. `forecast` (current window only, from the second
reading on) is the median and the 10th to 90th percentile of the value at the reset,
from the window's spend in buckets resampled over the time left, each draw with its
own rate from `rate_factor`; when `rough`, the range also covers the straight line of
percent against time through the window's readings (`line` says it widened it).
`runs_out_at` is when the median path reaches 100 %, `runs_out_early` and
`runs_out_late` the range's ends (null: never before the reset).
`recording_since` is the first status line sample's time.
A segment whose readings all say 0 % is dropped unless it is the latest: until
something is used, Claude Code reports the 5-hour window as starting at each refresh
(its reset moving on ten minutes at a time), which is no window that ran. A 5-hour
reading with no recorded account whose session is placed only by a later entry goes
with the one account whose readings around it share its reset time.
`costs` ("what 1% costs"): a kind of token (model and category) with under
`min_share` (1 %) of the fitted spend has too few tokens to measure alone, so those
kinds are fitted as one column of their dollars (`merged`, in dollars per 1 %); each
other kind has a weight with an 80 % range from the least-squares covariance of the
kept weights. A row is left out (`left_out`) when that range reaches zero; it is
`rough` when its high end is more than `rough_ratio` (2) times its low end. A row's
dollars are its tokens per 1 % at the average price its tokens had.
`by` (the last 8 segments; up to 6 members plus `(other)`, largest first) is what the
index's requests spent in the segment split by `group`, and their fitted percent (null
without a fit). Its `key` is a project's display label, an agent type (`main` for main
threads, `(unknown agent)` for a subagent of no known type), a model's price-table id,
or a kind of token (`input`, `cache_write`, `cache_read`, `output`, web tools counted
with output). Every split adds up to the segment's `usd` and to the same percent: the
fit is linear in each kind's tokens and dollars, so the parts of a split share it out.
Usage from other machines or claude.ai counts toward the limits but is not in the
logs.

Accounts: each account (Claude Code's `oauthAccount.accountUuid`, the `id`) has its
own windows, segments, fit and forecast; `email` is its `oauthAccount.emailAddress`
(null when unknown), shown only on this local site and in `capcom limits`, and
`label` is the email, or "earlier account" for the `unknown` id (samples from before
accounts were recorded, on no known account's weekly cycle). The recorder reads only
those two keys of `~/.claude.json`. `current` is the account Claude Code was last
seen logged in with. A window's spend (`usd`, `by`) is that of the
account's sessions, through the session ids of its samples and the recorder's
session log; `inferred_usd` is the part from sessions no sample names, put on the
account Claude Code was logged in with at the time. With a single account,
everything is that account's. `combined`: before each account's weekly limit,
`usd_left` = (100 - `percent`) x the account's own `usd_per_percent` (null without
a fit); its `usd_left` is the sum of those known; `runs_out_at` is when that sum is
spent at `pace_usd_per_hour` (all accounts' spend over the last `pace_hours`), null
with nothing spent or nothing left; `reset_first` is an account whose weekly
window resets before then. The website's account filter (Overview and Limits) is applied
in the browser and kept there (localStorage), never in the URL.

### `GET /api/limit-window?account=<id>&window=5-hour&start=2026-10-22T1820&group=category`

One window of one account, for its page (`/limits/window/<id>/<5-hour|weekly>/<start>`,
linked from each segment's `path`). `window` is `5-hour` or `weekly` (or the
`five_hour`/`seven_day` names); `start` is the window's start in local time
(`YYYY-MM-DDTHHMM`, or ISO 8601): the segment starting in that minute, else the one
holding that time. 404 for an unknown account or no such window, 400 for a bad query.
Its cache key holds the samples' stamp, as `/api/limits`.

```
{account: {id, label, current, several}, window, label, start, start_known, resets_at,
 until, current, peak, hit_limit, readings,
 points: [[ms, percent]],                       # the segment's line (0 % at a known start)
 group,
 fitted: {times: [ms], percent: [p], usd: [u],  # the fit's percent of the spend, adding up
          base,                                 # where it starts: 0, or the first reading
          by: [{key, percent: [p]}]},           # split by `group`, adding up at `times`
 step_minutes, bucket_minutes,
 fit: {method, usd_per_percent, rough, intervals, note} | null,
 own_usd_per_percent,                           # this window's spend / its rise; null under 1 point
 fitted_percent, inferred_usd,
 totals: Tally,                                 # its categories carry `percent` too
 by_project, by_agent, by_model: [Group + {percent}],
 sessions: [Session + {percent}], sessions_count,  # the 10 costliest
 spikes: Spikes | null,                         # as /api/day's
 before, after,                                 # the pages of the windows around it, or null
 unavailable}
```

The spend is the account's (as the window's `usd` on `/api/limits`); `percent` is
the fit's percent of a part's spend (null without a fit). `fitted.percent` starts at
`base`: 0 at a known start, else the first reading. `group` (default `category`, as
`/api/limits`'s, with the same 400s) splits the line: `fitted.by` is each member's fit
of its own spend, adding up, at the same `times` (up to 6 members, largest at the end
first, plus `(other)`; empty without a fit), with keys as `/api/limits`'s `by`; at every
time, `base` plus the members' percents is `fitted.percent`.

### `GET /api/checks?days=14`

501 (milestone 4) until `capcom.checks` is built.

```
{days, since, until, overlapping,
 total: {usd_per_week, findings, of, all, text,
         judgement_calls: {usd_per_week, findings, items: [{kind, scope, usd_per_week}]}?} | null,
 rollups: [{kind, title, headline, saving_usd_per_week, scope_noun,
            scopes: [{scope, scope_label, saving_usd_per_week, share}],
            findings: [n], overlaps: [kind], judgement_call, counted}],
 findings: [{n, kind, title, scope, scope_label, scope_path, summary, saving_usd_per_week, advice, change,
             evidence: [Evidence], metrics, overlaps: [kind], counted, overlapped_by: [n],
             visual}],
 small: {findings, usd_per_week, below_usd_per_week},
 footer}
```

`rollups` (`capcom.checks.rollup`) is one row per kind across its scopes (projects, or
agent types for `scope_noun` "agent type"), largest first and judgement calls last:
its saving, each scope's `share` of it, the `n` of its findings, `overlaps` (the
kinds whose counted findings kept one of its findings out of the total, by the
total's own test; empty when none did), and whether its findings are all, some or
none `counted` in the total. Rows of different kinds are not to be added up (`overlaps`); the total is.
Findings under $0.10/week are not in `findings` nor in the total: `small` counts
them. `judgement_calls` (only when there are any) are findings whose saving trades
quality (a cheaper model for an agent): never in the total. `footer` says in words
what the total covers.

Findings come ranked by weekly saving; `scope_label` names a project scope as in
"Projects" (other scopes, an agent type or `all`, as they are). Some savings count the same dollars
(`overlaps`, from `metrics["overlaps"]`; `overlapping` says whether any do), so they
are never added up: `total` sums only the findings with `counted` true, picked largest
first, each one that overlaps none picked before it (`overlapped_by` lists the `n`
of those it overlaps). Two findings overlap when one's kind is in the other's
`overlaps` and their scopes may hold the same requests: the same scope, `all`, two
scopes not both project folders, or an agent type and a folder that type ran in
(`metrics.folders`, from capcom.checks); two agent types, two different folders, or
an agent type and a folder it never ran in (OpenClaw's included) never do (`of` counts every finding, `all`
says whether that is every finding); null when there are none. `text` is the sentence
`capcom checks` prints too, e.g. "up to $46.00/week across 4 findings that don't
overlap (of 5; overlapping savings are counted once)". `visual` is a small chart from the metrics, or
null:
`{type: "squares" | "split", label, verb, total, filled}` (tools or skills loaded
against those used, or sessions that loaded claude.ai connectors against those that
called one; a bar past 120), `{type: "histogram", label, buckets: [{label, count,
small}]}` (tool calls per subagent of the finding's agent type in the window, from the
index; `small` marks two calls or fewer), `{type: "dots", label, values, threshold}`
(context size when each compaction of the finding's project started, from the index),
`{type: "gauge", label, value, limit}` (memory size against its budget, the largest
context against its threshold).

Each finding also carries `finding_id`, `"<kind>@<scope>"`, the same in every window
and scan; `mark`, its open mark (a `Mark`, below) or null; and `measure`, the name of
the rate capcom.changes would measure for its kind, or null when it does not say.

### Did it work?

The user marks a finding as applied on a date; `capcom.changes` measures the
finding's own rate over the 4 weeks before that date and from the day after, weekly
(`design.md`, "Did it work?"). The marks live in `$XDG_DATA_HOME/capcom/changes.jsonl`,
the only file capcom writes besides its cache; its size and modification time are
part of the cache key of `checks` and `changes` (the replies that show marks), so a
mark shows on Checks and here at once and leaves every other reply cached. The
measures keep each finished day in `changes-days.sqlite` in the cache folder, the
file `capcom changes` keeps too, so a request reads from the index only the days not
measured before (and today). All dates are
`YYYY-MM-DD` local dates; `marked_at` and `since` are ISO 8601 times. 501
(`module` "changes") while `capcom.changes` is missing.

```
Mark = {id, finding_id, kind, scope, scope_label, applied_on, title, what, note,
        expected_usd_per_week, marked_at, source}
Result = Mark + {days_since, measure, before, after, before_range: [low, high] | null,
                 requests_per_week_before, requests_per_week_after,
                 measured_usd_per_week, verdict, verdict_due, reason,
                 evidence: [{label, before, after}], quality: [{label, before, after}],
                 window: {before_from, before_to, after_from}}
```

`what` is "suggested" (the finding's own change) or "other"; `source` "user" or
"noticed"; `note` is the user's own words, at most 500 characters, which the pages
render as text. `title` and `expected_usd_per_week` are the finding's when it was
marked. `measure` names what is counted (such as "Tool-wait rewrites per 1,000
requests"; "(no measure)" for a kind capcom cannot measure yet). The verdict compares
rates, so a busier week does not count: `working` / `worse` when the after days' rate
is below / above the before days' by a permutation test over days at 95 % and by more
than the before days' usual swing; `no_effect` otherwise; `too_early` under 7 days, or
too few requests in scope since the date (`verdict_due`: when to look again, projected
from the pace of requests since; null when none came since); `no_data` (the pages say
"Can't tell") without a measure, with too little in scope before the date, for work
that only started after it, or when the requests in scope per week changed 4-fold.
`reason` is that sentence with its numbers. `before_range` is the lowest and highest
before week (weeks with a small share of the usual workload left out).
`measured_usd_per_week` is given for working (positive, a saving) and worse
(negative), null otherwise: (before − after) × the after weeks' workload × what one
counted unit cost in the before window, API-equivalent. `evidence` holds the counts
behind the measure, `quality` the signals to judge a model change by (requests per
spawn, tool errors per 100 calls); empty otherwise. `window.before_from` is the first
before week's start, null without weeks.

#### `GET /api/changes`

```
{marks, counts: {working, no_effect, worse, too_early, no_data},
 with_verdict: {changes, measured_usd_per_week, expected_usd_per_week, share},
 next_verdict, changes: [Result], path}
```

Every open mark measured, the latest date first. `with_verdict` sums the measured and
the expected savings over the changes with a verdict (working, no clear effect or
worse); `share` is measured over expected, null without an expectation.
`next_verdict` is the earliest `verdict_due` of the changes still too early. `path`
is the marks file.

#### `GET /api/changes/<id>`

One `Result` with `weeks: [{week_start, value, requests, after}]`: the before weeks
(up to 4, clipped where the logs begin) and the weeks from the day after the date,
each starting on the applied weekday, `value` null for a week without requests in
scope; empty for a kind without a measure. 404 for an unknown id.

#### `GET /api/changes/noticed?days=14`

`{days, noticed: [{finding_id, kind, scope, scope_label, title, path, detail, since,
saving_usd_per_week}]}`: changes capcom saw in a file it already reads (an agent
definition's frontmatter, `detail` such as "model: sonnet", modified at `since`) that
answer a finding of the Checks window (`days`: 7, 14 or 30), without an open mark yet.
The page asks the user to confirm each; "Not this" is remembered in the browser only.
The reply is cached like the others, so a definition edited since shows within 5
minutes, or after a scan or a mark.

`/api/status` also carries `changes`: how many marks are open (null without
`capcom.changes`), the sidebar's count.

#### `POST /api/changes`

The one write. The request must have (and send it within 30 seconds: every request
has a socket timeout, so a body shorter than its `Content-Length` cannot hold a
thread):

- a `Host` of `127.0.0.1` or `localhost` (403, as every request);
- an `Origin` of `http://127.0.0.1:<port>` or `http://localhost:<port>`, this server's
  own port, or with no `Origin`, a `Referer` on one of them (403 `cross_origin`);
- the header `X-Capcom-Token` equal to this launch's token, a random
  `secrets.token_urlsafe` value made when the server starts and found only in the
  pages' `<meta name="capcom-token">` (403 `bad_token`). An open page refreshes its
  views in place and never loads index.html again, so after a restart its token is the
  old launch's: on a `bad_token` it reads `/` once more (a same-origin read, as the
  first was) and retries the write once with the token found there;
- `Content-Type: application/json` (415) and a `Content-Length` (411) of at most
  4,096 bytes (413);
- a JSON object in one of these shapes, no other fields (400 otherwise):

```
{action: "mark", kind, scope, applied_on, what?, note?, days?, source?}
{action: "edit", id, applied_on?, note?}         (at least one of the two)
{action: "unmark", id}
```

A mark's `kind` and `scope` must name a finding of Checks over `days` (7, 14 or 30;
14 by default) without an open mark (409 `already_marked`: one open mark per
finding, so re-marking is changing the date, or unmarking first; also when `capcom
mark` wrote one meanwhile and `capcom.changes` refuses the second); its title and
saving are taken from that finding (from the cached Checks reply of that window when
there is one), not from the request. The finding is looked up before the write lock
is taken; under it, only the marks are read again and written. `applied_on` cannot be after today nor
before the first day the index holds; `id` must be an open mark (404). The reply is
`{mark: Mark}`, or `{unmarked: id}`. `capcom serve --read-only` answers 405 and the
pages leave out every button that writes.

### Ask Roger

#### `POST /api/ask`

The chat in the pages' corner (`static/ask.js`, `ask.py`), behind capcom's mascot,
Roger. The user turns it on once
in the panel; the choice stays in that browser. Each question starts the user's own
`claude` CLI headless:

```
claude -p --model claude-haiku-5-5 --tools "" --strict-mcp-config
  --mcp-config <capcom's own server> --allowedTools mcp__capcom__spend,...
  --permission-mode dontAsk --setting-sources ""
  --disallowedTools Bash,Write,Edit,... --disable-slash-commands --no-session-persistence
  --no-chrome --max-turns 6 --effort low --output-format stream-json --verbose
  --include-partial-messages --system-prompt <capcom's>
```

with `ENABLE_CLAUDEAI_MCP_SERVERS=false`, in an empty temporary folder, the prompt on
stdin. capcom makes no network request itself; the CLI does, signed in as the user.

The one MCP server is capcom's (`asktools.py`, `python -I -m capcom.report.asktools
--index <path> --now <iso> [--tz <zone>]`), which the CLI starts for the question and
which ends with it. Its four read-only tools answer from the index (`askfacts.py`),
in aggregates, labels, ids and file paths only, never a session's title or other
text from the logs, at most 4 calls per question:

| Tool | Arguments | Answers |
| --- | --- | --- |
| `spend` | `group_by` (project, model, agent_type, day, session), `days` (1 to 90, default 14) or `since`/`until` (local dates), filters `project`, `model` (a part of the id, such as "opus"), `agent_type` ("main": the main threads), `session_id` | the total and up to 15 rows (every day for `day`): dollars, share, requests, sessions; a project's folder; a session's id, project, folder, first and last request |
| `session` | `session_id` (or its first 8 characters) | its project, folders, entrypoint, version, first and last request, total, spend by model, agent type and day, subagents spawned by type |
| `findings` | `days` | the Checks findings: kind, scope, title, summary, saving per week, numeric metrics (no evidence) |
| `where_defined` | `name`, `kind` (agent, skill, command) | the files that define it (a project's `.claude/`, `~/.claude/`, a plugin's folder), and for an agent the projects that spawned it |

Guarded like `POST /api/changes`: this launch's token in `X-Capcom-Token`, an Origin
(or Referer) of this server, `application/json`, a Content-Length of at most 20 KB.
Two answers at a time (a third gets 429), each stopped after 90 s, lookups included; when the page
closes the connection the child is killed by its PID (and its process group).
`--no-ask` turns it off (405); `--read-only` does not, as it writes nothing.

Body, every other field ignored:

```
{page: "Checks",                         # the page's name
 part: {label, text, ref} | null,        # the part a mark was clicked on (at most 160
                                         #   and 3000 characters): its numbers and labels
                                         #   in capcom's words, never text from the logs
                                         # ref: what it is about, each field checked, else
                                         #   dropped: {kind: "finding", finding, scope, days},
                                         #   {kind: "mark", id}, {kind: "session", id},
                                         #   {kind: "day", date}, {kind: "view", days}
 selection: "..." | null,                # the text the user selected (at most 1000
                                         #   characters), each private piece it crossed
                                         #   replaced by "[left out]"
 where: "Checks › By kind › ...",        # where it sits: the page, then the headings
                                         #   of its card and section (at most 200)
 left_out: ["session names", ...],       # what kinds of private pieces it lost, only
                                         #   from ask.LEFT_OUT, else 400
 history: [{role: "user"|"assistant", text}],   # the chat so far, the last 8 turns
 question: "..."}                        # at most 600 characters; default: explain the
                                         #   part, or the selection when there is one
```

A selection's `part` is the card or section around it, built as a mark's is. The
page leaves out of a selection, and only names: session titles (the links to
sessions, a session page's heading, a spike's session), a subagent's spawn
description, evidence rows, tables, the user's notes and anything typed in a field,
and pasted config (`PRIVATE` in `static/ask.js`).

With a `ref`, the server adds to the prompt what stands behind the part, from the
index, in at most about 600 tokens (`askfacts.Facts.context`): the total over the
part's range and its split by project (with folders), model, agent type and day, the
top 3 session ids, and for an agent type the files that define it and the projects
that spawned it; for a marked change, from 4 weeks before its date.

Reply: `200 application/x-ndjson`, one JSON object per line as the answer is written,
until the connection closes:

```
{type: "tool", tool, label, done}        # a lookup starts: "Looking up the spend by
                                         #   project…", and "Looked up ..." once it is done
{type: "text", text}                     # the next piece of the answer
{type: "done", suggestions: [str]}       # last: 2 or 3 suggested replies
{type: "error", error: "timeout"|"claude_failed", message}   # last, instead of done
```

