"""Codex provider continuation survives local runtime changes."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_codex_restore_ignores_old_tool_surface_metadata(main_db):
    from registry.service import register_codex_thread
    from subagents import codex_runtime_support
    from subagents.codex_runtime_state import _CODEX_THREADS

    _CODEX_THREADS.pop("main-1", None)
    register_codex_thread(
        "main-1",
        "provider-thread-old",
        tool_surface_fingerprint="old-surface",
        db=main_db,
    )

    restored = await codex_runtime_support.restore_thread_id("main-1", main_db)

    assert restored == "provider-thread-old"
    assert _CODEX_THREADS["main-1"] == "provider-thread-old"


@pytest.mark.asyncio
async def test_codex_restore_keeps_thread_without_tool_surface_metadata(main_db):
    from registry.service import register_codex_thread
    from subagents import codex_runtime_support
    from subagents.codex_runtime_state import _CODEX_THREADS

    _CODEX_THREADS.pop("main-1", None)
    register_codex_thread(
        "main-1",
        "provider-thread-current",
        db=main_db,
    )

    restored = await codex_runtime_support.restore_thread_id("main-1", main_db)

    assert restored == "provider-thread-current"
    assert _CODEX_THREADS["main-1"] == "provider-thread-current"
