"""The pane endpoint: a read-only GET plus post-response repair.

``GET /api/threads/{id}/artifacts`` must not mutate anything while producing
its response — reconcile-apply runs as a background task once the response is
sent, so the client still gets automatic repair (via the ``artifacts_changed``
broadcast and its own refetch) without a GET that writes.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from infra.database import get_db  # noqa: E402
from registry import models  # noqa: E402
from tests.registry_helpers import make_session  # noqa: E402


_TOKEN = "pane-token"


@pytest.fixture()
def pane_client(monkeypatch):
    """The pane router over the ambient ephemeral test database.

    Deliberately not ``isolated_db``: this endpoint dispatches through
    ``run_in_db``, and that fixture only redirects the calling thread's
    connection — DB-pool workers keep reading the ambient database, so an
    isolated row would be invisible to the very code under test.
    """
    from api import registry_pane as registry_api

    monkeypatch.setattr(
        registry_api,
        "authenticate",
        lambda supplied: {"id": "steve"} if supplied == "pane-token" else None,
    )
    app = FastAPI()
    app.include_router(registry_api.router)
    return TestClient(app)


def _missing_artifact(db, sid: str, path) -> dict:
    return models.register_artifact(
        integration="workspace", type="document", external_id=str(path),
        parent_thread_id=sid, title=path.name, path=str(path), db=db,
    )


def _state(db, artifact_id: str) -> str:
    return db.execute(
        "SELECT state FROM artifact_identities WHERE id=?", (artifact_id,),
    ).fetchone()["state"]


def test_pane_get_returns_the_row_then_repairs_it_in_the_background(
    pane_client, tmp_path,
):
    db = get_db()
    sid = make_session(db, prefix="pane-api")
    art = _missing_artifact(db, sid, tmp_path / "ghost.md")

    first = pane_client.get(f"/api/threads/{sid}/artifacts?token=pane-token")

    assert first.status_code == 200
    # The response itself reflects pre-repair truth: the read did not mutate.
    assert art["id"] in {n["id"] for n in first.json()["nodes"] if n["kind"] == "artifact"}
    # The scheduled background task has run by the time TestClient returns.
    assert _state(db, art["id"]) == "archived"

    second = pane_client.get(f"/api/threads/{sid}/artifacts?token=pane-token")

    body = second.json()
    assert art["id"] not in {n["id"] for n in body["nodes"] if n["kind"] == "artifact"}
    assert body["history"] == []


def test_pane_get_response_shape_has_no_total(pane_client):
    db = get_db()
    sid = make_session(db, prefix="pane-api")

    body = pane_client.get(f"/api/threads/{sid}/artifacts?token=pane-token").json()

    assert set(body) == {"nodes", "needs_attention", "history"}


def test_pane_get_requires_auth_and_ownership(pane_client):
    db = get_db()
    mine = make_session(db, prefix="pane-api")
    theirs = make_session(db, prefix="pane-api", user_id="leksi")

    assert pane_client.get(f"/api/threads/{mine}/artifacts?token=nope").status_code == 401
    assert pane_client.get(
        f"/api/threads/{theirs}/artifacts?token=pane-token"
    ).status_code == 404


@pytest.fixture(autouse=True)
def _clear_reconcile_coalescing_state():
    """Reset the module-level coalescing sets so a stuck pass in one test
    cannot leave a thread id marked in-flight for every test after it."""
    from api import registry_pane as registry_api

    registry_api._reconcile_in_flight.clear()
    registry_api._reconcile_rerun_requested.clear()
    yield
    registry_api._reconcile_in_flight.clear()
    registry_api._reconcile_rerun_requested.clear()


def _passes(registry_api, monkeypatch):
    """Drive ``_reconcile_after_response`` with a pass we can hold open.

    Returns ``(calls, started, release)``: the first pass blocks on
    ``release`` so a second trigger can land while it is genuinely in flight.
    """
    calls: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def _apply(fn):
        calls.append("pass")
        if len(calls) == 1:
            started.set()
            await release.wait()
        return None

    monkeypatch.setattr(registry_api, "run_in_db", _apply)
    return calls, started, release


async def test_a_trigger_arriving_mid_pass_is_not_dropped(monkeypatch):
    """A pane GET landing mid-pass must still get repair.

    The running pass may already be past its candidate scan, so returning
    early would leave a file deleted in between waiting for some later,
    unrelated fetch to notice it.
    """
    from api import registry_pane as registry_api

    calls, started, release = _passes(registry_api, monkeypatch)

    first = asyncio.create_task(registry_api._reconcile_after_response("thread-mid-pass"))
    await started.wait()
    await registry_api._reconcile_after_response("thread-mid-pass")
    # Still coalesced: the second trigger did not start a concurrent scan.
    assert calls == ["pass"]

    release.set()
    await first

    assert calls == ["pass", "pass"]
    assert registry_api._reconcile_in_flight == set()
    assert registry_api._reconcile_rerun_requested == set()


async def test_many_triggers_mid_pass_coalesce_into_one_rerun(monkeypatch):
    """Several panes refetching after one broadcast cost one extra pass, not
    one pass each."""
    from api import registry_pane as registry_api

    calls, started, release = _passes(registry_api, monkeypatch)

    first = asyncio.create_task(registry_api._reconcile_after_response("thread-many-triggers"))
    await started.wait()
    for _ in range(5):
        await registry_api._reconcile_after_response("thread-many-triggers")

    release.set()
    await first

    assert calls == ["pass", "pass"]
    assert registry_api._reconcile_in_flight == set()
    assert registry_api._reconcile_rerun_requested == set()


async def test_a_failed_pass_still_honors_a_pending_rerun(monkeypatch):
    """A trigger must not be swallowed by the failure of the pass it landed in."""
    from api import registry_pane as registry_api

    calls: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def _apply(fn):
        calls.append("pass")
        if len(calls) == 1:
            started.set()
            await release.wait()
            raise RuntimeError("db worker down")
        return None

    monkeypatch.setattr(registry_api, "run_in_db", _apply)

    first = asyncio.create_task(registry_api._reconcile_after_response("thread-failed-pass"))
    await started.wait()
    await registry_api._reconcile_after_response("thread-failed-pass")
    release.set()
    await first

    assert calls == ["pass", "pass"]
    assert registry_api._reconcile_in_flight == set()


async def test_background_repair_failure_is_contained(monkeypatch):
    from api import registry_pane as registry_api

    async def _exploding(fn):
        raise RuntimeError("db worker down")

    monkeypatch.setattr(registry_api, "run_in_db", _exploding)

    await registry_api._reconcile_after_response("thread-y")

    assert registry_api._reconcile_in_flight == set()


def _pane(client, sid: str) -> dict:
    return client.get(f"/api/threads/{sid}/artifacts?token=" + _TOKEN).json()


def test_retry_clears_a_stale_needs_attention_row(pane_client, tmp_path, monkeypatch):
    """The full user loop for a transient I/O error on a file that was never
    actually missing: the banner appears, and Retry clears it.

    Nothing else can clear it. ``identity_version`` is the identity's
    ``updated_at``, which no part of this path changes, so a ``failed``
    receipt that is never retired keeps matching forever - a permanent
    "needs attention" row whose Retry button does nothing at all.
    """
    from registry import reconcile

    db = get_db()
    sid = make_session(db, prefix="pane-api")
    present = tmp_path / "present.md"
    present.write_text("here")
    art = models.register_artifact(
        integration="workspace", type="document", external_id=str(present),
        parent_thread_id=sid, title=present.name, path=str(present), db=db,
    )

    real_stat = os.stat

    def _denying_stat(path, *a, **kw):
        if str(path) == str(present):
            raise PermissionError(f"denied: {path}")
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(reconcile.os, "stat", _denying_stat)
    _pane(pane_client, sid)
    flagged = _pane(pane_client, sid)

    assert [i["artifact_id"] for i in flagged["needs_attention"]] == [art["id"]]
    assert flagged["needs_attention"][0]["retryable"] is True

    # The EACCES clears and the user presses Retry.
    monkeypatch.setattr(reconcile.os, "stat", real_stat)
    retry = pane_client.post(
        f"/api/threads/{sid}/artifacts/reconcile?token=" + _TOKEN
    )
    assert retry.status_code == 200

    after = _pane(pane_client, sid)
    assert after["needs_attention"] == []
    assert art["id"] in {n["id"] for n in after["nodes"] if n["kind"] == "artifact"}


def test_needs_attention_never_references_a_row_absent_from_the_pane(
    pane_client, tmp_path,
):
    """A quiet artifact has no pane row to name, so it must not raise an
    attention entry - the client can only render the literal 'Artifact'."""
    from registry import reconcile

    db = get_db()
    sid = make_session(db, prefix="pane-api")
    present = tmp_path / "hidden.md"
    present.write_text("here")
    art = models.register_artifact(
        integration="workspace", type="document", external_id=str(present),
        parent_thread_id=sid, title=present.name, path=str(present), db=db,
    )
    version = db.execute(
        "SELECT updated_at FROM artifact_identities WHERE id=?", (art["id"],),
    ).fetchone()["updated_at"]
    reconcile._write_receipt(
        db, artifact_id=art["id"], identity_version=version, thread_id=sid,
        action="skip", status="failed", reason="EIO: transient",
    )
    # The user removes it from the pane.
    db.execute(
        "UPDATE artifact_registrations SET surfacing='quiet' WHERE artifact_id=?",
        (art["id"],),
    )
    db.commit()

    body = _pane(pane_client, sid)

    assert art["id"] not in {n["id"] for n in body["nodes"] if n["kind"] == "artifact"}
    assert body["needs_attention"] == []
