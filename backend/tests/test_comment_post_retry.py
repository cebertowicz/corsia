"""A comment post's timing header is logged or dropped whole, and a retried live capture finds its stored comment."""
import asyncio
import logging

import httpx
import pytest
from fastapi import FastAPI

from api import doc_comments
from api.comment_post_timing import COMMENT_TIMING_HEADER, MAX_HEADER_BYTES, parse_comment_timing
from auth_fixtures import OWNER_TOKEN
from doceditor.live_document import IdentityInUse
from infra import app_loop
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


@pytest.mark.asyncio
async def test_get_and_post_log_a_good_header_and_ignore_a_bad_one(ownership_scope, tmp_path, monkeypatch, caplog):
    _, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    app = FastAPI()
    app.include_router(doc_comments.router)
    url = f"/api/artifacts/{entry.artifact_id}/comments"
    # The second POST repeats the first's client id, so the server answers it with the stored row.
    reserve(ownership_scope, entry, "timed")
    body = dict(delivery_policy="immediate", body="Timing evidence", anchor_text="b", from_off=2, to_off=3,
                live_capture=True, client_id="timed")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        with caplog.at_level(logging.INFO, logger="api.comment_post_timing"):
            assert (await client.get(url, params={"token": OWNER_TOKEN}, headers={COMMENT_TIMING_HEADER: GOOD})).status_code == 200
            posted = await client.post(url, params={"token": OWNER_TOKEN}, headers={COMMENT_TIMING_HEADER: "n=4,cap=9"}, json=body)
            assert posted.status_code == 200, posted.text
            bad = await client.get(url, params={"token": OWNER_TOKEN}, headers={COMMENT_TIMING_HEADER: "n=5,body=Timing evidence"})
            assert bad.status_code == 200
            assert (await client.post(url, params={"token": OWNER_TOKEN}, headers={COMMENT_TIMING_HEADER: "x" * 600},
                                      json=body)).status_code == 200
    lines = [record.getMessage() for record in caplog.records if record.name == "api.comment_post_timing"]
    assert lines == [
        f"comment_post_timing method=GET artifact={entry.artifact_id} n=3 a=2 cap=41 ens=0 pn=12007 out=timeout",
        f"comment_post_timing method=POST artifact={entry.artifact_id} n=4 cap=9",
    ]
    assert all("Timing evidence" not in line for line in lines)


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
