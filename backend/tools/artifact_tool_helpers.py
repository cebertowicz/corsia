"""Micro-helpers shared by the artifact tool modules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.filesystem_gate import join_and_resolve


def default_workspace(workspace: Path | None) -> Path:
    """Return the constructor workspace, or the configured workspace root."""
    from infra.config import WORKSPACE

    return workspace or WORKSPACE


def tool_path(path: str, workspace: Path) -> str:
    """Return the physical file a path argument names for this tool (#872).

    A relative path joins onto the tool's own workspace through the shared
    join rule, so artifact tools name the same file as the file, shell and
    promote tools built on that workspace; the registry receives it absolute.
    """
    return str(join_and_resolve(path, workspace))


def to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True)


def causal_turn_id(kwargs: dict[str, Any]) -> str | None:
    """Return the current turn id, when one is bound, as the removal batch key.

    Every artifact-removal chat event from this turn shares this id, letting
    the frontend collapse them into one grouped line (see
    ``registry.removal_events.emit_artifact_removal_event``).
    """
    identity = kwargs.get("_host_access_turn_identity")
    turn_id = getattr(identity, "turn_id", None)
    return turn_id if isinstance(turn_id, str) else None
