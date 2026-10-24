"""Checks on what rides in every prompt: connectors, MCP servers, skills, memory and
agent tools. All sessions are synthetic."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from capcom.checks import run_checks
from capcom.types import EventKind, ToolCall
from tests.checks.helpers import (
    CWD,
    PROJECT,
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


def test_a_plugin_skill_is_advised_off_by_plugin_not_by_skill_md() -> None:
    evs = [*_skills(), ev(EventKind.SKILLS_INVOKED, 3, names=("pdf", "xlsx", "slides"))]
    f = only(_checks(index(run(100), evs)), "skills_unused")
    assert f.change is None  # a plugin update would overwrite an edited SKILL.md
    assert "plugin:docx comes from the plugin plugin" in f.advice
    assert "disable it here" in f.advice and "enabledPlugins" in f.advice
    assert "SKILL.md frontmatter" not in f.advice
    assert f.metrics["plugin_skills"] == 1
    assert f.saving_usd_per_week == pytest.approx(1_000 * 100 * READ_RATE)
    # with a plain skill unused too, only that one gets the SKILL.md line
    evs[-1] = ev(EventKind.SKILLS_INVOKED, 3, names=("pdf", "xlsx"))
    f = only(_checks(index(run(100), evs)), "skills_unused")
    assert f.change == "disable-model-invocation: true"
    assert "No project invoked slides:" in f.advice and "plugin:docx comes" in f.advice


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


def _coder_runs(  # type: ignore[no-untyped-def]
    calls: tuple[str, ...],
    kind: str = "coder",
    built_in: bool | None = None,
    spawns: int = 3,
    session: str = "s1",
    project: str = PROJECT,
):
    reqs, agents = [], []
    for i in range(spawns):
        aid = f"{session}a{i}" if session != "s1" else f"a{i}"
        reqs += run(40, start=10 + i, session=session, agent=aid, project=project, calls={0: calls})
        agents.append(agent(aid, kind, session=session, is_built_in=built_in))
    return reqs, agents


def _definition(tmp_path: Path, tools: str) -> Path:
    folder = tmp_path / "agents"
    folder.mkdir()
    (folder / "coder.md").write_text(f"---\nname: coder\n{tools}\n---\nBody text.\n")
    return folder


def _listing(names: tuple[str, ...], spawns: int = 3, session: str = "s1") -> list:  # type: ignore[type-arg]
    return [
        ev(
            EventKind.TOOL_SET,
            9,
            session,
            f"{session}a{i}" if session != "s1" else f"a{i}",
            source="api-request-shape",
            names=names,
            size_tokens=4000,
            tools_size_tokens=2000,
        )
        for i in range(spawns)
    ]


def test_agent_tools_never_used_get_a_tools_line(tmp_path: Path) -> None:
    folder = _definition(tmp_path, "tools: Read, Edit, Bash, WebFetch, WebSearch")
    reqs, agents = _coder_runs(("Read", "Bash"))
    f = only(_checks(index(reqs, agents=agents), [folder]), "agent_tools_unused")
    assert f.scope == "coder"
    assert f.change == "tools: Read, Bash"  # in the file's order
    assert f"frontmatter of {folder / 'coder.md'} (between its --- lines)" in f.advice
    # three tools without a listing: the default 600 tokens each, 120 requests
    assert f.saving_usd_per_week == pytest.approx(3 * 600 * 120 * READ_RATE)
    assert f.metrics["tools_given"] == 5 and f.metrics["tools_called"] == 2


def test_an_agent_without_a_tools_line_gets_a_line_marked_as_a_restriction(
    tmp_path: Path,
) -> None:
    folder = _definition(tmp_path, "model: opus")
    reqs, agents = _coder_runs(("Read",), spawns=10)
    evs = _listing(("Read", "Write"), spawns=10)
    f = only(_checks(index(reqs, evs, agents), [folder]), "agent_tools_unused")
    assert f.change is not None
    comment, line = f.change.splitlines()
    assert line == "tools: Read"
    assert comment.startswith("# a restriction")  # a YAML comment: safe to paste
    assert "only these 1 tools" in comment and "inherits all 2" in comment
    assert "no tools: line" in f.advice and "is a restriction" in f.advice
    assert f.metrics["restricts"] is True
    assert f.saving_usd_per_week == pytest.approx(1_000 * 400 * READ_RATE)


def test_few_spawns_without_a_tools_line_get_advice_but_no_line(tmp_path: Path) -> None:
    folder = _definition(tmp_path, "model: opus")
    reqs, agents = _coder_runs(("Read",))
    evs = _listing(("Read", "Write"))
    f = only(_checks(index(reqs, evs, agents), [folder]), "agent_tools_unused")
    assert f.change is None
    assert "thin sample" in f.advice
    assert f"{folder / 'coder.md'} has no tools: line in its frontmatter" in f.advice
    assert f.saving_usd_per_week == pytest.approx(1_000 * 120 * READ_RATE)


def test_the_restricting_line_keeps_every_tool_called_in_claude_code_projects(
    tmp_path: Path,
) -> None:
    folder = _definition(tmp_path, "model: opus")
    root = tmp_path / ".openclaw"
    claw = re.sub(r"[^A-Za-z0-9]", "-", str(root)) + "-workspace"  # Claude Code's naming
    # Claude Code spawns: Read listed and called; Grep called but in no listing
    reqs, agents = _coder_runs(("Read", "Grep"), spawns=10)
    evs = _listing(("Read", "Write", "Bash"), spawns=10)
    # OpenClaw spawns call its own tools, listed in their own sessions only
    more, more_agents = _coder_runs(("process", "mcp__claw__exec"), session="s9", project=claw)
    evs += [
        ev(
            EventKind.TOOL_SET,
            9,
            "s9",
            a.agent_id,
            source="api-request-shape",
            names=("Read", "process", "mcp__claw__exec"),
            size_tokens=4000,
            tools_size_tokens=2000,
        )
        for a in more_agents
    ]
    idx = index(reqs + more, evs, agents + more_agents)
    found = run_checks(idx, TABLE, SINCE, UNTIL, agent_dirs=[folder], openclaw_root=root)
    f = only(found, "agent_tools_unused")
    assert f.change is not None and f.change.splitlines()[-1] == "tools: Grep, Read"
    for name in ("process", "mcp__claw__exec"):
        assert name not in f.change and name in f.advice  # named as left out
    assert f.metrics["spawns"] == 10 and f.metrics["tools_given"] == 3


def _shapes(names: tuple[str, ...], minute: float = 9, spawns: int = 10, **attrs) -> list:  # type: ignore[no-untyped-def, type-arg]
    return [
        ev(
            EventKind.TOOL_SET,
            minute,
            agent=f"a{i}",
            source="api-request-shape",
            names=names,
            size_tokens=8000,
            **attrs,
        )
        for i in range(spawns)
    ]


def test_deferred_tools_ride_by_name_and_save_nothing(tmp_path: Path) -> None:
    folder = _definition(tmp_path, "model: opus")
    reqs, agents = _coder_runs(("Read",), spawns=10)
    evs = _shapes(("Read", "Write", "Bash"), deferred_names=("Write",), tools_size_tokens=2_000)
    evs += [
        ev(
            EventKind.TOOL_SET,
            9,
            agent=f"a{i}",
            source="deferred_tools_delta",
            added=("mcp__x__y",),
            size_tokens=10,
        )
        for i in range(10)
    ]
    f = only(_checks(index(reqs, evs, agents), [folder]), "agent_tools_unused")
    # only Bash's schema (2,000 tokens over Read and Bash) rides; Write and mcp__x__y
    # are names only
    assert f.saving_usd_per_week == pytest.approx(1_000 * 400 * READ_RATE)
    assert f.metrics["tools_given"] == 3 and f.metrics["unused_with_schema"] == 1
    assert "deferred, sent by name only" in f.summary


@pytest.mark.parametrize("minute", [9, 29.5])
def test_the_saving_is_the_same_when_listings_begin_mid_window(
    tmp_path: Path, minute: float
) -> None:
    # listings logged from the start, or only for the last half of the requests (an
    # older Claude Code logged none): the same weekly figure
    folder = _definition(tmp_path, "model: opus")
    reqs, agents = _coder_runs(("Read",), spawns=10)
    evs = _shapes(("Read", "Write"), minute=minute, tools_size_tokens=2_000)
    f = only(_checks(index(reqs, evs, agents), [folder]), "agent_tools_unused")
    assert f.saving_usd_per_week == pytest.approx(1_000 * 400 * READ_RATE)


def test_tools_called_before_a_short_window_stay_in_the_line(tmp_path: Path) -> None:
    folder = _definition(tmp_path, "model: opus")
    reqs, agents = _coder_runs(("Read",), spawns=10)
    # ten days before the window, a coder called one of the improved server's tools
    early = run(1, start=-60 * 24 * 10, agent="old", calls={0: ("mcp__improved__find",)})
    agents.append(agent("old", "coder"))
    names = ("Read", "Write", "mcp__improved__find", "mcp__improved__get_plan")
    evs = _shapes(names, tools_size_tokens=4_000)
    f = only(_checks(index(reqs + early, evs, agents), [folder]), "agent_tools_unused")
    assert f.change is not None and f.change.splitlines()[-1] == "tools: Read, mcp__improved"
    assert f.saving_usd_per_week == pytest.approx(1_000 * 400 * READ_RATE)  # Write only


def test_a_project_level_definition_counts_only_its_own_project(tmp_path: Path) -> None:
    folder = tmp_path / "proj" / ".claude" / "agents"
    folder.mkdir(parents=True)
    (folder / "coder.md").write_text("---\nname: coder\n---\n")
    other = "-home-user-other"
    reqs, agents = _coder_runs(("Read",), spawns=10)
    # main-thread turns give the project its cwd, where the definition lives
    reqs += [replace(r, cwd=str(tmp_path / "proj")) for r in run(2)]
    evs = _listing(("Read", "Write"), spawns=10)
    more, more_agents = _coder_runs(("Write",), session="s7", project=other)
    evs += _listing(("Read", "Write"), spawns=3, session="s7")
    f = only(_checks(index(reqs + more, evs, agents + more_agents), [folder]), "agent_tools_unused")
    assert f.change is not None and f.change.splitlines()[-1] == "tools: Read"
    assert f.metrics["spawns"] == 10


def test_the_user_agents_folder_is_user_level_even_for_a_home_project(tmp_path: Path) -> None:
    home = tmp_path / "home"
    folder = home / ".claude" / "agents"
    folder.mkdir(parents=True)
    (folder / "coder.md").write_text("---\nname: coder\n---\n")
    reqs, agents = _coder_runs(("Read",), spawns=10)
    # a session run from the home folder, whose .claude/agents is the user's folder
    home_reqs = [replace(r, cwd=str(home)) for r in run(2, session="s5", project="-home")]
    evs = _listing(("Read", "Write"), spawns=10)
    idx = index(reqs + home_reqs, evs, agents)
    found = run_checks(idx, TABLE, SINCE, UNTIL, agent_dirs=[folder], claude_root=home / ".claude")
    f = only(found, "agent_tools_unused")
    assert f.metrics["spawns"] == 10


def test_an_agent_using_all_its_tools_is_not_flagged(tmp_path: Path) -> None:
    folder = _definition(tmp_path, "tools: Read, Bash")
    reqs, agents = _coder_runs(("Read", "Bash"))
    assert "agent_tools_unused" not in kinds(_checks(index(reqs, agents=agents), [folder]))


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


def test_the_deferred_tool_placeholder_is_not_a_tool_given(tmp_path: Path) -> None:
    folder = _definition(tmp_path, "model: opus")
    reqs, agents = _coder_runs(("Read",), spawns=10)
    evs = _listing(("Read", "Write", "DeferredToolPlaceholder"), spawns=10)
    f = only(_checks(index(reqs, evs, agents), [folder]), "agent_tools_unused")
    assert [e.label for e in f.evidence] == ["never called: Write"]
    assert f.metrics["tools_given"] == 2
    assert f.change is not None and "DeferredToolPlaceholder" not in f.change


def test_a_called_mcp_tool_keeps_its_whole_server(tmp_path: Path) -> None:
    folder = _definition(tmp_path, "model: opus")
    reqs, agents = _coder_runs(("Read", "mcp__gh__get_issue"), spawns=10)
    evs = _listing(("Read", "Write", "mcp__gh__get_issue", "mcp__gh__list_prs"), spawns=10)
    f = only(_checks(index(reqs, evs, agents), [folder]), "agent_tools_unused")
    assert f.change is not None and f.change.splitlines()[-1] == "tools: Read, mcp__gh"
    assert [e.label for e in f.evidence] == ["never called: Write"]
    # the listing's 2,000 tool tokens over four names: Write alone is priced
    assert f.saving_usd_per_week == pytest.approx(500 * 400 * READ_RATE)


def test_a_project_definition_wins_over_the_user_one_for_its_sessions(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    main = repo / ".claude" / "agents"
    tree = repo / ".worktrees" / "b" / ".claude" / "agents"
    user = tmp_path / "home" / ".claude" / "agents"
    for folder, tools in ((main, "Read, Write"), (tree, "Read, Edit"), (user, "Read, Grep")):
        folder.mkdir(parents=True)
        (folder / "coder.md").write_text(f"---\nname: coder\ntools: {tools}\n---\n")
    # sessions of a worktree of the repository, and of another project
    reqs, agents = _coder_runs(("Read",))
    reqs += [replace(r, cwd=str(repo / ".worktrees" / "b")) for r in run(2)]
    more, more_agents = _coder_runs(("Read",), session="s7", project="-home-user-other")
    idx = index(reqs + more, agents=agents + more_agents)
    found = run_checks(
        idx, TABLE, SINCE, UNTIL, agent_dirs=[tree, main, user], claude_root=user.parent
    )
    by_file = {
        f.advice.split("frontmatter of ")[1].split(" ")[0]: f
        for f in found
        if f.kind == "agent_tools_unused"
    }
    assert set(by_file) == {str(main / "coder.md"), str(user / "coder.md")}
    assert by_file[str(main / "coder.md")].evidence[0].label == "never called: Write"
    assert by_file[str(user / "coder.md")].evidence[0].label == "never called: Grep"
    assert all(f.metrics["spawns"] == 3 for f in by_file.values())


# --------------------------------------------------------------------------- overlaps


def test_agent_tools_unused_overlaps_the_rewrites_only(tmp_path: Path) -> None:
    from capcom.checks.rules import _REREADS, _REWRITES, overlaps
    from tests.checks.test_usage import _tool_wait

    # prefix tokens: rewritten after every gap, but below the line a handoff keeps
    assert set(_REWRITES) <= set(overlaps("agent_tools_unused"))
    assert not set(_REREADS) & set(overlaps("agent_tools_unused"))
    folder = _definition(tmp_path, "tools: Read, Bash, Write")
    reqs, agents = _coder_runs(("Read",))
    wait, evs, wait_agents = _tool_wait(12, agent_id="w1")
    found = _checks(index(reqs + wait, evs, agents + wait_agents), [folder])
    coder = only(found, "agent_tools_unused")
    expiry = only(found, "subagent_cache_expiry")
    assert "subagent_cache_expiry" in coder.metrics["overlaps"]
    assert "agent_tools_unused" in expiry.metrics["overlaps"]


def test_prefix_items_do_not_overlap_the_rereads() -> None:
    reqs = run(100, cache_read=200_000)
    found = _checks(index(reqs, _connector_events()))
    # connectors sit at the start of the prompt, below the line a handoff keeps:
    # they share no dollars with the rereads above it
    f = only(found, "claudeai_connectors_unused")
    assert f.metrics["overlaps"] == ()
    lr = only(found, "long_running_agents")
    assert lr.metrics["overlaps"] == ()


def test_overlaps_follow_the_claim_types() -> None:
    from capcom.checks.rules import _PREFIX, _REREADS, _REWRITES, _TAIL, overlaps

    for a in _REREADS:  # (a) x (a) overlap, (a) x (b) never
        assert set(_REREADS) - {a} <= set(overlaps(a))
        assert not set(_REWRITES) & set(overlaps(a))
    # each request is in one rewrite at most; the cache-lifetime comparison reprices
    # every wait, so it shares the idle and late-resume rewrites' dollars
    waits = {"idle_cache_rewrites", "subagent_resumed_late"}
    for b in _REWRITES:
        lifetime = "subagent_cache_expiry"
        shared = waits if b == lifetime else {lifetime} if b in waits else set()
        assert set(_REWRITES) & set(overlaps(b)) == shared
        assert set(_PREFIX) | set(_TAIL) <= set(overlaps(b))
    for t in _TAIL:
        assert set(_REREADS) | set(_REWRITES) <= set(overlaps(t))
    assert "model_mix" in overlaps("long_running_agents")


def test_type_folders_are_where_each_agent_type_ran() -> None:
    from datetime import UTC, datetime

    from capcom.checks.facts import Facts
    from capcom.checks.rules import type_folders
    from capcom.types import AgentInfo

    t = datetime(2026, 10, 1, tzinfo=UTC)
    facts = Facts(t, t)
    facts.agents = {
        "a1": AgentInfo("a1", "s1", "coder"),
        "a2": AgentInfo("a2", "s2", "coder"),
        "a3": AgentInfo("a3", "s1"),
    }
    facts.session_project = {"s1": "-home-u-alpha", "s2": "-tmp-claude-1000--home-u-beta-x"}
    got = type_folders(facts)
    assert set(got["coder"]) >= {"-home-u-alpha", "-tmp-claude-1000--home-u-beta-x"}
    assert got["(unknown agent)"] == ("-home-u-alpha",)
