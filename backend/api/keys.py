"""Settings › Keys: keys agents use but can never read.

Two routes accept any signed-in caller, so any agent (the workspace conductor
through ``corsia_api`` among them): list entries' metadata, and add an inert
entry by name, label and description. Neither ever carries a value. Every other route is the person's alone: it needs a
signed-in human session from the ``Authorization`` header, and the writes
(set, replace, clear, remove) also need the account password in the same
request (``api.key_write_guard``).

These routes parse their own request bodies instead of declaring a model,
because FastAPI's validation error echoes what was submitted; a refusal here
carries a fixed code and message and never the input.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from api.key_write_guard import NOT_SAVED, refusal, verified_write
from infra import key_store
from infra.auth import authenticate, require_user, token_from_request
from infra.db_async import run_in_db
from infra.time import utc_storage_iso_to_api
from tools.web_search_outcome import CHECK_QUERY, run_search

router = APIRouter(prefix="/api/keys", tags=["keys"])
log = logging.getLogger(__name__)

#: Largest request body these routes read; a value plus a password fits easily.
MAX_BODY_BYTES = 8192


def _human_session(authorization: str | None = Header(None)) -> tuple[dict, str]:
    """The signed-in person's session and its token, from the header only."""
    token = token_from_request(None, authorization)
    user = authenticate(token)
    if not user or not token:
        raise HTTPException(401, "Invalid or expired session")
    if user.get("token_kind") != "session":
        raise HTTPException(
            403,
            "This change needs the signed-in person: open CORSIA and make it there.",
        )
    return user, token


async def _body(request: Request) -> dict[str, Any]:
    """Read a bounded JSON object; any failure is a fixed refusal."""
    try:
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError
        parsed = json.loads(raw) if raw else {}
        if not isinstance(parsed, dict):
            raise ValueError
        return parsed
    except Exception:
        raise refusal(400, "invalid_request", NOT_SAVED) from None


def _public(entry: dict) -> dict:
    return {**entry, "set_at": utc_storage_iso_to_api(entry["set_at"])}


def _store_refusal(exc: key_store.KeyStoreError) -> HTTPException:
    return refusal(400, "key_store", str(exc))


def _added_by(user: dict) -> tuple[str | None, str | None]:
    thread_id = user.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        return None, user.get("name")
    from infra.database import get_db

    row = get_db().execute("SELECT title FROM threads WHERE id = ?", (thread_id,)).fetchone()
    return thread_id, (row["title"] if row and row["title"] else None)


@router.get("")
async def list_keys(user: dict = Depends(require_user)) -> dict:
    """Every entry's metadata. Never a value."""
    entries = await run_in_db(key_store.list_entries)
    return {"keys": [_public(entry) for entry in entries]}


@router.post("")
async def add_key(request: Request, user: dict = Depends(require_user)) -> dict:
    """Add an entry with no value. It does nothing until code that uses it ships."""
    body = await _body(request)

    def add() -> dict:
        thread_id, name = _added_by(user)
        return key_store.add_entry(
            body.get("name"), body.get("label"), body.get("description"),
            added_by_thread_id=thread_id, added_by_name=name,
        )

    try:
        entry = await run_in_db(add)
    except key_store.KeyStoreError as exc:
        raise _store_refusal(exc) from None
    return {"key": _public(entry)}


async def _guarded(
    request: Request, session: tuple[dict, str], name: str, effect: Callable[[dict], None],
) -> dict:
    user, token = session
    body = await _body(request)
    try:
        await verified_write(token, user["id"], body.get("password"), lambda: effect(body))
        entry = await run_in_db(key_store.entry_metadata, name)
    except key_store.KeyStoreError as exc:
        raise _store_refusal(exc) from None
    return {"key": _public(entry)}


@router.put("/{name}/value")
async def set_key_value(
    name: str, request: Request, session: tuple[dict, str] = Depends(_human_session),
) -> dict:
    """Set or replace an entry's value. The response never shows it back."""
    return await _guarded(
        request, session, name, lambda body: key_store.set_value(name, body.get("value")),
    )


@router.post("/{name}/clear")
async def clear_key_value(
    name: str, request: Request, session: tuple[dict, str] = Depends(_human_session),
) -> dict:
    """Remove an entry's value from Settings › Keys, keeping the entry."""
    return await _guarded(request, session, name, lambda body: key_store.clear_value(name))


@router.post("/{name}/remove")
async def remove_key(
    name: str, request: Request, session: tuple[dict, str] = Depends(_human_session),
) -> dict:
    """Remove an added entry and its value."""
    user, token = session
    body = await _body(request)
    try:
        await verified_write(token, user["id"], body.get("password"), lambda: key_store.remove_entry(name))
    except key_store.KeyStoreError as exc:
        raise _store_refusal(exc) from None
    return {"removed": name}


@router.post("/{name}/check")
async def check_key(name: str, user: dict = Depends(require_user)) -> dict:
    """Run one search with the key search would use, and say what it came to."""
    builtin = key_store.BUILTINS.get(name)
    if builtin is None:
        raise refusal(404, "not_checkable", "No check exists for that key.")
    try:
        resolved = await run_in_db(key_store.resolve_builtin, builtin)
    except key_store.KeyStoreError as exc:
        raise _store_refusal(exc) from None
    if resolved is None:
        return {"outcome": "not_set", "reason": None}
    outcome = await run_search(resolved[0], CHECK_QUERY, 1)
    return {"outcome": outcome.kind, "reason": outcome.reason}
