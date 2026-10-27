"""What a confined (``review``) thread's CORSIA tool calls may reach, on any harness.

The tool boundary (``tools.registry``) reads the thread's stored confinement
(``infra.thread_confinement``) on every call. A confined thread may call only
the read tools below, and each of their paths, as given and as resolved (so a
symlink or ``..`` cannot leave), must sit inside one of its read dirs. Those
dirs passed the parent's own read checks (``require_readable_dirs``) when the
thread was made, and the ordinary checks still apply on top. No write, and no
reviewed host permit, ever applies to a confined call.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from infra.thread_confinement import ConfinementRecordDamaged, parse

#: The only tools a confined thread has: they read, and resolve every path below.
REVIEW_READ_TOOLS = frozenset({"read_file", "grep", "glob", "list_dir"})

_roots: ContextVar[tuple[Path, ...] | None] = ContextVar("confined_read_roots", default=None)


def context_roots(thread_context: Mapping[str, Any]) -> tuple[Path, ...] | None:
    """The call's read roots from the boundary's thread row, ``None`` if unconfined.

    Raises ``ConfinementRecordDamaged`` for a record that cannot be read.
    """
    dirs = parse(thread_context.get("confinement"), thread_context.get("confined_read_dirs"))
    return None if dirs is None else tuple(Path(path) for path in dirs)


def call_refusal(name: str, thread_context: Mapping[str, Any]) -> str | None:
    """Why a confined thread may not call ``name`` (the dispatched tool), or ``None``."""
    try:
        roots = context_roots(thread_context)
    except ConfinementRecordDamaged as error:
        return f"Error: refused: {error}; no tool may run."
    if roots is None or name in REVIEW_READ_TOOLS:
        return None
    return (f"Error: refused: {name} is not available to a confined review worker; "
            f"it may only use {', '.join(sorted(REVIEW_READ_TOOLS))} on its read paths.")


@contextmanager
def scope(thread_context: Mapping[str, Any]) -> Iterator[None]:
    """Run one call with its thread's read roots in force (none for an unconfined one)."""
    token = _roots.set(context_roots(thread_context))
    try:
        yield
    finally:
        _roots.reset(token)


def confined() -> bool:
    """Whether the running call is a confined thread's."""
    return _roots.get() is not None


def check(given: Path, resolved: Path, *, for_write: bool) -> None:
    """Refuse a confined call's write, or a path outside its read roots (as given and resolved)."""
    roots = _roots.get()
    if roots is None:
        return
    if for_write:
        raise PermissionError("A confined review worker cannot write.")
    lexical = Path(os.path.normpath(given.absolute()))
    for candidate in (lexical, resolved):
        if not any(candidate == root or candidate.is_relative_to(root) for root in roots):
            raise PermissionError(
                f"Path {given} is outside this confined worker's read paths."
            )


async def advertised(registry: Any, thread_context: Mapping[str, Any] | None) -> tuple[dict, ...]:
    """The provider-facing tools for the already loaded thread row.

    A damaged record advertises nothing; the boundary refuses every call anyway.
    """
    from tools.catalog import advertised_tool_definitions

    definitions = advertised_tool_definitions(registry)
    try:
        roots = context_roots(thread_context or {})
    except ConfinementRecordDamaged:
        return ()
    if roots is None:
        return definitions
    return tuple(d for d in definitions if d["function"]["name"] in REVIEW_READ_TOOLS)
