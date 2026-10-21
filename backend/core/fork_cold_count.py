"""One owner for counting cold fork input, by the writer's tokenizer.

The cold admission, its send-time capacity check, and every harness fork must
agree on what "input tokens" means, or an admitted fold overflows or a possible
fold is refused. Counting serialized bytes (the prior bound) overcounts real
prompts by roughly 2x for Claude and 4x for the Codex surface, which refuses
folds that would have fit.

Families, and why each bound is sound:

- **GPT-5** writers tokenize with ``o200k_base``, which is bundled, so the count
  is exact.
- **Claude** ships no public tokenizer. Recorded provider receipts for Claude 5
  cold checkpoints measured 1.281-1.663 provider tokens per ``o200k`` token, so
  5/3 bounds the observed range; byte-level BPE can never emit more tokens than
  UTF-8 bytes, so the byte length caps it for dense or multilingual text where
  the ratio would otherwise overshoot.
- **Unknown** writers keep the byte bound, the only count proved without a
  tokenizer.

This owner counts cold input only. Warm forks are counted by the provider's own
reported occupancy, and live send preflight keeps its own estimator.
"""

from __future__ import annotations

import json

# Recorded Claude-5 cold receipts peaked at 1.663 provider tokens per o200k
# token; 5/3 covers that range without inventing a new margin.
CLAUDE_TOKENS_PER_O200K_NUMERATOR = 5
CLAUDE_TOKENS_PER_O200K_DENOMINATOR = 3

GPT5_FAMILY = "gpt-5"
CLAUDE_FAMILY = "claude"
UNKNOWN_FAMILY = "unknown"


def serialize_cold_payload(payload: object) -> str:
    """Render the exact canonical text a cold count is taken over."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def _identity_terms(writer: object) -> tuple[str, ...]:
    """Collect the model identity strings that can name a tokenizer family."""
    if isinstance(writer, str):
        return (writer,)
    terms = (
        getattr(writer, "expected_reported_model", None),
        getattr(writer, "transport_model", None),
        getattr(writer, "catalog_id", None),
    )
    return tuple(str(term) for term in terms if term)


def _model_names(term: str) -> tuple[str, ...]:
    """Yield the term and its last path/prefix segment, lowercased."""
    lowered = term.strip().lower()
    tail = lowered.rsplit("/", 1)[-1]
    return (lowered, tail)


def tokenizer_family(writer: object) -> str:
    """Name the tokenizer family of a writer target or model identity."""
    for term in _identity_terms(writer):
        for name in _model_names(term):
            if name.startswith(GPT5_FAMILY) or name.startswith("openai-gpt-5"):
                return GPT5_FAMILY
            if CLAUDE_FAMILY in name:
                return CLAUDE_FAMILY
    return UNKNOWN_FAMILY


def _o200k_tokens(text: str) -> int | None:
    """Count with the bundled o200k encoding, or report its absence."""
    from core.token_estimation import _model_encoding

    encoding = _model_encoding("gpt-5")
    if encoding is None:
        return None
    return len(encoding.encode(text, disallowed_special=()))


def cold_input_tokens(payload: object, writer: object) -> int:
    """Count one cold payload as its writer's transport will tokenize it.

    ``writer`` is an execution target or a catalog model id. The result never
    undercounts the family's measured behavior, so an admitted fold does not
    overflow the window it was admitted against.
    """
    text = serialize_cold_payload(payload)
    byte_bound = len(text.encode("utf-8"))
    family = tokenizer_family(writer)
    if family == UNKNOWN_FAMILY:
        return byte_bound
    exact = _o200k_tokens(text)
    if exact is None:
        # The bundled encoding is the reason a family is claimed at all; without
        # it the only proved bound is bytes.
        return byte_bound
    if family == GPT5_FAMILY:
        return exact
    scaled = -(
        -exact * CLAUDE_TOKENS_PER_O200K_NUMERATOR
        // CLAUDE_TOKENS_PER_O200K_DENOMINATOR
    )
    return min(byte_bound, scaled)
