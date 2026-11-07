"""A script's GitHub identity is its owner's shell's: the verified agents' account, else locked.

Runs ``run_script`` through the real envelope builders up to the spawn, for each account
state. Every view is a fake and every token value a synthetic canary; no real ``gh``,
login or credential is touched.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from automation.script_sandbox import _folder_bytecode_deny
from connections import github_agent
from connections.github_agent import AgentAccountView
from connections.github_agent_home import BOT_ENV_NAMES, LOCKED_HOME

THREAD = "script-gh-thread"
_TOKENS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GIT_ASKPASS")
_LOCKED_GIT = [
    ("credential.helper", ""),
    ("credential.https://github.com.helper", ""),
    ("url.https://github.com/.insteadOf", "git@github.com:"),
    ("url.https://github.com/.insteadOf", "ssh://git@github.com/"),
    ("url.https://github.com/.pushInsteadOf", "https://github.com/"),
    ("user.name", ""),
    ("user.email", ""),
    ("user.useConfigOnly", "true"),
]


def _views(agent_home: Path) -> dict[str, AgentAccountView]:
    base = {"account_id": "acct-1", "home": str(agent_home), "binary": "/opt/agent/gh"}
    return {
        "ready": AgentAccountView("ready", **base, login="agents-bot", user_id=77),
        "person": AgentAccountView("wrong_account", "person_account", **base),
        "unverified": AgentAccountView("unverified", "own_login_unknown", **base),
        "verifying": AgentAccountView("unverified", "verifying", **base),
        "suspended": AgentAccountView("needs_auth", "token_rejected", **base),
        "absent": AgentAccountView("not_installed", absent=True),
    }


@pytest.fixture
def run(monkeypatch, tmp_path):
    """``run_script`` through the real profile and argv builders; returns what the spawn received."""
    from automation import script_sandbox
    from host_access.processes import ForegroundProcessResult
    from tests.shell_platform_helpers import use_mac_shell
    from tools import shell_guard

    home = tmp_path / "home"
    person_gh = tmp_path / "person-gh"
    agent_home = tmp_path / "agent-gh"
    for path in (home, person_gh, agent_home):
        path.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    # The host's own gh home and a token in every name gh or Git reads one from.
    monkeypatch.setenv("GH_CONFIG_DIR", str(person_gh))
    for name in _TOKENS:
        monkeypatch.setenv(name, "synthetic-canary")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "osxkeychain")
    use_mac_shell(monkeypatch)
    safehouse = tmp_path / "safehouse"
    safehouse.write_text("")
    monkeypatch.setattr(shell_guard, "_safehouse_executable", lambda: safehouse)
    monkeypatch.setattr(shell_guard, "pinned_repo_sandbox_profile", lambda *_a, **_k: "")
    monkeypatch.setattr(shell_guard, "get_sandbox_add_dirs_rw", lambda _thread_id, policies=None: [])
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_ro", lambda policies=None: [])
    monkeypatch.setattr(shell_guard, "agent_tool_ro_dirs", lambda: [])
    seen: dict = {}

    async def nothing(*_a, **_k):
        return None

    async def cwd(*_a, **_k):
        return str(tmp_path), None

    async def prepare(command, cwd_, envelope):
        seen["command"] = command
        profile = tmp_path / "p.sb"
        if envelope.profile_required:  # Linux runs with no profile, as ``_write_envelope`` does
            seen["profile"] = envelope.build_profile(command, cwd_, profile, **envelope.profile_options)
        seen["argv"] = envelope.build_argv(command, cwd_, profile, **envelope.argv_options)
        return None, ["true"]

    async def spawn(_argv, **kwargs):
        seen["env"] = kwargs["env"]
        return ForegroundProcessResult(None, b"", b"", 0)

    monkeypatch.setattr(script_sandbox, "_checked_environment", nothing)
    monkeypatch.setattr(script_sandbox, "load_shell_repo_policies", lambda *_a: ([], []))
    monkeypatch.setattr(script_sandbox, "_resolve_execution_cwd", cwd)
    monkeypatch.setattr(script_sandbox, "prepare_execution_envelope", prepare)
    monkeypatch.setattr(script_sandbox.shell_sandbox, "recheck_after", nothing)
    monkeypatch.setattr(script_sandbox.PROCESS_OWNER, "spawn", spawn)
    views = _views(agent_home)

    def go(state: str, verified: str | None = None, *, authenticated: bool = False):
        reads: list[bool] = []
        waits: list[bool] = []
        view = views[state]
        answer = views[verified or state]

        def read(db=None):
            reads.append(True)
            return view

        def verify(db=None):
            waits.append(True)
            return answer

        monkeypatch.setattr(github_agent, "view", read)
        monkeypatch.setattr(github_agent, "verify_now", verify)
        if authenticated:
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def client(*_args):
                yield tmp_path, tmp_path / "socket", tmp_path / "placeholder", tmp_path

            monkeypatch.setattr(script_sandbox, "_authenticated_client", client)
        result = asyncio.run(script_sandbox.run_script(
            THREAD, None, tmp_path / "s.sh", run_id="r", timeout_seconds=5, authenticated=authenticated))
        return {**seen, "reads": len(reads), "waits": len(waits), "result": result, "view": answer,
                "person_gh": person_gh.resolve(), "agent_home": agent_home.resolve(),
                "home_gh": (home / ".config" / "gh").resolve()}

    return go


def _git(env: dict[str, str]) -> list[tuple[str, str]]:
    return [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(int(env["GIT_CONFIG_COUNT"]))]


def _deny(path: Path) -> str:
    return f'(deny file-read* file-write* (subpath "{path}"))'


def _read_only(argv: list[str]) -> list[str]:
    granted = next((part for part in argv if part.startswith("--add-dirs-ro=")), "--add-dirs-ro=")
    return [path for path in granted.removeprefix("--add-dirs-ro=").split(":") if path]


def test_unapproved_run_hides_even_a_verified_account(run):
    seen = run("ready")
    env = seen["env"]
    assert seen["reads"] == 1 and seen["waits"] == 0
    assert env["GH_CONFIG_DIR"] == LOCKED_HOME
    assert _git(env) == _LOCKED_GIT
    assert str(seen["agent_home"]) not in _read_only(seen["argv"])
    for home in (seen["person_gh"], seen["home_gh"], seen["agent_home"]):
        assert _deny(home) in seen["profile"]
    assert not any(name in env for name in _TOKENS)
    assert seen["result"].github_locked == (
        "scripts get no GitHub login: this script isn't approved to use the agents' GitHub account; "
        "only if it failed on a GitHub call that needs that account, schedule it with "
        "authenticated=true so the person can approve it")


def test_authenticated_run_keeps_ready_account_locked(run):
    seen = run("ready", authenticated=True)
    assert seen["result"].github_locked is None
    assert seen["env"]["GH_CONFIG_DIR"] != str(seen["agent_home"])
    assert _git(seen["env"]) == _LOCKED_GIT
    assert str(seen["agent_home"]) not in _read_only(seen["argv"])
    assert _deny(seen["agent_home"]) in seen["profile"]
    assert not any(name in seen["env"] for name in _TOKENS)


def test_only_an_authenticated_run_is_kept_off_python_bytecode_in_its_folder(run, tmp_path):
    authenticated = run("ready", authenticated=True)
    assert f"export PYTHONPYCACHEPREFIX={tmp_path / 'pycache'};" in authenticated["command"]
    assert authenticated["profile"].endswith(_folder_bytecode_deny(tmp_path))
    plain = run("ready")
    assert "PYTHONPYCACHEPREFIX" not in plain["command"]
    assert "__pycache__" not in plain["profile"]


def test_the_bytecode_deny_matches_the_folder_and_cache_in_any_case():
    cache = "__[Pp][Yy][Cc][Aa][Cc][Hh][Ee]__"
    assert _folder_bytecode_deny(Path('/w/a.b (c)+"d')) == (
        f'(deny file-read* file-write* (regex #"^/[Ww]/[Aa][.][Bb] .[Cc]...[Dd]/(.*/)?{cache}(/.*)?$"))\n')


def test_a_run_with_no_cached_answer_waits_for_the_verification(run):
    seen = run("verifying", verified="ready")  # nothing cached yet; the verification says ready
    assert seen["reads"] == 1 and seen["waits"] == 1
    assert seen["env"]["GH_CONFIG_DIR"] == LOCKED_HOME
    assert seen["result"].github_locked.endswith("so the person can approve it")


@pytest.mark.parametrize("state, reason", [
    ("person", "scripts get no GitHub login: the agents' GitHub sign-in is the person's own GitHub account; "
               "sign it out and sign in as the agents' own account"),
    ("unverified", "scripts get no GitHub login: CORSIA doesn't know the person's own GitHub login, so "
                   "it can't tell the agents' account from it; it checks again on a later run, and if the "
                   "person isn't signed in to GitHub CLI on this computer, they need to sign in"),
    ("suspended", "scripts get no GitHub login: GitHub rejected the sign-in for the agents' GitHub account; "
                  "sign it out and in again"),
    ("absent", "scripts get no GitHub login: there is no agents' GitHub account"),
])
def test_every_other_state_runs_the_script_in_the_shells_locked_state(run, state, reason):
    seen = run(state)
    env = seen["env"]
    assert seen["reads"] == 1
    assert seen["waits"] == 0  # only a pending verification is worth waiting for
    assert env["GH_CONFIG_DIR"] == LOCKED_HOME
    assert _git(env) == _LOCKED_GIT
    assert set(BOT_ENV_NAMES) <= set(env)
    assert str(seen["agent_home"]) not in _read_only(seen["argv"])
    for home in (seen["person_gh"], seen["home_gh"]):
        assert _deny(home) in seen["profile"]
    if not seen["view"].absent:
        assert _deny(seen["agent_home"]) in seen["profile"]
    assert not any(name in env for name in _TOKENS)
    passed = next(part for part in seen["argv"] if part.startswith("--env-pass="))
    assert set(BOT_ENV_NAMES) <= set(passed.removeprefix("--env-pass=").split(","))
    assert seen["result"].github_locked == reason


@pytest.mark.parametrize("state", ["ready", "unverified", "absent"])
def test_linux_scripts_hide_the_same_homes_from_the_same_view(run, monkeypatch, state):
    from tools import linux_shell, shell_envelope
    from types import SimpleNamespace

    captured = {}

    def shell_argv(command, cwd, _profile=None, **kwargs):
        captured.update(kwargs)
        return ["true"]

    monkeypatch.setattr(shell_envelope, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(linux_shell, "shell_argv", shell_argv)
    seen = run(state)
    assert not captured["agent_account"].ready
    hidden = set(captured["extra_hidden_roots"])
    assert {seen["person_gh"], seen["home_gh"]} <= hidden
    # An absent account has no home to hide; any recorded home stays hidden.
    assert (seen["agent_home"] in hidden) is (state != "absent")


def test_git_askpass_is_a_credential_name():
    from tools.shell_environment import is_credential_env_name

    assert all(is_credential_env_name(name) for name in _TOKENS)
