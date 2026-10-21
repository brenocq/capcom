from datetime import UTC, datetime

import pytest

from capcom.cost.model import price, price_usage
from capcom.cost.prices import load_prices
from capcom.types import Usage

from .fakes import req

T = load_prices()
M = 1_000_000


def test_each_category_at_its_rate() -> None:
    c = price(req(input=M, cache_read=M, cache_write_5m=M, cache_write_1h=M, output=M), T)
    assert c.input == pytest.approx(4)
    assert c.cache_read == pytest.approx(0.2)
    assert c.cache_write == pytest.approx(5 + 8)  # the 5m / 1h split
    assert c.output == pytest.approx(20)
    assert c.total == pytest.approx(37.2)
    assert c.priced_as == "claude-opus-5-5" and c.exact_price


@pytest.mark.parametrize(
    ("model", "read_usd"),
    [
        ("claude-opus-5-5", 0.20),  # 0.05x
        ("claude-sonnet-5-5", 0.10),  # 0.05x
        ("claude-fable-5-1", 0.25),  # 0.025x
        ("claude-fable-5", 1.0),  # 0.1x
        ("claude-haiku-4-5-20251001", 0.10),
    ],
)
def test_cache_read_multiplier_per_model(model: str, read_usd: float) -> None:
    assert price(req(model=model, cache_read=M), T).cache_read == pytest.approx(read_usd)


def test_long_context_reprices_the_whole_request() -> None:
    small = price(req(model="claude-haiku-5-5", cache_read=100_000, output=M), T)
    assert small.output == pytest.approx(0.5)  # prompt of exactly 100k: base rates
    big = price(req(model="claude-haiku-5-5", input=1, cache_read=100_000, output=M), T)
    assert big.output == pytest.approx(2.5)
    assert big.cache_read == pytest.approx(0.1 * 0.05)
    assert big.input == pytest.approx(0.5 / M)


def test_long_context_counts_cache_writes_in_the_prompt() -> None:
    c = price(req(model="claude-haiku-5-5", cache_write_1h=150_000), T)
    assert c.cache_write == pytest.approx(0.15 * 1.0)


def test_fast_mode() -> None:
    c = price(req(input=M, output=M, cache_read=M, speed="fast"), T)
    assert c.input == pytest.approx(8) and c.output == pytest.approx(40)
    assert c.cache_read == pytest.approx(0.4)
    assert price(req(output=M, speed="standard"), T).output == pytest.approx(20)


def test_fast_mode_without_a_listed_price_is_flagged() -> None:
    c = price(req(model="claude-sonnet-5-5", output=M, speed="fast"), T)
    assert c.output == pytest.approx(10)
    assert not c.exact_price


def test_thinking_is_not_counted_twice() -> None:
    c = price(req(output=M, thinking=600_000), T)
    assert c.output == pytest.approx(20)
    assert c.total == pytest.approx(20)


def test_geo_multiplier_only_on_models_from_4_6() -> None:
    assert price(req(model="claude-opus-4-6", input=M, geo="us"), T).input == pytest.approx(5.5)
    old = price(req(model="claude-opus-4-5", input=M, geo="us"), T)
    assert old.input == pytest.approx(5)
    old = price(req(model="claude-haiku-4-5", input=M, geo="us"), T)
    assert old.input == pytest.approx(1)


def test_older_prices_apply_to_older_requests() -> None:
    before = datetime(2026, 10, 7, 23, 59, tzinfo=UTC)
    after = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    old = price(req(model="claude-sonnet-5-5", when=before, cache_read=M, output=M), T)
    assert old.cache_read == pytest.approx(0.2)
    assert old.output == pytest.approx(10)  # the rest of the row is unchanged
    new = price(req(model="claude-sonnet-5-5", when=after, cache_read=M), T)
    assert new.cache_read == pytest.approx(0.1)
    assert price_usage("claude-sonnet-5-5", Usage(cache_read=M), T).cache_read == pytest.approx(0.1)
    u = Usage(cache_read=M)
    assert price_usage("claude-sonnet-5-5", u, T, when=before).cache_read == pytest.approx(0.2)
    # other models have no history
    assert price(req(when=before, cache_read=M), T).cache_read == pytest.approx(0.2)


def test_geo_multiplier() -> None:
    c = price(req(input=M, output=M, web_searches=1000, geo="us"), T)
    assert c.input == pytest.approx(4.4) and c.output == pytest.approx(22)
    assert c.web == pytest.approx(10)  # web search is not a token category
    assert price(req(input=M, geo="global"), T).input == pytest.approx(4)
    assert price(req(input=M, geo="not_available"), T).input == pytest.approx(4)


def test_fast_and_geo_stack() -> None:
    assert price(req(output=M, speed="fast", geo="us"), T).output == pytest.approx(44)


def test_web_search_and_fetch() -> None:
    c = price(req(web_searches=3, web_fetches=5), T)
    assert c.web == pytest.approx(0.03)


def test_synthetic_costs_nothing() -> None:
    c = price(req(model="<synthetic>", input=M, output=M, web_searches=2), T)
    assert c.total == 0 and not c.exact_price


def test_price_usage_matches_price() -> None:
    u = Usage(input=10, cache_read=20_000, cache_write_1h=3_000, output=500)
    r = req(input=10, cache_read=20_000, cache_write_1h=3_000, output=500)
    assert price_usage("claude-opus-5-5", u, T) == price(r, T)
