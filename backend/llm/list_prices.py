"""Maintained public list prices for API-equivalent Measurement reporting.

Source-controlled product data shared by every installation. It does not
participate in call-time billing: ``llm.pricing`` owns stored ``cost_usd``.
``llm.api_equivalent`` applies these rates to recorded tokens at read time.
"""

from __future__ import annotations

from dataclasses import dataclass


ANTHROPIC_PRICING = "https://platform.claude.com/docs/en/about-claude/pricing"
# The raw maintained page exposes the full Standard table, including rows the
# rendered page omits. It says short context is <=272K input tokens and long
# context is >272K input tokens.
OPENAI_PRICING = "https://developers.openai.com/api/docs/pricing.md"
OPENAI_LONG_CONTEXT_INPUT_TOKENS = 272_000
ALIBABA_PRICING = "https://www.alibabacloud.com/help/en/model-studio/model-pricing"
ALIBABA_FAMILY = "Alibaba"
# Alibaba's idle hours are 22:00-08:00 UTC+8, which is 14:00-24:00 UTC.
ALIBABA_IDLE_UTC_HOURS = (14, 24)
# Model Studio publishes a separate price list per region, and a workspace
# endpoint's host names its region: {WorkspaceId}.{region}.maas.aliyuncs.com.
ALIBABA_MAAS_SUFFIX = ".maas.aliyuncs.com"
ALIBABA_SINGAPORE = "alibaba:ap-southeast-1"
ALIBABA_BEIJING = "alibaba:cn-beijing"


def listing_for_host(host: str) -> str | None:
    """The regional price list whose cloud endpoint ``host`` is, or None for any other host.

    A list rate applies to an External endpoint only on its own provider's
    cloud, in a region whose list this table carries. The same open-weight
    identity served anywhere else (a self-hosted server, another cloud, an
    unlisted region) has no list rate.
    """
    host = host.lower().rstrip(".")
    if not host.endswith(ALIBABA_MAAS_SUFFIX):
        return None
    labels = host.removesuffix(ALIBABA_MAAS_SUFFIX).split(".")
    listing = f"alibaba:{labels[-1]}"
    return listing if len(labels) >= 2 and listing in LISTINGS else None


@dataclass(frozen=True)
class ListPrice:
    """One canonical identity's public USD rates per million tokens."""

    model: str
    input_per_million: float
    cache_write_per_million: float | None
    cache_read_per_million: float | None
    output_per_million: float
    source_url: str
    checked_date: str
    # ``None`` is a public absence, not a free bucket. Long vectors exist only
    # where the provider publishes a distinct Standard long-context rate.
    long_input_per_million: float | None = None
    long_cache_write_per_million: float | None = None
    long_cache_read_per_million: float | None = None
    long_output_per_million: float | None = None
    # A provider whose public rate changes by time of day lists a second vector
    # for its idle hours, which apply over ``idle_utc_hours`` [start, end) of
    # the call's own UTC hour. ``listing`` is the regional price list (see
    # ``listing_for_host``) an External rate belongs to, and ``provider`` the
    # Cost family it reports under.
    idle_input_per_million: float | None = None
    idle_cache_write_per_million: float | None = None
    idle_cache_read_per_million: float | None = None
    idle_output_per_million: float | None = None
    idle_utc_hours: tuple[int, int] | None = None
    provider: str | None = None
    listing: str | None = None


# Static rates, one dated public source per entry. They carry no price history:
# a changed rate replaces its row and checked date. A provider whose identity
# the ledger does not record is absent rather than guessed, as is an External
# identity missing from its provider's public list.
LIST_PRICES: tuple[ListPrice, ...] = (
    ListPrice("claude-opus-5-5", 4, 5, 0.20, 20, ANTHROPIC_PRICING, "2026-10-07"),
    ListPrice("claude-opus-5", 5, 6.25, 0.50, 25, ANTHROPIC_PRICING, "2026-10-07"),
    ListPrice("claude-sonnet-5-5", 2, 2.5, 0.20, 10, ANTHROPIC_PRICING, "2026-10-07"),
    ListPrice("claude-sonnet-5", 2, 2.5, 0.20, 10, ANTHROPIC_PRICING, "2026-10-07"),
    ListPrice("claude-fable-5-1", 10, 12.5, 0.25, 50, ANTHROPIC_PRICING, "2026-10-07"),
    ListPrice("claude-fable-5", 10, 12.5, 1, 50, ANTHROPIC_PRICING, "2026-10-07"),
    ListPrice("claude-haiku-4-5-20251001", 1, 1.25, 0.10, 5, ANTHROPIC_PRICING, "2026-10-07"),
    # OpenAI Standard short and long vectors. Long context is >272K input
    # tokens; cache-write ``None`` is unavailable publicly, not $0.
    ListPrice("gpt-6.1-sol", 2, 2.5, 0.10, 10, OPENAI_PRICING, "2026-10-07", 4, 5, 0.20, 15),
    ListPrice("gpt-6-sol", 2, 2.5, 0.20, 10, OPENAI_PRICING, "2026-10-07", 4, 5, 0.40, 15),
    ListPrice("gpt-5.6-sol", 4, 5, 0.40, 20, OPENAI_PRICING, "2026-10-07", 8, 10, 0.80, 30),
    ListPrice("gpt-5.6-terra", 2, 2.5, 0.20, 12, OPENAI_PRICING, "2026-10-07", 4, 5, 0.40, 18),
    ListPrice("gpt-5.6-luna", 0.2, 0.25, 0.02, 1.2, OPENAI_PRICING, "2026-10-07", 0.4, 0.5, 0.04, 1.8),
    ListPrice("gpt-6-luna", 0.1, 0.125, 0.01, 0.5, OPENAI_PRICING, "2026-10-07", 0.2, 0.25, 0.02, 0.75),
    ListPrice("gpt-6-astra", 10, 12.5, 1, 50, OPENAI_PRICING, "2026-10-07", 20, 25, 2, 75),
    ListPrice("gpt-5.4", 2.5, None, 0.25, 15, OPENAI_PRICING, "2026-10-07", 5, None, 0.5, 22.5),
    ListPrice("gpt-5.3-codex", 1.75, None, 0.175, 14, OPENAI_PRICING, "2026-10-07"),
    # gpt-5.3-codex-spark has no published exact-identity rate; it stays unpriced.
    # Alibaba Model Studio, one block per region list. Cached input is 10% of
    # the input rate for deepseek-v4.1-flash; qwen3.8-flash and glm-5.3 publish
    # no cached-input rate, and none publishes a cache-write rate.
    # Singapore (International deployment scope).
    ListPrice("deepseek-v4.1-flash", 0.3, None, 0.03, 1.2, ALIBABA_PRICING, "2026-10-08",
              idle_input_per_million=0.15, idle_cache_read_per_million=0.015, idle_output_per_million=0.6,
              idle_utc_hours=ALIBABA_IDLE_UTC_HOURS, provider=ALIBABA_FAMILY, listing=ALIBABA_SINGAPORE),
    ListPrice("qwen3.8-flash", 0.15, None, None, 0.47, ALIBABA_PRICING, "2026-10-08",
              provider=ALIBABA_FAMILY, listing=ALIBABA_SINGAPORE),
    ListPrice("glm-5.3", 1.4, None, None, 4.4, ALIBABA_PRICING, "2026-10-08",
              provider=ALIBABA_FAMILY, listing=ALIBABA_SINGAPORE),
    # China (Beijing).
    ListPrice("deepseek-v4.1-flash", 0.283, None, 0.0283, 1.131, ALIBABA_PRICING, "2026-10-08",
              idle_input_per_million=0.141, idle_cache_read_per_million=0.0141, idle_output_per_million=0.565,
              idle_utc_hours=ALIBABA_IDLE_UTC_HOURS, provider=ALIBABA_FAMILY, listing=ALIBABA_BEIJING),
    ListPrice("qwen3.8-flash", 0.113, None, None, 0.382, ALIBABA_PRICING, "2026-10-08",
              provider=ALIBABA_FAMILY, listing=ALIBABA_BEIJING),
    ListPrice("glm-5.3", 1.131, None, None, 3.957, ALIBABA_PRICING, "2026-10-08",
              provider=ALIBABA_FAMILY, listing=ALIBABA_BEIJING),
)
LISTINGS = frozenset(price.listing for price in LIST_PRICES if price.listing is not None)
