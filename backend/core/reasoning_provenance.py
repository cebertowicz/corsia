"""Provider reasoning provenance: normalization, merge and replay eligibility.

Shared by every harness that persists or replays model reasoning.
"""

from __future__ import annotations

from typing import Any

from core.message_row import parse_meta

_REASONING_REPRESENTATION_KINDS = frozenset({
    "reasoning_content",
    "anthropic_thinking",
    "codex_reasoning_summary",
    "opaque",
    "unknown",
})
_REASONING_SOURCE_HARNESSES = frozenset({
    "native",
    "claude-code",
    "codex",
    "unknown",
})


def _nonempty_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _ordered_nonempty_strings(value: Any) -> list[str]:
    """Return ordered, de-duplicated non-empty strings from provider evidence."""
    values = value if isinstance(value, (list, tuple)) else []
    result: list[str] = []
    seen: set[str] = set()
    for candidate in values:
        normalized = _nonempty_string(candidate)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _evidence_list(
    plural: Any, raw_plural: Any, singular: Any, raw_singular: Any,
) -> list[str]:
    """Ordered evidence: the plural argument or stored list, else the one singleton."""
    values = _ordered_nonempty_strings(plural if plural is not None else raw_plural)
    if not values:
        single = _nonempty_string(singular) or _nonempty_string(raw_singular)
        if single is not None:
            values = [single]
    return values


def _normalized_anthropic_blocks(value: Any) -> list[dict[str, str]]:
    """Keep only complete provider-native Anthropic reasoning blocks."""
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, str]] = []
    for candidate in value:
        if not isinstance(candidate, dict):
            continue
        block_type = candidate.get("type")
        if block_type == "thinking":
            thinking = candidate.get("thinking")
            if not isinstance(thinking, str):
                continue
            block = {"type": "thinking", "thinking": thinking}
            signature = _nonempty_string(candidate.get("signature"))
            if signature is not None:
                block["signature"] = signature
            result.append(block)
        elif block_type == "redacted_thinking":
            data = _nonempty_string(candidate.get("data"))
            if data is not None:
                result.append({"type": "redacted_thinking", "data": data})
    return result


def normalize_reasoning_provenance(
    provenance: Any = None,
    *,
    source_harness: str | None = None,
    source_provider: str | None = None,
    source_effective_model: str | None = None,
    representation_kind: str | None = None,
    native_signature: str | None = None,
    native_reference: str | None = None,
    native_signatures: list[str] | tuple[str, ...] | None = None,
    native_references: list[str] | tuple[str, ...] | None = None,
    native_blocks: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    """Return the canonical provenance shape for persisted reasoning.

    Unknown or legacy input stays explicitly unknown; this function never
    guesses a provider or representation from model names or reasoning text.
    """
    raw = provenance if isinstance(provenance, dict) else {}
    harness = _nonempty_string(source_harness) or _nonempty_string(
        raw.get("source_harness")
    )
    if harness not in _REASONING_SOURCE_HARNESSES:
        harness = "unknown"
    provider = _nonempty_string(source_provider) or _nonempty_string(
        raw.get("source_provider")
    )
    if provider is None:
        provider = "unknown"
    kind = _nonempty_string(representation_kind) or _nonempty_string(
        raw.get("representation_kind")
    )
    if kind not in _REASONING_REPRESENTATION_KINDS:
        kind = "unknown"

    normalized = {
        "source_harness": harness,
        "source_provider": provider,
        "representation_kind": kind,
    }
    effective_model = _nonempty_string(source_effective_model) or _nonempty_string(
        raw.get("source_effective_model")
    )
    if effective_model is not None:
        normalized["source_effective_model"] = effective_model
    signatures = _evidence_list(
        native_signatures,
        raw.get("native_signatures"),
        native_signature,
        raw.get("native_signature"),
    )
    if signatures:
        normalized["native_signatures"] = signatures
        if len(signatures) == 1:
            normalized["native_signature"] = signatures[0]

    references = _evidence_list(
        native_references,
        raw.get("native_references"),
        native_reference,
        raw.get("native_reference"),
    )
    if references:
        normalized["native_references"] = references
        if len(references) == 1:
            normalized["native_reference"] = references[0]
    blocks = _normalized_anthropic_blocks(
        native_blocks if native_blocks is not None else raw.get("native_blocks")
    )
    if blocks:
        normalized["native_blocks"] = blocks
    return normalized


def replayable_anthropic_thinking(
    provenance: Any,
    *,
    destination_provider: str | None,
    destination_effective_model: str | None,
) -> bool:
    """Return whether model-bound Anthropic reasoning may be replayed.

    Catalog labels and harness families are deliberately absent from this
    decision. Missing legacy identity is unknown and therefore not replayable.
    """
    normalized = normalize_reasoning_provenance(provenance)
    source_provider = normalized.get("source_provider")
    source_model = normalized.get("source_effective_model")
    return (
        normalized.get("representation_kind") == "anthropic_thinking"
        and isinstance(source_provider, str)
        and source_provider not in {"", "unknown"}
        and isinstance(source_model, str)
        and source_model not in {"", "unknown"}
        and destination_provider == source_provider
        and destination_effective_model == source_model
    )


def anthropic_thinking_blocks_for_replay(
    provenance: Any,
    *,
    destination_provider: str | None,
    destination_effective_model: str | None,
) -> list[dict[str, str]]:
    """Return only provider-valid Anthropic blocks for exact-identity replay.

    Plain thinking requires its provider signature. Redacted blocks are already
    opaque provider state and remain replayable without a separate signature.
    """
    if not replayable_anthropic_thinking(
        provenance,
        destination_provider=destination_provider,
        destination_effective_model=destination_effective_model,
    ):
        return []
    normalized = normalize_reasoning_provenance(provenance)
    return [
        dict(block)
        for block in normalized.get("native_blocks", [])
        if (
            block.get("type") == "redacted_thinking"
            or (
                block.get("type") == "thinking"
                and bool(_nonempty_string(block.get("signature")))
            )
        )
    ]


def merge_reasoning_provenance_evidence(
    current: Any,
    incoming: Any,
    *,
    representation_kind: str | None = None,
) -> dict[str, Any] | None:
    """Merge ordered provider evidence before canonical persistence.

    Provider adapters retain authority over representation precedence. This
    helper owns only the common source-field fallback and ordered evidence
    merge, and deliberately leaves singleton compatibility fields to
    ``normalize_reasoning_provenance()`` at the persistence boundary.
    """
    if not isinstance(incoming, dict):
        return current if isinstance(current, dict) else None
    existing = current if isinstance(current, dict) else {}

    def _merged_values(plural: str, singular: str) -> list[str]:
        candidates: list[Any] = []
        for provenance in (existing, incoming):
            values = provenance.get(plural)
            if isinstance(values, (list, tuple)):
                candidates.extend(values)
            else:
                candidates.append(provenance.get(singular))
        return _ordered_nonempty_strings(candidates)

    merged = {
        "source_harness": incoming.get(
            "source_harness", existing.get("source_harness", "unknown")
        ),
        "source_provider": incoming.get(
            "source_provider", existing.get("source_provider", "unknown")
        ),
        "representation_kind": representation_kind or incoming.get(
            "representation_kind",
            existing.get("representation_kind", "unknown"),
        ),
    }
    effective_model = _nonempty_string(incoming.get("source_effective_model"))
    if effective_model is None:
        effective_model = _nonempty_string(existing.get("source_effective_model"))
    if effective_model is not None:
        merged["source_effective_model"] = effective_model
    references = _merged_values("native_references", "native_reference")
    signatures = _merged_values("native_signatures", "native_signature")
    if references:
        merged["native_references"] = references
    if signatures:
        merged["native_signatures"] = signatures
    blocks = _normalized_anthropic_blocks(incoming.get("native_blocks"))
    if not blocks:
        blocks = _normalized_anthropic_blocks(existing.get("native_blocks"))
    if blocks:
        merged["native_blocks"] = blocks
    return merged


def reasoning_provenance_from_metadata(metadata: Any) -> dict[str, Any]:
    """Read canonical provenance, returning ``unknown`` for legacy metadata."""
    meta = parse_meta(metadata)
    return normalize_reasoning_provenance(meta.get("reasoning_provenance"))


def reasoning_content_for_typed_replay(metadata: Any) -> str | None:
    """Return plaintext typed reasoning only when its provenance is explicit."""
    meta = parse_meta(metadata)
    provenance = reasoning_provenance_from_metadata(meta)
    if (
        provenance["representation_kind"] != "reasoning_content"
        or provenance["source_harness"] == "unknown"
        or provenance["source_provider"] == "unknown"
    ):
        return None
    return _nonempty_string(meta.get("reasoning"))
