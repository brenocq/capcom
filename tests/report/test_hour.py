"""/api/hour (one clock hour's breakdown) and the heatmap's hour-by-hour list in
/api/spend, on the synthetic index of conftest."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from capcom.report.server import Site
from capcom.types import (
    CauseKind,
    Confidence,
    Explanation,
    Index,
    PriceTable,
    Spike,
    SpikeKind,
)
from tests.report.conftest import FAKE, NOW, S1_AGENT, S1_MAIN, TABLE, Client, serve, usd

H9 = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)  # s1's busy hour: 09:00 to 10:00 UTC
IN_H9 = S1_MAIN[:3] + S1_AGENT


def close(a: float, b: float) -> bool:
    return abs(a - b) < 1e-6


def _spike(start: datetime, minutes: int, cost: float, sid: str = "s1") -> Spike:
    kind = next(iter(SpikeKind))
    end = start + timedelta(minutes=minutes)
    return Spike(kind, start, end, cost, cost / 4, session_id=sid, message_ids=("m",))


# One spike ends before 09:00, one starts in the hour before and runs into it, one
# starts inside it, one starts at 10:00 (the next hour).
BEFORE = _spike(H9 - timedelta(hours=2), 30, 4.0)
ACROSS = _spike(H9 - timedelta(minutes=30), 45, 8.0)
INSIDE = _spike(H9 + timedelta(minutes=40), 10, 2.0)
AFTER = _spike(H9 + timedelta(hours=1), 10, 5.0)


def fake_spikes(
    index: Index, table: PriceTable, since: datetime, until: datetime, sid: str | None
) -> list[Spike]:
    return [sp for sp in (BEFORE, ACROSS, INSIDE, AFTER) if since <= sp.start < until]


def fake_explain(index: Index, table: PriceTable, spikes: Sequence[Spike]) -> list[Explanation]:
    out = []
    for sp in spikes:
        out.append(
            Explanation(
                sp,
                CauseKind.IDLE_REWRITE,
                Confidence.CLEAR,
                sp.excess_usd / 2,
                "Rebuilt after idle",
            )
        )
        out.append(
            Explanation(sp, CauseKind.UNEXPLAINED, Confidence.UNEXPLAINED, sp.excess_usd / 2, "")
        )
    return out


@pytest.fixture(scope="module")
def spiky(index_path: Path) -> Iterator[Client]:
    analysis = replace(FAKE, find_spikes_fn=fake_spikes, explain_fn=fake_explain)
    yield from serve(Site(index_path, TABLE, analysis, clock=lambda: NOW, tz=UTC))


@pytest.fixture(scope="module")
def berlin(index_path: Path) -> Iterator[Client]:
    yield from serve(Site(index_path, TABLE, FAKE, clock=lambda: NOW, tz=ZoneInfo("Europe/Berlin")))


def hour(client: Client, start: str) -> tuple[int, Any]:
    status, data, _ = client.get("/api/hour?start=" + start.replace("+", "%2B"))
    return status, data


def test_an_hours_breakdown_sums_to_its_total(client: Client) -> None:
    status, d = hour(client, H9.isoformat())
    assert status == 200
    assert d["window"] == {
        "start": "2026-10-21T09:00:00+00:00",
        "end": "2026-10-21T10:00:00+00:00",
        "until": "2026-10-21T10:00:00+00:00",
    }
    assert d["partial"] is False
    total = d["totals"]["usd"]
    assert close(total, usd(IN_H9)) and d["totals"]["requests"] == len(IN_H9)
    assert close(sum(p["usd"] for p in d["by_project"]), total)
    assert close(sum(a["usd"] for a in d["by_agent"]), total)
    assert close(sum(c["usd"] for c in d["totals"]["categories"].values()), total)
    assert {a["key"] for a in d["by_agent"]} == {"main", "scout"}
    [session] = d["sessions"]
    assert session["session_id"] == "s1" and close(session["usd"], total)


def test_an_hour_matches_the_heatmap_hour(client: Client) -> None:
    heat = client.get("/api/spend?days=7")[1]["heatmap"]
    by_start = {datetime.fromisoformat(x["start"]): x for x in heat["hours"]}
    for start, x in by_start.items():
        d = hour(client, x["start"])[1]
        assert close(d["totals"]["usd"], x["usd"]), start
    assert close(sum(x["usd"] for x in heat["hours"]), sum(map(sum, heat["cells"])))
    h9 = by_start[H9]
    assert h9["project"] == "alpha" and close(h9["usd"], usd(IN_H9))
    assert heat["top_projects"][H9.weekday()][9] == "alpha"


def test_the_spikes_overlapping_an_hour_and_their_causes(spiky: Client) -> None:
    d = hour(spiky, H9.isoformat())[1]
    keys = [(s["start"], s["excess_usd"]) for s in d["spikes"]]
    # largest excess first; the spike that ended before and the one after are out
    assert keys == [
        (ACROSS.start.isoformat(), ACROSS.excess_usd),
        (INSIDE.start.isoformat(), INSIDE.excess_usd),
    ]
    assert all(s["explanations"] for s in d["spikes"])
    [cause] = d["causes"]  # the unexplained rest is not a cause
    assert cause["cause"] == "idle_rewrite" and cause["spikes"] == 2
    assert close(cause["excess_usd"], (ACROSS.excess_usd + INSIDE.excess_usd) / 2)


def test_the_current_hour_runs_up_to_now(client: Client) -> None:
    d = hour(client, "2026-10-21T14:00:00+00:00")[1]
    assert d["partial"] is False and close(d["totals"]["usd"], usd(S1_MAIN[3:]))
    now_hour = NOW.replace(minute=0)
    status, d = hour(client, now_hour.isoformat())
    assert status == 200 and d["partial"] is True and d["window"]["until"] == NOW.isoformat()


@pytest.mark.parametrize(
    ("start", "code"),
    [
        ("", 400),
        ("yesterday", 400),
        ("2026-10-21T09:30:00+00:00", 400),  # not the start of an hour
        ("2026-10-21T16:00:00+00:00", 404),  # not yet
    ],
)
def test_bad_hours(client: Client, start: str, code: int) -> None:
    assert hour(client, start)[0] == code


def test_a_plus_left_unescaped_and_a_naive_local_time(client: Client) -> None:
    status, d, _ = client.get("/api/hour?start=2026-10-21T09:00:00+00:00")  # "+" reads as " "
    assert status == 200 and close(d["totals"]["usd"], usd(IN_H9))
    status, d = hour(client, "2026-10-21T09:00:00")  # the server's local zone (UTC here)
    assert status == 200 and d["window"]["start"] == "2026-10-21T09:00:00+00:00"


def test_hours_in_a_local_zone(berlin: Client) -> None:
    heat = berlin.get("/api/spend?days=7")[1]["heatmap"]
    starts = [x["start"] for x in heat["hours"]]
    assert "2026-10-21T11:00:00+02:00" in starts  # 09:00 UTC, before Berlin leaves summer time
    assert all(s.endswith(":00:00+02:00") for s in starts)
    status, d = hour(berlin, "2026-10-21T11:00:00+02:00")
    assert status == 200 and close(d["totals"]["usd"], usd(IN_H9))
    assert hour(berlin, "2026-10-21T11:00:00Z")[0] == 200  # 13:00 local
    assert hour(berlin, "2026-10-21T11:30:00+02:00")[0] == 400
