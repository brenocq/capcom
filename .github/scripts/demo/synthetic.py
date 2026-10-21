"""A made-up two weeks of Claude Code logs, for the README's pictures and demo.

    python .github/scripts/demo/synthetic.py <claude-root> [--now ISO]

Writes <claude-root>/projects/... in the shape Claude Code 2.1.30x uses, through
tests/fixtures/generate.py's SessionWriter: five projects with neutral names
under /home/dev/code, main threads on Opus with subagents of a few types, and the
habits capcom is built to find: idle gaps past the cache lifetime, a late
compaction, a subagent fan-out, a workflow's burst of agents, a large tool
result, a model switch mid-session, tools and skills loaded but never used, a
long CLAUDE.md, and the story the demo tells: `coder` subagents whose cache
expires while their ten-minute test runs go on, twice in small and then once
in large (four coders, twelve rewrites), the window's steepest rise. Every
session ends with a cost-state that matches its requests, a little above them
(Claude Code's side calls), as real ones do.

Nothing here comes from real logs: names, titles, paths and commands are made
up. The data ends a few minutes before `--now` (default: now), so one session is
still live when the demo records. Seeded, so two runs differ only in dates.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tests.fixtures.generate import Reply, SessionWriter, _iso  # noqa: E402

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-4-5-20251001"
PRICES = json.loads((ROOT / "capcom" / "cost" / "prices.json").read_text())["models"]

HOME = "/home/dev"
PROJECTS = {
    "orbit-api": f"{HOME}/code/orbit-api",
    "tidepool-web": f"{HOME}/code/tidepool-web",
    "lantern-docs": f"{HOME}/code/lantern-docs",
    "quarry-etl": f"{HOME}/code/quarry-etl",
    "pebble-cli": f"{HOME}/code/pebble-cli",
}
TITLES = {
    "orbit-api": [
        "Add rate limiting to the orders endpoint",
        "Fix flaky pagination test",
        "Refactor auth middleware",
        "Migrate settings to TOML",
        "Speed up the search index",
        "Add webhook retries",
    ],
    "tidepool-web": [
        "Dark mode for the dashboard",
        "Rework the onboarding form",
        "Chart tooltips on touch screens",
        "Upgrade the bundler",
    ],
    "lantern-docs": [
        "Rewrite the getting started guide",
        "Generate the API reference",
        "Fix broken links",
    ],
    "quarry-etl": [
        "Backfill last quarter's events",
        "Parallelize the loader",
        "Schema drift alerts",
    ],
    "pebble-cli": ["Shell completions", "Release notes for 0.9"],
}
FILES = {
    "orbit-api": ["src/orders.py", "src/auth.py", "src/search.py", "tests/test_orders.py"],
    "tidepool-web": ["src/App.tsx", "src/theme.ts", "src/chart.tsx", "package.json"],
    "lantern-docs": ["guide/start.md", "guide/install.md", "reference/index.md"],
    "quarry-etl": ["etl/load.py", "etl/schema.py", "jobs/backfill.py"],
    "pebble-cli": ["src/main.rs", "src/complete.rs", "CHANGELOG.md"],
}
MCP_TOOLS = [f"mcp__tracker__{n}" for n in ("search", "create", "update", "comment", "link")]
MCP_TOOLS += [f"mcp__browser__{n}" for n in ("open", "click", "type", "screenshot", "scroll")]
BASE_TOOLS = ["Read", "Edit", "Write", "Bash", "Grep", "Glob", "Agent", "WebSearch", "TodoWrite"]
SKILLS = ["release-notes", "db-migrate", "slide-deck", "pdf-export", "brand-voice"]
AGENT_TYPES = {"Explore": HAIKU, "general-purpose": SONNET, "code-reviewer": OPUS, "coder": OPUS}
CAP = 100_000  # ordinary sessions stop growing here, so the planted habits stand out


def cost(model: str, u: dict[str, int]) -> float:
    p = PRICES[model]
    return (
        u["input"] * p["input"]
        + u["output"] * p["output"]
        + u["cache_read"] * p["cache_read"]
        + u["w5"] * p["cache_write_5m"]
        + u["w1"] * p["cache_write_1h"]
    ) / 1e6


class Thread:
    """One agent thread (main or subagent): its writer, its cached context and clock."""

    def __init__(
        self, session: Session, w: SessionWriter, model: str, ttl: str, t: datetime
    ) -> None:
        self.session, self.w, self.model, self.ttl = session, w, model, ttl
        self.t = t
        self.ctx = 0  # tokens in the cache
        self.pending = 0  # tool results since the last call, sent with the next one
        self.last: datetime | None = None

    def wait(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)

    def request(
        self,
        new: int,
        output: int,
        tools: list[tuple[str, dict[str, Any]]] | None = None,
        thinking: int = 0,
    ) -> str:
        """One model call adding `new` tokens to the context; a call after the
        cache lifetime rewrites all of it."""
        new += self.pending
        self.pending = 0
        life = timedelta(minutes=60 if self.ttl == "1h" else 5)
        expired = self.last is not None and self.t - self.last > life
        read = 0 if expired else self.ctx
        write = new + (self.ctx if expired else 0)
        w1, w5 = (write, 0) if self.ttl == "1h" else (0, write)
        u = {"input": 3, "output": output, "cache_read": read, "w5": w5, "w1": w1}
        self.session.add(self.model, u)
        self.w.api_request(self.t, self.model, ttl=self.ttl)
        mid = self.w.assistant(
            Reply(
                self.t,
                model=self.model,
                cache_read=read,
                cache_write_5m=w5,
                cache_write_1h=w1,
                output=output,
                thinking=thinking,
                tools=tools or [],
                records=2 if tools else 1,
                effort="high",
            )
        )
        self.ctx += new + output
        self.last = self.t
        return mid

    def tool(
        self, name: str, args: dict[str, Any], tokens: int, ran: float = 4.0, **kw: Any
    ) -> None:
        """A call to `name` whose result is `tokens` long, back after `ran` seconds."""
        self.request(
            new=self.session.rng.randint(150, 900),
            output=kw.pop("output", 260),
            tools=[(name, args)],
            **kw,
        )
        self.wait(ran)
        self.w.tool_result(self.w.last_tool_ids[0], self.t, chars=tokens * 4)
        self.pending += tokens
        self.wait(self.session.rng.uniform(2, 9))

    def work(self, steps: int, project: str, big: bool = False, slow: float = 1) -> None:
        """Ordinary steps: reads, edits, greps and test runs."""
        rng = self.session.rng
        cwd = PROJECTS[project]
        for _ in range(steps):
            kind = rng.choices(["Read", "Edit", "Grep", "Bash"], [4, 3, 2, 2])[0]
            path = f"{cwd}/{rng.choice(FILES[project])}"
            args: dict[str, Any] = {
                "Read": {"file_path": path},
                "Edit": {"file_path": path, "old_string": "a", "new_string": "b"},
                "Grep": {"pattern": "TODO", "path": cwd},
                "Bash": {"command": rng.choice(["make test", "git status", "make lint"])},
            }[kind]
            size = rng.randint(200, 2500) if kind != "Read" else rng.randint(1500, 10_000)
            size *= 2 if big else 1
            self.tool(kind, args, size, ran=rng.uniform(2, 25) * slow)


class Session:
    def __init__(
        self,
        root: Path,
        rng: random.Random,
        project: str,
        start: datetime,
        n: int,
        model: str = OPUS,
        ttl: str = "1h",
        title: str | None = None,
        mcp: bool = False,
        skills: bool = False,
        memory: int = 9_000,
    ) -> None:
        self.rng = rng
        self.project = project
        self.start = start
        self.usage: dict[str, dict[str, int]] = defaultdict(
            lambda: {"input": 0, "output": 0, "cache_read": 0, "w5": 0, "w1": 0}
        )
        sid = f"5e55{n:04x}-0000-4000-8000-{rng.getrandbits(48):012x}"
        self.w = SessionWriter(root, cwd=PROJECTS[project], session_id=sid)
        self.title = title or rng.choice(TITLES[project])
        tools = BASE_TOOLS + (MCP_TOOLS if mcp else [])
        self.w.write(
            {
                "type": "api-request-shape",
                "sessionId": sid,
                "shapeHash": f"h{len(tools)}",
                "timestamp": _iso(start),
                "version": self.w.version,
                "shape": {
                    "system": [{"type": "text", "text": "synthetic system prompt"}],
                    "tools": [
                        {
                            "name": t,
                            "description": "A made-up tool. "
                            * (120 if t.startswith("mcp__") else 40),
                            "input_schema": {"type": "object"},
                        }
                        for t in tools
                    ],
                },
            }
        )
        if mcp:
            self.w.mcp_instructions_delta(start, {"tracker": 6_000, "browser": 9_000})
        self.w.instructions(start, {f"{PROJECTS[project]}/CLAUDE.md": memory})
        if skills:
            self.w.skill_listing(start, SKILLS)
        self.w.agent_listing_delta(start, list(AGENT_TYPES))
        self.main = Thread(self, self.w, model, ttl, start)
        self.subs: list[SessionWriter] = []
        # The first call writes the system prompt, tools and memory to the cache.
        self.w.user_prompt(start)
        self.main.wait(3)
        self.main.request(new=18_000 + memory // 4 + (6_000 if mcp else 0), output=420)
        self.main.wait(5)

    def add(self, model: str, u: dict[str, int]) -> None:
        for k, v in u.items():
            self.usage[model][k] += v

    def prompt(self, gap: float) -> None:
        """The user comes back after `gap` seconds with a new prompt."""
        self.main.wait(gap)
        self.w.user_prompt(self.main.t)
        self.main.wait(self.rng.uniform(2, 6))

    def agents(
        self,
        kinds: list[tuple[str, str]],
        steps: tuple[int, int] = (4, 12),
        report: int = 1_500,
        workflow: bool = False,
        big: bool = False,
        prefix: int = 22_000,
        slow: float = 1,
    ) -> list[Thread]:
        """Subagents started together: from one reply's Agent calls, or by a workflow
        (no Agent call, logs under subagents/workflows/)."""
        m = self.main
        if not workflow:
            calls = [
                ("Agent", {"subagent_type": k, "description": d, "prompt": "synthetic"})
                for k, d in kinds
            ]
            m.request(new=600, output=300 + 120 * len(kinds), tools=calls)
            ids = list(m.w.last_tool_ids)
        else:
            m.tool("Bash", {"command": "claude-workflow run nightly-checks"}, 400, ran=2)
            ids = [None] * len(kinds)
        threads = []
        for i, ((kind, desc), tid) in enumerate(zip(kinds, ids, strict=True)):
            if workflow:
                aid = f"a{self.rng.getrandbits(64):016x}"
                folder = m.w.project / m.w.session_id / "subagents" / "workflows" / "wf-nightly"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"agent-{aid}.meta.json").write_text(
                    json.dumps(
                        {
                            "agentType": "workflow-subagent",
                            "description": desc,
                            "toolUseId": None,
                            "spawnDepth": 1,
                            "isBuiltIn": True,
                        }
                    )
                )
                sw = SessionWriter(
                    m.w.root,
                    m.w.cwd,
                    m.w.session_id,
                    _agent_id=aid,
                    _path=folder / f"agent-{aid}.jsonl",
                )
                model = SONNET
            else:
                sw = m.w.subagent(kind, desc, tool_use_id=tid)
                model = AGENT_TYPES[kind]
            self.subs.append(sw)
            th = Thread(self, sw, model, "5m", m.t + timedelta(seconds=1 + i * 0.4))
            sw.user_prompt(th.t, "synthetic task")
            th.wait(2)
            th.request(new=prefix, output=200)
            th.wait(3)
            th.work(self.rng.randint(*steps), self.project, big=big, slow=slow)
            th.request(new=300, output=report // 2)
            threads.append(th)
        end = max(th.t for th in threads)
        m.t = end + timedelta(seconds=2)
        if not workflow:
            for tid in ids:
                m.w.tool_result(tid, m.t, chars=report * 4)
                m.pending += report
        return threads

    def coders(self, tables: list[str], waits: int, dump: int) -> None:
        """One reply starts a `coder` subagent per table; each reads a large fixture
        dump, then runs the table's backfill tests `waits` times. A run takes about
        ten minutes, past a subagent's 5-minute cache, so the call after each run
        rewrites the whole context: `waits` rewrites per table."""
        m = self.main
        cwd = PROJECTS[self.project]
        calls = [
            (
                "Agent",
                {"subagent_type": "coder", "description": f"backfill {t}", "prompt": "synthetic"},
            )
            for t in tables
        ]
        m.request(new=600, output=300 + 160 * len(tables), tools=calls)
        ids = list(m.w.last_tool_ids)
        # When the first test runs come back: 15 minutes on, 20 s past an even minute
        # (capcom sums a session's spend in 2-minute steps; each round fills one).
        first = m.t + timedelta(minutes=15)
        first -= timedelta(seconds=first.timestamp() % 120 - 20)
        ends = []
        for i, (table, tid) in enumerate(zip(tables, ids, strict=True)):
            sw = m.w.subagent("coder", f"backfill {table}", tool_use_id=tid)
            self.subs.append(sw)
            th = Thread(self, sw, OPUS, "5m", m.t + timedelta(seconds=2 + i * 0.6))
            sw.user_prompt(th.t, "synthetic task")
            th.wait(2)
            th.request(new=24_000, output=300)
            th.work(6, self.project, big=True)
            th.tool("Read", {"file_path": f"{cwd}/fixtures/{table}_q3.csv"}, dump, ran=1)
            # Each run's failures go straight into the next run's call (and the last
            # into the report), so every call after the first comes back to a cold
            # cache. The test runs share one database, so the coders' runs end
            # within a few seconds of each other, every ten minutes.
            for k in range(waits):
                back = first + timedelta(seconds=k * 600 + i * 12 + self.rng.uniform(0, 4))
                th.tool(
                    "Bash",
                    {"command": f"make backfill-test TABLE={table}"},
                    3_000,
                    ran=(back - th.t).total_seconds(),
                )
            th.request(new=300, output=900)
            ends.append(th.t)
        m.t = max(ends) + timedelta(seconds=3)
        for tid in ids:
            m.w.tool_result(tid, m.t, chars=6_000)
            m.pending += 1_500

    def close(self) -> float:
        """The cost-state Claude Code writes on exit: every model's usage and cost,
        plus a small share for side calls the transcript never shows."""
        for sw in self.subs:
            sw.close()
        self.w.ai_title(self.title)
        models: dict[str, dict[str, Any]] = {}
        total = 0.0
        overhead = self.rng.uniform(0.002, 0.016)
        for model, u in self.usage.items():
            usd = cost(model, u) * (1 + overhead)
            total += usd
            models[model] = {
                "inputTokens": u["input"],
                "outputTokens": u["output"],
                "cacheReadInputTokens": u["cache_read"],
                "cacheCreationInputTokens": u["w5"] + u["w1"],
                "webSearchRequests": 0,
                "costUSD": round(usd, 6),
            }
        self.w.write(
            {
                "type": "cost-state",
                "sessionId": self.w.session_id,
                "totalCostUSD": round(total, 6),
                "totalAPIDuration": 1000,
                "totalToolDuration": 100,
                "totalLinesAdded": 0,
                "totalLinesRemoved": 0,
                "totalDuration": int((self.main.t - self.start).total_seconds() * 1000),
                "startTime": int(self.start.timestamp() * 1000),
                "modelUsage": models,
                "hasUnknownModelCost": False,
            }
        )
        self.w.close()
        return total


def ordinary(s: Session, turns: int, steps: tuple[int, int] = (4, 10), cap: int = 0) -> None:
    """Turns of ordinary work; with `cap`, stop once the context passes it."""
    for i in range(turns):
        if cap and s.main.ctx > cap:
            break
        s.prompt(s.rng.uniform(40, 240) if i else 0)
        s.main.work(s.rng.randint(*steps), s.project)
        if s.rng.random() < 0.25:
            s.agents([("Explore", "find where the config is read")], steps=(1, 2), report=900)
        s.main.request(new=400, output=s.rng.randint(300, 1200))


def generate(root: Path, now: datetime) -> dict[str, Any]:
    rng = random.Random(1969)
    today = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    day0 = today - timedelta(days=13)
    n = 0
    total = 0.0
    facts: dict[str, Any] = {"sessions": {}}

    def session(project: str, day: int, hour: float, **kw: Any) -> Session:
        nonlocal n
        n += 1
        start = day0 + timedelta(days=day, hours=hour, minutes=rng.uniform(0, 20))
        return Session(root, rng, project, start.astimezone(UTC), n, **kw)

    def done(s: Session, label: str | None = None) -> None:
        nonlocal total
        total += s.close()
        if label:
            facts["sessions"][label] = s.w.session_id

    for day in range(14):
        weekday = (day0 + timedelta(days=day)).weekday()
        if day == 13:  # today: sessions that started a few hours before now
            hours = [
                (now - timedelta(hours=h) - day0).total_seconds() / 3600 - 13 * 24
                for h in (5.5, 3.0)
            ]
        elif weekday >= 5:
            hours = [10.5] if rng.random() < 0.6 else []
        else:
            hours = [9.0, 10.5, 13.0, 14.5, 16.5][: rng.randint(3, 5)]
        for h in hours:
            project = rng.choices(list(PROJECTS), [5, 3, 2, 2, 1])[0]
            s = session(
                project,
                day,
                h,
                mcp=project == "orbit-api",
                skills=project != "pebble-cli",
                memory=42_000 if project == "orbit-api" else 9_000,
                ttl="5m" if project == "pebble-cli" else "1h",
            )
            ordinary(s, rng.randint(6, 13), cap=CAP)
            done(s)

        # The planted habits, one or two a day.
        if day == 2:  # idle past the cache lifetime: a long lunch at a big context
            s = session(
                "orbit-api",
                day,
                12.0,
                title="Speed up the search index",
                mcp=True,
                skills=True,
                memory=42_000,
            )
            ordinary(s, 8, steps=(8, 14))
            s.prompt(80 * 60)
            ordinary(s, 3)
            s.prompt(95 * 60)
            ordinary(s, 2)
            done(s, "idle")
        if day == 4:  # a large tool result reread for the rest of the session
            s = session("lantern-docs", day, 15.0, title="Generate the API reference", skills=True)
            ordinary(s, 2)
            s.prompt(60)
            s.main.tool(
                "Read",
                {"file_path": f"{PROJECTS['lantern-docs']}/build/openapi.json"},
                170_000,
                ran=1,
            )
            ordinary(s, 7)
            done(s, "large-result")
        if day == 6:  # context growth, then a late compaction
            s = session("tidepool-web", day, 10.0, title="Upgrade the bundler", skills=True)
            while s.main.ctx < 430_000:
                ordinary(s, 2, steps=(10, 16))
                s.main.work(6, s.project, big=True)
            pre = s.main.ctx
            s.main.wait(30)
            s.w.compact(s.main.t, pre=pre, post=38_000)
            s.main.ctx = 0
            s.main.request(new=38_000, output=900)
            ordinary(s, 4)
            facts["compaction_pre"] = pre
            done(s, "compaction")
        if day == 8:  # a fan-out: one reply starts eleven subagents
            s = session(
                "orbit-api",
                day,
                13.0,
                title="Audit every endpoint for input checks",
                mcp=True,
                skills=True,
                memory=42_000,
            )
            ordinary(s, 3)
            s.prompt(90)
            areas = ("orders", "users", "billing", "search", "webhooks", "admin", "export")
            kinds = [("general-purpose", f"check input validation in {x}") for x in areas]
            kinds += [("code-reviewer", f"review the {x} checks") for x in areas]
            s.agents(kinds, steps=(3, 5), report=2_500, prefix=100_000, slow=4, big=True)
            ordinary(s, 3)
            done(s, "fan-out")
        if day == 9:  # a workflow's agents, in parallel, without an Agent call
            s = session("quarry-etl", day, 10.5, title="Nightly data checks", skills=True)
            ordinary(s, 1)
            s.agents(
                [
                    ("workflow-subagent", f"check table {x}")
                    for x in (
                        "events",
                        "users",
                        "orders",
                        "invoices",
                        "sessions",
                        "devices",
                        "geo",
                        "refunds",
                    )
                ],
                steps=(3, 7),
                workflow=True,
            )
            ordinary(s, 2)
            done(s, "workflow")
        if day == 10:  # a model switch mid-session at a big context
            s = session(
                "orbit-api",
                day,
                15.5,
                title="Migrate settings to TOML",
                mcp=True,
                skills=True,
                memory=42_000,
            )
            ordinary(s, 6, steps=(8, 14))
            s.w.model_attachment(s.main.t, SONNET)
            s.main.model = SONNET
            s.main.last = s.main.t - timedelta(hours=2)  # a new model has no cache
            ordinary(s, 5)
            done(s, "model-switch")
        if day in (3, 7):  # the habit, small: a coder subagent waits on two long test runs
            s = session("quarry-etl", day, 15.0, title="Parallelize the loader", skills=True)
            ordinary(s, 2, cap=CAP)
            s.coders(["loader"], waits=2, dump=90_000)
            ordinary(s, 1, cap=CAP)
            done(s)
        if day == 11:  # the habit, large: four coders wait on ten-minute test runs
            s = session(
                "quarry-etl", day, 14.0, title="Backfill last quarter's events", skills=True
            )
            ordinary(s, 2, cap=CAP)
            s.coders(["events", "orders", "refunds", "invoices"], waits=3, dump=560_000)
            ordinary(s, 2, cap=CAP)
            done(s, "tool-wait")
        if day == 12:  # one-command subagents
            s = session(
                "tidepool-web", day, 11.0, title="Chart tooltips on touch screens", skills=True
            )
            for _ in range(4):
                ordinary(s, 1)
                s.agents(
                    [
                        ("Explore", "find the tooltip component"),
                        ("Explore", "list the chart files"),
                    ],
                    steps=(1, 1),
                    report=700,
                )
            done(s)

    # Today: a session still running, its last reply a couple of minutes ago.
    start = now - timedelta(minutes=48)
    n += 1
    s = Session(
        root,
        rng,
        "orbit-api",
        start.astimezone(UTC),
        n,
        mcp=True,
        skills=True,
        memory=42_000,
        title="Add webhook retries",
    )
    while s.main.t < now - timedelta(minutes=3):
        ordinary(s, 1)
    done(s, "live")
    facts["total_cost_state_usd"] = round(total, 2)
    facts["now"] = _iso(now)
    return facts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root", type=Path, help="the folder to fill (a fake CLAUDE_CONFIG_DIR)")
    ap.add_argument("--now", help="ISO time the data ends at (default: now)")
    args = ap.parse_args()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(UTC)
    if (args.root / "projects").exists():
        sys.exit(f"{args.root}/projects exists: give an empty folder")
    facts = generate(args.root, now.astimezone(UTC))
    (args.root / "synthetic.json").write_text(json.dumps(facts, indent=1))
    print(json.dumps(facts, indent=1))


if __name__ == "__main__":
    main()
