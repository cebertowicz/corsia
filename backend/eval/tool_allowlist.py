"""Disposable-evaluation tool allowlist admission and receipt material."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
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


@dataclass(frozen=True)
class CarryInUnseen:
    """What one sample side's read tools may not observe.

    Nothing under a ``hidden`` path exists for them: not listed, not searched, not read. ``shown``
    is the one file under them that ``read_file`` still opens, and ``view`` the bytes it shows
    (the lesson as its candidate side receives it, without its case). Paths are resolved.
    """

    hidden: tuple[Path, ...] = ()
    shown: Path | None = None
    view: bytes | None = None


_carry_in_unseen: ContextVar[CarryInUnseen | None] = ContextVar("carry_in_unseen", default=None)


@contextmanager
def carry_in_tool_isolation(unseen: CarryInUnseen = CarryInUnseen()) -> Iterator[None]:
    """Make this task and its child tasks record non-read-only eval calls and observe no more than ``unseen`` allows."""
    reset_handle = _carry_in_unseen.set(unseen)
    try:
        yield
    finally:
        _carry_in_unseen.reset(reset_handle)


def is_carry_in_record_only(tool_name: str) -> bool:
    """True when a carry-in turn must retain, but not execute, this call."""
    return _carry_in_unseen.get() is not None and tool_name not in CARRY_IN_READ_ONLY_TOOLS


def carry_in_hides(path: Path, *, reading: bool = False) -> bool:
    """Whether a carry-in sample's read tool must treat ``path`` as absent.

    ``reading`` is a read of the file's own content, which the ``shown`` file allows.
    """
    unseen = _carry_in_unseen.get()
    if unseen is None or not unseen.hidden:
        return False
    resolved = path.resolve(strict=False)
    if reading and resolved == unseen.shown:
        return False
    return any(resolved.is_relative_to(root) for root in unseen.hidden)


def carry_in_view(path: Path, data: bytes) -> bytes:
    """The bytes a carry-in sample's ``read_file`` shows of ``path`` whose content ``data`` is."""
    unseen = _carry_in_unseen.get()
    if unseen is None or unseen.view is None or path.resolve(strict=False) != unseen.shown:
        return data
    return unseen.view


async def record_only_result(tool_name: str, params: dict) -> str | None:
    """The record-only receipt for an isolated non-read-only call, else None."""
    if not is_carry_in_record_only(tool_name):
        return None
    return json.dumps({"ok": True, "record_only": True, "tool": tool_name})


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
