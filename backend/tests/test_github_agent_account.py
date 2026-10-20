"""The agents' own GitHub account: one view from committed rows, its readers, sign-in and shell wiring.

Every ``gh`` here is a synthetic script: no real credential, login, keychain or network is touched.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from connections import github_agent, github_person

BOT = "corsia-agent-bot"
BOT_ID = 4242
PERSON = "person-host"
_PR_BODY = "Owner thread ID: 1eaa12f5"

# The stored credential line is assembled at run time, so no source line carries one.
_GH = r"""#!/bin/sh
echo "${GH_CONFIG_DIR:-unset}|$*" >> "$VENDOR_LOG"
case "$*" in
  "auth status"*)
    if [ -f "$GH_CONFIG_DIR/hosts.yml" ]; then
      login=$(sed -n 's/^ *user: //p' "$GH_CONFIG_DIR/hosts.yml" | head -1)
      source="${TOKEN_SOURCE:-keyring}"
      printf '{"hosts":{"github.com":[{"state":"success","active":true,"host":"github.com","login":"%s","tokenSource":"%s"}]}}\n' "$login" "$source"
    else
      echo '{"hosts":{}}'
    fi
    exit 0;;
  "auth login"*)
    case "${LOGIN_MODE:-prompt}" in
      colored_prompt) printf '\033[33m!\033[0m First copy your one-time code: \033[1mABCD-\r1234\033[0m\n' >&2;;
      unrecognized) printf 'GitHub CLI is waiting for a browser response\n' >&2;;
      early_exit) printf 'GitHub CLI could not start device login\n' >&2; exit 1;;
      *)
        echo "! First copy your one-time code: ABCD-1234"
        echo "Open this URL to continue in your web browser: https://github.com/login/device";;
    esac
    i=0
    while [ ! -f "$APPROVE" ] && [ $i -lt 100 ]; do sleep 0.05; i=$((i+1)); done
    [ -f "$APPROVE" ] || exit 1
    mkdir -p "$GH_CONFIG_DIR"
    case "${LOGIN_STORAGE:-keyring}" in
      plaintext) printf 'github.com:\n    user: %s\n    oauth_%s: fake\n' "$(cat "$APPROVE")" token > "$GH_CONFIG_DIR/hosts.yml";;
      *) printf 'github.com:\n    user: %s\n' "$(cat "$APPROVE")" > "$GH_CONFIG_DIR/hosts.yml";;
    esac
    exit 0;;
  "auth logout"*)
    [ "${LOGOUT_MODE:-ok}" = fail ] && exit 1
    [ -f "$GH_CONFIG_DIR/hosts.yml" ] || exit 1
    rm -f "$GH_CONFIG_DIR/hosts.yml"; exit 0;;
  "api user"*)
    case "${API_MODE:-ok}" in
      401) echo "HTTP 401: Bad credentials (https://api.github.com/user)" >&2; exit 1;;
      down) echo "error connecting to api.github.com" >&2; exit 1;;
    esac
    [ -n "$API_GATE" ] && while [ ! -f "$API_GATE" ]; do sleep 0.02; done
    login=$(sed -n 's/^ *user: //p' "$GH_CONFIG_DIR/hosts.yml" | head -1)
    printf '{"login":"%s","id":4242}\n' "$login"; exit 0;;
esac
exit 3
"""


@pytest.fixture
def agent(isolated_db, monkeypatch, tmp_path):
    """A fake ``gh``, an allowed agents' home root, and the person signed in as ``PERSON``."""
    from connections.models import upsert_connection

    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "vendor.log"
    monkeypatch.setenv("VENDOR_LOG", str(log))
    monkeypatch.setenv("APPROVE", str(tmp_path / "approve"))
    monkeypatch.setenv(github_agent_home().HOME_ENV, str(tmp_path / "agents-home"))
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "user-gh"))
    github_agent.reset_for_tests()
    github_person.reset_for_tests()
    upsert_connection(type="cli", provider="github", status="available",
                      config={"binary": str(gh), "login": PERSON})

    async def _fake_gh(provider):
        return str(gh) if provider == "github" else None

    # The suite-wide guard finds no vendor binary; here the synthetic gh is the only one.
    monkeypatch.setattr("connections.account_controls._binary", _fake_gh)
    yield SimpleNamespace(gh=gh, log=log, approve=tmp_path / "approve", user_home=tmp_path / "user-gh")
    github_agent.reset_for_tests()


def github_agent_home():
    from connections import github_agent_home as home

    return home


def _add():
    from connections.vendor_accounts import add_account

    return add_account("github", "Agents")


def _write_credential(account, login: str = BOT) -> None:
    home = Path(account["source_home"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "hosts.yml").write_text(f"github.com:\n    user: {login}\n")


def _signed_in(login: str = BOT):
    from connections.vendor_accounts import set_account_state

    account = _add()
    _write_credential(account, login)
    return set_account_state(account["id"], "signed_in", expected_revision=account["revision"])


def _ready_view():
    return github_agent.verify_now()


def _set_person(login: str | None) -> None:
    from connections.models import get_connection, upsert_connection

    row = get_connection("github")
    config = {key: value for key, value in row["config"].items() if key != "login"}
    if login is not None:
        config["login"] = login
    upsert_connection(type="cli", provider="github", status=row["status"], config=config)


# -- add ------------------------------------------------------------------------------------------------


def test_add_reads_needs_auth_and_locks_shell(agent):
    account = _add()
    view = github_agent.view()
    assert (view.state, view.cause, view.absent) == ("needs_auth", "signed_out", False)
    env = github_agent.shell_env(view)
    assert env["GH_CONFIG_DIR"] == github_agent_home().LOCKED_HOME
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert pairs["credential.helper"] == "" and pairs["credential.https://github.com.helper"] == ""
    assert pairs["user.name"] == "" and pairs["user.email"] == ""
    assert github_agent.sandbox_ro_dirs(view) == []
    assert github_agent.default_home_deny(view) == agent.user_home
    assert Path(account["source_home"]).is_dir()


def test_add_refuses_second_row(agent):
    from connections.vendor_accounts import AccountError

    _add()
    with pytest.raises(AccountError, match="already exists"):
        _add()


def test_two_existing_rows_fail_closed(agent, isolated_db):
    """An instance that ran the earlier design may hold two rows; neither is used."""
    from infra.database import get_db

    account = _signed_in()
    db = get_db()
    db.execute("INSERT INTO vendor_accounts (id,connection_id,name,source_home,runtime_home,state,identity_json,"
               "created_at,updated_at) SELECT 'second',connection_id,'Second',source_home||'2',runtime_home||'2',"
               "'signed_in','{}',created_at,updated_at FROM vendor_accounts WHERE id=?", (account["id"],))
    db.commit()
    view = github_agent.verify_now()
    assert (view.state, view.cause) == ("needs_auth", "multiple_accounts")
    assert github_agent.shell_env(view)["GH_CONFIG_DIR"] == github_agent_home().LOCKED_HOME


def test_no_account_leaves_shells_as_before(agent):
    view = github_agent.view()
    assert view.absent and view.state == "not_installed"
    assert github_agent.shell_env(view) == {}
    assert github_agent.default_home_deny(view) is None
    assert github_agent.source_state(view) == {"state": "not_installed"}


# -- product sign-in ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signin_exposes_only_user_code(agent):
    from connections import account_controls

    account = await asyncio.to_thread(_add)
    try:
        started = await account_controls.start_signin(account["id"])
        assert started["status"] == "waiting"
        shown = await account_controls.wait_for_prompt(account["id"], timeout=5)
        assert shown["user_code"] == "ABCD-1234"
        assert shown["verification_uri"] == "https://github.com/login/device"
        assert set(shown) == {"account_id", "route", "status", "cause", "user_code", "verification_uri"}
        assert "Open this URL" not in json.dumps(shown)
    finally:
        await account_controls.cancel_signin(account["id"])


def test_signin_prompt_accepts_gh_291_colored_stderr_bytes():
    """Match gh's stderr device-flow form when terminal formatting reaches the pipe."""
    from connections import github_agent_home

    observed = b"\x1b[33m!\x1b[0m First copy your one-time code: \x1b[1mABCD-\r1234\x1b[0m\n"
    assert github_agent_home.signin_prompt(observed.decode()) == {
        "user_code": "ABCD-1234", "verification_uri": github_agent_home.DEVICE_URL,
    }


@pytest.mark.asyncio
async def test_connect_reads_gh_291_colored_stderr_prompt(agent, monkeypatch):
    from connections import account_controls
    from tools.github_agent_account import GitHubAgentAccountTool

    monkeypatch.setenv("LOGIN_MODE", "colored_prompt")
    tool = GitHubAgentAccountTool()
    connected = json.loads(await tool.execute(action="connect"))
    try:
        assert connected["signin"]["user_code"] == "ABCD-1234"
        assert connected["signin"]["verification_uri"] == "https://github.com/login/device"
    finally:
        await account_controls.cancel_signin(connected["signin"]["account_id"])


@pytest.mark.asyncio
async def test_connect_and_check_name_an_unrecognized_prompt(agent, monkeypatch):
    from connections import account_controls
    from tools import github_agent_account

    monkeypatch.setenv("LOGIN_MODE", "unrecognized")
    monkeypatch.setattr(github_agent_account, "_PROMPT_SECONDS", 0.01)
    tool = github_agent_account.GitHubAgentAccountTool()
    connected = json.loads(await tool.execute(action="connect"))
    account_id = connected["signin"]["account_id"]
    try:
        assert connected["signin"] == {
            "account_id": account_id, "route": "browser", "status": "waiting",
            "cause": "prompt_unrecognized", "next": github_agent_account._PROMPT_NEXT,
        }
        checked = json.loads(await tool.execute(action="check"))
        assert checked["signin"] == connected["signin"]
    finally:
        await account_controls.cancel_signin(account_id)


@pytest.mark.asyncio
async def test_connect_and_check_name_early_login_exit(agent, monkeypatch):
    from tools import github_agent_account

    monkeypatch.setenv("LOGIN_MODE", "early_exit")
    tool = github_agent_account.GitHubAgentAccountTool()
    connected = json.loads(await tool.execute(action="connect"))
    account_id = connected["signin"]["account_id"]
    assert connected["signin"] == {
        "account_id": account_id, "route": "browser", "status": "failed",
        "cause": "vendor_login_failed", "next": github_agent_account._LOGIN_FAILURE_NEXT,
    }
    checked = json.loads(await tool.execute(action="check"))
    assert checked["signin"] == connected["signin"]


@pytest.mark.asyncio
async def test_signin_completed_check_returns_ready(agent):
    from connections import account_controls
    from tools.github_agent_account import GitHubAgentAccountTool

    tool = GitHubAgentAccountTool()
    connected = json.loads(await tool.execute(action="connect"))
    assert connected["signin"]["user_code"] == "ABCD-1234"
    agent.approve.write_text(BOT)
    account_id = connected["signin"]["account_id"]
    task = account_controls._SIGNINS[account_id].task
    await asyncio.wait_for(asyncio.shield(task), 10)
    checked = json.loads(await tool.execute(action="check"))
    assert checked["signin"]["status"] == "completed"
    assert checked["account"] == {"state": "ready", "login": BOT, "account_id": account_id}
    # The login ran with the profile's one sign-in command, in the agents' home only.
    login_line = next(line for line in agent.log.read_text().splitlines() if "|auth login" in line)
    assert "--insecure-storage" in login_line and "--scopes workflow" in login_line
    assert not login_line.startswith(str(agent.user_home))


@pytest.mark.asyncio
async def test_signin_as_the_person_is_refused_and_logged_out(agent):
    from connections import account_controls
    from connections.vendor_accounts import get_account

    account = await asyncio.to_thread(_add)
    await account_controls.start_signin(account["id"])
    agent.approve.write_text(PERSON.upper())  # GitHub logins compare without case
    operation = account_controls._SIGNINS[account["id"]]
    await asyncio.wait_for(asyncio.shield(operation.task), 10)
    assert (operation.status, operation.cause) == ("failed", "person_account")
    assert get_account(account["id"])["state"] == "signed_out"
    assert not (Path(account["source_home"]) / "hosts.yml").exists()


@pytest.mark.asyncio
async def test_signin_in_corsia_owned_storage_connects(agent):
    from connections import account_controls

    account = await asyncio.to_thread(_add)
    await account_controls.start_signin(account["id"])
    agent.approve.write_text(BOT)
    operation = account_controls._SIGNINS[account["id"]]
    await asyncio.wait_for(asyncio.shield(operation.task), 10)
    assert (operation.status, operation.cause) == ("completed", None)


@pytest.mark.asyncio
async def test_plaintext_signin_in_corsia_owned_storage_connects(agent, monkeypatch):
    from connections import account_controls

    account = await asyncio.to_thread(_add)
    monkeypatch.setenv("LOGIN_STORAGE", "plaintext")
    monkeypatch.setenv("TOKEN_SOURCE", str(Path(account["source_home"]) / "hosts.yml"))
    await account_controls.start_signin(account["id"])
    agent.approve.write_text(BOT)
    operation = account_controls._SIGNINS[account["id"]]
    await asyncio.wait_for(asyncio.shield(operation.task), 10)
    assert (operation.status, operation.cause) == ("completed", None)
    assert (Path(account["source_home"]) / "hosts.yml").exists()


@pytest.mark.asyncio
async def test_connect_logs_out_a_stale_login_first(agent):
    from connections import account_controls

    account = await asyncio.to_thread(_add)
    await asyncio.to_thread(_write_credential, account, "stale-login")
    await account_controls.start_signin(account["id"])
    try:
        # The prompt is printed after the login's log line, so both calls are logged by then.
        await account_controls.wait_for_prompt(account["id"], timeout=5)
        calls = [line.split("|", 1)[1] for line in agent.log.read_text().splitlines()]
        assert calls[0].startswith("auth logout") and calls[1].startswith("auth login")
        assert not (Path(account["source_home"]) / "hosts.yml").exists()
    finally:
        await account_controls.cancel_signin(account["id"])


def test_login_command_uses_corsia_owned_storage_and_workflow_scope():
    from connections.account_profiles import profile_for

    login = profile_for("github").commands["login"]
    assert "--insecure-storage" in login and "--web" in login
    assert login[login.index("--scopes") + 1] == "workflow"


# -- shell sign-in, sign-out, removal ---------------------------------------------------------------------


def test_shell_login_cannot_write_home_ro_grant(agent, tmp_path, monkeypatch):
    """The verified home reaches a shell only through the read-only grant, never the read/write one."""
    from tools import shell_guard

    _signed_in()
    view = _ready_view()
    assert view.ready
    safehouse = tmp_path / "safehouse"
    safehouse.write_text("")
    monkeypatch.setattr(shell_guard.shutil, "which", lambda _name: str(safehouse))
    monkeypatch.setattr(shell_guard, "get_sandbox_add_dirs_rw", lambda _thread_id, policies=None: [tmp_path / "rw"])
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_ro", lambda policies=None: [])
    argv = shell_guard.safehouse_argv("true", tmp_path, tmp_path / "p.sb", agent_account=view)
    home = str(Path(view.home).resolve())
    assert f"--add-dirs-ro={home}" in argv
    assert not any(part.startswith("--add-dirs=") and home in part for part in argv)


def test_external_hosts_yml_on_signed_out_row_stays_locked(agent):
    account = _add()
    _write_credential(account)
    view = github_agent.verify_now()
    assert (view.state, view.cause) == ("needs_auth", "signed_out")
    assert github_agent.shell_env(view)["GH_CONFIG_DIR"] == github_agent_home().LOCKED_HOME
    assert github_agent.sandbox_ro_dirs(view) == []


@pytest.mark.asyncio
async def test_verify_signed_out_locks_next_spawn_without_hooks(agent):
    from connections import account_controls

    account = await asyncio.to_thread(_signed_in)
    assert (await asyncio.to_thread(_ready_view)).ready
    (Path(account["source_home"]) / "hosts.yml").unlink()
    result = await account_controls.verify(account["id"])
    assert (result["vendor_signed_in"], result["state"]) == (False, "signed_out")
    view = github_agent.view()
    assert view.state == "needs_auth" and not github_agent.sandbox_ro_dirs(view)


@pytest.mark.asyncio
async def test_removed_row_is_not_installed_and_ungranted(agent):
    from connections import account_controls

    account = await asyncio.to_thread(_signed_in)
    assert (await asyncio.to_thread(_ready_view)).ready
    await account_controls.remove(account["id"], expected_revision=account["revision"])
    view = github_agent.view()
    assert view.absent and view.state == "not_installed"
    assert github_agent.sandbox_ro_dirs(view) == [] and github_agent.shell_env(view) == {}


# -- revoked token and failed verification ---------------------------------------------------------------


def test_definite_401_drops_identity(agent, monkeypatch):
    _signed_in()
    assert _ready_view().ready
    monkeypatch.setenv("API_MODE", "401")
    view = github_agent.verify_now()
    assert (view.state, view.cause) == ("needs_auth", "token_rejected")
    assert github_agent.shell_env(view)["GH_CONFIG_DIR"] == github_agent_home().LOCKED_HOME


def test_network_failure_keeps_verified_same_fingerprint(agent, monkeypatch):
    _signed_in()
    assert _ready_view().ready
    monkeypatch.setenv("API_MODE", "down")
    view = github_agent.verify_now()
    assert view.ready and view.login == BOT


def test_failed_verify_retries_after_backoff(agent, monkeypatch):
    _signed_in()
    monkeypatch.setenv("API_MODE", "down")
    assert github_agent.verify_now().cause == "verify_failed"
    monkeypatch.delenv("API_MODE")
    runs = len(agent.log.read_text().splitlines())
    assert github_agent.view().cause == "verify_failed"  # inside the backoff: no new run
    assert len(agent.log.read_text().splitlines()) == runs
    for key in list(github_agent._attempts):
        github_agent._attempts[key] -= github_agent.RETRY_SECONDS
    github_agent.view()  # due again: starts one verification
    _wait(lambda: github_agent.view().ready)


def test_db_error_locks_shell(agent, monkeypatch):
    def broken(_db):
        raise OSError("database is locked")

    monkeypatch.setattr(github_agent, "_read", broken)
    view = github_agent.view()
    assert (view.state, view.cause, view.absent) == ("unverified", "read_failed", False)
    assert github_agent.shell_env(view)["GH_CONFIG_DIR"] == github_agent_home().LOCKED_HOME
    assert github_agent.default_home_deny(view) == agent.user_home


# -- the person's own login --------------------------------------------------------------------------------


def test_unknown_person_login_never_ready(agent, monkeypatch):
    probes = []
    monkeypatch.setattr(github_agent, "request_probe", lambda: probes.append(True))
    _set_person(None)
    _signed_in()
    view = github_agent.verify_now()
    assert (view.state, view.cause) == ("unverified", "own_login_unknown")
    assert github_agent.agent_login(view) is None and probes


def test_persisted_person_login_survives_cold_start(agent):
    _signed_in()
    assert _ready_view().ready
    github_agent.reset_for_tests()  # a restart forgets every verification
    _wait(lambda: github_agent.view().ready)  # the stored login answers at once; only GitHub is re-asked


def test_same_login_case_insensitive_is_wrong_account(agent):
    _signed_in(login=PERSON.upper())
    view = github_agent.verify_now()
    assert (view.state, view.cause) == ("wrong_account", "person_account")
    assert github_agent.shell_env(view)["GH_CONFIG_DIR"] == github_agent_home().LOCKED_HOME


# -- the cache ----------------------------------------------------------------------------------------------


def test_fingerprint_change_is_new_key(agent):
    account = _signed_in()
    assert _ready_view().login == BOT
    time.sleep(0.01)
    _write_credential(account, "corsia-other-bot")
    view = github_agent.view()
    assert (view.state, view.cause) == ("unverified", "verifying")
    _wait(lambda: github_agent.view().login == "corsia-other-bot")


def test_old_key_result_never_serves_new_revision(agent, monkeypatch, tmp_path):
    """A verification started for an old revision that finishes last never answers for the new one."""
    from connections.vendor_accounts import set_account_state

    gate = tmp_path / "gate"
    monkeypatch.setenv("API_GATE", str(gate))
    account = _signed_in()
    github_agent.view()  # starts revision 1's verification, held at the gate
    old_keys = set(github_agent._inflight)
    signed_out = set_account_state(account["id"], "signed_out", expected_revision=account["revision"])
    set_account_state(account["id"], "signed_in", expected_revision=signed_out["revision"])
    gate.write_text("open")
    for future in [github_agent._inflight.get(key) for key in old_keys]:
        if future is not None:
            future.result(timeout=10)
    view = github_agent.view()
    assert not view.ready  # the old revision's result is not this revision's answer
    _wait(lambda: github_agent.view().ready)


def test_concurrent_readers_join_single_flight(agent, monkeypatch, tmp_path):
    gate = tmp_path / "gate"
    monkeypatch.setenv("API_GATE", str(gate))
    _signed_in()
    views = []
    joined = []
    start = github_agent._start

    def counting_start(*args):
        future = start(*args)
        joined.append(future)
        return future

    monkeypatch.setattr(github_agent, "_start", counting_start)
    readers = [threading.Thread(target=lambda: views.append(github_agent.verify_now())) for _ in range(4)]
    for reader in readers:
        reader.start()
    # Every reader must be in the flight before it can finish; a reader that arrives after it runs its own.
    _wait(lambda: len(joined) == 4)
    assert len(set(joined)) == 1
    gate.write_text("open")
    for reader in readers:
        reader.join(15)
    assert [view.state for view in views] == ["ready"] * 4
    api_calls = [line for line in agent.log.read_text().splitlines() if line.endswith("|api user")]
    assert len(api_calls) == 1


# -- one view per spawn ---------------------------------------------------------------------------------------


def test_spawn_env_grant_and_deny_from_one_view(agent, tmp_path, monkeypatch):
    from tools import shell_guard

    _signed_in()
    view = _ready_view()
    safehouse = tmp_path / "safehouse"
    safehouse.write_text("")
    monkeypatch.setattr(shell_guard.shutil, "which", lambda _name: str(safehouse))
    monkeypatch.setattr(shell_guard, "get_sandbox_add_dirs_rw", lambda _thread_id, policies=None: [])
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_ro", lambda policies=None: [])
    monkeypatch.setattr(github_agent, "view", lambda db=None: pytest.fail("a spawn reads its view once"))
    argv = shell_guard.safehouse_argv("true", tmp_path, tmp_path / "p.sb", agent_account=view)
    rules = shell_guard._terminal_boundary_profile(tmp_path / "p.sb", tmp_path, leased=False, agent_account=view)
    env = github_agent.shell_env(view)
    assert f"--add-dirs-ro={Path(view.home).resolve()}" in argv
    assert f'(deny file-read* file-write* (subpath "{agent.user_home.resolve()}"))' in rules
    assert env["GH_CONFIG_DIR"] == view.home
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert pairs["user.name"] == BOT
    assert pairs["user.email"] == f"{BOT_ID}+{BOT}@users.noreply.github.com"
    assert pairs["credential.https://github.com.helper"] == f"!'{agent.gh}' auth git-credential"
    passed = next(part for part in argv if part.startswith("--env-pass="))
    assert set(env) <= set(passed.removeprefix("--env-pass=").split(","))


@pytest.mark.asyncio
async def test_bash_spawn_reads_one_view(agent, tmp_path, monkeypatch):
    from core.turn_core import TurnIdentity
    from tools import host_access_gate, shell

    await asyncio.to_thread(_signed_in)
    ready = await asyncio.to_thread(_ready_view)
    reads, seen = [], {}
    monkeypatch.setattr(github_agent, "view", lambda db=None: reads.append(True) or ready)

    async def consume(**_kwargs):
        return None

    def render_profile(command, cwd, profile_path, **kwargs):
        seen["profile"] = kwargs["agent_account"]
        return "(version 1)\n"

    def build_argv(command, cwd, profile_path, **kwargs):
        seen["argv"] = kwargs["agent_account"]
        return ["/opt/homebrew/bin/safehouse", "--", "/bin/bash", "-c", command]

    async def spawn(argv, **kwargs):
        seen["env"] = kwargs["env"]
        return shell.ForegroundProcessResult(returncode=0, stdout=b"", stderr=b"", timed_out=False)

    monkeypatch.setattr(host_access_gate, "consume_current_action_permit", consume)
    monkeypatch.setattr(shell, "list_registered_repo_policies", lambda *a, **k: [])
    monkeypatch.setattr(shell, "controlled_fast_forward_policy", lambda *_a, **_k: None)
    monkeypatch.setattr(shell, "is_safehouse_available", lambda: True)
    monkeypatch.setattr(shell, "safehouse_version_error", lambda: None)
    monkeypatch.setattr(shell, "is_path_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(shell, "execution_envelope_profile", render_profile)
    monkeypatch.setattr(shell, "safehouse_argv", build_argv)
    monkeypatch.setattr(shell.PROCESS_OWNER, "spawn", spawn)
    monkeypatch.setattr(shell.BashTool, "_guard_command", lambda self, command, cwd: None)

    await shell.BashTool(working_dir=str(tmp_path), default_timeout=30).execute(
        "true", _thread_id="thread-1", _thread_project_id=None,
        _host_access_turn_identity=TurnIdentity("turn-1", 1),
    )
    assert reads == [True]
    assert seen["profile"] is ready and seen["argv"] is ready
    assert seen["env"]["GH_CONFIG_DIR"] == ready.home


# -- readers elsewhere -------------------------------------------------------------------------------------------


def test_ownership_recognizes_the_verified_agents_login(agent, monkeypatch):
    from events.connectors import github_ownership

    monkeypatch.setattr(github_ownership, "cached_own_login", lambda: PERSON)
    payload = {"pull_request": {"user": {"login": BOT}, "body": _PR_BODY}}
    assert github_ownership.declaration_facts(payload) == {}
    _signed_in()
    assert _ready_view().ready
    assert github_ownership.declaration_facts(payload) == {"declared_owner_thread": "1eaa12f5"}


@pytest.mark.asyncio
async def test_model_sources_reports_the_agents_account(agent):
    from tools.model_sources import ModelSourcesTool

    await asyncio.to_thread(_signed_in)
    body = json.loads(await ModelSourcesTool().execute(recheck=False))
    assert body["github_agent"]["state"] in {"unverified", "ready"}
    await asyncio.to_thread(_ready_view)
    body = json.loads(await ModelSourcesTool().execute())
    assert body["github_agent"] == {"state": "ready", "login": BOT}


def test_github_agent_import_layers():
    """Each layer imports only downward, function-local imports included."""
    root = Path(__file__).resolve().parents[1]
    forbidden = {
        "connections/github_agent_home.py": {"connections.github_agent", "connections.github_person",
                                             "connections.account_profiles", "connections.account_controls"},
        "connections/github_person.py": {"connections.github_agent", "connections.account_profiles",
                                         "connections.account_controls", "tools"},
        "connections/account_profiles.py": {"connections.github_agent"},
        "connections/vendor_accounts.py": {"connections.github_agent"},
        "connections/account_controls.py": {"connections.github_agent"},
        "connections/__init__.py": {"connections.github_agent", "connections.github_person"},
        "connections/github_agent.py": {"tools", "events", "connections.account_controls"},
    }
    for relative, banned in forbidden.items():
        tree = ast.parse((root / relative).read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
            for name in names:
                assert not any(name == ban or name.startswith(ban + ".") for ban in banned), (relative, name)


def test_clean_env_drops_host_tokens_and_identity(agent, monkeypatch):
    home = github_agent_home()
    for name in (*home.TOKEN_NAMES, "GH_HOST", "GIT_AUTHOR_NAME", "GIT_CONFIG_COUNT"):
        monkeypatch.setenv(name, "host")
    env = home.clean_env()
    assert not {*home.TOKEN_NAMES, "GH_CONFIG_DIR", "GH_HOST", "GIT_AUTHOR_NAME", "GIT_CONFIG_COUNT"} & set(env)
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and "PATH" in env


def test_shell_env_strips_host_identity(agent, monkeypatch):
    from tools.shell_environment import safehouse_subprocess_env

    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "person@example.com")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'credential.helper'='osxkeychain'")
    env = safehouse_subprocess_env("thread-1")
    assert "GIT_AUTHOR_EMAIL" not in env and "GIT_CONFIG_PARAMETERS" not in env and "GH_CONFIG_DIR" not in env


def _wait(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.02)
