from __future__ import annotations

from pathlib import Path

import pytest

from delegation_docs_fixtures import (
    _OTHER,
    _OWNER,
    _comment_by_thread_a,
    _mk_thread,
    _patch,
    _post,
    _registrable_roots,
    doc,
)

pytestmark = pytest.mark.integration


def test_patch_empty_body_is_400(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER)
    top = _post(
        integration_client,
        _OWNER,
        doc_path=str(doc),
        quote="A unique tail sentence",
        body="top",
        as_session_id=sid,
    )
    cid = top.json()["comment"]["id"]
    r = _patch(integration_client, _OWNER, cid, body="   ", as_session_id=sid)
    assert r.status_code == 400


def test_patch_resolves_as_agent(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER)
    top = _post(
        integration_client,
        _OWNER,
        doc_path=str(doc),
        quote="A unique tail sentence",
        body="top",
        as_session_id=sid,
    )
    cid = top.json()["comment"]["id"]
    r = _patch(integration_client, _OWNER, cid, resolved=True, as_session_id=sid)
    assert r.status_code == 200, r.text
    assert r.json()["comment"]["resolved"] == 1


def test_agent_reopen_stays_silent(integration_client, doc):
    from infra.database import get_db

    sid = _mk_thread(integration_client, _OWNER)
    top = _post(
        integration_client,
        _OWNER,
        doc_path=str(doc),
        quote="A unique tail sentence",
        body="top",
        as_session_id=sid,
    )
    cid = top.json()["comment"]["id"]
    assert _patch(integration_client, _OWNER, cid, resolved=True, as_session_id=sid).status_code == 200
    reopened = _patch(integration_client, _OWNER, cid, resolved=False, as_session_id=sid)
    assert reopened.status_code == 200, reopened.text

    db = get_db()
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM doc_feedback_batch_items WHERE comment_id=? AND event='reopened'",
            (cid,),
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_patch_unowned_as_session_id_is_403(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER)
    top = _post(
        integration_client,
        _OWNER,
        doc_path=str(doc),
        quote="A unique tail sentence",
        body="top",
        as_session_id=sid,
    )
    cid = top.json()["comment"]["id"]
    r = _patch(integration_client, _OTHER, cid, resolved=True, as_session_id=sid)
    assert r.status_code == 403


def test_patch_unknown_comment_is_404(integration_client):
    sid = _mk_thread(integration_client, _OWNER)
    r = _patch(
        integration_client,
        _OWNER,
        "no-such-comment",
        resolved=True,
        as_session_id=sid,
    )
    assert r.status_code == 404


def test_patch_empty_is_400(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER)
    top = _post(
        integration_client,
        _OWNER,
        doc_path=str(doc),
        quote="A unique tail sentence",
        body="top",
        as_session_id=sid,
    )
    cid = top.json()["comment"]["id"]
    r = _patch(integration_client, _OWNER, cid, as_session_id=sid)
    assert r.status_code == 400


def test_agent_cannot_edit_another_threads_comment_body(integration_client, doc):
    _a, cid = _comment_by_thread_a(integration_client, doc)
    b = _mk_thread(integration_client, _OWNER, title="thread-B")
    r = _patch(integration_client, _OWNER, cid, body="B rewrites A", as_session_id=b)
    assert r.status_code == 403


def test_author_can_edit_own_comment_content(integration_client, doc):
    a, cid = _comment_by_thread_a(integration_client, doc)
    r = _patch(integration_client, _OWNER, cid, body="A revises A", as_session_id=a)
    assert r.status_code == 200, r.text
    assert r.json()["comment"]["body"] == "A revises A"


def test_other_thread_cannot_resolve_or_ack_another_threads_comment(integration_client, doc):
    # Thread A's comment registered the document, so A is both author and owner.
    _a, cid = _comment_by_thread_a(integration_client, doc)
    b = _mk_thread(integration_client, _OWNER, title="thread-B")
    assert _patch(integration_client, _OWNER, cid, acked=True, as_session_id=b).status_code == 403
    assert _patch(integration_client, _OWNER, cid, resolved=True, as_session_id=b).status_code == 403


def test_create_broadcasts_doc_comment_changed(integration_client, doc, monkeypatch):
    import infra.state as state

    seen = []
    monkeypatch.setattr(state, "broadcast_to_user", lambda uid, msg: seen.append(msg))
    sid = _mk_thread(integration_client, _OWNER)
    r = _post(
        integration_client,
        _OWNER,
        doc_path=str(doc),
        quote="A unique tail sentence",
        body="x",
        as_session_id=sid,
    )
    assert r.status_code == 200, r.text
    aid = r.json()["comment"]["artifact_id"]
    assert any(
        m.get("type") == "doc_comment_changed" and m.get("artifact_id") == aid
        for m in seen
    )


def test_patch_broadcasts_doc_comment_changed(integration_client, doc, monkeypatch):
    sid = _mk_thread(integration_client, _OWNER)
    top = _post(
        integration_client,
        _OWNER,
        doc_path=str(doc),
        quote="A unique tail sentence",
        body="top",
        as_session_id=sid,
    )
    cid = top.json()["comment"]["id"]
    aid = top.json()["comment"]["artifact_id"]
    import infra.state as state

    seen = []
    monkeypatch.setattr(state, "broadcast_to_user", lambda uid, msg: seen.append(msg))
    r = _patch(integration_client, _OWNER, cid, resolved=True, as_session_id=sid)
    assert r.status_code == 200, r.text
    assert any(
        m.get("type") == "doc_comment_changed" and m.get("artifact_id") == aid
        for m in seen
    )


def test_broadcast_failure_does_not_break_write(integration_client, doc, monkeypatch):
    import infra.state as state

    sid = _mk_thread(integration_client, _OWNER)

    def _boom(*a, **k):
        raise RuntimeError("ws down")

    monkeypatch.setattr(state, "broadcast_to_user", _boom)
    r = _post(
        integration_client,
        _OWNER,
        doc_path=str(doc),
        quote="A unique tail sentence",
        body="still lands",
        as_session_id=sid,
    )
    assert r.status_code == 200, r.text


def test_quote_422_on_new_doc_leaves_no_orphan_artifact(integration_client, doc):
    from registry import models

    sid = _mk_thread(integration_client, _OWNER)
    canonical = str(Path(str(doc)).resolve())
    assert models.resolve("workspace", "document", canonical) is None
    r = _post(
        integration_client,
        _OWNER,
        doc_path=str(doc),
        quote="this span is absent from the doc",
        body="x",
        as_session_id=sid,
    )
    assert r.status_code == 422
    assert models.resolve("workspace", "document", canonical) is None


def test_agent_reopen_of_a_rejected_suggestion_by_its_author_adds_no_node_and_no_note(integration_client, doc):
    from doceditor import comment_actions
    from infra.database import get_db, write_transaction

    author = _mk_thread(integration_client, _OWNER, title="suggester")
    posted = _post(
        integration_client,
        _OWNER,
        doc_path=str(doc),
        quote="A unique tail sentence",
        body="tighter",
        replacement="A shorter tail sentence",
        as_session_id=author,
    )
    assert posted.status_code == 200, posted.text
    cid = posted.json()["comment"]["id"]

    def _counts():
        db = get_db()
        try:
            notes = db.execute(
                "SELECT COUNT(*) FROM context_change_journal WHERE object_kind='thread_context' AND object_id=?",
                (author,),
            ).fetchone()[0]
            nodes = db.execute(
                "SELECT COUNT(*) FROM plan_nodes n JOIN plans p ON p.id=n.plan_id "
                "WHERE p.owner_kind='thread' AND p.owner_id=?", (author,),
            ).fetchone()[0]
            return notes, nodes
        finally:
            db.close()

    db = get_db()
    try:
        with write_transaction(db, scope="reject_for_agent_reopen") as connection:
            comment_actions.record_decision(
                cid, decision="reject", decided_by="Robin", decided_by_thread_id=None, db=connection,
            )
    finally:
        db.close()
    assert _counts() == (1, 0)

    reopened = _patch(integration_client, _OWNER, cid, resolved=False, as_session_id=author)
    assert reopened.status_code == 200, reopened.text

    assert _counts() == (1, 0)