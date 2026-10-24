"""capcom.report.colors: one color per project, agent type and model (/api/colors).
Every entity here is made up."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capcom.checks.agents import find_colors
from capcom.report import api
from capcom.report.colors import (
    CLAUDE,
    GENERAL,
    MAIN_COLOR,
    NEAR,
    SHADE_APART,
    SHADES,
    Entity,
    assign,
    colors_json,
    contrast,
    delta_e,
    epoch_day,
    oklab,
    pair_delta,
    project_color,
)
from capcom.store.index import open_index
from capcom.types import SessionInfo
from tests.report.conftest import ALPHA, BETA, FAKE, NOW, TABLE, Client

STYLE = Path(__file__).resolve().parents[2] / "capcom" / "report" / "static" / "style.css"
CARDS = ("#FFFFFF", "#30302E")  # the cards the marks sit on (style.css --card)
DATAVIZ = (  # the dataviz reference palette, light and dark steps, in its order
    ("#2a78d6", "#3987e5"),
    ("#eb6834", "#d95926"),
    ("#1baf7a", "#199e70"),
    ("#eda100", "#c98500"),
    ("#e87ba4", "#d55181"),
    ("#008300", "#008300"),
    ("#6250d6", "#9085e9"),
    ("#e34948", "#e66767"),
)
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def day(n: int, hours: float = 0) -> datetime:
    return T0 + timedelta(days=n, hours=hours)


def ent(key: str, first: int, claude: str | None = None, last: int | None = None) -> Entity:
    """An entity first seen on day `first` of T0, making requests every day to `last`."""
    days = set(range(epoch_day(day(first)), epoch_day(day(first if last is None else last)) + 1))
    return Entity((key,), day(first), claude, days)


# --------------------------------------------------------------------------- the palette


def test_the_claude_colors_are_the_session_dots_in_both_themes() -> None:
    css = STYLE.read_text()
    light, dark = css.split('[data-theme="dark"]', 1)
    for name, (lo, hi) in CLAUDE.items():
        assert re.search(rf"--sc-{name}: {lo};", light), name
        assert re.search(rf"--sc-{name}: {hi};", dark), name
        assert SHADES[name][0] == CLAUDE[name]


def test_every_shade_stays_nearest_its_claude_color_and_apart_from_its_siblings() -> None:
    for name, shades in SHADES.items():
        assert len(shades) >= 2, name  # the color itself and at least one shade
        for theme in (0, 1):
            base = CLAUDE[name][theme]
            for i, shade in enumerate(shades[1:], 1):
                own = delta_e(shade[theme], base)
                others = min(
                    delta_e(shade[theme], c[theme]) for n, c in CLAUDE.items() if n != name
                )
                assert own + 1.5 <= others, (name, i, theme)
                for before in shades[:i]:
                    assert delta_e(shade[theme], before[theme]) >= SHADE_APART, (name, i, theme)


def test_the_claude_colors_and_shades_keep_3_to_1_on_the_cards_of_both_themes() -> None:
    for shades in SHADES.values():
        for pair in shades:
            for theme in (0, 1):
                assert contrast(pair[theme], CARDS[theme]) >= 3.0, pair


def chroma(hex_: str) -> float:
    _, a, b = oklab(hex_)
    return float((a * a + b * b) ** 0.5)


def test_the_general_palette_is_the_dataviz_one_then_four_shades_apart() -> None:
    assert GENERAL[: len(DATAVIZ)] == DATAVIZ
    for extra in GENERAL[len(DATAVIZ) :]:
        assert min(pair_delta(extra, c) for c in DATAVIZ) >= 12
        for theme in (0, 1):
            assert contrast(extra[theme], CARDS[theme]) >= 3.0, extra
            assert chroma(extra[theme]) >= 0.10, extra  # the dataviz floor: not a gray
    assert len(set(GENERAL)) == len(GENERAL)


def test_the_main_thread_color_keeps_away_from_every_other_color() -> None:
    shades = [s for ss in SHADES.values() for s in ss]
    assert min(pair_delta(MAIN_COLOR, c) for c in GENERAL) >= 12
    assert min(pair_delta(MAIN_COLOR, c) for c in shades) >= 11
    for theme in (0, 1):
        assert contrast(MAIN_COLOR[theme], CARDS[theme]) >= 3.0
        assert chroma(MAIN_COLOR[theme]) >= 0.10


# --------------------------------------------------------------------------- assignment


def test_a_claude_color_is_used_as_it_is() -> None:
    got = assign([ent("capcom", 0, "orange"), ent("other", 1)])
    assert got["capcom"] == CLAUDE["orange"]


def test_a_taken_claude_color_gives_a_shade_nearer_it_than_any_other_claude_color() -> None:
    got = assign([ent("first", 0, "purple"), ent("second", 3, "purple"), ent("third", 5, "purple")])
    assert got["first"] == CLAUDE["purple"]
    assert got["second"] == SHADES["purple"][1] and got["third"] == SHADES["purple"][2]
    for key in ("second", "third"):
        for theme in (0, 1):
            own = delta_e(got[key][theme], CLAUDE["purple"][theme])
            assert all(
                own < delta_e(got[key][theme], c[theme]) for n, c in CLAUDE.items() if n != "purple"
            )
        assert pair_delta(got[key], got["first"]) >= SHADE_APART


def test_the_earliest_seen_keeps_the_claude_color_itself() -> None:
    a = assign([ent("late", 9, "blue"), ent("early", 2, "blue")])
    assert a["early"] == CLAUDE["blue"] and a["late"] == SHADES["blue"][1]


def test_an_uncolored_entity_gets_a_general_color_that_does_not_move_when_others_come() -> None:
    old = [ent("alpha", 0), ent("beta", 1), ent("gamma", 2)]
    before = assign(old)
    assert len({before[k] for k in ("alpha", "beta", "gamma")}) == 3
    assert all(c in GENERAL for c in before.values())
    later = assign([*old, ent("delta", 5), ent("epsilon", 6)])
    assert all(later[k] == before[k] for k in before)
    # and the order the entities come in does not matter
    assert assign(list(reversed(old))) == before


def test_an_uncolored_entity_keeps_away_from_the_claude_colors_held() -> None:
    held = [ent("orange-one", 0, "orange"), ent("blue-one", 1, "blue")]
    got = assign([*held, *(ent(f"p{i}", 2 + i) for i in range(4))])
    for i in range(4):
        for h in ("orange-one", "blue-one"):
            assert pair_delta(got[f"p{i}"], got[h]) >= NEAR
    # dataviz blue and orange sit next to Claude's blue and orange: both are skipped
    assert GENERAL[0] not in got.values() and GENERAL[1] not in got.values()


def test_past_the_palette_a_color_is_shared_with_the_entity_idle_longest() -> None:
    # one entity per general color, all busy until day 30 but "quiet" (idle since day 1)
    ents = [
        ent(f"busy{i}", i, last=30) if i else ent("quiet", 0, last=1) for i in range(len(GENERAL))
    ]
    got = assign([*ents, ent("newcomer", 20)])
    assert got["newcomer"] == got["quiet"]


def test_a_project_takes_the_color_most_of_its_recent_colored_sessions_use() -> None:
    def ses(i: int, color: str | None) -> SessionInfo:
        return SessionInfo(f"s{i}", ALPHA, first_seen=day(i), color=color)

    assert project_color([ses(0, None), ses(1, "")]) is None
    assert project_color([ses(0, "red"), ses(1, "blue"), ses(2, "red")]) == "red"
    # a tie goes to the newest
    assert project_color([ses(0, "red"), ses(1, "blue")]) == "blue"
    # only the last ten colored sessions count: an old habit gives way
    many = [ses(i, "green") for i in range(8)] + [ses(10 + i, "pink") for i in range(6)]
    assert project_color(many) == "pink"
    # an unknown name is no Claude Code color
    assert project_color([ses(0, "chartreuse")]) is None


def test_every_key_and_label_of_an_entity_maps_to_one_color() -> None:
    got = colors_json(
        {"project": [Entity(("-home-x-capcom", "capcom"), day(0), "orange"), ent("-home-x-b", 1)]}
    )
    pal = got["palette"]
    assert got["general"] == len(GENERAL) and pal[: len(GENERAL)] == [list(c) for c in GENERAL]
    i = got["project"]["capcom"]
    assert got["project"]["-home-x-capcom"] == i and pal[i] == list(CLAUDE["orange"])
    assert got["project"]["-home-x-b"] < len(GENERAL)


# --------------------------------------------------------------------------- the index


def test_agent_definitions_give_their_color(tmp_path: Path) -> None:
    project, user = tmp_path / "p", tmp_path / "u"
    project.mkdir()
    user.mkdir()
    (project / "scout.md").write_text("---\nname: scout\ncolor: Green\n---\nbody\n")
    (user / "scout.md").write_text("---\nname: scout\ncolor: red\n---\n")
    (user / "plain.md").write_text("---\nname: plain\ntools: Read\n---\n")
    (user / "quoted.md").write_text('---\ndescription: x\ncolor: "blue"\n---\n')
    assert find_colors([project, user]) == {"scout": "green", "quoted": "blue"}


def test_the_endpoint_colors_projects_agents_and_models(index_path: Path, tmp_path: Path) -> None:
    (tmp_path / "scout.md").write_text("---\nname: scout\ncolor: cyan\n---\n")
    ctx = api.Context(
        open_index(index_path, readonly=True), TABLE, FAKE, NOW, tz=UTC, agent_dirs=[tmp_path]
    )
    got = api.colors(ctx)
    pal = got["palette"]
    # alpha's session was colored purple in Claude Code: its key and its label
    assert pal[got["project"][ALPHA]] == list(CLAUDE["purple"])
    assert got["project"]["alpha"] == got["project"][ALPHA]
    assert got["project"][BETA] != got["project"][ALPHA]
    # the merged temporary-folders series has a color of its own
    assert "temporary" in got["project"]
    assert pal[got["agent"]["scout"]] == list(CLAUDE["cyan"])
    assert {"main", "unknown", "(unknown agent)"} <= set(got["agent"])
    # the main thread always has its own color, which no agent type takes
    assert pal[got["agent"]["main"]] == list(MAIN_COLOR)
    assert sum(pal[i] == list(MAIN_COLOR) for i in set(got["agent"].values())) == 1
    assert "claude-opus-5-5" in got["model"] and "<synthetic>" not in got["model"]


def test_the_server_serves_the_colors(client: Client) -> None:
    status, data, _ = client.get("/api/colors")
    assert status == 200
    assert set(data) >= {"palette", "general", "project", "agent", "model"}
