import io
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from capcom.config import load_config
from capcom.cost.prices import load_prices
from capcom.cost.top import (
    TEMPORARY,
    Row,
    aggregate,
    format_calibration,
    format_top,
    project_label,
    run_calibrate,
    run_top,
    window_start,
)
from capcom.types import AgentInfo, Calibration, CostState, ModelUsage, SessionInfo

from .fakes import T0, FakeIndex, at, req

T = load_prices()
M = 1_000_000


def sample() -> FakeIndex:
    return FakeIndex(
        reqs=[
            req("s1", when=at(0), project="-home-user-a", input=M, cache_read=M, output=M),
            req(
                "s1",
                when=at(1),
                project="-home-user-a",
                agent="ag1",
                model="claude-haiku-5-5",
                output=M,
            ),
            req("s1", when=at(2), project="-home-user-a", agent="ag2", output=1000, estimated=True),
            req("s2", when=at(60 * 30), project="-home-user-b", model="claude-opus-5-6", output=M),
            req("s3", when=at(5), project="-tmp-claude-xyz", model="<synthetic>", output=5),
            req("s4", when=at(6), project="-tmp-other", model="claude-sonnet-5-5", input=M),
        ],
        infos=[SessionInfo("s1", "-home-user-a", title="fix the parser")],
        agent_infos=[AgentInfo("ag1", "s1", agent_type="scout")],
        costs=[CostState("s1", 0, 25.0, {"claude-opus-5-5": ModelUsage(cost_usd=24.5)}, None)],
    )


def rows_by(by: str, idx: FakeIndex | None = None) -> dict[str, Row]:
    idx = idx or sample()
    titles = {s.session_id: s.title for s in idx.infos if s.title}
    types = {a.agent_id: a.agent_type for a in idx.agent_infos if a.agent_type}
    return {r.label: r for r in aggregate(idx.requests(), by, T, titles, types)}


def test_by_project_groups_temporary_folders() -> None:
    rows = rows_by("project")
    assert set(rows) == {"-home-user-a", "-home-user-b", TEMPORARY}
    tmp = rows[TEMPORARY]
    assert tmp.requests == 1  # the <synthetic> reply is not a model call
    a = rows["-home-user-a"]
    assert a.requests == 3
    assert a.usd_input == pytest.approx(4)
    assert a.usd_cache_read == pytest.approx(0.2)
    assert a.total == pytest.approx(4 + 0.2 + 20 + 0.5 + 0.02)
    assert a.flags == "~"
    assert rows["-home-user-b"].flags == "?"


def test_by_session_uses_titles() -> None:
    rows = rows_by("session")
    assert "fix the parser" in rows and "s2" in rows


def test_by_agent_type() -> None:
    rows = rows_by("agent")
    assert set(rows) == {"main", "scout", "(unknown agent)"}
    assert rows["main"].requests == 3  # <synthetic> left out


def test_by_model_and_day() -> None:
    assert set(rows_by("model")) == {
        "claude-opus-5-5",
        "claude-haiku-5-5",
        "claude-opus-5-6",
        "claude-sonnet-5-5",
    }


def test_days_are_local(monkeypatch: pytest.MonkeyPatch) -> None:
    set_tz(monkeypatch, "UTC")
    assert list(rows_by("day")) == ["2026-10-21", "2026-10-20"]  # newest first
    set_tz(monkeypatch, "Pacific/Kiritimati")  # UTC+14: s2 (15:00 UTC on the 21st) is the 22nd
    assert list(rows_by("day")) == ["2026-10-22", "2026-10-20"]


def set_tz(monkeypatch: pytest.MonkeyPatch, tz: str) -> None:
    monkeypatch.setenv("TZ", tz)
    time.tzset()


@pytest.fixture(autouse=True)
def _restore_tz() -> Iterator[None]:
    yield
    time.tzset()  # monkeypatch has restored TZ by now


def test_rows_are_sorted_by_total() -> None:
    rows = aggregate(sample().requests(), "project", T)
    totals = [r.total for r in rows]
    assert totals == sorted(totals, reverse=True)


def test_unknown_grouping() -> None:
    with pytest.raises(ValueError):
        aggregate(sample().requests(), "color", T)


def test_format_top_limits_and_totals() -> None:
    text = format_top(aggregate(sample().requests(), "model", T), "model", limit=2)
    assert "all (4 models)" in text
    assert "2 more rows" in text
    assert "~ 20% of requests (1) have output estimated" in text
    assert "output totals are lower bounds" in text
    assert "? 1 requests of models the price table lacks" in text
    assert "!" not in text  # the <synthetic> reply is not a model call


def test_unpriced_models_are_named_apart() -> None:
    idx = FakeIndex(
        reqs=[
            req("s1", when=at(0), model="gpt-x", output=M),
            req("s1", when=at(1), model="claude-opus-5-6", output=M),
        ]
    )
    rows = aggregate(idx.requests(), "session", T)
    assert (rows[0].inexact_price, rows[0].unpriced, rows[0].flags) == (1, 1, "?!")
    text = format_top(rows, "session", limit=0)
    assert "! 1 requests of models with no price, counted at $0: gpt-x" in text
    assert "? 1 requests of models the price table lacks" in text


@pytest.mark.parametrize(
    ("folder", "label"),
    [
        ("-home-user-proj", "-home-user-proj"),
        ("-tmp-lesson-bench-judge-AbC123", TEMPORARY),
        (
            "-tmp-claude-1000--home-user-Github-kit-tex-"
            "0123abcd-0123-4567-89ab-0123456789ab-scratchpad-demo",
            "temporary (-home-user-Github-kit-tex)",
        ),
        (
            "-tmp-claude-1000--home-user-proj--worktrees-feat-x-"
            "0123abcd-0123-4567-89ab-0123456789ab",
            "temporary (-home-user-proj--worktrees-feat-x)",
        ),
        ("-tmp-claude-1000--home-user-proj", TEMPORARY),  # no session id after it
        (
            "-private-tmp-claude-501--Users-u-Github-kittex-"
            "0123abcd-0123-4567-89ab-0123456789ab-scratchpad-run-kittex",
            "temporary (-Users-u-Github-kittex)",
        ),
        ("-var-folders-kx-w0m-abc123-T-build", TEMPORARY),
    ],
)
def test_project_label(folder: str, label: str) -> None:
    assert project_label(folder) == label


def test_run_top_prints_a_table() -> None:
    out = io.StringIO()
    cfg = load_config({"HOME": "/nonexistent"})
    assert run_top(cfg, "agent", days=0, limit=10, index=sample(), out=out) == 0
    text = out.getvalue()
    assert "scout" in text and "main" in text and "$" in text


def test_run_top_window(monkeypatch: pytest.MonkeyPatch) -> None:
    set_tz(monkeypatch, "UTC")
    out = io.StringIO()
    cfg = load_config({"HOME": "/nonexistent"})
    now = T0.replace(day=21, hour=12)
    run_top(cfg, "session", days=1, limit=10, index=sample(), out=out, now=now)
    text = out.getvalue()
    assert "s2" in text and "fix the parser" not in text
    assert "local time, since 2026-10-21 00:00" in text


def test_window_start_is_local_midnight(monkeypatch: pytest.MonkeyPatch) -> None:
    set_tz(monkeypatch, "Etc/GMT-2")  # UTC+2
    now = datetime(2026, 10, 21, 23, 30, tzinfo=UTC)  # 01:30 on the 22nd, local
    assert window_start(2, now) == datetime(2026, 10, 20, 22, 0, tzinfo=UTC)
    assert window_start(0, now) is None


def test_run_top_without_an_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = load_config(env)
    assert run_top(cfg, "project", days=14, limit=5) == 1
    assert run_calibrate(cfg, limit=5) == 1
    assert "run `capcom scan` first" in capsys.readouterr().err


def test_run_top_with_a_broken_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = load_config(env)
    cfg.index_path.parent.mkdir(parents=True)
    cfg.index_path.write_bytes(b"not a database, just synthetic bytes" * 100)
    assert run_top(cfg, "model", days=0, limit=5) == 1
    assert "run `capcom scan` first" in capsys.readouterr().err


def test_run_calibrate() -> None:
    out = io.StringIO()
    cfg = load_config({"HOME": "/nonexistent"})
    assert run_calibrate(cfg, limit=5, index=sample(), out=out) == 0
    text = out.getvalue()
    assert "fix the parser" in text
    assert "comparable" in text and "all: 1 sessions" in text
    assert "not comparable: 1 sessions, cost-state $25.00" in text and "no startTime" in text


def test_calibrate_splits_the_non_comparable_dollars_by_reason() -> None:
    checks = [
        Calibration("s1", 9.0, 10.0, comparable=False, reasons=("WebFetch", "away")),
        Calibration("s2", 1.0, 2.0, comparable=False, reasons=("away",)),
        Calibration("s3", 5.0, 5.0),
    ]
    text = format_calibration(checks, {}, limit=0)
    assert "not comparable: 2 sessions, cost-state $12.00" in text
    assert "counts under each" in text
    cells = [ln.split() for ln in text.splitlines()]
    lines = {c[0]: c[1:] for c in cells if len(c) > 1 and c[1].isdigit()}
    assert lines["away"] == ["2", "$12.00", "$10.00", "-16.7%", "1", "$2.00"]
    assert lines["WebFetch"] == ["1", "$10.00", "$9.00", "-10.0%", "0", "$0.00"]
