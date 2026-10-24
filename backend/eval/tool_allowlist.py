"""Disposable-evaluation tool allowlist admission and receipt material."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


_ENV = "CORSIA_EVAL_TOOL_ALLOWLIST"

# Carry-in samples inspect a fresh product turn. These are the only tools that
# may observe state during one; every other resolved tool is recorded without
# execution so a sample can score its intended action without mutating work.
CARRY_IN_READ_ONLY_TOOLS = frozenset({
    "glob",  # Finds candidate paths without mutating the workspace.
    "grep",  # Finds relevant existing text without mutating the workspace.
    "list_dir",  # Enumerates candidate paths without mutating the workspace.
    "plan_read",  # Reads delivery state without changing the plan.
    "read_file",  # Reads target content without changing the file.
})
_carry_in_tool_isolation: ContextVar[bool] = ContextVar(
    "carry_in_tool_isolation", default=False,
)


@contextmanager
def carry_in_tool_isolation() -> Iterator[None]:
    """Make this task and its child tasks record non-read-only eval calls."""
    reset_handle = _carry_in_tool_isolation.set(True)
    try:
        yield
    finally:
        _carry_in_tool_isolation.reset(reset_handle)


def is_carry_in_record_only(tool_name: str) -> bool:
    """True when a carry-in turn must retain, but not execute, this call."""
    return _carry_in_tool_isolation.get() and tool_name not in CARRY_IN_READ_ONLY_TOOLS


async def record_only_result(tool_name: str, params: dict) -> str | None:
    """The record-only stub's result for an isolated non-read-only call, else None."""
    if not is_carry_in_record_only(tool_name):
        return None
    from eval.null_tool import RecordOnlyTool

    return await RecordOnlyTool(tool_name).execute(**params)


def eval_tool_allowlist() -> tuple[str, ...] | None:
    """Return the exact test-only tool set, refusing production configuration."""
    raw = os.environ.get(_ENV)
    if raw is None:
        return None
    if os.environ.get("CORSIA_TEST_MODE") != "1":
        raise RuntimeError(f"{_ENV} is allowed only when CORSIA_TEST_MODE=1")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{_ENV} must be a JSON list of tool names") from exc
    if not isinstance(values, list) or not values or any(
        not isinstance(name, str) or not name for name in values
    ):
        raise RuntimeError(f"{_ENV} must be a non-empty JSON list of tool names")
    names = tuple(sorted(set(values)))
    if len(names) != len(values):
        raise RuntimeError(f"{_ENV} must not contain duplicate tool names")
    return names


def allowlist_hash(names: Iterable[str]) -> str:
    """Hash the canonical allowlist bytes recorded in the context receipt."""
    payload = json.dumps(sorted(names), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
