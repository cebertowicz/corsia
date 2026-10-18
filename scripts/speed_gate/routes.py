"""Enumerate every GET route of the application and fill its parameters from fixture ids.

A route that cannot be called is returned with the reason; the harness lists
it in the report, it is never skipped silently.
"""

from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .sqlite_pin import sqlite3

_PATH_PARAM = re.compile(r"\{(\w+)(?::\w+)?\}")
#: Routes that are not a database read: framework docs, the SPA catch-all, a live stream.
NOT_A_READ = {
    "/openapi.json": "framework schema document, no application read",
    "/docs": "framework docs page, no application read",
    "/docs/oauth2-redirect": "framework docs page, no application read",
    "/redoc": "framework docs page, no application read",
    "/{path:path}": "static single-page-app catch-all, no database read",
}


@dataclass
class Target:
    template: str
    url: str | None
    reason: str | None = None  # why it cannot be called


def walk(routes, prefix: str = ""):
    """Yield ``(full path template, route)`` through nested included routers."""
    for route in routes:
        context = getattr(route, "include_context", None)
        if context is not None:
            yield from walk(context.included_router.routes, prefix + context.prefix)
        elif getattr(route, "methods", None):
            yield prefix + route.path, route


def get_routes(app) -> list[tuple[str, Any]]:
    found = [(path, route) for path, route in walk(app.routes) if "GET" in route.methods]
    return sorted(found, key=lambda item: item[0])


def fixture_ids(db_path: str) -> dict[str, str]:
    """Identifiers read from the fixture database for path and query parameters."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    def first(sql: str) -> str | None:
        try:
            row = db.execute(sql).fetchone()
        except sqlite3.Error:  # a tree whose schema lacks the table has no such id
            return None
        return None if row is None else str(row[0])

    ids = {
        # Generic thread reads use an ordinary thread. The Focus first-page
        # variant below targets a persistent agent whose newest turns include
        # production-shaped seed/tool/assistant rows.
        "thread_id": "thread-00018", "agent_thread_id": "thread-00000",
        "parent_thread_id": "thread-00000", "project_id": "project-00",
        "node_id": "node-project-00-0001", "user_id": "steve", "metric": "messages",
        "artifact_id": first("SELECT artifact_id FROM artifact_registrations ORDER BY rowid LIMIT 1")
        or first("SELECT id FROM artifact_identities ORDER BY rowid LIMIT 1"),
        "model_id": first("SELECT id FROM models ORDER BY id LIMIT 1"),
        "server_id": first("SELECT id FROM network_servers ORDER BY id LIMIT 1"),
        "account_id": first("SELECT id FROM vendor_accounts ORDER BY id LIMIT 1"),
        "lease_id": first("SELECT id FROM host_access_leases ORDER BY id LIMIT 1"),
        "message_id": first("SELECT id FROM messages WHERE thread_id='thread-00000' ORDER BY seq LIMIT 1"),
        "name": first("SELECT provider FROM models ORDER BY id LIMIT 1") or "fixture-name",
        "vendor": "codex", "provider": "claude-code", "tool_call_id": "fixture-tool-call",
        "file_id": "fixture-file", "filename": "fixture.txt", "job_id": "fixture-job",
    }
    db.close()
    return {key: value for key, value in ids.items() if value is not None}


#: Query parameters the fixture fills; the route's own defaults cover the rest.
QUERY_FILL = {"project": "all", "agent": "none", "timezone": "UTC", "scope": "workspace",
              "path": "README.md", "status": "active", "range": "12w"}


#: Optional parameters every real client sets: an unpaged read of one thread is not a journey.
CLIENT_PAGING = {"limit": "50"}


def query_params(route) -> list[tuple[str, bool]]:
    """``(wire name, required)`` of every query parameter the route declares."""
    dependant = getattr(route, "dependant", None)
    found = []
    for param in getattr(dependant, "query_params", []):
        wire = getattr(param.field_info, "alias", None) or param.name
        found.append((wire, param.field_info.is_required()))
    return found


def runtime_query_values() -> dict[str, str]:
    """Client query values that only exist after the target app has loaded."""
    from infra import config

    values = {"path": str(config.WORKSPACE / "README.md")}
    if importlib.util.find_spec("api.measurement_registry") is None:
        return values
    from api.measurement_registry import REGISTRY
    values["ids"] = ",".join(sorted(name for name, item in REGISTRY.items() if item.available))
    return values


def build_target(template: str, route, ids: dict[str, str], extra: dict[str, str],
                 token: str) -> Target:
    if template in NOT_A_READ:
        return Target(template, None, reason=NOT_A_READ[template])
    values: dict[str, str] = {}
    for name in _PATH_PARAM.findall(template):
        if name not in ids:
            return Target(template, None, reason=f"no fixture value for path parameter '{name}'")
        values[name] = quote(ids[name], safe="")
    url = _PATH_PARAM.sub(lambda m: values[m.group(1)], template)
    query = []
    for name, required in query_params(route):
        if name == "token":
            query.append(f"token={quote(token, safe='')}")  # some routes read only the query token
            continue
        value = extra.get(name) or (QUERY_FILL.get(name) if required else None) or CLIENT_PAGING.get(name)
        value = value or (ids.get(name) if required else None)
        if value is None and required:
            return Target(template, None, reason=f"no fixture value for required query parameter '{name}'")
        if value is not None:
            query.append(f"{name}={quote(value, safe=',')}")
    return Target(template, url + ("?" + "&".join(query) if query else ""))


#: Reads whose client query shape changes the response substantially. The default
#: page is the ordinary route target above; Focus gets its own identity because
#: the same path has different server-side projection and payload size.
CLIENT_QUERY_VARIANTS = {
    "/api/threads/{thread_id}/messages": (("?limit=50&view=focus", "view=focus"),),
}


def build_targets(template: str, route, ids: dict[str, str], extra: dict[str, str],
                  token: str) -> list[Target]:
    """Return the generic client read plus explicitly distinct query variants."""
    primary = build_target(template, route, ids, extra, token)
    if primary.url is None:
        return [primary]
    variants = [primary]
    for suffix, extra_query in CLIENT_QUERY_VARIANTS.get(template, ()):
        variant_ids = {**ids, "thread_id": ids.get("agent_thread_id", ids["thread_id"])}
        variant = build_target(template, route, variant_ids, extra, token)
        assert variant.url is not None  # the primary and variant share the same route contract
        separator = "&" if "?" in variant.url else "?"
        variants.append(Target(template + suffix, variant.url + separator + extra_query))
    return variants
