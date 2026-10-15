"""A provider-resume preview counts the retained provider input once.

The retained capacity is the provider's whole input (prefix, tools and history,
plus the terminal tail). The fixed sources are inside it, so History is only the
remainder and the aggregate is retained + user.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_context_preview_contracts import (
    _patch_preview_sources,
    _preview_connection,
    _target,
)
from tests.test_sheet_scope_accounting import _accounted, _scoped_sources


def _fixed_estimate(monkeypatch, *, sources: int, user: int = 0):
    """Pin the fixed side of the estimate so the arithmetic is exact."""
    import core.context_admission as admission

    class Result:
        def to_dict(self):
            return {
                "status": "allowed", "continuity_mode": "unknown",
                "model_context_window": 258_000, "block_reason": None,
                "total_tokens": sources + user,
                "current_user_tokens": user, "history_tokens": 0,
                "source_tokens": sources, "source_section_tokens": {"tools": sources},
                "prepared_context": None,
            }

    monkeypatch.setattr(admission, "check_context_preflight", lambda **_kwargs: Result())


def _estimate(retained):
    from core.context_preview_estimate import estimated_preflight

    return estimated_preflight(
        "t", model="m", current_user_content="", db=None,
        live_window=258_000,
        tool_definition_tokens=0, retained_provider_tokens=retained,
    )


def test_screenshot_case_counts_retained_once(monkeypatch):
    _fixed_estimate(monkeypatch, sources=48_000)

    estimate = _estimate(235_000)

    assert estimate["total_tokens"] == 235_000
    assert estimate["history_tokens"] == 187_000
    assert estimate["checkpoint_tokens"] == 0
    assert estimate["continuity_mode"] == "provider_resume"


def test_user_text_is_added_once_on_top_of_retained(monkeypatch):
    _fixed_estimate(monkeypatch, sources=48_000, user=500)

    estimate = _estimate(235_000)

    assert estimate["total_tokens"] == 235_000 + 500
    assert estimate["history_tokens"] == 187_000


@pytest.mark.parametrize("retained", [10_000, 0])
def test_retained_below_fixed_keeps_the_truthful_aggregate(monkeypatch, retained):
    _fixed_estimate(monkeypatch, sources=48_000)

    estimate = _estimate(retained)

    assert estimate["history_tokens"] == 0
    assert estimate["total_tokens"] == retained  # never scaled or inflated


def test_cold_path_still_adds_sources_and_history(monkeypatch):
    _fixed_estimate(monkeypatch, sources=48_000)
    monkeypatch.setattr(
        "core.context_preview_estimate._installed_checkpoint_history", lambda *_a: None,
    )
    monkeypatch.setattr("core.context_admission._delivered_rows", lambda *_a: [])

    estimate = _estimate(None)

    assert estimate["total_tokens"] == 48_000
    assert estimate["continuity_mode"] == "unknown"
    assert estimate["loaded_tool_tokens"] == []


def test_real_source_rows_equal_the_resume_headline(monkeypatch, tmp_path):
    """The sheet's own accounting adds up with real skill/role sources."""
    from core import context_preview
    from infra.provider_resume import ProviderCapacity

    connection, base_row = _preview_connection()
    thread_row = dict(base_row)
    thread_row["model"] = "codex/model"
    _patch_preview_sources(monkeypatch, _target(harness="codex", model="codex/model"))
    sources = _scoped_sources(tmp_path)
    monkeypatch.setattr(
        context_preview, "collect_context_sources",
        lambda **_kwargs: SimpleNamespace(
            sources=sources, agent_facts={}, installed=None, project_cwd=None,
        ),
    )
    monkeypatch.setattr(
        "infra.provider_resume.read_provider_capacity",
        lambda *_a, **_k: ProviderCapacity(235_000, 30, "codex", "codex/model"),
    )

    preview = context_preview.build_next_turn_context_preview(
        "preview-thread", thread_row, db=connection,
        current_user_content="hello", runtime_truth={"codex_has_thread": True},
    )

    assert preview["continuity_mode"] == "provider_resume"
    assert preview["total_used"] == 235_030 + preview["user_tokens"]
    parts = _accounted(preview)
    assert sum(parts) == preview["total_used"], (parts, preview["total_used"])
    connection.close()


def test_resumable_provider_without_capacity_uses_the_cold_estimate(monkeypatch, tmp_path):
    """A resumable session without a receipt exposes its cold reconstruction."""
    from core import context_preview
    from infra.provider_resume import ProviderResumeDecision

    connection, base_row = _preview_connection()
    thread_row = dict(base_row)
    thread_row["model"] = "codex/model"
    _patch_preview_sources(monkeypatch, _target(harness="codex", model="codex/model"))
    sources = _scoped_sources(tmp_path)
    monkeypatch.setattr(
        context_preview, "collect_context_sources",
        lambda **_kwargs: SimpleNamespace(
            sources=sources, agent_facts={}, installed=None, project_cwd=None,
        ),
    )
    monkeypatch.setattr(
        "infra.provider_resume.provider_resume_decision",
        lambda *_a, **_k: ProviderResumeDecision(True, None),
    )

    preview = context_preview.build_next_turn_context_preview(
        "preview-thread", thread_row, db=connection,
        current_user_content="hello", runtime_truth={"codex_has_thread": True},
    )
    cold = context_preview.build_next_turn_context_preview(
        "preview-thread", thread_row, db=connection, current_user_content="hello",
    )

    assert preview["token_source"] == "estimated"
    assert preview["continuity_mode"] != "provider_resume"
    assert preview["total_used"] == cold["total_used"]
    connection.close()


def test_previewing_another_model_estimates_it_instead_of_deferring(
    monkeypatch, tmp_path,
):
    """A composer-selected model is a switch: the live session cannot resume it."""
    from core import context_preview
    from infra.provider_resume import ProviderCapacity

    connection, base_row = _preview_connection()
    thread_row = dict(base_row)
    thread_row["model"] = "codex/stored"
    _patch_preview_sources(monkeypatch, _target(harness="codex", model="codex/model"))
    sources = _scoped_sources(tmp_path)
    monkeypatch.setattr(
        context_preview, "collect_context_sources",
        lambda **_kwargs: SimpleNamespace(
            sources=sources, agent_facts={}, installed=None, project_cwd=None,
        ),
    )
    monkeypatch.setattr(
        "infra.provider_resume.read_provider_capacity",
        lambda *_a, **_k: ProviderCapacity(235_000, 30, "codex", "codex/stored"),
    )

    preview = context_preview.build_next_turn_context_preview(
        "preview-thread", thread_row, db=connection,
        current_user_content="hello", model_override="codex/model",
        runtime_truth={"codex_has_thread": True},
    )

    assert isinstance(preview, dict)
    assert preview["continuity_mode"] != "provider_resume"
    connection.close()


@pytest.mark.asyncio
async def test_preview_route_returns_an_estimate(monkeypatch):
    """The route returns the cold estimate instead of a nullable deferral."""
    from api import thread_runtime

    preview = {"continuity_mode": "checkpoint_tail", "token_source": "estimated"}

    async def run_in_db(function, *args):
        original = getattr(function, "__wrapped__", function)
        if original is thread_runtime._load_next_turn_context_snapshot_provider:
            return "codex", {"model": "codex/model"}, SimpleNamespace()
        assert original is thread_runtime._load_next_turn_context_snapshot_preview
        return preview

    monkeypatch.setattr(
        thread_runtime, "authenticate", lambda _token: {"id": "test-user"}
    )
    monkeypatch.setattr(thread_runtime, "run_in_db", run_in_db)
    monkeypatch.setattr(
        "infra.runtime_truth.read_runtime_truth", AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        "subagents.harness_tool_bridge.build_tool_registry",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "tools.catalog.advertised_tool_definitions", lambda _registry: [],
    )

    result = await thread_runtime.next_turn_context_snapshot(
        "preview-thread", token="[REDACTED]"
    )

    assert result == {"preview": preview}


@pytest.mark.asyncio
async def test_absent_preview_still_maps_to_404(monkeypatch):
    """The existing None sentinel remains reserved for an absent thread."""
    from api import thread_runtime
    from fastapi import HTTPException

    monkeypatch.setattr(
        thread_runtime, "authenticate", lambda _token: {"id": "test-user"}
    )

    async def run_in_db(*_args):
        return None

    monkeypatch.setattr(thread_runtime, "run_in_db", run_in_db)

    with pytest.raises(HTTPException) as error:
        await thread_runtime.next_turn_context_snapshot(
            "missing-thread", token="[REDACTED]"
        )

    assert getattr(error.value, "status_code", None) == 404
