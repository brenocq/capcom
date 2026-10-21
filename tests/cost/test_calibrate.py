from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import pytest

from capcom.cost.calibrate import calibrate
from capcom.cost.prices import load_prices
from capcom.types import CostState, Event, EventKind, ModelUsage

from .fakes import T0, FakeIndex, at, req

T = load_prices()


@dataclass
class SharedIndex(FakeIndex):
    """A FakeIndex with the store's optional `shared_replies` count."""

    shared: dict[str, list[datetime]] = field(default_factory=dict)

    def shared_replies(
        self, session_id: str, since: datetime | None = None, until: datetime | None = None
    ) -> int:
        return sum(
            1
            for t in self.shared.get(session_id, [])
            if (since is None or t >= since) and (until is None or t < until)
        )


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


def test_shared_replies_make_a_session_not_comparable() -> None:
    reqs = [req("s1", when=at(1), output=M), req("s2", when=at(1), output=M)]
    idx = SharedIndex(reqs=reqs, costs=[cs("s1", 20.0, claude_opus_5_5=20.0)])
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
        (100, 900, True),  # 9x, but below the token floor
        (100, 1_200, False),  # 12x and 1,100 tokens more
        (5_000, 40_000, True),  # 8x: within the ratio
        (5_000, 60_000, False),  # 12x
        (0, 900, True),
        (0, 1_500, False),
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
