"""Pure fit ledgers for frozen provider and checkpoint-generation payloads."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping


# Accounting fallback only; a proved model window always wins.
UNPROVEN_WINDOW_FALLBACK_TOKENS = 262_144

_SECTION_NAMES = (
    "fixed",
    "checkpoint",
    "tail",
    "current_input",
    "tools",
    "provider_wrapper",
)


def _canonical_json(value: Any) -> str:
    """Serialize one frozen provider payload deterministically for identity."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False, default=str,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def proven_window_tokens(proven_target_window_tokens: int | None) -> int | None:
    """Return exact target window evidence, or None when there is none."""
    if (
        isinstance(proven_target_window_tokens, bool)
        or not isinstance(proven_target_window_tokens, int)
        or proven_target_window_tokens <= 0
    ):
        return None
    return proven_target_window_tokens


def effective_destination_window_tokens(
    proven_target_window_tokens: int | None,
    fallback_window_tokens: int = UNPROVEN_WINDOW_FALLBACK_TOKENS,
) -> int:
    """Use the proved model window, or a fallback that never becomes evidence."""
    if (
        isinstance(fallback_window_tokens, bool)
        or not isinstance(fallback_window_tokens, int)
        or fallback_window_tokens <= 0
    ):
        raise ValueError("configured fallback window must be positive")
    proven = proven_window_tokens(proven_target_window_tokens)
    if proven is None:
        return fallback_window_tokens
    return proven


def _require_integer_at_least(value: int, minimum: int, message: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(message)
    return value


def partition_destination_sections(
    rendered_input_tokens: int,
    *,
    fixed: int,
    checkpoint: int,
    tail: int,
    current_input: int,
    tools: int,
) -> dict[str, int]:
    """Partition one renderer total without counting any semantic estimate twice."""
    if (
        isinstance(rendered_input_tokens, bool)
        or not isinstance(rendered_input_tokens, int)
        or rendered_input_tokens < 0
    ):
        raise ValueError("renderer input total must be a non-negative integer")
    estimates = {
        "current_input": current_input,
        "tools": tools,
        "checkpoint": checkpoint,
        "tail": tail,
        "fixed": fixed,
    }
    for name, value in estimates.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} estimate must be a non-negative integer")
    remaining = rendered_input_tokens
    allocated: dict[str, int] = {}
    # Allocate least-substitutable input first; remaining estimates receive
    # only unattributed renderer tokens, so tokenizer overlap is never doubled.
    for name in ("current_input", "tools", "checkpoint", "tail", "fixed"):
        amount = min(estimates[name], remaining)
        allocated[name] = amount
        remaining -= amount
    allocated["provider_wrapper"] = remaining
    return {name: allocated[name] for name in _SECTION_NAMES}


def _validated_sections(section_tokens: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    missing = [name for name in _SECTION_NAMES if name not in section_tokens]
    extra = sorted(set(section_tokens) - set(_SECTION_NAMES))
    if missing or extra:
        raise ValueError(
            "destination fit sections must be exactly "
            f"{_SECTION_NAMES!r}; missing={missing!r} extra={extra!r}"
        )
    normalized: list[tuple[str, int]] = []
    for name in _SECTION_NAMES:
        value = section_tokens[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f"destination fit section {name!r} must be a non-negative integer"
            )
        normalized.append((name, value))
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class DestinationFitLedger:
    """One immutable admission decision from renderer-owned token truth."""
    harness: str
    target_catalog_id: str
    proven_target_window_tokens: int | None
    effective_window_tokens: int
    input_identity: str
    current_input_sha256: str
    destination_payload_sha256: str
    rendered_input_tokens: int
    section_tokens: tuple[tuple[str, int], ...]
    component_sha256: tuple[tuple[str, str], ...]
    fits: bool
    block_reason: str | None
    input_count_provenance: str = "estimated:characters"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe diagnostic projection."""
        return {
            "harness": self.harness,
            "target_catalog_id": self.target_catalog_id,
            "proven_target_window_tokens": self.proven_target_window_tokens,
            "effective_window_tokens": self.effective_window_tokens,
            "input_identity": self.input_identity,
            "current_input_sha256": self.current_input_sha256,
            "destination_payload_sha256": self.destination_payload_sha256,
            "rendered_input_tokens": self.rendered_input_tokens,
            "section_tokens": dict(self.section_tokens),
            "component_sha256": dict(self.component_sha256),
            "fits": self.fits,
            "block_reason": self.block_reason,
            "input_count_provenance": self.input_count_provenance,
        }


@dataclass(frozen=True, slots=True)
class CheckpointGenerationFitProof:
    """Predispatch proof for one exact generation request and output ceiling."""
    target_catalog_id: str
    proven_target_window_tokens: int | None
    effective_window_tokens: int
    request_sha256: str
    rendered_input_tokens: int
    constrained_max_output_tokens: int
    safety_margin_tokens: int
    total_tokens: int
    fits: bool
    block_reason: str | None


class ProviderCallBlocked(RuntimeError):
    """An ordinary provider write lacks an exact accepted fit ledger."""


class ProviderFitLedgerMissing(ProviderCallBlocked):
    """No fit ledger reached the provider-call barrier on this path."""


def require_provider_call_identity(
    ledger: DestinationFitLedger | None,
    *,
    harness: str,
    target_catalog_id: str,
    accounting_payload: Any,
    input_identity: str,
    current_input: Any,
) -> DestinationFitLedger:
    """Consume one ledger only when every frozen provider identity still matches."""
    if ledger is None:
        raise ProviderFitLedgerMissing(
            f"provider call for harness={harness!r} target={target_catalog_id!r} "
            "received no destination fit ledger; attach one before dispatch"
        )
    if not isinstance(ledger, DestinationFitLedger):
        raise ProviderFitLedgerMissing(
            f"provider call for harness={harness!r} target={target_catalog_id!r} "
            f"received {type(ledger).__name__} where DestinationFitLedger was "
            "expected; attach the ledger during context preflight before dispatch"
        )
    if ledger.harness != harness:
        raise ProviderCallBlocked("provider call harness does not match fit ledger")
    if ledger.target_catalog_id != target_catalog_id:
        raise ProviderCallBlocked("provider call target does not match fit ledger")
    if ledger.destination_payload_sha256 != _digest(accounting_payload):
        raise ProviderCallBlocked(
            "provider call payload does not match the destination fit ledger"
        )
    if ledger.input_identity != input_identity:
        raise ProviderCallBlocked(
            "provider call FIFO/input identity does not match the fit ledger"
        )
    if ledger.current_input_sha256 != _digest(current_input):
        raise ProviderCallBlocked(
            "provider call current FIFO input does not match the fit ledger"
        )
    return ledger


def require_provider_call_fit(
    ledger: DestinationFitLedger | None,
    *,
    harness: str,
    target_catalog_id: str,
    accounting_payload: Any,
    input_identity: str,
    current_input: Any,
) -> DestinationFitLedger:
    """Require both frozen provider identity and actual input-window fit."""
    accepted = require_provider_call_identity(
        ledger,
        harness=harness,
        target_catalog_id=target_catalog_id,
        accounting_payload=accounting_payload,
        input_identity=input_identity,
        current_input=current_input,
    )
    if not accepted.fits:
        raise ProviderCallBlocked(
            "provider call input exceeds the selected model context window"
        )
    return accepted


def build_destination_fit_ledger(
    *,
    harness: str,
    target_catalog_id: str,
    proven_target_window_tokens: int | None,
    accounting_payload: Any,
    rendered_input_tokens: int,
    section_tokens: Mapping[str, int],
    input_identity: str,
    current_input: Any,
    components: Mapping[str, Any],
    fallback_window_tokens: int = UNPROVEN_WINDOW_FALLBACK_TOKENS,
    input_count_provenance: str = "estimated:characters",
) -> DestinationFitLedger:
    """Decide fit from one renderer-owned total and exact section partition."""
    if not isinstance(input_identity, str) or not input_identity.strip():
        raise ValueError("destination fit input identity must be non-empty")
    _require_integer_at_least(
        rendered_input_tokens, 0, "rendered input tokens must be a non-negative integer"
    )
    sections = _validated_sections(section_tokens)
    section_total = sum(value for _name, value in sections)
    if section_total != rendered_input_tokens:
        raise ValueError(
            "destination fit section sum does not match renderer-owned input "
            f"total ({section_total} != {rendered_input_tokens})"
        )
    effective_window = effective_destination_window_tokens(
        proven_target_window_tokens,
        fallback_window_tokens,
    )
    if rendered_input_tokens > effective_window:
        block_reason = (
            "frozen destination input exceeds the selected model context "
            f"window ({rendered_input_tokens}/{effective_window} tokens)"
        )
    else:
        block_reason = None
    serialized = _canonical_json(accounting_payload)
    return DestinationFitLedger(
        harness=str(harness),
        target_catalog_id=str(target_catalog_id),
        proven_target_window_tokens=proven_window_tokens(
            proven_target_window_tokens
        ),
        effective_window_tokens=effective_window,
        input_identity=input_identity.strip(),
        current_input_sha256=_digest(current_input),
        destination_payload_sha256=hashlib.sha256(
            serialized.encode("utf-8")
        ).hexdigest(),
        rendered_input_tokens=rendered_input_tokens,
        section_tokens=sections,
        component_sha256=tuple(
            (str(name), _digest(value))
            for name, value in sorted(components.items())
        ),
        fits=block_reason is None,
        block_reason=block_reason,
        input_count_provenance=input_count_provenance,
    )


def prove_checkpoint_generation_fit(
    *,
    target_catalog_id: str,
    proven_target_window_tokens: int | None,
    serialized_request: str,
    rendered_input_tokens: int,
    constrained_max_output_tokens: int,
    safety_margin_tokens: int,
    fallback_window_tokens: int = UNPROVEN_WINDOW_FALLBACK_TOKENS,
) -> CheckpointGenerationFitProof:
    """Bind renderer-owned request size, output ceiling, and one margin."""
    for label, value, positive in (
        ("rendered input", rendered_input_tokens, False),
        ("constrained max output", constrained_max_output_tokens, True),
        ("safety margin", safety_margin_tokens, False),
    ):
        invalid = (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or (positive and value == 0)
        )
        if invalid:
            qualifier = "positive" if positive else "non-negative"
            raise ValueError(f"{label} tokens must be a {qualifier} integer")
    effective_window = effective_destination_window_tokens(
        proven_target_window_tokens,
        fallback_window_tokens,
    )
    total = (
        rendered_input_tokens
        + constrained_max_output_tokens
        + safety_margin_tokens
    )
    if total > effective_window:
        reason = (
            "checkpoint generation request does not fit before dispatch "
            f"({total}/{effective_window} tokens)"
        )
    else:
        reason = None
    return CheckpointGenerationFitProof(
        target_catalog_id=target_catalog_id,
        proven_target_window_tokens=proven_window_tokens(
            proven_target_window_tokens
        ),
        effective_window_tokens=effective_window,
        request_sha256=hashlib.sha256(
            serialized_request.encode("utf-8")
        ).hexdigest(),
        rendered_input_tokens=rendered_input_tokens,
        constrained_max_output_tokens=constrained_max_output_tokens,
        safety_margin_tokens=safety_margin_tokens,
        total_tokens=total,
        fits=reason is None,
        block_reason=reason,
    )
