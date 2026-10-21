"""capcom.timeline: spend series and spike detection (contract: capcom.types.TimelineModule).

`cumulative` and `per_step` give the chart series (series.py), grouped as in
groups.py; `find_spikes` finds single expensive requests and slope changes per
agent thread, and slope changes of whole sessions (subagent fan-out) (spikes.py);
`regime_changes` finds the days the daily spend moved to a new level, and the
sessions and agent types behind it (regimes.py)."""

from __future__ import annotations

from capcom.timeline.regimes import RegimeChange, regime_changes
from capcom.timeline.series import cumulative, per_step
from capcom.timeline.spikes import find_spikes

__all__ = ["RegimeChange", "cumulative", "find_spikes", "per_step", "regime_changes"]
