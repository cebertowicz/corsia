"""Per-run forwarder that sends a request carrying a saved item, so the script never holds the value.

An approved run whose approval names a saved item with a place gets this h11 server on its own socket
(``socket_dir/"s"``, mode 0600), built like ``script_github_proxy``. The script (``corsia send``) asks it for one
request in absolute form, ``https://<place>/<path>``, naming each item by a placeholder: ``{{corsia-item:<name>}}``
for a Settings › Keys item, ``{{corsia-login:<id>}}`` for a saved login's password. A placeholder may sit in a
header value, where the value goes in as it is, or in a JSON body, where it goes in JSON-escaped; anywhere else
the request is refused. Every item the request names must be one the approval lists with this place, and must
still pass the fills' own check for the owner; otherwise nothing is sent.

A GitHub place goes through the run's GitHub forwarder (``socket_dir/"g"``), which adds the login, the read
budget and the host allowlist; there the ``Authorization`` header is that forwarder's, so no placeholder may sit
in it. Every other place is sent direct. The response is read uncompressed up to the limit, and every saved value
in it is hidden before the script gets it. Each item a sent request used records one row in the fills' history
(``saved_login_results.audit``), never the value. Logs carry ids, fixed reasons and exception type names only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import h11
import httpx

from automation import script_blanking
from automation.script_github_proxy import (
    _ALLOWED_HOSTS, OVER_RESPONSE, UNCHECKED, _has_body, _read_request, _request_body, _reply,
)
from automation.script_items import ItemsUnavailable, ItemUse, RunItems, check_use
from automation.script_schedules import DEFAULT_TIMEOUT_SECONDS
from automation.script_tool_channel import REQUEST_LIMIT
from infra import saved_items, saved_logins
from infra.database import write_transaction
from infra.key_store_values import KeyStoreError
from infra.saved_login_results import audit

logger = logging.getLogger(__name__)

SOCKET_ENV = "CORSIA_SCRIPT_SEND_SOCKET"
_PLACEHOLDER_BYTES = re.compile(rb"\{\{corsia-(item|login):([A-Za-z0-9._-]{1,80})\}\}")
_DROPPED = frozenset({b"connection", b"keep-alive", b"transfer-encoding", b"content-length", b"host", b"te",
                      b"upgrade", b"proxy-connection", b"proxy-authorization", b"accept-encoding",
                      b"content-encoding"})
_MIB = REQUEST_LIMIT // (1024 * 1024)

_TARGET = b"CORSIA sends a saved item only to https://<place>/<path>, port 443, named in full in the request.\n"
_WHERE = b"A saved item's placeholder goes only in a header value or a JSON body (Content-Type: application/json).\n"
_NONE = (b"Name a saved item in the request with {{corsia-item:<name>}} or {{corsia-login:<id>}}; "
         b"CORSIA sends only requests that use one.\n")
_UNLISTED = b"This script's approval doesn't list that saved item.\n"
_GIVEN = b"This script's approval gives that saved item to the script; read it with corsia item instead.\n"
_HOST = b"This script's approval sends that saved item only to another place.\n"
_GITHUB_AUTH = b"On a GitHub address the Authorization header is CORSIA's GitHub login; put the saved item elsewhere.\n"
_NO_GITHUB = b"This script has no GitHub login, so CORSIA can't send to a GitHub address for it.\n"
_GONE = b"Saved item is no longer available to this script.\n"
OVER_REQUEST = f"The request is larger than {_MIB} MiB, so CORSIA didn't send it.\n".encode()
_FAILED = b"The request failed before an answer came back.\n"


class _Refused(Exception):
    def __init__(self, status: int, body: bytes, reason: str) -> None:
        super().__init__(reason)
        self.status, self.body, self.reason = status, body, reason


def _target(request: h11.Request) -> tuple[str, str]:
    """The place and origin-form path of an absolute ``https://`` target on port 443; refused otherwise."""
    try:
        parts = urlsplit(request.target.decode("ascii"))
        port = parts.port
    except (UnicodeDecodeError, ValueError):
        raise _Refused(403, _TARGET, "target") from None
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or port not in (None, 443)):
        raise _Refused(403, _TARGET, "target")
    return parts.hostname, (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def _named(request: h11.Request, body: bytes, host: str) -> set[tuple[str, str]]:
    """Every placeholder the request names, refused where it may not sit."""
    if _PLACEHOLDER_BYTES.search(request.target) or any(_PLACEHOLDER_BYTES.search(name) for name, _ in request.headers):
        raise _Refused(403, _WHERE, "placement")
    named = {(kind.decode(), key.decode()) for _, value in request.headers for kind, key in
             _PLACEHOLDER_BYTES.findall(value)}
    if host.encode() in _ALLOWED_HOSTS and any(name.lower() == b"authorization" and _PLACEHOLDER_BYTES.search(value)
                                               for name, value in request.headers):
        raise _Refused(403, _GITHUB_AUTH, "github_authorization")
    in_body = {(kind.decode(), key.decode()) for kind, key in _PLACEHOLDER_BYTES.findall(body)}
    if in_body:
        types = [value for name, value in request.headers if name.lower() == b"content-type"]
        if len(types) != 1 or types[0].split(b";")[0].strip().lower() != b"application/json":
            raise _Refused(403, _WHERE, "placement")
    if not named | in_body:
        raise _Refused(403, _NONE, "no_item")
    return named | in_body


def _uses(run: RunItems, named: set[tuple[str, str]], host: str) -> dict[tuple[str, str], ItemUse]:
    by_key = {use.key: use for use in run.uses}
    uses = {}
    for kind, key in named:
        use = by_key.get(key if kind == "item" else f"login:{key}")
        if use is None:
            raise _Refused(403, _UNLISTED, "unlisted")
        if use.place is None:
            raise _Refused(403, _GIVEN, "given")
        if use.place != host:
            raise _Refused(403, _HOST, "host")
        uses[(kind, key)] = use
    return uses


def _values(run: RunItems, uses: dict[tuple[str, str], ItemUse]) -> dict[bytes, str]:
    """Each placeholder's value, after the fills' check for the owner passes again now."""
    values = {}
    try:
        for (kind, key), use in uses.items():
            check_use(run.owner, use)
            value = (saved_items.item_value_for_fill(use.name) if use.name is not None
                     else saved_logins.password_for_fill(use.login))
            values[b"{{corsia-%s:%s}}" % (kind.encode(), key.encode())] = value
    except (ItemsUnavailable, KeyStoreError):
        raise _Refused(403, _GONE, "unavailable") from None
    return values


def _swapped(request: h11.Request, body: bytes, values: dict[bytes, str]) -> tuple[list[tuple[bytes, bytes]], bytes]:
    """Each placeholder replaced: as the value's UTF-8 bytes in a header, JSON-escaped in the body."""
    def raw(match: re.Match) -> bytes:
        return values[match.group(0)].encode("utf-8")

    def escaped(match: re.Match) -> bytes:
        return json.dumps(values[match.group(0)])[1:-1].encode("ascii")

    headers = [(name, _PLACEHOLDER_BYTES.sub(raw, value)) for name, value in request.headers
               if name.lower() not in _DROPPED]
    headers.append((b"accept-encoding", b"identity"))
    return headers, _PLACEHOLDER_BYTES.sub(escaped, body)


def _record(run: RunItems, uses: dict[tuple[str, str], ItemUse], result: str) -> None:
    with write_transaction(None, scope="script_item_use") as conn:
        for use in uses.values():
            audit(conn, site=use.place or "", thread_id=run.owner, result=result, script_id=run.script_id,
                  **({"item": use.name} if use.name is not None else {"login_id": use.login or ""}))


async def _body(reader: asyncio.StreamReader, conn: h11.Connection, request: h11.Request) -> bytes:
    body = bytearray()
    if _has_body(request):
        async for chunk in _request_body(reader, conn):
            body.extend(chunk)
            if len(body) > REQUEST_LIMIT:
                raise _Refused(413, OVER_REQUEST, "request_over_limit")
    return bytes(body)


async def _answer(client: httpx.AsyncClient, request: h11.Request, url: str, headers, body: bytes):
    async with client.stream(request.method.decode("ascii"), url, headers=headers, content=body or None) as response:
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > REQUEST_LIMIT:
                return response.status_code, [], None
        kept = [(name, value) for name, value in response.headers.raw if name.lower() not in _DROPPED]
        return response.status_code, kept, bytes(content)


async def _respond(conn: h11.Connection, writer: asyncio.StreamWriter, status: int, headers, body: bytes) -> None:
    headers = [*headers, (b"content-length", str(len(body)).encode()), (b"connection", b"close")]
    writer.write(conn.send(h11.Response(status_code=status, headers=headers)))
    if body:
        writer.write(conn.send(h11.Data(data=body)))
    writer.write(conn.send(h11.EndOfMessage()))
    await writer.drain()


async def _send(run: RunItems, clients: dict[str, httpx.AsyncClient | None], request: h11.Request,
                reader: asyncio.StreamReader, conn: h11.Connection) -> tuple[int, list, bytes]:
    host, path = _target(request)
    body = await _body(reader, conn, request)
    uses = _uses(run, _named(request, body, host), host)
    github = host.encode() in _ALLOWED_HOSTS
    client = clients["github"] if github else clients["direct"]
    if client is None:
        raise _Refused(403, _NO_GITHUB, "no_github_login")
    values = await asyncio.to_thread(_values, run, uses)
    headers, sent = _swapped(request, body, values)
    try:
        status, kept, content = await _answer(client, request, f"{'http' if github else 'https'}://{host}{path}",
                                              headers, sent)
    except Exception as exc:  # noqa: BLE001 - the script gets fixed text; only the type is logged
        logger.warning("script item forwarder: request failed for script %s: %s", run.script_id, type(exc).__name__)
        await asyncio.to_thread(_record, run, uses, "unknown")
        return 502, [], _FAILED
    await asyncio.to_thread(_record, run, uses, "filled")
    if content is None:
        return 502, [], OVER_RESPONSE
    try:
        hidden = await asyncio.to_thread(script_blanking.saved_values)
    except script_blanking.Unchecked:
        return 502, [], UNCHECKED
    kept = [(name, script_blanking.data(value, hidden)) for name, value in kept]
    return status, kept, script_blanking.data(content, hidden)


async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, run: RunItems,
                 clients: dict[str, httpx.AsyncClient | None]) -> None:
    conn = h11.Connection(h11.SERVER)
    try:
        request = await _read_request(reader, conn)
        if request is None:
            return
        try:
            status, headers, body = await _send(run, clients, request, reader, conn)
        except _Refused as refused:
            logger.info("script item forwarder refused a request for script %s: %s", run.script_id, refused.reason)
            await _reply(conn, writer, refused.status, refused.body)
            return
        await _respond(conn, writer, status, headers, body)
    except Exception as exc:  # noqa: BLE001 - only the type is logged: a message could carry the request
        logger.warning("script item forwarder failed for script %s: %s", run.script_id, type(exc).__name__)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


@asynccontextmanager
async def item_proxy(path: Path, run: RunItems, *, github_socket: Path | None,
                     transport: httpx.AsyncBaseTransport | None = None,
                     github_transport: httpx.AsyncBaseTransport | None = None):
    """Serve this run's saved-item requests until it exits. One client for direct places and one for the GitHub
    forwarder, each bounded by the run's own default timeout, are opened with the server and closed after it."""
    active: set[asyncio.Task] = set()
    async with AsyncExitStack() as stack:
        options = {"trust_env": False, "follow_redirects": False, "timeout": DEFAULT_TIMEOUT_SECONDS}
        clients: dict[str, httpx.AsyncClient | None] = {
            "direct": await stack.enter_async_context(httpx.AsyncClient(transport=transport, **options)),
            "github": None,
        }
        if github_socket is not None:
            uds = github_transport or httpx.AsyncHTTPTransport(uds=str(github_socket))
            clients["github"] = await stack.enter_async_context(httpx.AsyncClient(transport=uds, **options))

        def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.create_task(_serve(reader, writer, run=run, clients=clients))
            active.add(task)
            task.add_done_callback(active.discard)

        server = await asyncio.start_unix_server(connected, path=str(path))
        try:
            path.chmod(0o600)
            yield
        finally:
            server.close()
            await server.wait_closed()
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            path.unlink(missing_ok=True)
