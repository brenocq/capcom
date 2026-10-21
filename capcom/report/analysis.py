"""The analysis modules the website calls (timeline, explain, checks), behind one
small adapter so the API can run, and be tested, before those modules exist.

A module that is still a stub raises NotImplementedError; the adapter turns that
into `Unavailable`, which the API reports as a missing part (or a 501) instead of
an error.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from capcom.timeline.regimes import RegimeChange
from capcom.types import (
    Explanation,
    Finding,
    GroupBy,
    Index,
    PriceTable,
    Series,
    Spike,
)


class Unavailable(Exception):
    """An analysis module that is not built yet. `module` names it, `milestone` says
    when it is planned."""

    def __init__(self, module: str, milestone: int) -> None:
        super().__init__(f"capcom.{module} is not available yet (milestone {milestone})")
        self.module = module
        self.milestone = milestone


class SeriesFn(Protocol):
    """capcom.timeline.cumulative or per_step: `top` keeps the largest members and
    folds the rest into one "(other)" series."""

    def __call__(
        self,
        index: Index,
        table: PriceTable,
        since: datetime,
        until: datetime,
        by: GroupBy,
        step: timedelta,
        *,
        top: int | None = None,
    ) -> list[Series]: ...


SpikesFn = Callable[[Index, PriceTable, datetime, datetime, str | None], list[Spike]]
ExplainFn = Callable[[Index, PriceTable, Sequence[Spike]], list[Explanation]]
ChecksFn = Callable[[Index, PriceTable, datetime, datetime], list[Finding]]
# capcom.timeline.regime_changes(index, table, since, until, tz, openclaw_root)
RegimesFn = Callable[..., list[RegimeChange]]


def _call[T](module: str, milestone: int, fn: Callable[[], T]) -> T:
    try:
        return fn()
    except NotImplementedError as err:
        raise Unavailable(module, milestone) from err


@dataclass(frozen=True, slots=True)
class Analysis:
    """The functions of capcom.timeline, capcom.explain and capcom.checks (or fakes)."""

    cumulative_fn: SeriesFn
    per_step_fn: SeriesFn
    find_spikes_fn: SpikesFn
    explain_fn: ExplainFn
    checks_fn: ChecksFn
    regimes_fn: RegimesFn | None = None  # None: capcom.timeline.regime_changes

    def cumulative(
        self,
        index: Index,
        table: PriceTable,
        since: datetime,
        until: datetime,
        by: GroupBy,
        step: timedelta,
        top: int | None = None,
    ) -> list[Series]:
        return _call(
            "timeline",
            2,
            lambda: self.cumulative_fn(index, table, since, until, by, step, top=top),
        )

    def per_step(
        self,
        index: Index,
        table: PriceTable,
        since: datetime,
        until: datetime,
        by: GroupBy,
        step: timedelta,
        top: int | None = None,
    ) -> list[Series]:
        return _call(
            "timeline", 2, lambda: self.per_step_fn(index, table, since, until, by, step, top=top)
        )

    def find_spikes(
        self,
        index: Index,
        table: PriceTable,
        since: datetime,
        until: datetime,
        session_id: str | None = None,
    ) -> list[Spike]:
        """Spikes alone (for chart markers), without the explanations."""
        return _call(
            "timeline", 2, lambda: self.find_spikes_fn(index, table, since, until, session_id)
        )

    def spikes(
        self, index: Index, table: PriceTable, since: datetime, until: datetime
    ) -> tuple[list[Spike], list[Explanation]]:
        spikes = _call("timeline", 3, lambda: self.find_spikes_fn(index, table, since, until, None))
        return spikes, _call("explain", 3, lambda: self.explain_fn(index, table, spikes))

    def explain(
        self, index: Index, table: PriceTable, spikes: Sequence[Spike]
    ) -> list[Explanation]:
        """Causes for spikes found earlier (chart markers, one session's spikes)."""
        if not spikes:
            return []
        return _call("explain", 3, lambda: self.explain_fn(index, table, spikes))

    def checks(
        self, index: Index, table: PriceTable, since: datetime, until: datetime
    ) -> list[Finding]:
        return _call("checks", 4, lambda: self.checks_fn(index, table, since, until))

    def regimes(self) -> RegimesFn:
        """The function behind the "what changed" story (capcom.report.story)."""
        if self.regimes_fn is not None:
            fn = self.regimes_fn
        else:
            from capcom.timeline.regimes import regime_changes

            fn = regime_changes

        def call(*args: Any, **kwargs: Any) -> list[RegimeChange]:
            return _call("timeline", 2, lambda: fn(*args, **kwargs))

        return call


def default_analysis() -> Analysis:
    """The real modules."""
    import capcom.checks
    import capcom.explain
    import capcom.timeline

    def spikes_fn(
        index: Index, table: PriceTable, since: datetime, until: datetime, sid: str | None
    ) -> list[Spike]:
        return capcom.timeline.find_spikes(index, table, since, until, sid)

    return Analysis(
        cumulative_fn=capcom.timeline.cumulative,
        per_step_fn=capcom.timeline.per_step,
        find_spikes_fn=spikes_fn,
        explain_fn=capcom.explain.explain,
        checks_fn=capcom.checks.run_checks,
    )
