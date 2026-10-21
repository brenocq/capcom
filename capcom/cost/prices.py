"""The price table (prices.json) and model-name lookups.

`lookup` turns whatever model name a log carries into a `Price`: the exact table
row when the name is a known id, a dated id or an alias of one, else the closest
version of the same family (flagged inexact). `<synthetic>` and names that are
not Claude models price at zero, also flagged.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from functools import cache
from importlib import resources
from typing import Any

from capcom.types import LongContextPrice, Price, PriceTable

FAMILIES = ("fable", "opus", "sonnet", "haiku")
ZERO_MODEL = "<unpriced>"
_DATE = re.compile(r"-(\d{8})$")
_SUFFIX = re.compile(r"\[[^\]]*\]$")  # e.g. "claude-sonnet-4-5[1m]"
_VERTEX_DATE = re.compile(r"@(\d{8})$")  # e.g. "claude-sonnet-4@20250514"
# e.g. "us.anthropic.claude-...", "global.anthropic.claude-...", "us-gov.anthropic.claude-..."
_BEDROCK = re.compile(r"^(?:[a-z0-9]+(?:-[a-z0-9]+)*\.)*anthropic\.")
_BEDROCK_VERSION = re.compile(r"-v\d+(?::\d+)?$")  # e.g. "...-v1:0"


def _price(model: str, row: dict[str, Any]) -> Price:
    lc = row.get("long_context")
    long_context = None
    if isinstance(lc, dict):
        long_context = LongContextPrice(
            threshold_tokens=int(lc["threshold_tokens"]),
            input=float(lc["input"]),
            output=float(lc["output"]),
            cache_write_5m=float(lc["cache_write_5m"]),
            cache_write_1h=float(lc["cache_write_1h"]),
            cache_read=float(lc["cache_read"]),
        )
    fast = row.get("fast_multiplier")
    return Price(
        model=model,
        input=float(row["input"]),
        output=float(row["output"]),
        cache_write_5m=float(row["cache_write_5m"]),
        cache_write_1h=float(row["cache_write_1h"]),
        cache_read=float(row["cache_read"]),
        long_context=long_context,
        fast_multiplier=None if fast is None else float(fast),
        geo_pricing=bool(row.get("geo", False)),
    )


def parse_prices(data: dict[str, Any]) -> PriceTable:
    """A PriceTable from prices.json's structure. Aliases become extra keys that map
    to their model's Price (whose `model` is the canonical id)."""
    models: dict[str, Price] = {}
    rows: dict[str, dict[str, Any]] = data.get("models", {})
    for model, row in rows.items():
        p = _price(model, row)
        models[model] = p
        for alias in row.get("aliases", ()):
            models.setdefault(alias, p)
    previous: dict[str, tuple[tuple[date, Price], ...]] = {}
    for model, entries in data.get("previous", {}).items():
        base = rows.get(model)
        if base is None:
            continue
        history = [
            (date.fromisoformat(e["valid_until"]), _price(model, {**base, **e})) for e in entries
        ]
        previous[model] = tuple(sorted(history, key=lambda h: h[0], reverse=True))
    return PriceTable(
        checked=date.fromisoformat(data["checked"]),
        sources=tuple(data.get("sources", ())),
        models=models,
        web_search_per_1k=float(data.get("web_search_per_1k", 0.0)),
        web_fetch_per_1k=float(data.get("web_fetch_per_1k", 0.0)),
        geo_multipliers={k: float(v) for k, v in data.get("geo_multipliers", {}).items()},
        previous=previous,
    )


@cache
def load_prices() -> PriceTable:
    """The packaged price table (capcom/cost/prices.json)."""
    text = resources.files("capcom.cost").joinpath("prices.json").read_text(encoding="utf-8")
    return parse_prices(json.loads(text))


def normalize(model: str) -> str:
    """Strip what is not part of the model id: case, a `[1m]`-style suffix, Bedrock and
    Vertex decorations, a provider prefix such as `anthropic/`."""
    name = model.strip().lower()
    name = _SUFFIX.sub("", name)
    if "/" in name:
        name = name.rsplit("/", 1)[1]
    name = _BEDROCK.sub("", name)
    name = _BEDROCK_VERSION.sub("", name)
    name = _VERTEX_DATE.sub(r"-\1", name)
    return name


def is_placeholder(model: str) -> bool:
    """`<synthetic>` and the like: Claude Code's stand-in replies (errors, interrupts),
    not model calls. capcom leaves them out of totals and calibrations alike."""
    return model.strip().startswith("<")


def price_at(table: PriceTable, p: Price, when: datetime | None) -> Price:
    """`p` as it was priced at `when` (the table's `previous` rows), else `p`."""
    if when is None:
        return p
    day = when.astimezone(UTC).date() if when.tzinfo else when.date()
    chosen = p
    for valid_until, old in table.previous.get(p.model, ()):  # newest first
        if day < valid_until:
            chosen = old
    return chosen


def canonical(table: PriceTable, model: str) -> str:
    """The table id `model` prices as when it is an exact match, else its normalized
    name (the key for per-model totals on both sides of a calibration)."""
    p, exact = lookup(table, model)
    return p.model if exact else normalize(model)


def zero_price() -> Price:
    return Price(ZERO_MODEL, 0.0, 0.0, 0.0, 0.0, 0.0)


def _family_version(name: str) -> tuple[str, tuple[int, int]] | None:
    """("opus", (5, 5)) for "claude-opus-5-5"; also the old order "claude-3-5-haiku"."""
    parts = _DATE.sub("", name).split("-")
    family = next((p for p in parts if p in FAMILIES), None)
    if family is None:
        return None
    nums = [int(p) for p in parts if p.isdigit() and len(p) <= 2]
    if not nums:
        return family, (0, 0)
    return family, (nums[0], nums[1] if len(nums) > 1 else 0)


def _exact(table: PriceTable, name: str) -> Price | None:
    if name in table.models:
        return table.models[name]
    undated = _DATE.sub("", name)
    if undated != name and undated in table.models:
        return table.models[undated]
    # an undated alias of a dated id: "claude-sonnet-4-5" -> "claude-sonnet-4-5-20250929"
    for key, p in table.models.items():
        m = _DATE.search(key)
        if m and key[: m.start()] == undated:
            return p
    return None


def lookup(table: PriceTable, model: str) -> tuple[Price, bool]:
    """The Price for `model` and whether it was an exact match.

    Exact: a table id, a dated id of one, or an alias. Otherwise the same family's
    closest version (newer wins a tie), or the newest Opus for a Claude name of an
    unknown family. `<synthetic>`, empty and non-Claude names price at zero.
    """
    name = normalize(model)
    if not name or name.startswith("<"):
        return zero_price(), False
    p = _exact(table, name)
    if p is not None:
        return p, True
    fv = _family_version(name)
    if fv is None:
        if "claude" not in name:
            return zero_price(), False
        fv = ("opus", (99, 99))
    family, version = fv
    best: tuple[tuple[int, int], Price] | None = None
    target = version[0] * 100 + version[1]
    for key, cand in table.models.items():
        cfv = _family_version(key)
        if cfv is None or cfv[0] != family:
            continue
        v = cfv[1][0] * 100 + cfv[1][1]
        rank = (abs(v - target), -v)
        if best is None or rank < best[0]:
            best = (rank, cand)
    if best is None:
        return zero_price(), False
    return best[1], False
