<div align="center">
  <img alt="capcom: a radio-signal mark and the word capcom beside a two-week spend chart drawing itself, with numbered spike markers. Mission control for your Claude Code spend." src="https://github.com/brenocq/capcom/raw/assets/banner.svg" width="880">

  <a href="#install">Install</a> · <a href="#what-you-get">What you get</a> · <a href="#commands">Commands</a> · <a href="#faq">FAQ</a>

  <a href="https://github.com/brenocq/capcom/actions/workflows/tests.yml"><img src="https://github.com/brenocq/capcom/actions/workflows/tests.yml/badge.svg" alt="🧪 Tests"/></a>
</div>

<br>

<p align="center">
  <img alt="capcom's website in a browser window: two weeks of spend grouped by project, with a tooltip on the chart, then by model; the Spikes page, opening a burst of 14 subagents to its cause and cost; and the Checks page, copying the settings change for two unused MCP servers" src="https://github.com/brenocq/capcom/raw/assets/demo.webp" width="992">
</p>

capcom reads the logs Claude Code keeps on your machine and shows where your
tokens and dollars go: every session and subagent on one timeline, why the
spend jumped when it did, and which habits of your setup cost the most, with
the change to make.

## Install

You need Python 3.12 or newer, and Claude Code's logs (it writes them under
`~/.claude/projects` as you use it).

```
uv tool install git+https://github.com/brenocq/capcom
capcom serve --open
```

`capcom serve` reads the logs into an index in `~/.cache/capcom` (about half a
minute the first time, then only what is new) and opens the website on
`http://127.0.0.1:8787`.

## What you get

<p align="center">
  <img alt="Adding up: two weeks of spend drawing in, one line per project with a band below it and the total on top, with numbered spike markers popping onto the total line." src="https://github.com/brenocq/capcom/raw/assets/feature-adding-up.svg" width="49%">
  <img alt="Why: a spike's bars, the usual pace in gray and the extra rising in terracotta, then its cause typing in with the dollars it added." src="https://github.com/brenocq/capcom/raw/assets/feature-why.svg" width="49%">
</p>
<p align="center">
  <img alt="Fan-out: a session's main thread runs along a timeline while fourteen subagent lanes burst in below it, with what the burst cost." src="https://github.com/brenocq/capcom/raw/assets/feature-fan-out.svg" width="49%">
  <img alt="Checks: a weekly saving counting up, then the settings change for unused MCP servers appearing line by line and being copied." src="https://github.com/brenocq/capcom/raw/assets/feature-checks.svg" width="49%">
</p>
<p align="center">
  <img alt="Trust: capcom's total next to Claude Code's own, and each session's difference landing inside a band of 2 percent." src="https://github.com/brenocq/capcom/raw/assets/feature-trust.svg" width="49%">
  <img alt="Private: a padlock snapping shut beside three checks: reads the logs read-only, makes no network requests, nothing leaves your machine." src="https://github.com/brenocq/capcom/raw/assets/feature-private.svg" width="49%">
</p>

## Commands

| Command | What it does |
| --- | --- |
| `capcom serve` | The website on 127.0.0.1 (`--open` opens it); rescans the logs in the background. |
| `capcom scan` | Reads new log lines into the index. |
| `capcom top` | Totals by project, session, agent type, model or day (`--by`). |
| `capcom spikes` | Spend spikes and their causes. |
| `capcom checks` | Setup habits, ranked by what they cost a week, with the change to make. |
| `capcom calibrate` | capcom's totals against Claude Code's own, session by session. |

The commands that read a window take `--days` (14 by default; 0 for all).

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
It opens Claude Code's logs read-only, never reads credentials, writes only its
own index in `~/.cache/capcom`, and serves the website on 127.0.0.1 only.

</details>

<details>
<summary><b>How does it know why?</b></summary>
<br>

From what Claude Code already logs for every request: tokens by kind, the cache
lifetime, timestamps, tool calls and the size of their results, compactions,
model switches and subagent spawns. A spike is a request or a stretch far above
that thread's own recent pace. Rules then claim the dollars they can explain
(a cache that expired while you were away, a compaction, a model switch, a
large tool result, a burst of subagents, a growing context, and a few more),
and whatever no rule explains is shown as unexplained rather than guessed.

</details>

<details>
<summary><b>How accurate is it?</b></summary>
<br>

Claude Code writes a streaming reply several times; capcom counts each reply
once, and prices cache writes by their lifetime. Claude Code also keeps its own
running cost for each session, and `capcom calibrate` compares the two: on the
author's logs, 1,426 of 1,429 comparable sessions came within 2 %. A session
is not comparable when it made calls the logs don't show, such as compactions
or web fetches.

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
<summary><b>OpenClaw?</b></summary>
<br>

OpenClaw's work that runs through Claude Code lands in Claude Code's logs, so
capcom counts it, grouped as `openclaw` projects (it finds OpenClaw in
`~/.openclaw`, or `$OPENCLAW_STATE_DIR`). Calls OpenClaw makes straight to a
provider are not in those logs and are not counted yet.

</details>

<details>
<summary><b>What about the 5-hour and weekly limits?</b></summary>
<br>

Coming. Claude Code's logs don't record them; its status line does, so capcom
will record them from there and forecast where each will be at its reset.

</details>

## License

[MIT](LICENSE).
