"""A comment post's timing header is logged or dropped whole, and a retried live capture finds its stored comment."""
import asyncio
import logging

import httpx
import pytest
from fastapi import FastAPI
from loguru import logger

from api import doc_comments
from api.doc_comment_models import CommentCreate
from api.comment_post_timing import COMMENT_TIMING_HEADER, MAX_HEADER_BYTES, log_comment_timing, parse_comment_timing
from auth_fixtures import OWNER_TOKEN
from doceditor.live_document import IdentityInUse
from infra import app_loop
from infra.otel_logs.capture import CaptureQueue, LogCapture
from tests.test_document_live_file_tools import opened
from tests.test_live_comment_creation import invoke, reserve
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from ws import document_control

GOOD = "n=3,a=2,cap=41,ens=0,pn=12007,out=timeout"


def test_parse_keeps_allowlisted_numbers_and_outcome():
    assert parse_comment_timing(GOOD) == {"n": 3, "a": 2, "cap": 41, "ens": 0, "pn": 12007, "out": "timeout"}
    assert parse_comment_timing("n=4,rec=1,recms=230,post=900,app=850,total=1200,out=delivered")["recms"] == 230


@pytest.mark.parametrize("raw", [
    None, "",
    "n=1,body=hello",                 # an unknown key could carry text
    "n=1,n=2",                        # a repeated key
    "n=-1", "n=1.5", "n=1234567890",  # not a 1-9 digit count
    "out=lost",                       # not an outcome the browser sends
    "n=1,,a=2", "n", "n=1;a=2",       # not key=value pairs
    "n=1," + ",".join(f"a{i}=1" for i in range(200)),
    "n=" + "1" * MAX_HEADER_BYTES,    # over the size cap
])
def test_parse_drops_a_malformed_header_whole(raw):
    assert parse_comment_timing(raw) is None


# Every key the browser sends at once, each at its longest value: the header the size cap is derived from.
LONGEST = ("n=999999999,a=999999999,cap=999999999,ens=999999999,pn=999999999,rec=1,out=delivered,"
           "total=999999999,post=999999999,app=999999999,recms=999999999")


def test_the_size_cap_is_the_longest_header_the_browser_builds():
    assert len(LONGEST) == MAX_HEADER_BYTES == 145
    assert parse_comment_timing(LONGEST) is not None
    assert parse_comment_timing(LONGEST + " ") is None


@pytest.mark.parametrize(("artifact_id", "logged"), [
    ("6017b05b-497", "6017b05b-497"),  # str(uuid.uuid4())[:12], the one shape artifact ids are made in
    ("6017b05b-4970", "-"), ("6017B05B-497", "-"), ("../etc", "-"),
])
def test_only_an_artifact_id_in_its_made_shape_is_logged(artifact_id, logged):
    lines: list[str] = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    try:
        log_comment_timing(GOOD, "GET", artifact_id)
    finally:
        logger.remove(sink)
    assert [line.strip() for line in lines] == [f"comment_post_timing method=GET artifact={logged} {GOOD.replace(',', ' ')}"]


@pytest.mark.asyncio
async def test_get_and_post_log_a_good_header_and_ignore_a_bad_one(ownership_scope, tmp_path, monkeypatch):
    """Read from the log capture as production attaches it: a line logged through stdlib at INFO never reaches it."""
    _, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    capture = LogCapture(CaptureQueue())
    app_loop.capture(asyncio.get_running_loop())
    app = FastAPI()
    app.include_router(doc_comments.router)
    url = f"/api/artifacts/{entry.artifact_id}/comments"
    # The second POST repeats the first's client id, so the server answers it with the stored row.
    reserve(ownership_scope, entry, "timed")
    body = dict(delivery_policy="immediate", body="Timing evidence", anchor_text="b", from_off=2, to_off=3,
                live_capture=True, client_id="timed")
    capture.attach()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get(url, params={"token": OWNER_TOKEN}, headers={COMMENT_TIMING_HEADER: GOOD})).status_code == 200
            posted = await client.post(url, params={"token": OWNER_TOKEN}, headers={COMMENT_TIMING_HEADER: "n=4,cap=9"}, json=body)
            assert posted.status_code == 200, posted.text
            bad = await client.get(url, params={"token": OWNER_TOKEN}, headers={COMMENT_TIMING_HEADER: "n=5,body=Timing evidence"})
            assert bad.status_code == 200
            assert (await client.post(url, params={"token": OWNER_TOKEN}, headers={COMMENT_TIMING_HEADER: "x" * 600},
                                      json=body)).status_code == 200
    finally:
        capture.detach()
    records = [r for r in capture.queue.drain(10_000, 1 << 40) if r.body.startswith("comment_post_timing")]
    assert {(r.severity_text, r.logger) for r in records} == {("INFO", "api.comment_post_timing")}
    lines = [r.body for r in records]
    assert lines == [
        f"comment_post_timing method=GET artifact={entry.artifact_id} n=3 a=2 cap=41 ens=0 pn=12007 out=timeout",
        f"comment_post_timing method=POST artifact={entry.artifact_id} n=4 cap=9",
    ]
    assert all("Timing evidence" not in line for line in lines)


@pytest.mark.asyncio
async def test_handlers_called_directly_without_the_timing_header_still_work(ownership_scope, tmp_path, monkeypatch):
    """A direct call passes no header: the handlers must not see FastAPI's marker in its place."""
    _, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    reserve(ownership_scope, entry, "direct")
    created = await doc_comments.create_comment(entry.artifact_id, OWNER_TOKEN, CommentCreate(
        delivery_policy="immediate", body="Direct call", anchor_text="b", from_off=2, to_off=3,
        live_capture=True, client_id="direct"))
    assert created["comment"]["id"] == "direct"
    listed = await doc_comments.list_comments(entry.artifact_id, OWNER_TOKEN)
    assert "direct" in [c["id"] for c in listed["comments"]]


@pytest.mark.asyncio
async def test_reserving_a_stored_comments_id_is_refused_as_identity_in_use(ownership_scope, tmp_path, monkeypatch):
    """A retried capture under the draft's one id, after its first POST stored the comment, is told the id is taken."""
    _, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    stored = await invoke("http", ownership_scope, entry, "draft-one-id")
    assert stored["comment"]["id"] == "draft-one-id"
    with pytest.raises(IdentityInUse) as refused:
        reserve(ownership_scope, entry, "draft-one-id")
    assert refused.value.kind == "identity_in_use"


@pytest.mark.asyncio
async def test_an_identity_refusal_reaches_the_browser_by_its_kind(monkeypatch):
    sent = []
    async def send(socket, frame):
        sent.append(frame)
    async def operate(socket, user, data):
        raise IdentityInUse("Comment identity is already in use")
    monkeypatch.setattr(document_control, "send_to_client", send)
    monkeypatch.setattr(document_control, "_operate", operate)
    await document_control.handle_frame(object(), {"id": "u"}, {"type": "doc_reserve", "artifact_id": "a", "request_id": "r"})
    assert sent == [{"type": "doc_error", "artifact_id": "a", "request_id": "r", "kind": "identity_in_use",
                     "blocking": False, "message": "Comment identity is already in use"}]


@pytest.mark.asyncio
async def test_a_reply_posted_again_under_its_drafts_id_is_stored_once(ownership_scope, tmp_path, monkeypatch):
    """A reply's retry posts under the draft's one id: the server answers with the reply stored first, never a second one."""
    _, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    root = (await invoke("http", ownership_scope, entry, "root-thread"))["comment"]["id"]
    app = FastAPI()
    app.include_router(doc_comments.router)
    url = f"/api/artifacts/{entry.artifact_id}/comments"
    reply = dict(delivery_policy="immediate", parent_id=root, client_id="reply-one-id")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(url, params={"token": OWNER_TOKEN}, json={**reply, "body": "Half a reply"})
        again = await client.post(url, params={"token": OWNER_TOKEN}, json={**reply, "body": "Half a reply"})
        # Changed after a press with no answer: still the stored reply, which the browser shows as the earlier version.
        changed = await client.post(url, params={"token": OWNER_TOKEN}, json={**reply, "body": "Half a reply, changed"})
    assert [r.status_code for r in (first, again, changed)] == [200, 200, 200], (first.text, again.text, changed.text)
    assert {r.json()["comment"]["id"] for r in (first, again, changed)} == {"reply-one-id"}
    assert changed.json()["comment"]["body"] == "Half a reply"
    listed = await doc_comments.list_comments(entry.artifact_id, OWNER_TOKEN)
    thread = next(c for c in listed["comments"] if c["id"] == root)
    assert [r["id"] for r in thread["replies"]] == ["reply-one-id"]
