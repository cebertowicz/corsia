"""Effective context snapshot contract.

This module owns the typed record of what CORSIA resolved before a provider
turn. Harness-specific delivery code can then consume the same snapshot rather
than rediscovering context independently.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from core.token_estimation import _text_tokens


def _jsonable(value: Any) -> Any:
    """Return a stable JSON-compatible copy of ``value``."""
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _stable_json(value: Any) -> str:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def provider_source_artifact(
    *,
    harness: str,
    method: str,
    snapshot_id: str | None,
    payload: dict[str, Any],
    kind: str = "provider-bound-payload",
) -> dict[str, Any]:
    """Build a stable proof record for provider-bound context material."""
    frozen_payload = _jsonable(payload)
    return {
        "kind": kind,
        "harness": harness,
        "method": method,
        "snapshot_id": snapshot_id,
        "content_hash": _sha256({
            "harness": harness,
            "method": method,
            "snapshot_id": snapshot_id,
            "payload": frozen_payload,
        }),
        "payload": frozen_payload,
    }


@dataclass
class ContextSourceEntry:
    """One source that was considered for effective context."""

    source_id: str
    origin_section: str
    label: str
    stable_reference: str
    freshness: str
    inclusion: str
    reason: str
    owner_surface: str
    prompt_content: str | None = None
    debug_reference: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    prompt_tokens_override: int | None = None
    #: Hashes of the file bytes this row was read from (``core.guidance_manifest``).
    #: Kept out of ``metadata`` and the inventory so snapshot hashes stay stable.
    raw_version: dict[str, Any] | None = None

    def frozen_copy(self) -> "ContextSourceEntry":
        return ContextSourceEntry(
            source_id=self.source_id,
            origin_section=self.origin_section,
            label=self.label,
            stable_reference=self.stable_reference,
            freshness=self.freshness,
            inclusion=self.inclusion,
            reason=self.reason,
            owner_surface=self.owner_surface,
            prompt_content=self.prompt_content,
            debug_reference=self.debug_reference,
            metadata=_jsonable(self.metadata),
            prompt_tokens_override=self.prompt_tokens_override,
            raw_version=None if self.raw_version is None else _jsonable(self.raw_version),
        )

    def prompt_token_count(self, model: str | None) -> int | None:
        """Tokens this row adds to the prompt, or None when it delivers nothing.

        The one measure for a source row: the inventory shows it and context
        preflight sums it, so the two cannot disagree on framing or override.
        """
        if not (self.prompt_content and self.inclusion in {"included", "summarized"}):
            return None
        if self.prompt_tokens_override is not None:
            return max(0, int(self.prompt_tokens_override))
        return _text_tokens(self.prompt_content, model) + 4

    def to_inventory_dict(self, *, model: str | None = None) -> dict[str, Any]:
        prompt_tokens = self.prompt_token_count(model)
        metadata = _jsonable(self.metadata)
        if prompt_tokens is not None and isinstance(metadata.get("skills"), list):
            from core.skill_inventory import with_skill_tokens

            metadata = with_skill_tokens(
                metadata, self.prompt_content or "", prompt_tokens, model,
            )
        return {
            "source_id": self.source_id,
            "origin_section": self.origin_section,
            "label": self.label,
            "stable_reference": self.stable_reference,
            "freshness": self.freshness,
            "inclusion": self.inclusion,
            "reason": self.reason,
            "owner_surface": self.owner_surface,
            "debug_reference": self.debug_reference,
            "prompt_tokens": prompt_tokens,
            "metadata": metadata,
        }


@dataclass(frozen=True)
class ContextDeliveryLeaf:
    """How one harness should receive a snapshot."""

    harness: str
    destination_mode: str
    snapshot_id: str
    content_hash: str
    limitation_status: str = "none"
    source_artifact_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "destination_mode": self.destination_mode,
            "snapshot_id": self.snapshot_id,
            "content_hash": self.content_hash,
            "limitation_status": self.limitation_status,
            "source_artifact_refs": list(self.source_artifact_refs),
        }


@dataclass(frozen=True)
class EffectiveContextSnapshot:
    """Frozen context truth for one start/turn/spawn/resume/switch boundary."""

    snapshot_id: str
    content_hash: str
    thread_id: str
    reason: str
    scope_facts: dict[str, Any]
    agent_facts: dict[str, Any]
    harness_model_facts: dict[str, Any]
    cwd: str | None
    source_root: str | None
    source_inventory: tuple[ContextSourceEntry, ...]
    frozen_prompt_material: dict[str, Any]
    delivery_leaves: tuple[ContextDeliveryLeaf, ...]
    lifecycle_class: str = "turn"
    retention_class: str = "debug"

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "content_hash": self.content_hash,
            "thread_id": self.thread_id,
            "reason": self.reason,
            "scope_facts": _jsonable(self.scope_facts),
            "agent_facts": _jsonable(self.agent_facts),
            "harness_model_facts": _jsonable(self.harness_model_facts),
            "cwd": self.cwd,
            "source_root": self.source_root,
            "source_inventory": [
                source.to_inventory_dict(model=self.harness_model_facts.get("model"))
                for source in self.source_inventory
            ],
            "frozen_prompt_material": _jsonable(self.frozen_prompt_material),
            "delivery_leaves": [leaf.to_dict() for leaf in self.delivery_leaves],
            "lifecycle_class": self.lifecycle_class,
            "retention_class": self.retention_class,
        }

def _source_text(sources: tuple[ContextSourceEntry, ...]) -> str:
    chunks: list[str] = []
    for source in sources:
        if source.prompt_content and source.inclusion in {"included", "summarized"}:
            chunks.append(
                f"[{source.origin_section}] {source.label}\n{source.prompt_content}"
            )
    return "\n\n".join(chunks)


def _destination_mode(harness: str) -> str:
    if harness == "native":
        return "prefix-snapshot"
    if harness == "claude-code":
        return "jsonl-seed"
    if harness == "codex":
        return "thread-start-inject-resume"
    return "declared-by-adapter"


def build_effective_context_snapshot(
    *,
    thread_id: str,
    reason: str,
    scope_facts: dict[str, Any] | None,
    agent_facts: dict[str, Any] | None,
    harness_model_facts: dict[str, Any] | None,
    cwd: str | None,
    source_root: str | None,
    sources: list[ContextSourceEntry] | tuple[ContextSourceEntry, ...],
    history: list[dict[str, Any]] | None = None,
    current_user_content: str | list[dict[str, Any]] | None = None,
    provider_payload: dict[str, Any] | None = None,
    delivery_harnesses: tuple[str, ...] = ("native",),
    lifecycle_class: str = "turn",
    retention_class: str = "debug",
) -> EffectiveContextSnapshot:
    """Build a deterministic effective-context snapshot.

    ``metadata`` remains visible inventory data, but prompt truth is frozen from
    source ``prompt_content`` plus history/current input before any provider
    delivery leaf receives it.
    """
    frozen_sources = tuple(source.frozen_copy() for source in sources)
    material = {
        "source_text": _source_text(frozen_sources),
        "history": _jsonable(history or []),
        "current_user_content": _jsonable(current_user_content),
        "provider_payload": _jsonable(provider_payload or {}),
    }
    payload = {
        "thread_id": thread_id,
        "reason": reason,
        "scope_facts": _jsonable(scope_facts or {}),
        "agent_facts": _jsonable(agent_facts or {}),
        "harness_model_facts": _jsonable(harness_model_facts or {}),
        "cwd": cwd,
        "source_root": source_root,
        "source_inventory": [
            source.to_inventory_dict(model=(harness_model_facts or {}).get("model"))
            for source in frozen_sources
        ],
        "frozen_prompt_material": material,
        "lifecycle_class": lifecycle_class,
        "retention_class": retention_class,
    }
    content_hash = _sha256(payload)
    snapshot_id = f"ctx-{content_hash[:16]}"
    delivery_leaves = tuple(
        ContextDeliveryLeaf(
            harness=harness,
            destination_mode=_destination_mode(harness),
            snapshot_id=snapshot_id,
            content_hash=content_hash,
        )
        for harness in delivery_harnesses
    )

    return EffectiveContextSnapshot(
        snapshot_id=snapshot_id,
        content_hash=content_hash,
        thread_id=thread_id,
        reason=reason,
        scope_facts=copy.deepcopy(_jsonable(scope_facts or {})),
        agent_facts=copy.deepcopy(_jsonable(agent_facts or {})),
        harness_model_facts=copy.deepcopy(_jsonable(harness_model_facts or {})),
        cwd=cwd,
        source_root=source_root,
        source_inventory=frozen_sources,
        frozen_prompt_material=copy.deepcopy(material),
        delivery_leaves=delivery_leaves,
        lifecycle_class=lifecycle_class,
        retention_class=retention_class,
    )
