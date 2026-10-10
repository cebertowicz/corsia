"""Model pricing and canonical durable-write cost policy."""

from __future__ import annotations

from datetime import datetime, timezone
import math

from loguru import logger

from infra.database import get_db
from llm.resolution import Harness

_LOCAL_PREFIX = "omlx@"          # oMLX (the managed engine)
_EXTERNAL_PREFIX = "external@"   # External providers (user-owned endpoints)
_SUBSCRIPTION_HARNESSES: frozenset[Harness] = frozenset({"claude-code", "codex"})
_HISTORICAL_SUBSCRIPTION_SOURCES = frozenset({
    "external_harness:claude-code",
    "external_harness:codex",
})
_HISTORICAL_SUBSCRIPTION_MODEL_PREFIXES = ("claude-code/", "codex/")


def is_subscription_usage(harness: Harness | None) -> bool:
    """Whether this resolved harness has no per-call subscription charge."""
    return harness in _SUBSCRIPTION_HARNESSES


def is_historical_subscription_usage(model: str | None, source: str | None) -> bool:
    """Classify persisted subscription evidence without resolving current catalog state.

    Canonical historical model identities are sufficient on their own. A prior
    external-harness source is also durable evidence when a row stored only its
    provider transport alias.
    """
    return bool(
        (model and model.startswith(_HISTORICAL_SUBSCRIPTION_MODEL_PREFIXES))
        or source in _HISTORICAL_SUBSCRIPTION_SOURCES
    )


def is_local_model(model: str | None) -> bool:
    """True if ``model`` is served by the managed engine (oMLX): never billed.

    The cost-classification predicate for ``local`` (zero dollars, intentional).
    External endpoints are not local: they may front a paid cloud API, so their
    charge is unknown until the endpoint is marked (see ``is_external_model``).
    """
    return model is not None and model.startswith(_LOCAL_PREFIX)


def is_external_model(model: str | None) -> bool:
    """True if ``model`` is served by a user-added External endpoint."""
    return model is not None and model.startswith(_EXTERNAL_PREFIX)


def recorded_cost(
    model: str,
    usage,
    *,
    harness: Harness | None = None,
) -> float | None:
    """Retain chargeable call-time dollars for the canonical usage ledger.

    Subscription harnesses have no per-call charge, while the selected local
    model is intentionally free. External endpoints report no charge on the
    wire, so the charge is unknown (NULL) and classified at read time from the
    billing set on the endpoint. Local requests use an OpenAI wire alias, so the
    ledger caller supplies the selected catalog model. Never reprice cloud usage.
    """
    if is_subscription_usage(harness) or is_external_model(model):
        return None
    return 0.0 if is_local_model(model) else usage.get("cost_usd")


def calculate_cost(
    model: str,
    usage: dict,
    call_time: str | None = None,
) -> float | None:
    """Calculate a live call's charge, intentional local zero, or unknown."""
    if is_local_model(model):
        return 0.0
    if is_external_model(model):
        return None
    buckets = (
        "prompt_tokens", "completion_tokens", "cache_creation_tokens", "cache_read_tokens",
    )
    if any(
        isinstance(usage.get(key), bool)
        or not isinstance(usage.get(key), (int, float))
        or not math.isfinite(usage[key]) or usage[key] < 0
        for key in buckets
    ):
        return None
    if not call_time:
        call_time = datetime.now(timezone.utc).isoformat()

    try:
        db = get_db()
        row = db.execute(
            "SELECT input_cost_per_token, output_cost_per_token, "
            "cache_creation_cost_per_token, cache_read_cost_per_token "
            "FROM model_pricing "
            "WHERE model = ? AND effective_date <= ? "
            "ORDER BY effective_date DESC LIMIT 1",
            (model, call_time),
        ).fetchone()
        db.close()
    except Exception as e:
        logger.warning(f"pricing: failed to look up rates for {model}: {e}")
        return None

    if not row:
        return None

    # This function requires the DISJOINT contract from infra.provider_usage:
    # ``prompt_tokens`` is the uncached prompt, and the cache buckets are
    # separate quantities rather than subsets of it. No bucket is deducted
    # from another. The previous code subtracted the cache buckets from
    # ``prompt_tokens``, which billed real uncached tokens at zero whenever
    # cache traffic exceeded the fresh prompt -- the normal warm-thread case.
    #
    # That precondition holds because this is only ever called with LIVE usage
    # from the response being priced, and every live producer normalizes
    # through infra.provider_usage first. Cost is then stored on the row, and
    # every consumer sums the stored ``cost_usd`` column rather than repricing
    # from token counts.
    #
    # It does NOT hold for rows stored before that contract: those carry a
    # cache-INCLUSIVE ``prompt_tokens`` beside a populated
    # ``cache_read_tokens``, so the same tokens appear in two columns. Passing
    # such a row here would bill its cache traffic twice. Nothing does today,
    # and nothing should start without first repairing those rows -- tracked
    # in issue #1215, which also carries the NULL-vs-zero schema change the
    # repair depends on. Until it lands, `llm_usage` holds two incompatible
    # meanings and a SUM spanning that boundary is not a real number.
    rate_fields = (
        "input_cost_per_token", "output_cost_per_token",
        "cache_creation_cost_per_token", "cache_read_cost_per_token",
    )
    cost = 0.0
    for bucket, rate_field in zip(buckets, rate_fields, strict=True):
        count, rate = usage[bucket], row[rate_field]
        if count == 0:
            continue
        if (isinstance(rate, bool) or not isinstance(rate, (int, float))
                or not math.isfinite(rate) or rate < 0):
            return None
        cost += count * rate
    return round(cost, 10)
