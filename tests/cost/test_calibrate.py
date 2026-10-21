from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from capcom.cost.calibrate import calibrate, check_session, steps
from capcom.cost.prices import load_prices
from capcom.sources.claude_code import ClaudeCodeSource
from capcom.store.index import open_index
from capcom.types import CostState, Event, EventKind, FileState, LogFile, LogKind, ModelUsage
from tests.fixtures.generate import Reply, SessionWriter
from tests.fixtures.generate import at as gen_at

from .fakes import T0, FakeIndex, at, req

T = load_prices()


M = 1_000_000
START_MS = int(T0.timestamp() * 1000)


def cs(
    session: str,
    total: float,
    start_ms: int | None = START_MS,
    when: object = None,
    **models: float,
) -> CostState:
    return CostState(
        session_id=session,
        offset=100,
        total_cost_usd=total,
        models={m.replace("_", "-"): ModelUsage(cost_usd=v) for m, v in models.items()},
        start_time_ms=start_ms,
        timestamp=when,  # type: ignore[arg-type]
    )


def test_main_and_subagents_against_the_last_cost_state() -> None:
    idx = FakeIndex(
        reqs=[
            req("s1", when=at(1), output=M),  # $20
            req("s1", when=at(2), agent="a1", input=M),  # $4, a subagent
            req("s1", when=at(3), model="claude-haiku-5-5", output=M),  # $0.50
            req("s2", when=at(1), output=M),  # another session
        ],
        costs=[cs("s1", 24.5, claude_opus_5_5=24.0, claude_haiku_5_5=0.5)],
    )
    [cal] = calibrate(idx, T)
    assert cal.session_id == "s1"
    assert cal.capcom_usd == pytest.approx(24.5)
    assert cal.spread == pytest.approx(0)
    assert cal.per_model["claude-opus-5-5"] == pytest.approx((24.0, 24.0))
    assert cal.per_model["claude-haiku-5-5"] == pytest.approx((0.5, 0.5))
    assert cal.comparable and cal.reasons == ()


def test_requests_before_the_process_start_are_left_out() -> None:
    idx = FakeIndex(
        reqs=[req("s1", when=at(-60), output=M), req("s1", when=at(5), output=M)],
        costs=[cs("s1", 20.0, claude_opus_5_5=20.0)],
    )
    [cal] = calibrate(idx, T)
    assert cal.capcom_usd == pytest.approx(20.0)


def test_without_a_start_time_the_session_is_not_comparable() -> None:
    idx = FakeIndex(
        reqs=[req("s1", when=at(-60), output=M), req("s1", when=at(5), output=M)],
        costs=[cs("s1", 20.0, start_ms=None, claude_opus_5_5=20.0)],
    )
    [cal] = calibrate(idx, T)
    assert not cal.comparable
    assert cal.reasons == ("no startTime",)


def test_the_window_ends_at_the_next_record_after_the_cost_state() -> None:
    last, nxt = at(5), at(6)
    reqs = [
        req("s1", when=at(1), output=M),
        req("s1", when=last, output=M),  # the record just before the cost-state
        # Written after the last record before the line (a background subagent, or
        # records out of order), but before the next one: cost-state counts it.
        req("s1", when=last + timedelta(seconds=0.5), agent="a1", output=M),
        req("s1", when=nxt, output=M),  # the next record: the end, exclusive
    ]
    late = [Event(EventKind.AWAY, "s1", None, at(9), "late")]  # after it, too
    state = replace(cs("s1", 60.0, when=last, claude_opus_5_5=60.0), next_timestamp=nxt)
    [cal] = calibrate(FakeIndex(reqs=reqs, evs=late, costs=[state]), T)
    assert cal.capcom_usd == pytest.approx(60.0)
    assert cal.comparable
    # No record after the cost-state yet: no upper cut.
    open_end = replace(state, next_timestamp=None)
    [cal] = calibrate(FakeIndex(reqs=reqs, costs=[open_end]), T)
    assert cal.capcom_usd == pytest.approx(80.0)


def test_a_next_record_out_of_order_does_not_cut_the_replies_before_it() -> None:
    last = at(5)
    reqs = [req("s1", when=at(1), output=M), req("s1", when=last, output=M)]
    early = at(3)  # the next record's time, before the cost-state's own
    state = replace(cs("s1", 40.0, when=last, claude_opus_5_5=40.0), next_timestamp=early)
    [cal] = calibrate(FakeIndex(reqs=reqs, costs=[state]), T)
    assert cal.capcom_usd == pytest.approx(40.0)  # the window ends at last + 1 ms
    assert cal.comparable
    after = [*reqs, req("s1", when=last + timedelta(milliseconds=1), output=M)]
    [cal] = calibrate(FakeIndex(reqs=after, costs=[state]), T)
    assert cal.capcom_usd == pytest.approx(40.0)


def test_shared_replies_make_a_session_not_comparable() -> None:
    reqs = [req("s1", when=at(1), output=M), req("s2", when=at(1), output=M)]
    idx = FakeIndex(reqs=reqs, costs=[cs("s1", 20.0, claude_opus_5_5=20.0)])
    idx.shared = {"s1": [at(1)]}
    [cal] = calibrate(idx, T)
    assert cal.reasons == ("shares replies with another session",)
    idx.shared = {"s1": [at(-60)]}  # before the process start: not in the window
    [cal] = calibrate(idx, T)
    assert cal.comparable


def _with_input(state: CostState, model: str, tokens: int) -> CostState:
    models = dict(state.models)
    models[model] = replace(models[model], input=tokens)
    return replace(state, models=models)


@pytest.mark.parametrize(
    ("logged", "cost_state", "comparable"),
    [
        (100, 900, True),  # 800 tokens more: below the token floor
        (100, 1_200, False),  # 1,100 tokens more
        (5_000, 5_900, True),
        (5_000, 40_000, False),  # 8x: no ratio lets it through
        (0, 900, True),
        (0, 1_500, False),
        (2_000_000, 2_030_000, True),  # 1.5 % of a large session's input
        (2_000_000, 2_100_000, False),  # a large session missing a 100k-token call
    ],
)
def test_calls_not_in_the_log(logged: int, cost_state: int, comparable: bool) -> None:
    idx = FakeIndex(
        reqs=[req("s1", when=at(1), output=M, input=logged)],
        costs=[
            _with_input(
                cs("s1", 20.0 + logged * 4 / M, claude_opus_5_5=20.0 + logged * 4 / M),
                "claude-opus-5-5",
                cost_state,
            )
        ],
    )
    [cal] = calibrate(idx, T)
    assert cal.comparable is comparable
    assert cal.reasons == (() if comparable else ("calls not in the log",))


def test_model_names_are_normalized_on_both_sides() -> None:
    idx = FakeIndex(
        reqs=[req("s1", when=at(1), output=M)],
        costs=[
            CostState("s1", 0, 20.0, {"claude-opus-5-5[1m]": ModelUsage(cost_usd=20.0)}, START_MS)
        ],
    )
    [cal] = calibrate(idx, T)
    assert list(cal.per_model) == ["claude-opus-5-5"]


@pytest.mark.parametrize(
    ("extra_req", "event", "reason"),
    [
        ({"tools": ("WebFetch",)}, None, "WebFetch"),
        ({"tools": ("WebSearch",)}, None, "WebSearch"),
        ({"estimated": True}, None, "estimated output"),
        ({}, EventKind.AWAY, "away"),
        ({}, EventKind.COMPACTION, "compaction"),
    ],
)
def test_side_calls_make_a_session_not_comparable(
    extra_req: dict[str, object], event: EventKind | None, reason: str
) -> None:
    idx = FakeIndex(
        reqs=[req("s1", when=at(1), output=M, **extra_req)],  # type: ignore[arg-type]
        costs=[cs("s1", 25.0, claude_opus_5_5=25.0)],
    )
    if event is not None:
        idx.evs.append(Event(event, "s1", None, at(2), "k1"))
    [cal] = calibrate(idx, T)
    assert not cal.comparable
    assert reason in cal.reasons


def test_tiny_sessions() -> None:
    idx = FakeIndex(
        reqs=[req("s1", when=at(1), output=1000)],  # $0.02
        costs=[cs("s1", 0.021, claude_opus_5_5=0.02, claude_haiku_4_5_20251001=0.001)],
    )
    [cal] = calibrate(idx, T)
    assert cal.reasons == ("below $0.10",)
    assert cal.per_model["claude-haiku-4-5-20251001"] == pytest.approx((0.0, 0.001))


def test_the_small_side_model_of_every_process_is_ignored() -> None:
    idx = FakeIndex(
        reqs=[req("s1", when=at(1), output=M)],  # $20
        costs=[cs("s1", 20.009, claude_opus_5_5=20.0, claude_haiku_4_5=0.009)],
    )
    assert calibrate(idx, T)[0].comparable
    # above $0.01 and 1 % of the session it counts
    idx.costs = [cs("s1", 20.5, claude_opus_5_5=20.0, claude_haiku_4_5=0.5)]
    assert calibrate(idx, T)[0].reasons == ("model only in cost-state",)


def test_per_model_keys_are_the_table_ids_on_both_sides() -> None:
    idx = FakeIndex(
        reqs=[req("s1", when=at(1), model="claude-haiku-4-5", output=M)],
        costs=[cs("s1", 5.0, claude_haiku_4_5_20251001=5.0)],
    )
    [cal] = calibrate(idx, T)
    assert dict(cal.per_model) == {"claude-haiku-4-5-20251001": pytest.approx((5.0, 5.0))}


def test_synthetic_replies_are_left_out() -> None:
    idx = FakeIndex(
        reqs=[req("s1", when=at(1), output=M), req("s1", when=at(2), model="<synthetic>")],
        costs=[cs("s1", 20.0, claude_opus_5_5=20.0)],
    )
    [cal] = calibrate(idx, T)
    assert list(cal.per_model) == ["claude-opus-5-5"]
    assert cal.comparable


def test_spread_sign() -> None:
    idx = FakeIndex(
        reqs=[req("s1", when=at(1), output=M)], costs=[cs("s1", 25.0, claude_opus_5_5=25.0)]
    )
    assert calibrate(idx, T)[0].spread == pytest.approx(-0.2)


def test_shared_replies_against_the_store(tmp_path: Path) -> None:
    """A session and a copy of its log (another session id) in a real SqliteIndex:
    the copy, read last, keeps the reply's row, so only the original misses it."""
    s = SessionWriter(tmp_path / "logs", cwd="/w", session_id="sess-orig")
    s.assistant(Reply(gen_at(10), output=1_000_000))
    s.cost_state(20.0, {"claude-opus-5-5": {"inputTokens": 3, "costUSD": 20.0}})
    s.close()
    fork = s.path.with_name("sess-copy.jsonl")
    fork.write_text(s.path.read_text().replace("sess-orig", "sess-copy"))
    with open_index(tmp_path / "cache" / "index.sqlite") as ix:
        for path, sid in ((s.path, "sess-orig"), (fork, "sess-copy")):
            log = LogFile(path, LogKind.SESSION, "-w", sid)
            for b in ClaudeCodeSource().parse(log, 0):
                ix.apply(log, b, FileState(path, 1, b.end_offset, 1, b.end_offset))
        got = {c.session_id: c for c in calibrate(ix, T)}
    # The original's window lost its only reply, so its model is "only in cost-state".
    assert "shares replies with another session" in got["sess-orig"].reasons
    assert got["sess-copy"].comparable


# --------------------------------------------------------------------------- steps

H = 3_600_000  # ms


def step_cs(
    offset: int,
    total: float,
    when: int,
    nxt: int | None,
    dur_ms: int | None,
    start_ms: int | None = START_MS,
    **tokens: int,
) -> CostState:
    """A cost-state written at minute `when`, the next record at minute `nxt`, with
    Opus 5.5 tokens (`output=`, `cache_read=`, ...) and its dollars."""
    mu = ModelUsage(cost_usd=total, **tokens)
    return CostState(
        session_id="s1",
        offset=offset,
        total_cost_usd=total,
        models={"claude-opus-5-5": mu},
        start_time_ms=start_ms,
        timestamp=at(when),
        next_timestamp=None if nxt is None else at(nxt),
        total_duration_ms=dur_ms,
    )


def test_restored_totals_compare_per_process_step() -> None:
    # Process 1: minutes 0-60, $20. A process that wrote no cost-state: minute 120,
    # $20. Process 2 restores process 1's totals: minutes 180-200, +$4 over 20 min.
    idx = FakeIndex(
        reqs=[
            req("s1", when=at(10), output=M),
            req("s1", when=at(120), output=M),
            req("s1", when=at(190), input=M),
        ],
        costs=[
            step_cs(100, 20.0, 60, 120, H, output=M),
            step_cs(200, 24.0, 200, None, H + 20 * 60_000, output=M, input=M),
        ],
    )
    cal = check_session(idx, idx.costs[-1], T)
    assert cal.claude_code_usd == pytest.approx(24.0)
    assert cal.capcom_usd == pytest.approx(24.0)
    assert cal.outside_usd == pytest.approx(20.0)
    assert cal.comparable and cal.rest_usd == pytest.approx(0)


def test_steps_skip_a_repeated_exit_and_restart_with_a_new_start() -> None:
    a = step_cs(100, 20.0, 60, 61, H)
    again = step_cs(150, 20.0, 61, 62, H)  # the same exit written twice
    later = step_cs(200, 24.0, 200, None, H + 20 * 60_000)
    fresh = step_cs(300, 3.0, 400, None, 10 * 60_000, start_ms=START_MS + 390 * 60_000)
    got = steps([a, again, later, fresh])
    assert [s.usd for s in got] == pytest.approx([20.0, 4.0, 3.0])
    assert got[0].since == T0 and got[0].until == at(61)
    assert got[1].since == at(200) - timedelta(minutes=21) and got[1].until is None
    assert got[2].since == at(390)


def test_a_process_that_restored_an_older_total_grows_from_it() -> None:
    a = step_cs(100, 20.0, 60, 61, H)
    b = step_cs(200, 30.0, 300, 301, 2 * H)  # restored a, ran an hour
    c = step_cs(300, 25.0, 310, None, H + 30 * 60_000)  # also restored a, ran 30 min
    got = steps([a, b, c])
    assert [s.usd for s in got] == pytest.approx([20.0, 10.0, 5.0])
    assert got[2].since == at(310) - timedelta(minutes=31)


def test_a_shorter_duration_with_the_same_start_grows_by_cost_alone() -> None:
    # $20 over an hour, then $24 with a duration of 20 minutes (a restore that lost
    # the duration): $4 more, not a fresh $24.
    a = step_cs(100, 20.0, 60, 70, H)
    b = step_cs(200, 24.0, 200, None, 20 * 60_000)
    got = steps([a, b])
    assert [s.usd for s in got] == pytest.approx([20.0, 4.0])
    assert got[1].since == at(70)
    # A cost below every earlier record starts again from the process start.
    c = step_cs(300, 3.0, 300, None, 30 * 60_000)
    assert [s.usd for s in steps([a, c])] == pytest.approx([20.0, 3.0])


def test_the_base_must_have_no_more_tokens_and_be_written_before_the_start() -> None:
    a = step_cs(100, 20.0, 60, 61, H, cache_read=5 * M)
    # b costs more than a but counts fewer cache reads: it restored an older total.
    b = step_cs(200, 22.0, 300, 301, 2 * H, cache_read=4 * M)
    got = steps([step_cs(50, 10.0, 30, 31, H // 2, cache_read=2 * M), a, b])
    assert got[-1].usd == pytest.approx(12.0)  # grew from the $10 record
    # c exited by minute 201 (the next record). From `early`, its process started by
    # minute 201 less 165 (after `early` was written); from `later`, by minute 41,
    # before `later` was: it restored `early`.
    early = step_cs(50, 10.0, 30, 31, 25 * 60_000)
    later = step_cs(100, 20.0, 60, 61, 30 * 60_000)
    c = step_cs(200, 25.0, 200, 201, 190 * 60_000)
    got = steps([early, later, c])
    assert got[-1].usd == pytest.approx(15.0)
    assert got[-1].since == at(34)
    # An exit long after the last record (idle before it) does not rule `later` out.
    idle = step_cs(200, 25.0, 200, 300, 190 * 60_000)
    assert steps([early, later, idle])[-1].usd == pytest.approx(5.0)


def test_a_step_never_starts_before_its_base_was_written() -> None:
    # The process idled before it exited: its duration grew by more than the time
    # since a was written (within the slack), so the step starts at a's time.
    a = step_cs(100, 20.0, 60, 61, H)
    b = step_cs(200, 24.0, 100, None, H + 40 * 60_000 + 30_000)
    got = steps([a, b])
    assert got[1].since == at(60)


def test_below_ten_cents_uses_the_compared_total() -> None:
    # Two processes that each started from zero: $0.05, then $0.06. The last record
    # alone is under $0.10, the compared total is not.
    later = START_MS + 120 * 60_000
    idx = FakeIndex(
        reqs=[req("s1", when=at(5), output=2500), req("s1", when=at(130), output=3000)],
        costs=[
            step_cs(100, 0.05, 60, 90, H, output=2500),
            step_cs(200, 0.06, 150, None, H, start_ms=later, output=3000),
        ],
    )
    cal = check_session(idx, idx.costs[-1], T)
    assert cal.claude_code_usd == pytest.approx(0.11)
    assert "below $0.10" not in cal.reasons


def test_output_above_capcom_estimate_is_a_range() -> None:
    # A subagent's estimated output: 0.1 M tokens logged, cost-state counted 0.6 M.
    idx = FakeIndex(
        reqs=[req("s1", when=at(5), agent="a1", estimated=True, output=M // 10)],
        costs=[step_cs(100, 12.0, 60, None, H, output=6 * M // 10)],
    )
    [cal] = calibrate(idx, T)
    assert cal.capcom_usd == pytest.approx(2.0)
    assert cal.output_above == pytest.approx({"claude-opus-5-5": 10.0})
    assert cal.unlogged == {}
    assert cal.rest_usd == pytest.approx(0)
    assert "estimated output" in cal.reasons


def test_calls_not_in_the_log_are_priced_per_model_and_category() -> None:
    # The log has 1 M cache reads; cost-state counted 11 M (and the same output).
    idx = FakeIndex(
        reqs=[req("s1", when=at(5), cache_read=M, output=M)],
        costs=[step_cs(100, 22.5, 60, None, H, cache_read=11 * M, output=M)],
    )
    [cal] = calibrate(idx, T)
    rate = cal.capcom_usd - 20.0  # the window's own cache-read price per M
    assert list(cal.unlogged) == ["claude-opus-5-5"]
    assert cal.unlogged["claude-opus-5-5"] == pytest.approx({"cache_read": 10 * rate})
    assert cal.output_above == {}
    assert cal.logged_more == {}
    assert cal.rest_usd == pytest.approx(22.5 - 11 * rate - 20.0)


def test_what_the_log_counts_more_of_is_shown_too() -> None:
    # The log has 2 M uncached input; cost-state counted 1 M (and the same output).
    idx = FakeIndex(
        reqs=[req("s1", when=at(5), input=2 * M, output=M)],
        costs=[step_cs(100, 24.0, 60, None, H, input=M, output=M)],
    )
    [cal] = calibrate(idx, T)
    assert cal.unlogged == {}
    assert cal.logged_more["claude-opus-5-5"] == pytest.approx({"input": 4.0})
    assert cal.capcom_usd == pytest.approx(28.0)
    assert cal.rest_usd == pytest.approx(0)


class _NoCostStates:
    """An Index without the optional `cost_states`: only the last record compares."""

    def __init__(self, idx: FakeIndex) -> None:
        self.requests = idx.requests
        self.events = idx.events
        self.shared_replies = idx.shared_replies


def test_an_index_without_cost_states_compares_the_last_record_alone() -> None:
    idx = FakeIndex(
        reqs=[req("s1", when=at(5), output=M)],
        costs=[step_cs(100, 20.0, 60, None, H, output=M)],
    )
    cal = check_session(_NoCostStates(idx), idx.costs[-1], T)  # type: ignore[arg-type]
    assert cal.capcom_usd == pytest.approx(20.0) and cal.comparable
