from __future__ import annotations

import os
import sys
import uuid

import pytest
from auth_fixtures import OWNER_TOKEN, OTHER_TOKEN

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_OWNER = OWNER_TOKEN
_OTHER = OTHER_TOKEN

DOC_BODY = (
    "# Design\n"
    "\n"
    "The conductor owns the communication graph.\n"
    "\n"
    "The conductor owns the communication graph.\n"
    "A unique tail sentence anchors here.\n"
)


@pytest.fixture(autouse=True)
def _registrable_roots(integration_workspace, monkeypatch):
    from tools import sandbox_config

    monkeypatch.setattr(
        sandbox_config,
        "_STATIC_ALLOWED_RW",
        [integration_workspace.resolve()],
    )


def _mk_thread(client, token, title="worker thread"):
    r = client.post(f"/api/threads?token={token}", json={"title": title})
    assert r.status_code == 200, r.text
    return r.json()["thread_id"]


@pytest.fixture
def doc(integration_workspace):
    p = integration_workspace / f"design-{uuid.uuid4().hex[:8]}.md"
    p.write_text(DOC_BODY)
    yield p
    p.unlink(missing_ok=True)


def _post(client, token, **body):
    return client.post(f"/api/delegation/docs/comments?token={token}", json=body)


def _patch(client, token, comment_id, **body):
    return client.patch(
        f"/api/delegation/docs/comments/{comment_id}?token={token}", json=body
    )


def _comment_by_thread_a(client, doc):
    a = _mk_thread(client, _OWNER, title="thread-A")
    top = _post(
        client,
        _OWNER,
        doc_path=str(doc),
        quote="A unique tail sentence",
        body="by A",
        as_session_id=a,
    )
    return a, top.json()["comment"]["id"]
