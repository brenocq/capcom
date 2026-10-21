"""Pricing OpenClaw's direct calls: Gemini from the table, OpenClaw's own estimate
only for a model the table lacks."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from capcom.cost.model import REPORTED, price
from capcom.cost.prices import ZERO_MODEL, load_prices, lookup
from capcom.types import Request

from .fakes import req

T = load_prices()
M = 1_000_000
BEFORE = datetime(2026, 10, 21, tzinfo=UTC)
AFTER = datetime(2027, 1, 2, tzinfo=UTC)


def _with_cost(r: Request, usd: float | None) -> Request:
    return dataclasses.replace(r, reported_cost_usd=usd)


@pytest.mark.parametrize(
    ("model", "rates"),
    [
        # input, output, cache read; cache writes at the input rate
        ("gemini-3.8-flash", (0.75, 3.75, 0.075)),
        ("gemini-3.1-pro-preview", (2.0, 12.0, 0.2)),
        ("gemini-3.1-pro-preview-customtools", (2.0, 12.0, 0.2)),
        ("gemini-2.5-flash", (0.3, 2.5, 0.03)),
        ("google/gemini-2.5-flash", (0.3, 2.5, 0.03)),
    ],
)
def test_gemini_is_priced_from_the_table(model: str, rates: tuple[float, float, float]) -> None:
    k = 50_000  # prompt below gemini-3.1-pro-preview's long-context threshold
    r = _with_cost(req(model=model, when=BEFORE, input=k, output=k, cache_read=k), 99.0)
    c = price(r, T)
    assert (c.input, c.output, c.cache_read) == pytest.approx([x * k / M for x in rates])
    assert c.exact_price and c.priced_as != REPORTED  # the estimate is ignored
    w = price(req(model=model, when=BEFORE, cache_write_5m=k, cache_write_1h=k), T)
    assert w.cache_write == pytest.approx(2 * rates[0] * k / M)


def test_gemini_flash_doubles_on_2027_01_01() -> None:
    usage = {"input": M, "output": M, "cache_read": M}
    before = price(req(model="gemini-3.8-flash", when=BEFORE, **usage), T)
    after = price(req(model="gemini-3.8-flash", when=AFTER, **usage), T)
    assert after.total == pytest.approx(2 * before.total)
    assert after.total == pytest.approx(1.5 + 7.5 + 0.15)


def test_gemini_pro_long_context_above_200k() -> None:
    small = price(req(model="gemini-3.1-pro-preview", input=200_000, output=M), T)
    big = price(req(model="gemini-3.1-pro-preview", input=200_001, output=M), T)
    assert small.output == pytest.approx(12.0)
    assert big.output == pytest.approx(18.0)
    assert big.input == pytest.approx(200_001 * 4.0 / M)


def test_unknown_model_falls_back_to_the_reported_cost() -> None:
    r = _with_cost(req(model="some-other-model-1", input=M, output=M), 0.42)
    c = price(r, T)
    assert c.total == pytest.approx(0.42)
    assert c.priced_as == REPORTED and not c.exact_price


def test_without_a_reported_cost_an_unknown_model_stays_unpriced() -> None:
    c = price(req(model="some-other-model-1", input=M), T)
    assert c.total == 0 and c.priced_as == ZERO_MODEL
    # OpenClaw writes 0 when it has no rates for the model: no estimate either
    c = price(_with_cost(req(model="some-other-model-1", input=M), 0.0), T)
    assert c.priced_as == ZERO_MODEL


def test_claude_models_ignore_the_reported_cost() -> None:
    r = _with_cost(req(model="claude-opus-4-7", input=M), 123.0)
    assert price(r, T).input == pytest.approx(5.0)
    # an unknown Claude name still prices as its family, not at the estimate
    r = _with_cost(req(model="claude-opus-9-9", input=M), 123.0)
    assert price(r, T).priced_as == lookup(T, "claude-opus-9-9")[0].model


def test_placeholders_ignore_the_reported_cost() -> None:
    assert price(_with_cost(req(model="<synthetic>"), 1.0), T).total == 0
