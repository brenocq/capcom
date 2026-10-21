from datetime import date

import pytest

from capcom.cost.prices import ZERO_MODEL, load_prices, lookup, normalize

T = load_prices()


def test_table_loads_with_its_metadata() -> None:
    assert T.checked == date(2026, 10, 21)
    assert T.sources
    assert T.web_search_per_1k == 10.0
    assert T.web_fetch_per_1k == 0.0
    assert T.geo_multipliers == {"us": 1.1}


def test_exact_id() -> None:
    p, exact = lookup(T, "claude-opus-5-5")
    assert exact and p.model == "claude-opus-5-5"
    assert (p.input, p.output, p.cache_write_5m, p.cache_write_1h, p.cache_read) == (
        4,
        20,
        5,
        8,
        0.2,
    )


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),  # dated id in the table
        ("claude-haiku-4-5", "claude-haiku-4-5-20251001"),  # alias
        ("claude-opus-4-0", "claude-opus-4-20250514"),  # explicit alias
        ("claude-opus-4", "claude-opus-4-20250514"),
        ("claude-sonnet-4-5", "claude-sonnet-4-5-20250929"),
        ("claude-opus-4-6-20260301", "claude-opus-4-6"),  # a dated id of an undated model
        ("claude-opus-5-5[1m]", "claude-opus-5-5"),  # Claude Code's 1M-context suffix
        ("Claude-Sonnet-5-5", "claude-sonnet-5-5"),
        ("claude-3-5-haiku-latest", "claude-3-5-haiku-20241022"),
        ("anthropic/claude-opus-5-5", "claude-opus-5-5"),
        ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5-20250929"),
        ("claude-sonnet-4@20250514", "claude-sonnet-4-20250514"),
    ],
)
def test_dated_ids_and_aliases_are_exact(name: str, model: str) -> None:
    p, exact = lookup(T, name)
    assert exact
    assert p.model == model


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("claude-opus-5-6", "claude-opus-5-5"),  # a newer Opus: the closest one
        ("claude-sonnet-4-7", "claude-sonnet-4-6"),
        ("claude-haiku-6", "claude-haiku-5-5"),
        ("claude-3-7-sonnet-20250219", "claude-sonnet-4-20250514"),  # unverified, left out
        ("claude-3-haiku-20240307", "claude-3-5-haiku-20241022"),
        ("claude-mystery-1", "claude-opus-5-5"),  # unknown Claude family: newest Opus
    ],
)
def test_family_fallback_is_flagged(name: str, model: str) -> None:
    p, exact = lookup(T, name)
    assert not exact
    assert p.model == model


@pytest.mark.parametrize("name", ["<synthetic>", "", "gemma4:12b-it-qat", "gpt-9"])
def test_synthetic_and_foreign_models_cost_nothing(name: str) -> None:
    p, exact = lookup(T, name)
    assert not exact
    assert p.model == ZERO_MODEL
    assert p.input == p.output == p.cache_read == 0


def test_normalize() -> None:
    assert normalize(" claude-opus-5[1m] ") == "claude-opus-5"


def test_rules_in_the_table() -> None:
    haiku, _ = lookup(T, "claude-haiku-5-5")
    assert haiku.long_context is not None
    assert haiku.long_context.threshold_tokens == 100_000
    assert lookup(T, "claude-opus-5-5")[0].fast_multiplier == 2
    assert lookup(T, "claude-sonnet-5-5")[0].fast_multiplier is None
    # cache reads: each model's own multiple of its input price
    assert lookup(T, "claude-opus-5-5")[0].cache_read == pytest.approx(0.05 * 4)
    assert lookup(T, "claude-sonnet-5-5")[0].cache_read == pytest.approx(0.05 * 2)
    assert lookup(T, "claude-fable-5-1")[0].cache_read == pytest.approx(0.025 * 10)
    assert lookup(T, "claude-opus-4-8")[0].cache_read == pytest.approx(0.1 * 5)
