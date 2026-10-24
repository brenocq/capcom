<div align="center">
  <img alt="capcom: Roger, a terracotta capsule in mission-control headphones, and the word capcom, beside a two-week spend chart drawing itself, with numbered spike markers; Roger waves and says: Roger that! Mission control for your Claude Code spend." src="https://github.com/brenocq/capcom/raw/assets/banner.svg" width="880">

  <a href="#demo">Demo</a> · <a href="#install">Install</a> · <a href="#what-you-get">What you get</a> · <a href="#commands">Commands</a> · <a href="#faq">FAQ</a>

  <a href="https://github.com/brenocq/capcom/actions/workflows/tests.yml"><img src="https://github.com/brenocq/capcom/actions/workflows/tests.yml/badge.svg" alt="🧪 Tests"/></a>
</div>

<br>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://github.com/brenocq/capcom/raw/assets/demo-dark.webp">
    <img alt="capcom's website in a browser window, in six steps. Spend jumped: two weeks of spend by project, the total rising steeply on one day. Why: split by agent type, the coder subagents made the jump, and its spike marker opens the cause, a cache that expired 12 times while Bash ran. Where: the session's timeline, its coder subagents spread into lanes, and one of them opens on its own page. Which step: that agent's steps, three long test runs, the second opened to its command, its failing test and the request that followed. Is it a habit: Checks, by kind, puts the same cause first with what it would save a week, and its change, a one-hour cache for subagents, is copied. Did it work: a change marked applied, with its before, after and verdict." src="https://github.com/brenocq/capcom/raw/assets/demo-light.webp" width="992">
  </picture>
</p>

capcom reads the logs Claude Code keeps on your machine and shows where your
tokens and dollars go: why the spend jumped, every session, subagent and step on
one timeline, which habits of your setup cost the most with the change to make
(and later, whether it worked), and where your usage limits will be at their
reset.

> [!NOTE]
> capcom is new and may still get things wrong. Its costs, causes, savings and
> forecasts are estimates from the logs, not your bill: check anything you act on
> against Claude Code's own `/usage`, and [open an
> issue](https://github.com/brenocq/capcom/issues) when something looks off.

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
  <img alt="Adding up: two weeks of spend drawing in, one line per project and the total, with numbered spike markers popping onto the total line; then the steepest day opens into its 24 hours." src="https://github.com/brenocq/capcom/raw/assets/feature-adding-up.svg" width="49%">
  <img alt="Why: a spike's bars, the usual pace in gray and the extra rising in terracotta, then its cause typing in with the dollars it added." src="https://github.com/brenocq/capcom/raw/assets/feature-why.svg" width="49%">
</p>
<p align="center">
  <img alt="Timeline: a four-hour session drawn on linear time, mostly empty, then its two long pauses squeeze into hatched slivers and a spend-rate lane draws beneath, jumping after each pause." src="https://github.com/brenocq/capcom/raw/assets/feature-timeline.svg" width="49%">
  <img alt="Every step: a coder agent's steps filling a strip left to right, three long test runs in orange; the second opens to its command, its failing test and exit code, beside what the next request cost." src="https://github.com/brenocq/capcom/raw/assets/feature-every-step.svg" width="49%">
</p>
<p align="center">
  <img alt="Checks: a weekly saving counting up, then one row per kind of habit with what it would save a week, each bar split by the projects it spans; one row is marked applied, and a line below says it worked and what it saved a week." src="https://github.com/brenocq/capcom/raw/assets/feature-checks.svg" width="49%">
  <img alt="Limits: the weekly usage rising and dropping to zero at its reset, then a forecast fan from now to the next reset that reaches past 100 percent; beside it, rings for the 5-hour and weekly windows with where each stands now and where it lands at its reset." src="https://github.com/brenocq/capcom/raw/assets/feature-limits.svg" width="49%">
</p>
<p align="center">
  <img alt="Ask Roger: the mascot waves as a question about the largest spike gets a typed answer in two plain sentences, with two follow-up questions to pick from." src="https://github.com/brenocq/capcom/raw/assets/feature-ask-roger.svg" width="49%">
  <img alt="Private: a padlock snapping shut beside three checks: reads your logs read-only, no network (Ask Roger only, when you turn it on), tool details read live and never stored." src="https://github.com/brenocq/capcom/raw/assets/feature-private.svg" width="49%">
</p>

## Commands

| Command | What it does |
| --- | --- |
| `capcom serve` | The website on 127.0.0.1 (`--open` opens it), kept current as you work. `--no-ask` turns Ask Roger off. |
| `capcom scan` | Reads new log lines into the index. |
| `capcom top` | What changed, then totals by project, session, agent type, model or day (`--by`). |
| `capcom spikes` | What changed, then each spend spike with its cause. |
| `capcom checks` | Setup habits by kind, ranked by what they would save a week, with the change to make. |
| `capcom limits` | The 5-hour and weekly windows: now, at reset, and what spent them (`--install` starts recording). |
| `capcom mark` · `capcom changes` | Record that you made a check's change; later, whether it worked (`unmark` takes a mark back). |
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
The one exception is yours to turn on: the website's Ask Roger chat runs your
own `claude` CLI (Haiku, with every tool and MCP server off and no session
saved) when you ask a question. It sends the page's name, the numbers and labels
of what you asked about, the text you selected (session titles and other private
pieces left out), and capcom's own totals it may look up (spend, sessions,
findings and where an agent or skill is defined); never file contents or tool
output. It opens Claude Code's logs
read-only, never reads credentials, writes only its
own files (the index in `~/.cache/capcom`, and the usage-limit samples in
`~/.local/state/capcom`), and serves the website on 127.0.0.1 only. The one
change it makes to Claude Code's own files is the status line entry
`capcom limits --install` shows you, and only once you say yes (it saves a copy
of the file first, and `--uninstall` takes the entry out).

</details>

<details>
<summary><b>Can I see what an agent actually ran?</b></summary>
<br>

Yes: open a step on an agent's page to see its call (the command, or an edit's
old and new text), its output, and the reply text or thinking summary around it.
capcom reads that one call from the thread's own log file, read-only, only while
you look at it in `capcom serve`. The text is never stored or cached, never in an
exported report, and never sent to Ask Roger.

</details>

<details>
<summary><b>Did my change work?</b></summary>
<br>

After you apply a check's change, `capcom mark` (or the button on Checks)
records the date. The Did it work? page then compares the weeks before and after
on that habit's own measure, and says what the change saved a week.

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
author's logs, 1,470 of 1,474 comparable sessions came within 2 %. A session
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
adds the history on one timeline with every subagent in it, down to each
agent's steps, the cause of each
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

<details>
<summary><b>Who is Roger?</b></summary>
<br>

capcom's mascot: the terracotta capsule in mission-control headphones in the
banner (in mission control, the CAPCOM is the one voice that talks to the crew).
On the website he waves from the round button in the corner, which opens the
Ask Roger chat: ask about the page, or press Explain on a dense card, and he
answers in a few plain words.

</details>

## Demo

<!-- Placeholder: replace this URL once the MP4 is uploaded. -->
<p align="center">
  <a href="https://github.com/brenocq/capcom/releases/download/tour/capcom-tour.mp4"><img alt="The launch video's opening: capcom's Spend chart over two weeks, with a play button." src="https://github.com/brenocq/capcom/raw/assets/video-poster.png" width="720"></a>
  <br>
  <a href="https://github.com/brenocq/capcom/releases/download/tour/capcom-tour.mp4">Watch the 80-second tour, with sound</a>
</p>

## License

[MIT](LICENSE).
