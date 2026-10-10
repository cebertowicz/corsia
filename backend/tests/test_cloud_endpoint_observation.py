"""A cloud External endpoint is observed by discovery and real dispatches, never swept.

The sweep's transition code stays the only writer of server status, the
observed model set and (through discovery) each row's availability; these tests
feed it from each source and read the result where the user sees it.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import openai
import pytest

_CLOUD_HOST = "ws-example.us-east-1.maas.aliyuncs.com"
_CLOUD_BASE = f"https://{_CLOUD_HOST}/compatible-mode/v1"
_LAN_HOST = "192.168.1.20"


def _register(host: str, base: str, *, status: str = "online") -> str:
    from llm import network_discovery as nd

    sid = nd.upsert_server(
        provider="external", instance_id=f"{host}:443", instance_name=host,
        host=host, port=443, kind="external", dialect="openai", status=status,
    )
    nd.set_server_metadata(sid, base_url=base, context_window=65_536)
    return sid


@pytest.fixture
def listing(monkeypatch):
    """What the endpoint's ``/models`` lists; no request leaves the test."""
    from llm import external_image_input
    from llm import external_provider as ep

    listed: dict = {"ids": ["qwen-flash", "glm-5.1"], "error": None}

    async def _fetch(api_base, dialect, api_key):
        assert api_base == _CLOUD_BASE
        if listed["error"] is not None:
            raise listed["error"]
        return list(listed["ids"])

    async def _no_image_probe(server_id):
        return False

    monkeypatch.setattr(ep, "fetch_external_models", _fetch)
    monkeypatch.setattr(external_image_input, "detect_image_input", _no_image_probe)
    return listed


async def _drain() -> None:
    from llm import server_observation

    while server_observation._PENDING:
        await asyncio.gather(*list(server_observation._PENDING))


def _statuses(sid: str) -> dict[str, str]:
    from llm.external_provider import served_model_id
    from llm.model_status import get_model_status_rows

    return {
        served_model_id(row["litellm_model"]): row["status"]
        for row in get_model_status_rows()
        if row["litellm_model"].startswith(f"external@{sid}/")
    }


def _available(sid: str) -> dict[str, int]:
    from infra.database import get_db
    from llm.external_provider import served_model_id

    rows = get_db().execute(
        "SELECT litellm_model, available FROM models WHERE litellm_model LIKE ?",
        (f"external@{sid}/%",),
    ).fetchall()
    return {served_model_id(r["litellm_model"]): r["available"] for r in rows}


async def _discovered_cloud(listing) -> str:
    from llm import network_discovery as nd

    sid = _register(_CLOUD_HOST, _CLOUD_BASE)
    await nd._discover_and_notify(sid)
    assert _statuses(sid) == {"qwen-flash": "ready", "glm-5.1": "ready"}
    return sid


def _request(status: int) -> Exception:
    request = httpx.Request("POST", f"{_CLOUD_BASE}/chat/completions")
    response = httpx.Response(status, request=request)
    return {
        401: openai.AuthenticationError, 403: openai.PermissionDeniedError,
        404: openai.NotFoundError, 429: openai.RateLimitError,
    }[status]("authentication failed for this key", response=response, body=None)


def _wrapped_connection_error() -> Exception:
    import litellm

    try:
        raise httpx.ConnectError("name resolution failed")
    except httpx.ConnectError:
        try:
            raise litellm.APIConnectionError(
                message="request failed", llm_provider="openai", model="qwen-flash",
            )
        except litellm.APIConnectionError as wrapped:
            return wrapped


def _stalled() -> Exception:
    """A stall exactly as ``stream_idle`` raises it: ``from None`` inside a timeout."""
    from llm.stream_idle import ProviderStreamStalled

    try:
        try:
            raise TimeoutError
        except TimeoutError:
            raise ProviderStreamStalled(120, "streaming the response") from None
    except ProviderStreamStalled as stalled:
        return stalled


async def _dispatch_failing_with(monkeypatch, error: Exception):
    """One real ``chat_completion`` to the cloud base whose provider call raises ``error``."""
    from llm import base
    from llm.router import chat_completion

    async def _acompletion(**_kwargs):
        raise error

    monkeypatch.setattr(base, "acompletion", _acompletion)
    response = await chat_completion(
        [{"role": "user", "content": "hi"}], model="openai/qwen-flash",
        api_key="k", api_base=_CLOUD_BASE, retry_transient_errors=False,
    )
    await _drain()
    return response


@pytest.mark.asyncio
async def test_sweep_never_probes_a_cloud_endpoint_and_still_sweeps_lan(
    isolated_db, monkeypatch,
) -> None:
    from llm import external_sweep
    from llm import network_discovery as nd

    cloud = _register(_CLOUD_HOST, _CLOUD_BASE)
    lan = _register(_LAN_HOST, f"http://{_LAN_HOST}:443/v1")
    swept: list[str] = []

    async def _sweep(_client, server) -> None:
        swept.append(server["id"])

    monkeypatch.setattr(external_sweep, "sweep_external", _sweep)
    await nd._sweep_all_servers()

    assert swept == [lan]
    assert cloud not in swept


@pytest.mark.asyncio
async def test_after_restart_a_discovered_cloud_row_is_ready(
    isolated_db, listing,
) -> None:
    import app_lifecycle
    from infra.database import get_db
    from llm import network_discovery as nd

    sid = _register(_CLOUD_HOST, _CLOUD_BASE)
    # A restart: the observed set is gone and the last probe is long past the
    # sweep's freshness window.
    nd._LOADED_BY_SERVER.clear()
    db = get_db()
    db.execute("UPDATE network_server_endpoints SET last_ok_at = '2020-01-01T00:00:00+00:00'")
    db.commit()

    assert await app_lifecycle._discover_network_servers(logging.getLogger("t")) == 1

    assert _statuses(sid) == {"qwen-flash": "ready", "glm-5.1": "ready"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_an_auth_failure_marks_the_endpoint_broken(
    isolated_db, listing, monkeypatch, status,
) -> None:
    from llm import network_discovery as nd

    sid = await _discovered_cloud(listing)

    response = await _dispatch_failing_with(monkeypatch, _request(status))

    assert response.finish_reason == "error"
    assert nd.get_server(sid)["status"] == "offline"
    # Withdrawn: out of the picker, not merely not ready.
    assert _statuses(sid) == {}
    assert set(_available(sid).values()) == {0}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 429])
async def test_other_provider_failures_leave_the_endpoint_alone(
    isolated_db, listing, monkeypatch, status,
) -> None:
    """The message says "authentication failed"; only the status is read."""
    from llm import network_discovery as nd

    sid = await _discovered_cloud(listing)

    await _dispatch_failing_with(monkeypatch, _request(status))

    assert nd.get_server(sid)["status"] == "online"
    assert _statuses(sid) == {"qwen-flash": "ready", "glm-5.1": "ready"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connection", "stall"])
async def test_a_connection_loss_or_stall_leaves_the_endpoint_alone(
    isolated_db, listing, monkeypatch, failure,
) -> None:
    """Nothing re-probes a cloud server, so a local network blip must not strand it."""
    from llm import network_discovery as nd

    sid = await _discovered_cloud(listing)
    error = _wrapped_connection_error() if failure == "connection" else _stalled()

    response = await _dispatch_failing_with(monkeypatch, error)

    assert response.finish_reason == "error"
    assert nd.get_server(sid)["status"] == "online"
    assert _statuses(sid) == {"qwen-flash": "ready", "glm-5.1": "ready"}


@pytest.mark.asyncio
async def test_a_startup_discovery_connection_error_leaves_the_endpoint_online(
    isolated_db, listing,
) -> None:
    import app_lifecycle
    from llm import network_discovery as nd

    sid = await _discovered_cloud(listing)
    listing["error"] = _wrapped_connection_error()

    assert await app_lifecycle._discover_network_servers(logging.getLogger("t")) == 1

    assert nd.get_server(sid)["status"] == "online"
    assert _statuses(sid) == {"qwen-flash": "ready", "glm-5.1": "ready"}


def test_only_a_401_or_403_in_the_shown_chain_is_an_endpoint_failure() -> None:
    from llm.server_observation import endpoint_failure_of

    def _raised(outer: Exception, inner: Exception, *, suppress: bool) -> Exception:
        try:
            try:
                raise inner
            except Exception:
                if suppress:
                    raise outer from None
                raise outer
        except Exception as caught:
            return caught

    assert endpoint_failure_of(_stalled()) is None
    assert endpoint_failure_of(_wrapped_connection_error()) is None
    assert endpoint_failure_of(_raised(RuntimeError(), _request(401), suppress=False)) == "auth"
    assert endpoint_failure_of(_raised(RuntimeError(), _request(401), suppress=True)) is None


@pytest.mark.asyncio
async def test_an_auth_offline_endpoint_recovers_by_rediscovery_restoring_only_listed_rows(
    isolated_db, listing, monkeypatch,
) -> None:
    from llm import network_discovery as nd
    from llm.network_endpoints import build_api_base

    sid = await _discovered_cloud(listing)
    await _dispatch_failing_with(monkeypatch, _request(401))
    assert nd.get_server(sid)["status"] == "offline"
    # Not dispatchable while offline, so only discovery can bring it back.
    assert build_api_base(sid) is None

    listing["ids"] = ["qwen-flash"]
    await nd._discover_and_notify(sid)  # what add, edit, Test and startup run

    assert nd.get_server(sid)["status"] == "online"
    assert _available(sid).get("glm-5.1", 0) == 0
    assert _statuses(sid) == {"qwen-flash": "ready"}


@pytest.mark.asyncio
@pytest.mark.parametrize("retire", ["disabled", "removed"])
async def test_a_disabled_or_removed_endpoint_is_never_dispatched(
    isolated_db, listing, retire,
) -> None:
    from infra.database import get_db
    from llm import network_discovery as nd
    from llm.resolution import (
        ModelUnavailableError, resolve_execution_target, validate_native_target,
    )
    from llm.router import LLMResponse, _observed

    sid = await _discovered_cloud(listing)
    row_id = get_db().execute(
        "SELECT id FROM models WHERE litellm_model = ?", (f"external@{sid}/qwen-flash",),
    ).fetchone()["id"]
    if retire == "disabled":
        assert nd.set_server_enabled(sid, False) is True
    else:
        assert nd.remove_server(sid) is True

    # Neither a success observed from a request already in flight nor a
    # rediscovery revives it.
    async def _answered():
        return LLMResponse(content="ok")

    await _observed(_CLOUD_BASE, _answered())
    await _drain()
    await nd._discover_and_notify(sid)

    with pytest.raises(ModelUnavailableError):
        await validate_native_target(resolve_execution_target(row_id))
    assert set(_available(sid).values()) == {0}


@pytest.mark.asyncio
async def test_a_failed_observation_leaves_the_turn_result_and_emits_an_event(
    isolated_db, listing, monkeypatch,
) -> None:
    from infra import product_events
    from llm import network_discovery as nd
    from llm import server_observation
    from llm.router import LLMResponse, _observed

    sid = await _discovered_cloud(listing)
    events: list[tuple[str, str, dict]] = []

    def _broken(_api_base):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(server_observation, "cloud_server_for_base", _broken)
    monkeypatch.setattr(
        product_events, "emit_product_event",
        lambda category, action, **kw: events.append((category, action, kw["properties"])),
    )
    failed = LLMResponse(content="Error calling LLM: denied", finish_reason="error",
                         endpoint_failure="auth")

    async def _refused():
        return failed

    assert await _observed(_CLOUD_BASE, _refused()) is failed
    await _drain()

    assert events == [("external_endpoint", "observation_failed",
                       {"source": "dispatch", "error": "RuntimeError"})]
    assert nd.get_server(sid)["status"] == "online"


@pytest.mark.asyncio
async def test_isolated_structured_dispatch_records_its_outcome(
    isolated_db, listing, monkeypatch,
) -> None:
    from llm import base
    from llm import isolated_structured
    from llm import network_discovery as nd
    from llm.resolution import resolve_execution_target

    sid = await _discovered_cloud(listing)

    async def _acompletion(**_kwargs):
        raise _request(401)

    monkeypatch.setattr(base, "acompletion", _acompletion)

    async def _as_resolved(target):
        return target

    target = resolve_execution_target(f"external@{sid}/qwen-flash")
    request = isolated_structured.IsolatedStructuredRequest(
        system_prompt="fixed policy", user_prompt="{}",
        output_schema={"type": "object"}, max_output_tokens=100,
        allow_provider_retries=False,
    )
    with pytest.raises(Exception):
        await isolated_structured._invoke_native_structured(
            target, request, validate_target=_as_resolved,
        )
    await _drain()

    assert nd.get_server(sid)["status"] == "offline"
