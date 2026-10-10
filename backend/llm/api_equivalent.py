"""Read-time API-equivalent dollars for recorded token buckets.

The pure counterpart of ``llm.pricing``: where that module records what a call
cost when it ran, this prices the retained token buckets at today's public list
prices. It never writes ``cost_usd`` and owns no price history.
"""

from __future__ import annotations

from typing import cast

from llm.list_prices import LIST_PRICES, OPENAI_LONG_CONTEXT_INPUT_TOKENS, ListPrice
from llm.pricing import is_external_model, is_local_model

# A price is one identity on one list: an External identity can appear on
# several regional lists at different rates, a harness identity on none.
_PRICES = {(price.model, price.listing): price for price in LIST_PRICES}
if len(_PRICES) != len(LIST_PRICES):
    raise ValueError("LIST_PRICES lists an identity twice on the same list")


def _price(model: str, listing: str | None) -> ListPrice | None:
    """The list price a recorded selector bills at, or None when unknown.

    Only a literal identity prices: a Claude Code alias such as ``opus`` names
    whatever the catalog resolved at call time, which the ledger did not keep.
    ``listing`` is the regional price list an External selector's endpoint
    selects (``llm.list_prices.listing_for_host``), None otherwise.
    """
    harness, _, selector = model.partition("/")
    if harness.startswith("external@"):
        # An External endpoint prices only on the list its own cloud host selects.
        return _PRICES.get((selector, listing)) if listing is not None else None
    selector = selector.removesuffix("[1m]")
    if harness == "codex" or (harness == "claude-code" and selector.startswith("claude-")):
        return _PRICES.get((selector, None))
    return None


def price_identity(model: str, listing: str | None = None) -> str | None:
    """The exact public identity a recorded selector names, or None when unknown."""
    price = _price(model, listing)
    return price.model if price is not None else None


def is_not_api_priced(model: str, listing: str | None = None) -> bool:
    """Local and External usage with no public list rate for its served identity."""
    return (is_local_model(model) or is_external_model(model)) and _price(model, listing) is None


def _known(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_long_context(prompt: int, cache_write: int, cache_read: int) -> bool:
    """Whether OpenAI bills the call at its Standard long-context vector.

    The official boundary is input only: output tokens do not move a call into
    the long tier. The ledger buckets are disjoint, so all three input buckets
    contribute exactly once.
    """
    return prompt + cache_write + cache_read > OPENAI_LONG_CONTEXT_INPUT_TOKENS


def has_public_long_context_price(model: str, listing: str | None = None) -> bool:
    """Whether the recorded identity has a distinct published Standard long vector."""
    price = _price(model, listing)
    return price is not None and price.long_input_per_million is not None


def _rates(price: ListPrice, *, long_context: bool,
           idle_hours: bool = False) -> tuple[float | None, float | None, float | None, float | None]:
    """Choose the published Standard vector, falling back only when no long or idle vector exists."""
    if idle_hours and price.idle_input_per_million is not None:
        return (
            price.idle_input_per_million,
            price.idle_cache_write_per_million,
            price.idle_cache_read_per_million,
            price.idle_output_per_million,
        )
    if long_context and price.long_input_per_million is not None:
        return (
            price.long_input_per_million,
            price.long_cache_write_per_million,
            price.long_cache_read_per_million,
            price.long_output_per_million,
        )
    return (
        price.input_per_million,
        price.cache_write_per_million,
        price.cache_read_per_million,
        price.output_per_million,
    )


def has_unpublished_cache_rate(model: str, cache_write: int, cache_read: int, *, idle_hours: bool = False,
                               listing: str | None = None) -> bool:
    """Whether a positive cache bucket has no published rate for the recorded identity."""
    price = _price(model, listing)
    if price is None:
        return False
    _, write_rate, read_rate, _ = _rates(price, long_context=False, idle_hours=idle_hours)
    return (cache_write > 0 and write_rate is None) or (cache_read > 0 and read_rate is None)


def api_equivalent(model: str, prompt: int | None, completion: int | None,
                   cache_write: int | None, cache_read: int | None, *,
                   long_context: bool = False, idle_hours: bool = False,
                   listing: str | None = None) -> float | None:
    """Dollars at the verified Standard list rate, or ``None`` when unknown.

    The buckets are disjoint ledger columns. A positive bucket without a
    published price is Unknown, never silently free. ``long_context`` is
    determined per call before aggregation because the public tier threshold
    applies to input tokens for an individual call; ``idle_hours`` selects a
    provider's idle-hours vector where one is published; ``listing`` is as
    for ``_price``.
    """
    price = _price(model, listing)
    buckets = (prompt, completion, cache_write, cache_read)
    if price is None or not all(_known(bucket) for bucket in buckets):
        return None
    validated_buckets: tuple[int, int, int, int] = (
        cast(int, prompt), cast(int, completion), cast(int, cache_write), cast(int, cache_read))
    input_rate, cache_write_rate, cache_read_rate, output_rate = _rates(
        price, long_context=long_context, idle_hours=idle_hours)
    rates = (input_rate, output_rate, cache_write_rate, cache_read_rate)
    if any(rate is None and bucket > 0 for bucket, rate in zip(validated_buckets, rates, strict=True)):
        return None
    return sum(bucket * (rate or 0) for bucket, rate in zip(validated_buckets, rates, strict=True)) / 1_000_000
