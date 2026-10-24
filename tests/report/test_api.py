"""Each API endpoint on the synthetic index: status codes, JSON shape, numbers."""

from __future__ import annotations

import json
from datetime import UTC

import pytest

from tests.report.conftest import (
    COUNTED,
    NOW,
    S1_AGENT,
    S1_MAIN,
    S2,
    S3,
    S4,
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


@pytest.mark.parametrize("group", ["project", "model", "agent"])
def test_overview_splits_today_by_group(client: Client, group: str) -> None:
    status, data, _ = client.get(f"/api/overview?group={group}")
    assert status == 200
    assert data["group"] == group
    cum = data["cumulative_today"]
    assert len(cum["members"]) <= 6
    assert cum["total"][-1] == pytest.approx(usd(TODAY), abs=1e-5)
    # the rest of the page does not depend on the split
    assert close(data["today"]["totals"]["usd"], usd(TODAY))
    assert client.get("/api/overview")[1]["group"] == "category"
    assert client.get("/api/overview?group=nope")[0] == 400


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


@pytest.mark.parametrize("group", ["category", "project"])
def test_spend_today(client: Client, group: str) -> None:
    status, data, _ = client.get(f"/api/spend?days=1&group={group}")
    assert status == 200
    assert data["window"]["days"] == 1
    assert data["window"]["since"].startswith("2026-10-21T00:00")
    assert close(data["totals"]["usd"], usd(TODAY))
    assert data["cumulative"]["step_s"] == 300
    assert len(data["daily"]["days"]) == 1
    hourly = data["hourly"]
    assert len(hourly["hours"]) == 24
    assert hourly["hours"][0]["start"].startswith("2026-10-21T00:00")
    assert sum(x["total"] for x in hourly["hours"]) == pytest.approx(usd(TODAY), abs=1e-5)
    assert all(len(x["values"]) == len(hourly["members"]) for x in hourly["hours"])
    # the morning of s1, at 09:00 UTC; nothing after now
    assert hourly["hours"][9]["total"] > 0
    now_hour = NOW.astimezone(UTC).hour
    assert all(x["total"] == 0 for x in hourly["hours"][now_hour + 1 :])
    # the longer windows send no hourly bars
    assert client.get("/api/spend?days=7")[1]["hourly"] is None


def test_spend_a_past_day(client: Client) -> None:
    status, data, _ = client.get("/api/spend?day=2026-10-20&group=project")
    assert status == 200
    win = data["window"]
    assert win["scale"] == "day" and win["days"] is None and win["day"] == "2026-10-20"
    assert win["since"].startswith("2026-10-20T00:00") and win["end"].startswith("2026-10-21T00:00")
    assert win["prev"] == "2026-10-19" and win["next"] == "2026-10-21"
    assert win["current"] is False and win["group"] == "project"
    assert close(data["totals"]["usd"], usd(S2))
    assert data["cumulative"]["step_s"] == 300
    assert data["cumulative"]["total"][-1] == pytest.approx(usd(S2), abs=1e-5)
    hours = data["hourly"]["hours"]
    assert len(hours) == 24 and hours[0]["start"].startswith("2026-10-20T00:00")
    assert close(hours[15]["total"], usd(S2))
    assert sum(x["total"] for x in hours) == pytest.approx(usd(S2), abs=1e-5)
    assert data["sessions_count"] == 0 and data["temporary_sessions"] == 1
    # a day has no week to lay out, nor days to compare
    assert data["daily"] is None and data["heatmap"] is None and data["regime"] is None
    assert data["minutes"] is None


def test_spend_today_as_a_day_has_no_next(client: Client) -> None:
    data = client.get("/api/spend?day=2026-10-21")[1]
    assert data["window"]["next"] is None and data["window"]["current"] is True
    assert close(data["totals"]["usd"], usd(TODAY))
    assert sum(x["total"] for x in data["hourly"]["hours"]) == pytest.approx(usd(TODAY), abs=1e-5)
    # another day is its own reply, not a cached one
    assert close(client.get("/api/spend?day=2026-10-18")[1]["totals"]["usd"], usd(S3))


def test_spend_an_hour(client: Client) -> None:
    status, data, _ = client.get("/api/spend?hour=2026-10-21T09")
    assert status == 200
    win = data["window"]
    assert win["scale"] == "hour" and win["hour"] == "2026-10-21T09" and win["day"] == "2026-10-21"
    assert win["prev"] == "2026-10-21T08" and win["next"] == "2026-10-21T10"
    in_hour = S1_MAIN[:3] + S1_AGENT  # 09:00 to 09:30
    assert close(data["totals"]["usd"], usd(in_hour))
    assert data["cumulative"]["step_s"] == 60
    bars = data["minutes"]["hours"]
    assert len(bars) == 12 and bars[0]["start"].startswith("2026-10-21T09:00")
    assert bars[6]["start"].startswith("2026-10-21T09:30")
    assert close(bars[0]["total"], usd(S1_MAIN[:2] + S1_AGENT))
    assert close(bars[6]["total"], usd(S1_MAIN[2:3]))
    assert data["hourly"] is None and data["daily"] is None and data["heatmap"] is None
    assert [r["session_id"] for r in data["sessions"]] == ["s1"]


def test_spend_the_hour_now_has_no_next(client: Client) -> None:
    last = client.get("/api/spend?hour=2026-10-21T14")[1]["window"]
    assert last["next"] == "2026-10-21T15" and last["current"] is False
    data = client.get("/api/spend?hour=2026-10-21T15:00")[1]
    assert data["window"]["next"] is None and data["window"]["current"] is True
    assert data["totals"]["usd"] == 0 and data["sessions"] == []


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("day=2026-13-01", 400),
        ("day=yesterday", 400),
        ("day=2026-10-22", 404),
        ("hour=2026-10-21T25", 400),
        ("hour=2026-10-21T09:30", 400),
        ("hour=2026-10-21", 400),
        ("hour=2026-10-21T16", 404),
        ("day=2026-10-21&hour=2026-10-21T09", 400),
    ],
)
def test_spend_bad_day_or_hour(client: Client, query: str, code: int) -> None:
    assert client.get(f"/api/spend?{query}")[0] == code


def test_typical_hours_are_the_mean_over_the_days_before(client: Client) -> None:
    status, data, _ = client.get("/api/typical-hours?day=2026-10-21")
    assert status == 200
    # the first request is on Oct 11: ten days of history before Oct 21, of 28
    assert data["of_days"] == 28 and data["days"] == 10
    hours = data["hours"]
    assert len(hours) == 24 and hours[0]["start"].startswith("2026-10-21T00:00")
    assert close(hours[15]["usd"], usd(S2 + S3 + S4) / 10)
    assert all(x["usd"] == 0 for i, x in enumerate(hours) if i != 15)
    # the day itself never counts: Oct 20 has only the nine days before it
    data = client.get("/api/typical-hours?day=2026-10-20")[1]
    assert data["days"] == 9 and close(data["hours"][15]["usd"], usd(S3 + S4) / 9)
    first = client.get("/api/typical-hours?day=2026-10-11")[1]
    assert first["days"] == 0 and all(x["usd"] == 0 for x in first["hours"])
    assert client.get("/api/typical-hours?day=2026-10-22")[0] == 404
    assert client.get("/api/typical-hours")[0] == 400


def test_spend_sessions_and_buckets(client: Client) -> None:
    data = client.get("/api/spend?days=7")[1]
    assert data["sessions_count"] == 2 and data["temporary_sessions"] == 1
    rows = data["sessions"]
    assert rows[0]["session_id"] == max(
        ["s1", "s2", "s3"], key=lambda s: usd([r for r in WEEK if r.session_id == s])
    )
    s1 = next(r for r in rows if r["session_id"] == "s1")
    assert s1["title"] == "Alpha work" and s1["project"] == "alpha"
    assert s1["color"] == "purple"
    assert next(r for r in rows if r["session_id"] != "s1")["color"] is None
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
    # search finds a renamed session by its own name, not by Claude Code's title
    found = client.get("/api/sessions?days=0&q=alpha%20work")[1]["sessions"]
    assert [(r["session_id"], r["color"]) for r in found] == [("s1", "purple")]
    assert client.get("/api/sessions?days=0&q=alpha%20draft")[1]["sessions"] == []
    found = client.get("/api/sessions?days=0&q=beta")[1]["sessions"]
    assert {r["session_id"] for r in found} == {"s3", "s4"}
    last = client.get("/api/sessions?days=0&sort=last&limit=1")[1]["sessions"]
    assert [r["session_id"] for r in last] == ["s1"]
    assert client.get("/api/sessions?days=3")[1]["total"] == 2  # s1, s2
    assert client.get("/api/sessions?sort=title")[0] == 400
    assert client.get("/api/sessions?dir=up")[0] == 400


def test_sessions_prompts_and_cost_per_active_hour(client: Client) -> None:
    rows = client.get("/api/sessions?days=0&sort=prompts")[1]["sessions"]
    assert (rows[0]["session_id"], rows[0]["prompts"]) == ("s1", 2)
    # s4's log is gone (never reread since prompts were recorded): unknown, not 0
    assert {r["session_id"]: r["prompts"] for r in rows[1:]} == {"s2": 0, "s3": 0, "s4": None}
    for d in ("asc", "desc"):
        got = client.get(f"/api/sessions?days=0&sort=prompts&dir={d}")[1]["sessions"]
        assert got[-1]["session_id"] == "s4"  # unknown sorts last either way
    for r in rows:
        rate = r["usd_per_active_hour"]
        if r["active_seconds"] < 300:
            assert rate is None  # too short for a rate
        else:
            assert rate == pytest.approx(r["usd"] * 3600 / r["active_seconds"], rel=1e-3)
    # a missing rate sorts last in both directions
    for d in ("asc", "desc"):
        got = client.get(f"/api/sessions?days=0&sort=usd_per_active_hour&dir={d}")[1]["sessions"]
        rates = [r["usd_per_active_hour"] for r in got]
        known = [x for x in rates if x is not None]
        assert rates == known + [None] * (len(rates) - len(known))
        assert known == sorted(known, reverse=d == "desc")


def test_sessions_sort_direction(client: Client) -> None:
    def order(qs: str) -> list[str]:
        return [r["session_id"] for r in client.get("/api/sessions?days=0" + qs)[1]["sessions"]]

    def costs(qs: str) -> list[float]:
        return [r["usd"] for r in client.get("/api/sessions?days=0" + qs)[1]["sessions"]]

    # no dir is descending, as before; asc is the exact reverse when costs differ
    assert order("") == order("&sort=usd&dir=desc")
    assert costs("&sort=usd&dir=asc") == sorted(costs(""))
    assert order("&sort=last&dir=asc") == list(reversed(order("&sort=last")))
    assert order("&sort=last&dir=asc&limit=1") == [order("&sort=last")[-1]]


def test_session_detail(client: Client) -> None:
    status, data, _ = client.get("/api/session/s1")
    assert status == 200
    s = data["session"]
    assert s["title"] == "Alpha work" and s["project"] == "alpha"
    assert s["title_custom"] is True and s["color"] == "purple"
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
    # requests per effort level, the most used first, over the same levels as efforts
    counts = data["session"]["effort_counts"]
    assert sorted(counts) == data["session"]["efforts"]
    assert list(counts.values()) == sorted(counts.values(), reverse=True)
    cal = data["calibration"]
    assert cal["claude_code_usd"] == pytest.approx(usd(TODAY), abs=1e-5)
    assert cal["capcom_usd"] == pytest.approx(usd(TODAY), abs=1e-5)
    # the cost adding up per category: it ends at the totals, never goes down, and
    # keeps both sides of the 327-minute pause (a squeezed one) and the last request
    series = data["cost_series"]
    assert series["t"][-1] == cols["t"][-1] and series["t"] == sorted(series["t"])
    for key, cat in data["totals"]["categories"].items():
        if key in series:
            assert series[key][-1] == pytest.approx(cat["usd"], abs=1e-5)
            assert series[key] == sorted(series[key])
    assert sum(series[k][-1] for k in ("cache_read", "cache_write", "output", "input")) == (
        pytest.approx(usd(TODAY), abs=1e-5)
    )
    gap = next(i for i in range(1, len(cols["t"])) if cols["t"][i] - cols["t"][i - 1] > 3600e3)
    assert {cols["t"][gap - 1], cols["t"][gap]} <= set(series["t"])


def test_day_against_the_days_with_spend_before_it(client: Client) -> None:
    status, d, _ = client.get(f"/api/day/{NOW.date()}")
    assert status == 200 and d["day"] == "2026-10-21" and d["today"] is True
    assert close(d["totals"]["usd"], usd(TODAY)) and d["sessions_count"] == 1
    assert d["window"]["until"] == NOW.isoformat()  # today runs up to now
    assert close(d["cumulative"]["total"][-1], usd(TODAY))
    # the 14 days before hold three days with spend (s2, s3, s4): their median
    typical = d["typical"]
    assert typical["days"] == 3 and typical["of_days"] == 14
    assert close(typical["usd"], sorted([usd(S2), usd(S3), usd(S4)])[1])
    [alpha] = d["by_project"]
    assert alpha["label"] == "alpha" and alpha["typical_usd"] == 0.0
    assert {a["key"] for a in d["by_agent"]} == {"main", "scout"}


@pytest.mark.parametrize(
    ("group", "finals"),
    [
        ("category", None),  # checked below: the four kinds, output with web tools
        ("project", {"-home-user-alpha": usd(TODAY)}),
        ("model", {"claude-opus-5-5": usd(TODAY)}),
        ("agent", {"main": usd(S1_MAIN), "scout": usd(S1_AGENT)}),
    ],
)
def test_day_splits_its_chart_by_group(
    client: Client, group: str, finals: dict[str, float] | None
) -> None:
    status, d, _ = client.get(f"/api/day/{NOW.date()}?group={group}")
    assert status == 200 and d["group"] == group
    cum = d["cumulative"]
    got = {m["key"]: m["final"] for m in cum["members"]}
    if finals is None:
        assert set(got) == {"cache_read", "cache_write", "output", "input"}
    else:
        assert set(got) == set(finals)
        assert all(close(got[k], v) for k, v in finals.items())
    assert close(cum["total"][-1], usd(TODAY))
    # the rest of the page does not depend on the split
    assert close(d["totals"]["usd"], usd(TODAY))
    assert [p["label"] for p in d["by_project"]] == ["alpha"]


def test_day_split_defaults_to_category_and_refuses_an_unknown_group(client: Client) -> None:
    assert client.get(f"/api/day/{NOW.date()}")[1]["group"] == "category"
    status, err, _ = client.get(f"/api/day/{NOW.date()}?group=nope")
    assert status == 400 and err["error"] == "bad_query"
    # a past day splits by its own members: beta alone on s3's day
    past = client.get("/api/day/2026-10-18?group=project")[1]["cumulative"]
    assert [m["label"] for m in past["members"]] == ["beta"]


def test_a_past_day_and_its_projects_typical_day(client: Client) -> None:
    status, d, _ = client.get("/api/day/2026-10-18")  # s3, beta
    assert status == 200 and d["today"] is False
    assert (
        close(d["totals"]["usd"], usd(S3)) and d["window"]["until"] == "2026-10-19T00:00:00+00:00"
    )
    assert d["typical"]["days"] == 1 and close(d["typical"]["usd"], usd(S4))
    [beta] = d["by_project"]
    assert beta["label"] == "beta" and close(beta["typical_usd"], usd(S4))


def test_a_day_without_spend(client: Client) -> None:
    status, d, _ = client.get("/api/day/2026-09-01")
    assert status == 200 and d["totals"]["requests"] == 0
    assert d["typical"] is None and d["by_project"] == [] and d["sessions"] == []


@pytest.mark.parametrize(
    ("path", "code"),
    [("2026-13-01", 400), ("yesterday", 400), ("2026-10-22", 404), ("2026-10-21/x", 404)],
)
def test_day_bad_paths(client: Client, path: str, code: int) -> None:
    assert client.get(f"/api/day/{path}")[0] == code


def test_day_page_is_served(client: Client) -> None:
    status, body, _ = client.get(f"/day/{NOW.date()}")
    assert status == 200 and b'src="/static/app.js"' in body


def test_spawn_description_labels_only_its_agent(client: Client) -> None:
    data = client.get("/api/session/s1")[1]
    [agent] = data["agents"]
    assert agent["description"] == "Map the parser"
    assert json.dumps(data).count("Map the parser") == 1
    for path in (
        "overview",
        "spend",
        "sessions",
        "live",
        "spikes",
        "checks",
        "limits",
        "day/2026-10-21",
    ):
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
    assert s["color"] == "purple"
    assert close(s["usd_last_10_min"], usd(S1_MAIN[-1:]))
    assert s["usd_per_hour"] == pytest.approx(6 * s["usd_last_10_min"], abs=1e-5)
    # the test server never scans: it must say so and how old the index is, so the
    # page doesn't claim that no session ran
    assert data["watching"] is False and data["index_updated"]


def test_spikes_and_checks(client: Client, stub_client: Client) -> None:
    status, data, _ = client.get("/api/spikes")
    assert status == 200 and data["spikes"] == []
    status, data, _ = client.get("/api/checks")
    assert status == 200 and data["findings"][0]["saving_usd_per_week"] == 3.5
    status, data, _ = stub_client.get("/api/spikes")
    assert status == 501 and data["milestone"] == 3 and data["module"] == "timeline"
    status, data, _ = stub_client.get("/api/checks")
    assert status == 501 and data["milestone"] == 4
