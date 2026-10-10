from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


_TOKEN = "deleg-token-01"


def _insert_native_model(model_id: str = "native/test") -> None:
    """Put one ordinary, available native model in the catalog.

    A thread's selected model has to BE in the catalog for anything to be
    delivered to it — an event wake records the resolved catalog identity the
    same way an ordinary send does, so a session pointing at a model the
    catalog has never heard of cannot be woken. Tests that seed a session by
    hand have to seed its model too, or they are testing a thread that could
    not exist.
    """
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO models (id,harness,provider,"
            "provider_model_id,litellm_model,display_name,backend,source,"
            "enabled,available,context_window,metadata_json,created_at,"
            "updated_at) VALUES (?,'native','anthropic','test',?,'Test',"
            "'api','test',1,1,200000,'{}','t','t')",
            (model_id, model_id),
        )
        db.commit()
    finally:
        db.close()


def _insert_session(
    thread_id: str,
    *,
    user_id: str = "u-test",
    title: str = "T",
    model: str | None = "claude-code/sonnet",
    spawned_by: str | None = "parent",
    project_id: str | None = None,
    lifecycle_state: str = "active",
    archived_reason: str | None = None,
) -> None:
    from infra.database import get_db

    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    try:
        db.execute(
            """INSERT INTO threads
               (id, user_id, title, model, spawned_by,
                project_id, lifecycle_state, archived_reason, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                thread_id,
                user_id,
                title,
                model,
                spawned_by,
                project_id,
                lifecycle_state,
                archived_reason,
                now,
                now,
            ),
        )
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def delegation_client(
    isolated_db, monkeypatch
):
    import api.delegation as delegation

    monkeypatch.setattr(
        delegation,
        "authenticate",
        lambda token: {"id": "u-test"} if token == _TOKEN else None,
    )
    monkeypatch.setattr(delegation, "emit_product_event", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(delegation.router)
    return TestClient(app)


def _messages(thread_id: str) -> list[dict]:
    from infra.database import get_db

    db = get_db()
    try:
        rows = db.execute(
            "SELECT role,content,metadata,delivery_state,delivery_error,"
            "delivery_revision,seq FROM messages "
            "WHERE thread_id = ? ORDER BY created_at, id",
            (thread_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


def _set_pending_compaction(thread_id: str) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "UPDATE threads SET pending_compaction_trigger = ?, "
            "pending_compaction_detail = ? WHERE id = ?",
            ("manual_semantic", "test boundary", thread_id),
        )
        db.commit()
    finally:
        db.close()


def _pending_compaction(thread_id: str) -> str | None:
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute(
            "SELECT pending_compaction_trigger FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        return row["pending_compaction_trigger"]
    finally:
        db.close()


async def _checkpoint_tail_preflight(**kwargs) -> dict:
    target = kwargs["target"]
    return {
        "status": "allowed",
        "continuity_mode": "checkpoint_tail",
        "trigger_reason": "manual_semantic",
        "model_context_window": 200_000,
        "total_tokens": 1_000,
        "prepared_context": prepared_context_with_fit_ledger(target),
    }


def _rendered_payload_for(target):
    """Return the rendered payload shape this harness actually dispatches.

    Native dispatch unwraps the prepared context down to this object and
    carries the fit ledger on it, so a native target must render a real
    ``NativePreparedContext``. A stand-in of a different shape lets a test
    pass against a dispatch path production could never take.
    """
    if getattr(target, "harness", None) != "native":
        return {"prepared": "checkpoint_tail"}

    from core.turn_context import NativePreparedContext

    return NativePreparedContext(
        messages=(),
        context_usage={},
        tool_definitions=(),
        context_identity=None,
    )


def prepared_context_with_fit_ledger(target):
    """Build a dispatch-valid prepared payload carrying a genuine fit ledger.

    The provider call barrier consumes the renderer-attached ledger and
    revalidates it against the same frozen rendered payload, so tests that mock
    preflight or construct DispatchContext directly must supply this shape.
    """
    from core.context_payload import (
        PreparedContextPayload,
        RenderedDestinationPayload,
    )
    from core.destination_preflight import attach_destination_fit_ledger

    window = target.context_window
    small = isinstance(window, int) and 0 < window < 16_000
    input_tokens = 100 if small else 1_000
    prepared = PreparedContextPayload(
        mode="checkpoint_tail",
        target_catalog_id=target.catalog_id,
        model_context_window=target.context_window,
        history_epoch=0,
        sealed_boundary=None,
        replay=SimpleNamespace(mode="checkpoint_tail"),
        rendered=RenderedDestinationPayload(
            payload=_rendered_payload_for(target),
            input_tokens=input_tokens,
            accounting_payload={"prepared": "checkpoint_tail"},
            fit_section_tokens={
                "fixed": 0,
                "checkpoint": 0,
                "tail": 0,
                "current_input": input_tokens,
                "tools": 0,
                "provider_wrapper": 0,
            },
            fit_input_identity="test-preflight-input",
            fit_current_input={"prepared": "checkpoint_tail"},
            fit_components={"payload": {"prepared": "checkpoint_tail"}},
        ),
    )
    return attach_destination_fit_ledger(
        prepared,
        harness=target.harness,
        proven_target_window_tokens=target.context_window,
    )


def _thread_row(thread_id: str):
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute(
            "SELECT * FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
    finally:
        db.close()


def _install_fake_native_context_agent(monkeypatch) -> None:
    """Keep send tests focused on admission/atomicity, not agent boot config."""
    from core import agent as agent_mod
    from core.media_projection import ValidatedMediaProjection

    def _build_user_content(content, media, **_kwargs):
        if isinstance(media, ValidatedMediaProjection):
            return [
                *[
                    {
                        "type": "image_url",
                        "image_url": {"url": image.data_url()},
                    }
                    for image in media.images
                ],
                {"type": "text", "text": content},
            ]
        return [
            {"type": "text", "text": content},
            *[
                {"type": "image_url", "image_url": {"url": item}}
                for item in media
            ],
        ]

    fake_native_agent = SimpleNamespace(
        max_tokens=0,
        tools=SimpleNamespace(get_definitions=lambda: []),
        context=SimpleNamespace(
            build_system_prompt=lambda **_kwargs: "test system prompt",
            _build_user_content=_build_user_content,
        ),
        _connect_mcp=AsyncMock(),
    )
    monkeypatch.setattr(
        agent_mod,
        "get_agent",
        AsyncMock(return_value=fake_native_agent),
    )


def _empty_tail():
    """A stub replay tail with nothing older to drop.

    The cold-fill ladder walks whole turns from the oldest end, so an empty
    tail means the only candidate is the untruncated selection. Tests that
    assert the honest "this cannot fit at all" answer use this so the ladder is
    genuinely exhausted rather than bypassed.
    """
    return SimpleNamespace(turns=(), start_exclusive=None)
