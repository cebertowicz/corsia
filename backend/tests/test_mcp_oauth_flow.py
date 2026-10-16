"""Hosted-app sign-in flow: C-R2, C-R3, C-R4, C-R8a and the callback half of C-R9 and C-R10.

A sign-in belongs to the app and a pending flow to the session that started it.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from infra.database import get_db
from infra.db_async import run_in_db
from oauth_fake_as import ACCESS, BASE, DCR_SECRET, MCP_URL
from oauth_fixtures import browse, env, sinks  # noqa: F401
from vendor_account_fixtures import isolated_shutdown_tasks  # noqa: F401
import tools.mcp_oauth_flow as flow
import tools.mcp_oauth_listener as mcp_oauth_listener
import tools.mcp_oauth_protocol as protocol
import tools.mcp_oauth_store as store
from tools.mcp_config import MCPConfigError
from tools.mcp_oauth_errors import SignInError


async def test_dcr_sign_in_keeps_the_grant_as_the_apps_signed_in_by_its_starter(env, sinks):  # C-R2, C-R4, C-R10
    started = await env.start()
    link = started["authorization_url"]
    assert started["reopened"] is False and link.startswith(f"{BASE}/authorize?")
    assert env.fake.registrations[0]["application_type"] == "native"
    assert env.fake.registrations[0]["token_endpoint_auth_method"] == "none"

    page = await browse(env.fake.consent(link))

    assert page.status_code == 200 and "connected to CORSIA" in page.text
    grant = store.load(env.app_id)
    assert grant and grant.access_token == ACCESS.format(n=1) and grant.account == "person@example.test"
    assert grant.signed_in_by == env.user_id
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
    link = flow.pending_link(env.app_id, user_id=env.user_id, session_hash=env.session_hash)
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


async def test_another_session_never_gets_the_pending_link_and_its_press_ends_that_flow(env):  # finding 1
    first = await env.start()
    second = await env.start(other=True)
    assert second["reopened"] is False and second["authorization_url"] != first["authorization_url"]
    assert flow.pending_link(env.app_id, user_id=env.user_id, session_hash=env.session_hash) is None
    assert flow.pending_link(env.app_id, user_id=env.user_id, session_hash=env.other_session_hash) is None
    mine = flow.pending_link(env.app_id, user_id=env.other_user_id, session_hash=env.other_session_hash)
    assert mine == second["authorization_url"] and len(flow._flows) == 1
    assert env.audit() == ["sign_in_started", "sign_in_superseded", "sign_in_started"]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.parametrize("fixed_port", [False, True])
async def test_a_superseded_flows_callback_is_refused_and_stores_nothing(env, fixed_port):  # Governance 2
    config = {"url": MCP_URL, "oauth_callback": {"port": free_port()}} if fixed_port else None
    link_a = (await env.start(config=config))["authorization_url"]
    link_b = (await env.start(config=config, other=True))["authorization_url"]
    redirect_a = env.fake.consent(link_a)
    if fixed_port:  # B's listener took the port: A's state is not the one it is waiting for
        assert (await browse(redirect_a)).status_code == 400
    else:  # A's listener is closed
        with pytest.raises(Exception):
            await browse(redirect_a)
    assert store.load(env.app_id) is None and env.fake.token_requests == [] and env.connects == []
    assert flow.status(env.app_id)["status"] == "pending"
    assert (await browse(env.fake.consent(link_b))).status_code == 200
    assert store.load(env.app_id).signed_in_by == env.other_user_id


@pytest.mark.parametrize("ender", ["other_session", "cancel"])
async def test_a_flow_superseded_or_cancelled_during_the_code_exchange_stores_nothing(env, monkeypatch, ender):  # Governance 2
    link_a = (await env.start())["authorization_url"]
    exchanging, release = asyncio.Event(), asyncio.Event()
    real = protocol.exchange_code

    async def slow_exchange(*args, **kwargs):
        exchanging.set()
        await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(protocol, "exchange_code", slow_exchange)
    callback = asyncio.create_task(browse(env.fake.consent(link_a)))
    await exchanging.wait()
    if ender == "cancel":
        assert await flow.cancel(env.app_id, actor=("person", env.user_id)) is True
    else:
        await env.start(other=True)
    release.set()
    assert (await callback).status_code == 200
    assert store.load(env.app_id) is None and env.connects == []
    assert flow.status(env.app_id)["status"] == ("idle" if ender == "cancel" else "pending")
    assert "sign_in_completed" not in env.audit()


@pytest.mark.parametrize("ender", ["other_session", "cancel"])
async def test_a_cancel_or_press_landing_while_the_grant_is_written_is_ordered_after_it(env, monkeypatch, ender):  # Governance 2
    """One per-app lock orders every change to the current flow and the grant: no reply says cancelled or
    superseded for a sign-in whose grant was kept."""
    link = (await env.start())["authorization_url"]
    writing, release = threading.Event(), threading.Event()
    real = flow._keep

    def slow_keep(grant):
        writing.set()
        release.wait(5)
        return real(grant)

    monkeypatch.setattr(flow, "_keep", slow_keep)
    callback = asyncio.create_task(browse(env.fake.consent(link)))
    assert await asyncio.to_thread(writing.wait, 5)
    if ender == "cancel":
        ending = asyncio.create_task(flow.cancel(env.app_id, actor=("person", env.user_id)))
    else:
        ending = asyncio.create_task(env.start(other=True))
    await asyncio.sleep(0.05)
    waited = not ending.done()
    release.set()
    assert (await callback).status_code == 200
    answer = await ending
    assert waited
    assert store.load(env.app_id).signed_in_by == env.user_id
    actions = env.audit()
    assert actions.count("sign_in_completed") == 1
    assert "sign_in_cancelled" not in actions and "sign_in_superseded" not in actions
    if ender == "cancel":
        assert answer is False and flow.status(env.app_id) == {"status": "idle"}
    else:
        assert answer["reopened"] is False and flow.status(env.app_id)["status"] == "pending"
        assert flow._flows[env.app_id].user_id == env.other_user_id


async def signed_in_twice(env, *, account: str | None = None) -> tuple[store.Grant, store.Grant]:
    await browse(env.fake.consent((await env.start())["authorization_url"]))
    first = store.load(env.app_id)
    if account:
        env.fake.account = account
    await browse(env.fake.consent((await env.start(other=True))["authorization_url"]))
    return first, store.load(env.app_id)


async def test_a_new_sign_in_replaces_the_grant_and_only_then_revokes_the_old(env, monkeypatch):  # Governance 1
    seen: list[str] = []
    real = flow.tell_provider

    async def tell(grant):
        seen.append(store.load(env.app_id).access_token)
        return await real(grant)

    monkeypatch.setattr(flow, "tell_provider", tell)
    first, second = await signed_in_twice(env, account="second@example.test")
    assert second.access_token == ACCESS.format(n=2) and second.signed_in_by == env.other_user_id
    assert seen == [ACCESS.format(n=2)] and env.fake.revoked == [first.refresh_token]
    assert "sign_in_replaced_revoked" in env.audit()


async def test_a_failed_revoke_of_the_replaced_grant_keeps_the_new_one(env, sinks):  # Governance 1
    env.fake.revoke_mode = "fail"
    first, second = await signed_in_twice(env, account="second@example.test")
    assert second.access_token == ACCESS.format(n=2) and env.fake.revoked == [first.refresh_token]
    assert f"sign_in_replaced_revoke_failed:refused:{BASE}" in env.audit()
    sinks.assert_no(first.access_token, first.refresh_token)


async def test_the_same_client_and_account_signing_in_again_does_not_revoke_the_new_grant(env):  # Governance 1
    _, second = await signed_in_twice(env)
    assert env.fake.revoked == [] and store.load(env.app_id).access_token == second.access_token
    assert "sign_in_replaced_not_revoked:same_client_and_account" in env.audit()


@pytest.mark.parametrize("unknown", ["new", "both"])
async def test_an_unknown_account_is_never_taken_as_the_same_one(env, unknown):  # Governance 1
    if unknown == "both":
        env.fake.account = None
    await browse(env.fake.consent((await env.start())["authorization_url"]))
    env.fake.account = None
    await browse(env.fake.consent((await env.start(other=True))["authorization_url"]))
    assert store.load(env.app_id).account is None and env.fake.revoked == []
    assert "sign_in_replaced_not_revoked:same_client_account_unknown" in env.audit()
    assert "sign_in_replaced_not_revoked:same_client_and_account" not in env.audit()


async def test_the_revoke_authenticates_a_confidential_client_and_names_the_refresh_token(env):  # RFC 7009
    first, _ = await signed_in_twice(env, account="second@example.test")
    assert first.client_secret == DCR_SECRET
    assert env.fake.revoke_forms == [{
        "token": first.refresh_token, "token_type_hint": "refresh_token", "client_id": "dcr-client-1", "client_secret": DCR_SECRET,
    }]


async def test_a_grant_without_a_refresh_token_is_revoked_as_an_access_token(env):  # RFC 7009
    await browse(env.fake.consent((await env.start())["authorization_url"]))
    grant = store.load(env.app_id)
    grant.refresh_token, grant.client_secret = None, None
    assert await flow.tell_provider(grant) is None
    assert env.fake.revoke_forms == [{"token": grant.access_token, "token_type_hint": "access_token", "client_id": "dcr-client-1"}]


@pytest.mark.parametrize("ending", ["cancel", "expire", "decline", "fail"])
async def test_a_sign_in_that_does_not_complete_leaves_the_existing_grant_working(env, monkeypatch, ending):  # Governance 1
    await browse(env.fake.consent((await env.start())["authorization_url"]))
    kept = store.load(env.app_id)
    if ending == "expire":
        monkeypatch.setattr(flow, "EXPIRY_SECONDS", 0.05)
    link = (await env.start(other=True))["authorization_url"]
    if ending == "cancel":
        await flow.cancel(env.app_id, actor=("person", env.other_user_id))
    elif ending == "expire":
        await asyncio.sleep(0.2)
    elif ending == "decline":
        await browse(env.fake.consent(link, deny=True))
    else:
        env.fake.token_mode = "malformed"
        await browse(env.fake.consent(link))
    assert flow.status(env.app_id)["status"] != "pending"
    assert store.load(env.app_id).access_token == kept.access_token and env.fake.revoked == []


async def test_a_sign_in_for_a_removed_app_stores_nothing(env):  # Governance 3
    link = (await env.start())["authorization_url"]
    get_db().execute("DELETE FROM connected_apps WHERE id=?", (env.app_id,))
    get_db().commit()
    await browse(env.fake.consent(link))
    assert store.load(env.app_id) is None and flow.status(env.app_id)["failure_class"] == "unknown"


async def test_removing_an_app_ends_the_sign_in_in_the_same_write_then_revokes(env):  # Governance 3, 5
    import tools.mcp_oauth_signout as signout
    from connections.app_lifecycle import delete_app

    await browse(env.fake.consent((await env.start())["authorization_url"]))
    grant = store.load(env.app_id)
    def delete(db):
        return delete_app(env.app_id, remove_keys=False, audit_actor=("agent", "thread-a"), db=db)

    await signout.remove_app(
        env.app_id, lambda: run_in_db(signout.delete_with_sign_in, env.app_id, delete), actor=("agent", "thread-a"),
    )
    assert store.load(env.app_id) is None and env.fake.revoked == [grant.refresh_token]
    rows = get_db().execute(
        "SELECT actor_kind, actor_id FROM connected_app_audit WHERE app_id=? AND action='remove_revoked'", (env.app_id,),
    ).fetchall()
    assert [(row["actor_kind"], row["actor_id"]) for row in rows] == [("agent", "thread-a")]


async def test_a_failed_delete_ends_and_revokes_nothing(env):  # Governance 3
    import tools.mcp_oauth_signout as signout

    await browse(env.fake.consent((await env.start())["authorization_url"]))
    grant = store.load(env.app_id)

    def failing(db):
        raise RuntimeError("the app could not be removed")

    with pytest.raises(RuntimeError):
        await signout.remove_app(
            env.app_id, lambda: run_in_db(signout.delete_with_sign_in, env.app_id, failing), actor=("person", env.user_id),
        )
    assert store.load(env.app_id).access_token == grant.access_token and env.fake.revoked == []


async def test_shutdown_stops_a_pending_sign_in(env, isolated_shutdown_tasks):  # finding 6
    from app_shutdown_owned_tasks import shutdown_owned_tasks

    link = (await env.start())["authorization_url"]
    pending = flow._flows[env.app_id]
    await shutdown_owned_tasks([None] * 5, Mock())
    assert flow._flows == {} and pending.timer.done()
    with pytest.raises(Exception):
        await browse(env.fake.consent(link))
    assert env.audit()[-1] == "sign_in_cancelled"


async def test_wrong_state_is_refused_and_the_flow_keeps_listening(env):  # C-R3, C-R9
    link = (await env.start())["authorization_url"]
    redirect = env.fake.consent(link)
    bad = redirect.replace(parse_qs(urlsplit(redirect).query)["state"][0], "wrong-state-value")
    assert (await browse(bad)).status_code == 400
    assert flow.status(env.app_id)["status"] == "pending"
    assert (await browse(redirect)).status_code == 200
    assert store.load(env.app_id) is not None
    assert "sign_in_refused:state:1" in env.audit()


async def test_a_valid_code_with_another_flows_state_is_refused(env):  # C-R9
    other = env.new_app("Other")
    link_a = (await env.start())["authorization_url"]
    link_b = (await env.start(app_id=other))["authorization_url"]
    redirect_b = env.fake.consent(link_b)
    port_a = parse_qs(urlsplit(link_a).query)["redirect_uri"][0]
    crossed = port_a + "?" + urlsplit(redirect_b).query
    assert (await browse(crossed)).status_code == 400
    assert flow.status(env.app_id)["status"] == "pending" and store.load(env.app_id) is None
    assert (await browse(redirect_b)).status_code == 200 and store.load(other) is not None


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
    first = store.load(env.app_id)
    with pytest.raises(Exception):  # the listener closed after the one valid callback
        await browse(redirect)
    code = parse_qs(urlsplit(redirect).query)["code"][0]
    async with protocol.new_client() as http:
        with pytest.raises(SignInError):
            await protocol.exchange_code(
                http, await protocol.discover(http, MCP_URL), client_id="dcr-client-1", code=code,
                redirect_uri=env.fake.codes[code]["redirect"], verifier="x" * 43,
            )
    assert store.load(env.app_id).access_token == first.access_token


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
    assert await flow.cancel(env.app_id, actor=("person", env.user_id)) is True
    assert flow.status(env.app_id) == {"status": "idle"} and await flow.cancel(env.app_id, actor=("person", env.user_id)) is False
    with pytest.raises(Exception):
        await browse(callback)
    assert env.audit()[-1] == "sign_in_cancelled"


async def test_declining_at_the_provider_reads_declined(env):  # C-R3
    link = (await env.start())["authorization_url"]
    assert (await browse(env.fake.consent(link, deny=True))).status_code == 200
    assert flow.status(env.app_id)["failure_class"] == "declined" and "sign_in_failed:declined" in env.audit()


@pytest.mark.parametrize("then", ["sign_in", "sign_out"])
async def test_a_failure_another_try_would_not_fix_is_cleared_by_sign_in_or_sign_out(env, then):  # no dead end
    import tools.mcp_oauth_signout as signout

    link = (await env.start())["authorization_url"]
    await browse(env.fake.consent(link, deny=True).replace("error=access_denied", "error=invalid_scope"))
    assert flow.status(env.app_id) == {"status": "failed", "failure_class": "permissions", "at": flow.status(env.app_id)["at"]}
    if then == "sign_in":
        await env.start()
        assert flow.status(env.app_id)["status"] == "pending"
        await flow.cancel(env.app_id, actor=("person", env.user_id))
    else:
        await signout.sign_out(env.app_id, actor=("person", env.user_id))
    assert flow.status(env.app_id) == {"status": "idle"}


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


async def test_a_callback_host_the_listener_does_not_answer_is_refused_before_any_listener(env):  # C-R9
    # [::1] is loopback, but the listener binds 127.0.0.1 alone, so the browser would reach nothing.
    for host in ("0.0.0.0", "evil.example", "::", "[::1]", "::1"):
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


REMOTE_MCP = "https://mcp.example.test/mcp"
REMOTE_AS = "https://as.example.test"
LOCAL = "http://127.0.0.1:9"


def remote_provider(local_field: str | None) -> tuple[list[str], httpx.AsyncClient]:
    """A remote https app whose metadata may point one field at a port on this Mac; records every request."""
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.startswith("/.well-known/oauth-protected-resource"):
            issuer = LOCAL if local_field == "issuer" else REMOTE_AS
            return httpx.Response(200, json={"resource": REMOTE_MCP, "authorization_servers": [issuer]})
        if request.url.path == "/.well-known/oauth-authorization-server":
            meta = {
                "issuer": REMOTE_AS, "authorization_endpoint": f"{REMOTE_AS}/authorize",
                "token_endpoint": f"{REMOTE_AS}/token", "revocation_endpoint": f"{REMOTE_AS}/revoke",
                "userinfo_endpoint": f"{REMOTE_AS}/userinfo", "registration_endpoint": f"{REMOTE_AS}/register",
            }
            if local_field and local_field != "issuer":
                meta[local_field] = f"{LOCAL}/{local_field}"
            return httpx.Response(200, json=meta)
        return httpx.Response(404)

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False)


@pytest.mark.parametrize("local_field", ["issuer", "authorization_endpoint", "token_endpoint"])
async def test_a_remote_app_cannot_point_a_required_endpoint_at_this_mac(local_field):  # SSRF
    seen, client = remote_provider(local_field)
    async with client:
        with pytest.raises(SignInError):
            await protocol.discover(client, REMOTE_MCP)
    assert seen and not any(url.startswith(LOCAL) for url in seen)


@pytest.mark.parametrize("local_field", ["revocation_endpoint", "userinfo_endpoint", "registration_endpoint"])
async def test_a_remote_apps_optional_endpoint_on_this_mac_is_dropped(local_field):  # SSRF
    seen, client = remote_provider(local_field)
    async with client:
        disc = await protocol.discover(client, REMOTE_MCP)
    assert getattr(disc, local_field) is None and not any(url.startswith(LOCAL) for url in seen)


async def test_a_remote_grant_never_refreshes_or_revokes_at_this_mac():  # SSRF
    seen, client = remote_provider(None)
    async with client:
        with pytest.raises(protocol.RefreshUnavailable):
            await protocol.refresh_tokens(
                client, token_endpoint=f"{LOCAL}/token", client_id="c", refresh_token="r" * 12, resource=REMOTE_MCP,
                server_url=REMOTE_MCP,
            )
        assert await protocol.revoke(
            client, f"{LOCAL}/revoke", token="t" * 12, token_type_hint="refresh_token", client_id="c",
            server_url=REMOTE_MCP,
        ) == "unreachable"
    assert seen == []


async def test_an_app_on_this_mac_may_use_plain_http_on_this_mac():
    seen, client = remote_provider(None)
    assert protocol.check_url(f"{LOCAL}/token", server_url=f"{LOCAL}/mcp") == f"{LOCAL}/token"
    with pytest.raises(SignInError):
        protocol.check_url("http://example.test/token", server_url=f"{LOCAL}/mcp")
    with pytest.raises(SignInError):
        protocol.check_url("https://localhost:9/token", server_url=REMOTE_MCP)
    await client.aclose()
    assert seen == []
