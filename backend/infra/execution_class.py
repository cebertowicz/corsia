"""Derive a thread execution class from its resolved catalog target.

All runtime ingress surfaces that classify a thread's driver use this resolver
so event delivery, delegation, WebSocket sends, conductor status, and shared
send validation cannot drift.

Classification only; call sites supply ingress-specific native-turn inputs.

Execution classes:

- ``cc_main`` — CC-backed thread, not a delegated subagent. Steered via
  ``cc_runtime.feed_main`` (spawns/resumes the main CC process).
- ``cc_sub`` — CC-backed AND has a parent (``spawned_by IS NOT NULL``) — a
  delegated child. Steered via ``cc_runtime.feed`` (replies into / resumes it).
- ``codex_main`` — Codex-backed thread, not a delegated subagent. Steered via
  ``codex_runtime.feed_main``.
- ``codex_sub`` — Codex-backed delegated child. Steered via
  ``codex_runtime.feed``.
- ``native`` — CORSIA-driven model thread. Driven by an ``execute_turn`` layer.

Model-id spelling is not an execution signal. ``ExecutionTarget.harness`` is
the sole driver axis; parent presence only selects main versus subagent mode.

Falsy/None sessions: all public predicates return False / classify as
``"native"`` — callers are responsible for the existence check (both call sites
already 404/return False on a missing row before reaching dispatch).

Unexpected thread-row shapes (anything without ``.keys()``) raise ``TypeError``.

**Scope of "single source of truth":** this module governs *execution-class
dispatch* (which turn driver handles a thread). Provider-specific runtimes may
still enforce their own resumability preconditions after this classifier picks
the driver.
"""

from __future__ import annotations

from typing import Any, Literal

from llm.resolution import ExecutionTarget, resolve_execution_target

ExecutionClass = Literal["cc_main", "cc_sub", "codex_main", "codex_sub", "native"]


def _field(thread_row: Any, key: str) -> Any:
    """Read ``key`` from a thread row regardless of shape.

    Accepts ``sqlite3.Row`` (``events/delivery`` and ``api/delegation.send_turn``)
    and plain ``dict`` (``api/delegation.thread_status``). Both expose ``.keys()``;
    ``sqlite3.Row`` does not support ``.get``, so we membership-check before
    indexing.

    Raises ``TypeError`` on an unrecognised shape (no ``.keys()``).
    Raises ``KeyError`` when the column is absent from the row — a narrowed
    SELECT that omits ``model`` or ``spawned_by`` would silently misclassify;
    callers must SELECT those columns (or ``SELECT *``).
    Returns ``None`` when the key is present but its value is NULL — that is a
    legitimate database value, not a missing column.
    """
    if not hasattr(thread_row, "keys"):
        raise TypeError(
            f"_field: expected sqlite3.Row or dict, got {type(thread_row).__name__!r}"
            f" (value={thread_row!r})"
        )
    if key not in thread_row.keys():
        raise KeyError(
            f"_field: column {key!r} absent from row — SELECT is missing expected "
            f"columns. Row keys: {list(thread_row.keys())!r}"
        )
    return thread_row[key]  # None means SQL NULL — legitimate, not a missing column


def _target(thread_row: Any, target: ExecutionTarget | None) -> ExecutionTarget | None:
    model = _field(thread_row, "model")
    if target is not None:
        return target
    return resolve_execution_target(model) if model else None


def is_cc_backed(
    thread_row: Any, *, target: ExecutionTarget | None = None
) -> bool:
    """True when the thread runs on the CC execution class (``cc_main`` or
    ``cc_sub``). Falsy/None sessions return False.

    Classification comes only from the resolved catalog target."""
    if not thread_row:
        return False
    resolved = _target(thread_row, target)
    return resolved is not None and resolved.harness == "claude-code"


def is_subagent(thread_row: Any) -> bool:
    """True when the thread is a delegated subagent child — i.e. it has a parent
    (``spawned_by IS NOT NULL`` → steered via ``cc_runtime.feed``).

    Parent presence is the single source of truth for subagent-ness: a thread is
    a subagent exactly when it was spawned by another thread. Falsy/None sessions
    return False."""
    if not thread_row:
        return False
    return _field(thread_row, "spawned_by") is not None


def is_codex_backed(
    thread_row: Any, *, target: ExecutionTarget | None = None
) -> bool:
    """True when the resolved target uses the Codex execution class.

    Falsy/None sessions return False."""
    if not thread_row:
        return False
    resolved = _target(thread_row, target)
    return resolved is not None and resolved.harness == "codex"


def provider_label(
    model: str | None,
    *,
    target: ExecutionTarget | None = None,
    db=None,
) -> str:
    """Resolve a selected catalog model to its public runtime label."""
    if not model:
        return "native"
    resolved = target
    if resolved is None:
        resolved = (
            resolve_execution_target(model, db=db)
            if db is not None
            else resolve_execution_target(model)
        )
    if resolved.harness == "claude-code":
        return "cc"
    if resolved.harness == "codex":
        return "codex"
    return "native"


def execution_class(
    thread_row: Any, *, target: ExecutionTarget | None = None
) -> ExecutionClass:
    """Classify a thread row into its execution class.

    Returns ``"cc_main"``/``"cc_sub"`` (CC-backed), ``"codex_main"``/
    ``"codex_sub"`` (Codex-backed), or ``"native"``. A falsy/None thread row
    classifies as ``"native"`` (callers do the existence check — both call sites
    already 404/return False on a missing row before reaching dispatch).
    """
    if not thread_row:
        return "native"
    resolved = _target(thread_row, target)
    if resolved is not None and resolved.harness == "codex":
        return "codex_sub" if is_subagent(thread_row) else "codex_main"
    if resolved is not None and resolved.harness == "claude-code":
        return "cc_sub" if is_subagent(thread_row) else "cc_main"
    return "native"
