"""Canonical provider token accounting.

One disjoint measurement model for every provider, so rows in ``llm_usage``
are comparable and summable across harnesses.

Two provider facts silently corrupt totals unless they are handled once, here:

**Cache counts are not reported the same way.**

* Anthropic (Claude Code) reports ``input_tokens`` as the tokens that were
  neither read from nor written to the cache. ``cache_read_input_tokens`` and
  ``cache_creation_input_tokens`` are *disjoint* from it.
* The Codex app-server reports ``inputTokens`` with ``cachedInputTokens`` as a
  *subset*, so the uncached remainder is the difference between them.
* OpenAI-compatible endpoints report ``prompt_tokens`` with
  ``prompt_tokens_details.cached_tokens`` as a subset, like Codex.

Adding a subset, or failing to add a disjoint count, moves a turn's total by
the size of its cache traffic -- which on a warm thread is most of the prompt.
Each adapter below converts its provider's shape into the same disjoint
counts, and every consumer reads those instead of provider-native fields.

**A turn is many model calls, and the ledger records the turn's volume.**

Neither provider hands us that number directly:

* Claude Code emits one usage payload per call, so the turn's volume is the
  *sum* of them. Keeping only the final call undercounts every multi-call turn.
* The Codex app-server emits ``thread/tokenUsage/updated`` carrying a
  ``total`` that accumulates over the whole *thread* (it advances by each
  ``last``), so recording ``total`` per turn re-bills every earlier turn. The
  turn's volume is the *delta* of that counter across the turn.

Volume is therefore accumulated (``+``) or differenced (``-``), never sampled.

Context occupancy is a different question from volume -- "how full is the
window right now" versus "how many tokens did this turn process". Occupancy is
the *latest* call's ``input_total``; volume is the accumulated ``total``. They
must never share a field.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


def _count(value: Any) -> int | None:
    """Return a nonnegative integral count, or None when unusable.

    Provider telemetry arrives as untrusted JSON: booleans, strings, NaN, and
    negatives all appear in practice and none of them are token counts.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return None
    count = int(value)
    return count if count >= 0 else None


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Disjoint token counts for one or more model calls.

    The four fields never overlap, so ``total`` is their sum and instances can
    be added across the calls of a turn or differenced across a cumulative
    counter.
    """

    input_uncached: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    #: Bucket names the provider never reported. ``0`` means measured zero;
    #: membership here means unknown, and the ledger stores NULL. Codex has no
    #: cache-creation concept and may omit ``outputTokens`` entirely, so
    #: storing those as zero would assert a measurement nobody made -- the
    #: unknown-as-zero conflation this contract exists to remove. Arithmetic
    #: still treats them as zero, since an absent count contributes nothing.
    unreported: frozenset[str] = frozenset()

    @property
    def total(self) -> int:
        """Every token this usage accounts for -- the ledger's volume."""
        return self.input_uncached + self.cache_read + self.cache_write + self.output

    @property
    def input_total(self) -> int:
        """The prompt side only -- what occupies the context window."""
        return self.input_uncached + self.cache_read + self.cache_write

    def __add__(self, other: TokenUsage) -> TokenUsage:
        """Accumulate another call's usage into this one."""
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            input_uncached=self.input_uncached + other.input_uncached,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            output=self.output + other.output,
            # A bucket is only known for the whole if it was known for every
            # part; one unreported call makes the accumulated bucket unknown.
            unreported=self.unreported | other.unreported,
        )

    def __sub__(self, other: TokenUsage) -> TokenUsage:
        """Difference two readings of a cumulative counter.

        Clamped at zero per field: a provider that resets or reorders its
        counter must not produce negative usage.
        """
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            input_uncached=max(0, self.input_uncached - other.input_uncached),
            cache_read=max(0, self.cache_read - other.cache_read),
            cache_write=max(0, self.cache_write - other.cache_write),
            output=max(0, self.output - other.output),
            unreported=self.unreported | other.unreported,
        )

    def ledger_row(self) -> dict[str, int | None]:
        """Render the ``llm_usage`` token columns for this usage.

        ``prompt_tokens`` carries the uncached prompt only; the cache columns
        carry the rest of the prompt. They stay disjoint so a SUM over the
        table is the true token volume.

        A bucket the provider never reported is written as NULL, and so is
        ``total_tokens`` when any component is unknown: a total assembled from
        an unknown part would contradict its own columns.
        """
        columns = {
            "prompt_tokens": ("input_uncached", self.input_uncached),
            "completion_tokens": ("output", self.output),
            "cache_creation_tokens": ("cache_write", self.cache_write),
            "cache_read_tokens": ("cache_read", self.cache_read),
        }
        row: dict[str, int | None] = {
            column: (None if field in self.unreported else value)
            for column, (field, value) in columns.items()
        }
        row["effective_input_tokens"] = self.input_total
        row["total_tokens"] = None if self.unreported else self.total
        return row


def from_anthropic(usage: Mapping[str, Any] | None) -> TokenUsage | None:
    """Normalize one Anthropic/Claude Code usage payload.

    ``input_tokens`` already excludes both cache counts, so the three map
    straight onto the disjoint fields.
    """
    if not isinstance(usage, Mapping):
        return None
    input_uncached = _count(usage.get("input_tokens"))
    if input_uncached is None:
        return None
    return TokenUsage(
        input_uncached=input_uncached,
        cache_read=_count(usage.get("cache_read_input_tokens")) or 0,
        cache_write=_count(usage.get("cache_creation_input_tokens")) or 0,
        output=_count(usage.get("output_tokens")) or 0,
        unreported=frozenset({"output"}) if _count(usage.get("output_tokens")) is None else frozenset(),
    )


def from_codex(usage: Mapping[str, Any] | None) -> TokenUsage | None:
    """Normalize one Codex app-server token-usage block (``last`` or ``total``).

    ``cachedInputTokens`` is a subset of ``inputTokens``, so the uncached
    remainder is their difference. Codex reports no cache-creation count.
    """
    if not isinstance(usage, Mapping):
        return None
    input_tokens = _count(usage.get("inputTokens"))
    if input_tokens is None:
        return None
    output = _count(usage.get("outputTokens"))
    # ``cache_write`` stays a measured zero: the Codex app-server has no
    # cache-creation concept at all, so zero is the true count rather than an
    # absent measurement. ``outputTokens`` is different -- some payloads omit
    # it, and calling that zero output would assert a measurement nobody made.
    return _subset_convention(
        input_tokens,
        _count(usage.get("cachedInputTokens")) or 0,
        output or 0,
        unreported=frozenset() if output is not None else frozenset({"output"}),
    )


def unmeasured_ledger_row() -> dict[str, None]:
    """Render ledger token columns for a call whose usage was not measured.

    Every count is explicitly ``None`` so the row records "a call happened and
    its tokens are unknown". A zeroed row would instead assert that a real
    call was free, which is the one thing an accounting ledger must never do.
    """
    return {
        "prompt_tokens": None,
        "completion_tokens": None,
        "cache_creation_tokens": None,
        "cache_read_tokens": None,
        "total_tokens": None,
    }


def _subset_convention(
    prompt_tokens: int,
    cache_read: int,
    output: int,
    *,
    unreported: frozenset[str] = frozenset(),
) -> TokenUsage:
    """Build usage where ``cache_read`` is already inside ``prompt_tokens``."""
    return TokenUsage(
        input_uncached=max(0, prompt_tokens - cache_read),
        cache_read=min(cache_read, prompt_tokens),
        output=output,
        unreported=unreported,
    )


def reports_cache_outside_prompt(
    model: str | None, *, api_base: str | None = None,
) -> bool:
    """Return whether this response reports cache counts OUTSIDE ``prompt_tokens``.

    Anthropic's native API does; OpenAI-compatible endpoints (including oMLX)
    do not. The telemetry shape cannot decide it -- litellm *mirrors*
    Anthropic's ``cache_read_input_tokens`` into
    ``prompt_tokens_details.cached_tokens``, so both field styles carry the
    same positive number for Anthropic, and neither attribute presence nor a
    positive value distinguishes the conventions.

    The convention belongs to the ENDPOINT the request actually reached, not
    to the model family, so the endpoint is checked first:

    * An explicit ``api_base`` means the request went to an OpenAI-compatible
      server -- a gateway, a LiteLLM proxy, or a local runtime -- whatever the
      model id says. Claude served through OpenRouter reports the subset form
      while still calling itself ``anthropic/claude-...``, so trusting the
      name there produces the ~90% overstatement this module exists to
      prevent.
    * Without one, litellm reached the vendor's native API directly, and the
      model id identifies the vendor. Every Anthropic id carries the family
      name, including Claude via Bedrock or Vertex where the provider is not
      ``anthropic``.

    Anything else is treated as OpenAI-compatible, which is the convention
    those endpoints implement.
    """
    if api_base:
        return False
    if not model:
        return False
    name = model.lower()
    return "claude" in name or "anthropic" in name


def from_litellm(
    usage: Any, *, model: str | None = None, api_base: str | None = None,
) -> TokenUsage | None:
    """Normalize a litellm/OpenAI-SDK usage object into disjoint counts.

    One code path serves several vendors, and they disagree about whether the
    cache counts are already inside ``prompt_tokens``:

    * Anthropic reports them **outside** it (disjoint).
    * OpenAI-compatible endpoints, including oMLX, report
      ``prompt_tokens_details.cached_tokens`` **inside** it (a subset).

    Reading a subset as disjoint makes a warm oMLX turn look ~90% larger than
    it was; reading disjoint as a subset undercounts Anthropic by its cache
    traffic. ``model`` and ``api_base`` select the convention -- see
    ``reports_cache_outside_prompt`` for why the payload cannot.

    Returns ``None`` when no usable prompt count is present. Callers must
    record that as unmeasured; a zeroed row would bill a real call as free.
    """
    if usage is None:
        return None
    prompt_tokens = _count(getattr(usage, "prompt_tokens", None))
    if prompt_tokens is None:
        return None
    reported_output = _count(getattr(usage, "completion_tokens", None))
    output = reported_output or 0
    unreported = frozenset({"output"}) if reported_output is None else frozenset()

    details = getattr(usage, "prompt_tokens_details", None)
    cached = _count(getattr(details, "cached_tokens", None)) or 0
    disjoint_read = _count(getattr(usage, "cache_read_input_tokens", None)) or 0
    disjoint_write = (
        _count(getattr(usage, "cache_creation_input_tokens", None)) or 0
    )

    if prompt_tokens == 0 and cached == 0 and disjoint_read == 0 and disjoint_write == 0:
        # Nothing was reported on the input side. litellm defaults
        # ``prompt_tokens`` to 0 rather than leaving it unset, so this is how an
        # absent measurement arrives -- and no real completion has a zero-token
        # prompt. Report it as unmeasured instead of billing the call as free.
        return None

    if reports_cache_outside_prompt(model, api_base=api_base):
        return TokenUsage(
            input_uncached=prompt_tokens,
            cache_read=disjoint_read or cached,
            cache_write=disjoint_write,
            output=output,
            unreported=unreported,
        )

    if cached > prompt_tokens:
        # A subset cannot exceed its superset. Whatever the vendor is, this
        # payload is reporting disjoint counts, so honour the arithmetic over
        # the vendor guess rather than clamping real tokens away.
        #
        # This branch is reachable for a real case -- an Anthropic-convention
        # payload arriving with api_base set, i.e. Claude through a gateway --
        # and that vendor does report cache creation, so carry it. Omitting it
        # silently dropped those tokens from the bill.
        return TokenUsage(
            input_uncached=prompt_tokens,
            cache_read=cached,
            cache_write=disjoint_write,
            output=output,
            unreported=unreported,
        )

    return _subset_convention(prompt_tokens, cached, output, unreported=unreported)


def from_openai(usage: Mapping[str, Any] | None) -> TokenUsage | None:
    """Normalize one OpenAI-compatible usage mapping.

    ``prompt_tokens_details.cached_tokens`` is a subset of ``prompt_tokens``,
    as with Codex. Provided for payloads that arrive as plain mappings rather
    than SDK objects; ``from_litellm`` handles the SDK shape.
    """
    if not isinstance(usage, Mapping):
        return None
    prompt_tokens = _count(usage.get("prompt_tokens"))
    if prompt_tokens is None:
        return None
    details = usage.get("prompt_tokens_details")
    cache_read = 0
    if isinstance(details, Mapping):
        cache_read = _count(details.get("cached_tokens")) or 0
    return _subset_convention(
        prompt_tokens, cache_read, _count(usage.get("completion_tokens")) or 0,
        unreported=frozenset({"output"}) if _count(usage.get("completion_tokens")) is None else frozenset(),
    )


def checkpoint_call_receipts(usage: dict[str, object]) -> dict[str, object]:
    """Wrap a legacy single checkpoint receipt without double-wrapping calls."""
    calls = usage.get("calls")
    if (
        set(usage) == {"calls"}
        and isinstance(calls, list)
        and all(isinstance(call, dict) for call in calls)
    ):
        return usage
    return {"calls": [usage]}


def fork_receipt(usage: TokenUsage | None) -> dict[str, int | None] | None:
    """Render measured fork usage using the ledger's disjoint token columns.

    Unmeasured calls return ``None``. This normalizes new receipts only; it
    does not decode historical provider-specific usage shapes.
    """
    return None if usage is None else usage.ledger_row()
