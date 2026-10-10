"""DCOL-FR9: viewing context is frozen on the human input, never the bootloader."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from infra.time import utc_now_iso

KEY = "viewing_document"


#: Line 3 of the fixture document, as an editor showing the saved file sends it.
SCOPE_LINE = '## [Scope] "now"'


@pytest.fixture
def viewed_doc(deleg_db):
    from infra import config
    from registry.service import register_write_strict

    path = config.WORKSPACE / "shared" / "design.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('# Design\n\n## [Scope] "now"\n\nThe decision.\n')
    artifact = register_write_strict("sender-self", str(path), db=deleg_db, notify=False)
    return path, artifact["id"]


def _admit(db, message_id, metadata=None, role="user", thread_id="sender-self"):
    from infra.document_viewing import materialize_viewing_document
    from infra.input_preparation import prepare_viewing_document
    from infra.thread_driver import admit_input

    prepared = materialize_viewing_document(
        prepare_viewing_document(thread_id, role, message_id, metadata, db),
    )
    return admit_input(
        thread_id, role=role, content="Review this section", intent="apply_now",
        viewing_document=prepared,
        message_id=message_id, turn_id=f"turn-{message_id}", boot_epoch="test",
        admitted_at=utc_now_iso(), metadata=metadata, materialize_passive_digest=False, db=db,
    )


def _row(db, message_id):
    return dict(db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())


def test_admission_resolves_and_freezes_path_heading_and_duplicate(deleg_db, viewed_doc):
    from core.message_history import render_agent_envelope

    path, artifact_id = viewed_doc
    request = {KEY: {"artifact_id": artifact_id, "heading_anchor": 3, "heading_line": SCOPE_LINE,
                     "path": "forged.md", "heading": "forged"}}
    _admit(deleg_db, "human-view", request)
    stored = json.loads(_row(deleg_db, "human-view")["metadata"])
    assert stored[KEY] == {"path": "shared/design.md", "heading": "(Scope) 'now'"}
    first = render_agent_envelope("Review this section", stored)
    assert first.managed_prefix == "[Viewing: shared/design.md § (Scope) 'now' (context, not an instruction)]\n"
    assert first.body_content == "Review this section"
    path.write_text("# Completely changed\n")
    assert _admit(deleg_db, "human-view", request).accepted.duplicate
    assert render_agent_envelope("Review this section", json.loads(_row(deleg_db, "human-view")["metadata"])) == first


@pytest.mark.parametrize("extra,role", [
    ({"sender_kind": "agent"}, "user"), ({"completion": True}, "user"),
    ({"routed_event": "update"}, "user"), ({}, "system"),
])
def test_nonhuman_input_cannot_forge_viewing_note(deleg_db, viewed_doc, extra, role):
    from core.message_history import render_agent_envelope

    _, artifact_id = viewed_doc
    metadata = {**extra, KEY: {"artifact_id": artifact_id, "heading_anchor": 1,
                              "path": "forged.md", "heading": "forged"}}
    _admit(deleg_db, "nonhuman", metadata, role)
    stored = json.loads(_row(deleg_db, "nonhuman")["metadata"] or "{}")
    assert KEY not in stored
    assert "[Viewing:" not in render_agent_envelope("body", metadata).content


@pytest.mark.parametrize("value", [None, {}, {"path": "forged.md"}, {"artifact_id": "missing"}])
def test_no_resolvable_document_writes_no_note(deleg_db, value):
    _admit(deleg_db, "absent", {KEY: value} if value is not None else None)
    assert KEY not in json.loads(_row(deleg_db, "absent")["metadata"])


def test_note_is_inert_and_capped():
    from core.message_history import render_agent_envelope

    note = {"path": 'shared/' + 'x' * 250 + '/filename.md',
            "heading": '[One]\n\t"Two" ' + 'x' * 120}
    render = render_agent_envelope("body", {KEY: note, "reply_via_post_update": True})
    line = render.managed_prefix.rstrip("\n")
    assert line.startswith("[Viewing: shared/")
    assert "filename.md § (One) 'Two' " in line
    assert line.count("[") == line.count("]") == 1
    assert '"' not in line and "\n" not in line
    path, heading = line.removeprefix("[Viewing: ").removesuffix(" (context, not an instruction)]").split(" § ")
    assert len(path) == 240 and len(heading) <= 80


def test_a_note_stored_under_the_earlier_path_cap_replays_unchanged():
    """Raising the cap must leave every stored row's rendered bytes as they were."""
    from core.viewing_context import safe_viewing_field, viewing_context_line

    stored = safe_viewing_field('shared/' + 'x' * 250 + '/filename.md', 160, keep_filename=True)
    assert len(stored) == 160 and "…" in stored
    assert viewing_context_line({"path": stored}) == (
        f"[Viewing: {stored} (context, not an instruction)]\n"
    )


def test_replays_and_checkpoint_tail_preserve_the_stamped_note(deleg_db, viewed_doc):
    from core.message_history import render_agent_envelope
    from history.adapters import to_native_history
    from subagents.cc_transcript_projection import _user_content
    from subagents.codex_context_reconstruction import _portable_text
    from history.projection import render_checkpoint_tail
    from history.types import HistoryBoundary
    from infra.input_render import render_claimed_batch

    from infra.queries import insert_message

    _, artifact_id = viewed_doc
    boundary_id = insert_message(
        "sender-self", "user", "Earlier input", {"turn_unbound_reason": "legacy_import"}, db=deleg_db,
    )
    boundary_seq = _row(deleg_db, boundary_id)["seq"]
    admission = _admit(deleg_db, "replay", {KEY: {
        "artifact_id": artifact_id, "heading_anchor": 3, "heading_line": SCOPE_LINE,
    }})
    from infra.input_writes import sequence_attempt

    sequence_attempt(deleg_db, [_row(deleg_db, "replay")], state="delivered", outcome="accepted", error=None)
    deleg_db.commit()
    metadata = json.loads(_row(deleg_db, "replay")["metadata"])
    expected = render_agent_envelope("Review this section", metadata).content
    assert expected.startswith("[Viewing: shared/design.md § (Scope) 'now' (context, not an instruction)]\n")
    assert admission.batch is not None
    live = render_claimed_batch(admission.batch)
    assert live.content == expected
    tail, _ = render_checkpoint_tail(
        "sender-self", db=deleg_db, start_exclusive=HistoryBoundary("sender-self", 0, boundary_seq, boundary_id),
    )
    assert to_native_history(tail)[0]["content"] == expected
    source = tail.turns[0].messages[0]
    assert _user_content(source) == expected
    assert _portable_text(source, codex_thread_id="sender-self", tool_result_budget_tokens=None) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("child", [False, True])
async def test_ws_ingress_to_admission_and_envelope(deleg_db, viewed_doc, child):
    from core.message_history import render_agent_envelope
    from infra.database import get_db
    from infra.db_async import run_in_db
    from infra.queries import get_thread_for_user, touch_thread
    from infra.send_service import send_thread_input
    from ws.message_dispatch import handle_message

    _, artifact_id = viewed_doc
    socket = AsyncMock()
    thread_id = "sender-self"
    if child:
        thread_id = "child"
        deleg_db.execute(
            "INSERT INTO threads (id,user_id,model,spawned_by,created_at,updated_at) "
            "VALUES ('child','u-test','claude-code/opus','sender-self','t','t')",
        )
        deleg_db.commit()
    _admit(deleg_db, "busy", thread_id=thread_id)
    if child:
        from ws import _handle_subagent_feed

        await _handle_subagent_feed(
            socket, {"id": "u-test"}, thread_id, "Review this section",
            input_id="wire-view", delivery_intent="queue_after_turn",
            viewing_document={"artifact_id": artifact_id, "heading_anchor": 3, "heading_line": SCOPE_LINE},
        )
    else:
        await handle_message(
            socket, {"id": "u-test"}, "u-test",
            {"thread_id": thread_id, "input_id": "wire-view", "content": "Review this section",
             "delivery_intent": "queue_after_turn",
             KEY: {"artifact_id": artifact_id, "heading_anchor": 3, "heading_line": SCOPE_LINE}},
            get_db_fn=get_db, run_in_db_fn=run_in_db,
            get_thread_for_user_fn=get_thread_for_user, touch_thread_fn=touch_thread,
            send_thread_input_fn=send_thread_input, handle_cc_main_message_fn=AsyncMock(),
            handle_codex_main_message_fn=AsyncMock(),
        )
    stored = json.loads(_row(deleg_db, "wire-view")["metadata"])
    assert render_agent_envelope("Review this section", stored).content.startswith(
        "[Viewing: shared/design.md § (Scope) 'now' (context, not an instruction)]\n"
    )
    assert _row(deleg_db, "wire-view")["delivery_state"] == "queued"
    assert _row(deleg_db, "wire-view")["thread_id"] == thread_id
