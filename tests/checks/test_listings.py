"""Checks on what rides in every prompt: connectors, MCP servers, skills, memory and
agent tools. All sessions are synthetic."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from capcom.checks import run_checks
from capcom.types import EventKind, ToolCall
from tests.checks.helpers import (
    CWD,
    READ_RATE,
    SINCE,
    TABLE,
    UNTIL,
    agent,
    ev,
    index,
    kinds,
    only,
    run,
)

DOCS_TOOLS = ("mcp__claude_ai_Docs__read", "mcp__claude_ai_Docs__write")


def _checks(idx, agent_dirs=()):  # type: ignore[no-untyped-def]
    return run_checks(idx, TABLE, SINCE, UNTIL, agent_dirs=list(agent_dirs))


def _connector_events(session: str = "s1") -> list:  # type: ignore[type-arg]
    return [
        ev(
            EventKind.TOOL_SET,
            0,
            session,
            source="deferred_tools_delta",
            added=DOCS_TOOLS,
            size_tokens=12,
        ),
        ev(
            EventKind.TOOL_SET,
            0,
            session,
            source="mcp_instructions_delta",
            added=("claude.ai Docs",),
            size_tokens=1000,
        ),
    ]


# --------------------------------------------------------------------------- connectors


def test_connectors_never_called_give_the_env_change_and_a_hand_checked_saving() -> None:
    idx = index(run(200), _connector_events())
    f = only(_checks(idx), "claudeai_connectors_unused")
    # 6 + 6 tokens of deferred names + 1,000 of instructions, read from the cache by
    # 200 requests at $0.20 per million, over a one-week window
    assert f.saving_usd_per_week == pytest.approx(1012 * 200 * READ_RATE)
    assert f.scope == "-home-user-proj"
    assert f.change is not None and '"ENABLE_CLAUDEAI_MCP_SERVERS": "false"' in f.change
    assert f"{CWD}/.claude/settings.local.json" in f.advice
    assert f.metrics["sessions_using"] == 0
    assert f.metrics["tokens_per_request"] == 1012
    assert f.metrics["requests"] == 200  # three items, each request counted once


def test_connectors_called_in_the_session_are_not_flagged() -> None:
    idx = index(run(200, calls={5: (DOCS_TOOLS[0],)}), _connector_events())
    assert "claudeai_connectors_unused" not in kinds(_checks(idx))


def test_connector_saving_counts_only_the_sessions_that_never_call_them() -> None:
    reqs = (
        run(200, session="a") + run(200, session="b") + run(100, session="c", calls={0: DOCS_TOOLS})
    )
    evs = _connector_events("a") + _connector_events("b") + _connector_events("c")
    f = only(_checks(index(reqs, evs)), "claudeai_connectors_unused")
    assert f.saving_usd_per_week == pytest.approx(1012 * 400 * READ_RATE)
    assert f.metrics["sessions_using"] == 1
    # turning them off for the project would break session c: no change, and the
    # advice names the session that uses them
    assert f.change is None
    assert "Sessions c " in f.advice


def test_the_window_pace_scales_a_shorter_window_to_a_week() -> None:
    idx = index(run(200), _connector_events())
    found = run_checks(idx, TABLE, SINCE, SINCE + (UNTIL - SINCE) / 7, agent_dirs=[])
    f = only(found, "claudeai_connectors_unused")
    assert f.saving_usd_per_week == pytest.approx(1012 * 200 * READ_RATE * 7)


# --------------------------------------------------------------------------- MCP servers


def _github(session: str = "s1") -> list:  # type: ignore[type-arg]
    names = tuple(f"mcp__github__t{i}" for i in range(10))
    return [
        ev(
            EventKind.TOOL_SET,
            0,
            session,
            source="api-request-shape",
            names=("Read", "Bash", *names),
            size_tokens=20_000,
            tools_size_tokens=12_000,
        )
    ]


def test_an_mcp_server_never_called_is_flagged_with_its_tool_sizes() -> None:
    idx = index(run(100, calls={0: ("Read",)}), _github())
    f = only(_checks(idx), "mcp_servers_unused")
    # tools_size_tokens 12,000 over 12 tools: 1,000 each, 10 of them unused
    assert f.saving_usd_per_week == pytest.approx(10_000 * 100 * READ_RATE)
    assert f.change is not None and "github" in f.change
    assert f.metrics["tools_loaded"] == 12
    assert f.metrics["tools_called"] == 1


def test_a_called_mcp_server_is_not_flagged() -> None:
    idx = index(run(100, calls={3: ("mcp__github__t4",)}), _github())
    assert "mcp_servers_unused" not in kinds(_checks(idx))


def test_shape_without_tools_size_uses_its_tools_share() -> None:
    evs = [
        ev(
            EventKind.TOOL_SET,
            0,
            source="api-request-shape",
            names=("Read", "mcp__x__a"),
            size_tokens=8_000,
        )
    ]
    f = only(_checks(index(run(100), evs)), "mcp_servers_unused")
    assert f.saving_usd_per_week == pytest.approx(8_000 * 0.75 / 2 * 100 * READ_RATE)


def test_deferred_tools_in_a_shape_are_priced_by_name_only() -> None:
    deferred = tuple(f"mcp__github__t{i}" for i in range(10))
    evs = [
        ev(
            EventKind.TOOL_SET,
            0,
            source="api-request-shape",
            names=("Read", "Bash", *deferred),
            deferred_names=deferred,
            size_tokens=30_000,
            tools_size_tokens=2_000,
        ),
        ev(EventKind.TOOL_SET, 0, source="deferred_tools_delta", added=deferred, size_tokens=1_000),
    ]
    f = only(_checks(index(run(100, calls={0: ("Read",)}), evs)), "mcp_servers_unused")
    # the ten deferred tools cost their 100 tokens of listing each, not a schema
    assert f.saving_usd_per_week == pytest.approx(1_000 * 100 * READ_RATE)


def test_a_record_entry_already_in_the_shape_counts_once() -> None:
    evs = [
        ev(
            EventKind.TOOL_SET,
            0,
            source="api-request-shape",
            names=("Read", "mcp__x__a"),
            deferred_names=(),
            size_tokens=8_000,
            tools_size_tokens=2_000,
        ),
        ev(
            EventKind.TOOL_SET,
            0,
            source="deferred_tools_record",
            names=("mcp__x__a", "mcp__x__b"),
            size_tokens=600,
        ),
    ]
    f = only(_checks(index(run(100), evs)), "mcp_servers_unused")
    # a: 1,000 tokens from the shape (2,000 over two loaded tools); b: 300 from the record
    assert f.saving_usd_per_week == pytest.approx((1_000 + 300) * 100 * READ_RATE)


# --------------------------------------------------------------------------- skills


def _skills() -> list:  # type: ignore[type-arg]
    return [
        ev(
            EventKind.TOOL_SET,
            0,
            source="skill_listing",
            names=("pdf", "plugin:docx", "xlsx", "slides"),
            size_tokens=4_000,
            is_initial=True,
        )
    ]


def test_skills_never_invoked_are_flagged() -> None:
    reqs = run(100)
    reqs[2] = replace(reqs[2], tool_calls=(ToolCall("tu_skill", "Skill", "pdf"),))
    evs = [*_skills(), ev(EventKind.SKILLS_INVOKED, 30, names=("docx",))]
    f = only(_checks(index(reqs, evs)), "skills_unused")
    assert f.metrics["skills_listed"] == 4
    assert f.metrics["skills_invoked"] == 2
    assert f.saving_usd_per_week == pytest.approx(2 * 1_000 * 100 * READ_RATE)
    assert "xlsx" in f.summary and "slides" in f.summary
    assert f.change == "disable-model-invocation: true"


def test_all_skills_invoked_gives_no_finding() -> None:
    evs = [*_skills(), ev(EventKind.SKILLS_INVOKED, 3, names=("pdf", "docx", "xlsx", "slides"))]
    assert "skills_unused" not in kinds(_checks(index(run(100), evs)))


def test_a_slash_command_counts_as_an_invocation() -> None:
    evs = [
        *_skills(),
        ev(EventKind.SKILLS_INVOKED, 3, names=("pdf", "docx", "xlsx")),
        ev(EventKind.SKILLS_INVOKED, 4, names=("slides",), source="slash_command"),
    ]
    assert "skills_unused" not in kinds(_checks(index(run(100), evs)))


def test_a_skill_used_in_another_project_is_moved_not_disabled() -> None:
    other = "-home-user-other"
    reqs = [*run(100), *run(10, session="s2", project=other)]
    evs = [
        *_skills(),
        ev(EventKind.SKILLS_INVOKED, 3, names=("pdf", "docx")),
        ev(EventKind.SKILLS_INVOKED, 3, "s2", names=("xlsx", "slides")),
    ]
    f = only(_checks(index(reqs, evs)), "skills_unused")
    assert f.change is None  # disabling xlsx and slides would hide them there too
    assert "move them" in f.advice and other in f.advice
    assert f.metrics["skills_used_elsewhere"] == 2
    evs[-1] = ev(EventKind.SKILLS_INVOKED, 3, "s2", names=("xlsx",))
    f = only(_checks(index(reqs, evs)), "skills_unused")
    assert f.change == "disable-model-invocation: true"
    assert "No project invoked slides" in f.advice and "xlsx is used in other" in f.advice


# --------------------------------------------------------------------------- memory


def _memory(size: int) -> list:  # type: ignore[type-arg]
    return [
        ev(
            EventKind.MEMORY,
            0,
            source="instructions",
            paths=(f"{CWD}/CLAUDE.md",),
            size_tokens=size,
        )
    ]


def test_large_instructions_save_the_part_above_the_budget() -> None:
    f = only(_checks(index(run(100), _memory(6_000))), "large_memory")
    assert f.saving_usd_per_week == pytest.approx((6_000 - 2_000) * 100 * READ_RATE)
    assert f.evidence[0].label.startswith("CLAUDE.md: ")  # relative to the repo root


def test_short_instructions_give_no_finding() -> None:
    assert "large_memory" not in kinds(_checks(index(run(100), _memory(1_500))))


def test_a_new_instructions_listing_replaces_the_old_one() -> None:
    later = ev(
        EventKind.MEMORY, 50.5, source="instructions", paths=("/x/CLAUDE.md",), size_tokens=500
    )
    evs = [*_memory(6_000), later]
    f = only(_checks(index(run(100), evs)), "large_memory")
    assert f.saving_usd_per_week == pytest.approx(4_000 * 50 * READ_RATE)


def test_worktree_copies_of_a_file_count_as_the_file() -> None:
    evs = [
        ev(
            EventKind.MEMORY,
            0,
            f"s{i}",
            source="nested_memory",
            paths=(f"{CWD}/.worktrees/{branch}/AGENTS.md",),
            size_tokens=6_000,
        )
        for i, branch in enumerate(("feat/a", "fix/b", "c"))
    ]
    evs.append(
        ev(
            EventKind.MEMORY,
            0,
            "s3",
            source="nested_memory",
            paths=(f"{CWD}/AGENTS.md",),
            size_tokens=6_000,
        )
    )
    evs.append(
        ev(
            EventKind.MEMORY,
            0,
            "s4",
            source="instructions",
            paths=(f"{CWD}/CLAUDE.md", f"{CWD}/AGENTS.md", f"{CWD}/docs/x.md"),
            size_tokens=9_000,
        )
    )
    reqs = [r for i in range(5) for r in run(10, session=f"s{i}")]
    f = only(_checks(index(reqs, evs)), "large_memory")
    labels = [e.label.split(":")[0] for e in f.evidence]
    assert sorted(labels) == ["AGENTS.md", "CLAUDE.md, AGENTS.md, docs/x.md"]
    assert f.metrics["files_over"] == 2
    assert "2 always-loaded attachments" in f.summary
    # the saving is unchanged: every copy's part above the budget, in its own requests
    assert f.saving_usd_per_week == pytest.approx((4 * 4_000 + 3_000) * 10 * READ_RATE)


# --------------------------------------------------------------------------- agent tools


def _coder_runs(calls: tuple[str, ...], kind: str = "coder", built_in: bool | None = None):  # type: ignore[no-untyped-def]
    reqs, agents = [], []
    for i in range(3):
        aid = f"a{i}"
        reqs += run(40, start=10 + i, agent=aid, calls={0: calls})
        agents.append(agent(aid, kind, is_built_in=built_in))
    return reqs, agents


def _definition(tmp_path: Path, tools: str) -> Path:
    folder = tmp_path / "agents"
    folder.mkdir()
    (folder / "coder.md").write_text(f"---\nname: coder\n{tools}\n---\nBody text.\n")
    return folder


def test_agent_tools_never_used_get_a_tools_line(tmp_path: Path) -> None:
    folder = _definition(tmp_path, "tools: Read, Edit, Bash, WebFetch, WebSearch")
    reqs, agents = _coder_runs(("Read", "Bash"))
    f = only(_checks(index(reqs, agents=agents), [folder]), "agent_tools_unused")
    assert f.scope == "coder"
    assert f.change == "tools: Read, Bash"  # in the file's order
    assert str(folder / "coder.md") in f.advice
    # three tools without a listing: the default 600 tokens each, 120 requests
    assert f.saving_usd_per_week == pytest.approx(3 * 600 * 120 * READ_RATE)
    assert f.metrics["tools_given"] == 5 and f.metrics["tools_called"] == 2


def test_agent_tools_from_its_own_listing_when_the_definition_has_none(tmp_path: Path) -> None:
    folder = _definition(tmp_path, "model: opus")
    reqs, agents = _coder_runs(("Read",))
    evs = [
        ev(
            EventKind.TOOL_SET,
            9,
            "s1",
            f"a{i}",
            source="api-request-shape",
            names=("Read", "Write"),
            size_tokens=4000,
            tools_size_tokens=2000,
        )
        for i in range(3)
    ]
    f = only(_checks(index(reqs, evs, agents), [folder]), "agent_tools_unused")
    assert f.change == "tools: Read"
    assert f.saving_usd_per_week == pytest.approx(1_000 * 120 * READ_RATE)


def test_an_agent_using_all_its_tools_is_not_flagged(tmp_path: Path) -> None:
    folder = _definition(tmp_path, "tools: Read, Bash")
    reqs, agents = _coder_runs(("Read", "Bash"))
    assert "agent_tools_unused" not in kinds(_checks(index(reqs, agents=agents), [folder]))


def _listing(names: tuple[str, ...]) -> list:  # type: ignore[type-arg]
    return [
        ev(
            EventKind.TOOL_SET,
            9,
            "s1",
            f"a{i}",
            source="api-request-shape",
            names=names,
            size_tokens=4000,
            tools_size_tokens=2000,
        )
        for i in range(3)
    ]


def test_built_in_agents_are_not_flagged(tmp_path: Path) -> None:
    folder = tmp_path / "agents"
    folder.mkdir()
    (folder / "Explore.md").write_text("---\nname: Explore\n---\n")
    (folder / "helper.md").write_text("---\nname: helper\n---\n")
    evs = _listing(("Read", "Write"))
    for kind, built_in in (("Explore", None), ("helper", True)):
        reqs, agents = _coder_runs(("Read",), kind=kind, built_in=built_in)
        found = _checks(index(reqs, evs, agents), [folder])
        assert "agent_tools_unused" not in kinds(found), kind
    # the same runs as a custom agent are flagged (the listing makes Write unused)
    reqs, agents = _coder_runs(("Read",), kind="helper")
    assert "agent_tools_unused" in kinds(_checks(index(reqs, evs, agents), [folder]))


def test_an_agent_type_without_a_definition_file_gets_no_advice(tmp_path: Path) -> None:
    reqs, agents = _coder_runs(("Read",), kind="workflow-subagent")
    evs = _listing(("Read", "Write"))
    assert "agent_tools_unused" not in kinds(_checks(index(reqs, evs, agents), [tmp_path]))


def test_the_tools_line_keeps_servers_patterns_and_the_agent_tool(tmp_path: Path) -> None:
    folder = _definition(
        tmp_path, "tools: Read, Task, mcp__github, mcp__slack, Bash(git *), WebFetch"
    )
    reqs, agents = _coder_runs(("Read", "Agent", "mcp__github__get_issue"))
    f = only(_checks(index(reqs, agents=agents), [folder]), "agent_tools_unused")
    assert f.change == "tools: Read, Task, mcp__github, Bash(git *)"
    assert [e.label for e in f.evidence] == ["never called: mcp__slack", "never called: WebFetch"]
    assert f.metrics["tools_given"] == 6 and f.metrics["tools_called"] == 4


# --------------------------------------------------------------------------- overlaps


def test_findings_name_the_kinds_they_may_overlap_with() -> None:
    reqs = run(100, cache_read=200_000)
    found = _checks(index(reqs, _connector_events()))
    f = only(found, "claudeai_connectors_unused")
    assert f.metrics["overlaps"] == ("long_running_agents",)
    lr = only(found, "long_running_agents")
    assert lr.metrics["overlaps"] == ("claudeai_connectors_unused",)
