"""Authenticated live-document frames, separate from the turn admission queue."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from pathlib import Path
import logging

from fastapi import HTTPException, WebSocketDisconnect

from api.files_load import read_document
from doceditor.document_scope import MAX_TEXT_BYTES, UI_THREAD
from doceditor.live_admission import OpenRefused, falls_back_to_plain
from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from doceditor.live_runtime import LiveEntry, get_documents
from doceditor.live_store import BrowserMessage, StoreRefused
from infra.db_async import run_in_db
from infra.state import send_to_client
from ws.document_failures import failure_state

FRAME_TYPES = {"doc_open", "doc_update", "doc_sync", "doc_flush", "doc_replace", "doc_choice", "doc_close", "doc_positions",
               "doc_release"}


@dataclass
class Subscription:
    entry: LiveEntry
    actor: WriteActor
    writable: bool
    user_id: str


_subscriptions: dict[object, dict[str, Subscription]] = {}
_commits: dict[str, asyncio.Task] = {}
_tasks: dict[object, set[asyncio.Task]] = {}
_openings: dict[object, set[asyncio.Task]] = {}
log = logging.getLogger(__name__)


def _encoded(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


_CLOSED_SOCKET_SEND_ERRORS = {
    "Unexpected ASGI message 'websocket.send', after sending 'websocket.close'",
    "Unexpected ASGI message 'websocket.send', after sending 'websocket.close' or response already completed.",
}


def _closed_socket_error(error: BaseException) -> bool:
    """Whether document delivery lost a socket after its work already completed."""
    return isinstance(error, (WebSocketDisconnect, ConnectionError)) or (
        isinstance(error, RuntimeError) and str(error) in _CLOSED_SOCKET_SEND_ERRORS)


def _observe_send(task: asyncio.Task) -> None:
    """Retrieve a broadcast write's outcome; a socket that closed first is expected."""
    if task.cancelled() or (error := task.exception()) is None:
        return
    if _closed_socket_error(error):
        log.debug("Live-document frame not delivered to a closed socket: %s", error)
    else:
        log.warning("Live-document frame not delivered", exc_info=error)


def _broadcast(artifact_id: str, frame: dict, *, exclude=None) -> None:
    for socket, subscriptions in tuple(_subscriptions.items()):
        if socket is not exclude and artifact_id in subscriptions:
            send_to_client(socket, {"artifact_id": artifact_id, **frame}).add_done_callback(_observe_send)


def _changed(artifact_id: str, entry: LiveEntry, update: bytes, sender, author: dict | None = None) -> None:
    frame = {"type": "doc_update", "store_id": entry.live.store.store_id,
             "update": _encoded(update), "digest": entry.live.digest,
             "ranges": [{"id": identifier, "from_off": start, "to_off": end}
                        for identifier, (start, end) in entry.live.ranges.items()]}
    if author is not None:
        frame["author"] = author
    _broadcast(artifact_id, frame, exclude=sender)


async def _commit(artifact_id: str, entry: LiveEntry) -> None:
    try:
        while entry.live.received:
            await entry.live.commit_received()
    except StoreRefused as exc:
        _broadcast(artifact_id, {"type": "doc_error", "kind": "store_refused", "message": str(exc)})
    finally:
        _commits.pop(artifact_id, None)


def _schedule_commit(artifact_id: str, entry: LiveEntry) -> None:
    if artifact_id not in _commits:
        _commits[artifact_id] = asyncio.create_task(_commit(artifact_id, entry))


async def _open(socket, user: dict, data: dict) -> None:
    manager = get_documents()
    if manager is None:
        raise OpenRefused("service_unavailable", "Live document service is unavailable")
    path, project_id, thread_id = data["path"], data.get("project_id"), data.get("thread_id")
    loaded = await run_in_db(read_document, path, user["id"], project_id, thread_id, live_open=True)
    artifact = loaded["artifact"]
    if artifact is None:
        await send_to_client(socket, {"type": "doc_opened", "live": False, "request_id": data.get("request_id")})
        return
    if data.get("expected_artifact_id") and artifact["id"] != data["expected_artifact_id"]:
        await send_to_client(socket, {"type": "doc_opened", "live": False, "request_id": data.get("request_id")})
        return
    resolved = Path(artifact["path"])
    actor = WriteActor(thread_id or UI_THREAD, project_id, direct_user=True, user_id=user["id"])
    try:
        entry = await manager.open(artifact["id"], resolved)
    except OpenRefused as refused:
        if not falls_back_to_plain(refused.kind):
            raise
        # The open already logged and reported the refusal; the browser holds the copy.
        await send_to_client(socket, {"type": "doc_opened", "live": False, "live_unavailable": refused.kind,
                                      "request_id": data.get("request_id")})
        return
    if entry is None:
        await send_to_client(socket, {"type": "doc_opened", "live": False, "request_id": data.get("request_id")})
        return
    artifact_id = artifact["id"]
    _subscriptions.setdefault(socket, {})[artifact_id] = Subscription(entry, actor, loaded["writable"], user["id"])
    entry.live.acknowledge = lambda ids: _broadcast(artifact_id, {
        "type": "doc_ack", "store_id": entry.live.store.store_id, "message_ids": list(ids)})
    # Reserve each socket's doc_update before the merge returns, so no later
    # answer that captured the merged state can be written ahead of it.
    entry.live.update_changed = lambda update, sender, author=None: _changed(artifact_id, entry, update, sender, author)
    entry.live.preview_changed = lambda frame: _broadcast(artifact_id, frame)
    def failure_changed(kind, message, provenance):
        frame = {"type": "doc_error" if message else "doc_cleared",
                 "store_id": entry.live.store.store_id, "kind": kind, **provenance}
        if message:
            frame.update(message=message, blocking=True)
        _broadcast(artifact_id, frame)
    entry.live.failure_changed = failure_changed
    entry.live.viewer_actor = lambda: _viewer_actor(artifact_id)
    entry.live.pending_changed = lambda: _schedule_pending(artifact_id, entry)
    entry.publisher.blocked = failure_changed
    state = await _pending_state(entry)
    from doceditor.live_comments import positions
    anchors = await positions(entry, artifact_id)
    failures = await failure_state(entry, reconcile=True)
    await send_to_client(socket, {"type": "doc_opened", "live": True, "artifact_id": artifact_id,
                                "store_id": entry.live.store.store_id, "update": _encoded(entry.live.doc.get_update()),
                                "vector": _encoded(entry.live.doc.get_state()), "digest": entry.live.digest,
                                "writable": loaded["writable"], "request_id": data.get("request_id"),
                                "ranges": anchors["ranges"], **state,
                                "setup_incarnation": manager.setup_incarnation, **failures})
    for failure in failures["blocking"]:
        provenance = {key: value for key, value in failure.items() if key not in {"kind", "message"}}
        failure_changed(failure["kind"], failure["message"], provenance)


def _viewer_actor(artifact_id: str) -> WriteActor | None:
    """The actor a last-viewer flush would use now: the latest remaining writable viewer's."""
    actors = [rows[artifact_id].actor for rows in _subscriptions.values()
              if artifact_id in rows and rows[artifact_id].writable]
    return actors[-1] if actors else None


async def _blocking(entry: LiveEntry) -> list[dict]:
    return (await failure_state(entry))["blocking"]


async def _pending_state(entry: LiveEntry) -> dict:
    async with entry.live.committer_lock:
        pending = await asyncio.to_thread(entry.live.store.pending)
        return {"pending": pending is not None and pending.state != "held",
                "held": await _held(entry, pending)}


def _schedule_pending(artifact_id: str, entry: LiveEntry) -> None:
    owners = [_tasks.setdefault(socket, set()) for socket, subscriptions in tuple(_subscriptions.items())
              if artifact_id in subscriptions]
    if not owners:
        return
    async def notify():
        try:
            state = await _pending_state(entry)
            _broadcast(artifact_id, {"type": "doc_pending", "store_id": entry.live.store.store_id, **state})
        except Exception:
            log.exception("Document pending state could not be sent for %s", artifact_id)
    task = asyncio.create_task(notify())
    # Existing socket-task ownership makes disconnect drain the notification
    # before closing its store; there is no independent notification lifetime.
    for owned in owners:
        owned.add(task)
        task.add_done_callback(owned.discard)


async def _held(entry: LiveEntry, pending) -> dict | None:
    if pending is None or pending.state != "held":
        return None
    from doceditor.live_text import clone
    outside = await asyncio.to_thread(entry.live.path.read_text, encoding="utf-8")
    return {"corsia_text": entry.live.text, "file_text": outside,
            "candidate_text": str(clone(pending.mirror_state)["text"]), "choice_pending": pending.choice is not None}


def _client_id(value) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("A comment range needs its client id")
    return value


def _offset(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("A comment range needs nonnegative offsets")
    return value


def _reserved(live, socket, user_id: str, ask) -> bool:
    """Track the capture's selection under its comment's client id when the browser's state is the live one.

    Everything is read and decided without yielding.
    """
    if not isinstance(ask, dict):
        raise ValueError("A comment range request must be an object")
    identifier, start, end = _client_id(ask.get("client_id")), _offset(ask.get("from_off")), _offset(ask.get("to_off"))
    digest, length = ask.get("digest"), _offset(ask.get("text_length_utf16"))
    if start >= end or not isinstance(digest, str) or not isinstance(ask.get("vector"), str):
        raise ValueError("A comment range needs a selection and the browser's state")
    return live.reserve(identifier, start, end, user_id, socket, vector=base64.b64decode(ask["vector"], validate=True),
                        digest=digest, length=length)


async def _operate(socket, user: dict, data: dict) -> None:
    artifact_id = data.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ValueError("Document frame needs an artifact id")
    subscription = _subscriptions.get(socket, {}).get(artifact_id)
    if subscription is None or subscription.user_id != user["id"]:
        await send_to_client(socket, {"type": "doc_error", "kind": "not_open", "artifact_id": artifact_id,
                                    "request_id": data.get("request_id")})
        return
    entry, live = subscription.entry, subscription.entry.live
    if data.get("store_id") != live.store.store_id:
        await send_to_client(socket, {"type": "doc_error", "kind": "store_mismatch", "artifact_id": artifact_id,
                                    "store_id": live.store.store_id, "request_id": data.get("request_id")})
        return
    kind = data["type"]
    if kind == "doc_positions":
        from doceditor.live_comments import positions
        seq = data.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise ValueError("Comment positions need a nonnegative sequence")
        if data.get("reserve") is not None:
            await send_to_client(socket, {"type": "doc_positions", "artifact_id": artifact_id,
                "store_id": live.store.store_id, "seq": seq, "request_id": data.get("request_id"),
                "reserved": _reserved(live, socket, subscription.user_id, data["reserve"])})
            return
        anchors = await positions(entry, artifact_id, reload=bool(data.get("reload")))
        await send_to_client(socket, {"type": "doc_positions", "artifact_id": artifact_id,
            "store_id": live.store.store_id, "seq": seq, "request_id": data.get("request_id"),
            **anchors, "vector": _encoded(anchors["vector"])})
        return
    if kind == "doc_release":
        live.release_held(_client_id(data.get("client_id")), socket)
        return
    if kind == "doc_close":
        await _leave(socket, artifact_id)
        return
    if kind == "doc_sync" and "update" not in data:
        vector = base64.b64decode(data.get("vector", ""), validate=True)
        await send_to_client(socket, {"type": "doc_update", "artifact_id": artifact_id,
                                    "store_id": live.store.store_id, "update": _encoded(live.doc.get_update(vector)),
                                    "digest": live.digest})
        return
    if not subscription.writable:
        raise PermissionError("This document is read-only")
    if kind in {"doc_update", "doc_sync"}:
        encoded = data["update"]
        if len(encoded) > 4 * ((MAX_TEXT_BYTES + 2) // 3):
            raise ValueError("Document update is too large")
        identifier = data["message_id"]
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
            raise ValueError("Document update needs a message id")
        message = BrowserMessage(identifier, base64.b64decode(encoded, validate=True))
        if not live.receive(message, sender=socket):
            raise StoreRefused("The server backlog is full; keep this update on the device and retry")
        await send_to_client(socket, {"type": "doc_received", "artifact_id": artifact_id,
                                    "store_id": live.store.store_id, "message_ids": [identifier]})
        _schedule_commit(artifact_id, entry)
        return
    if kind == "doc_flush" and data.get("retry") is True:
        await entry.lane.retry.retry_blocked()
        if entry.policy is not None:
            entry.lane.schedule_setup(lambda: entry.policy.setup(subscription.actor))
        await entry.publisher.retry_blocked()
    if kind == "doc_choice":
        result = await entry.lane.choose(data["choice"], actor=subscription.actor)
    else:
        operation = Operation("replace", content=data["text"], actor=subscription.actor) if kind == "doc_replace" else Operation("flush", actor=subscription.actor)
        result = await entry.lane.submit(operation)
    result.require_landed()
    state = await _pending_state(entry)
    await send_to_client(socket, {"type": "doc_saved", "artifact_id": artifact_id, "store_id": live.store.store_id,
                                "server_saved": result.server_saved, "request_id": data.get("request_id"),
                                "digest": live.digest, **state,
                                "blocking": await _blocking(entry)})


async def handle_frame(socket, user: dict, data: dict) -> None:
    """Dispatch one authenticated frame; errors retain browser and store evidence."""
    try:
        if data["type"] == "doc_open":
            await _open(socket, user, data)
        else:
            await _operate(socket, user, data)
    except (HTTPException, ValueError, PermissionError, StoreRefused, OSError) as exc:
        title = getattr(exc, "title", None)
        await send_to_client(socket, {"type": "doc_error", "artifact_id": data.get("artifact_id"),
                                    "request_id": data.get("request_id"),
                                    "kind": getattr(exc, "kind", "store_refused" if isinstance(exc, StoreRefused) else "refused"),
                                    "blocking": isinstance(exc, StoreRefused) or getattr(exc, "blocking", False),
                                    **({"title": title} if title else {}),
                                    "message": str(exc.detail) if isinstance(exc, HTTPException) else str(exc)})
    except Exception as error:
        if _closed_socket_error(error):
            log.debug("Live-document frame not delivered to a closed socket: %s", error)
            return
        log.exception("Live document frame failed")
        await send_to_client(socket, {"type": "doc_error", "artifact_id": data.get("artifact_id"),
                                    "request_id": data.get("request_id"), "kind": "internal",
                                    "message": "The document operation failed; keep your device copy and retry"})


def schedule_frame(socket, user: dict, data: dict) -> None:
    """Keep file/store waits off the receiver so turn and Stop frames still flow."""
    owned = _tasks.setdefault(socket, set())
    # Updates following an open wait only for admission, never for a file flush.
    opening_tasks = _openings.setdefault(socket, set())
    openings = tuple(opening_tasks)
    async def dispatch():
        if openings:
            await asyncio.gather(*openings)
        await handle_frame(socket, user, data)
    task = asyncio.create_task(dispatch())
    owned.add(task)
    if data["type"] == "doc_open":
        opening_tasks.add(task)
        task.add_done_callback(opening_tasks.discard)
    task.add_done_callback(owned.discard)


async def _leave(socket, artifact_id: str) -> None:
    subscriptions = _subscriptions.get(socket, {})
    subscription = subscriptions.pop(artifact_id, None)
    if subscription is not None:
        subscription.entry.live.release_holder(socket)
    if subscription is None or any(artifact_id in rows for rows in _subscriptions.values()):
        return
    async def flush():
        task = _commits.get(artifact_id)
        if task:
            await asyncio.shield(task)
        await subscription.entry.live.commit_received()
        if subscription.writable:
            result = await subscription.entry.lane.submit(Operation("flush", actor=subscription.actor))
            result.require_landed()
    manager = get_documents()
    if manager is not None:
        await manager.evict(artifact_id, subscription.entry, before_close=flush,
                            can_evict=lambda: not any(artifact_id in rows for rows in _subscriptions.values()))


async def disconnect(socket) -> None:
    """Finish started document tasks and evict after the final viewer's flush."""
    # Only sockets admitted to this owner have document work to finish.
    if not any(owner is socket for owner in _tasks) and not any(owner is socket for owner in _subscriptions):
        return
    try:
        tasks = _tasks.pop(socket, set())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for artifact_id in tuple(_subscriptions.get(socket, {})):
            try:
                await _leave(socket, artifact_id)
            except Exception:
                log.exception("Document disconnect failed for %s", artifact_id)
    finally:
        _subscriptions.pop(socket, None)
        _openings.pop(socket, None)
