"""A broadcast to a socket that already closed is observed, and other viewers still get the frame."""
import asyncio
import gc
import logging
import pytest
from tests.test_document_live_transport import Socket


class ClosedSocket:
    async def send_json(self, frame):
        raise RuntimeError("Unexpected ASGI message 'websocket.send', after sending 'websocket.close'")


@pytest.mark.asyncio
async def test_closed_subscriber_send_is_observed_and_others_receive(monkeypatch, caplog):
    from ws import document_control
    open_socket, closed_socket = Socket(), ClosedSocket()
    monkeypatch.setattr(document_control, "_subscriptions",
                        {closed_socket: {"artifact-1": object()}, open_socket: {"artifact-1": object()}})
    loop = asyncio.get_running_loop()
    unretrieved = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unretrieved.append(context))
    try:
        with caplog.at_level(logging.DEBUG, logger=document_control.__name__):
            document_control._broadcast("artifact-1", {"type": "doc_update"})
            frame = await open_socket.next("doc_update")
            for _ in range(3):
                await asyncio.sleep(0)
        gc.collect()
    finally:
        loop.set_exception_handler(previous)
    assert frame["artifact_id"] == "artifact-1"
    assert unretrieved == []
    assert [r.levelno for r in caplog.records if "closed socket" in r.getMessage()] == [logging.DEBUG]
