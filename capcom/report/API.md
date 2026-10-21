# capcom serve: the JSON API

`capcom serve` listens on `127.0.0.1` only (default port 8787). Every endpoint is a
`GET` (`HEAD` gives the same headers without the body); other methods get 405. There are no CORS headers, and a request whose `Host`
header is not `127.0.0.1` or `localhost` gets 403 (DNS rebinding). Each request opens
the index read-only and closes it before answering.

Replies are cached per scan generation: the key holds the size and modification time
of `index.sqlite` and its `-wal`, the background scanner's count of finished scans,
the local date, the endpoint and its query, so the first call after a scan computes
again and a repeat call is answered from memory. Entries live at most 5 minutes (some
numbers, such as "the last hour", move with the clock alone) and the cache holds at
most 32 MB of encoded replies, least recently used out first. `status` and `live` are
never cached; errors are never cached.

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
| 404 | `not_found` | Unknown endpoint or session. |
| 501 | `unavailable` | The analysis module behind it is not built yet; adds `module` and `milestone`. |
| 503 | `no_index` | No index, or one written by another capcom version: run `capcom scan`. Adds `scanning`: true when serve's scan is running (the message then says the logs are being read). |
| 503 | `index_busy` | SQLite could not read the index. |

Anything else is a plain-text `500 internal error` that says nothing more; the
traceback goes to the terminal only. A static name that cannot be a file (a NUL byte,
a name too long) or that resolves outside `static/` is a 404.
Page paths (`/`, `/spend`, `/sessions`, `/session/<id>`, `/spikes`, `/checks`, `/limits`) serve
`static/index.html`, or a 503 page saying to run `capcom scan` when the index is
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
folder (`~` for home) for a tooltip.

**Session row**: `session_id, title, project, project_path, project_dir, temporary, git_branch,
subagents, active_seconds, first, last, peak_context, cache_read_share, usd, requests,
estimated_output`. `temporary` is true for a folder under /tmp. Active time sums the gaps under 5 minutes between consecutive
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
it>)`, e.g. `openclaw (workspace)`, each its own row. Every folder under /tmp is
temporary: Claude Code's scratchpads (`/tmp/claude-<uid>/<project folder>/<session
id>/...`) are grouped as `temporary (<project label>)`, other temporary folders as
`(temporary)`. The chart series (`capcom.timeline`) group all temporary folders as one
member, labeled `temporary folders`, and all OpenClaw folders as one, labeled
`openclaw (all folders)`.

## Endpoints

### `GET /api/status`

`{now, sessions, prices_checked, scanning, index: {updated, size_bytes}}`. `updated` is
the later modification time of `index.sqlite` and `index.sqlite-wal`. `scanning` is
true while serve's first scan has not finished: totals may be partial until then.

### `GET /api/overview`

```
{now, running_sessions,
 today: Block, week: Block + {days: 7, full_days: 6, elapsed_days},
 previous_7_days: {since, daily_average_usd,     # the 7 days before today
                   same_hour_average_usd, same_hour},
 spending_now: {usd_last_hour, spark: {step_s, times, values} | null},
 cumulative_today: Cumulative (by category, 5-minute steps) | null,
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

`sessions_count` leaves out the sessions in temporary folders (/tmp), counted apart in
`temporary_sessions`; together they are `/api/sessions`' `total` for the same `days`
(whose `temporary` counts the same temporary sessions).

### `GET /api/day/<YYYY-MM-DD>`

One local day, the page a bar of Spend's per-day chart opens: what it cost against a
typical day, and where it went. 400 for a malformed date, 404 for a day not yet come.

```
{day, today, window: {since, until},  # until: the next midnight, or now for today
 totals, sessions_count, temporary_sessions,
 typical: {days, of_days, usd, categories: {cache_read, cache_write, output, input}} | null,
 cumulative: Cumulative | null,       # by category, 10-minute steps
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
           entrypoint, first_seen, last_seen, running, models, efforts},
 totals, active_seconds,
 calibration: {claude_code_usd, capcom_usd, spread, comparable, reasons, reason_labels,
               part: {since, until, first, last, process, processes, whole_session, text},
               session_usd, outside_usd, output_above_usd, output_range, unlogged_usd,
               unlogged_text, logged_more_usd, logged_more_text, rest_usd, note} | null,
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
usd_last_10_min, usd_per_hour, last_seen}]}`. The rate is the last 10 minutes' spend
times six.

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

### `GET /api/limits?days=14`

The subscription's 5-hour and weekly windows from `capcom.limits`: the samples
`capcom record-limits` appends to `limits.jsonl` in the state folder
(`$XDG_STATE_HOME/capcom`, else `~/.local/state/capcom`: no scan rebuilds them, so they
stay out of the cache), and the seed from the index's rate-limit errors. `days` is one
of 7, 14, 30, 90 and bounds `segments`.
The reply's cache key also holds the size and modification time of `limits.jsonl`,
since the recorder writes it without a scan.

```
Limits = {available, recording, message, install, samples, last_sample, recording_since, days,
          windows: {five_hour: Window | null, seven_day: Window | null}}
Window = {window, label, samples, current,
          reading: {percent, time, resets_at, source} | null,
          fit: {method, note, percent_per_usd, usd_per_percent, intervals, readings, rough,
                rate_factor, mean_abs_error, r2,
                weights: [{model, category, percent_per_mtok}]} | null,
          forecast: {reading, now, median, low, high, resets_at, runs_out_at,
                     runs_out_early, runs_out_late, rough, readings, rate_factor, line,
                     bucket_minutes, buckets, fan: [[ms, median, low, high]]} | null,
          reason,                       # why there is no forecast (it needs two readings, ...)
          history: [[ms, percent]],     # the current window's line, from its start at 0 %
          start, start_known,           # start_known: history's first point is the reset, not a reading
          segments: [{start, start_known, resets_at, peak, hit_limit, reset,
                      points: [[ms, percent]], usd,
                      by_project: [{project, usd, percent}] | null}]}   # newest first
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
`by_project` (the last 8 segments; up to 6 projects plus `(other)`) is what the
index's requests spent in the segment, and their fitted percent (null without a fit).
Usage from other machines or claude.ai counts toward the limits but is not in the
logs.

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
