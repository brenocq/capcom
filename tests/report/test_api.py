"""Each API endpoint on the synthetic index: status codes, JSON shape, numbers."""

from __future__ import annotations

import json

import pytest

from tests.report.conftest import (
    COUNTED,
    NOW,
    S1_AGENT,
    S1_MAIN,
    S2,
    S3,
    Client,
    usd,
)

TODAY = S1_MAIN + S1_AGENT
WEEK = TODAY + S2 + S3


def close(a: float, b: float) -> bool:
    return abs(a - b) < 1e-5


def test_status(client: Client) -> None:
    status, data, _ = client.get("/api/status")
    assert status == 200
    assert data["sessions"] == 4
    assert data["index"]["size_bytes"] > 0
    assert data["prices_checked"]


def test_overview_totals(client: Client) -> None:
    status, data, _ = client.get("/api/overview")
    assert status == 200
    today, week = data["today"], data["week"]
    assert close(today["totals"]["usd"], usd(TODAY))
    assert today["totals"]["requests"] == len(TODAY)  # the <synthetic> reply is left out
    assert close(week["totals"]["usd"], usd(WEEK))
    assert close(data["previous_7_days"]["daily_average_usd"], usd(S1_MAIN[:0] + S2 + S3) / 7)
    assert today["sessions"] == 1 and week["sessions"] == 3
    cats = today["totals"]["categories"]
    assert sum(c["usd"] for c in cats.values()) == pytest.approx(today["totals"]["usd"])
    assert cats["cache_read"]["tokens"] == sum(r.usage.cache_read for r in TODAY)
    # by project: alpha by its cwd name, the /tmp folder grouped
    projects = {p["label"]: p for p in week["by_project"]}
    assert set(projects) == {"alpha", "beta", "(temporary)"}
    assert close(projects["alpha"]["usd"], usd(TODAY))
    agents = {a["key"]: a for a in today["by_agent"]}
    assert close(agents["scout"]["usd"], usd(S1_AGENT))
    assert agents["scout"]["count"] == 1 and agents["main"]["count"] == 1
    assert today["subagents"]["count"] == 1
    assert today["subagents"]["two_tool_calls_or_fewer"] == 1
    assert close(data["spending_now"]["usd_last_hour"], usd(S1_MAIN[-1:]))
    assert data["running_sessions"] == 1
    assert data["limits"]["available"] is False
    assert "capcom limits --install" in data["limits"]["message"]
    assert data["unavailable"] == []


def test_overview_estimated_output_share(client: Client) -> None:
    week = client.get("/api/overview")[1]["week"]["totals"]
    est = week["estimated_output"]
    assert est["requests"] == 1
    assert est["tokens"] == S3[0].usage.output
    out = sum(r.usage.output for r in WEEK)
    assert est["share"] == pytest.approx(S3[0].usage.output / out, abs=1e-4)


def test_overview_cumulative_today(client: Client) -> None:
    cum = client.get("/api/overview")[1]["cumulative_today"]
    assert cum["step_s"] == 300
    n = len(cum["times"])
    assert n == 15 * 12  # midnight to 15:00 in 5-minute steps
    assert all(len(v) == n for v in cum["values"])
    assert cum["total"][-1] == pytest.approx(usd(TODAY), abs=1e-5)
    keys = [m["key"] for m in cum["members"]]
    assert keys[0] == max(keys, key=lambda k: dict(zip(keys, cum["values"], strict=True))[k][-1])
    assert cum["times"][-1] == int(NOW.timestamp() * 1000)


def test_overview_without_timeline(stub_client: Client) -> None:
    status, data, _ = stub_client.get("/api/overview")
    assert status == 200
    assert data["cumulative_today"] is None
    assert data["spending_now"]["spark"] is None
    assert data["unavailable"] == ["timeline"]
    assert close(data["today"]["totals"]["usd"], usd(TODAY))


@pytest.mark.parametrize("group", ["category", "project", "model", "agent", "session"])
def test_spend_groups(client: Client, group: str) -> None:
    status, data, _ = client.get(f"/api/spend?days=14&group={group}")
    assert status == 200
    assert data["window"]["group"] == group
    total = usd(COUNTED)
    assert close(data["totals"]["usd"], total)
    cum = data["cumulative"]
    assert cum["step_s"] == 3600
    assert cum["total"][-1] == pytest.approx(total, abs=1e-5)
    assert len(cum["members"]) <= 6
    daily = data["daily"]
    assert len(daily["days"]) == 14
    assert daily["days"][-1]["date"] == "2026-10-21"
    assert sum(d["total"] for d in daily["days"]) == pytest.approx(total, abs=1e-5)
    assert close(daily["days"][-1]["total"], usd(TODAY))
    heat = data["heatmap"]
    assert len(heat["cells"]) == 7 and all(len(r) == 24 for r in heat["cells"])
    assert sum(map(sum, heat["cells"])) == pytest.approx(total, abs=1e-5)
    # today (Wednesday) at 09:00 UTC holds the morning of s1
    assert heat["cells"][2][9] > 0


def test_spend_sessions_and_buckets(client: Client) -> None:
    data = client.get("/api/spend?days=7")[1]
    assert data["sessions_count"] == 2 and data["temporary_sessions"] == 1
    rows = data["sessions"]
    assert rows[0]["session_id"] == max(
        ["s1", "s2", "s3"], key=lambda s: usd([r for r in WEEK if r.session_id == s])
    )
    s1 = next(r for r in rows if r["session_id"] == "s1")
    assert s1["title"] == "Alpha work" and s1["project"] == "alpha"
    assert s1["subagents"] == 1
    assert s1["peak_context"] == 60_000
    # gaps under 5 minutes: 2 + 1 + 1 minutes between the morning requests
    assert s1["active_seconds"] == 4 * 60
    buckets = data["context_buckets"]
    assert sum(b["requests"] for b in buckets) == len(WEEK)
    assert sum(b["usd"] for b in buckets) == pytest.approx(usd(WEEK), abs=1e-5)


@pytest.mark.parametrize("query", ["days=5", "days=x", "group=nope", "days=0"])
def test_spend_bad_query(client: Client, query: str) -> None:
    status, data, _ = client.get(f"/api/spend?{query}")
    assert status == 400
    assert data["error"] == "bad_query"


def test_spend_without_timeline(stub_client: Client) -> None:
    data = stub_client.get("/api/spend")[1]
    assert data["cumulative"] is None and data["daily"] is None and data["heatmap"] is None
    assert data["unavailable"] == ["timeline"]
    assert close(data["totals"]["usd"], usd(COUNTED))


def test_sessions_list(client: Client) -> None:
    status, data, _ = client.get("/api/sessions?days=0")
    assert status == 200
    assert data["total"] == 4
    assert [r["usd"] for r in data["sessions"]] == sorted(
        (r["usd"] for r in data["sessions"]), reverse=True
    )
    found = client.get("/api/sessions?days=0&q=scout")[1]["sessions"]
    assert [r["session_id"] for r in found] == ["s1"]
    found = client.get("/api/sessions?days=0&q=beta")[1]["sessions"]
    assert {r["session_id"] for r in found} == {"s3", "s4"}
    last = client.get("/api/sessions?days=0&sort=last&limit=1")[1]["sessions"]
    assert [r["session_id"] for r in last] == ["s1"]
    assert client.get("/api/sessions?days=3")[1]["total"] == 2  # s1, s2
    assert client.get("/api/sessions?sort=title")[0] == 400


def test_session_detail(client: Client) -> None:
    status, data, _ = client.get("/api/session/s1")
    assert status == 200
    s = data["session"]
    assert s["title"] == "Alpha work" and s["project"] == "alpha"
    assert s["running"] is True
    assert s["models"] == ["claude-opus-5-5"]
    assert close(data["totals"]["usd"], usd(TODAY))
    cols = data["requests"]
    assert len(cols["t"]) == len(TODAY)
    assert all(len(v) == len(TODAY) for v in cols.values())
    assert cols["t"] == sorted(cols["t"])
    assert cols["agent"].count(0) == 2 and cols["agent"].count(-1) == 4
    assert max(cols["context"]) == 60_000
    assert sum(cols["usd"]) == pytest.approx(usd(TODAY), abs=1e-5)
    [agent] = data["agents"]
    assert agent["agent_type"] == "scout" and agent["requests"] == 2
    assert agent["tool_calls"] == 1 and agent["finished"] is True
    types = {t["agent_type"]: t for t in data["agent_types"]}
    assert types["scout"]["spawns"] == 1
    assert sum(t["share"] for t in types.values()) == pytest.approx(1.0, abs=1e-3)
    assert data["main"]["requests"] == 4
    # two pauses: 26 minutes in the morning, then about five and a half hours
    assert [round(p["minutes"]) for p in data["pauses"]] == [26, 327]
    assert data["pauses"][0]["next_cache_write"] == 5_000
    [comp] = data["compactions"]
    assert comp["pre_tokens"] == 180_000 and comp["trigger"] == "auto"
    cal = data["calibration"]
    assert cal["claude_code_usd"] == pytest.approx(usd(TODAY), abs=1e-5)
    assert cal["capcom_usd"] == pytest.approx(usd(TODAY), abs=1e-5)


def test_spawn_description_labels_only_its_agent(client: Client) -> None:
    data = client.get("/api/session/s1")[1]
    [agent] = data["agents"]
    assert agent["description"] == "Map the parser"
    assert json.dumps(data).count("Map the parser") == 1
    for path in ("overview", "spend", "sessions", "live", "spikes", "checks", "limits"):
        assert "Map the parser" not in json.dumps(client.get(f"/api/{path}")[1]), path


def test_session_not_found(client: Client) -> None:
    status, data, _ = client.get("/api/session/nope")
    assert status == 404 and data["error"] == "not_found"


def test_live(client: Client) -> None:
    status, data, _ = client.get("/api/live")
    assert status == 200
    assert data["window_s"] == 600
    [s] = data["sessions"]
    assert s["session_id"] == "s1" and s["title"] == "Alpha work"
    assert close(s["usd_last_10_min"], usd(S1_MAIN[-1:]))
    assert s["usd_per_hour"] == pytest.approx(6 * s["usd_last_10_min"], abs=1e-5)


def test_spikes_and_checks(client: Client, stub_client: Client) -> None:
    status, data, _ = client.get("/api/spikes")
    assert status == 200 and data["spikes"] == []
    status, data, _ = client.get("/api/checks")
    assert status == 200 and data["findings"][0]["saving_usd_per_week"] == 3.5
    status, data, _ = stub_client.get("/api/spikes")
    assert status == 501 and data["milestone"] == 3 and data["module"] == "timeline"
    status, data, _ = stub_client.get("/api/checks")
    assert status == 501 and data["milestone"] == 4
