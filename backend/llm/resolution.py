"""Resolve model IDs to callable provider models."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from llm.context_window import WindowFacts, resolve_window_facts


class ModelUnavailableError(Exception):
    """Raised when a user-selected model is unavailable and no silent fallback is allowed."""

    def __init__(self, model_id: str, reason: str):
        self.model_id = model_id
        self.reason = reason
        super().__init__(f"Model '{model_id}' is unavailable: {reason}")


Harness = Literal["native", "claude-code", "codex"]


@dataclass(frozen=True, slots=True)
class ExecutionTarget:
    """One immutable catalog resolution carried through turn dispatch."""

    catalog_id: str
    harness: Harness
    provider: str
    backend: str
    transport_model: str
    expected_reported_model: str | None
    context_window: int | None
    available: bool
    availability_reason: str | None
    display_name: str
    supports_reasoning: bool
    supports_vision: bool = False
    preserve_thinking_default: bool | None = None
    #: Explicit account selected for an isolated harness invocation, when its
    #: caller resolved one under a project scope.
    account_id: str | None = None
    #: Provider capacity before any operator cap. Equals ``context_window``
    #: unless a ``model_profiles`` cap is in force; see ``WindowFacts``.
    discovered_context_window: int | None = None
    #: Which rule produced ``context_window``; see ``WindowFacts.source``.
    context_window_source: str = "provider"

    @property
    def windows(self) -> WindowFacts:
        """This resolution's counted and discovered windows, which travel together."""
        return WindowFacts(
            self.context_window, self.discovered_context_window,
            self.context_window_source,
        )


def _target_row(model: str, *, db) -> dict | None:
    exact = db.execute("SELECT * FROM models WHERE id = ?", (model,)).fetchone()
    if exact:
        return dict(exact)
    matches = db.execute(
        "SELECT * FROM models WHERE litellm_model = ?", (model,)
    ).fetchall()
    if not matches and model.startswith("omlx@"):
        provider_model_id = model.partition("/")[2]
        if provider_model_id:
            matches = db.execute(
                "SELECT * FROM models WHERE provider = 'omlx' "
                "AND provider_model_id = ? AND available = 1",
                (provider_model_id,),
            ).fetchall()
    if len(matches) > 1:
        raise ValueError(f"ambiguous model transport identity: {model}")
    return dict(matches[0]) if matches else None


def catalog_row(model: str, *, db) -> dict | None:
    """The catalog row an id, selector or oMLX transport names, or None.

    The lookup ``resolve_execution_target`` starts from, for readers that need
    the row a recorded selector names rather than a dispatchable target.
    Raises ``ValueError`` when two rows claim the same transport identity.
    """
    return _target_row(model, db=db)


def _dispatchable(row: dict, harness: str) -> bool:
    """Whether a catalog row can run a turn.

    Hiding a Claude Code or Codex model takes it out of the picker only: a
    thread or agent already pinned to it keeps running. A native row that is
    switched off is withdrawn.
    """
    return bool(row.get("available")) and (harness != "native" or bool(row.get("enabled")))


def _resolve_non_native_target(
    harness: str, row: dict, metadata: dict, available: bool, connection
) -> tuple[bool, str | None, str, str | None]:
    """Resolve a harness-backed row's availability and transport identity."""
    connection_row = connection.execute(
        "SELECT status FROM connections WHERE provider = ?", (harness,)
    ).fetchone()
    reason: str | None = None
    if connection_row and connection_row["status"] == "disabled":
        available = False
        reason = "provider_disabled"
    elif not connection_row or connection_row["status"] != "available":
        available = False
        reason = "connection_unavailable"
    elif not available:
        reason = (
            metadata.get("availability_reason")
            or row.get("last_error")
            or "discovered_model_unavailable"
        )
    transport_model = str(row.get("provider_model_id") or "")
    if not transport_model:
        raise ValueError(f"empty harness selector for catalog row {row['id']}")
    expected = metadata.get("resolved_identity")
    return available, reason, transport_model, str(expected) if expected else None


def _resolve_native_target(
    row: dict, available: bool
) -> tuple[bool, str | None, str, None]:
    """Resolve a native row's availability and transport identity."""
    reason: str | None = None
    if not available:
        reason = row.get("last_error") or "model_unavailable"
    transport_model = str(row.get("litellm_model") or "")
    if not transport_model:
        raise ValueError(f"empty native transport for catalog row {row['id']}")
    return available, reason, transport_model, None


def resolve_execution_target(
    selected_model: str | None, *, db=None
) -> ExecutionTarget:
    """Resolve one exact catalog identity without prefix classification."""
    model = str(selected_model or "").strip()
    if not model:
        raise ModelUnavailableError("(none)", "No model configured")
    if db is None:
        from infra.database import get_db

        connection = get_db()
        close = True
    else:
        connection = db
        close = False
    try:
        row = _target_row(model, db=connection)
        if row is None:
            raise ModelUnavailableError(
                model, f"Model '{model}' not found in catalog"
            )
        harness = str(row.get("harness") or "")
        if harness not in ("native", "claude-code", "codex"):
            raise ValueError(
                f"unsupported harness {harness!r} for catalog row {row['id']}"
            )
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(
                f"invalid model metadata for catalog row {row['id']}"
            ) from exc
        if not isinstance(metadata, dict):
            raise ValueError(f"invalid model metadata for catalog row {row['id']}")

        internal_reviewer = bool(metadata.get("internal_reviewer"))
        available = _dispatchable(row, harness)
        if harness == "codex" and internal_reviewer:
            # The provider-owned review selector is intentionally hidden from
            # the user catalog but remains callable when Codex is connected.
            available = True
        if harness != "native":
            available, reason, transport_model, expected = _resolve_non_native_target(
                harness, row, metadata, available, connection
            )
        else:
            available, reason, transport_model, expected = _resolve_native_target(
                row, available
            )

        windows = resolve_window_facts(row, db=connection)
        preserve_thinking_default = metadata.get("preserve_thinking_default")
        if not isinstance(preserve_thinking_default, bool):
            preserve_thinking_default = None

        return ExecutionTarget(
            catalog_id=str(row["id"]), harness=harness,
            provider=str(row.get("provider") or ""),
            backend=str(row.get("backend") or ""),
            transport_model=transport_model,
            expected_reported_model=expected,
            context_window=windows.effective,
            discovered_context_window=windows.discovered,
            context_window_source=windows.source,
            available=available,
            availability_reason=reason,
            display_name=str(row.get("display_name") or row["id"]),
            supports_reasoning=bool(row.get("supports_reasoning")),
            supports_vision=bool(row.get("supports_vision")),
            preserve_thinking_default=preserve_thinking_default,
        )
    finally:
        if close:
            connection.close()


def resolve_harness_selector(
    harness: Literal["claude-code", "codex"], selector: str, *, db=None
) -> ExecutionTarget:
    """Resolve an exact transport selector through the same target constructor."""
    exact = str(selector or "").strip()
    if not exact:
        raise ModelUnavailableError("(none)", "No harness selector configured")
    if db is None:
        from infra.database import get_db

        connection = get_db()
        close = True
    else:
        connection = db
        close = False
    try:
        rows = connection.execute(
            "SELECT id FROM models WHERE harness = ? AND provider_model_id = ?",
            (harness, exact),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError(f"ambiguous {harness} selector: {exact}")
        if not rows:
            raise ModelUnavailableError(
                exact, f"Model selector '{exact}' not found in catalog"
            )
        return resolve_execution_target(rows[0]["id"], db=connection)
    finally:
        if close:
            connection.close()


async def _check_local_model_health(litellm_model: str) -> tuple[bool, str]:
    """Return ``(healthy, reason)`` for a local model server."""
    from llm.local_backends import find_backend

    backend = find_backend(litellm_model)
    if backend is None:
        return False, f"Unknown local model type: {litellm_model}"
    return await backend.check_health(litellm_model)


async def validate_native_target(
    target: ExecutionTarget,
    *,
    event: bool = False,
) -> ExecutionTarget:
    """Recheck one resolved Native target immediately before adapter use."""
    if target.harness != "native":
        reason = "harness model cannot run through native chat"
        if event:
            raise RuntimeError(
                f"event turn: requested model {target.catalog_id} is not a native "
                "model; not substituting a fallback model (fail-loud)."
            )
        raise ModelUnavailableError(target.catalog_id, reason)
    if not target.available:
        reason = target.availability_reason or "model_unavailable"
        if event:
            raise RuntimeError(
                f"event turn: requested model {target.catalog_id} is unavailable "
                f"({reason}); not substituting a fallback model (fail-loud)."
            )
        raise ModelUnavailableError(target.catalog_id, reason)
    if target.backend == "api":
        return target

    healthy, reason = await _check_local_model_health(target.transport_model)
    if healthy:
        return target
    if event:
        raise RuntimeError(
            f"event turn: requested model {target.catalog_id} is unavailable "
            f"({reason}); not substituting a fallback model (fail-loud)."
        )
    raise ModelUnavailableError(target.catalog_id, reason)
