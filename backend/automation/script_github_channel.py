"""Run-scoped GitHub broker endpoint for sandboxed scripts.

A channel opens only for a script run a rule started from an event a connector
or CORSIA itself recorded whose stored facts name a repository
(``bound_repository``); the claim reads it from the stored run input, so nothing
a script sends or emits can choose it. Each request is
vetted by the broker's read policy, then limited to ``repos/<that repo>/...``,
and goes through the pollers' shared read budget: asked before, told after.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from connections.github_broker import GithubReadRefused, default_broker
from connections.github_read_request import ReadRequest, parse_gh_args
from events.connectors.github_read_budget import budget
from host_access.credential_sanitizer import sanitize_credential_text
from tools.unix_socket_policy import corsia_temp_root_for_bind

SOCKET_ENV = "CORSIA_SCRIPT_GITHUB_SOCKET"
log = logging.getLogger(__name__)
_REPOSITORY = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")
_STATUS_LINE = re.compile(r"^HTTP/\S+\s+(\d{3})")
#: Event origins whose facts CORSIA wrote; an emitter's or agent's facts are theirs.
_RECORDED_ORIGINS = frozenset({"connector", "platform"})


def bound_repository(event: Any) -> str | None:
    """The ``owner/name`` a stored event's ``repo`` fact names, or None when it names none
    or a script or agent wrote the event."""
    if not isinstance(event, Mapping) or event.get("origin") not in _RECORDED_ORIGINS:
        return None
    facts = event.get("facts")
    repo = facts.get("repo") if isinstance(facts, Mapping) else None
    match = _REPOSITORY.match(repo) if isinstance(repo, str) else None
    if match is None or any(part in (".", "..") for part in match.groups()):
        return None
    return repo


def _bound_request(args: list[str], repo: str) -> ReadRequest:
    """The vetted read, refused unless its path is inside ``repos/<repo>/``."""
    request = parse_gh_args(["api", *args])
    parts = request.path.partition("?")[0].split("/")
    if (len(parts) < 3 or parts[0].lower() != "repos"
            or f"{parts[1]}/{parts[2]}".casefold() != repo.casefold()):
        raise GithubReadRefused("repository_not_allowed", f"this run reads only repos/{repo}/...")
    return request


def _split_response(stdout: str) -> tuple[int, dict[str, str], int] | None:
    """``gh api -i`` output's status, lower-case headers and where its body starts."""
    end = stdout.find("\n")
    match = _STATUS_LINE.match(stdout[:end]) if end >= 0 else None
    if match is None:
        return None
    headers: dict[str, str] = {}
    start = end + 1
    while (end := stdout.find("\n", start)) >= 0:
        line = stdout[start:end].rstrip("\r")
        start = end + 1
        if not line:
            return int(match.group(1)), headers, start
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return None


def _json(text: str) -> Any:
    try:
        return json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        return None


def _read(request: ReadRequest, context: str) -> dict[str, Any]:
    """One budgeted broker read; the client gets the output it asked for, unchanged."""
    until = budget.blocked_until(low_priority=True)
    if until is not None:
        log.info("script github read deferred by the shared read budget %s", context)
        resumes = datetime.fromtimestamp(until, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"code": 1, "stdout": "",
                "stderr": f"GitHub reads are paused by the shared rate-limit budget until {resumes}\n"}
    # Headers are always read so that the budget learns from every response.
    result = default_broker()(replace(request, include=True).argv())
    stdout = str(result.stdout or "")
    split = _split_response(stdout)
    if split is None:
        log.warning("script github read returned no HTTP response (exit %s) %s: %s",
                    result.returncode, context, str(result.stderr or "")[:200])
    else:
        status, headers, body = split
        budget.record(status, headers, _json(stdout[body:]))
        if not request.include:
            stdout = stdout[body:]
    return {"code": result.returncode, "stdout": stdout, "stderr": str(result.stderr or "")}


def _respond(request_line: bytes, *, repo: str, context: str) -> dict[str, Any]:
    try:
        args = json.loads(request_line)
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError("expected a JSON list of string arguments")
    except (ValueError, UnicodeDecodeError) as exc:
        log.warning("script github read malformed request %s: %s", context, exc)
        return {"code": 1, "stdout": "", "stderr": f"malformed GitHub read request: {exc}\n"}
    try:
        return _read(_bound_request(args, repo), context)
    except GithubReadRefused as exc:
        log.warning("script github read refused %s: %s", context, exc)
        return {"code": 1, "stdout": "", "stderr": f"GitHub read refused: {exc}\n"}
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("script github read failed %s: %s: %s", context, type(exc).__name__, exc)
        return {"code": 1, "stdout": "", "stderr": f"GitHub read failed: {type(exc).__name__}: {exc}\n"}
    except Exception as exc:  # noqa: BLE001 - the client gets the cause; the task never dies silently
        log.exception("script github read failed unexpectedly %s", context)
        cause = sanitize_credential_text(f"{type(exc).__name__}: {exc}")[:300]
        return {"code": 1, "stdout": "", "stderr": f"GitHub read failed: {cause}\n"}


async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                 repo: str, context: str) -> None:
    try:
        try:
            line = await reader.readline()
        except ValueError as exc:  # a line past the stream limit
            log.warning("script github read malformed request %s: %s", context, exc)
            response = {"code": 1, "stdout": "", "stderr": f"malformed GitHub read request: {exc}\n"}
        else:
            log.info("script github read %s", context)
            response = await asyncio.to_thread(_respond, line, repo=repo, context=context)
        data = json.dumps(response).encode()
        writer.write(len(data).to_bytes(8, "big") + data)
        await writer.drain()
    except OSError as exc:
        log.warning("script github read could not answer %s: %s", context, exc)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


@asynccontextmanager
async def github_channel(thread_id: str, run_id: str, rule_id: str | None, *, repo: str):
    """Serve reads of ``repo`` for this run until its script exits; reachable from its thread only."""
    path = corsia_temp_root_for_bind(thread_id) / uuid.uuid4().hex[:10]
    context = f"thread={thread_id} run={run_id} rule={rule_id} repo={repo}"
    active: set[asyncio.Task] = set()

    def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(_serve(reader, writer, repo=repo, context=context))
        active.add(task)
        task.add_done_callback(active.discard)

    server = await asyncio.start_unix_server(connected, path=str(path))
    try:
        os.chmod(path, 0o600)
        yield path
    finally:
        server.close()
        await server.wait_closed()
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        path.unlink(missing_ok=True)
