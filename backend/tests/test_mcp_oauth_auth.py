"""Stored sign-in at work: C-R1, C-R5, C-R6 and the refresh, sign-out and leak half of C-R9 and C-R10."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from infra.database import get_db
from oauth_fake_as import ACCESS, BASE, DCR_SECRET, MCP_URL, REFRESH
from oauth_fixtures import browse, env, sinks  # noqa: F401
import tools.mcp_oauth_auth as mcp_oauth_auth
import tools.mcp_oauth_secrets as mcp_oauth_secrets
import tools.mcp_oauth_signout as signout
import tools.mcp_oauth_store as store
from tools.mcp_config import MCPConfigError
from tools.mcp_oauth_auth import OAuthGrantAuth


async def signed_in(env, *, expired: bool = False) -> store.Grant:
    link = (await env.start())["authorization_url"]
    await browse(env.fake.consent(link))
    grant = store.load(env.app_id, env.user_id)
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


async def test_a_server_that_challenges_without_a_sign_in_reads_needs_sign_in(env):  # C-R1
    auth = OAuthGrantAuth(env.app_id, MCP_URL)
    assert (await call(env, auth)).status_code == 401 and auth.challenged is True


async def test_an_expired_token_refreshes_once_and_the_pair_is_rotated(env):  # C-R5
    old = await signed_in(env, expired=True)
    response = await call(env, OAuthGrantAuth(env.app_id, MCP_URL))
    fresh = store.load(env.app_id, env.user_id)
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
    assert store.load(env.app_id, env.user_id) is not None and env.lost == []
    assert "refresh_failed:unavailable" in env.audit()
    sinks.assert_no("LEAKED-access-token-zzzz")


async def test_a_provider_outage_keeps_the_sign_in(env):  # C-R5
    await signed_in(env, expired=True)
    env.fake.refresh_mode = "down"
    await call(env, OAuthGrantAuth(env.app_id, MCP_URL))
    assert store.load(env.app_id, env.user_id) is not None and env.lost == []


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
    store.delete_all(env.app_id)
    with pytest.raises(store.GrantStoreError):
        store.rotate(grant)
    assert store.load(env.app_id) is None


async def test_a_token_is_never_sent_to_another_origin(env):  # Attach rule
    await signed_in(env)
    with pytest.raises(MCPConfigError):
        await call(env, OAuthGrantAuth(env.app_id, MCP_URL), url="http://127.0.0.1:1/other")


async def test_sign_out_revokes_then_deletes(env):  # C-R6
    grant = await signed_in(env)
    result = await signout.sign_out(env.app_id, user_id=env.user_id)
    assert result == {"had_sign_in": True, "provider_told": True, "provider_url": None}
    assert env.fake.revoked == [grant.refresh_token] and store.load(env.app_id) is None
    assert env.audit()[-1] == "sign_out_revoked"


@pytest.mark.parametrize("mode", ["fail", "hang"])
async def test_a_revoke_failure_or_timeout_still_deletes(env, mode):  # C-R6, C-R9
    await signed_in(env)
    env.fake.revoke_mode = mode
    result = await signout.sign_out(env.app_id, user_id=env.user_id)
    assert result == {"had_sign_in": True, "provider_told": False, "provider_url": BASE}
    assert store.load(env.app_id) is None and env.audit()[-1] == "sign_out_local_only"


async def test_a_provider_without_revocation_is_said_so(env):  # C-R6
    env.fake.revoke = False
    await signed_in(env)
    result = await signout.sign_out(env.app_id, user_id=env.user_id)
    assert result["provider_told"] is False and result["provider_url"] == BASE and store.load(env.app_id) is None


async def test_signing_out_without_a_sign_in_is_harmless(env):  # C-R6
    assert (await signout.sign_out(env.app_id, user_id=env.user_id))["had_sign_in"] is False


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
    assert store.entry_name(env.app_id, env.user_id).startswith("mcp-oauth-")


async def test_the_sign_in_registration_stores_the_runtime_start_path(env):  # run_in_db path is real
    from infra.db_async import run_in_db

    await signed_in(env)
    mcp_oauth_secrets.forget(ACCESS.format(n=1))
    assert await run_in_db(store.register_stored) == 1
    assert ACCESS.format(n=1) in mcp_oauth_secrets.registered()
