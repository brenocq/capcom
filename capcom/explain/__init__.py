"""capcom.explain: spikes matched to causes (contract: capcom.types.ExplainModule).

Stub until the explain module is built."""

from __future__ import annotations

from collections.abc import Sequence

from capcom.types import Explanation, Index, PriceTable, Spike


def explain(index: Index, table: PriceTable, spikes: Sequence[Spike]) -> list[Explanation]:
    raise NotImplementedError("capcom.explain.explain")
