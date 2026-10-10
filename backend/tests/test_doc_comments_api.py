from __future__ import annotations

import os
import sys
import uuid

import pytest
from auth_fixtures import OWNER_TOKEN, OWNER_USER

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

pytestmark = pytest.mark.integration

_TOKEN = OWNER_TOKEN


@pytest.fixture
def doc_file(integration_workspace):
    p = integration_workspace / f"notes-spec-{uuid.uuid4().hex[:8]}.md"
    p.write_text("# Spec\n\nThe router decides where an inbound event goes.\n")
    yield p
    p.unlink(missing_ok=True)


@pytest.fixture
def artifact_id(integration_client, doc_file):
    r = integration_client.post(
        f"/api/files/register?token={_TOKEN}", json={"path": str(doc_file)}
    )
    return r.json()["artifact"]["id"]


def test_comment_thread_roundtrip(integration_client, artifact_id):
    r = integration_client.post(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}",
        json={"body": "Tighten this.", "anchor_text": "router decides", "from_off": 10, "to_off": 24},
    )
    assert r.status_code == 200
    top = r.json()["comment"]

    r2 = integration_client.post(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}",
        json={"body": "Done.", "parent_id": top["id"], "author": "jarvis", "author_kind": "agent"},
    )
    assert r2.status_code == 200

    r3 = integration_client.get(f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}")
    payload = r3.json()
    threads = payload["comments"]
    assert set(payload["feedback"]) == {"open_count", "owner_label", "grace_closes_at", "undelivered_batches"}
    assert len(threads) == 1
    assert threads[0]["body"] == "Tighten this."
    assert [x["body"] for x in threads[0]["replies"]] == ["Done."]
    assert threads[0]["replies"][0]["author_kind"] == "user"
    # Accounts have no display name, so an authenticated caller's label is the
    # username they signed in with — not the capitalized name the old
    # hardcoded token table carried.
    assert threads[0]["replies"][0]["author"] == OWNER_USER


def test_comment_create_idempotent_on_client_id(integration_client, artifact_id):
    cid = "fixed-client-id-327"
    payload = {
        "body": "Tighten this synthetic clause.",
        "anchor_text": "synthetic",
        "from_off": 4,
        "to_off": 13,
        "client_id": cid,
    }

    r1 = integration_client.post(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}", json=payload
    )
    assert r1.status_code == 200
    first = r1.json()["comment"]
    assert first["id"] == cid

    r2 = integration_client.post(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}", json=payload
    )
    assert r2.status_code == 200
    second = r2.json()["comment"]
    assert second["id"] == cid
    assert second["created_at"] == first["created_at"]
    assert second["body"] == first["body"]

    threads = integration_client.get(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}"
    ).json()["comments"]
    matching = [t for t in threads if t["id"] == cid]
    assert len(matching) == 1


def test_comment_create_mints_id_when_no_client_id(integration_client, artifact_id):
    body = {"body": "No client id here.", "anchor_text": "router", "from_off": 0, "to_off": 6}
    r1 = integration_client.post(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}", json=body
    )
    r2 = integration_client.post(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}", json=body
    )
    assert r1.status_code == 200 and r2.status_code == 200
    id1, id2 = r1.json()["comment"]["id"], r2.json()["comment"]["id"]
    assert id1 and id2 and id1 != id2


def test_comment_patch_resolve_ack_and_positions(integration_client, artifact_id, doc_file):
    cid = integration_client.post(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}",
        json={"body": "Anchor me.", "anchor_text": "Spec", "from_off": 2, "to_off": 6},
    ).json()["comment"]["id"]

    r = integration_client.patch(
        f"/api/comments/{cid}?token={_TOKEN}", json={"resolved": True, "acked": True}
    )
    row = r.json()["comment"]
    assert row["resolved"] == 1 and row["acked"] == 1

    doc_file.write_text("Prefix. # Spec\n\nThe router decides where an inbound event goes.\n")
    r2 = integration_client.post(
        f"/api/artifacts/{artifact_id}/comments/positions?token={_TOKEN}",
        json={"updates": [{"id": cid, "from_off": 10, "to_off": 14}]},
    )
    assert r2.json()["updated"] == 1

    threads = integration_client.get(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}"
    ).json()["comments"]
    target = next(t for t in threads if t["id"] == cid)
    assert (target["from_off"], target["to_off"]) == (10, 14)


def test_comment_patch_404_and_empty_400(integration_client, artifact_id):
    assert integration_client.patch(
        f"/api/comments/nope?token={_TOKEN}", json={"resolved": True}
    ).status_code == 404
    assert integration_client.post(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}", json={"body": "   "}
    ).status_code == 400


def _agent_comment(artifact_id: str) -> str:
    from doceditor import comments
    from infra.database import get_db, write_transaction

    db = get_db()
    try:
        with write_transaction(db, scope="test_agent_comment") as connection:
            return comments.create(
                artifact_id, body="Agent wrote this.", author="Reviewer",
                author_kind="agent", author_thread_id="reviewer", db=connection,
            )["id"]
    finally:
        db.close()


def test_user_edits_own_comment_text(integration_client, artifact_id):
    cid = integration_client.post(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}", json={"body": "First wording."},
    ).json()["comment"]["id"]
    r = integration_client.patch(f"/api/comments/{cid}?token={_TOKEN}", json={"body": "Second wording."})
    assert r.status_code == 200
    assert r.json()["comment"]["body"] == "Second wording."


def test_user_cannot_edit_agent_comment_text(integration_client, artifact_id):
    cid = _agent_comment(artifact_id)
    r = integration_client.patch(f"/api/comments/{cid}?token={_TOKEN}", json={"body": "Rewritten."})
    assert r.status_code == 403
    assert "Reply to it" in r.json()["detail"]
    threads = integration_client.get(f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}").json()["comments"]
    assert next(t for t in threads if t["id"] == cid)["body"] == "Agent wrote this."


def test_user_still_resolves_reopens_and_moves_agent_comment(integration_client, artifact_id):
    cid = _agent_comment(artifact_id)
    url = f"/api/comments/{cid}?token={_TOKEN}"
    assert integration_client.patch(url, json={"resolved": True}).json()["comment"]["resolved"] == 1
    assert integration_client.patch(url, json={"resolved": False}).json()["comment"]["resolved"] == 0
    moved = integration_client.patch(url, json={"from_off": 2, "to_off": 6, "anchor_text": "Spec"})
    assert moved.status_code == 200 and moved.json()["comment"]["from_off"] == 2


def test_user_reopen_records_feedback_but_resolve_ack_and_edit_stay_silent(
    integration_client, artifact_id, monkeypatch,
):
    from doceditor import feedback_batches, feedback_reopens

    seen = []
    discarded = []
    monkeypatch.setattr(feedback_batches, "record_reopen", lambda *args, **kwargs: seen.append(kwargs))
    monkeypatch.setattr(
        feedback_reopens, "discard_open_item", lambda _db, comment_id, _now: discarded.append(comment_id)
    )

    cid = _agent_comment(artifact_id)
    url = f"/api/comments/{cid}?token={_TOKEN}"
    assert integration_client.patch(url, json={"resolved": True}).status_code == 200
    assert integration_client.patch(url, json={"acked": True}).status_code == 200
    assert integration_client.patch(
        url, json={"from_off": 2, "to_off": 6, "anchor_text": "Spec"}
    ).status_code == 200
    assert integration_client.patch(url, json={"resolved": False}).status_code == 200
    assert integration_client.patch(url, json={"resolved": True}).status_code == 200

    assert seen == [{"user_id": OWNER_USER, "user_display": OWNER_USER}]
    assert discarded == [cid, cid]


@pytest.fixture
def second_artifact_id(integration_client, integration_workspace):
    p = integration_workspace / f"second-doc-{uuid.uuid4().hex[:8]}.md"
    p.write_text("# Second doc\n\nA completely different document.\n")
    r = integration_client.post(
        f"/api/files/register?token={_TOKEN}", json={"path": str(p)}
    )
    yield r.json()["artifact"]["id"]
    p.unlink(missing_ok=True)


def test_cross_artifact_client_id_returns_409(integration_client, artifact_id, second_artifact_id):
    cid = f"cross-art-{uuid.uuid4().hex[:8]}"

    r1 = integration_client.post(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}",
        json={"body": "Comment on artifact A.", "client_id": cid},
    )
    assert r1.status_code == 200
    first = r1.json()["comment"]
    assert first["id"] == cid
    assert first["artifact_id"] == artifact_id

    r2 = integration_client.post(
        f"/api/artifacts/{second_artifact_id}/comments?token={_TOKEN}",
        json={"body": "Attempted replay on artifact B.", "client_id": cid},
    )
    assert r2.status_code == 409, (
        f"Expected 409 for cross-artifact id replay, got {r2.status_code}: {r2.text}"
    )
    body_text = r2.text
    assert artifact_id not in body_text, "Response must not leak artifact A's id"
    assert "Comment on artifact A" not in body_text, "Response must not leak artifact A's body"


def test_client_id_too_long_returns_422(integration_client, artifact_id):
    long_id = "a" * 65
    r = integration_client.post(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}",
        json={"body": "Should be rejected.", "client_id": long_id},
    )
    assert r.status_code == 422, f"Expected 422 for oversized client_id, got {r.status_code}"


def test_client_id_invalid_chars_returns_422(integration_client, artifact_id):
    for bad_id in ["id with spaces", "id/slash", "id@symbol", "id\nnewline"]:
        r = integration_client.post(
            f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}",
            json={"body": "Should be rejected.", "client_id": bad_id},
        )
        assert r.status_code == 422, (
            f"Expected 422 for client_id={bad_id!r}, got {r.status_code}"
        )


def test_client_id_valid_boundary_values(integration_client, artifact_id):
    max_id = "A" * 64
    r = integration_client.post(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}",
        json={"body": "Max-length id.", "client_id": max_id},
    )
    assert r.status_code == 200, f"Expected 200 for 64-char client_id, got {r.status_code}"

    mixed_id = "Az09-_abcDEF"
    r2 = integration_client.post(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}",
        json={"body": "Mixed allowed chars.", "client_id": mixed_id},
    )
    assert r2.status_code == 200, f"Expected 200 for mixed-charset client_id, got {r2.status_code}"


def test_artifact_description_patch(integration_client, artifact_id):
    r = integration_client.patch(
        f"/api/artifacts/{artifact_id}?token={_TOKEN}",
        json={"description": "Spec for the event router."},
    )
    assert r.status_code == 200
    assert r.json()["artifact"]["description"] == "Spec for the event router."


def test_list_reanchors_external_file_changes_and_orphans(integration_client, artifact_id, doc_file):
    quote = "router decides"
    original = doc_file.read_text()
    start = original.index(quote)
    created = integration_client.post(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}",
        json={"body": "Keep this.", "anchor_text": quote,
              "from_off": start, "to_off": start + len(quote)},
    ).json()["comment"]
    assert created["anchor_prefix"] == "# Spec\n\nThe "

    doc_file.write_text("Intro line.\n" + original)
    listed = integration_client.get(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}"
    ).json()
    row = next(comment for comment in listed["comments"] if comment["id"] == created["id"])
    shifted = start + len("Intro line.\n")
    assert listed["anchors_checked"] is True
    assert (row["from_off"], row["to_off"], row["orphaned"]) == (
        shifted, shifted + len(quote), False,
    )

    doc_file.write_text("Intro line.\n# Spec\n\nThe route chooses where an inbound event goes.\n")
    orphaned = integration_client.get(
        f"/api/artifacts/{artifact_id}/comments?token={_TOKEN}"
    ).json()["comments"]
    row = next(comment for comment in orphaned if comment["id"] == created["id"])
    assert row["orphaned"] is True
    assert (row["from_off"], row["to_off"]) == (shifted, shifted + len(quote))


def test_comment_create_can_join_outer_transaction(artifact_id):
    from doceditor import comments
    from infra.database import get_db, write_transaction

    db = get_db()
    try:
        with write_transaction(db, scope="test_comment_create") as connection:
            comments.create(
                artifact_id,
                body="Join the caller-owned transaction.",
                db=connection,
            )
            assert connection.in_transaction
    finally:
        db.close()


def test_comment_create_collision_preserves_outer_transaction(artifact_id):
    """A collision rolls back create's savepoint, never its caller's transaction."""
    from doceditor import comments
    from doceditor.comments import CrossArtifactIdError
    from infra.database import get_db, write_transaction

    client_id = "collision-preserves-outer-transaction"
    comments.create(artifact_id, body="Existing comment.", client_id=client_id)

    db = get_db()
    try:
        with write_transaction(db, scope="test_comment_collision") as connection:
            with pytest.raises(CrossArtifactIdError):
                comments.create(
                    "other-artifact",
                    body="Conflicting comment.",
                    client_id=client_id,
                    db=connection,
                )
            assert connection.in_transaction
            comments.create(artifact_id, body="Outer transaction still works.", db=connection)
            assert connection.in_transaction
    finally:
        db.close()
