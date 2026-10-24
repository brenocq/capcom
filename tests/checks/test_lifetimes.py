"""The cache-lifetime comparison (capcom.checks.lifetimes) and the check built on it:
the 5-minute cache, the 1-hour cache and the 5-minute cache with pings, on
synthetic threads."""

from __future__ import annotations

from datetime import timedelta

import pytest

from capcom.checks import run_checks
from capcom.checks.facts import collect
from capcom.checks.lifetimes import MAIN, MAX_PINGS, SUBAGENT, Planner, Plans
from capcom.checks.rules import _subagent_advice, ping_break_even
from capcom.types import EventKind, Finding, Request
from tests.checks.helpers import SINCE, TABLE, UNTIL, agent, ev, index, kinds, only
from tests.cost.fakes import at, req

K = 100_000  # every prompt here: 100k tokens, read or written


def _rates() -> tuple[float, float, float, float]:
    return Planner(TABLE, SINCE).rates(req(when=at(0)), K)


READ, W5, W1, OUT = _rates()
PING = K * READ + 300 * W5 + 50 * OUT  # one check-in through a wait


def _r(
    minute: float,
    *,
    sub: bool = False,
    tools: tuple[str, ...] = (),
    read: int = 0,
    w5: int = 0,
    w1: int = 0,
) -> Request:
    return req(
        when=at(minute),
        agent="a1" if sub else None,
        tools=tools,
        cache_read=read,
        cache_write_5m=w5,
        cache_write_1h=w1,
    )


def _plans(reqs: list[Request], evs=()) -> dict[str, Plans]:  # type: ignore[no-untyped-def]
    return collect(index(reqs, list(evs), [agent("a1", "coder")]), TABLE, SINCE, UNTIL).cache_plans


def _checks(reqs: list[Request], evs=()) -> list[Finding]:  # type: ignore[no-untyped-def]
    idx = index(reqs, list(evs), [agent("a1", "coder")])
    return run_checks(idx, TABLE, SINCE, UNTIL, agent_dirs=[])


def test_rates_follow_the_price_table() -> None:
    assert pytest.approx(1.6 * W5) == W1 and READ < W5 and OUT > W1  # 2x and 1.25x input


def test_a_5_minute_thread_idle_20_minutes_is_cheapest_on_the_1_hour_cache() -> None:
    p = _plans([_r(1, w5=K), _r(21, w5=K)])[MAIN]
    assert p.current == "5m" and p.requests == 2
    assert p.five_min == pytest.approx(2 * K * W5)
    assert p.one_hour == pytest.approx(K * W1 + K * READ)  # the second request reads
    assert p.pinged == pytest.approx(p.five_min)  # the user's wait: no ping covers it
    assert p.modeled == pytest.approx(p.recorded)
    assert p.gaps == [0, 1] and p.cheapest() == "1h"


def test_a_tool_wait_can_be_pinged_through() -> None:
    reqs = [_r(1, sub=True, tools=("Bash",), w5=K), _r(21, sub=True, w5=K)]
    result = ev(EventKind.TOOL_RESULT, 20.5, agent="a1", tool_use_id="x", tool="Bash")
    p = _plans(reqs, [result])[SUBAGENT]
    # the wait runs from the first request to the second one's start (the result)
    pings = int(19.5 / 4)
    assert p.pings == pings and p.gaps == [1, 0]
    assert p.pinged == pytest.approx(K * W5 + pings * PING + K * READ)
    assert p.cheapest() == "ping"


def test_a_question_to_the_user_cannot_be_pinged() -> None:
    p = _plans([_r(1, tools=("AskUserQuestion",), w5=K), _r(21, w5=K)])[MAIN]
    assert p.gaps == [0, 1] and p.pinged == pytest.approx(p.five_min)


def test_a_1_hour_thread_keeps_its_cache_through_20_minutes() -> None:
    p = _plans([_r(1, w1=K), _r(21, read=K)])[MAIN]
    assert p.current == "1h"
    assert p.one_hour == pytest.approx(K * W1 + K * READ)
    assert p.modeled == pytest.approx(p.recorded)
    assert p.five_min == pytest.approx(2 * K * W5)


def test_a_wait_over_an_hour_expires_every_cache() -> None:
    reqs = [_r(1, tools=("Bash",), w1=K), _r(81, w1=K)]
    p = _plans(reqs, [ev(EventKind.TOOL_RESULT, 80.5, tool_use_id="x", tool="Bash")])[MAIN]
    assert p.long_gaps == [1, 0] and p.pings == MAX_PINGS
    assert p.one_hour == pytest.approx(2 * K * W1)
    assert p.pinged == pytest.approx(2 * K * W5 + MAX_PINGS * PING)


def test_a_compaction_or_a_model_change_breaks_the_prefix() -> None:
    reqs = [_r(1, w1=K), _r(2, w1=K)]
    p = _plans(reqs, [ev(EventKind.COMPACTION, 1.5)])[MAIN]
    assert p.one_hour == pytest.approx(2 * K * W1)


def test_the_lifetime_in_use_is_the_windows_last_write() -> None:
    p = _plans([_r(1, sub=True, w5=K), _r(2, sub=True, read=K), _r(30, sub=True, w1=K)])[SUBAGENT]
    assert p.current == "1h" and p.written_5m == K and p.written_1h == K


def test_an_api_request_record_names_the_lifetime_of_a_request_that_wrote_nothing() -> None:
    reqs = [_r(1, w5=K), _r(2, read=K), _r(20, read=K)]
    evs = [ev(EventKind.API_REQUEST, 1.9, cache_ttl="1h")]
    p = _plans(reqs, evs)[MAIN]
    # the second request lives an hour: the third one, 18 minutes on, is warm
    assert p.modeled == pytest.approx(K * W5 + 2 * K * READ)


# --------------------------------------------------------------------------- the check


def _rewrite_after_wait(minutes: float, lifetime: str, sub: bool = True) -> list[Request]:
    first = {"w5": K} if lifetime == "5m" else {"w1": K}
    return [_r(1, sub=sub, tools=("Bash",), **first), _r(1 + minutes, sub=sub, **first)]


def _result(minute: float, call: Request):  # type: ignore[no-untyped-def]
    """The result of `call`'s tool call, at `minute`."""
    tid = call.tool_calls[0].tool_use_id
    return ev(EventKind.TOOL_RESULT, minute, agent=call.agent_id, tool_use_id=tid, tool="Bash")


def test_a_20_minute_wait_under_the_1_hour_cache_is_no_rewrite() -> None:
    # the first request wrote with the 1-hour lifetime: whatever the second one wrote,
    # a 20-minute wait did not expire that cache
    reqs = [_r(1, sub=True, tools=("Bash",), w1=K), _r(21, sub=True, w5=K)]
    found = _checks(reqs, [_result(20.5, reqs[0])])
    f = [f for f in found if f.kind == "subagent_cache_expiry"]
    assert not f or f[0].metrics["rewrites"] == 0
    assert "idle_cache_rewrites" not in kinds(found)


def test_a_switch_to_the_1_hour_cache_still_expires_the_5_minute_one() -> None:
    reqs = [_r(1, sub=True, tools=("Bash",), w5=K), _r(21, sub=True, w1=K)]
    f = only(_checks(reqs, [_result(20.5, reqs[0])]), "subagent_cache_expiry")
    assert f.metrics["rewrites"] == 1


def test_subagents_on_5_minutes_are_told_to_ping_with_the_dollars() -> None:
    reqs = _rewrite_after_wait(20, "5m")
    f = only(_checks(reqs, [_result(20.5, reqs[0])]), "subagent_cache_expiry")
    p = _plans(reqs, [_result(20.5, reqs[0])])[SUBAGENT]
    assert f.scope == "all" and f.metrics["rewrites"] == 1
    assert f.metrics["subagent_current"] == "5m" and f.metrics["subagent_cheapest"] == "ping"
    assert f.saving_usd_per_week == pytest.approx(p.five_min - p.pinged)
    assert f"${p.five_min - p.pinged:,.2f} less" in f.summary
    assert "background" in f.advice and "every 4 minutes" in f.advice
    assert "approve the command sooner" in f.advice  # a permission wait looks the same
    assert f.metrics["tools"] == ("Bash",) and f.evidence
    assert f.change is None and f.advice.startswith("Recommended for subagents: ")


def test_subagents_cheapest_on_the_1_hour_cache_get_the_setting() -> None:
    # two 20-minute pauses after the subagent ended its turn: no ping covers them
    reqs = [_r(1, sub=True, w5=K), _r(21, sub=True, w5=K), _r(41, sub=True, w5=K)]
    f = only(_checks(reqs), "subagent_cache_expiry")
    assert f.metrics["subagent_cheapest"] == "1h"
    assert f.change is not None and '"subagentPromptCacheTtl": "1h"' in f.change


def test_subagents_on_1_hour_that_could_ping_are_told_to_remove_the_setting() -> None:
    reqs = _rewrite_after_wait(20, "1h")
    reqs[1] = _r(21, sub=True, read=K)
    f = only(_checks(reqs, [_result(20.5, reqs[0])]), "subagent_cache_expiry")
    assert f.metrics["subagent_current"] == "1h" and f.metrics["subagent_cheapest"] == "ping"
    assert "remove subagentPromptCacheTtl" in f.advice


def test_the_cheapest_setup_already_in_use_is_calm() -> None:
    # a main thread on the 1-hour cache through two user pauses, then a real expiry
    # after a 70-minute tool call: the 1-hour cache is still the cheapest
    reqs = [
        _r(1, w1=K),
        _r(21, read=K),
        _r(41, tools=("Bash",), read=K),
        _r(112, w1=K),
    ]
    f = only(_checks(reqs, [_result(111.5, reqs[2])]), "subagent_cache_expiry")
    assert f.saving_usd_per_week == 0 and f.metrics["rewrites"] == 1
    cost = f"${f.metrics['main_1h_usd']:,.2f}"
    assert f.summary == f"Main threads use the 1-hour cache ({cost} of cache reads and writes)."
    assert "Recommended" not in f.advice and f.change is None


def test_main_threads_are_never_told_to_change_their_lifetime() -> None:
    # the 1-hour cache in use costs more than the 5-minute one (short gaps, one
    # long tool wait), but main threads have no settings key: report, never advise
    reqs = [
        _r(1, w1=K),
        _r(2, read=K),
        _r(3, tools=("Bash",), read=K),
        _r(74, w1=K),
        _r(75, read=K),
    ]
    f = only(_checks(reqs, [_result(73.5, reqs[2])]), "subagent_cache_expiry")
    assert f.metrics["main_current"] == "1h" and f.metrics["main_cheapest"] != "1h"
    assert f.saving_usd_per_week == 0 and f.change is None
    assert "less" not in f.summary and "would" not in f.summary
    assert "Main" not in f.advice and "main" not in f.advice
    assert "Recommended" not in f.advice and "1-hour" not in f.advice


def test_subagent_and_main_parts_each_get_one_sentence() -> None:
    reqs = [*_rewrite_after_wait(20, "5m"), _r(30, w1=K), _r(31, read=K)]
    f = only(_checks(reqs, [_result(20.5, reqs[0])]), "subagent_cache_expiry")
    main, sub = f.summary.split(". ", 1)
    assert main.startswith("Main threads use the 1-hour cache (") and "less" not in main
    assert sub.startswith("Subagents use the 5-minute cache (") and sub.endswith("less.")
    assert f.advice.startswith("Recommended for subagents: ")


def test_no_finding_without_a_saving_or_a_rewrite() -> None:
    found = _checks([_r(1, w1=K), _r(21, read=K)])
    assert "subagent_cache_expiry" not in kinds(found)


def test_the_window_counts_only_its_own_requests() -> None:
    before = req(when=SINCE - timedelta(minutes=10), cache_write_5m=K)
    p = _plans([before, _r(1, read=K)])[MAIN]
    assert p.requests == 1


def test_the_ping_advice_states_its_break_even_share() -> None:
    p = Plans()
    p.five_min, p.one_hour, p.pinged = 100.0, 80.0, 70.0
    share = ping_break_even(p)
    assert share == pytest.approx(2 / 3)
    text, change = _subagent_advice(p, "1h", "ping")
    assert "at least 67% of long waits get a check within 4 minutes" in text
    assert "remove subagentPromptCacheTtl" in text and change is None


def test_a_ping_that_cannot_beat_the_1_hour_cache_is_never_advised() -> None:
    p = Plans()
    p.five_min, p.one_hour, p.pinged = 100.0, 60.0, 70.0
    assert ping_break_even(p) > 1
    text, change = _subagent_advice(p, "5m", "ping")
    assert "every 4 minutes" not in text and change is not None


def test_no_break_even_when_the_5_minute_cache_alone_is_cheaper() -> None:
    p = Plans()
    p.five_min, p.one_hour, p.pinged = 50.0, 80.0, 45.0
    assert ping_break_even(p) == 0
    text, _ = _subagent_advice(p, "1h", "ping")
    assert "pays only" not in text
