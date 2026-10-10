
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from delegation_docs_fixtures import (
    _OTHER,
    _OWNER,
    DOC_BODY,
    _mk_thread,
    _post,
    _registrable_roots,
    doc,
)

pytestmark = pytest.mark.integration


def test_shared_integration_client_suspends_background_loops_for_isolated_db(
    integration_client,
    isolated_db,
) -> None:
    """The session app cannot sweep a function-scoped database fixture.

    Every long-running loop production starts must be suspended here. Each one
    otherwise keeps polling whichever isolated per-test database is active
    later and can crash the suite.
    """
    from infra import state

    assert integration_client is not None
    assert isolated_db is not None
    for attribute in (
        "_plan_liveness_task",
        "_network_health_task",
        "_nightly_job_task",
        "_live_bulletin_task",
    ):
        task = getattr(state, attribute)
        assert task is not None, f"{attribute} was never started"
        assert not task.done(), f"{attribute} is not still suspended"
        assert task.get_coro().cr_code.co_name == "_suspend_forever", (
            f"{attribute} is running the real production loop"
        )


def test_doc_path_registers_on_first_comment_and_attributes_agent(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER, title="Project conductor")
    r = _post(integration_client, _OWNER, doc_path=str(doc),
              quote="A unique tail sentence", body="Nice anchor.", as_session_id=sid)
    assert r.status_code == 200, r.text
    c = r.json()["comment"]
    assert c["author_kind"] == "agent"
    assert c["author"] == "Project conductor"  # title snapshot, server-derived
    assert c["body"] == "Nice anchor."  # the comment, never the document it anchors to
    assert DOC_BODY[c["from_off"]:c["to_off"]] == "A unique tail sentence"
    assert c["anchor_text"] == "A unique tail sentence"

    from registry import models

    artifact = models.get_artifact(c["artifact_id"])
    assert artifact["external_id"].startswith("workspace://")
    assert artifact["path"] == str(doc.resolve())


def test_forging_author_is_loudly_rejected(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER, title="worker-A")
    r = _post(integration_client, _OWNER, doc_path=str(doc),
              quote="A unique tail sentence", body="x", as_session_id=sid,
              author="Steve", author_kind="user")
    assert r.status_code == 422, r.text


def test_attribution_is_server_derived(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER, title="worker-A")
    r = _post(integration_client, _OWNER, doc_path=str(doc),
              quote="A unique tail sentence", body="x", as_session_id=sid)
    assert r.status_code == 200, r.text
    c = r.json()["comment"]
    assert c["author_kind"] == "agent"
    assert c["author"] == "worker-A"


def test_artifact_id_addressing_after_registration(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER)
    first = _post(integration_client, _OWNER, doc_path=str(doc),
                  quote="A unique tail sentence", body="one", as_session_id=sid)
    aid = first.json()["comment"]["artifact_id"]
    r = _post(integration_client, _OWNER, artifact_id=aid,
              quote="communication graph", near="A unique tail",
              body="two", as_session_id=sid)
    assert r.status_code == 200, r.text



def test_agent_comments_on_doc_owned_by_a_different_thread_succeeds(integration_client, doc):
    owner = _mk_thread(integration_client, _OWNER, title="doc-owner")
    _post(integration_client, _OWNER, doc_path=str(doc),
          quote="A unique tail sentence", body="owner note", as_session_id=owner)
    other = _mk_thread(integration_client, _OWNER, title="reviewer")
    r = _post(integration_client, _OWNER, doc_path=str(doc),
              quote="communication graph", near="A unique tail",
              body="reviewer note", as_session_id=other)
    assert r.status_code == 200, r.text
    assert r.json()["comment"]["author"] == "reviewer"



def test_both_addressing_fields_is_400(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER)
    r = _post(integration_client, _OWNER, doc_path=str(doc), artifact_id="abc",
              quote="x", body="y", as_session_id=sid)
    assert r.status_code == 400


def test_neither_addressing_field_is_400(integration_client):
    sid = _mk_thread(integration_client, _OWNER)
    r = _post(integration_client, _OWNER, quote="x", body="y", as_session_id=sid)
    assert r.status_code == 400


def test_stale_artifact_id_is_422(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER)
    first = _post(integration_client, _OWNER, doc_path=str(doc),
                  quote="A unique tail sentence", body="one", as_session_id=sid)
    aid = first.json()["comment"]["artifact_id"]
    from registry import models
    models.retire(aid, tombstone=True)
    r = _post(integration_client, _OWNER, artifact_id=aid, review=True,
              body="should fail", as_session_id=sid)
    assert r.status_code == 422


def test_unknown_artifact_id_is_422(integration_client):
    sid = _mk_thread(integration_client, _OWNER)
    r = _post(integration_client, _OWNER, artifact_id="nonexistent-id",
              review=True, body="x", as_session_id=sid)
    assert r.status_code == 422


def test_doc_path_outside_roots_is_400(integration_client):
    sid = _mk_thread(integration_client, _OWNER)
    r = _post(integration_client, _OWNER, doc_path="/etc/passwd",
              review=True, body="x", as_session_id=sid)
    assert r.status_code == 400



def test_quote_not_found_is_422(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER)
    r = _post(integration_client, _OWNER, doc_path=str(doc),
              quote="this span is absent", body="x", as_session_id=sid)
    assert r.status_code == 422


def test_oversized_doc_is_413(integration_client, integration_workspace):
    big = integration_workspace / f"big-{uuid.uuid4().hex[:8]}.md"
    big.write_bytes(b"# big\n" + b"x" * 1_000_001)
    try:
        sid = _mk_thread(integration_client, _OWNER)
        r = _post(integration_client, _OWNER, doc_path=str(big),
                  quote="x", body="x", as_session_id=sid)
        assert r.status_code == 413
    finally:
        big.unlink(missing_ok=True)


def test_binary_doc_is_415(integration_client, integration_workspace):
    bin_doc = integration_workspace / f"bin-{uuid.uuid4().hex[:8]}.md"
    bin_doc.write_bytes(b"# doc\n\x00\x00 binary content here")
    try:
        sid = _mk_thread(integration_client, _OWNER)
        r = _post(integration_client, _OWNER, doc_path=str(bin_doc),
                  quote="binary", body="x", as_session_id=sid)
        assert r.status_code == 415
    finally:
        bin_doc.unlink(missing_ok=True)


def test_ambiguous_quote_is_422_with_candidate_lines(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER)
    r = _post(integration_client, _OWNER, doc_path=str(doc),
              quote="The conductor owns the communication graph.",
              body="x", as_session_id=sid)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["candidate_lines"] == [3, 5]



def test_unowned_as_session_id_is_403(integration_client, doc):
    steve_thread = _mk_thread(integration_client, _OWNER, title="steves")
    r = _post(integration_client, _OTHER, doc_path=str(doc),
              quote="A unique tail sentence", body="x", as_session_id=steve_thread)
    assert r.status_code == 403


def test_as_session_id_required(integration_client, doc):
    r = _post(integration_client, _OWNER, doc_path=str(doc),
              quote="A unique tail sentence", body="x")
    assert r.status_code == 400


def test_empty_body_is_400(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER)
    r = _post(integration_client, _OWNER, doc_path=str(doc),
              quote="A unique tail sentence", body="   ", as_session_id=sid)
    assert r.status_code == 400



def test_review_is_whole_doc_no_anchor(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER)
    r = _post(integration_client, _OWNER, doc_path=str(doc), review=True,
              body="Whole-doc review.", as_session_id=sid)
    assert r.status_code == 200, r.text
    c = r.json()["comment"]
    assert c["review"] == 1
    assert c["from_off"] is None and c["to_off"] is None


def test_review_with_quote_is_400(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER)
    r = _post(integration_client, _OWNER, doc_path=str(doc), review=True,
              quote="A unique tail sentence", body="x", as_session_id=sid)
    assert r.status_code == 400


def test_reply_with_quote_is_400(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER)
    top = _post(integration_client, _OWNER, doc_path=str(doc),
                quote="A unique tail sentence", body="top", as_session_id=sid)
    aid = top.json()["comment"]["artifact_id"]
    pid = top.json()["comment"]["id"]
    r = _post(integration_client, _OWNER, artifact_id=aid, parent_id=pid,
              quote="communication graph", body="reply", as_session_id=sid)
    assert r.status_code == 400


def test_reply_threads_under_parent(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER)
    top = _post(integration_client, _OWNER, doc_path=str(doc),
                quote="A unique tail sentence", body="top", as_session_id=sid)
    parent_id = top.json()["comment"]["id"]
    aid = top.json()["comment"]["artifact_id"]
    r = _post(integration_client, _OWNER, artifact_id=aid, parent_id=parent_id,
              body="a reply", as_session_id=sid)
    assert r.status_code == 200, r.text
    assert r.json()["comment"]["parent_id"] == parent_id


def test_bare_reply_attaches_to_parent_artifact(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER)
    top = _post(integration_client, _OWNER, doc_path=str(doc),
                quote="A unique tail sentence", body="top", as_session_id=sid)
    pid = top.json()["comment"]["id"]
    aid = top.json()["comment"]["artifact_id"]
    r = _post(integration_client, _OWNER, parent_id=pid, body="bare reply", as_session_id=sid)
    assert r.status_code == 200, r.text
    assert r.json()["comment"]["parent_id"] == pid
    assert r.json()["comment"]["artifact_id"] == aid


def test_reply_contradicting_target_is_400(integration_client, doc, integration_workspace):
    sid = _mk_thread(integration_client, _OWNER)
    top = _post(integration_client, _OWNER, doc_path=str(doc),
                quote="A unique tail sentence", body="top", as_session_id=sid)
    pid = top.json()["comment"]["id"]
    doc2 = integration_workspace / f"other-{uuid.uuid4().hex[:8]}.md"
    doc2.write_text(DOC_BODY)
    try:
        second = _post(integration_client, _OWNER, doc_path=str(doc2),
                       quote="A unique tail sentence", body="t2", as_session_id=sid)
        aid2 = second.json()["comment"]["artifact_id"]
        r = _post(integration_client, _OWNER, parent_id=pid, artifact_id=aid2,
                  body="cross-doc reply", as_session_id=sid)
        assert r.status_code == 400
    finally:
        doc2.unlink(missing_ok=True)


def test_reply_matching_doc_path_succeeds(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER)
    top = _post(integration_client, _OWNER, doc_path=str(doc),
                quote="A unique tail sentence", body="top", as_session_id=sid)
    pid = top.json()["comment"]["id"]
    r = _post(integration_client, _OWNER, parent_id=pid, doc_path=str(doc),
              body="reply, matching path", as_session_id=sid)
    assert r.status_code == 200, r.text
    assert r.json()["comment"]["parent_id"] == pid


def test_reply_unknown_parent_is_404(integration_client):
    sid = _mk_thread(integration_client, _OWNER)
    r = _post(integration_client, _OWNER, parent_id="no-such-comment",
              body="reply", as_session_id=sid)
    assert r.status_code == 404


def test_reply_with_unregistered_doc_is_400_no_orphan(integration_client, doc, integration_workspace):
    from registry import models
    sid = _mk_thread(integration_client, _OWNER)
    top = _post(integration_client, _OWNER, doc_path=str(doc),
                quote="A unique tail sentence", body="top", as_session_id=sid)
    pid = top.json()["comment"]["id"]
    fresh = integration_workspace / f"fresh-{uuid.uuid4().hex[:8]}.md"
    fresh.write_text(DOC_BODY)
    canonical = str(Path(str(fresh)).resolve())
    try:
        r = _post(integration_client, _OWNER, parent_id=pid, doc_path=str(fresh),
                  body="reply on a fresh doc", as_session_id=sid)
        assert r.status_code == 400
        assert models.resolve("workspace", "document", canonical) is None  # no orphan
    finally:
        fresh.unlink(missing_ok=True)


def test_reply_inaccessible_parent_is_404(integration_client, doc):
    owner = _mk_thread(integration_client, _OWNER)
    top = _post(integration_client, _OWNER, doc_path=str(doc),
                quote="A unique tail sentence", body="top", as_session_id=owner)
    pid = top.json()["comment"]["id"]
    other = _mk_thread(integration_client, _OTHER)
    r = _post(integration_client, _OTHER, parent_id=pid, body="intrude", as_session_id=other)
    assert r.status_code == 404


def test_client_id_collision_across_docs_is_409_no_orphan(integration_client, doc, integration_workspace):
    from registry import models
    sid = _mk_thread(integration_client, _OWNER)
    cid = f"dup-{uuid.uuid4().hex[:8]}"
    first = _post(integration_client, _OWNER, doc_path=str(doc),
                  quote="A unique tail sentence", body="one", as_session_id=sid,
                  client_id=cid)
    assert first.status_code == 200
    doc2 = integration_workspace / f"design2-{uuid.uuid4().hex[:8]}.md"
    doc2.write_text(DOC_BODY)
    canonical2 = str(Path(str(doc2)).resolve())
    try:
        r = _post(integration_client, _OWNER, doc_path=str(doc2),
                  quote="A unique tail sentence", body="two", as_session_id=sid,
                  client_id=cid)
        assert r.status_code == 409
        assert models.resolve("workspace", "document", canonical2) is None  # no orphan
    finally:
        doc2.unlink(missing_ok=True)


def test_idempotent_client_id(integration_client, doc):
    sid = _mk_thread(integration_client, _OWNER)
    cid = f"agent-{uuid.uuid4().hex[:8]}"
    body = dict(doc_path=str(doc), quote="A unique tail sentence",
                body="once", as_session_id=sid, client_id=cid)
    r1 = _post(integration_client, _OWNER, **body)
    r2 = _post(integration_client, _OWNER, **body)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["comment"]["id"] == r2.json()["comment"]["id"]

def test_cross_user_target_artifact_id_is_404(integration_client, doc):
    owner = _mk_thread(integration_client, _OWNER, title="steve-owner")
    first = _post(integration_client, _OWNER, doc_path=str(doc),
                  quote="A unique tail sentence", body="steve", as_session_id=owner)
    aid = first.json()["comment"]["artifact_id"]
    other = _mk_thread(integration_client, _OTHER, title="other-user")
    r = _post(integration_client, _OTHER, artifact_id=aid, review=True,
              body="intrude", as_session_id=other)
    assert r.status_code == 404


def test_cross_user_target_doc_path_is_404(integration_client, doc):
    owner = _mk_thread(integration_client, _OWNER, title="steve-owner")
    _post(integration_client, _OWNER, doc_path=str(doc),
          quote="A unique tail sentence", body="steve", as_session_id=owner)
    other = _mk_thread(integration_client, _OTHER, title="other-user")
    r = _post(integration_client, _OTHER, doc_path=str(doc), review=True,
              body="intrude", as_session_id=other)
    assert r.status_code == 404
