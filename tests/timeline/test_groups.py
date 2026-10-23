"""Grouping keys and labels (synthetic requests only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from capcom.cost.model import price
from capcom.cost.prices import load_prices
from capcom.timeline.groups import (
    CATEGORIES,
    OPENCLAW,
    TEMPORARY,
    Grouper,
    ProjectNames,
    folder_name,
    is_temporary,
    temporary_project,
)
from capcom.types import AgentInfo, Event, EventKind, GroupBy, Request, SessionInfo
from tests.cost.fakes import T0, FakeIndex, req

TABLE = load_prices()
OPENCLAW_ROOT = Path("/home/user/.openclaw")


def grouper(ix: FakeIndex | None = None) -> Grouper:
    return Grouper.from_index(ix or FakeIndex(), TABLE, OPENCLAW_ROOT)


def key(r: Request, by: GroupBy, g: Grouper | None = None) -> tuple[str, str]:
    g = g or grouper()
    members = g.members(r, by, price(r, TABLE))
    assert len(members) == 1
    return members[0][0], members[0][1]


def test_folder_name_matches_claude_code() -> None:
    assert folder_name("/home/user/.openclaw") == "-home-user--openclaw"
    assert folder_name("/home/user/my_proj.v2") == "-home-user-my-proj-v2"


def test_project_groups_temporary_and_openclaw() -> None:
    assert is_temporary("-tmp-claude-1000-abc")
    assert is_temporary("-tmp")
    assert not is_temporary("-tmpfoo")
    assert not is_temporary("-home-user-tmp")
    assert key(req(project="-tmp-scratch-1"), GroupBy.PROJECT) == (TEMPORARY, TEMPORARY)
    assert key(req(project="-home-user--openclaw"), GroupBy.PROJECT) == (OPENCLAW, OPENCLAW)
    assert key(req(project="-home-user--openclaw-workspace-x"), GroupBy.PROJECT)[0] == OPENCLAW
    assert key(req(project="-home-user--openclawish"), GroupBy.PROJECT)[0] != OPENCLAW
    assert key(req(project="-home-user-proj"), GroupBy.PROJECT) == (
        "-home-user-proj",
        "-home-user-proj",
    )


def test_openclaw_root_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCLAW_STATE_DIR", "/srv/claw.state")
    g = Grouper.from_index(FakeIndex(), TABLE)
    assert g.project("-srv-claw-state-agents")[0] == OPENCLAW


def test_model_is_canonical_and_skips_synthetic() -> None:
    assert key(req(model="claude-haiku-4-5", output=10), GroupBy.MODEL)[0] == (
        "claude-haiku-4-5-20251001"
    )
    assert key(req(model="claude-opus-5-5[1m]", output=10), GroupBy.MODEL)[0] == "claude-opus-5-5"
    synthetic = req(model="<synthetic>")
    assert grouper().members(synthetic, GroupBy.MODEL, price(synthetic, TABLE)) == []


def test_agent_main_type_and_unknown() -> None:
    ix = FakeIndex(agent_infos=[AgentInfo("a1", "s1", agent_type="scout"), AgentInfo("a2", "s1")])
    g = grouper(ix)
    assert key(req(agent=None), GroupBy.AGENT, g) == ("main", "main")
    assert key(req(agent="a1"), GroupBy.AGENT, g) == ("scout", "scout")
    assert key(req(agent="a2"), GroupBy.AGENT, g) == ("unknown", "unknown")
    assert key(req(agent="a9"), GroupBy.AGENT, g) == ("unknown", "unknown")
    # agent ids are looked up within their own session
    assert key(req(session="s2", agent="a1"), GroupBy.AGENT, g)[0] == "unknown"


def test_session_label_is_title_or_short_id() -> None:
    sid = "0123456789abcdef"
    ix = FakeIndex(infos=[SessionInfo("titled", "-p", title="Fix the parser")])
    g = grouper(ix)
    assert key(req(session="titled"), GroupBy.SESSION, g) == ("titled", "Fix the parser")
    assert key(req(session=sid), GroupBy.SESSION, g) == (sid, "01234567")


def test_category_splits_the_cost() -> None:
    r = req(input=1000, cache_read=100_000, cache_write_1h=2000, output=500, web_searches=2)
    cost = price(r, TABLE)
    members = grouper().members(r, GroupBy.CATEGORY, cost)
    assert [(k, label) for k, label, _ in members] == list(CATEGORIES)
    got = {k: v for k, _, v in members}
    assert got == {
        "input": cost.input,
        "cache_read": cost.cache_read,
        "cache_write": cost.cache_write,
        "output": cost.output,
        "web": cost.web,
    }
    assert abs(sum(got.values()) - cost.total) < 1e-12


def test_agent_type_falls_back_to_the_spawning_call() -> None:
    spawn = Event(
        EventKind.SPAWN,
        "s1",
        None,
        T0,
        "k1",
        {"tool_use_id": "tu_9", "agent_type": "builder", "description": "x"},
    )
    other_session = Event(
        EventKind.SPAWN, "s2", None, T0, "k2", {"tool_use_id": "tu_8", "agent_type": "scout"}
    )
    ix = FakeIndex(
        agent_infos=[
            AgentInfo("a1", "s1", tool_use_id="tu_9"),
            AgentInfo("a2", "s1", tool_use_id="tu_8"),
        ],
        evs=[spawn, other_session],
    )
    g = grouper(ix)
    assert key(req(agent="a1"), GroupBy.AGENT, g) == ("builder", "builder")
    assert key(req(agent="a2"), GroupBy.AGENT, g)[0] == "unknown"  # another session's call


SESSION_ID = "0f0e0d0c-0b0a-4908-8706-050403020100"
MAC_TMPDIR = "-var-folders-kx-w0m-dxbs2w78j6s684b84f8h0000gn-T"  # /var/folders/kx/w0m_.../T


@pytest.mark.parametrize(
    ("folder", "project"),
    [
        # Linux: /tmp
        ("-tmp", None),
        ("-tmp-lesson-bench", None),
        (f"-tmp-claude-1000--home-u-Github-toy-{SESSION_ID}-scratchpad", "-home-u-Github-toy"),
        # macOS: /tmp links to /private/tmp, where Claude Code keeps its scratchpads
        ("-private-tmp", None),
        ("-private-tmp-run-1", None),
        (
            f"-private-tmp-claude-501--Users-u-Github-kittex-{SESSION_ID}-scratchpad-run-kittex",
            "-Users-u-Github-kittex",
        ),
        # macOS: $TMPDIR, /var/folders/<2>/<id>/T, a link to /private/var/folders/...
        (MAC_TMPDIR, None),
        (f"-private{MAC_TMPDIR}-build", None),
        (f"{MAC_TMPDIR}-claude-501--Users-u-Github-kittex-{SESSION_ID}", "-Users-u-Github-kittex"),
    ],
)
def test_temporary_roots(folder: str, project: str | None) -> None:
    assert is_temporary(folder)
    assert temporary_project(folder) == project


@pytest.mark.parametrize(
    "folder",
    ["-private", "-private-tmpx", "-var-folders", "-var-folders-kx", "-Users-u-tmp", "-tmpfoo"],
)
def test_not_temporary_roots(folder: str) -> None:
    assert not is_temporary(folder)
    assert temporary_project(folder) is None


def test_macos_scratchpad_is_named_after_its_project() -> None:
    kittex = "-Users-u-Github-kittex"
    scratch = f"-private-tmp-claude-501-{kittex}-{SESSION_ID}-scratchpad-run-kittex"
    cwd = f"/private/tmp/claude-501/{kittex}/{SESSION_ID}/scratchpad/run/kittex"
    sessions = [
        SessionInfo("a", kittex, cwd="/Users/u/Github/kittex"),
        SessionInfo("b", scratch, cwd=cwd),
    ]
    names = ProjectNames.build(sessions, OPENCLAW_ROOT, home=Path("/Users/u"))
    assert names.label(kittex) == "kittex"  # not told apart from the scratchpad by path
    assert names.key(scratch) == f"temporary ({kittex})"
    assert names.label(scratch) == "temporary (kittex)"
    assert key(req(project=scratch), GroupBy.PROJECT) == (TEMPORARY, TEMPORARY)


# --------------------------------------------------------------------------- project names


def test_project_names_rule() -> None:
    from capcom.timeline.groups import ProjectNames
    from capcom.types import SessionInfo

    toy = "-home-user-Github-toy-brain"
    scratch = f"-tmp-claude-1000-{toy}-0f0e0d0c-0b0a-4908-8706-050403020100-scratchpad"
    sessions = [
        # moved into a subfolder: still named after the repo folder
        SessionInfo("a", toy, cwd="/home/user/Github/toy-brain/lewm"),
        SessionInfo("b", "-home-user--openclaw", cwd="/home/user/.openclaw"),
        SessionInfo("c", "-home-user--openclaw-workspace", cwd="/home/user/.openclaw/workspace"),
        SessionInfo("d", "-home-user-x-app", cwd="/home/user/x/app"),
        SessionInfo("e", "-home-user-y-app", cwd="/home/user/y/app"),
        SessionInfo("f", "-home-user-odd", cwd="/elsewhere"),  # no cwd explains it
        SessionInfo("g", scratch, cwd="/tmp/whatever"),
    ]
    names = ProjectNames.build(sessions, OPENCLAW_ROOT, home=Path("/home/user"))
    assert names.label(toy) == "toy-brain"
    assert names.path(toy) == "~/Github/toy-brain"
    assert names.label("-home-user--openclaw") == "openclaw"
    assert names.label("-home-user--openclaw-workspace") == "openclaw (workspace)"
    assert names.label("-home-user--openclaw-agents-x") == "openclaw (agents-x)"  # no cwd
    assert names.key("-home-user--openclaw-workspace") == "-home-user--openclaw-workspace"
    assert names.label("-home-user-x-app") == "~/x/app"  # same last part: by path
    assert names.label("-home-user-y-app") == "~/y/app"
    assert names.label("-home-user-odd") == "-home-user-odd"
    assert names.key(scratch) == f"temporary ({toy})"
    assert names.label(scratch) == names.label(names.key(scratch)) == "temporary (toy-brain)"
    assert names.label("-tmp-x") == "(temporary)"


def test_openclaw_state_dir_and_a_repo_named_openclaw_are_told_apart() -> None:
    from pathlib import Path

    from capcom.timeline.groups import ProjectNames

    names = ProjectNames.build(
        [
            ("-home-u-Github-openclaw", "/home/u/Github/openclaw"),
            ("-home-u--openclaw", None),  # the state dir, no cwd seen
        ],
        openclaw_root=Path("/home/u/.openclaw"),
        home=Path("/home/u"),
    )
    assert names.label("-home-u-Github-openclaw") == "~/Github/openclaw"
    assert names.label("-home-u--openclaw") == "~/.openclaw"
