"""Stored sign-in at work: C-R1, C-R5, C-R6 and the refresh, sign-out and leak half of C-R9 and C-R10."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from auth_fixtures import OWNER_USER
from infra.database import get_db
from oauth_fake_as import ACCESS, BASE, DCR_SECRET, MCP_URL, REFRESH
from oauth_fixtures import browse, env, sinks  # noqa: F401
import tools.mcp_oauth_auth as mcp_oauth_auth
import tools.mcp_oauth_flow as flow
import tools.mcp_oauth_protocol as protocol
import tools.mcp_oauth_secrets as mcp_oauth_secrets
import tools.mcp_oauth_signout as signout
import tools.mcp_oauth_store as store
from tools.mcp_config import MCPConfigError
from tools.mcp_oauth_auth import OAuthGrantAuth

PERSON = ("person", OWNER_USER)


async def signed_in(env, *, expired: bool = False) -> store.Grant:
    link = (await env.start())["authorization_url"]
    await browse(env.fake.consent(link))
    grant = store.load(env.app_id)
    assert grant is not None
    if expired:
        grant.expires_at = time.time() - 10
        store.rotate(grant)
    return grant


async def call(env, auth: OAuthGrantAuth, url: str = MCP_URL) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.fake.app), auth=auth) as client:
        return await client.post(url)


async def test_the_stored_token_is_attached_to_the_servers_own_requests(env):  # C-R4
    grant = await signed_in(env)
    response = await call(env, OAuthGrantAuth(env.app_id, MCP_URL))
    assert response.status_code == 200 and env.fake.refresh_requests == []
    assert grant.access_token in env.fake.valid_access


@pytest.mark.parametrize("change", ["replace_config", "approved_proposal"])
async def test_a_grant_never_goes_to_an_address_it_was_not_issued_for(env, change):  # C-R4
    from api.connected_app_signin_rows import sign_in_view
    from connections.app_lifecycle import approve_proposal, get_app, propose_config, replace_config
    from connections.app_reach import runtime_app_config

    moved = "http://localhost:7999/mcp"  # the same fake server under another origin
    await signed_in(env, expired=True)
    revision = get_app(env.app_id, db=get_db())["revision"]
    if change == "replace_config":
        replace_config(env.app_id, {"url": moved}, expected_revision=revision, db=get_db())
    else:
        propose_config(env.app_id, {"url": moved}, actor_thread_id="thread-a", expected_revision=revision, db=get_db())
        approve_proposal(env.app_id, audit_actor=PERSON, db=get_db())
    _, config = runtime_app_config(env.app_id, db=get_db())
    auth = OAuthGrantAuth(env.app_id, config["url"])
    response = await call(env, auth, moved)
    assert config["url"] == moved and response.status_code == 401 and auth.challenged is True  # reads needs_sign_in
    assert env.fake.mcp_hosts_with_auth == [] and env.fake.refresh_requests == []
    assert sign_in_view(config, env.app_id, person=True, db=get_db())["signed_in"] is False
    assert store.load(env.app_id) is not None  # kept until the person signs in there, which replaces and revokes it


async def test_a_server_that_challenges_without_a_sign_in_reads_needs_sign_in(env):  # C-R1
    auth = OAuthGrantAuth(env.app_id, MCP_URL)
    assert (await call(env, auth)).status_code == 401 and auth.challenged is True


async def test_an_expired_token_refreshes_once_and_the_pair_is_rotated(env):  # C-R5
    old = await signed_in(env, expired=True)
    response = await call(env, OAuthGrantAuth(env.app_id, MCP_URL))
    fresh = store.load(env.app_id)
    assert response.status_code == 200 and len(env.fake.refresh_requests) == 1
    assert fresh.access_token == ACCESS.format(n=2) and fresh.refresh_token == REFRESH.format(n=2)
    assert fresh.access_token != old.access_token and fresh.refresh_count == 1


async def test_a_401_triggers_one_refresh_then_a_retry(env):  # C-R5
    await signed_in(env)
    env.fake.valid_access = {"some-newer-token-than-stored"}
    env.fake.mint = lambda: (ACCESS.format(n=9), REFRESH.format(n=9))
    env.fake.valid_access.add(ACCESS.format(n=9))
    response = await call(env, OAuthGrantAuth(env.app_id, MCP_URL))
    assert response.status_code == 200 and len(env.fake.refresh_requests) == 1


async def test_a_401_storm_makes_one_refresh(env):  # C-R5, C-R9
    await signed_in(env, expired=True)
    auths = [OAuthGrantAuth(env.app_id, MCP_URL) for _ in range(12)]
    responses = await asyncio.gather(*(call(env, auth) for auth in auths))
    assert all(r.status_code == 200 for r in responses) and len(env.fake.refresh_requests) == 1


async def test_a_rejected_refresh_ends_the_sign_in_and_reports_it(env, sinks):  # C-R5, C-R9, C-R10
    grant = await signed_in(env, expired=True)
    env.fake.refresh_mode = "rejected"
    auth = OAuthGrantAuth(env.app_id, MCP_URL)
    await call(env, auth)
    await asyncio.sleep(0.05)
    assert store.load(env.app_id) is None and env.lost == [env.app_id]
    assert "refresh_failed:rejected" in env.audit()
    sinks.assert_no(grant.access_token, grant.refresh_token)


async def test_a_malformed_refresh_response_keeps_the_sign_in_and_leaks_nothing(env, sinks):  # C-R9
    await signed_in(env, expired=True)
    env.fake.refresh_mode = "malformed"
    await call(env, OAuthGrantAuth(env.app_id, MCP_URL))
    assert store.load(env.app_id) is not None and env.lost == []
    assert "refresh_failed:unavailable" in env.audit()
    sinks.assert_no("LEAKED-access-token-zzzz")


async def test_a_provider_outage_keeps_the_sign_in(env):  # C-R5
    await signed_in(env, expired=True)
    env.fake.refresh_mode = "down"
    await call(env, OAuthGrantAuth(env.app_id, MCP_URL))
    assert store.load(env.app_id) is not None and env.lost == []


async def test_a_rotation_write_failure_leads_to_needs_sign_in(env, monkeypatch):  # C-R9
    await signed_in(env, expired=True)

    def broken(grant, *, db=None):
        raise store.GrantStoreError("the refreshed sign-in could not be kept")

    monkeypatch.setattr(store, "rotate", broken)
    await call(env, OAuthGrantAuth(env.app_id, MCP_URL))
    await asyncio.sleep(0.05)
    assert store.load(env.app_id) is None and env.lost == [env.app_id]
    assert "refresh_failed:write_failed" in env.audit()


async def test_rotation_can_only_replace_an_existing_sign_in(env):  # Governance: refresh never creates a grant
    grant = await signed_in(env)
    store.end(env.app_id)
    with pytest.raises(store.GrantStoreError):
        store.rotate(grant)
    assert store.load(env.app_id) is None


async def test_a_token_is_never_sent_to_another_origin(env):  # Attach rule
    await signed_in(env)
    with pytest.raises(MCPConfigError):
        await call(env, OAuthGrantAuth(env.app_id, MCP_URL), url="http://127.0.0.1:1/other")


def told_while_stored(env, monkeypatch) -> list[str | None]:
    """The app's stored access token at each moment a grant is revoked at the provider."""
    seen: list[str | None] = []
    real = flow.tell_provider

    async def tell(grant):
        stored = store.load(env.app_id)
        seen.append(stored.access_token if stored else None)
        return await real(grant)

    monkeypatch.setattr(flow, "tell_provider", tell)
    return seen


async def test_sign_out_ends_the_sign_in_here_then_revokes_it(env, monkeypatch):  # C-R6, Governance 3
    grant = await signed_in(env)
    seen = told_while_stored(env, monkeypatch)
    result = await signout.sign_out(env.app_id, actor=PERSON)
    assert result == {"had_sign_in": True, "provider_told": True, "provider_url": None}
    assert seen == [None] and env.fake.revoked == [grant.refresh_token] and store.load(env.app_id) is None
    assert env.audit()[-1] == "sign_out_revoked"


@pytest.mark.parametrize("mode,reason", [("fail", "refused"), ("hang", "unreachable")])
async def test_a_revoke_failure_or_timeout_still_ends_it_and_is_audited(env, sinks, mode, reason):  # C-R6, Governance 3
    grant = await signed_in(env)
    env.fake.revoke_mode = mode
    result = await signout.sign_out(env.app_id, actor=PERSON)
    assert result == {"had_sign_in": True, "provider_told": False, "provider_url": BASE}
    assert store.load(env.app_id) is None and env.audit()[-1] == f"sign_out_revoke_failed:{reason}:{BASE}"
    sinks.assert_no(grant.access_token, grant.refresh_token)


async def test_sign_out_cancels_a_pending_sign_in_as_its_actor(env):  # Governance 5
    await env.start()
    assert (await signout.sign_out(env.app_id, actor=PERSON))["had_sign_in"] is False
    assert flow.status(env.app_id) == {"status": "idle"}
    assert env.audit()[-2:] == ["sign_in_cancelled", "sign_out_no_sign_in"]


async def test_a_provider_without_revocation_is_said_so(env):  # C-R6
    env.fake.revoke = False
    await signed_in(env)
    result = await signout.sign_out(env.app_id, actor=PERSON)
    assert result["provider_told"] is False and result["provider_url"] == BASE and store.load(env.app_id) is None


async def test_signing_out_without_a_sign_in_is_harmless(env):  # C-R6
    assert (await signout.sign_out(env.app_id, actor=PERSON))["had_sign_in"] is False


async def test_stored_tokens_are_redacted_again_after_a_restart(env):  # C-R9
    grant = await signed_in(env)
    mcp_oauth_secrets.forget(grant.access_token, grant.refresh_token, DCR_SECRET)
    assert grant.access_token not in mcp_oauth_secrets.registered()
    assert store.register_stored() == 1
    assert grant.access_token in mcp_oauth_secrets.registered()
    assert mcp_oauth_secrets.redact(f"x {grant.refresh_token} y") == "x [REDACTED] y"


async def test_stored_tokens_reach_no_table_but_the_key_store(env):  # C-R9 (config, audit, snapshots)
    grant = await signed_in(env)
    db = get_db()
    needles = (grant.access_token, grant.refresh_token, DCR_SECRET)
    for table in ("connected_apps", "connected_app_audit"):
        text = " ".join(str(tuple(row)) for row in db.execute(f"SELECT * FROM {table}"))
        assert not any(needle in text for needle in needles), table
    assert store.entry_name(env.app_id).startswith("mcp-oauth-")


async def test_a_stored_token_stays_redacted_after_many_newer_values(env):  # finding 7
    grant = await signed_in(env)
    mcp_oauth_secrets.register(*(f"newer-secret-value-{n:04d}" for n in range(300)))
    text = f"x {grant.access_token} {grant.refresh_token} y"
    assert mcp_oauth_secrets.redact(text) == f"x {mcp_oauth_secrets.MARK} {mcp_oauth_secrets.MARK} y"


async def test_an_ended_sign_in_stops_being_held_but_stays_redacted(env):  # finding 7
    grant = await signed_in(env)
    store.end(env.app_id)
    assert mcp_oauth_secrets.redact(grant.access_token) == mcp_oauth_secrets.MARK


async def test_reading_a_sign_in_writes_nothing(env):  # a GET reads only
    await signed_in(env)
    before = get_db().total_changes
    assert store.load(env.app_id) is not None and store.load("an-app-never-signed-in") is None
    assert get_db().total_changes == before


async def test_a_refresh_waits_for_the_apps_one_lock(env):  # Governance 4 (C-R5)
    await signed_in(env, expired=True)
    async with store.grant_lock(env.app_id):
        pending = asyncio.create_task(call(env, OAuthGrantAuth(env.app_id, MCP_URL)))
        await asyncio.sleep(0.1)
        assert env.fake.refresh_requests == [] and not pending.done()
    assert (await pending).status_code == 200 and len(env.fake.refresh_requests) == 1


async def test_a_completed_sign_in_waits_for_the_apps_one_lock(env):  # Governance 4 (C-R5)
    link = (await env.start())["authorization_url"]
    async with store.grant_lock(env.app_id):
        pending = asyncio.create_task(browse(env.fake.consent(link)))
        await asyncio.sleep(0.2)
        assert store.load(env.app_id) is None and not pending.done()
    assert (await pending).status_code == 200 and store.load(env.app_id) is not None


class _CountingStream(httpx.AsyncByteStream):
    """An endless provider body that counts how much of it was pulled."""

    def __init__(self) -> None:
        self.sent = 0

    async def __aiter__(self):
        while self.sent < 64 * 1024 * 1024:
            self.sent += 64 * 1024
            yield b" " * (64 * 1024)


async def test_an_oversized_provider_body_is_cut_off_unread(env):  # finding 8
    stream = _CountingStream()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    async with httpx.AsyncClient(transport=transport) as http:
        response, raw = await protocol._send(http, "POST", f"{BASE}/token")
    assert response.status_code == 200 and len(raw) == protocol._BODY_MAX
    assert stream.sent <= protocol._BODY_MAX + 64 * 1024


async def test_the_sign_in_registration_stores_the_runtime_start_path(env):  # run_in_db path is real
    from infra.db_async import run_in_db

    await signed_in(env)
    mcp_oauth_secrets.forget(ACCESS.format(n=1))
    assert await run_in_db(store.register_stored) == 1
    assert ACCESS.format(n=1) in mcp_oauth_secrets.registered()
