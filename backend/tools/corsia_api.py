"""Native, token-free, bounded calls through the application's real handlers."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import urlencode

from starlette.routing import Match

from infra.db_async import run_in_db
from infra.internal_principal import internal_request
from tools.base import Tool
from tools.conductor_api_policy import (
    contains_forbidden_fields,
    refused_body_field,
    refused_repo_link,
    registered_routes,
    route_class,
    safe_response,
)
from tools.conductor_api_identity import attribution_matches, bind_delegation_origin

# Match the native tool output budget; stop collecting, rather than buffering
# an unbounded response and truncating it after execution.
MAX_RESPONSE_BYTES = 32_768
MAX_REQUEST_BYTES = 32_768
REQUEST_TIMEOUT_SECONDS = 30

logger = logging.getLogger(__name__)


class ApiCallError(ValueError):
    """A bounded call could not be admitted or completed."""


def conductor_user(actor_thread_id: str) -> str:
    from infra.conductor import get_coordination_scopes
    from infra.database import get_db
    from infra.queries import thread_available_sql

    db = get_db()
    try:
        actor = db.execute(
            f"SELECT user_id FROM threads WHERE id=? AND {thread_available_sql()}",
            (actor_thread_id,),
        ).fetchone()
        if actor is None or not get_coordination_scopes(actor_thread_id, db=db):
            raise ApiCallError("Missing conductor capability; only the CORSIA agent or a designated project conductor can use corsia_api")
        return str(actor["user_id"])
    finally:
        db.close()


def _scope(method: str, path: str, query: dict, body: dict | None, *, actor: str | None = None) -> tuple[dict, bytes]:
    if not re.fullmatch(r"/api(?:/[A-Za-z0-9_~.{}:@+/-]+)?", path) or any(
        segment in {".", ".."} for segment in path.split("/")
    ) or "//" in path:
        raise ApiCallError("path must be a local /api path, without URL, query, escape or traversal")
    from tools.conductor_api_paths import forbidden_path

    if actor is not None:
        if not attribution_matches(query, actor) or not attribution_matches(body, actor):
            raise ApiCallError("Acting identity cannot be changed; sender and creator must be the calling thread")
        body = bind_delegation_origin(path, body, actor)
    if forbidden_path(query, for_write=method != "GET") or forbidden_path(body, for_write=method != "GET"):
        raise ApiCallError("Protected file paths are off-limits; use the native scoped file tools")
    if contains_forbidden_fields(query) or contains_forbidden_fields(body):
        raise ApiCallError("Credential and host-access fields are off-limits; use the human settings surface")
    if refusal := refused_body_field(path, method, body) or refused_repo_link(path, method, body):
        raise ApiCallError(refusal)
    # Legacy signatures require a query argument; this literal carries no
    # authentication authority. Only the revocable task-local principal does.
    query_bytes = urlencode({**query, "token": "internal-request"}, doseq=True).encode()
    payload = json.dumps(body).encode() if body is not None else b""
    if len(query_bytes) + len(payload) > MAX_REQUEST_BYTES:
        raise ApiCallError("Request exceeds the native tool size limit; reduce the query/body")
    return {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": query_bytes, "root_path": "",
        "headers": [(b"content-type", b"application/json"), (b"host", b"in-process")],
        "server": ("in-process", 0), "client": ("in-process", 0),
    }, payload


async def call_application(app, scope: dict, payload: bytes, user_id: str, actor: str) -> dict:
    route = next((route for route in registered_routes(app) if route.matches(scope)[0] == Match.FULL), None)
    if route is None:
        raise ApiCallError(f"No CORSIA API route matches {scope['method']} {scope['path']}")
    if route_class(route) != "open":
        raise ApiCallError("Route is off-limits for corsia_api (person-only); use the human surface and report the missing capability")
    status = 500
    response = bytearray()
    consumed = False
    completed = asyncio.Event()

    async def receive() -> dict:
        nonlocal consumed
        if not consumed:
            consumed = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await completed.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            if len(response) + len(message.get("body", b"")) > MAX_RESPONSE_BYTES:
                raise ApiCallError("Response exceeds the native tool size limit; request a smaller page or summary")
            response.extend(message.get("body", b""))
            if not message.get("more_body", False):
                completed.set()

    with internal_request(user_id, actor):
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            await app(scope, receive, send)
    if status >= 400:
        result: dict[str, Any] = {"status": status, "error": "API request failed; check the route schema, target and current state"}
        if status == 422:
            try:
                details = json.loads(response).get("detail", [])
                if isinstance(details, list):
                    result["validation_errors"] = [
                        {"field": item.get("loc"), "type": item.get("type")}
                        for item in details if isinstance(item, dict)
                    ]
            except (ValueError, AttributeError):
                pass
        return result
    try:
        data = json.loads(response)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiCallError("Route did not return JSON; use its native file or download tool") from exc
    return {"status": status, "data": safe_response(data)}


class CorsiaApiTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "corsia_api"

    @property
    def description(self) -> str:
        return (
            "Call any CORSIA API route in-process as the CORSIA agent or a project conductor. "
            "Load skill coordination-api before use. No credential is needed. Full access except the "
            "person-only list: auth and users, credentials and secrets, host-access authority, "
            "answering the person's decisions, conductor designation, sign-in flows, webhooks, "
            "UI handlers that record the person as actor, and tool recursion. "
            "Other threads lack this capability. Returns bounded JSON; "
            "writes use the same handlers, audit and events as the UI. Use only for user-authorized effects."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "required": ["method", "path"],
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                "path": {"type": "string", "pattern": "^/api(?:/.*)?$"},
                "query": {"type": "object"}, "body": {"type": "object"},
            },
        }

    async def execute(self, method: str, path: str, query: dict | None = None,
                      body: dict | None = None, **kwargs: Any) -> str:
        actor = kwargs.get("_thread_id")
        if not isinstance(actor, str) or not actor:
            return "Error: current thread identity is unavailable; retry from a live CORSIA agent or conductor turn."
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            return "Error: method must be GET, POST, PUT, PATCH or DELETE."
        try:
            user_id = await run_in_db(conductor_user, actor)
            scope, payload = _scope(method, path, query or {}, body, actor=actor)
            from main import app

            result = await call_application(app, scope, payload, user_id, actor)
            return json.dumps(result, ensure_ascii=False)
        except ApiCallError as exc:
            return f"Error: {exc}."
        except TimeoutError:
            return "Error: in-process API call timed out; verify destination state before retrying a write."
        except Exception:
            # Handler exceptions may contain secrets; never return their text.
            logger.exception("In-process API call failed: method=%s path=%s", method, path)
            return "Error: in-process API call failed; verify destination state before retrying a write."
