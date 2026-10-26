"""The agents' own GitHub account: one view read from committed rows, and its readers.

``view(db)`` is the only owner of the account's state. It reads the non-removed
``github`` account rows and the person's stored login in one query, fingerprints
the account's ``hosts.yml``, and looks the result up in a verification cache keyed
by everything that could change the answer. Nothing is pushed in: a lifecycle
commit bumps the revision and a credential write changes the fingerprint, so
either is a cache miss on the next read. Every reader is a pure function of one
view, so a shell spawn that reads one view gets an environment, a grant and a deny
rule that agree.

The account is ready only when ``gh api user`` in its own home answered with a
login that is not the person's own, and the person's login is known.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for
from dataclasses import dataclass, replace
from pathlib import Path

from connections import agent_binary_from_config
from connections.github_agent_home import (
    BOT_ENV_NAMES,
    GIT_CONFIG_SLOTS,
    LOCKED_HOME,
    LOGIN,
    clean_env,
    default_home,
    stores_token,
)
from connections.github_person import request_probe, same_login
from infra.database import get_db

log = logging.getLogger(__name__)

PROVIDER = "github"
# A verified answer is served while it is re-checked after this long.
FRESH_SECONDS = 15 * 60.0
# A failed or due verification starts again no sooner than this.
RETRY_SECONDS = 60.0
VERIFY_SECONDS = 10.0
_ROWS = """SELECT a.id, a.source_home, a.state, a.revision, c.config
           FROM vendor_accounts a JOIN connections c ON c.id=a.connection_id
           WHERE c.provider=? AND a.state!='removed' AND a.source_home IS NOT NULL"""
_PERSON = "SELECT config FROM connections WHERE provider=?"


@dataclass(frozen=True)
class Verified:
    login: str
    user_id: int
    at: float


@dataclass(frozen=True)
class Failed:
    definite: bool
    cause: str
    at: float


@dataclass(frozen=True)
class AgentAccountView:
    """One read of the agents' account; ``absent`` is the only state that leaves shells alone."""

    state: str  # not_installed | needs_auth | unverified | wrong_account | ready
    cause: str | None = None
    absent: bool = False
    account_id: str | None = None
    home: str | None = None
    binary: str | None = None
    login: str | None = None
    user_id: int | None = None

    @property
    def ready(self) -> bool:
        return self.state == "ready"


# key -> result, and key -> in-flight verification. A key is an immutable fact:
# (account_id, revision, source_home, binary, hosts.yml fingerprint).
_results: dict[tuple, Verified | Failed] = {}
_inflight: dict[tuple, Future] = {}
_attempts: dict[tuple, float] = {}
_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="github-agent-verify")


def _fingerprint(home: str) -> tuple[int, ...] | None:
    try:
        stat = os.stat(Path(home) / "hosts.yml")
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)


def _config(raw) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _verify(binary: str, home: str) -> Verified | Failed:
    """Ask GitHub who this home's stored token belongs to; only a 401 is a definite no."""
    now = time.monotonic()
    if not stores_token(Path(home)):
        return Failed(True, "token_not_in_home", now)
    env = clean_env() | {"GH_CONFIG_DIR": home, "GH_PROMPT_DISABLED": "1"}
    try:
        result = subprocess.run([binary, "api", "user"], capture_output=True, env=env,
                                stdin=subprocess.DEVNULL, timeout=VERIFY_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("agents' github verification unavailable: %s", type(exc).__name__)
        return Failed(False, "verify_failed", time.monotonic())
    if result.returncode != 0:
        definite = b"HTTP 401" in result.stderr
        return Failed(definite, "token_rejected" if definite else "verify_failed", time.monotonic())
    try:
        user = json.loads(result.stdout)
        login, user_id = user["login"], user["id"]
    except (ValueError, KeyError, TypeError):
        return Failed(False, "verify_failed", time.monotonic())
    if not isinstance(login, str) or not LOGIN.match(login) or not isinstance(user_id, int) or isinstance(user_id, bool):
        return Failed(False, "verify_failed", time.monotonic())
    return Verified(login, user_id, time.monotonic())


def _verify_and_store(key: tuple, binary: str, home: str) -> None:
    """Verify, then store the answer under the key it started with, before the future completes.

    A soft failure never drops a verified answer.
    """
    try:
        result = _verify(binary, home)
    except Exception as exc:  # noqa: BLE001 - a crashed verification is an unknown answer
        log.warning("agents' github verification failed: %s", type(exc).__name__)
        result = Failed(False, "verify_failed", time.monotonic())
    with _lock:
        _inflight.pop(key, None)
        previous = _results.get(key)
        if isinstance(result, Failed) and not result.definite and isinstance(previous, Verified):
            return
        _results[key] = result


def _start(key: tuple, binary: str, home: str) -> Future:
    """Run or join the one verification for this key (call with ``_lock`` held)."""
    running = _inflight.get(key)
    if running is not None:
        return running
    _attempts[key] = time.monotonic()
    # Its store takes ``_lock``, which the caller holds, so it always finds this entry to remove.
    future = _executor.submit(_verify_and_store, key, binary, home)
    _inflight[key] = future
    return future


def _due(key: tuple, entry: Verified | Failed | None, now: float) -> bool:
    last = _attempts.get(key)
    if last is not None and now - last < RETRY_SECONDS:
        return False
    if entry is None:
        return True
    if isinstance(entry, Verified):
        return now - entry.at >= FRESH_SECONDS
    return not entry.definite


def _agent_binary(config: dict) -> str | None:
    """Resolve only the agents' GitHub executable, including their cached CORSIA install."""
    return agent_binary_from_config(config)


def _read(db) -> tuple[list, str | None]:
    connection = db or get_db()
    rows = connection.execute(_ROWS, (PROVIDER,)).fetchall()
    person = connection.execute(_PERSON, (PROVIDER,)).fetchone()
    login = _config(person["config"] if person else None).get("login")
    return rows, login if isinstance(login, str) and login else None


def _resolve(db, *, wait: float | None) -> AgentAccountView:
    try:
        rows, person = _read(db)
    except Exception as exc:  # noqa: BLE001 - an unreadable row is never read as no account
        log.warning("agents' github account read failed: %s", type(exc).__name__)
        return AgentAccountView("unverified", "read_failed")
    if not rows:
        return AgentAccountView("not_installed", absent=True)
    if len(rows) > 1:
        return AgentAccountView("needs_auth", "multiple_accounts")
    row = rows[0]
    from connections.account_row_shape import current_home

    account_id = str(row["id"])
    home = str(current_home(str(row["source_home"]), PROVIDER, account_id))
    base = AgentAccountView("needs_auth", account_id=account_id, home=home)
    binary = _agent_binary(_config(row["config"]))
    if binary is None:
        return replace(base, state="not_installed", cause="github_executable_missing")
    base = replace(base, binary=binary)
    if row["state"] != "signed_in":
        return replace(base, cause="signed_out")
    fingerprint = _fingerprint(home)
    if fingerprint is None:
        return replace(base, cause="credential_missing")
    key = (account_id, int(row["revision"]), home, binary, fingerprint)
    with _lock:
        for stale in [k for k in _results if k != key]:
            del _results[stale]
        for stale in [k for k in _attempts if k != key and k not in _inflight]:
            del _attempts[stale]
        entry = _results.get(key)
        future = None
        if wait is not None or _due(key, entry, time.monotonic()):
            future = _start(key, binary, home)
    if wait is not None and future is not None:
        wait_for([future], timeout=wait)  # the answer, or its failure, is stored before completion
        with _lock:
            entry = _results.get(key)
    if person is None:
        request_probe()
        return replace(base, state="unverified", cause="own_login_unknown")
    if entry is None:
        return replace(base, state="unverified", cause="verifying")
    if isinstance(entry, Failed):
        return replace(base, state="needs_auth" if entry.definite else "unverified", cause=entry.cause)
    if same_login(entry.login, person):
        return replace(base, state="wrong_account", cause="person_account")
    return replace(base, state="ready", login=entry.login, user_id=entry.user_id)


def view(db=None) -> AgentAccountView:
    """The agents' account now; a verification it needs runs in the background."""
    return _resolve(db, wait=None)


def verify_now(db=None) -> AgentAccountView:
    """Run or join this account's verification, wait for it (bounded), and read the view."""
    return _resolve(db, wait=VERIFY_SECONDS)


def source_state(account: AgentAccountView) -> dict:
    """The model-source row: the state, a named cause, and the login once verified."""
    shown: dict = {"state": account.state}
    if account.cause is not None:
        shown["cause"] = account.cause
    if account.ready:
        shown["login"] = account.login
    return shown


def _git_config(account: AgentAccountView) -> list[tuple[str, str]]:
    helper = f"!'{account.binary}' auth git-credential" if account.ready else ""
    name = (account.login or "") if account.ready else ""
    email = f"{account.user_id}+{account.login}@users.noreply.github.com" if account.ready else ""
    entries = [
        # An empty helper clears every helper configured before it, the person's keychain included.
        ("credential.helper", ""),
        ("credential.https://github.com.helper", helper),
        ("url.https://github.com/.insteadOf", "git@github.com:"),
        ("url.https://github.com/.insteadOf", "ssh://git@github.com/"),
        ("user.name", name),
        ("user.email", email),
        ("user.useConfigOnly", "true"),
    ]
    assert len(entries) == GIT_CONFIG_SLOTS
    return entries


def shell_env(account: AgentAccountView) -> dict[str, str]:
    """An agent shell's GitHub identity: nothing without an account, the bot's once ready, else locked.

    Locked means ``gh`` points at an absent home, Git has no credential helper and an
    empty identity, and SSH remotes go over HTTPS, so a push fails rather than falls
    back to the person's own login.
    """
    if account.absent:
        return {}
    entries = _git_config(account)
    env = {"GH_CONFIG_DIR": account.home if account.ready and account.home else LOCKED_HOME,
           "GIT_CONFIG_COUNT": str(len(entries))}
    for index, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    assert set(env) == set(BOT_ENV_NAMES)
    return env


def sandbox_ro_dirs(account: AgentAccountView) -> list[Path]:
    """The read-only shell grant: the agents' home, only once verified."""
    return [Path(account.home)] if account.ready and account.home else []


def default_home_deny(account: AgentAccountView) -> Path | None:
    """The person's own ``gh`` home, hidden from shells whenever the agents' account exists."""
    return None if account.absent else default_home()


def agent_login(account: AgentAccountView) -> str | None:
    """The agents' verified login, for recognizing CORSIA's own pull requests."""
    return account.login if account.ready else None


def reset_for_tests() -> None:
    with _lock:
        _results.clear()
        _inflight.clear()
        _attempts.clear()
