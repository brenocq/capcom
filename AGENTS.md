# capcom: working notes for agents

capcom is a local, read-only analyzer of Claude Code's session logs: where the
tokens and dollars go, why the spend jumped, and which setup habits cost the
most. Python, run from the command line, writing an HTML report.

Read first: `docs/README.md`, then the note for your part. `docs/` is kept in
the main checkout only and never committed (from a worktree, read
`~/Github/capcom/docs/`). Then `capcom/types.py`, the contracts between
modules. Never add or commit anything under `docs/`.

## Layout and ownership

- `capcom/<module>/` and `tests/<module>/` belong to one module each:
  `sources`, `store`, `cost`, `timeline`, `explain`, `checks`, `report`. Work
  only inside your own module's directories. A change anywhere else
  (`types.py`, `cli.py`, `pyproject.toml`) must be small, additive and called
  out in your final report.
- `capcom/types.py` is the shared contract. Don't change an existing field or
  signature; adding an optional field or a new type is fine.

## Rules that break silently

- **Read-only on the logs.** Never write, move, lock or truncate anything under
  `~/.claude/` or `~/.openclaw/`. Open files read-only; a live session may be
  appending to the one you read, so a last line may be partial (skip it, read
  it next time). The one exception is `capcom limits --install`/`--uninstall`
  (`capcom/limits/install.py`): it rewrites the `statusLine` entry of
  `~/.claude/settings.json`, and only after the user answers yes.
- **The logs are private.** They hold the user's conversations, file contents
  and sometimes secrets. Never commit them, anything derived from their text,
  or a report built from them. Test fixtures are synthetic, written by hand or
  by a generator in `tests/`. In chat and reports, quote no conversation text
  beyond a tool name, a file path or a short label.
- **No network.** capcom makes no network requests at run time. Never read
  `~/.claude/.credentials.json`, `~/.openclaw/credentials/`, auth profiles or
  any token.
- **One reply, one count.** Claude Code writes an assistant message several
  times while it streams: count each `message.id` once, from its last record
  (`docs/data-sources.md`).
- **Formats drift with Claude Code versions.** Every field read from a log is
  optional; an unknown record type is skipped, never an error.

## Commands

```sh
uv sync                       # once per worktree
uv run pytest                 # tests (synthetic fixtures only)
uv run ruff check . && uv run ruff format --check .
uv run mypy capcom
uv run capcom report          # on the real logs: under the memory cap below
```

The runtime uses the standard library only (`json`, `sqlite3`, `pathlib`);
dev tools (pytest, ruff, mypy) are dev dependencies. A runtime dependency needs
the main session's approval.

## Heavy jobs

The machine has 31 GB of RAM and agents run side by side; it has frozen from
memory exhaustion before. The logs are about 6 GB.

- Run capcom on the real logs only under a cap:
  `systemd-run --user --scope -p MemoryMax=1G -p MemorySwapMax=0 uv run capcom ...`.
  Stream the files; never load a whole log into memory.
- At most 2 heavy processes per agent (a full scan, a headless browser, a
  nested Claude Code session), and check `free -g` first.
- Nested Claude Code sessions (only if a task needs one) follow
  `docs/orchestration.md`, "Nested sessions": every tool and connector off.

## Commits

Conventional commits with the module as scope (`feat(sources): ...`,
`fix(cost): ...`, `test(timeline): ...`), and a body of one to three short
paragraphs saying what changed and why. No bullet-list changelogs and no
`Co-Authored-By` or other agent signatures. Commit only on your own branch, in
your own worktree, and never push.

## Writing

American English everywhere (code, comments, docs, commits): optimize, color,
behavior, labeled.
