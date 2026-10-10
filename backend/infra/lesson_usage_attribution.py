"""View-only lesson descriptor estimates from the selected frozen snapshot.

These numbers subdivide the Skills estimate. They never change snapshot
membership, identity, delivered material or aggregate usage.
"""
from __future__ import annotations

from collections import Counter
import math
import re
from typing import Any

from core.lesson_index import descriptor_estimates


def _source_estimates(source: dict[str, Any], model: str | None) -> list[dict[str, Any]] | None:
    metadata = source.get("metadata")
    lessons = metadata.get("lessons") if isinstance(metadata, dict) else None
    if not isinstance(lessons, list):
        return None
    if any(
        not isinstance(row, dict)
        or not isinstance(row.get("id"), str) or not row["id"]
        or not isinstance(row.get("behavioral_sha256"), str)
        or re.fullmatch(r"[a-f0-9]{64}", row["behavioral_sha256"]) is None
        for row in lessons
    ):
        return None
    identities = Counter(row["id"] for row in lessons)
    # The canonical calculator keys by lesson id. An ambiguous id cannot be
    # assigned a cost to one descriptor or behavioral version.
    costed = [
        row for row in lessons
        if identities[row["id"]] == 1
        and isinstance(row.get("index_line"), str) and row["index_line"]
    ]
    budget = source.get("prompt_tokens")
    estimates: dict[str, int] = {}
    if (
        isinstance(budget, (int, float)) and not isinstance(budget, bool)
        and math.isfinite(budget) and budget >= 0 and model
    ):
        estimates = descriptor_estimates(costed, skills_tokens=int(budget), model=model)
    return [{
        "id": row["id"],
        "behavioral_sha256": row["behavioral_sha256"],
        "prompt_tokens": estimates.get(row["id"]),
    } for row in lessons]


def lesson_index_estimates(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Project costs without modifying any part of a live or stored snapshot."""
    facts = snapshot.get("harness_model_facts")
    model = facts.get("model") if isinstance(facts, dict) else None
    model = model if isinstance(model, str) else None
    inventory = snapshot.get("source_inventory")
    inventory = inventory if isinstance(inventory, list) else []
    estimates: dict[str, list[dict[str, Any]] | None] = {}
    for source in inventory:
        if not isinstance(source, dict) or not isinstance(source.get("source_id"), str):
            continue
        metadata = source.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("provider") != "skills":
            continue
        source_id = source["source_id"]
        if source_id in estimates:
            estimates[source_id] = None
        else:
            estimates[source_id] = _source_estimates(source, model)
    return {"snapshot_id": snapshot.get("snapshot_id"), "model": model, "sources": estimates}


def _remove_lesson_estimates(usage: dict[str, Any]) -> None:
    usage.pop("lesson_index_estimates", None)


def attach_lesson_estimates(usage: dict[str, Any]) -> None:
    """Derive from the final selected snapshot at an existing usage export edge."""
    snapshot = usage.get("context_snapshot")
    if isinstance(snapshot, dict) and isinstance(snapshot.get("source_inventory"), list):
        usage["lesson_index_estimates"] = lesson_index_estimates(snapshot)
    else:
        _remove_lesson_estimates(usage)


def without_lesson_estimates(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """A withheld snapshot cannot retain a descriptor projection from an older row."""
    if not isinstance(usage, dict) or "lesson_index_estimates" not in usage:
        return usage
    result = dict(usage)
    _remove_lesson_estimates(result)
    return result
