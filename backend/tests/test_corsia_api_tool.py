"""Conductor API authentication, permanent route boundaries and bounded output."""
from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

import asyncio
import json

import pytest
from fastapi import FastAPI

from infra.conductor import set_scope_coordinator
from infra.database import get_db
from infra.internal_principal import current_internal_user, internal_request
from tests.registry_helpers import make_session
from tools.conductor_api_policy import off_limits, registered_routes, route_class
from tools.corsia_api import ApiCallError, CorsiaApiTool, _scope, call_application


def _actors():
    db = get_db()
    try:
        db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('other','Other','t','t')")
        db.commit()
        conductor = make_session(db, prefix="conductor")
        ordinary = make_session(db, prefix="ordinary")
        project = make_session(db, prefix="project", project_id="other")
        assert set_corsia_agent(conductor, db=db)
        assert set_project_conductor("other", project, db=db)
        return conductor, ordinary, project
    finally:
        db.close()


@pytest.mark.asyncio
async def test_real_handlers_without_a_credential(isolated_db):
    conductor, ordinary, project = _actors()
    tool = CorsiaApiTool()
    # Legacy authenticate(token) and dependency-authenticated handlers both work.
    for path in ("/api/threads", "/api/agents"):
        result = json.loads(await tool.execute("GET", path, _thread_id=conductor))
        assert result["status"] == 200, result
    ok = json.loads(await tool.execute("GET", "/api/agents", _thread_id=project))
    assert ok["status"] == 200, ok
    for actor in (ordinary, "missing"):
        denied = await tool.execute("GET", "/api/agents", _thread_id=actor)
        assert "Missing conductor capability" in denied
    assert current_internal_user() is None
    from infra.auth import authenticate

    assert authenticate("internal-request") is None


BLOCKLIST_ROWS = {
    "B1": 1, "B2": 1, "B3": 1, "B4": 3, "B5": 2, "B6": 1, "B8": 4,
    "B9": 1, "B10": 1, "B11": 10, "B12": 1,
}


def test_blocklist_rows_are_pinned():
    """Changing the person-only list is deliberate: update this pin with it."""
    from collections import Counter
    from tools.conductor_api_policy import OFF_LIMITS_ROWS

    from tools.conductor_api_policy import BODY_FIELD_ROWS

    assert dict(Counter(row for row, *_ in OFF_LIMITS_ROWS)) == BLOCKLIST_ROWS
    assert all(reason for *_, reason in OFF_LIMITS_ROWS)
    assert [(row, field) for row, _, _, field, _ in BODY_FIELD_ROWS] == [("B13", "api_base")]


def _concrete(path):
    return __import__("re").sub(r"\{[^}]+\}", "test", path)


@pytest.mark.asyncio
async def test_registered_routes_are_open_unless_blocklisted(isolated_db):
    from main import app

    conductor, _, _ = _actors()
    tool = CorsiaApiTool()
    denied_count = 0
    for route in registered_routes(app):
        path = getattr(route, "path", "")
        if not path.startswith("/api"):
            continue
        if route_class(route) != "off-limits":
            continue
        denied_count += 1
        for method in route.methods:
            if not off_limits(path, method):
                continue
            result = await tool.execute(method, _concrete(path), _thread_id=conductor)
            assert result.startswith("Error:"), (method, path, result)
            assert "off-limits" in result, (method, path, result)
    assert denied_count >= 20
    unknown = FastAPI()
    unknown.get("/api/future")(lambda: {})
    assert route_class(unknown.routes[-1]) == "open"


@pytest.mark.asyncio
async def test_previously_refused_settings_read_succeeds(isolated_db):
    # first-run.md tells the conductor to read the reviewer status here; it was off-limits.
    # (`/api/system/settings`, named in the brief, is not a registered route: it 404s.)
    conductor, _, _ = _actors()
    result = json.loads(await CorsiaApiTool().execute("GET", "/api/settings", _thread_id=conductor))
    assert result["status"] == 200, result
    # safe_response drops every host-access key, so the reviewer status is not visible here.
    assert "host_access" not in json.dumps(result)


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/auth/new"), ("GET", "/api/users/new"), ("GET", "/api/sessions/new"),
    ("GET", "/api/tokens/new"),
    ("POST", "/api/host-access/leases/new"),
    ("GET", "/api/credentials/new"), ("GET", "/api/secrets/new"),
    ("PUT", "/api/keys/web-search/value"), ("POST", "/api/keys/x/clear"),
    ("POST", "/api/keys/x/remove"),
    ("POST", "/api/plans/decisions/d/answer"), ("POST", "/api/comments/c/decision"),
    ("PUT", "/api/conductor"),
    ("DELETE", "/api/settings/providers/openai"), ("DELETE", "/api/settings/network-servers/s"),
    ("POST", "/api/vendor-accounts/a/signin"), ("POST", "/api/vendor-accounts/a/signout"),
    ("DELETE", "/api/vendor-accounts/a/signin"), ("DELETE", "/api/vendor-accounts/a"),
    ("POST", "/api/cc/_tool-bridge/new"), ("POST", "/api/corsia_api"), ("POST", "/api/tools/x"),
    ("POST", "/api/webhooks/github"),
    ("POST", "/api/plans/p/mutations"), ("DELETE", "/api/threads/t"),
    ("POST", "/api/artifacts/a/comments/root/viewed"),
    ("DELETE", "/api/security/alert"),
    ("PUT", "/api/corsia-agent"),
])
def test_each_blocklist_family_is_refused(method, path):
    assert off_limits(path, method), (method, path)


def test_every_blocklist_row_has_a_refused_representative():
    import re
    from tools.conductor_api_policy import OFF_LIMITS_ROWS

    samples = {
        "B1": ("POST", "/api/auth/x"), "B2": ("POST", "/api/host-access/x"),
        "B3": ("GET", "/api/secrets/x"),
    }
    hit = set()
    for method, path in [s for s in samples.values()] + [
        ("PUT", "/api/keys/k/value"), ("DELETE", "/api/settings/providers/p"),
        ("DELETE", "/api/settings/network-servers/s"), ("POST", "/api/plans/decisions/d/answer"),
        ("POST", "/api/comments/c/decision"), ("PUT", "/api/conductor"),
        ("POST", "/api/vendor-accounts/a/signin"), ("POST", "/api/vendor-accounts/a/signout"),
        ("DELETE", "/api/vendor-accounts/a/signin"), ("DELETE", "/api/vendor-accounts/a"),
        ("POST", "/api/tools/x"), ("POST", "/api/webhooks/x"),
        ("POST", "/api/plans/p/mutations"), ("PATCH", "/api/threads/t"),
        ("POST", "/api/artifacts/a/comments"), ("POST", "/api/doc-feedback"),
        ("PATCH", "/api/artifacts/a/display"), ("POST", "/api/attention/a/clear"),
        ("POST", "/api/threads/t/seen"), ("POST", "/api/plans/operator/threads/t/seen"),
        ("POST", "/api/artifacts/a/comments/c/viewed"), ("POST", "/api/doc-reviews"),
        ("DELETE", "/api/security/alert"),
    ]:
        for index, (_, method_pattern, path_pattern, _) in enumerate(OFF_LIMITS_ROWS):
            if re.fullmatch(method_pattern, method) and re.search(path_pattern, path):
                hit.add(index)
    assert hit == set(range(len(OFF_LIMITS_ROWS)))


def test_provider_endpoint_and_repo_link_are_refused_by_body(isolated_db, tmp_path):
    from infra.config import WORKSPACE

    with pytest.raises(ApiCallError, match="does not move a provider endpoint"):
        _scope("PUT", "/api/settings/providers/openai", {}, {"api_base": "https://example.test/v1"})
    _scope("PUT", "/api/settings/providers/openai", {}, {"enabled": True})
    with pytest.raises(ApiCallError, match="inside the workspace"):
        _scope("POST", "/api/projects/p/repos", {}, {"mode": "link", "path": str(tmp_path / "outside")})
    inside = WORKSPACE / "projects" / "inside"
    _scope("POST", "/api/projects/p/repos", {}, {"mode": "link", "path": str(inside)})
    _scope("POST", "/api/projects/p/repos", {}, {"mode": "create", "name": "new"})


def _person_only_routes():
    """Registered routes whose dependencies include a person-session guard."""
    from api.keys import _human_session
    from infra.auth import require_human_user
    from main import app

    guards = {require_human_user, _human_session}

    def uses_guard(dependant):
        return dependant.call in guards or any(uses_guard(sub) for sub in dependant.dependencies)

    return [route for route in registered_routes(app)
            if hasattr(route, "dependant") and getattr(route, "path", "").startswith("/api")
            and uses_guard(route.dependant)]


@pytest.mark.asyncio
async def test_person_session_routes_refuse_the_conductor(isolated_db):
    conductor, _, _ = _actors()
    tool = CorsiaApiTool()
    routes = _person_only_routes()
    assert routes
    for route in routes:
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            result = await tool.execute(method, _concrete(route.path), body={} if method != "GET" else None,
                                        _thread_id=conductor)
            if result.startswith("Error:"):
                assert "off-limits" in result, (method, route.path, result)
            else:
                assert json.loads(result)["status"] in {401, 403}, (method, route.path, result)
    # Handlers that call require_human_user in their body, behind an open path.
    for method, path, body in (("PATCH", "/api/settings", {"live_bulletins": {"enabled": True}}),
                               ("POST", "/api/system/restart", None)):
        result = json.loads(await tool.execute(method, path, body=body, _thread_id=conductor))
        assert result["status"] == 403, (method, path, result)


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/keys/{name}/check"), ("POST", "/api/vendor-accounts/{account_id}/verify"),
    ("POST", "/api/vendor-accounts/{account_id}/signin/check"),
    ("POST", "/api/threads/{thread_id}/corsia-agent-account"),
    ("PUT", "/api/threads/{thread_id}/vendor-account"), ("PUT", "/api/settings/providers/{name}"),
    ("GET", "/api/security/alert"), ("GET", "/api/settings"), ("PATCH", "/api/settings"),
    ("GET", "/api/settings/providers"), ("GET", "/api/settings/network-servers/{server_id}/settings"),
    ("POST", "/api/eval/turn"), ("POST", "/api/mcp/import"),
    ("POST", "/api/system/drain"), ("POST", "/api/system/drain/hard-cap"), ("DELETE", "/api/system/drain"),
    ("PUT", "/api/vendor-accounts/{account_id}/billing"), ("POST", "/api/vendor-accounts/{account_id}/overage"),
    ("PUT", "/api/settings/network-servers/{server_id}/billing"),
])
def test_opened_routes_are_registered_and_open(method, path):
    """Governance-reviewed openings: each is a live route and nothing on the blocklist covers it."""
    from main import app

    matches = [route for route in registered_routes(app)
               if getattr(route, "path", "") == path and method in (getattr(route, "methods", None) or ())]
    assert matches, (method, path)
    assert not off_limits(path, method), (method, path)


@pytest.mark.asyncio
async def test_billing_routes_reach_their_handlers_for_a_conductor(isolated_db):
    """Price, overage and endpoint-billing writes pass the blocklist and body filter and run the handler."""
    conductor, _, _ = _actors()
    tool = CorsiaApiTool()
    made = json.loads(await tool.execute("POST", "/api/vendor-accounts",
                                         body={"provider": "codex", "name": "Billed"}, _thread_id=conductor))
    account_id = made["data"]["id"]
    priced = json.loads(await tool.execute(
        "PUT", f"/api/vendor-accounts/{account_id}/billing", _thread_id=conductor,
        body={"monthly_price_cents": 2000, "charge_day": 17, "expected_billing_revision": 0}))
    assert priced["status"] == 200, priced
    overage = json.loads(await tool.execute(
        "POST", f"/api/vendor-accounts/{account_id}/overage", _thread_id=conductor,
        body={"amount_cents": 500, "month": "2026-09", "expected_billing_revision": 1}))
    assert overage["status"] == 200, overage
    # An unknown endpoint reaches the handler's own 404, not an off-limits refusal.
    endpoint = await tool.execute(
        "PUT", "/api/settings/network-servers/missing/billing", _thread_id=conductor,
        body={"mode": None, "expected_billing_revision": 0})
    assert "off-limits" not in endpoint, endpoint
    assert json.loads(endpoint)["status"] == 404, endpoint


@pytest.mark.asyncio
async def test_key_check_and_value_vendor_verify_and_signin(isolated_db):
    conductor, _, _ = _actors()
    tool = CorsiaApiTool()
    # A conductor checks the saved key; with none saved the check says so.
    checked = json.loads(await tool.execute("POST", "/api/keys/web-search/check", _thread_id=conductor))
    assert checked["status"] == 200, checked
    assert checked["data"] == {"outcome": "not_set", "reason": None}, checked
    value = await tool.execute("PUT", "/api/keys/web-search/value", body={"value": "x"}, _thread_id=conductor)
    assert "off-limits" in value
    made = json.loads(await tool.execute("POST", "/api/vendor-accounts",
                                         body={"provider": "codex", "name": "A"}, _thread_id=conductor))
    account_id = made["data"]["id"]
    verified = json.loads(await tool.execute(
        "POST", f"/api/vendor-accounts/{account_id}/verify", _thread_id=conductor))
    assert "off-limits" not in json.dumps(verified)
    signin = await tool.execute("POST", f"/api/vendor-accounts/{account_id}/signin", _thread_id=conductor)
    assert "off-limits" in signin


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,query,body", [
    ("POST", "/api/threads/{target}/seen", {}, {"unread": False}),
    ("POST", "/api/plans/operator/threads/{target}/seen", {}, {}),
    ("PUT", "/api/conductor", {}, {"thread_id": None}),
    ("PUT", "/api/conductor", {"scope": "workspace"}, {"thread_id": "{target}"}),
    ("PUT", "/api/conductor", {"scope": "other"}, {"thread_id": "{target}"}),
    ("PUT", "/api/corsia-agent", {}, {"thread_id": "{target}"}),
])
async def test_human_seen_state_and_conductor_designation_denied(
    isolated_db, monkeypatch, method, path, query, body,
):
    from infra.conductor import get_scope_coordinator
    import api.conductor as conductor_routes
    import api.plans as plan_routes
    import api.thread_seen as seen_routes

    conductor, ordinary, _ = _actors()
    reached = []

    def mutation(*args, **kwargs):
        reached.append(True)
        raise AssertionError("Human state or authority mutation must not be reached")

    monkeypatch.setattr(seen_routes, "_mark_reported", mutation)
    monkeypatch.setattr(plan_routes, "_thread_view_operation", mutation)
    monkeypatch.setattr(conductor_routes, "set_scope_coordinator", mutation)
    body = {key: ordinary if value == "{target}" else value for key, value in body.items()}
    result = await CorsiaApiTool().execute(
        method, path.format(target=ordinary), query=query, body=body, _thread_id=conductor,
    )
    assert "off-limits" in result, result
    assert not reached
    assert get_corsia_agent() == conductor


@pytest.mark.asyncio
async def test_real_write_uses_existing_handler_and_readback(isolated_db):
    conductor, _, _ = _actors()
    tool = CorsiaApiTool()
    made = json.loads(await tool.execute("POST", "/api/vendor-accounts",
                                         body={"provider": "codex", "name": "Test account"},
                                         _thread_id=conductor))
    assert made["status"] == 200, made
    read = json.loads(await tool.execute("GET", "/api/vendor-accounts", _thread_id=conductor))
    assert "Test account" in json.dumps(read)


@pytest.mark.parametrize("path", ["https://host/api/agents", "/api/../auth/login",
                                    "/api/%61uth/login", "/api//auth/login", "/api/agents?token=x"])
def test_path_cannot_escape_local_route_policy(path):
    with pytest.raises(ApiCallError):
        _scope("GET", path, {}, None)


@pytest.mark.parametrize("body", [{"host_access_auto_approve": True}, {"host_access_auto_approve": False},
                                  {"host_access_reviewer_fallback_model": "m"},
                                  {"host_access_reviewer_model": "m", "host_access_auto_approve": True},
                                  {"nested": {"host_access_lease_ttl": 1}}, {"nested": {"api_key": "sentinel"}},
                                  {"authorization": "Bearer sentinel"},
                                  {"base_url": "https://user:sentinel@example.test/v1"}])
def test_authority_and_credential_fields_denied(body):
    with pytest.raises(ApiCallError, match="off-limits"):
        _scope("PATCH", "/api/settings", {}, body)


def test_the_reviewer_model_is_the_one_host_access_key_a_conductor_may_send():
    _scope("PATCH", "/api/settings", {}, {"host_access_reviewer_model": "claude-code/opus"})


@pytest.mark.asyncio
async def test_a_conductor_sets_the_reviewer_model_but_never_auto_approve(isolated_db):
    from infra.settings import get_agent_defaults, get_host_access_auto_approve

    conductor, ordinary, _ = _actors()
    tool = CorsiaApiTool()

    set_model = json.loads(await tool.execute(
        "PATCH", "/api/settings", body={"host_access_reviewer_model": "claude-code/opus"}, _thread_id=conductor))
    assert set_model["status"] == 200, set_model
    assert get_agent_defaults()["host_access_reviewer_model"] == "claude-code/opus"

    for body in ({"host_access_auto_approve": True},
                 {"host_access_reviewer_model": "claude-code/opus", "host_access_auto_approve": True}):
        refused = await tool.execute("PATCH", "/api/settings", body=body, _thread_id=conductor)
        assert "off-limits" in refused
    assert get_host_access_auto_approve() is False

    cleared = json.loads(await tool.execute(
        "PATCH", "/api/settings", body={"host_access_reviewer_model": None}, _thread_id=conductor))
    assert cleared["status"] == 403, cleared
    assert get_agent_defaults()["host_access_reviewer_model"] == "claude-code/opus"

    not_a_conductor = await tool.execute(
        "PATCH", "/api/settings", body={"host_access_reviewer_model": "claude-code/sonnet"}, _thread_id=ordinary)
    assert "conductor capability" in not_a_conductor
    assert get_agent_defaults()["host_access_reviewer_model"] == "claude-code/opus"


@pytest.mark.asyncio
async def test_bounded_output_secret_redaction_and_revocation():
    from infra.auth import require_user
    from fastapi import Depends

    app = FastAPI()
    captured = []
    gate = asyncio.Event()

    async def later():
        await gate.wait()
        captured.append(current_internal_user())

    @app.get("/api/test", dependencies=[Depends(require_user)])
    async def result():
        asyncio.create_task(later())
        return {"token": "never-return", "nested": {"password": "never-return"}, "text": "api_key=never-return"}

    @app.get("/api/large")
    async def large():
        return {"text": "x" * 40_000}

    scope, payload = _scope("GET", "/api/test", {}, None)
    result = await call_application(app, scope, payload, "user", "actor")
    assert "never-return" not in json.dumps(result)
    gate.set()
    await asyncio.sleep(0)
    assert captured == [None]
    scope, payload = _scope("GET", "/api/large", {}, None)
    with pytest.raises(ApiCallError, match="size limit"):
        await call_application(app, scope, payload, "user", "actor")


@pytest.mark.asyncio
async def test_handler_failure_logs_method_and_path_without_body(isolated_db, monkeypatch, caplog):
    import main
    import tools.corsia_api as api_tool
    from starlette.requests import Request

    recorded = []
    monkeypatch.setattr(main, "emit_product_event", lambda *args, **kwargs: recorded.append(kwargs))
    scope, _ = _scope("GET", "/api/agents", {}, None)
    with internal_request("user", "actor"):
        response = await main._emit_error_api(Request(scope), RuntimeError("unlabelled-secret-sentinel"))
    assert response.status_code == 500
    assert "unlabelled-secret-sentinel" not in json.dumps(recorded)

    async def failed(*args):
        raise RuntimeError("unlabelled-secret-sentinel")

    monkeypatch.setattr(api_tool, "call_application", failed)
    conductor, _, _ = _actors()
    result = await CorsiaApiTool().execute(
        "POST", "/api/vendor-accounts", body={"name": "request-body-sentinel"}, _thread_id=conductor,
    )
    assert result.startswith("Error:") and "unlabelled-secret-sentinel" not in result
    messages = [record.getMessage() for record in caplog.records]
    assert "In-process API call failed: method=POST path=/api/vendor-accounts" in messages
    assert all("request-body-sentinel" not in message for message in messages)


def test_human_authority_is_not_inherited():
    from fastapi import HTTPException
    from infra.auth import require_human_user

    with internal_request("user", "actor"):
        with pytest.raises(HTTPException) as error:
            require_human_user(current_internal_user())
        assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_conductor_changes_a_feature_through_the_one_settings_patch(isolated_db, monkeypatch):
    import api.system as system

    monkeypatch.setattr(system, "apply_cpu_profiler", lambda enabled: enabled)
    conductor, _, _ = _actors()
    tool = CorsiaApiTool()
    changed = json.loads(await tool.execute(
        "PATCH", "/api/settings", body={"features": {"focus_mode": False}}, _thread_id=conductor))
    assert changed["status"] == 200, changed
    assert changed["data"]["features"]["focus_mode"] is False, changed
    # The settings PATCH is the one features route; the old conductor subset route is gone.
    gone = await tool.execute(
        "PATCH", "/api/system/features", body={"features": {"cpu_profiler": False}}, _thread_id=conductor)
    assert gone == "Error: No CORSIA API route matches PATCH /api/system/features.", gone
