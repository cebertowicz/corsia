"""Read comparison facts without retaining vendor credentials or raw identifiers."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from pathlib import Path

from connections.account_profiles import profile_for
from connections.harness_process import killed_on_exit
from connections.vendor_accounts import record_default_identity
from infra.db_async import run_in_db

log = logging.getLogger(__name__)

CODEX_PUBLIC_USER_CLAIMS = ("https://api.openai.com/auth", "chatgpt_user_id", "user_id")


def codex_identity(home: str | Path) -> dict[str, str]:
    """Read the account fingerprint and observed login email from the Codex auth file."""
    try:
        tokens = json.loads((Path(home) / "auth.json").read_text())["tokens"]
        account = tokens["account_id"]
        token = tokens["id_token"]
        payload = token.split(".")
        if len(payload) != 3:
            return {}
        encoded = payload[1]
        claims = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
        namespace, user_key, fallback = CODEX_PUBLIC_USER_CLAIMS
        auth = claims.get(namespace)
        user = auth.get(user_key) if isinstance(auth, dict) else None
        user = user if isinstance(user, str) and user else auth.get(fallback) if isinstance(auth, dict) else None
        if not (isinstance(account, str) and account and isinstance(user, str) and user):
            return {}
        canonical = json.dumps([account, user], ensure_ascii=False, separators=(",", ":"))
        identity = {"account_fp": hashlib.sha256(canonical.encode()).hexdigest()}
        email = claims.get("email") or (auth.get("email") if isinstance(auth, dict) else None)
        if isinstance(email, str) and email:
            identity["email"] = email
        return identity
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


async def default_identity(provider: str) -> dict[str, str]:
    """Read the ambient Default only, never signing it in or out."""
    if provider == "codex":
        from subagents.codex_env import default_codex_home

        return await run_in_db(codex_identity, default_codex_home())
    from connections.account_controls import _binary, _COMMAND_SECONDS

    binary = await _binary(provider)
    if binary is None:
        return {}
    profile = profile_for(provider)
    env = profile.clean_env()
    env.pop(profile.home_variable, None)
    try:
        process = await asyncio.create_subprocess_exec(
            binary, *profile.commands["status"], env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        async with killed_on_exit(process):
            async with asyncio.timeout(_COMMAND_SECONDS):
                stdout, _ = await process.communicate()
        report = json.loads(stdout) if process.returncode == 0 else None
        if not isinstance(report, dict) or report.get("loggedIn") is not True:
            return {}
        return profile.facts_from_status(report)
    except (OSError, TimeoutError, ValueError):
        return {}


def capture_default_observation(provider: str, *, db=None):
    """Fence a discovery's Default observation before its CLI read; none without a Default row."""
    from connections.account_observation import begin_default_observation
    from connections.vendor_accounts import resolve_account_id
    from infra.database import get_db

    connection = db or get_db()
    try:
        if resolve_account_id(provider, db=connection) is None:
            return None
        return begin_default_observation(provider, db=connection)
    finally:
        if db is None:
            connection.close()


async def read_default_facts(provider: str) -> dict[str, str]:
    """The Default's facts for a discovery; a failed read is logged and observes nothing."""
    try:
        return await default_identity(provider)
    except Exception as exc:  # noqa: BLE001 - a failed identity read must not fail the catalog refresh
        # Only the type: the message of a vendor read can carry the account's email.
        log.warning("Default %s identity read failed: %s", provider, type(exc).__name__)
    return {}


def observe_discovered_identity(provider: str, identity: dict, *, token, db=None) -> bool:
    """Commit a discovery's fenced Default observation; False only when a newer one superseded it.

    Unreadable or incomplete facts are not evidence that the login changed, so
    they record nothing: the last known identity, receipt and usage stand.
    """
    from connections.account_observation import has_comparison_facts

    if token is None or not has_comparison_facts(provider, identity):
        return True
    return record_default_identity(provider, identity, token=token,
                                   schedule_usage_check=False, db=db).accepted


async def observe_default_read(provider: str, token) -> None:
    """Commit a discovery's Default identity under the fence taken before its read."""
    if token is not None:
        await run_in_db(observe_discovered_identity, provider, await read_default_facts(provider), token=token)


def _default_observation_token(provider: str):
    from connections.account_observation import begin_default_observation
    from infra.database import get_db

    connection = get_db()
    try:
        return begin_default_observation(provider, db=connection)
    finally:
        connection.close()


async def refresh_default_identity(provider: str):
    """Capture before the external read so a delayed observation cannot replace newer facts."""
    token = await run_in_db(_default_observation_token, provider)
    try:
        identity = await default_identity(provider)
    except Exception:  # noqa: BLE001 - a failed ambient read must clear the old comparison snapshot
        identity = {}
    return await run_in_db(record_default_identity, provider, identity, token=token)


async def vendor_status(provider: str, home: str, binary: str) -> tuple[bool | None, dict[str, str], str | None]:
    """Whether the vendor says this home is signed in, its identity facts, and a cause when unknown."""
    from connections.account_controls import _run

    try:
        code, stdout = await _run(provider, home, binary, "status")
    except (OSError, TimeoutError):
        return None, {}, "vendor_status_unavailable"
    profile = profile_for(provider)
    if profile.parse_status is not None:
        signed_in, facts = profile.parse_status(code, stdout)
        return signed_in, facts, None if signed_in is not None else "vendor_status_unreadable"
    if code == 1:
        return False, {}, None
    if code != 0:
        return None, {}, "vendor_status_failed"
    if not profile.status_report_keys:
        # A vendor that names no facts in its status report keeps them in its auth file.
        return True, await run_in_db(codex_identity, home), None
    try:
        report = json.loads(stdout)
    except ValueError:
        report = None
    if not isinstance(report, dict) or not isinstance(report.get("loggedIn"), bool):
        return None, {}, "vendor_status_unreadable"
    if not report["loggedIn"]:
        return False, {}, None
    return True, profile.facts_from_status(report), None
