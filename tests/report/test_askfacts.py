# ruff: noqa: E501  (the fixtures and stub keep their lines whole)
"""What stands behind a part (capcom/report/askfacts.py), on a synthetic index: the
facts each kind of reference adds to a prompt, the lookups' answers, where agents,
skills and commands are defined, and that no session title, spawn description,
evidence row or tool input ever comes out. All data here is made up."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from capcom.cost.prices import load_prices
from capcom.report.analysis import Analysis
from capcom.report.api import Context
from capcom.report.askfacts import (
    MAX_CONTEXT,
    PRECEDENCE,
    Facts,
    FactsError,
    clip,
    definitions,
)
from capcom.store.index import open_index
from capcom.timeline.groups import folder_name
from capcom.types import (
    AgentInfo,
    Batch,
    ChangeMark,
    Evidence,
    FileState,
    Finding,
    LogFile,
    LogKind,
    Record,
    Request,
    SessionInfo,
    ToolCall,
    Usage,
)
from tests.report.conftest import FAKE

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
TABLE = load_prices()
OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5"
S_A = "aaaa1111-0000-4000-8000-000000000001"
S_B = "bbbb2222-0000-4000-8000-000000000002"
S_C = "cccc3333-0000-4000-8000-000000000003"
# Strings from the "logs" that must never come out of a fact or a tool.
SECRETS = (
    "Secret title one",
    "Renamed secret",
    "Secret title two",
    "Secret spawn description",
    "/secret/tool/input.txt",
    "secret evidence row",
    "secret-branch",
    "a note the user wrote",
)
_n = iter(range(1, 1_000_000))


def _req(
    sid: str, when: datetime, project: str, agent: str | None = None, model: str = OPUS, **u: int
) -> Request:
    i = next(_n)
    return Request(
        message_id=f"m{i}",
        session_id=sid,
        agent_id=agent,
        project_dir=project,
        timestamp=when,
        model=model,
        usage=Usage(**u),
        tool_calls=(ToolCall(f"t{i}", "Read", "/secret/tool/input.txt"),),
    )


@dataclass
class World:
    index_path: Path
    home: Path
    claude_root: Path
    proj_a: Path
    proj_b: Path
    changes: Path


def _put(ix: Any, log: LogFile, records: list[Record]) -> None:
    ix.apply(log, Batch(list(records), 100), FileState(log.path, 1, 100, 1, 100))


def build(tmp: Path) -> World:
    home = tmp / "home"
    proj_a = home / "Github" / "proj-a"
    proj_b = home / "Github" / "proj-b"
    claude_root = home / ".claude"
    for d in (proj_a / ".claude" / "agents", proj_b, claude_root / "agents"):
        d.mkdir(parents=True)
    (proj_a / ".claude" / "agents" / "fixer.md").write_text(
        "---\nname: fixer\nmodel: opus\n---\nSecret title one in a definition body\n"
    )
    (claude_root / "agents" / "scout.md").write_text("---\nname: scout\n---\nbody\n")
    (claude_root / "skills" / "review").mkdir(parents=True)
    (claude_root / "skills" / "review" / "SKILL.md").write_text("---\nname: review\n---\n")
    (proj_b / ".claude" / "commands").mkdir(parents=True)
    (proj_b / ".claude" / "commands" / "ship.md").write_text("Ship it\n")
    plug = claude_root / "plugins" / "cache" / "market" / "helpers" / "1.0" / "agents"
    plug.mkdir(parents=True)
    (plug / "helper.md").write_text("---\nname: helper\n---\n")

    pa = folder_name(proj_a)
    pb = folder_name(proj_b)
    path = tmp / "index.sqlite"
    ix = open_index(path)
    a_main = [
        _req(S_A, NOW - timedelta(days=2, hours=h), pa, model=SONNET, input=1000, output=500)
        for h in range(3)
    ]
    a_fix = [
        _req(
            S_A,
            NOW - timedelta(days=2, hours=h, minutes=5),
            pa,
            "ag1",
            cache_write_1h=20_000,
            output=2000,
        )
        for h in range(4)
    ]
    b_main = [
        _req(S_B, NOW - timedelta(days=1, hours=h), pb, input=5000, output=3000) for h in range(2)
    ]
    b_fix = [
        _req(S_B, NOW - timedelta(days=1, minutes=30), pb, "ag2", cache_write_1h=10_000, output=900)
    ]
    c_old = [_req(S_C, NOW - timedelta(days=20), pb, input=100, output=100)]
    log_a = LogFile(Path(f"/logs/{pa}/{S_A}.jsonl"), LogKind.SESSION, pa, S_A)
    _put(ix, log_a, [
        SessionInfo(S_A, pa, cwd=str(proj_a), title="Secret title one", custom_title="Renamed secret",
                    git_branch="secret-branch", first_seen=a_main[-1].timestamp, last_seen=a_main[0].timestamp),
        *a_main,
    ])  # fmt: skip
    log_ag1 = LogFile(
        Path(f"/logs/{pa}/{S_A}/subagents/agent-ag1.jsonl"), LogKind.SUBAGENT, pa, S_A, "ag1"
    )
    _put(
        ix,
        log_ag1,
        [AgentInfo("ag1", S_A, agent_type="fixer", description="Secret spawn description"), *a_fix],
    )
    log_b = LogFile(Path(f"/logs/{pb}/{S_B}.jsonl"), LogKind.SESSION, pb, S_B)
    _put(ix, log_b, [SessionInfo(S_B, pb, cwd=str(proj_b), title="Secret title two",
                                 first_seen=b_main[-1].timestamp, last_seen=b_main[0].timestamp), *b_main])  # fmt: skip
    log_ag2 = LogFile(
        Path(f"/logs/{pb}/{S_B}/subagents/agent-ag2.jsonl"), LogKind.SUBAGENT, pb, S_B, "ag2"
    )
    _put(
        ix,
        log_ag2,
        [AgentInfo("ag2", S_B, agent_type="fixer", description="Secret spawn description"), *b_fix],
    )
    log_c = LogFile(Path(f"/logs/{pb}/{S_C}.jsonl"), LogKind.SESSION, pb, S_C)
    _put(ix, log_c, [SessionInfo(S_C, pb, cwd=str(proj_b)), *c_old])
    ix.close()
    changes = tmp / "changes.jsonl"
    return World(path, home, claude_root, proj_a, proj_b, changes)


class FakeChanges:
    def __init__(self, marks: list[ChangeMark]) -> None:
        self.marks = marks

    def load(self, path: Any = None) -> list[ChangeMark]:
        return self.marks


MARK = ChangeMark(
    7, "model_mix", "fixer", date(2026, 10, 15), "The fixer agent runs on Opus",
    note="a note the user wrote",
)  # fmt: skip


def checks(*args: Any) -> list[Finding]:
    return [
        Finding(
            "model_mix", "The fixer agent runs on Opus", "fixer", "It ran 5 requests on Opus.", 12.5,
            "Try Sonnet.", evidence=(Evidence("secret evidence row", session_id=S_A),),
            metrics={"requests": 5, "share": 0.8, "folders": ("a", "b"), "names": "secret evidence row"},
        )
    ]  # fmt: skip


def analysis() -> Analysis:
    return Analysis(
        FAKE.cumulative_fn, FAKE.per_step_fn, FAKE.find_spikes_fn, FAKE.explain_fn, checks,
        changes_mod=FakeChanges([MARK]),
    )  # fmt: skip


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """The world in a folder outside /tmp, which capcom groups as "(temporary)": the
    projects keep their own names, as on a real machine. Removed afterwards."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="capcom-ask-test-", dir=base))
    try:
        w = build(root)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(w.claude_root))
        monkeypatch.setenv("HOME", str(w.home))
        yield w
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def facts(world: World) -> Iterator[Facts]:
    ix = open_index(world.index_path, readonly=True)
    ctx = Context(ix, TABLE, analysis(), NOW, tz=UTC, index_path=world.index_path)
    yield Facts(ctx)
    ix.close()


def no_secrets(out: Any) -> None:
    text = out if isinstance(out, str) else json.dumps(out)
    for s in SECRETS:
        assert s not in text, s


# --------------------------------------------------------------------------- the context


def test_an_agent_finding_says_where_it_ran_on_which_model_and_where_it_is_defined(
    facts: Facts, world: World
) -> None:
    text = facts.context({"kind": "finding", "finding": "model_mix", "scope": "fixer", "days": 14})
    assert "fixer agent type" in text
    assert "proj-a" in text and "proj-b" in text
    assert "claude-opus-5-5" in text and SONNET not in text  # only the agent's requests
    assert str(world.proj_a / ".claude" / "agents" / "fixer.md") in text
    assert "a project agent, in proj-a" in text
    assert "Spawned in: proj-a" in text or "Spawned in: proj-b" in text
    assert S_A in text  # top sessions by id
    assert len(text) <= MAX_CONTEXT
    no_secrets(text)


def test_a_project_finding_covers_that_project(facts: Facts, world: World) -> None:
    pb = folder_name(world.proj_b)
    text = facts.context({"kind": "finding", "finding": "late_compactions", "scope": pb})
    assert "project proj-b" in text
    assert (
        "By project: proj-b" in text and "proj-a" not in text.split("By project:")[1].split("\n")[0]
    )
    assert "defined in" not in text
    no_secrets(text)


def test_a_marked_change_counts_from_four_weeks_before_its_date(facts: Facts) -> None:
    text = facts.context({"kind": "mark", "id": 7})
    assert "marked applied on 2026-10-15" in text
    assert "since 2026-09-17" in text
    assert "fixer" in text and "fixer.md" in text
    no_secrets(text)
    assert facts.context({"kind": "mark", "id": 99}) == ""


def test_a_session_by_its_id_with_no_title(facts: Facts, world: World) -> None:
    text = facts.context({"kind": "session", "id": S_A})
    assert S_A in text and str(world.proj_a) in text
    assert "By model:" in text and "By agent type:" in text and "fixer" in text
    assert "Subagents spawned: fixer 1" in text
    no_secrets(text)
    assert facts.context({"kind": "session", "id": "ffffffff-none"}) == ""


def test_a_day_and_a_range(facts: Facts) -> None:
    day = facts.context({"kind": "day", "date": "2026-10-20"})
    assert "on 2026-10-20" in day and "proj-b" in day and "proj-a" not in day
    view = facts.context({"kind": "view", "days": 7})
    assert "the last 7 days" in view and "By day:" in view
    assert "2026-10-01" not in view  # S_C is 20 days back
    assert "the last 30 days" in facts.context({"kind": "view", "days": 30})
    no_secrets(day + view)


def test_clip_keeps_whole_lines_under_the_budget() -> None:
    lines = [f"line {i} " + "x" * 90 for i in range(100)]
    out = clip(lines, 500)
    assert len(out) <= 500 and out.split("\n")[-1].startswith("line ")
    assert all(line in lines for line in out.split("\n"))


# --------------------------------------------------------------------------- the lookups


def test_spend_grouped_and_filtered(facts: Facts) -> None:
    out = facts.spend({"group_by": "project", "model": "opus", "days": 14})
    labels = [r["project"] for r in out["rows"]]
    assert labels[0] in ("proj-a", "proj-b") and set(labels) == {"proj-a", "proj-b"}
    assert out["filter"] == "model opus"
    assert out["total"]["requests"] == 4 + 2 + 1
    assert sum(r["requests"] for r in out["rows"]) == 7
    assert all("folder" in r for r in out["rows"])
    by_agent = facts.spend({"group_by": "agent_type", "project": "proj-a"})
    assert {r["agent_type"]: r["requests"] for r in by_agent["rows"]} == {"fixer": 4, "main": 3}
    by_day = facts.spend({"group_by": "day", "days": 30})
    assert [r["day"] for r in by_day["rows"]] == ["2026-10-01", "2026-10-19", "2026-10-20"]
    sessions = facts.spend({"group_by": "session", "agent_type": "fixer"})
    assert {r["session_id"] for r in sessions["rows"]} == {S_A, S_B}
    ranged = facts.spend({"group_by": "model", "since": "2026-10-19", "until": "2026-10-19"})
    assert ranged["total"]["requests"] == 7
    no_secrets([out, by_agent, by_day, sessions, ranged])


@pytest.mark.parametrize(
    "args",
    [
        {"group_by": "title"},
        {"group_by": "project", "days": 0},
        {"group_by": "project", "days": 91},
        {"group_by": "project", "days": "7"},
        {"group_by": "project", "since": "yesterday"},
        {"group_by": "project", "since": "2026-10-10", "until": "2026-10-01"},
        {"group_by": "project", "since": "2026-01-01", "until": "2026-10-01"},
        {"group_by": "project", "session_id": "../../etc"},
        {"group_by": "project", "project": "x" * 300},
    ],
)
def test_spend_refuses_what_it_cannot_take(facts: Facts, args: dict[str, Any]) -> None:
    with pytest.raises(FactsError):
        facts.spend(args)


def test_session_lookup_by_prefix(facts: Facts) -> None:
    out = facts.session({"session_id": S_B[:8]})
    assert out["found"] and out["session_id"] == S_B and out["project"] == "proj-b"
    assert out["subagents_spawned"] == {"fixer": 1}
    assert {r["model"] for r in out["by_model"]} == {OPUS}
    assert facts.session({"session_id": "dddddddd"})["found"] is False
    no_secrets(out)


def test_findings_give_numbers_but_no_evidence(facts: Facts) -> None:
    out = facts.findings({"days": 14})
    f = out["findings"][0]
    assert f["kind"] == "model_mix" and f["scope"] == "fixer"
    assert f["numbers"] == {"requests": 5.0, "share": 0.8}
    no_secrets(out)


def test_where_defined_agents_skills_commands_and_plugins(facts: Facts, world: World) -> None:
    out = facts.where_defined({"name": "fixer"})
    assert out["defined_in"] == [
        {
            "path": str(world.proj_a / ".claude/agents/fixer.md"),
            "level": "project",
            "what": "agent",
            "project": "proj-a",
            "use": "the one used in proj-a",
            "overridden": False,
        }
    ]
    assert "precedence" not in out
    assert [r["project"] for r in out["spawned_in"]] == ["proj-a", "proj-b"]
    scout = facts.where_defined({"name": "scout", "kind": "agent"})["defined_in"]
    assert scout == [
        {"path": str(world.claude_root / "agents/scout.md"), "level": "user", "what": "agent",
         "use": "the one used in every project", "overridden": False}
    ]  # fmt: skip
    skill = facts.where_defined({"name": "review", "kind": "skill"})["defined_in"]
    assert skill[0]["path"].endswith("skills/review/SKILL.md")
    cmd = facts.where_defined({"name": "/ship", "kind": "command"})["defined_in"]
    assert cmd[0]["path"] == str(world.proj_b / ".claude/commands/ship.md")
    plug = facts.where_defined({"name": "helpers:helper"})["defined_in"]
    assert plug[0]["level"] == "plugin" and plug[0]["plugin"] == "helpers"
    gone = facts.where_defined({"name": "general-purpose"})
    assert gone["defined_in"] == [] and "built into Claude Code" in gone["message"]
    with pytest.raises(FactsError):
        facts.where_defined({"name": "../x y"})
    no_secrets([out, scout, skill, cmd, plug])


def test_definitions_never_read_a_body(world: World) -> None:
    found = definitions("fixer", "agent", [world.proj_a], world.claude_root)
    assert "Secret" not in json.dumps(found)


def test_nothing_from_the_logs_comes_out_of_any_lookup(facts: Facts) -> None:
    outs: list[Any] = []
    for by in ("project", "model", "agent_type", "day", "session"):
        outs.append(facts.spend({"group_by": by, "days": 90}))
    for sid in (S_A, S_B, S_C):
        outs.append(facts.session({"session_id": sid}))
        outs.append(facts.context({"kind": "session", "id": sid}))
    outs.append(facts.findings({}))
    outs.append(facts.where_defined({"name": "fixer"}))
    no_secrets(outs)


def test_the_definition_claude_code_uses_comes_first(facts: Facts, world: World) -> None:
    """The same agent at every level: a project's own file overrides the user's,
    which overrides a plugin's, and each project that spawned it says which it got."""
    user = world.claude_root / "agents" / "fixer.md"
    user.write_text("---\nname: fixer\n---\nbody\n")
    plugin = world.claude_root / "plugins/cache/market/helpers/1.0/agents/fixer.md"
    plugin.write_text("---\nname: fixer\n---\nbody\n")
    project = world.proj_a / ".claude/agents/fixer.md"
    out = facts.where_defined({"name": "fixer"})
    rows = out["defined_in"]
    assert [(r["path"], r["level"], r["overridden"]) for r in rows] == [
        (str(project), "project", False),
        (str(user), "user", True),
        (str(plugin), "plugin", True),
    ]
    assert rows[0]["use"] == "the one used in proj-a"
    assert rows[1]["use"] == (
        "overridden in proj-a by the project's own file; used in every other project"
    )
    assert rows[2]["use"] == "overridden by the user's file"
    assert out["precedence"] == PRECEDENCE
    uses = {r["project"]: r["uses"] for r in out["spawned_in"]}
    assert uses == {"proj-a": str(project), "proj-b": str(user)}
    # the facts say the same, the project's file first
    text = facts.context({"kind": "finding", "finding": "model_mix", "scope": "fixer"})
    assert text.index(str(project)) < text.index(str(user)) < text.index(str(plugin))
    assert "overridden in proj-a by the project's own file" in text
    assert PRECEDENCE in text
    assert f"using {user}" in text
    no_secrets(text)
    # without a project file, the user's is the one used and the plugin's is overridden
    project.unlink()
    rows = facts.where_defined({"name": "fixer"})["defined_in"]
    assert [(r["level"], r["overridden"]) for r in rows] == [("user", False), ("plugin", True)]
    assert rows[0]["use"] == "the one used in every project"
