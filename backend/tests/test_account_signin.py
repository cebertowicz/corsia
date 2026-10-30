"""Added accounts sign in, are checked and sign out only through the vendor's own commands.

Pins: a Default sign-in releases and re-reads only the Default while added accounts keep
their own state; discovery leaves a receipt behind; sign-in submits survive reload; and a
raw Turn receipt stays private across paging and a synthetic process restart. Every vendor
CLI here is a synthetic script - no real credential, login or Keychain is touched."""
from __future__ import annotations

import asyncio
import base64
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from connections import account_controls, account_login_wait, account_signin, harness_models
from connections.account_receipts import account_receipt, receipt_is_current
from connections.vendor_accounts import get_account, set_account_state
from test_claude_catalog_usage_ownership import (  # noqa: F401
    _report,
    _seed_old_account,
    live_app_loop,
)
from test_provider_sources import _listed, _ok, _stream, claude_session, client  # noqa: F401
from vendor_account_fixtures import (  # noqa: F401
    _accept,
    _known_default,
    _start,
    accounts,
    claude,
    default_id,
    isolated_shutdown_tasks,
    KEY,
    ready,
    receipt_of,
    seed_default_account,
    seed_usage,
)

REF = "signin-0123456789abcdef"


@pytest.fixture(autouse=True)
def fixed_failure_reference(monkeypatch):
    """A failed sign-in's reference is random; pin it so results compare whole."""
    monkeypatch.setattr(account_signin, "new_reference", lambda prefix: REF)


def test_a_failed_sign_in_logs_the_reference_its_row_shows(caplog):
    with caplog.at_level("WARNING", logger="connections.account_signin"):
        assert account_signin.failure_reference("codex", "vendor_login_failed") == REF
    assert f"codex sign-in failed reference={REF} cause=vendor_login_failed" in caplog.text


class ExitedLogin:
    returncode = 0

    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_eof()

    async def wait(self):
        return 0


_FILE_STORE = '-c cli_auth_credentials_store="file" '


def _unsigned(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "e30." + payload + "."


def _fingerprint(account, user):
    return hashlib.sha256(json.dumps([account, user], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


_CODEX = r"""#!/bin/sh
echo "$CODEX_HOME|$*" >> "$VENDOR_LOG"
case "$*" in
  *"login status"*)
    [ -n "$STATUS_EXIT" ] && exit "$STATUS_EXIT"
    if [ -f "$CODEX_HOME/signed" ]; then echo "Logged in using ChatGPT"; exit 0; fi
    echo "Not logged in"; exit 1;;
  *login*)
    echo "vendor output with a link nobody may see"
    echo "vendor error text nobody may see" >&2
    while [ -n "$LOGIN_HOLD" ] && [ ! -f "$LOGIN_HOLD" ]; do sleep 0.05; done
    [ -n "$LOGIN_FAIL" ] && exit 1
    if [ ! -f "$CODEX_HOME/auth.json" ] && [ -z "$NO_AUTH" ]; then
      printf '{"tokens":{"account_id":"%s","id_token":"__TOKEN__"}}' "$CODEX_HOME" > "$CODEX_HOME/auth.json"
    fi
    touch "$CODEX_HOME/signed"; exit 0;;
  *logout*)
    [ -n "$LOGOUT_FAIL" ] && exit 1
    rm -f "$CODEX_HOME/signed"; exit 0;;
esac
exit 3
""".replace("__TOKEN__", _unsigned({"https://api.openai.com/auth": {"chatgpt_user_id": "synthetic-user"}}))


# The signed-in and signed-out reports carry exactly the key sets the owner's
# proof recorded from Claude Code 2.1.287 (ACCOUNT-SIGNIN-FEASIBILITY.md, Observed).
_CLAUDE = r"""#!/bin/sh
echo "${CLAUDE_CONFIG_DIR:-unset}|$*" >> "$VENDOR_LOG"
home=${CLAUDE_CONFIG_DIR:-$TEST_CLAUDE_DEFAULT}
case "$*" in
  "auth status")
    [ -n "$STATUS_EXIT" ] && exit "$STATUS_EXIT"
    [ -n "$STATUS_GARBLED" ] && { echo "not json"; exit 0; }
    if [ -f "$home/signed" ]; then
      if [ -f "$home/identity.json" ]; then cat "$home/identity.json"; exit 0; fi
      identity=$(basename "$(dirname "$home")")
      echo '{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty","email":"person@example.test","orgId":"org-synthetic-1","orgName":"Example Team","subscriptionType":"team","analyticsDisabled":false,"configDirectory":"/x","projectsDirectory":"/x/projects"}' | sed "s/person@/$identity@/"
      exit 0
    fi
    echo '{"loggedIn":false,"authMethod":"none","apiProvider":"firstParty","analyticsDisabled":false,"configDirectory":"/x","projectsDirectory":"/x/projects"}'
    exit 1;;
  "auth login")
    echo "vendor output with a link nobody may see"
    echo "vendor error text nobody may see" >&2
    while [ -n "$LOGIN_HOLD" ] && [ ! -f "$LOGIN_HOLD" ]; do sleep 0.05; done
    [ -n "$LOGIN_FAIL" ] && exit 1
    touch "$CLAUDE_CONFIG_DIR/signed"
    while [ -n "$LOGIN_LINGER" ] && [ ! -f "$LOGIN_LINGER" ]; do sleep 0.05; done
    exit 0;;
  "auth logout")
    [ -n "$LOGOUT_FAIL" ] && exit 1
    rm -f "$CLAUDE_CONFIG_DIR/signed"; exit 0;;
esac
exit 3
"""


_SIGNED_IN_KEYS = {"analyticsDisabled", "apiProvider", "authMethod", "configDirectory", "email", "loggedIn",
                   "orgId", "orgName", "projectsDirectory", "subscriptionType"}


_SIGNED_OUT_KEYS = {"analyticsDisabled", "apiProvider", "authMethod", "configDirectory", "loggedIn",
                    "projectsDirectory"}


_ARGV = {
    "codex": {"login": _FILE_STORE + "login", "status": _FILE_STORE + "login status",
              "logout": _FILE_STORE + "logout"},
    "claude-code": {"login": "auth login", "status": "auth status", "logout": "auth logout"},
}


both = pytest.mark.parametrize("provider", ["codex", "claude-code"])


def _script(path: Path, body: str) -> str:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def scripted_vendor_clis(isolated_db, tmp_path, monkeypatch):
    from connections import account_controls
    from connections.models import upsert_connection
    from connections.vendor_accounts import add_account

    log = tmp_path / "vendor.log"
    log.touch()
    monkeypatch.setenv("VENDOR_LOG", str(log))
    upsert_connection(type="cli", provider="codex", status="available",
                      config={"binary": _script(tmp_path / "codex", _CODEX)})
    upsert_connection(type="cli", provider="claude-code", status="available",
                      config={"binary": _script(tmp_path / "claude", _CLAUDE)})
    default_codex, default_claude = tmp_path / "default-codex", tmp_path / "default-claude"
    default_codex.mkdir()
    default_claude.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(default_codex))
    monkeypatch.setenv("TEST_CLAUDE_DEFAULT", str(default_claude))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    account_controls._SIGNINS.clear()
    defaults = {provider: seed_default_account(provider) for provider in ("codex", "claude-code")}
    rows = {"log": log, "codex": add_account("codex", "Work"), "claude-code": add_account("claude-code", "Personal"),
            "default": defaults,
            "default_homes": {"codex": default_codex, "claude-code": default_claude}}
    _identity_file(rows["claude-code"]["source_home"], "claude-code", "person@example.test", "org-synthetic-1")
    yield rows
    account_controls._SIGNINS.clear()


def _identity_file(home, provider, account, user):
    home = Path(home)
    if provider == "codex":
        token = _unsigned({"https://api.openai.com/auth": {"chatgpt_user_id": user}})
        (home / "auth.json").write_text(json.dumps({"tokens": {"account_id": account, "id_token": token}}))
        return {"account_fp": _fingerprint(account, user)}
    report = json.loads(next(line[line.index("{"):line.rindex("}") + 1]
                             for line in _CLAUDE.splitlines() if "echo '{\"loggedIn\":true" in line))
    report.update(email=account, orgId=user)
    (home / "identity.json").write_text(json.dumps(report))
    return {"email": account, "org_id": user, "org_name": "Example Team", "plan": "team"}


def test_codex_identity_reads_email_from_the_existing_auth_file_reader(tmp_path):
    from connections.account_identity import codex_identity

    token = _unsigned({
        "https://api.openai.com/auth": {"chatgpt_user_id": "synthetic-user"},
        "email": "default@example.test",
    })
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"account_id": "synthetic-account", "id_token": token}}))

    assert codex_identity(tmp_path) == {
        "account_fp": _fingerprint("synthetic-account", "synthetic-user"),
        "email": "default@example.test",
    }


def _calls(log: Path) -> list[tuple[str, str]]:
    pairs = (line.partition("|") for line in log.read_text().splitlines())
    return [(home, args) for home, _, args in pairs]


async def _baseline_read(log: Path) -> None:
    """Wait until a held Claude Code login's status has been read once, before the person signs in."""
    async with asyncio.timeout(30):
        while "auth status" not in [args for _, args in _calls(log)]:
            await asyncio.sleep(0.01)


async def _started(log: Path, count: int = 1) -> None:
    """Wait until the fake vendor has recorded ``count`` commands; a spawn returns before the script runs."""
    async with asyncio.timeout(30):
        while len(_calls(log)) < count:
            await asyncio.sleep(0.01)


def _account(account_id):
    from connections.vendor_accounts import get_account

    return get_account(account_id)


async def _finished(account_id):
    from connections import account_controls

    await account_controls._SIGNINS[account_id].task
    return await account_controls.check_signin(account_id)


def _created(account_id, when):
    from infra.database import get_db

    connection = get_db()
    try:
        connection.execute("UPDATE vendor_accounts SET created_at=? WHERE id=?", (when, account_id))
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def sign_in_api_client(isolated_db):
    from api.vendor_accounts import router
    from connections import account_controls
    from connections.models import upsert_connection
    from infra.auth import require_user

    upsert_connection(type="cli", provider="codex", status="available", config={"binary": "/bin/sh"})
    upsert_connection(type="cli", provider="claude-code", status="available", config={"binary": "/bin/sh"})
    account_controls._SIGNINS.clear()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as api:
        app.dependency_overrides[require_user] = lambda: {"id": "user"}
        yield api


async def _finished_or_gone(account_id):
    from connections import account_controls

    operation = account_controls._SIGNINS.get(account_id)
    if operation is not None and operation.task is not None:
        await operation.task


def _seed_added_accounts(provider="claude-code"):
    """Two added accounts, each limited and each holding one thread; returns what must not change."""
    from connections import harness_usage
    from connections.vendor_accounts import add_account, set_account_state
    from infra.database import write_transaction

    reset = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    accounts = [add_account(provider, name) for name in ("X", "Y")]
    # Usage belongs to a login, so each added account is signed in before it is limited.
    accounts = [set_account_state(account["id"], "signed_in", expected_revision=account["revision"]) for account in accounts]
    with write_transaction(None, scope="test_seed_added_accounts") as db:
        for account in accounts:
            harness_usage.record_usage(provider=provider, receipt=receipt_of(provider, account["id"], db=db), windows=[{
                "vendor": provider, "kind": "five_hour" if provider == "claude-code" else "codex_primary",
                "used_percent": 100, "resets_at": reset, "source": "discovery",
            }], verdict={"state": "limited", "reason": f"{account['id']}_limit", "resets_at": reset}, db=db)
            thread = f"thread-{account['id']}"
            db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES (?,'u',?,'t0','t0')",
                       (thread, f"{provider}/sonnet"))
            db.execute("INSERT INTO thread_vendor_account_holds VALUES (?,?,?,'account_runtime_pending','t0')",
                       (thread, provider, account["id"]))
    return [account["id"] for account in accounts]


def _added_state(accounts):
    """Every stored byte of the added accounts and every account hold."""
    from infra.database import get_db

    db = get_db()
    try:
        rows = [tuple(db.execute("SELECT * FROM vendor_accounts WHERE id=?", (account,)).fetchone())
                for account in accounts]
        holds = [tuple(row) for row in db.execute("SELECT * FROM thread_vendor_account_holds ORDER BY thread_id")]
    finally:
        db.close()
    assert len(holds) == len(accounts)
    return rows, holds


def _observe(monkeypatch):
    """Record the after-commit effects a sign-in can start."""
    from connections import harness_usage_check

    releases, stops = [], []
    monkeypatch.setattr("infra.connection_limit_drive.schedule_scope_release", releases.append)
    monkeypatch.setattr("infra.connection_limit_release.apply_plan_limit_effect",
                        lambda provider, scopes, **_kw: stops.append((provider, [scope for scope, _ in scopes])))
    scheduled = Mock()
    monkeypatch.setattr(harness_usage_check, "schedule_account_check", scheduled)
    return releases, stops, scheduled


def _by_status(provider, *, schedule_usage_check):
    from connections.models import set_connection_status

    return set_connection_status(provider, "available", schedule_usage_check=schedule_usage_check)


def _by_source_check(provider, *, schedule_usage_check):
    from connections.models import record_source_check

    return record_source_check(provider, status="available", check={"state": "ready"},
                               schedule_usage_check=schedule_usage_check)


def _by_apply_check(provider, *, schedule_usage_check):
    from connections import source_state

    return source_state.apply_check(provider, source_state.READY, schedule_usage_check=schedule_usage_check)


@pytest.mark.asyncio
async def test_completion_record_precedes_discovery_and_failure_preserves_it(accounts, monkeypatch):
    db, _, row, _ = accounts
    row = set_account_state(row['id'], 'signed_out', expected_revision=row['revision'], db=db)
    operation = account_controls.SignIn(row['id'], 'codex', row['revision'], process=ExitedLogin())
    async def status(*args):
        return True, {'account_fp': 'synthetic-completed-login'}, None
    async def discovery(provider, **kwargs):
        assert operation.public() == {'account_id': row['id'], 'route': 'browser',
            'status': 'completed', 'cause': None, 'duplicate_of': None}
        assert kwargs['account_id'] == row['id']
        assert receipt_is_current(kwargs['expected_receipt'], db=db)
        assert kwargs['expected_receipt'] == account_receipt(get_account(row['id'], db=db))
        raise RuntimeError('synthetic discovery failure')
    monkeypatch.setattr(account_controls, '_vendor_status', status)
    monkeypatch.setattr(harness_models, 'refresh_provider', discovery)
    await account_controls._hold(operation, row['source_home'], '/synthetic/codex')
    assert operation.status == 'completed' and operation.cause is None
    assert get_account(row['id'], db=db)['state'] == 'signed_in'


@pytest.mark.asyncio
async def test_finished_login_reports_failure_when_outcome_cannot_be_recorded(accounts, monkeypatch):
    db, _, row, _ = accounts
    row = set_account_state(row['id'], 'signed_out', expected_revision=row['revision'], db=db)
    operation = account_controls.SignIn(row['id'], 'codex', row['revision'], process=ExitedLogin(),
                                        bind=account_controls.FirstRunBind('thread', 'user'))
    async def status(*args):
        return True, {'account_fp': 'synthetic-completed-login'}, None
    async def finish(*args):
        assert operation.status == 'completed' and operation.bind.outcome is None
        raise RuntimeError('outcome unavailable')
    monkeypatch.setattr(account_controls, '_vendor_status', status)
    monkeypatch.setattr(account_controls, 'finish_signin', finish)
    await account_controls._hold(operation, row['source_home'], '/synthetic/codex')
    assert operation.status == 'failed' and operation.cause == 'signin_check_failed'
    assert operation.ref is not None
    assert get_account(row['id'], db=db)['state'] == 'signed_in'


@pytest.mark.asyncio
async def test_verify_started_on_old_login_cannot_discover_the_replacement(accounts, monkeypatch):
    db, _, row, _ = accounts
    entered, release = asyncio.Event(), asyncio.Event()
    old = account_receipt(row)
    async def status(*args):
        entered.set()
        await release.wait()
        return True, {}, None
    async def binary(provider):
        return '/synthetic/codex'
    async def forbidden_adapter(*args, **kwargs):
        pytest.fail('stale verify may not start a replacement login discovery')
    monkeypatch.setattr(account_controls, '_vendor_status', status)
    monkeypatch.setattr(account_controls, '_binary', binary)
    monkeypatch.setitem(harness_models._ADAPTERS, 'codex', (forbidden_adapter, None, 'synthetic'))
    task = asyncio.create_task(account_controls.verify(row['id']))
    await entered.wait()
    replacement = set_account_state(row['id'], 'signed_in', expected_revision=row['revision'], db=db)
    before = replacement['discovery']
    assert old != account_receipt(replacement)
    release.set()
    assert await task == {'account_id': row['id'], 'vendor_signed_in': True, 'cause': None, 'state': 'signed_in'}
    assert get_account(row['id'], db=db)['discovery'] == before


@pytest.mark.asyncio
@pytest.mark.parametrize('condition', ['signed_out', 'unknown', 'removed', 'c6'])
async def test_verify_never_starts_discovery_for_noneligible_auth(accounts, monkeypatch, condition):
    from connections.account_evidence import discovery_record, store_record
    from connections.vendor_accounts import AccountError

    db, _, row, _ = accounts
    if condition in ('signed_out', 'removed'):
        row = set_account_state(row['id'], condition, expected_revision=row['revision'], db=db)
    if condition == 'c6':
        receipt = account_receipt(row)
        record = discovery_record(row)
        record['auth_projection']['failed'] = {'cause': 'codex_auth_projection_changed',
            'account_revision': receipt.account_revision, 'login_seq': receipt.login_seq}
        store_record(row['id'], record, db=db)
        db.commit()
    async def binary(provider):
        return '/synthetic/codex'
    async def status(*args):
        return (None if condition == 'unknown' else True), {}, None
    async def forbidden(*args, **kwargs):
        pytest.fail('ineligible verify started discovery')
    monkeypatch.setattr(account_controls, '_binary', binary)
    monkeypatch.setattr(account_controls, '_vendor_status', status)
    monkeypatch.setattr(harness_models, 'refresh_provider', forbidden)
    if condition == 'removed':
        with pytest.raises(AccountError, match='removed'):
            await account_controls.verify(row['id'])
    else:
        result = await account_controls.verify(row['id'])
        assert set(result) == {'account_id', 'vendor_signed_in', 'cause', 'state'}


def test_fake_claude_reports_carry_the_recorded_real_key_sets():
    signed_in, signed_out = (json.loads(line[line.index("{"):line.rindex("}") + 1])
                             for line in _CLAUDE.splitlines() if "echo '{" in line)
    assert set(signed_in) == _SIGNED_IN_KEYS
    assert set(signed_out) == _SIGNED_OUT_KEYS


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_login_status_and_logout_run_the_exact_vendor_commands_in_the_stored_home(
        scripted_vendor_clis, provider, monkeypatch):
    from connections import account_controls

    account = scripted_vendor_clis[provider]
    spawned = []
    real_spawn = asyncio.create_subprocess_exec

    async def recording(*argv, **kwargs):
        spawned.append((argv, kwargs))
        return await real_spawn(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)
    variable, other = (("CODEX_HOME", "CLAUDE_CONFIG_DIR") if provider == "codex"
                       else ("CLAUDE_CONFIG_DIR", "CODEX_HOME"))
    monkeypatch.delenv(other, raising=False)
    started = await account_controls.start_signin(account["id"])
    assert started == {"account_id": account["id"], "route": "browser", "status": "waiting", "cause": None}
    assert (await _finished(account["id"]))["status"] == "completed"
    assert _account(account["id"])["state"] == "signed_in"
    _, cleanup = await account_controls.sign_out(account["id"], expected_revision=1)
    assert (cleanup["status"], cleanup["cause"]) == ("done", None)
    expected = [(account["source_home"], _ARGV[provider][action]) for action in ("login", "status", "logout")]
    expected.insert(-1, (account["source_home"], "--version"))
    assert _calls(scripted_vendor_clis["log"]) == expected
    assert len(spawned) == len(expected)
    for argv, kwargs in spawned:
        assert kwargs["env"][variable] == account["source_home"]
        assert other not in kwargs["env"]
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
        assert kwargs["stdout"] == asyncio.subprocess.PIPE
    # Login merges standard error into the discarded pipe, as observed; status and logout drop it.
    assert [kwargs["stderr"] for _, kwargs in spawned] == [
        asyncio.subprocess.STDOUT, asyncio.subprocess.DEVNULL,
        asyncio.subprocess.PIPE, asyncio.subprocess.DEVNULL]
    assert [list(argv[1:]) for argv, _ in spawned] == (
        [["-c", 'cli_auth_credentials_store="file"', "login"],
         ["-c", 'cli_auth_credentials_store="file"', "login", "status"], ["--version"],
         ["-c", 'cli_auth_credentials_store="file"', "logout"]]
        if provider == "codex" else [["auth", "login"], ["auth", "status"],
                                     ["--version"], ["auth", "logout"]])


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
async def test_claude_status_parsing_signed_in_and_signed_out(scripted_vendor_clis):
    from connections import account_controls

    claude = scripted_vendor_clis["claude-code"]
    binary = await account_controls._binary("claude-code")
    assert await account_controls._vendor_status("claude-code", claude["source_home"], binary) == (False, {}, None)
    Path(claude["source_home"], "signed").touch()
    assert await account_controls._vendor_status("claude-code", claude["source_home"], binary) == (
        True, {"email": "person@example.test", "org_name": "Example Team", "org_id": "org-synthetic-1",
               "plan": "team"}, None)


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
async def test_claude_signin_stores_identity_from_status_and_never_an_account_id(scripted_vendor_clis):
    from api.vendor_accounts import _safe
    from connections import account_controls

    claude = scripted_vendor_clis["claude-code"]
    await account_controls.start_signin(claude["id"])
    assert (await _finished(claude["id"]))["status"] == "completed"
    stored = _account(claude["id"])
    assert stored["identity"] == {"email": "person@example.test", "org_name": "Example Team",
                                  "org_id": "org-synthetic-1", "plan": "team"}
    assert _safe(stored)["identity"] == stored["identity"]
    assert (stored["state"], stored["revision"]) == ("signed_in", 1)


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_signin_starts_when_the_source_row_reads_needs_auth(scripted_vendor_clis, provider):
    """A fresh install's CLI is signed out, so its row reads needs_auth: that is when Sign in must work."""
    from connections import account_controls
    from connections.models import get_connection
    from infra.database import write_transaction

    with write_transaction(None, scope="test") as conn:
        conn.execute("UPDATE connections SET status = 'needs_auth' WHERE provider = ?", (provider,))
    assert get_connection(provider)["status"] == "needs_auth"
    account = scripted_vendor_clis[provider]
    started = await account_controls.start_signin(account["id"])
    assert started["status"] != "unavailable"
    assert (await _finished(account["id"]))["status"] == "completed"


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
async def test_codex_default_stays_signed_out_after_another_signin(scripted_vendor_clis):
    from api.vendor_accounts import list_vendor_accounts

    default_id = scripted_vendor_clis['default']['codex']['id']
    account = scripted_vendor_clis['codex']
    await account_controls.start_signin(account['id'])
    assert (await _finished(account['id']))['status'] == 'completed'
    _identity_file(scripted_vendor_clis['default_homes']['codex'], 'codex', 'default', 'user')
    rows = (await list_vendor_accounts(provider='codex'))['accounts']
    default = next(row for row in rows if row['id'] == default_id)
    assert default['state'] == 'signed_out'
    assert 'default_signed_out' not in default


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
async def test_codex_signin_stores_only_the_private_identity_fingerprint(scripted_vendor_clis):
    from connections import account_controls

    codex = scripted_vendor_clis["codex"]
    await account_controls.start_signin(codex["id"])
    assert (await _finished(codex["id"]))["status"] == "completed"
    assert (_account(codex["id"])["state"], _account(codex["id"])["identity"]) == (
        "signed_in", {"account_fp": _fingerprint(codex["source_home"], "synthetic-user")})


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
@pytest.mark.parametrize("code,expected", [("2", "vendor_status_failed"), ("1", "vendor_not_signed_in")])
async def test_only_the_status_exit_decides_signed_in(scripted_vendor_clis, provider, monkeypatch, code, expected):
    from connections import account_controls

    account = scripted_vendor_clis[provider]
    monkeypatch.setenv("STATUS_EXIT", code)
    await account_controls.start_signin(account["id"])
    assert await _finished(account["id"]) == {"account_id": account["id"], "route": "browser",
                                              "status": "failed", "cause": expected, "ref": REF}
    assert (_account(account["id"])["state"], _account(account["id"])["identity"]) == ("signed_out", {})


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
async def test_unreadable_claude_status_is_unknown_not_signed_in(scripted_vendor_clis, monkeypatch):
    from connections import account_controls

    claude = scripted_vendor_clis["claude-code"]
    monkeypatch.setenv("STATUS_GARBLED", "1")
    await account_controls.start_signin(claude["id"])
    assert (await _finished(claude["id"]))["cause"] == "vendor_status_unreadable"
    assert _account(claude["id"])["state"] == "signed_out"


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_failed_vendor_login_never_reads_status(scripted_vendor_clis, provider, monkeypatch):
    from connections import account_controls

    account = scripted_vendor_clis[provider]
    monkeypatch.setenv("LOGIN_FAIL", "1")
    await account_controls.start_signin(account["id"])
    assert (await _finished(account["id"]))["cause"] == "vendor_login_failed"
    assert [args for _, args in _calls(scripted_vendor_clis["log"])] == [_ARGV[provider]["login"]]
    assert _account(account["id"])["state"] == "signed_out"


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_held_login_waits_past_the_command_bound_and_shows_no_vendor_output(scripted_vendor_clis, provider, monkeypatch, tmp_path):
    from connections import account_controls

    account = scripted_vendor_clis[provider]
    release = tmp_path / "release"
    monkeypatch.setenv("LOGIN_HOLD", str(release))
    # A held login that used the short command bound would fail here.
    monkeypatch.setattr(account_controls, "_COMMAND_SECONDS", 0.2)
    started = await account_controls.start_signin(account["id"])
    await _started(scripted_vendor_clis["log"])
    await asyncio.sleep(0.6)
    held = await account_controls.check_signin(account["id"])
    assert held == started == {"account_id": account["id"], "route": "browser", "status": "waiting", "cause": None}
    assert await account_controls.start_signin(account["id"]) == started  # one live sign-in per account
    assert [args for _, args in _calls(scripted_vendor_clis["log"])] == [_ARGV[provider]["login"]]
    monkeypatch.setattr(account_controls, "_COMMAND_SECONDS", 30.0)
    release.touch()
    assert (await _finished(account["id"]))["status"] == "completed"
    assert "nobody may see" not in json.dumps(held)


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_a_login_the_browser_never_returns_to_ends_failed_timed_out_and_the_vendor_is_killed(scripted_vendor_clis, provider, monkeypatch, tmp_path):
    from connections import account_controls

    account = scripted_vendor_clis[provider]
    monkeypatch.setenv("LOGIN_HOLD", str(tmp_path / "never"))
    monkeypatch.setattr(account_controls, "_SIGNIN_WAIT_SECONDS", 0.3)
    await account_controls.start_signin(account["id"])
    await _started(scripted_vendor_clis["log"])
    process = account_controls._SIGNINS[account["id"]].process
    ended = await _finished(account["id"])
    assert ended == {"account_id": account["id"], "route": "browser", "status": "failed",
                     "cause": "signin_timed_out", "ref": REF}
    assert process.returncode is not None
    # The status command never ran, the account stays signed out, and a new sign-in can start.
    assert [args for _, args in _calls(scripted_vendor_clis["log"])] == [_ARGV[provider]["login"]]
    assert _account(account["id"])["state"] == "signed_out"
    monkeypatch.delenv("LOGIN_HOLD")
    await account_controls.start_signin(account["id"])
    assert (await _finished(account["id"]))["status"] == "completed"


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
async def test_a_claude_login_that_stored_its_credentials_but_never_exits_completes_the_sign_in(scripted_vendor_clis, monkeypatch, tmp_path):
    """The person signed in and the vendor's status says so; a lingering login must not keep the row on Sign in."""
    from connections import account_controls

    account = scripted_vendor_clis["claude-code"]
    hold = tmp_path / "hold"
    monkeypatch.setenv("LOGIN_HOLD", str(hold))
    monkeypatch.setenv("LOGIN_LINGER", str(tmp_path / "never"))
    monkeypatch.setattr(account_login_wait, "STATUS_POLL_SECONDS", 0.05)
    monkeypatch.setattr(account_login_wait, "LINGER_SECONDS", 0.2)
    await account_controls.start_signin(account["id"])
    await _started(scripted_vendor_clis["log"])
    process = account_controls._SIGNINS[account["id"]].process
    await _baseline_read(scripted_vendor_clis["log"])
    hold.touch()  # the person signs in after the baseline read
    finished = await asyncio.wait_for(_finished(account["id"]), 10)
    assert finished["status"] == "completed" and finished["cause"] is None
    assert _account(account["id"])["state"] == "signed_in"
    assert process.returncode is not None  # the lingering login was stopped, not left running


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
async def test_a_claude_login_that_exits_inside_the_linger_window_is_not_killed(scripted_vendor_clis, monkeypatch, tmp_path):
    from connections import account_controls

    account = scripted_vendor_clis["claude-code"]
    hold, release = tmp_path / "hold", tmp_path / "release"
    monkeypatch.setenv("LOGIN_HOLD", str(hold))
    monkeypatch.setenv("LOGIN_LINGER", str(release))
    monkeypatch.setattr(account_login_wait, "STATUS_POLL_SECONDS", 0.05)
    monkeypatch.setattr(account_login_wait, "LINGER_SECONDS", 10.0)
    await account_controls.start_signin(account["id"])
    await _started(scripted_vendor_clis["log"])
    process = account_controls._SIGNINS[account["id"]].process
    await _baseline_read(scripted_vendor_clis["log"])
    hold.touch()
    while [args for _, args in _calls(scripted_vendor_clis["log"])].count("auth status") < 3:
        await asyncio.sleep(0.01)
    release.touch()
    finished = await asyncio.wait_for(_finished(account["id"]), 10)
    assert finished["status"] == "completed"
    assert process.returncode == 0


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
async def test_credentials_already_in_the_home_never_end_a_new_login_early(scripted_vendor_clis, monkeypatch, tmp_path):
    """A signed-out account can still hold credentials (a failed logout): only this login's own exit ends it."""
    from connections import account_controls

    account = scripted_vendor_clis["claude-code"]
    (Path(account["source_home"]) / "signed").touch()
    monkeypatch.setenv("LOGIN_HOLD", str(tmp_path / "never"))
    monkeypatch.setattr(account_controls, "_SIGNIN_WAIT_SECONDS", 0.6)
    monkeypatch.setattr(account_login_wait, "STATUS_POLL_SECONDS", 0.05)
    await account_controls.start_signin(account["id"])
    await _started(scripted_vendor_clis["log"])
    ended = await _finished(account["id"])
    assert ended["status"] == "failed" and ended["cause"] == "signin_timed_out"
    assert _account(account["id"])["state"] == "signed_out"
    # The one status read is the baseline; the old credentials are never polled again.
    assert [args for _, args in _calls(scripted_vendor_clis["log"])].count("auth status") == 1


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_a_held_login_whose_status_never_reads_signed_in_still_times_out(scripted_vendor_clis, provider, monkeypatch, tmp_path):
    from connections import account_controls

    account = scripted_vendor_clis[provider]
    monkeypatch.setenv("LOGIN_HOLD", str(tmp_path / "never"))
    monkeypatch.setattr(account_controls, "_SIGNIN_WAIT_SECONDS", 0.6)
    monkeypatch.setattr(account_login_wait, "STATUS_POLL_SECONDS", 0.05)
    await account_controls.start_signin(account["id"])
    await _started(scripted_vendor_clis["log"])
    ended = await _finished(account["id"])
    assert ended["status"] == "failed" and ended["cause"] == "signin_timed_out"
    asked = [args for _, args in _calls(scripted_vendor_clis["log"])]
    # Claude Code's status is read while it waits; Codex's login alone decides, so its status is never read.
    assert (_ARGV[provider]["status"] in asked) == (provider == "claude-code")
    assert _account(account["id"])["state"] == "signed_out"


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_login_that_writes_to_standard_error_completes_and_shows_none_of_it(scripted_vendor_clis, provider, caplog, capfd):
    from connections import account_controls

    account = scripted_vendor_clis[provider]
    with caplog.at_level("DEBUG"):
        started = await account_controls.start_signin(account["id"])
        finished = await _finished(account["id"])
    assert finished == {"account_id": account["id"], "route": "browser", "status": "completed", "cause": None,
                        "duplicate_of": None}
    assert _account(account["id"])["state"] == "signed_in"
    captured = capfd.readouterr()
    shown = json.dumps([started, finished]) + caplog.text + captured.out + captured.err
    assert "nobody may see" not in shown


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_backend_shutdown_stops_a_held_login_and_drops_its_record(scripted_vendor_clis, provider, monkeypatch, tmp_path, isolated_shutdown_tasks):
    import logging

    from app_shutdown_owned_tasks import shutdown_owned_tasks
    from connections import account_controls

    account = scripted_vendor_clis[provider]
    monkeypatch.setenv("LOGIN_HOLD", str(tmp_path / "never"))
    await account_controls.start_signin(account["id"])
    await _started(scripted_vendor_clis["log"])
    operation = account_controls._SIGNINS[account["id"]]
    assert operation.process.returncode is None
    assert await shutdown_owned_tasks([None] * 5, logging.getLogger("shutdown")) is None
    assert operation.process.returncode is not None and operation.task.done()
    assert (operation.status, account_controls._SIGNINS) == ("cancelled", {})
    assert _account(account["id"])["state"] == "signed_out"
    assert [args for _, args in _calls(scripted_vendor_clis["log"])] == [_ARGV[provider]["login"]]


def test_lifespan_shutdown_reaches_the_owned_task_step_that_stops_sign_ins():
    import inspect

    import app_lifecycle
    import app_shutdown_owned_tasks

    assert app_lifecycle._shutdown_owned_tasks is app_shutdown_owned_tasks.shutdown_owned_tasks
    shutdown = inspect.getsource(app_lifecycle._shutdown_runtime_services)
    assert "await _shutdown_owned_tasks(startup_tasks, _log_shutdown)" in shutdown
    assert "_shutdown_runtime_services(startup_tasks)" in inspect.getsource(app_lifecycle.lifespan)


@pytest.mark.asyncio
async def test_a_failing_sign_in_stop_is_logged_by_name_and_does_not_stop_shutdown(monkeypatch, isolated_shutdown_tasks):
    from unittest.mock import Mock

    from app_shutdown_owned_tasks import shutdown_owned_tasks
    from connections import account_controls
    from infra import state

    async def broken():
        raise RuntimeError("will not stop")

    async def forever():
        await asyncio.Event().wait()

    monkeypatch.setattr(account_controls, "stop_all_signins", broken)
    refresh = asyncio.create_task(forever())
    monkeypatch.setattr(state, "_github_ownership_login_task", refresh, raising=False)
    logger = Mock()
    assert await shutdown_owned_tasks([None] * 5, logger) is None
    assert refresh.cancelled()
    assert [call.args[0] for call in logger.warning.call_args_list] == [
        "shutdown: account sign-in cleanup failed: will not stop"]


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
async def test_second_codex_signin_is_refused_while_the_first_is_held(scripted_vendor_clis, monkeypatch, tmp_path):
    from connections import account_controls
    from connections.vendor_accounts import add_account

    first, second = scripted_vendor_clis["codex"], add_account("codex", "Other")
    release = tmp_path / "release"
    monkeypatch.setenv("LOGIN_HOLD", str(release))
    assert (await account_controls.start_signin(first["id"]))["status"] == "waiting"
    refused = await account_controls.start_signin(second["id"])
    assert refused == {"account_id": second["id"], "route": "browser", "status": "refused",
                       "cause": "codex_signin_in_progress", "held_by": first["id"]}
    assert second["id"] not in account_controls._SIGNINS
    await _started(scripted_vendor_clis["log"])
    assert _calls(scripted_vendor_clis["log"]) == [(first["source_home"], _ARGV["codex"]["login"])]
    # A held Codex sign-in does not block Claude, and a finished one no longer blocks Codex.
    assert (await account_controls.start_signin(scripted_vendor_clis["claude-code"]["id"]))["status"] == "waiting"
    release.touch()
    assert (await _finished(first["id"]))["status"] == "completed"
    assert (await account_controls.start_signin(second["id"]))["status"] == "waiting"
    assert (await _finished(second["id"]))["status"] == "completed"
    assert (await _finished(scripted_vendor_clis["claude-code"]["id"]))["status"] == "completed"


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
async def test_concurrent_starts_for_one_account_spawn_one_login(scripted_vendor_clis, monkeypatch, tmp_path):
    from connections import account_controls

    codex = scripted_vendor_clis["codex"]
    release = tmp_path / "release"
    monkeypatch.setenv("LOGIN_HOLD", str(release))
    results = await asyncio.gather(*(account_controls.start_signin(codex["id"]) for _ in range(4)))
    assert {result["status"] for result in results} == {"waiting"}
    release.touch()
    await _finished(codex["id"])
    assert [args for _, args in _calls(scripted_vendor_clis["log"])].count(_ARGV["codex"]["login"]) == 1


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
@pytest.mark.parametrize('previous_login', [False, True])
async def test_cancel_kills_the_held_login_and_removes_only_new_account(scripted_vendor_clis, provider, monkeypatch, tmp_path, previous_login):
    from connections import account_controls

    account = scripted_vendor_clis[provider]
    if previous_login:
        account = set_account_state(account['id'], 'signed_in', expected_revision=account['revision'])
        account = set_account_state(account['id'], 'signed_out', expected_revision=account['revision'])
    monkeypatch.setenv("LOGIN_HOLD", str(tmp_path / "never"))
    await account_controls.start_signin(account["id"])
    await _started(scripted_vendor_clis["log"])
    process = account_controls._SIGNINS[account["id"]].process
    cancelled = await account_controls.cancel_signin(account["id"])
    assert cancelled == {"account_id": account["id"], "route": "browser", "status": "cancelled", "cause": None}
    assert process.returncode is not None and process.returncode != 0
    assert account["id"] not in account_controls._SIGNINS
    assert _account(account["id"])["state"] == ("signed_out" if previous_login else "removed")
    expected = [_ARGV[provider]["login"]] + ([] if previous_login else [_ARGV[provider]["logout"]])
    assert [args for _, args in _calls(scripted_vendor_clis["log"])] == expected
    if previous_login:
        monkeypatch.delenv("LOGIN_HOLD")
        await account_controls.start_signin(account["id"])
        assert (await _finished(account["id"]))["status"] == "completed"


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
async def test_finished_sign_in_is_kept_until_the_next_start_sign_out_or_remove(scripted_vendor_clis, monkeypatch):
    from connections import account_controls
    from connections.vendor_accounts import AccountError

    codex = scripted_vendor_clis["codex"]
    monkeypatch.setenv("LOGIN_FAIL", "1")
    await account_controls.start_signin(codex["id"])
    failed = await _finished(codex["id"])
    assert failed == {"account_id": codex["id"], "route": "browser", "status": "failed", "cause": "vendor_login_failed",
                      "ref": REF}
    kept = account_controls._SIGNINS[codex["id"]]
    assert [await account_controls.check_signin(codex["id"]) for _ in range(2)] == [failed, failed]
    assert account_controls._SIGNINS == {codex["id"]: kept}
    # The next start replaces the finished record: still one per account.
    monkeypatch.delenv("LOGIN_FAIL")
    await account_controls.start_signin(codex["id"])
    completed = await _finished(codex["id"])
    assert completed["status"] == "completed" and account_controls._SIGNINS[codex["id"]] is not kept
    assert [await account_controls.check_signin(codex["id"]) for _ in range(2)] == [completed, completed]
    assert list(account_controls._SIGNINS) == [codex["id"]]
    await account_controls.sign_out(codex["id"], expected_revision=1)
    assert account_controls._SIGNINS == {}
    with pytest.raises(AccountError, match="no sign-in is in progress"):
        await account_controls.check_signin(codex["id"])


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_second_sign_in_for_a_vendor_is_refused_while_one_is_held(scripted_vendor_clis, provider, monkeypatch, tmp_path):
    from connections import account_controls
    from connections.vendor_accounts import add_account

    first, second = scripted_vendor_clis[provider], add_account(provider, "Other")
    other = scripted_vendor_clis["claude-code" if provider == "codex" else "codex"]
    release = tmp_path / "release"
    monkeypatch.setenv("LOGIN_HOLD", str(release))
    started = await account_controls.start_signin(first["id"])
    assert started["status"] == "waiting"
    # A repeat start for the same account returns its live record.
    assert await account_controls.start_signin(first["id"]) == started
    cause = {"codex": "codex_signin_in_progress", "claude-code": "claude_signin_in_progress"}[provider]
    assert await account_controls.start_signin(second["id"]) == {
        "account_id": second["id"], "route": "browser", "status": "refused", "cause": cause,
        "held_by": first["id"]}
    assert second["id"] not in account_controls._SIGNINS
    # The other vendor is not blocked, and a finished sign-in no longer blocks its own.
    assert (await account_controls.start_signin(other["id"]))["status"] == "waiting"
    await _started(scripted_vendor_clis["log"], 2)
    assert sorted(_calls(scripted_vendor_clis["log"])) == sorted([
        (first["source_home"], _ARGV[provider]["login"]), (other["source_home"], _ARGV[other["provider"]]["login"])])
    release.touch()
    assert (await _finished(first["id"]))["status"] == "completed"
    assert (await account_controls.start_signin(second["id"]))["status"] == "waiting"
    assert (await _finished(second["id"]))["status"] == "completed"
    assert (await _finished(other["id"]))["status"] == "completed"


def _checked_account(provider, account_id):
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt
    from infra.database import write_transaction

    with write_transaction(None, scope='test_checked_account') as db:
        receipt = capture_receipt(provider, account_id=account_id, db=db)
        assert mark_discovery(account_id, receipt=receipt, status='ready', models={}, db=db)


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
async def test_a_repeated_account_is_removed_and_names_the_oldest_signed_in_match(scripted_vendor_clis):
    from api import vendor_accounts as routes
    from connections import account_controls
    from connections.vendor_accounts import add_account, record_evidence, set_account_state

    first, second = scripted_vendor_clis["claude-code"], add_account("claude-code", "Again")
    identity = _identity_file(second["source_home"], "claude-code", "person@example.test", "org-synthetic-1")
    await account_controls.start_signin(first["id"])
    assert (await _finished(first["id"]))["duplicate_of"] is None
    # A historical second signed-in match makes the oldest-match rule observable.
    third = add_account("claude-code", "Third")
    record_evidence(third["id"], identity=identity)
    set_account_state(third["id"], "signed_in", expected_revision=0)
    # The oldest match has already been checked.
    from infra.db_async import run_in_db
    await run_in_db(_checked_account, 'claude-code', third['id'])
    _created(third["id"], "2026-01-01T00:00:00+00:00")
    await account_controls.start_signin(second["id"])
    assert await _finished(second["id"]) == {
        "account_id": second["id"], "route": "browser", "status": "failed", "cause": "same_account",
        "duplicate_of": third["id"], "ref": REF}
    assert (_account(second["id"])["state"], _account(second["id"])["revision"]) == ("removed", 1)
    assert _account(second["id"])["identity"] == {} and _account(second["id"])["auth_cleanup"] is not None
    assert {row["id"]: row["duplicate_of"] for row in
            (await routes.list_vendor_accounts(provider="claude-code"))["accounts"]} == {
                scripted_vendor_clis["default"]["claude-code"]["id"]: None, first["id"]: third["id"],
                third["id"]: first["id"]}
    all_rows = (await routes.list_vendor_accounts(provider="claude-code", include_removed=True))["accounts"]
    assert next(row for row in all_rows if row["id"] == second["id"])["state"] == "removed"


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
async def test_codex_repeated_account_is_removed_by_its_fingerprint(scripted_vendor_clis):
    from api import vendor_accounts as routes
    from connections import account_controls
    from connections.vendor_accounts import add_account

    first, second = scripted_vendor_clis["codex"], add_account("codex", "Again")
    for row in (first, second):
        _identity_file(row["source_home"], "codex", "workspace-private", "user-private")
    await account_controls.start_signin(first["id"])
    assert (await _finished(first["id"]))["duplicate_of"] is None
    # The match has already been checked.
    from infra.db_async import run_in_db
    await run_in_db(_checked_account, 'codex', first['id'])
    await account_controls.start_signin(second["id"])
    assert await _finished(second["id"]) == {
        "account_id": second["id"], "route": "browser", "status": "failed", "cause": "same_account",
        "duplicate_of": first["id"], "ref": REF}
    rows = (await routes.list_vendor_accounts(provider="codex"))["accounts"]
    assert [row["duplicate_of"] for row in rows] == [None, None]
    assert _account(second["id"])["state"] == "removed"
    assert _account(second["id"])["identity"] == {} and _account(second["id"])["auth_cleanup"] is not None
    all_rows = (await routes.list_vendor_accounts(provider="codex", include_removed=True))["accounts"]
    assert next(row for row in all_rows if row["id"] == second["id"])["state"] == "removed"


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_cancel_after_the_vendor_login_succeeded_returns_the_real_result(scripted_vendor_clis, provider, monkeypatch):
    from connections import account_controls

    account = scripted_vendor_clis[provider]
    reached, release = asyncio.Event(), asyncio.Event()
    vendor_status = account_controls._vendor_status

    async def slow_status(*args):
        reached.set()
        await release.wait()
        return await vendor_status(*args)

    monkeypatch.setattr(account_controls, "_vendor_status", slow_status)
    await account_controls.start_signin(account["id"])
    async with asyncio.timeout(30):
        await reached.wait()
    operation = account_controls._SIGNINS[account["id"]]
    assert (operation.process.returncode, operation.status) == (0, "waiting")
    cancelling = asyncio.create_task(account_controls.cancel_signin(account["id"]))
    await asyncio.sleep(0.05)
    assert not cancelling.done() and not operation.task.done()
    release.set()
    completed = {"account_id": account["id"], "route": "browser", "status": "completed", "cause": None,
                 "duplicate_of": None}
    async with asyncio.timeout(30):
        assert await cancelling == completed
    assert _account(account["id"])["state"] == "signed_in"
    # Cancel on a finished record returns it and leaves it in place; no logout is ever run.
    assert await account_controls.cancel_signin(account["id"]) == completed
    assert account_controls._SIGNINS == {account["id"]: operation}
    assert [args for _, args in _calls(scripted_vendor_clis["log"])] == (
        [_ARGV[provider]["login"], _ARGV[provider]["status"], "--version"])


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_a_sign_in_check_that_hit_an_unexpected_error_ends_failed_and_blocks_nothing(
        scripted_vendor_clis, provider, monkeypatch, caplog):
    from connections import account_controls
    from connections.vendor_accounts import add_account

    first, second = scripted_vendor_clis[provider]["id"], add_account(provider, "Other")["id"]
    vendor_status = account_controls._vendor_status
    broken = [True]
    failed = {"account_id": first, "route": "browser", "status": "failed", "cause": "signin_check_failed", "ref": REF}

    async def status(*args):
        if broken[0]:
            raise RuntimeError("unexpected")
        return await vendor_status(*args)

    monkeypatch.setattr(account_controls, "_vendor_status", status)
    async with asyncio.timeout(30):
        with caplog.at_level("DEBUG"):
            assert (await account_controls.start_signin(first))["status"] == "waiting"
            dead = account_controls._SIGNINS[first]
            # The hold ends by itself, without an exception, and nobody is left waiting.
            assert await asyncio.gather(dead.task, return_exceptions=True) == [None]
            checked = await account_controls.check_signin(first)
        broken[0] = False
        assert checked == failed
        # Only the error's type is logged; its text reaches neither the record nor the log.
        assert "RuntimeError" in caplog.text
        assert "unexpected" not in json.dumps(checked) + caplog.text
        # Another account of the same vendor is not refused.
        assert (await account_controls.start_signin(second))["status"] == "waiting"
        assert (await _finished(second))["status"] == "completed"
        # Cancel returns the failed record like any finished one and leaves it in place.
        assert await account_controls.cancel_signin(first) == failed
        assert account_controls._SIGNINS[first] is dead
        assert await account_controls.check_signin(first) == failed
        # A start for the same account begins a new sign-in and replaces the record.
        assert (await account_controls.start_signin(first))["status"] == "waiting"
        assert account_controls._SIGNINS[first] is not dead
        assert (await _finished(first))["status"] == "completed"
    assert _account(first)["state"] == "signed_in"


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_a_hold_cancelled_from_outside_stays_cancelled_and_blocks_nothing(
        scripted_vendor_clis, provider, monkeypatch, tmp_path):
    from connections import account_controls
    from connections.vendor_accounts import add_account

    first, second = scripted_vendor_clis[provider]["id"], add_account(provider, "Other")["id"]
    monkeypatch.setenv("LOGIN_HOLD", str(tmp_path / "never"))
    async with asyncio.timeout(30):
        await account_controls.start_signin(first)
        await _started(scripted_vendor_clis["log"])
        dead = account_controls._SIGNINS[first]
        dead.task.cancel()
        await asyncio.gather(dead.task, return_exceptions=True)
        # The cancellation passed through the hold untouched: no answer was written.
        assert dead.task.cancelled()
        assert await account_controls.check_signin(first) == {
            "account_id": first, "route": "browser", "status": "waiting", "cause": None}
        # Nothing is waiting on it, so another account of the same vendor is not refused.
        assert (await account_controls.start_signin(second))["status"] == "waiting"
        other = account_controls._SIGNINS[second]
        # Cancel returns cancelled and drops the record.
        assert await account_controls.cancel_signin(first) == {
            "account_id": first, "route": "browser", "status": "cancelled", "cause": None}
        assert first not in account_controls._SIGNINS
        assert (await account_controls.cancel_signin(second))["status"] == "cancelled"
    assert dead.process.returncode is not None and other.process.returncode is not None
    assert account_controls._SIGNINS == {}


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_an_error_while_a_cancelled_sign_in_is_stopping_leaves_it_cancelled(
        scripted_vendor_clis, provider, monkeypatch, tmp_path, caplog):
    from connections import account_controls, harness_process

    account = scripted_vendor_clis[provider]
    holding = asyncio.Event()

    @contextlib.asynccontextmanager
    async def killed_then_broken(process):
        holding.set()
        try:
            yield
        except asyncio.CancelledError:
            await harness_process._kill_and_reap(process)
            raise RuntimeError("unexpected") from None

    monkeypatch.setattr(account_controls, "killed_on_exit", killed_then_broken)
    monkeypatch.setenv("LOGIN_HOLD", str(tmp_path / "never"))
    async with asyncio.timeout(30):
        await account_controls.start_signin(account["id"])
        await _started(scripted_vendor_clis["log"])
        # The vendor can record its call before the sign-in task takes its first step; cancel only once it holds.
        await holding.wait()
        operation = account_controls._SIGNINS[account["id"]]
        with caplog.at_level("DEBUG"):
            cancelled = await account_controls.cancel_signin(account["id"])
    assert cancelled == {"account_id": account["id"], "route": "browser", "status": "cancelled", "cause": None}
    # Only the error's type is logged; its text reaches neither the record nor the log.
    assert "RuntimeError" in caplog.text
    assert "unexpected" not in json.dumps(cancelled) + caplog.text
    assert operation.process.returncode is not None and account["id"] not in account_controls._SIGNINS
    assert _account(account["id"])["state"] == "signed_out"


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
@pytest.mark.parametrize("change", ["renamed", "removed"])
async def test_stale_or_removed_account_at_completion_writes_neither_identity_nor_state(
        scripted_vendor_clis, provider, change, monkeypatch, tmp_path):
    from connections import account_controls
    from connections.vendor_accounts import rename_account, set_account_state

    account = scripted_vendor_clis[provider]
    release = tmp_path / "release"
    monkeypatch.setenv("LOGIN_HOLD", str(release))
    await account_controls.start_signin(account["id"])
    if change == "renamed":
        rename_account(account["id"], "Renamed", expected_revision=0)
    else:
        set_account_state(account["id"], "removed", expected_revision=0)
    before = _account(account["id"])
    release.touch()
    assert await _finished(account["id"]) == {"account_id": account["id"], "route": "browser",
                                              "status": "superseded", "cause": "account_changed"}
    after = _account(account["id"])
    assert after == before
    assert (after["identity"], after["revision"]) == ({}, 1)
    assert after["state"] == ("signed_out" if change == "renamed" else "removed")


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
async def test_completion_is_one_transaction(scripted_vendor_clis, monkeypatch):
    from connections import account_controls, account_signin_commit
    from connections.vendor_accounts import AccountError

    claude = scripted_vendor_clis["claude-code"]

    def refused(*args, **kwargs):
        raise AccountError("account revision changed; refresh before acting")

    # The identity write succeeds first; a refused state change must take it back.
    monkeypatch.setattr(account_signin_commit, "change_state", refused)
    await account_controls.start_signin(claude["id"])
    assert (await _finished(claude["id"]))["status"] == "superseded"
    assert (_account(claude["id"])["identity"], _account(claude["id"])["state"]) == ({}, "signed_out")


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_sign_in_clears_the_last_sign_out_cleanup_result(scripted_vendor_clis, provider):
    from connections import account_controls

    account = scripted_vendor_clis[provider]
    _, cleanup = await account_controls.sign_out(account["id"], expected_revision=0)
    assert cleanup["status"] == "done"
    assert _account(account["id"])["identity"] == {} and _account(account["id"])["auth_cleanup"] is not None
    await account_controls.start_signin(account["id"])
    assert (await _finished(account["id"]))["status"] == "completed"
    assert _account(account["id"])["auth_cleanup"] is None
    assert "auth_cleanup" not in _account(account["id"])["identity"]


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_stored_home_is_passed_verbatim_and_never_rewritten(scripted_vendor_clis, provider):
    from connections import account_controls
    from connections.vendor_accounts import rename_account
    from infra.database import get_db

    account = scripted_vendor_clis[provider]
    # A spelling a path library would normalize: the vendor must still get these exact characters.
    parent, _, leaf = account["source_home"].rpartition("/")
    verbatim = parent + "/./" + leaf
    assert verbatim != account["source_home"] and Path(verbatim).is_dir()
    db = get_db()
    db.execute("UPDATE vendor_accounts SET source_home=? WHERE id=?", (verbatim, account["id"]))
    db.commit()
    db.close()
    rename_account(account["id"], "Renamed", expected_revision=0)
    assert _account(account["id"])["source_home"] == verbatim
    await account_controls.start_signin(account["id"])
    assert (await _finished(account["id"]))["status"] == "completed"
    assert _account(account["id"])["source_home"] == verbatim
    assert (await account_controls.verify(account["id"]))["vendor_signed_in"] is True
    await account_controls.sign_out(account["id"], expected_revision=2)
    assert (_account(account["id"])["source_home"], _account(account["id"])["runtime_home"]) == (
        verbatim, account["runtime_home"])
    assert [home for home, _ in _calls(scripted_vendor_clis["log"])] == [verbatim] * 6
    assert [args for _, args in _calls(scripted_vendor_clis["log"])] == [
        _ARGV[provider]["login"], _ARGV[provider]["status"], "--version",
        _ARGV[provider]["status"], "--version", _ARGV[provider]["logout"]]


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_failed_logout_on_remove_records_failed_and_deletes_nothing(scripted_vendor_clis, provider, monkeypatch):
    from connections import account_controls

    account = scripted_vendor_clis[provider]
    Path(account["source_home"], "signed").touch()
    monkeypatch.setenv("LOGOUT_FAIL", "1")
    removed, cleanup = await account_controls.remove(account["id"], expected_revision=0)
    assert removed["state"] == "removed"
    assert (cleanup["status"], cleanup["cause"]) == ("failed", "vendor_logout_failed")
    stored = _account(account["id"])
    assert stored["state"] == "removed"
    assert stored["auth_cleanup"] == cleanup
    assert Path(account["source_home"]).is_dir() and Path(account["runtime_home"]).is_dir()
    assert Path(account["source_home"], "signed").exists()
    assert _calls(scripted_vendor_clis["log"]) == [(account["source_home"], _ARGV[provider]["logout"])]


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_remove_runs_logout_after_the_tombstone_and_keeps_the_homes(scripted_vendor_clis, provider):
    from connections import account_controls

    account = scripted_vendor_clis[provider]
    Path(account["source_home"], "signed").touch()
    removed, cleanup = await account_controls.remove(account["id"], expected_revision=0)
    assert (removed["state"], cleanup["status"], cleanup["cause"]) == ("removed", "done", None)
    assert _account(account["id"])["auth_cleanup"]["status"] == "done"
    assert not Path(account["source_home"], "signed").exists()
    assert Path(account["source_home"]).is_dir() and Path(account["runtime_home"]).is_dir()


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_sign_out_and_remove_stop_a_held_sign_in(scripted_vendor_clis, provider, monkeypatch, tmp_path):
    from connections import account_controls

    account = scripted_vendor_clis[provider]
    monkeypatch.setenv("LOGIN_HOLD", str(tmp_path / "never"))
    await account_controls.start_signin(account["id"])
    process = account_controls._SIGNINS[account["id"]].process
    await account_controls.remove(account["id"], expected_revision=0)
    assert process.returncode is not None
    assert account_controls._SIGNINS == {}
    assert _account(account["id"])["state"] == "removed"


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_default_account_signs_in_and_out_in_its_own_home(scripted_vendor_clis, provider):
    from connections import account_controls

    default = scripted_vendor_clis["default"][provider]
    await account_controls.start_signin(default["id"])
    assert (await _finished(default["id"]))["status"] == "completed"
    signed_in = _account(default["id"])
    _, cleanup = await account_controls.sign_out(default["id"], expected_revision=signed_in["revision"])
    assert (cleanup["status"], cleanup["cause"]) == ("done", None)
    assert all(home == default["source_home"] for home, _ in _calls(scripted_vendor_clis["log"]))


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_verify_flips_only_on_a_definite_signed_out_answer_and_never_signs_in(scripted_vendor_clis, provider, monkeypatch):
    from connections import account_controls
    from connections.vendor_accounts import set_account_state

    account = scripted_vendor_clis[provider]
    Path(account["source_home"], "signed").touch()
    # A signed-out account stays signed out even though the vendor says signed in.
    assert await account_controls.verify(account["id"]) == {
        "account_id": account["id"], "vendor_signed_in": True, "cause": None, "state": "signed_out"}
    set_account_state(account["id"], "signed_in", expected_revision=0)
    monkeypatch.setenv("STATUS_EXIT", "2")
    assert await account_controls.verify(account["id"]) == {
        "account_id": account["id"], "vendor_signed_in": None, "cause": "vendor_status_failed", "state": "signed_in"}
    monkeypatch.delenv("STATUS_EXIT")
    Path(account["source_home"], "signed").unlink()
    assert (await account_controls.verify(account["id"]))["state"] == "signed_out"
    assert _account(account["id"])["revision"] == 2


def test_record_evidence_keeps_the_cleanup_result_and_never_takes_it_as_a_fact(isolated_db, monkeypatch):
    from dataclasses import replace

    from connections import account_profiles
    from connections.models import upsert_connection
    from connections.vendor_accounts import add_account, get_account, record_auth_cleanup, record_evidence

    upsert_connection(type="cli", provider="claude-code", status="available", config={})
    # Even a profile that lists them as facts cannot have them written as identity.
    monkeypatch.setitem(account_profiles._PROFILES, "claude-code", replace(
        account_profiles.profile_for("claude-code"), identity_facts=("email", "auth_cleanup")))
    account = add_account("claude-code", "Personal")
    cleanup = {"status": "failed", "cause": "vendor_logout_failed", "at": "t"}
    record_auth_cleanup(account["id"], cleanup)
    record_evidence(account["id"], identity={"email": "person@example.test", "hint": "forged",
                                             "auth_cleanup": "forged"})
    assert get_account(account["id"])["identity"] == {"email": "person@example.test"}
    assert get_account(account["id"])["auth_cleanup"] == cleanup
    record_auth_cleanup(account["id"], None)
    assert get_account(account["id"])["identity"] == {"email": "person@example.test"}
    assert get_account(account["id"])["auth_cleanup"] is None
    assert get_account(account["id"])["revision"] == 0


@both
@pytest.mark.parametrize("leave", ["signout", "remove"])
def test_an_unmarked_test_starts_no_vendor_process(sign_in_api_client, provider, leave, monkeypatch):
    """The conftest guard hides the vendor binary, here a real executable, from every unmarked test."""
    import subprocess

    def spawned(*args, **kwargs):
        raise AssertionError("an unmarked test must not start a vendor process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawned)
    monkeypatch.setattr(subprocess, "Popen", spawned)
    added = sign_in_api_client.post("/api/vendor-accounts", json={"provider": provider, "name": "Added"}).json()
    started = sign_in_api_client.post("/api/vendor-accounts/" + added["id"] + "/signin")
    missing = "codex_executable_missing" if provider == "codex" else "claude_executable_missing"
    assert started.json() == {"account_id": added["id"], "route": None, "status": "unavailable", "cause": missing,
                              "ref": REF}
    path = "/api/vendor-accounts/" + added["id"]
    left = (sign_in_api_client.post(path + "/signout", json={"expected_revision": 0}) if leave == "signout"
            else sign_in_api_client.request("DELETE", path, json={"expected_revision": 0}))
    assert left.status_code == 200
    body = left.json()
    assert (body["auth_cleanup_status"], body["auth_cleanup_cause"]) == ("failed", missing)
    assert body["state"] == ("signed_out" if leave == "signout" else "removed")
    assert body["identity"] == {} and "auth_cleanup" not in body


def test_missing_signin_check_route_matches_setup_recovery(monkeypatch):
    from api.vendor_accounts import router
    from infra.auth import require_user

    monkeypatch.setattr(account_controls, '_SIGNINS', {})
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {'id': 'user'}
    with TestClient(app) as api:
        response = api.post('/api/vendor-accounts/missing/signin/check')
    assert response.status_code == 409
    assert response.json() == {'detail': 'no sign-in is in progress for this account'}


@pytest.mark.real_harness_cli
def test_routes_run_sign_in_to_sign_out_and_never_return_the_cleanup_as_identity(isolated_db, tmp_path, monkeypatch):
    from api.vendor_accounts import router
    from connections import account_controls
    from connections.models import upsert_connection
    from infra.auth import require_user

    log = tmp_path / "vendor.log"
    log.touch()
    monkeypatch.setenv("VENDOR_LOG", str(log))
    default_claude = tmp_path / "default-claude"
    default_claude.mkdir()
    monkeypatch.setenv("TEST_CLAUDE_DEFAULT", str(default_claude))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "default-codex"))
    upsert_connection(type="cli", provider="claude-code", status="available",
                      config={"binary": _script(tmp_path / "claude", _CLAUDE)})
    account_controls._SIGNINS.clear()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as api:
        app.dependency_overrides[require_user] = lambda: {"id": "user"}
        added = api.post("/api/vendor-accounts", json={"provider": "claude-code", "name": "Personal"}).json()
        path = "/api/vendor-accounts/" + added["id"]
        assert api.post(path + "/signin/check").status_code == 409
        assert api.post(path + "/signin").json()["status"] in ("waiting", "completed")
        api.portal.call(_finished_or_gone, added["id"])
        listed = api.get("/api/vendor-accounts?provider=claude-code").json()["accounts"][-1]
        assert (listed["state"], listed["identity"]) == ("signed_in", {
            "email": added["id"] + "@example.test", "org_name": "Example Team", "org_id": "org-synthetic-1", "plan": "team"})
        assert api.post(path + "/verify").json()["vendor_signed_in"] is True
        left = api.post(path + "/signout", json={"expected_revision": listed["revision"]}).json()
        assert (left["state"], left["auth_cleanup_status"], left["auth_cleanup_cause"]) == ("signed_out", "done", None)
        assert "auth_cleanup" not in left and "auth_cleanup" not in left["identity"]
        assert api.delete(path + "/signin").json()["status"] == "cancelled"
    assert [args for _, args in _calls(log)] == [
        "auth login", "auth status", "--version", "auth status", "--version", "auth logout"]


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_same_as_default_is_refused_only_after_the_held_login_finishes(scripted_vendor_clis, provider, monkeypatch, tmp_path):
    from connections import account_controls
    from connections.vendor_accounts import record_evidence, set_account_state

    account = scripted_vendor_clis[provider]
    facts = _identity_file(account["source_home"], provider, "private-workspace", "private-person")
    default = scripted_vendor_clis["default"][provider]
    record_evidence(default["id"], identity=facts)
    set_account_state(default["id"], "signed_in", expected_revision=default["revision"])
    default_revision = _account(default["id"])["revision"]
    # Existing identity must survive refusal, rather than being overwritten by the attempted login.
    record_evidence(account["id"], identity={"email": "old@example.test", "org_id": "old-org"})
    before = _account(account["id"])["identity"]
    release = tmp_path / "release"
    monkeypatch.setenv("LOGIN_HOLD", str(release))
    async with asyncio.timeout(30):
        await account_controls.start_signin(account["id"])
        await _started(scripted_vendor_clis["log"])
        assert await account_controls.check_signin(account["id"]) == {
            "account_id": account["id"], "route": "browser", "status": "waiting", "cause": None}
        assert _account(default["id"])["identity"] == facts
        release.touch()
        assert await _finished(account["id"]) == {
            "account_id": account["id"], "route": "browser", "status": "failed", "cause": "same_account",
            "duplicate_of": scripted_vendor_clis["default"][provider]["id"], "ref": REF}
    stored = _account(account["id"])
    assert (stored["state"], stored["revision"]) == ("signed_out", 0)
    assert stored["identity"] == before
    assert stored["auth_cleanup"]["status"] == "done"
    assert not Path(account["source_home"], "signed").exists()
    default = _account(scripted_vendor_clis["default"][provider]["id"])
    assert default["identity"] == facts
    assert default["revision"] == default_revision
    assert default["state"] == "signed_in"


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
async def test_same_claude_email_in_a_different_org_is_not_a_repeat(scripted_vendor_clis):
    from connections import account_controls
    from connections.vendor_accounts import add_account, duplicate_of

    first, second = scripted_vendor_clis["claude-code"], add_account("claude-code", "Other org")
    for row, org in ((first, "org-one"), (second, "org-two")):
        _identity_file(row["source_home"], "claude-code", "same@example.test", org)
        await account_controls.start_signin(row["id"])
        assert (await _finished(row["id"]))["status"] == "completed"
    assert duplicate_of(_account(second["id"])) is None


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_signing_into_a_changed_identity_clears_usage_but_keeps_the_binding(scripted_vendor_clis, provider):
    from connections import account_controls, harness_usage
    from connections.vendor_accounts import add_account, duplicate_of, record_evidence
    from infra.database import get_db

    row = scripted_vendor_clis[provider]
    old = _identity_file(row["source_home"], provider, "old-workspace", "old-user")
    await account_controls.start_signin(row["id"])
    await _finished(row["id"])
    pair = add_account(provider, "Old pair")
    record_evidence(pair["id"], identity=old)
    assert duplicate_of(_account(row["id"])) == pair["id"]
    assert duplicate_of(_account(pair["id"])) == row["id"]
    db = get_db()
    try:
        db.execute("INSERT INTO threads(id,user_id,created_at,updated_at) VALUES ('bound','user','t','t')")
        db.execute("INSERT INTO thread_vendor_accounts(thread_id,provider,account_id) VALUES ('bound',?,?)",
                   (provider, row["id"]))
        db.commit()
    finally:
        db.close()
    harness_usage.record_usage(provider=provider, receipt=receipt_of(provider, row["id"]), facts={"plan": "old"})
    await account_controls.sign_out(row["id"], expected_revision=1)
    new = _identity_file(row["source_home"], provider, "new-workspace", "new-user")
    await account_controls.start_signin(row["id"])
    assert (await _finished(row["id"]))["duplicate_of"] is None
    assert _account(row["id"])["identity"] == new
    assert duplicate_of(_account(pair["id"])) is None
    assert harness_usage.stored_usage(provider=provider, account_id=row["id"]) is None
    db = get_db()
    try:
        assert db.execute("SELECT account_id FROM thread_vendor_accounts WHERE thread_id='bound'").fetchone()[0] == row["id"]
    finally:
        db.close()


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
@pytest.mark.parametrize("older_state", ["signed_out", "removed"])
async def test_an_older_signed_out_or_removed_pair_never_blocks_the_new_login(scripted_vendor_clis, provider, older_state):
    from connections import account_controls
    from connections.vendor_accounts import add_account, duplicate_of

    first, second = scripted_vendor_clis[provider], add_account(provider, "Newer")
    for row in (first, second):
        _identity_file(row["source_home"], provider, "shared-workspace", "shared-user")
    _created(first["id"], "2026-01-01T00:00:00+00:00")
    await account_controls.start_signin(first["id"])
    await _finished(first["id"])
    leave = account_controls.remove if older_state == "removed" else account_controls.sign_out
    await leave(first["id"], expected_revision=1)
    await account_controls.start_signin(second["id"])
    result = await _finished(second["id"])
    expected = first["id"] if older_state == "signed_out" else None
    assert result == {"account_id": second["id"], "route": "browser", "status": "completed", "cause": None,
                      "duplicate_of": expected}
    assert duplicate_of(_account(first["id"])) == (second["id"] if older_state == "signed_out" else None)
    assert duplicate_of(_account(second["id"])) == expected


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
@pytest.mark.parametrize("via", ["verify", "signin"])
async def test_ambient_identity_does_not_change_accounts_during_signin_or_verify(scripted_vendor_clis, provider, via):
    from api import vendor_accounts as routes
    from connections import account_controls
    from connections.vendor_accounts import add_account

    account = scripted_vendor_clis[provider]
    _identity_file(account["source_home"], provider, "shared-workspace", "shared-user")
    await account_controls.start_signin(account["id"])
    await _finished(account["id"])
    _identity_file(scripted_vendor_clis["default_homes"][provider], provider, "shared-workspace", "shared-user")
    Path(scripted_vendor_clis["default_homes"][provider], "signed").touch()
    before = list(_calls(scripted_vendor_clis["log"]))
    assert [row["duplicate_of"] for row in (await routes.list_vendor_accounts(provider=provider))["accounts"]] == [None, None]
    assert _calls(scripted_vendor_clis["log"]) == before  # listing never reads a credential or runs status
    if via == "verify":
        await account_controls.verify(account["id"])
    else:
        other = add_account(provider, "Different")
        await account_controls.start_signin(other["id"])
        assert (await _finished(other["id"]))["status"] == "completed"
    listed = {row["id"]: row for row in (await routes.list_vendor_accounts(provider=provider))["accounts"]}
    default_id = scripted_vendor_clis["default"][provider]["id"]
    assert listed[account["id"]]["duplicate_of"] is None
    assert listed[default_id]["duplicate_of"] is None
    assert (listed[account["id"]]["state"], listed[default_id]["state"], listed[default_id]["revision"]) == (
        "signed_in", "signed_out", 0)
    assert all(home != str(scripted_vendor_clis["default_homes"][provider])
               for home, _ in _calls(scripted_vendor_clis["log"]))


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_refusing_a_repeat_records_a_failed_logout_with_its_fixed_cause(scripted_vendor_clis, provider, monkeypatch):
    from connections import account_controls

    row = scripted_vendor_clis[provider]
    default = scripted_vendor_clis["default"][provider]
    for home in (row["source_home"], default["source_home"]):
        _identity_file(home, provider, "same-workspace", "same-user")
        Path(home, "signed").touch()
    await account_controls.start_signin(default["id"])
    assert (await _finished(default["id"]))["status"] == "completed"
    monkeypatch.setenv("LOGOUT_FAIL", "1")
    await account_controls.start_signin(row["id"])
    assert (await _finished(row["id"]))["cause"] == "same_account"
    cleanup = _account(row["id"])["auth_cleanup"]
    assert (cleanup["status"], cleanup["cause"]) == ("failed", "vendor_logout_failed")
    assert Path(row["source_home"], "signed").exists()
    assert _account(row["id"])["state"] == "signed_out"


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["file", "unreadable", "account", "token", "sub-only", "empty-user", "bad-token"])
@pytest.mark.parametrize("side", ["new", "other", "default"])
async def test_missing_codex_comparison_inputs_make_no_claim_and_clear_no_usage(scripted_vendor_clis, monkeypatch, missing, side):
    from connections import account_controls, harness_usage
    from connections.vendor_accounts import add_account, record_evidence

    row = scripted_vendor_clis["codex"]
    facts = _identity_file(row["source_home"], "codex", "same-workspace", "same-user")
    if side == "default":
        other_home = scripted_vendor_clis["default_homes"]["codex"]
        other_id = scripted_vendor_clis["default"]["codex"]["id"]
    else:
        other = add_account("codex", "Other")
        other_home, other_id = other["source_home"], other["id"]
        record_evidence(other_id, identity=facts if side == "new" else {})
        from connections.vendor_accounts import set_account_state
        set_account_state(other_id, "signed_in", expected_revision=0)
    _identity_file(other_home, "codex", "same-workspace", "same-user")
    target = Path(row["source_home"] if side == "new" else other_home, "auth.json")
    payload = json.loads(target.read_text())
    if missing == "file":
        target.unlink()
    elif missing == "unreadable":
        target.write_text("not json")
    else:
        if missing == "account":
            payload["tokens"].pop("account_id")
        elif missing == "token":
            payload["tokens"].pop("id_token")
        else:
            payload["tokens"]["id_token"] = (
                "bad.token" if missing == "bad-token" else _unsigned({"sub": "same-user"}) if missing == "sub-only"
                else _unsigned({"https://api.openai.com/auth": {"chatgpt_user_id": "", "user_id": ""}}))
        target.write_text(json.dumps(payload))
    # The synthetic login must not recreate a missing file.
    monkeypatch.setenv("NO_AUTH", "1")
    if side == "new":
        record_evidence(row["id"], identity=facts)
    seed_usage(row["id"], facts={"plan": "retained"})
    before = harness_usage.stored_usage(provider="codex", account_id=row["id"])
    assert before is not None
    await account_controls.start_signin(row["id"])
    result = await _finished(row["id"])
    assert result == {"account_id": row["id"], "route": "browser", "status": "completed", "cause": None,
                      "duplicate_of": None}
    assert harness_usage.stored_usage(provider="codex", account_id=row["id"]) is None
    if side == "new":
        assert _account(row["id"])["identity"] == {}


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@pytest.mark.parametrize("user_key", ["chatgpt_user_id", "user_id"])
async def test_codex_fingerprint_is_private_and_distinguishes_people_in_one_workspace(scripted_vendor_clis, user_key, caplog):
    from api import vendor_accounts as routes
    from connections import account_controls
    from connections.vendor_accounts import add_account

    workspace, user = "private-workspace-value", "private-user-value"
    first, second = scripted_vendor_clis["codex"], add_account("codex", "Other seat")
    token = _unsigned({"https://api.openai.com/auth": {user_key: user}, "sub": "unused-private-sub"})
    Path(first["source_home"], "auth.json").write_text(json.dumps({"tokens": {"account_id": workspace, "id_token": token}}))
    _identity_file(second["source_home"], "codex", workspace, "different-user")
    shown = []
    for row in (first, second):
        shown.append(await account_controls.start_signin(row["id"]))
        shown.append(await _finished(row["id"]))
        assert shown[-1]["status"] == "completed" and shown[-1]["duplicate_of"] is None
    assert _account(first["id"])["identity"] == {"account_fp": _fingerprint(workspace, user)}
    listed = await routes.list_vendor_accounts(provider="codex")
    assert [row["identity"] for row in listed["accounts"]] == [{}, {}, {}]
    output = json.dumps([shown, listed, _account(first["id"]), _account(second["id"])]) + caplog.text
    for secret in (workspace, user, token, "unused-private-sub"):
        assert secret not in output
    assert "account_fp" not in json.dumps([shown, listed])


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_vendor_busy_refusal_names_the_holder_and_cancelling_it_allows_a_new_start(scripted_vendor_clis, provider, monkeypatch, tmp_path):
    from connections import account_controls
    from connections.vendor_accounts import add_account

    first, second = scripted_vendor_clis[provider], add_account(provider, "Other")
    monkeypatch.setenv("LOGIN_HOLD", str(tmp_path / "never"))
    async with asyncio.timeout(30):
        await account_controls.start_signin(first["id"])
        await _started(scripted_vendor_clis["log"])
        assert await account_controls.start_signin(second["id"]) == {
            "account_id": second["id"], "route": "browser", "status": "refused",
            "cause": ("codex" if provider == "codex" else "claude") + "_signin_in_progress", "held_by": first["id"]}
        assert (await account_controls.cancel_signin(first["id"]))["status"] == "cancelled"
        assert (await account_controls.start_signin(second["id"]))["status"] == "waiting"
        await account_controls.cancel_signin(second["id"])
    assert account_controls._SIGNINS == {}


@pytest.mark.asyncio
@both
async def test_listing_accounts_reads_no_host_file_or_process(scripted_vendor_clis, provider, monkeypatch):
    from api.vendor_accounts import list_vendor_accounts

    touched = []

    def forbidden(*args, **kwargs):
        touched.append(True)
        raise AssertionError("listing read a credential or ran a vendor")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    result = await list_vendor_accounts(provider=provider)
    assert touched == []
    assert {row['id'] for row in result['accounts']} == {
        scripted_vendor_clis['default'][provider]['id'], scripted_vendor_clis[provider]['id']}


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["new", "other"])
@pytest.mark.parametrize("missing", ["email", "orgId"])
async def test_missing_claude_comparison_facts_on_either_side_make_no_claim(scripted_vendor_clis, side, missing):
    from connections import account_controls, harness_usage
    from connections.vendor_accounts import add_account, record_evidence, set_account_state

    row, other = scripted_vendor_clis["claude-code"], add_account("claude-code", "Incomplete")
    complete = _identity_file(row["source_home"], "claude-code", "same-email", "same-org")
    if side == "new":
        report = json.loads(Path(row["source_home"], "identity.json").read_text())
        report.pop(missing)
        Path(row["source_home"], "identity.json").write_text(json.dumps(report))
        record_evidence(row["id"], identity=complete)
        record_evidence(other["id"], identity=complete)
    else:
        incomplete = dict(complete)
        incomplete.pop("email" if missing == "email" else "org_id")
        record_evidence(other["id"], identity=incomplete)
    set_account_state(other["id"], "signed_in", expected_revision=0)
    seed_usage(row["id"], facts={"old": "usage"})
    before = harness_usage.stored_usage(provider="claude-code", account_id=row["id"])
    assert before is not None
    await account_controls.start_signin(row["id"])
    assert await _finished(row["id"]) == {
        "account_id": row["id"], "route": "browser", "status": "completed", "cause": None, "duplicate_of": None}
    assert harness_usage.stored_usage(provider="claude-code", account_id=row["id"]) is None


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_changed_revision_is_superseded_before_a_repeat_can_be_refused(scripted_vendor_clis, provider, monkeypatch, tmp_path):
    from connections import account_controls
    from connections.vendor_accounts import rename_account

    row = scripted_vendor_clis[provider]
    default = scripted_vendor_clis["default"][provider]
    for home in (row["source_home"], default["source_home"]):
        _identity_file(home, provider, "same-workspace", "same-user")
        Path(home, "signed").touch()
    await account_controls.start_signin(default["id"])
    assert (await _finished(default["id"]))["status"] == "completed"
    release = tmp_path / "release"
    monkeypatch.setenv("LOGIN_HOLD", str(release))
    async with asyncio.timeout(30):
        await account_controls.start_signin(row["id"])
        await _started(scripted_vendor_clis["log"])
        rename_account(row["id"], "Changed", expected_revision=0)
        before = _account(row["id"])
        release.touch()
        assert await _finished(row["id"]) == {
            "account_id": row["id"], "route": "browser", "status": "superseded", "cause": "account_changed"}
    assert _account(row["id"]) == before
    assert Path(row["source_home"], "signed").exists()
    assert _ARGV[provider]["logout"] not in [args for _, args in _calls(scripted_vendor_clis["log"])]


@pytest.mark.asyncio
@both
async def test_unknown_and_private_identity_facts_are_not_projected(scripted_vendor_clis, provider):
    from api.vendor_accounts import _safe
    from connections.vendor_accounts import record_evidence

    row = scripted_vendor_clis[provider]
    record_evidence(row["id"], identity={"unknown_fact": "injected", "account_fp": "internal"})
    expected = {"account_fp": "internal"} if provider == "codex" else {}
    assert _account(row["id"])["identity"] == expected
    assert _safe(_account(row["id"]))["identity"] == {}
    assert _safe(_account(row["id"]) | {"identity": {"unknown_fact": "injected"}})["identity"] == {}


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@pytest.mark.parametrize("reading", ["allowed", "limited", "missing", "write_failed"])
async def test_account_catalog_usage_leaves_other_accounts_alone(
    isolated_db, client, claude_session, monkeypatch, live_app_loop, reading,
):
    from connections import claude_usage, harness_usage, harness_models
    from connections.vendor_accounts import set_account_state

    default = seed_default_account('claude-code')
    set_account_state(default['id'], 'signed_in', expected_revision=default['revision'])
    _seed_old_account()
    accounts = _seed_added_accounts()
    before = _added_state(accounts)
    usage_before = harness_usage.stored_usage(provider='claude-code', account_id=default['id'])
    if reading in {"allowed", "limited"}:
        claude_session(_listed(usage=_report(20 if reading == "allowed" else 100)))
    else:
        claude_session(_stream(_ok("corsia-model-list", {"models": []})))
    if reading == "write_failed":
        apply_usage = claude_usage.apply_usage_response

        def failed_write(_response, *, receipt, db=None):
            apply_usage(_report(20), receipt=receipt, db=db)
            raise OSError("usage write failed after updating the meter")

        monkeypatch.setattr(claude_usage, "apply_usage_response", failed_write)
    releases, _stops, scheduled = _observe(monkeypatch)

    if reading == 'write_failed':
        with pytest.raises(OSError, match='usage write failed'):
            await harness_models.refresh_provider('claude-code', reason='manual', account_id=default['id'])
        assert harness_usage.stored_usage(provider='claude-code', account_id=default['id']) == usage_before
        assert releases == []
    else:
        result = await harness_models.refresh_provider('claude-code', reason='manual', account_id=default['id'])
        assert result.status == 'ready'
        usage = harness_usage.stored_usage(provider='claude-code', account_id=default['id'])
        capacity = harness_usage.project_for_model(usage, model='claude-code/opus')
        assert capacity['state'] == ('allowed' if reading == 'allowed' else 'limited')
        assert harness_usage.project_for_model(usage, model='claude-code/sonnet')['state'] == 'limited'
        assert all(subject.account_id == default['id'] for subject in releases)
    scheduled.assert_not_called()
    assert _added_state(accounts) == before
    for account in accounts:
        capacity = harness_usage.project_for_model(
            harness_usage.stored_usage(provider='claude-code', account_id=account), model='claude-code/sonnet')
        assert (capacity['state'], capacity['reason']) == ('limited', f'{account}_limit')


@pytest.mark.real_harness_cli
@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_connector_upsert_does_not_change_account_usage(
    isolated_db, client, monkeypatch, provider,
):
    from connections import harness_usage
    from connections.models import upsert_connection
    from connections.vendor_accounts import set_account_state

    default = seed_default_account(provider)
    set_account_state(default['id'], 'signed_in', expected_revision=default['revision'])
    _seed_old_account(provider=provider)
    accounts = _seed_added_accounts(provider)
    before = _added_state(accounts)
    usage_before = harness_usage.stored_usage(provider=provider, account_id=default['id'])
    releases, stops, scheduled = _observe(monkeypatch)

    upsert_connection(type='cli', provider=provider, status='available', config={'binary': '/bin/test'})

    scheduled.assert_not_called()
    assert _added_state(accounts) == before
    assert harness_usage.stored_usage(provider=provider, account_id=default['id']) == usage_before
    assert releases == [] and stops == []


@pytest.mark.real_harness_cli
@pytest.mark.parametrize("sign_in", [_by_status, _by_source_check, _by_apply_check])
@pytest.mark.parametrize("schedule", [True, False])
@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_source_status_writers_do_not_treat_a_ready_check_as_an_account_signin(
    isolated_db, client, monkeypatch, provider, schedule, sign_in,
):
    from connections import harness_usage
    from connections.vendor_accounts import set_account_state

    default = seed_default_account(provider)
    set_account_state(default['id'], 'signed_in', expected_revision=default['revision'])
    _seed_old_account(provider=provider)
    accounts = _seed_added_accounts(provider)
    before = _added_state(accounts)
    usage_before = harness_usage.stored_usage(provider=provider, account_id=default['id'])
    releases, _stops, scheduled = _observe(monkeypatch)

    assert sign_in(provider, schedule_usage_check=schedule)["status"] == "available"

    scheduled.assert_not_called()
    assert releases == []
    assert harness_usage.stored_usage(provider=provider, account_id=default['id']) == usage_before
    assert _added_state(accounts) == before


def test_paged_out_initial_anchor_remains_raw_and_private(accounts):
    from api.thread_paging import page_messages
    from api.thread_message_rows import public_delivery_message
    from infra.send_broadcast import project_input_message, read_input_broadcast_row
    from infra.thread_inputs import claim_safe_boundary
    from infra.vendor_login import read_turn_receipt

    db, default, _, _ = accounts
    _known_default(db, default)
    _accept(db, 'initial', {'visible': {'exact': [False, None, 1]}})
    batch = _start(db, 'T1')
    db.execute("UPDATE messages SET delivery_state='delivered' WHERE id='initial'")
    for number in range(3):
        _accept(db, f'later-{number}', {'visible': number})
        claim_safe_boundary('destination', 'T1', db=db)
    newest, more, cursor = page_messages(db, 'destination', 1, None)
    assert len(newest) == 1 and newest[0]['id'] != 'initial'
    assert more
    assert read_turn_receipt('T1', db=db) == batch.vendor_receipt
    all_rows, _, _ = page_messages(db, 'destination', None, None)
    initial = next(row for row in all_rows if row['id'] == 'initial')
    assert initial['metadata'] == {'visible': {'exact': [False, None, 1]}}
    row = db.execute("SELECT * FROM messages WHERE id='initial'").fetchone()
    assert KEY not in public_delivery_message(row)['metadata']
    assert project_input_message(read_input_broadcast_row('initial', db=db))['metadata'] == initial['metadata']
    assert json.loads(row['metadata'])[KEY]['turn_id'] == 'T1'


def test_sessionless_restart_uses_frozen_receipt_and_conflicts_refuse(accounts):
    from connections.account_receipts import receipt_is_current
    from infra.vendor_login import read_turn_receipt

    db, default, _, _ = accounts
    _known_default(db, default)
    _accept(db, 'initial')
    batch = _start(db, 'T1')
    path = db.execute('PRAGMA database_list').fetchone()[2]
    db.commit()
    fresh = sqlite3.connect(path)
    fresh.row_factory = sqlite3.Row
    try:
        assert read_turn_receipt('T1', db=fresh) == batch.vendor_receipt
        _known_default(db, default, login=2)
        assert read_turn_receipt('T1', db=fresh) == batch.vendor_receipt
        assert not receipt_is_current(batch.vendor_receipt, db=fresh)
        db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,active_turn_id,metadata) "
                   "VALUES ('conflict','destination','user','x','t','T1',?)", (json.dumps({KEY: {
                       'turn_id': 'T1', 'account_revision': 0, 'login_seq': 99}}),))
        db.commit()
        assert read_turn_receipt('T1', db=fresh) is None
        db.execute("DELETE FROM messages WHERE id='conflict'")
        db.execute("UPDATE messages SET active_turn_id=NULL WHERE id='initial'")
        db.commit()
        assert read_turn_receipt('T1', db=fresh) is None
    finally:
        fresh.close()


@pytest.mark.asyncio
async def test_same_default_row_delayed_login_A_receipts_cannot_mutate_login_B(accounts):
    from connections import harness_usage as usage
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import get_account
    from subagents.codex_usage_observer import (
        handle_rate_limits_notification, observe_turn_success, observe_turn_error)

    db, default, _, _ = accounts
    _known_default(db, default, login=1)
    receipt = capture_receipt('codex', account_id=default['id'], db=db)
    session = SimpleNamespace(vendor_account_bound=True, vendor_account_id=default['id'],
                              vendor_account_receipt=receipt)
    _known_default(db, default, login=2)
    current = capture_receipt('codex', account_id=default['id'], db=db)
    assert current.account_id == receipt.account_id
    assert current.account_revision == receipt.account_revision
    assert current.login_seq > receipt.login_seq
    usage.record_limited(provider='codex', receipt=receipt_of('codex', default['id'], db=db), resets_at='2033-05-18T03:33:20+00:00',
                         source='synthetic-B-limit', reason='usage_limit', db=db)
    before = get_account(default['id'], db=db)
    stored = usage.stored_usage(provider='codex', account_id=default['id'], db=db)
    await handle_rate_limits_notification(session, 'account/rateLimits/updated', {'rateLimits': {
        'limitId': 'codex', 'primary': {'usedPercent': 1, 'resetsAt': 2000000000},
        'rateLimitReachedType': None}})
    await observe_turn_success(session, datetime.now(timezone.utc))
    await observe_turn_error(session, {'codexErrorInfo': 'usageLimitExceeded'})
    assert usage.stored_usage(provider='codex', account_id=default['id'], db=db) == stored
    assert get_account(default['id'], db=db) == before


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['claude-code', 'codex'])
async def test_setup_recovers_after_first_discovery_fails(scripted_vendor_clis, monkeypatch, provider):
    from connections import account_controls, harness_models
    from connections.account_discovery_types import AccountRefreshResult
    from connections.corsia_agent_account import FirstRunBind
    from connections.source_state import apply_check
    from connections.vendor_accounts import add_account, list_accounts
    from infra.corsia_agent import set_corsia_agent
    from infra.database import get_db
    from api.provider_sources import _harness_source
    from llm.model_status import scoped_status_rows

    account = scripted_vendor_clis[provider]
    db = get_db()
    try:
        db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES ('first-run','user',NULL,'t','t')")
        db.execute("DELETE FROM models WHERE harness=?", (provider,))
        assert set_corsia_agent('first-run', db=db)
        apply_check(provider, 'needs_auth', 'signed out', db=db)
        db.commit()
    finally:
        db.close()
    reads = []

    async def discover(provider, **kwargs):
        reads.append(kwargs['account_id'])
        if len(reads) == 1:
            return AccountRefreshResult(provider, kwargs['account_id'], status='failed')
        from connections.account_discovery_commit import commit_discovery_bundle
        from infra.db_async import run_in_db
        return await run_in_db(commit_discovery_bundle, provider, SimpleNamespace(usage_report=None, cache_stage=None, unlisted_errors={}),
            [{'selector_id': 'rechecked', 'available': True, 'context_window': 200000}],
            context=SimpleNamespace(receipt=kwargs['expected_receipt'], account_id=kwargs['account_id']),
            harness_version='1')

    monkeypatch.setattr(harness_models, 'refresh_provider', discover)
    identity = ('person@example.test', 'org-synthetic-1') if provider == 'claude-code' else ('workspace-private', 'user-private')
    _identity_file(account['source_home'], provider, *identity)
    await account_controls.start_signin(account['id'], bind=FirstRunBind('first-run', 'user'))
    assert (await _finished(account['id']))['outcome'] == 'unchecked'
    line = next(row for row in _harness_source(provider)['accounts'] if row['account_id'] == account['id'])
    assert line['state'] == 'unchecked'
    again = add_account(provider, 'Again')
    _identity_file(again['source_home'], provider, *identity)
    await account_controls.start_signin(again['id'], bind=FirstRunBind('first-run', 'user'))
    result = await _finished(again['id'])
    assert (result['status'], result['cause'], result['outcome']) == ('failed', 'same_account', 'ready')
    assert result['duplicate_of'] == account['id']
    assert await account_controls.check_signin(again['id']) == result
    assert next(row for row in _harness_source(provider)['accounts'] if row['account_id'] == account['id'])['state'] == 'ready'
    model = next(row for row in scoped_status_rows(account_id=account['id']) if row['id'] == provider + '/rechecked')
    assert next(row for row in model['runs_on'] if row['account_id'] == account['id'])['status'] == 'ready'
    added = [row['id'] for row in list_accounts(provider=provider) if not row['is_default']]
    assert added == [account['id']]
    assert _account(again['id'])['state'] == 'removed'
    assert _account(again['id'])['auth_cleanup'] is not None
    assert reads == [account['id'], account['id']]


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['claude-code', 'codex'])
async def test_repeat_unchecked_signin_rechecks_existing_account(scripted_vendor_clis, monkeypatch, provider):
    from connections import account_controls, harness_models
    from connections.account_discovery_types import AccountRefreshResult
    from connections.vendor_accounts import add_account

    first = scripted_vendor_clis[provider]
    second = add_account(provider, 'Again')
    identity = ('person@example.test', 'org-synthetic-1') if provider == 'claude-code' else ('workspace-private', 'user-private')
    for account in (first, second):
        _identity_file(account['source_home'], provider, *identity)
    reads = []

    async def discover(provider, **kwargs):
        reads.append(kwargs['account_id'])
        if len(reads) == 1:
            return AccountRefreshResult(provider, kwargs['account_id'], status='failed')
        from types import SimpleNamespace
        from connections.account_discovery_commit import commit_discovery_bundle
        from infra.db_async import run_in_db

        return await run_in_db(commit_discovery_bundle, provider, SimpleNamespace(usage_report=None, cache_stage=None, unlisted_errors={}),
            [{'selector_id': 'rechecked', 'available': True, 'context_window': 200000}],
            context=SimpleNamespace(receipt=kwargs['expected_receipt'], account_id=kwargs['account_id']),
            harness_version='1')

    monkeypatch.setattr(harness_models, 'refresh_provider', discover)
    await account_controls.start_signin(first['id'])
    assert (await _finished(first['id']))['status'] == 'completed'
    await account_controls.start_signin(second['id'])
    outcome = await _finished(second['id'])
    assert outcome['status'] == 'failed'
    assert outcome['cause'] == 'same_account'
    assert outcome['duplicate_of'] == first['id']
    assert reads == [first['id'], first['id']]
    from llm.model_status import scoped_status_rows

    row = next(row for row in scoped_status_rows(account_id=first['id']) if row['id'] == provider + '/rechecked')
    assert next(entry for entry in row['runs_on'] if entry['account_id'] == first['id'])['status'] == 'ready'


@pytest.mark.asyncio
@both
async def test_startup_removes_only_unreferenced_never_signed_in_accounts(scripted_vendor_clis, provider):
    from connections.account_leftovers import remove_startup_leftovers
    from connections.provider_policy import effective_policy, set_policy
    from connections.vendor_accounts import add_account, list_accounts
    from infra.database import get_db

    leftover = scripted_vendor_clis[provider]
    disabled = add_account(provider, 'Disabled')
    prior = add_account(provider, 'Previously signed in')
    prior = set_account_state(prior['id'], 'signed_in', expected_revision=prior['revision'])
    prior = set_account_state(prior['id'], 'signed_out', expected_revision=prior['revision'])
    default = scripted_vendor_clis['default'][provider]
    order = [leftover['id'], default['id'], disabled['id'], prior['id']]
    set_policy(None, provider, rollover=True, order=order, disabled=[disabled['id']], expected_revision=0)
    await remove_startup_leftovers()
    assert _account(leftover['id'])['state'] == 'removed'
    assert _account(disabled['id'])['state'] == 'signed_out'
    assert _account(prior['id'])['state'] == 'signed_out'
    assert leftover['id'] not in {row['id'] for row in list_accounts(provider=provider)}
    db = get_db()
    try:
        policy = effective_policy(None, provider, db=db)
    finally:
        db.close()
    saved = set_policy(None, provider, rollover=policy['rollover'], order=policy['order'],
                       disabled=policy['disabled'], expected_revision=policy['revision'])
    assert leftover['id'] not in saved['order']


@pytest.mark.asyncio
@both
async def test_startup_sweep_survives_one_accounts_failure(scripted_vendor_clis, provider, monkeypatch):
    from connections import account_leftovers
    from connections.vendor_accounts import add_account

    failing = scripted_vendor_clis[provider]
    other = add_account(provider, 'Other')
    real = account_leftovers.commit_if_leftover

    def commit(account_id, *, db=None):
        if account_id == failing['id']:
            raise RuntimeError('unexpected row')
        return real(account_id, db=db)

    monkeypatch.setattr(account_leftovers, 'commit_if_leftover', commit)
    await account_leftovers.remove_startup_leftovers()
    assert _account(failing['id'])['state'] == 'signed_out'
    assert _account(other['id'])['state'] == 'removed'


@pytest.mark.asyncio
@both
async def test_startup_keeps_account_with_failed_cleanup(scripted_vendor_clis, provider):
    from connections.account_leftovers import remove_startup_leftovers
    from connections.vendor_accounts import record_auth_cleanup

    account = scripted_vendor_clis[provider]
    record_auth_cleanup(account['id'], {'status': 'failed', 'cause': 'vendor_logout_failed', 'at': '2026-01-01T00:00:00Z'})
    await remove_startup_leftovers()
    assert _account(account['id'])['state'] == 'signed_out'


@pytest.mark.asyncio
@both
async def test_cancel_keeps_a_never_signed_in_account_referenced_by_policy(scripted_vendor_clis, provider, monkeypatch, tmp_path):
    from connections.account_controls import cancel_signin, start_signin
    from connections.provider_policy import set_policy

    account = scripted_vendor_clis[provider]
    set_policy(None, provider, rollover=False, order=None, disabled=[account['id']], expected_revision=0)
    monkeypatch.setenv('LOGIN_HOLD', str(tmp_path / 'never'))
    await start_signin(account['id'])
    assert (await cancel_signin(account['id']))['status'] == 'cancelled'
    assert _account(account['id'])['state'] == 'signed_out'


@pytest.mark.parametrize(('report', 'code', 'expected'), [
    ({'loggedIn': False}, 0, 'signed_out'),
    ({'loggedIn': False}, 1, 'signed_out'),
    ({'loggedIn': False}, 2, 'unknown'),
    ({'loggedIn': True}, 0, 'signed_in'),
    ({'loggedIn': True}, 1, 'unknown'),
    ({'loggedIn': 0}, 0, 'unknown'),
    (None, 1, 'unknown'),
])
def test_claude_login_status_classification(report, code, expected):
    from connections.account_identity import claude_login_status

    assert claude_login_status(report, code) == expected


@pytest.mark.real_harness_cli
@pytest.mark.asyncio
@both
async def test_default_visibility_follows_its_own_signin_state(scripted_vendor_clis, provider):
    from api.vendor_accounts import list_vendor_accounts
    from connections import account_controls

    default_id = scripted_vendor_clis['default'][provider]['id']

    async def shown():
        rows = (await list_vendor_accounts(provider=provider))['accounts']
        return next(row for row in rows if row['id'] == default_id)

    assert (await shown())['state'] == 'signed_out'
    await account_controls.start_signin(default_id)
    assert (await _finished(default_id))['status'] == 'completed'
    signed_in = await shown()
    assert signed_in['state'] == 'signed_in'
    await account_controls.sign_out(default_id, expected_revision=signed_in['revision'])
    assert (await shown())['state'] == 'signed_out'
