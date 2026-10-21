"""The API-equivalent cost of one request (docs/design.md, "Cost model").

    cost = input x p_in + write_5m x p_w5m + write_1h x p_w1h + read x p_read
         + output x p_out + web_searches x p_search

Each category uses the model's own rate from the price table (cache reads are 0.1x
input on most models but 0.05x on Opus and Sonnet 5.5 and 0.025x on Fable 5.1).
On top: a long-context rate set when the prompt is above the model's threshold
(the whole request at those rates), the fast-mode multiplier when `speed` is
"fast", and the geo multiplier for `inference_geo`, each on every token category.
"""

from __future__ import annotations

from capcom.cost.prices import lookup
from capcom.types import CostBreakdown, PriceTable, Request, Usage

PER_MTOK = 1_000_000


def price_usage(
    model: str,
    usage: Usage,
    table: PriceTable,
    speed: str | None = None,
    inference_geo: str | None = None,
) -> CostBreakdown:
    """The cost of `usage` on `model` (what `price` does, without a Request)."""
    p, exact = lookup(table, model)
    rate_in, rate_out = p.input, p.output
    rate_w5, rate_w1, rate_read = p.cache_write_5m, p.cache_write_1h, p.cache_read
    lc = p.long_context
    if lc is not None and usage.context > lc.threshold_tokens:
        rate_in, rate_out = lc.input, lc.output
        rate_w5, rate_w1, rate_read = lc.cache_write_5m, lc.cache_write_1h, lc.cache_read
    mult = 1.0
    if speed == "fast":
        if p.fast_multiplier is not None:
            mult *= p.fast_multiplier
        elif p.input > 0:
            exact = False  # fast mode on a model without a listed fast price
    if inference_geo:
        mult *= table.geo_multipliers.get(inference_geo, 1.0)
    scale = mult / PER_MTOK
    web = (
        usage.web_searches * table.web_search_per_1k + usage.web_fetches * table.web_fetch_per_1k
    ) / 1000
    if p.input == 0 and p.output == 0:
        web = 0.0  # unpriced (e.g. `<synthetic>`): no cost at all
    return CostBreakdown(
        input=usage.input * rate_in * scale,
        cache_write=(usage.cache_write_5m * rate_w5 + usage.cache_write_1h * rate_w1) * scale,
        cache_read=usage.cache_read * rate_read * scale,
        output=usage.output * rate_out * scale,
        web=web,
        priced_as=p.model,
        exact_price=exact,
    )


def price(request: Request, table: PriceTable) -> CostBreakdown:
    """The API-equivalent cost of one request, in USD."""
    return price_usage(
        request.model,
        request.usage,
        table,
        speed=request.speed,
        inference_geo=request.inference_geo,
    )
