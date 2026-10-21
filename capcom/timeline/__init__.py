"""capcom.timeline: spend series and spike detection (contract: capcom.types.TimelineModule).

`cumulative` and `per_step` give the chart series (series.py), grouped as in
groups.py; `find_spikes` finds single expensive requests and slope changes per
agent thread (spikes.py)."""

from __future__ import annotations

from capcom.timeline.series import cumulative, per_step
from capcom.timeline.spikes import find_spikes

__all__ = ["cumulative", "find_spikes", "per_step"]
