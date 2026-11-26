"""Every grant-end cause journals the site, including an unattended deadline."""
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import infra.saved_login_fills as fills
import infra.saved_logins as saved_logins
from host_access import browser_grant_cleanup as cleanup, browser_grant_effects as effects, browser_grant_records as records
from tests.test_saved_login_fills import AGENT_THREAD, PERSON, world  # noqa: F401


@pytest.fixture(autouse=True)
def clear_memory():
    for state in (records._states, records._grants, records._generation, records._fences):
        state.clear()
    yield
    for state in (records._states, records._grants, records._generation, records._fences):
        state.clear()


def restart(monkeypatch):
    monkeypatch.setattr(records, "_states", {})
    monkeypatch.setattr(records, "_grants", {})


def approve(login, *, standing=False, seconds=60):
    request = fills.request_fill(AGENT_THREAD, login)
    return fills.approve_fill(request["id"], user_id=PERSON, revision=request["revision"],
                              application_hash=request["application_hash"],
                              until_revoked=standing, duration_seconds=None if standing else seconds)


def revoke(grant):
    fills.revoke_fill(grant["id"], user_id=PERSON, revision=grant["revision"],
                      application_hash=grant["application_hash"])


def pending_site():
    item = type("Owner", (), {"user_id": PERSON})()
    return records.pending(effects.profile(item))


@pytest.mark.parametrize("cause", ["revoke", "delete", "out_of_date", "standing"])
def test_end_causes_leave_pending_site(world, cause):
    grant = approve(world["login"], standing=cause == "standing")
    if cause in ("revoke", "standing"):
        revoke(grant)
    elif cause == "delete":
        saved_logins.delete_login(world["login"])
        fills.end_for_login(world["login"], deleted=True)
    else:
        saved_logins.mark_out_of_date(world["login"])
        fills.end_for_login(world["login"], deleted=False)
    assert "example.com" in pending_site()


@pytest.mark.asyncio
async def test_standing_approval_is_cleared_after_restart(world, monkeypatch):
    approve(world["login"], standing=True)
    profile = effects.profile(SimpleNamespace(user_id=PERSON))
    assert records._read(profile) == ["https://example.com"]
    restart(monkeypatch)
    clear = AsyncMock()
    monkeypatch.setattr(cleanup.browser_site_clear, "clear", clear)
    runtime = SimpleNamespace(record=SimpleNamespace(profile=profile), chrome=SimpleNamespace(endpoint="ws://test"))
    await cleanup.recover_launch(runtime)
    clear.assert_awaited_once_with("ws://test", "https://example.com")
    assert not records.pending(profile)


@pytest.mark.asyncio
async def test_failed_journal_write_leaves_standing_grant_active(world, monkeypatch):
    grant = approve(world["login"], standing=True)
    profile = effects.profile(SimpleNamespace(user_id=PERSON))
    def fail_write(*_args):
        raise OSError("write failed")
    with monkeypatch.context() as failure:
        failure.setattr(records, "_write", fail_write)
        with pytest.raises(OSError, match="write failed"):
            revoke(grant)
        assert grant["id"] in fills._approvals
        assert records.fenced(profile)
        assert pending_site() == {}
    restart(monkeypatch)
    clear = AsyncMock()
    monkeypatch.setattr(cleanup.browser_site_clear, "clear", clear)
    runtime = SimpleNamespace(record=SimpleNamespace(profile=profile), chrome=SimpleNamespace(endpoint="ws://test"))
    await cleanup.recover_launch(runtime)
    clear.assert_awaited_once_with("ws://test", "https://example.com")
    assert not records.pending(profile)


@pytest.mark.asyncio
async def test_failed_revocation_commit_leaves_journal_for_restart(world, monkeypatch):
    grant = approve(world["login"], standing=True)
    class FailingDelete(dict):
        def pop(self, key):
            raise RuntimeError("commit failed")
    monkeypatch.setattr(fills, "_approvals", FailingDelete(fills._approvals))
    submitted = []
    def hold_clear(coro):
        submitted.append(True)
        coro.close()
    monkeypatch.setattr(effects.app_loop, "submit", hold_clear)
    with pytest.raises(RuntimeError, match="commit failed"):
        revoke(grant)
    assert grant["id"] in fills._approvals
    assert not submitted
    assert "example.com" in pending_site()
    profile = effects.profile(SimpleNamespace(user_id=PERSON))
    clear = AsyncMock()
    monkeypatch.setattr(cleanup.browser_site_clear, "clear", clear)
    runtime = SimpleNamespace(record=SimpleNamespace(profile=profile), chrome=SimpleNamespace(endpoint="ws://test"))
    await cleanup.recover_launch(runtime)
    clear.assert_awaited_once_with("ws://test", "https://example.com")
    assert not records.pending(profile)


def test_deadline_ends_grant_without_read(world, monkeypatch):
    ended = threading.Event()
    original = effects.end
    def capture(item, commit, cause):
        original(item, commit, cause)
        ended.set()
    monkeypatch.setattr(effects, "end", capture)
    grant = approve(world["login"], seconds=1)
    assert ended.wait(4)
    assert grant["id"] not in fills._approvals
    assert "example.com" in pending_site()


def test_an_end_with_no_app_loop_defers_the_clear_to_the_next_launch(tmp_path, monkeypatch):
    target = str(tmp_path / "profile")
    monkeypatch.setattr(effects, "profile", lambda _item: target)
    monkeypatch.setattr(effects.app_loop, "_LOOP", None)
    released, events = [], []
    release = records.release
    monkeypatch.setattr(records, "release", lambda *args: (released.append(args), release(*args)))
    monkeypatch.setattr(effects, "emit_product_event", lambda *args, **kwargs: events.append((args, kwargs)))
    effects.end(SimpleNamespace(id="grant", site="https://login.example.com"), lambda: None, "expired")
    assert released == [(target, "example.com")]
    assert not records.fenced(target)
    assert records.pending(target) == {"example.com": (["https://login.example.com"], 1)}
    assert events == [(("browser_signout", "site_clear"), {"properties": {"outcome": "deferred", "cause": "expired"}})]
