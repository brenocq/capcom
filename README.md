<div align="center">
  <img alt="capcom: a radio-signal mark and the word capcom beside a two-week spend chart drawing itself, with numbered spike markers. Mission control for your Claude Code spend." src="https://github.com/brenocq/capcom/raw/assets/banner.svg" width="880">

  <a href="#install">Install</a> · <a href="#what-you-get">What you get</a> · <a href="#commands">Commands</a> · <a href="#faq">FAQ</a>

  <a href="https://github.com/brenocq/capcom/actions/workflows/tests.yml"><img src="https://github.com/brenocq/capcom/actions/workflows/tests.yml/badge.svg" alt="🧪 Tests"/></a>
</div>

<br>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://github.com/brenocq/capcom/raw/assets/demo-dark.webp">
    <img alt="capcom's website in a browser window, in six steps. Spend jumped: two weeks of spend by project, the total climbing, then rising steeply on one day. Who spent it: split by agent type, the coder subagents made the jump. Why: the jump's spike marker opens its cause, a cache that expired 12 times while Bash ran. Where: the session's page, a 9-minute pause and the full cache rewrite after it. Is it a habit: Checks, by kind, puts the same cause first with what it would save a week, and opens it. Fix it: a settings change that turns off servers never called is copied." src="https://github.com/brenocq/capcom/raw/assets/demo-light.webp" width="992">
  </picture>
</p>

capcom reads the logs Claude Code keeps on your machine (and OpenClaw's, if you
use it) and shows where your tokens and dollars go: what changed and when, every
session and subagent on one timeline, why the spend jumped, which habits of your
setup cost the most with the change to make, and where your usage limits will be
at their reset.

## Install

```
uv tool install git+https://github.com/brenocq/capcom
```

Or `pipx install git+https://github.com/brenocq/capcom`; from a clone,
`uv tool install --editable .`. You need Python 3.12 or newer (3.14 also reads
OpenClaw's compressed rows), and Claude Code's logs, which it writes under
`~/.claude/projects` as you use it. Then:

```
capcom scan
capcom serve --open
capcom checks
```

`capcom scan` reads the logs into an index in `~/.cache/capcom` (about half a
minute the first time, then only what is new), `capcom serve --open` opens the
website on `http://127.0.0.1:8787`, and `capcom checks` lists the setup habits
that cost you the most, with the change to make.

> [!TIP]
> For your 5-hour and weekly limits, run `capcom limits --install` once. It shows
> the change to Claude Code's status line and makes it when you say yes; your
> status line looks the same (with none, you get a short one). `capcom limits`
> and the Limits page then forecast each window at its reset.

## What you get

<p align="center">
  <img alt="Adding up: two weeks of spend drawing in, one line per project with a band below it and the total on top, with numbered spike markers popping onto the total line." src="https://github.com/brenocq/capcom/raw/assets/feature-adding-up.svg" width="49%">
  <img alt="Why: a spike's bars, the usual pace in gray and the extra rising in terracotta, then its cause typing in with the dollars it added." src="https://github.com/brenocq/capcom/raw/assets/feature-why.svg" width="49%">
</p>
<p align="center">
  <img alt="Fan-out: a session's main thread runs along a timeline while fourteen subagent lanes burst in below it, with what the burst cost." src="https://github.com/brenocq/capcom/raw/assets/feature-fan-out.svg" width="49%">
  <img alt="Checks: a weekly saving counting up, then one row per kind of habit with what it would save a week, each bar split by the projects it spans; overlapping kinds are counted once and a judgement call is kept apart." src="https://github.com/brenocq/capcom/raw/assets/feature-checks.svg" width="49%">
</p>
<p align="center">
  <img alt="Limits: the weekly usage rising and dropping to zero at its reset, then a forecast fan from now to the next reset that reaches past 100 percent; beside it, rings for the 5-hour and weekly windows with where each stands now and where it lands at its reset." src="https://github.com/brenocq/capcom/raw/assets/feature-limits.svg" width="49%">
  <img alt="OpenClaw: Claude Code's logs and OpenClaw's feed three lanes of one timeline: your own sessions, OpenClaw's sessions through Claude Code, and OpenClaw's calls straight to a model provider." src="https://github.com/brenocq/capcom/raw/assets/feature-openclaw.svg" width="49%">
</p>
<p align="center">
  <img alt="Trust: capcom's total next to Claude Code's own, and each session's difference landing inside a band of 2 percent." src="https://github.com/brenocq/capcom/raw/assets/feature-trust.svg" width="49%">
  <img alt="Private: a padlock snapping shut beside three checks: reads the logs read-only, makes no network requests, nothing leaves your machine." src="https://github.com/brenocq/capcom/raw/assets/feature-private.svg" width="49%">
</p>

## Commands

| Command | What it does |
| --- | --- |
| `capcom serve` | The website on 127.0.0.1 (`--open` opens it); rescans the logs in the background. Its Ask Claude chat explains a card in a few plain words (`--no-ask` turns it off). |
| `capcom scan` | Reads new log lines into the index. |
| `capcom top` | What changed, then totals by project, session, agent type, model or day (`--by`). |
| `capcom spikes` | What changed, then each spend spike with its cause. |
| `capcom checks` | Setup habits by kind, ranked by what they would save a week, with the change to make. |
| `capcom limits` | The 5-hour and weekly windows: now, at reset, and what spent them (`--install` starts recording). |
| `capcom calibrate` | capcom's totals against Claude Code's own, session by session. |

`top`, `spikes` and `checks` read the last 14 days (`--days`; 0 for all).

## FAQ

<details>
<summary><b>Is this my bill?</b></summary>
<br>

No. capcom prices every request at Anthropic's list API prices: what the same
work would cost through the API. On a Pro or Max plan you pay a flat fee, but
API-equivalent dollars are still the plainest way to compare sessions, habits
and spikes. The prices ship with capcom, with the date they were checked.

</details>

<details>
<summary><b>Does it send anything anywhere?</b></summary>
<br>

No. capcom makes no network requests and uses only Python's standard library.
The one exception is yours to turn on: the website's Ask Claude chat runs your
own `claude` CLI (Haiku, with every tool and MCP server off and no session
saved) when you ask a question, and sends only the numbers and labels of the
card you asked about, never text from your logs. It opens Claude Code's logs
read-only, never reads credentials, writes only its
own files (the index in `~/.cache/capcom`, and the usage-limit samples in
`~/.local/state/capcom`), and serves the website on 127.0.0.1 only. The one
change it makes to Claude Code's own files is the status line entry
`capcom limits --install` shows you, and only once you say yes (it saves a copy
of the file first, and `--uninstall` takes the entry out).

</details>

<details>
<summary><b>How does it know why?</b></summary>
<br>

From what Claude Code already logs for every request: tokens by kind, the cache
lifetime, timestamps, tool calls and the size of their results, compactions,
model switches and subagent spawns. A spike is a request or a stretch far above
that thread's own recent pace. Rules then claim the dollars they can explain (a
cache that expired while someone waited, a compaction, a model switch, a large
tool result, a burst of subagents, a growing context, and a few more), and
whatever no rule explains is shown as unexplained rather than guessed. An expired
cache is named by who was waiting, since the fix differs: you, away from the
session; an agent, on its own long tool call; or a finished subagent, on its
lead.

</details>

<details>
<summary><b>How are the savings counted?</b></summary>
<br>

Each saving is what the change would have saved at this window's pace, net of
the cost of starting fresh where the advice means a new session or agent, and
an upper bound ("up to"). The total counts each dollar once: where two findings
may claim the same dollars, only the larger is added. Changes that trade some
quality for money, such as a cheaper model for an agent, are judgement calls:
listed apart, and not in the total.

</details>

<details>
<summary><b>How accurate is it?</b></summary>
<br>

Claude Code writes a streaming reply several times; capcom counts each reply
once, and prices cache writes by their lifetime. Claude Code also keeps its own
running cost for each session, and `capcom calibrate` compares the two: on the
author's logs, 1,429 of 1,432 comparable sessions came within 2 %. A session
is not comparable when it made calls the logs don't show, such as compactions
or web fetches; for those, `capcom calibrate` estimates from token counts how
much of the gap is output above capcom's estimate and how much is calls the log
doesn't record. Spend in a Claude Code process that never wrote its running
cost is counted by capcom and left out of the comparison.

</details>

<details>
<summary><b>How is it different from ccusage or Claude Code's /usage?</b></summary>
<br>

`/usage` shows your plan's limits as they stand, and `/doctor` checks your
installation and settings. [ccusage](https://github.com/ryoppippi/ccusage)
reads the same logs and totals tokens and cost by day, month and session. capcom
adds the history on one timeline with every subagent in it, the cause of each
spike with its dollars, and checks of your setup with what each change would
save a week.

</details>

<details>
<summary><b>Does it read OpenClaw?</b></summary>
<br>

Yes, read-only. OpenClaw's work that runs through Claude Code lands in Claude
Code's logs, so capcom counts it there, grouped as `openclaw` projects. The calls
OpenClaw makes straight to a model provider are in its own agent databases under
`~/.openclaw` (or `$OPENCLAW_STATE_DIR`); capcom reads those as the project
`openclaw (direct)`. Some of their rows are compressed and need Python 3.14:
with an older Python they are skipped, and read on the first scan under 3.14.

</details>

<details>
<summary><b>How does it know my usage limits?</b></summary>
<br>

Claude Code's logs don't record them; its status line receives them on every
refresh (on Pro and Max plans). `capcom limits --install` puts capcom's recorder
in front of your status line command: the recorder passes its input on to your
command unchanged and keeps a sample each time a window rises. It works with any
status line, or none. From those samples and your own requests, capcom fits what
a percent of each window costs and forecasts where each will be at its reset,
with an 80 % range. Usage from other machines or claude.ai counts toward the
limits but is not in the logs, so it shows up as fit error.

</details>

## License

[MIT](LICENSE).
