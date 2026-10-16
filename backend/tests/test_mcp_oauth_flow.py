"""Hosted-app sign-in flow: C-R2, C-R3, C-R4, C-R8a and the callback half of C-R9 and C-R10."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest

from infra.database import get_db
from oauth_fake_as import ACCESS, BASE, DCR_SECRET, MCP_URL
from oauth_fixtures import browse, env, sinks  # noqa: F401
import tools.mcp_oauth_flow as flow
import tools.mcp_oauth_listener as mcp_oauth_listener
import tools.mcp_oauth_protocol as protocol
import tools.mcp_oauth_store as store
from tools.mcp_config import MCPConfigError
from tools.mcp_oauth_errors import SignInError


async def test_dcr_sign_in_keeps_the_grant_for_the_starting_person(env, sinks):  # C-R2, C-R4, C-R10
    started = await env.start()
    link = started["authorization_url"]
    assert started["reopened"] is False and link.startswith(f"{BASE}/authorize?")
    assert env.fake.registrations[0]["application_type"] == "native"
    assert env.fake.registrations[0]["token_endpoint_auth_method"] == "none"

    page = await browse(env.fake.consent(link))

    assert page.status_code == 200 and "connected to CORSIA" in page.text
    grant = store.load(env.app_id, env.user_id)
    assert grant and grant.access_token == ACCESS.format(n=1) and grant.account == "person@example.test"
    assert grant.client_secret == DCR_SECRET and grant.issuer == BASE and grant.resource == MCP_URL
    assert flow.status(env.app_id) == {"status": "idle"} and env.connects == [env.app_id]
    assert env.audit() == ["sign_in_started", "sign_in_completed"]
    verifier = env.fake.token_requests[0]["code_verifier"]
    sinks.assert_no(grant.access_token, grant.refresh_token, DCR_SECRET, verifier, env.fake.token_requests[0]["code"])


async def test_authorization_link_carries_pkce_resource_and_a_strong_state(env):  # C-R2
    link = (await env.start())["authorization_url"]
    query = {key: values[0] for key, values in parse_qs(urlsplit(link).query).items()}
    assert query["code_challenge_method"] == "S256" and query["resource"] == MCP_URL and len(query["state"]) >= 43
    assert query["redirect_uri"].startswith("http://127.0.0.1:") and query["redirect_uri"].endswith("/callback")


async def test_empty_metadata_setting_never_sends_a_metadata_url(env):  # C-R2
    env.fake.metadata_doc = True
    await env.start(metadata_url="")
    assert len(env.fake.registrations) == 1
    link = flow.pending_link(env.app_id)
    assert "client_id=dcr-client-1" in link and "client_id=http" not in link


async def test_metadata_document_is_the_client_id_when_set_and_advertised(env):  # C-R2
    env.fake.metadata_doc = True
    document = "http://127.0.0.1:8001/client.json"
    link = (await env.start(metadata_url=document))["authorization_url"]
    assert env.fake.registrations == []
    assert parse_qs(urlsplit(link).query)["client_id"] == [document]


async def test_metadata_document_is_not_used_when_the_server_does_not_advertise_it(env):  # C-R2
    await env.start(metadata_url="http://127.0.0.1:8001/client.json")
    assert len(env.fake.registrations) == 1


async def test_pasted_client_id_beats_registration(env):  # C-R2
    link = (await env.start(config={"url": MCP_URL, "oauth_client_id": "pasted-id-42"}))["authorization_url"]
    assert env.fake.registrations == [] and parse_qs(urlsplit(link).query)["client_id"] == ["pasted-id-42"]


async def test_a_second_press_reopens_the_same_page(env):  # C-R8a
    first = await env.start()
    second = await env.start()
    assert second["reopened"] is True and second["authorization_url"] == first["authorization_url"]
    assert len(env.fake.registrations) == 1 and len(flow._flows) == 1


async def test_wrong_state_is_refused_and_the_flow_keeps_listening(env):  # C-R3, C-R9
    link = (await env.start())["authorization_url"]
    redirect = env.fake.consent(link)
    bad = redirect.replace(parse_qs(urlsplit(redirect).query)["state"][0], "wrong-state-value")
    assert (await browse(bad)).status_code == 400
    assert flow.status(env.app_id)["status"] == "pending"
    assert (await browse(redirect)).status_code == 200
    assert store.load(env.app_id, env.user_id) is not None
    assert "sign_in_refused:state:1" in env.audit()


async def test_a_valid_code_with_another_flows_state_is_refused(env):  # C-R9
    other = env.new_app("Other")
    link_a = (await env.start())["authorization_url"]
    link_b = (await env.start(app_id=other))["authorization_url"]
    redirect_b = env.fake.consent(link_b)
    port_a = parse_qs(urlsplit(link_a).query)["redirect_uri"][0]
    crossed = port_a + "?" + urlsplit(redirect_b).query
    assert (await browse(crossed)).status_code == 400
    assert flow.status(env.app_id)["status"] == "pending" and store.load(env.app_id, env.user_id) is None
    assert (await browse(redirect_b)).status_code == 200 and store.load(other, env.user_id) is not None


@pytest.mark.parametrize("sent_iss", [None, "http://evil.example"])
async def test_missing_or_wrong_iss_is_refused_where_required(env, sent_iss):  # C-R9
    env.fake.iss = True
    link = (await env.start())["authorization_url"]
    redirect = env.fake.consent(link, with_iss=False)
    if sent_iss:
        redirect += f"&iss={sent_iss}"
    assert (await browse(redirect)).status_code == 400
    assert flow.status(env.app_id)["status"] == "pending"
    assert (await browse(env.fake.consent(link))).status_code == 200


async def test_a_reused_code_gets_no_second_grant(env):  # C-R9
    link = (await env.start())["authorization_url"]
    redirect = env.fake.consent(link)
    await browse(redirect)
    first = store.load(env.app_id, env.user_id)
    with pytest.raises(Exception):  # the listener closed after the one valid callback
        await browse(redirect)
    code = parse_qs(urlsplit(redirect).query)["code"][0]
    async with protocol.new_client() as http:
        with pytest.raises(SignInError):
            await protocol.exchange_code(
                http, await protocol.discover(http, MCP_URL), client_id="dcr-client-1", code=code,
                redirect_uri=env.fake.codes[code]["redirect"], verifier="x" * 43,
            )
    assert store.load(env.app_id, env.user_id).access_token == first.access_token


async def test_a_flow_whose_session_ended_stores_nothing(env):  # C-R9
    link = (await env.start())["authorization_url"]
    get_db().execute("DELETE FROM auth_tokens WHERE token_hash=?", (env.session_hash,))
    get_db().commit()
    assert (await browse(env.fake.consent(link))).status_code == 200
    assert store.load(env.app_id) is None and flow.status(env.app_id)["failure_class"] == "unknown"
    assert env.fake.token_requests == [] and env.connects == []


async def test_a_rebinding_host_header_is_refused_without_ending_the_flow(env):  # C-R9, CB
    link = (await env.start())["authorization_url"]
    redirect = env.fake.consent(link)
    assert (await browse(redirect, headers={"Host": "evil.example"})).status_code == 400
    assert flow.status(env.app_id)["status"] == "pending"
    assert (await browse(redirect)).status_code == 200


async def test_junk_requests_do_not_end_the_flow(env):  # C-R3
    link = (await env.start())["authorization_url"]
    callback = parse_qs(urlsplit(link).query)["redirect_uri"][0]
    assert (await browse(callback)).status_code == 400
    assert (await browse(callback.replace("/callback", "/other"))).status_code == 404
    assert flow.status(env.app_id)["status"] == "pending"
    assert (await browse(env.fake.consent(link))).status_code == 200


async def test_the_request_cap_ends_the_flow(env):  # C-R3
    link = (await env.start())["authorization_url"]
    callback = parse_qs(urlsplit(link).query)["redirect_uri"][0]
    for _ in range(mcp_oauth_listener.REQUEST_CAP):
        try:
            await browse(callback)
        except Exception:
            break
    await asyncio.sleep(0.05)
    assert flow.status(env.app_id)["status"] == "failed" and store.load(env.app_id) is None


async def test_the_flow_expires(env, monkeypatch):  # C-R3
    monkeypatch.setattr(flow, "EXPIRY_SECONDS", 0.05)
    await env.start()
    await asyncio.sleep(0.2)
    assert flow.status(env.app_id)["failure_class"] == "timed_out"
    assert "sign_in_expired" in env.audit()


async def test_cancel_ends_the_flow_and_closes_the_port(env):  # C-R10
    link = (await env.start())["authorization_url"]
    callback = parse_qs(urlsplit(link).query)["redirect_uri"][0]
    assert await flow.cancel(env.app_id, user_id=env.user_id) is True
    assert flow.status(env.app_id) == {"status": "idle"} and await flow.cancel(env.app_id, user_id=env.user_id) is False
    with pytest.raises(Exception):
        await browse(callback)
    assert env.audit()[-1] == "sign_in_cancelled"


async def test_declining_at_the_provider_reads_declined(env):  # C-R3
    link = (await env.start())["authorization_url"]
    assert (await browse(env.fake.consent(link, deny=True))).status_code == 200
    assert flow.status(env.app_id)["failure_class"] == "declined" and "sign_in_failed:declined" in env.audit()


async def test_a_malformed_token_response_stores_nothing_and_leaks_nothing(env, sinks):  # C-R9
    env.fake.token_mode = "malformed"
    link = (await env.start())["authorization_url"]
    await browse(env.fake.consent(link))
    assert store.load(env.app_id) is None and flow.status(env.app_id)["failure_class"] == "unknown"
    assert "LEAKED" not in str(flow.status(env.app_id)) and "LEAKED" not in " ".join(env.audit())
    sinks.assert_no("LEAKED-access-token-yyyy", "LEAKED-refresh-token-yyyy")


async def test_the_listener_binds_loopback_only(env):  # C-R3 (CB1)
    await env.start()
    assert flow._flows[env.app_id].listener.bound_addresses() == ["127.0.0.1"]


async def test_a_non_loopback_callback_override_is_refused_before_any_listener(env):  # C-R9
    for host in ("0.0.0.0", "evil.example", "::"):
        with pytest.raises(MCPConfigError):
            await env.start(config={"url": MCP_URL, "oauth_callback": {"host": host}})
    assert flow._flows == {}


async def test_a_callback_override_sets_port_and_path(env):  # C-R3
    link = (await env.start(config={"url": MCP_URL, "oauth_callback": {"path": "/oauth/cb", "host": "localhost"}}))["authorization_url"]
    redirect = parse_qs(urlsplit(link).query)["redirect_uri"][0]
    assert redirect.startswith("http://localhost:") and redirect.endswith("/oauth/cb")


async def test_a_redirect_to_another_host_during_discovery_fails(env):  # C-R9
    env.fake.redirect_resource_metadata = True
    with pytest.raises(SignInError):
        await env.start()
    assert flow._flows == {}
