"""/api/limits and the Overview's limit cards, on the website tests' synthetic index
with synthetic samples written in a state folder of their own."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import UTC, timedelta
from pathlib import Path

import pytest

from capcom.limits.model import NOT_ENOUGH, sample_json
from capcom.limits.view import NOT_RECORDING
from capcom.report.server import Site
from capcom.types import LimitSample, LimitWindow
from tests.report.conftest import (
    FAKE,
    NOW,
    S1_AGENT,
    S1_MAIN,
    S2,
    S3,
    TABLE,
    Client,
    ago,
    build_index,
    serve,
    usd,
)

H5, D7 = LimitWindow.FIVE_HOUR, LimitWindow.SEVEN_DAY


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_index(tmp_path_factory.mktemp("limits-index") / "index.sqlite")


@pytest.fixture
def cache(built: Path, tmp_path: Path) -> Path:
    folder = tmp_path / "capcom"
    folder.mkdir()
    shutil.copy(built, folder / "index.sqlite")
    return folder


@pytest.fixture
def state(tmp_path: Path) -> Path:
    folder = tmp_path / "state"
    folder.mkdir()
    return folder


@pytest.fixture
def client(cache: Path, state: Path) -> Iterator[Client]:
    site = Site(cache / "index.sqlite", TABLE, FAKE, clock=lambda: NOW, tz=UTC, state_dir=state)
    yield from serve(site)


def write(state: Path, samples: list[LimitSample]) -> None:
    with (state / "limits.jsonl").open("a") as f:
        for s in samples:
            f.write(sample_json(s) + "\n")


def samples() -> list[LimitSample]:
    week_reset = NOW + timedelta(hours=12)
    five_reset = NOW + timedelta(hours=1)
    return [
        LimitSample(ago(days=8), D7, 100.0, None, "s9", "rate_limit_error"),  # an old window
        LimitSample(ago(hours=7), D7, 30.0, week_reset, "s1"),
        LimitSample(ago(hours=6), D7, 33.0, week_reset, "s1"),
        LimitSample(ago(hours=5, minutes=30), D7, 36.0, week_reset, "s1"),
        LimitSample(ago(hours=5), D7, 40.0, week_reset, "s1"),
        LimitSample(ago(hours=3), D7, 42.0, week_reset, "s1"),
        LimitSample(ago(hours=1), D7, 44.0, week_reset, "s1"),
        LimitSample(ago(minutes=1), D7, 45.0, week_reset, "s1"),
        LimitSample(ago(hours=4), H5, 5.0, five_reset, "s1"),
        LimitSample(ago(minutes=2), H5, 9.0, five_reset, "s1"),
    ]


def test_empty_state(client: Client) -> None:
    status, data, _ = client.get("/api/limits")
    assert status == 200
    assert data["available"] is False and data["recording"] is False
    assert data["message"] == NOT_RECORDING and "capcom limits --install" in data["message"]
    assert data["windows"] == {"five_hour": None, "seven_day": None}
    _, ov, _ = client.get("/api/overview")
    assert ov["limits"]["available"] is False and ov["limits"]["message"] == NOT_RECORDING


WEEK_SPEND = S1_MAIN + S1_AGENT + S2 + S3  # the newest weekly window's requests


@pytest.mark.parametrize(
    ("group", "members"),
    [
        ("category", None),  # the four kinds, web tools counted with output
        ("project", {"alpha": usd(S1_MAIN + S1_AGENT), "(temporary)": usd(S2), "beta": usd(S3)}),
        ("model", {"claude-opus-5-5": usd(WEEK_SPEND)}),
        ("agent", {"main": usd(S1_MAIN + S2 + S3), "scout": usd(S1_AGENT)}),
    ],
)
def test_each_window_splits_its_spend_by_group(
    state: Path, client: Client, group: str, members: dict[str, float] | None
) -> None:
    write(state, samples())
    status, data, _ = client.get(f"/api/limits?days=14&group={group}")
    assert status == 200 and data["group"] == group
    by_project = client.get("/api/limits?days=14&group=project")[1]
    assert by_project["group"] == "project"  # its own reply, not the one cached before
    project = by_project["windows"]["seven_day"]["segments"][0]
    seg = data["windows"]["seven_day"]["segments"][0]
    got = {r["key"]: r["usd"] for r in seg["by"]}
    if members is None:
        assert set(got) == {"input", "cache_write", "cache_read", "output"}
    else:
        assert got == pytest.approx(members, abs=1e-4)
    # every split adds up to the window's spend and to the same share of the limit
    assert sum(got.values()) == pytest.approx(usd(WEEK_SPEND), abs=1e-3)
    assert seg["usd"] == pytest.approx(usd(WEEK_SPEND), abs=1e-4)
    percent = sum(r["percent"] for r in project["by"])
    assert percent > 0
    assert sum(r["percent"] for r in seg["by"]) == pytest.approx(percent, abs=0.05)


@pytest.mark.parametrize("group", ["session", "nope"])
def test_limits_refuse_a_group_they_cannot_split_by(
    state: Path, client: Client, group: str
) -> None:
    write(state, samples())
    status, err, _ = client.get(f"/api/limits?group={group}")
    assert status == 400 and err["error"] == "bad_query"
    assert err["message"] == "group must be one of: category, project, model, agent"
    # the default is by category, as on Spend; agent_type names the agent split too
    assert client.get("/api/limits")[1]["group"] == "category"
    assert client.get("/api/limits?group=agent_type")[1]["group"] == "agent"


def test_limits_with_samples(state: Path, client: Client) -> None:
    write(state, samples())
    status, data, _ = client.get("/api/limits?days=14&group=project")
    assert status == 200 and data["days"] == 14 and data["group"] == "project"
    assert data["available"] and data["recording"] and data["message"] is None
    week = data["windows"]["seven_day"]
    assert week["reading"]["percent"] == 45.0 and week["current"]
    assert week["fit"]["method"] == "per_usd" and week["fit"]["note"].startswith("About $")
    assert "of your weekly limit, from" in week["fit"]["note"]
    assert data["recording_since"] == ago(hours=7).isoformat(timespec="seconds")
    fc = week["forecast"]
    assert fc is not None and fc["low"] <= fc["median"] <= fc["high"]
    assert fc["median"] >= 45.0 and len(fc["fan"]) > 2
    start = NOW + timedelta(hours=12) - timedelta(days=7)
    assert week["history"][0] == [int(start.timestamp() * 1000), 0.0]
    segs = week["segments"]
    assert len(segs) == 2 and segs[0]["peak"] == 45.0 and segs[1]["hit_limit"]
    projects = {p["key"] for p in segs[0]["by"]}
    assert "alpha" in projects and segs[0]["usd"] > 0
    # two readings, the first at the window's start: one rise, a rough and wide forecast
    five = data["windows"]["five_hour"]
    assert five["reading"]["percent"] == 9.0 and five["reason"] is None
    assert five["fit"]["rough"] and five["fit"]["note"].endswith("from 1 rise (rough).")
    fc = five["forecast"]
    assert fc["rough"] and fc["readings"] == 2 and fc["rate_factor"] >= 2.5
    assert fc["low"] <= fc["median"] <= fc["high"]
    for key in ("runs_out_at", "runs_out_early", "runs_out_late"):
        assert key in fc


def test_one_reading_is_not_enough(state: Path, client: Client) -> None:
    write(state, [LimitSample(ago(hours=1), H5, 5.0, NOW + timedelta(hours=1), "s1")])
    five = client.get("/api/limits")[1]["windows"]["five_hour"]
    assert five["fit"] is None and five["forecast"] is None and five["reason"] == NOT_ENOUGH


def test_overview_cards_follow_new_samples(state: Path, client: Client) -> None:
    _, before, _ = client.get("/api/overview")
    assert before["limits"]["available"] is False
    write(state, samples())  # the recorder writes without a scan: the cache must notice
    _, after, _ = client.get("/api/overview")
    lim = after["limits"]
    assert lim["available"] and lim["windows"]["seven_day"]["forecast"] is not None
    assert "segments" not in lim["windows"]["seven_day"]


def test_bad_days(client: Client) -> None:
    status, data, _ = client.get("/api/limits?days=3")
    assert status == 400 and data["error"] == "bad_query"


def test_limits_page_is_served(client: Client) -> None:
    status, body, _ = client.get("/limits")
    assert status == 200 and b"app.js" in body
