"""Claude Code catalog discovery through its stream-JSON control protocol.

Every read here runs ``claude -p`` in control-only mode: no model turn, no
persisted session, no tools. One process lists the models and reads the
account's plan usage (``get_usage``); one more per selector reads its context
window. ``connections.harness_models`` stores what these return.

That one process is also the Claude Code sign-in check: whether it could list
the models is recorded as the source state (#1910). A session that fails does
not by itself mean signed out: only ``claude auth status`` exiting 1 does, so a
usage-limit or transport message can never take the source away.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, NoReturn

from connections import claude_usage, source_state
from connections.claude_context_probe import read_context_usage
from connections.harness_process import killed_on_exit
from infra.db_async import run_in_db
from subagents.cc_env import clean_claude_env
from subagents.managed_launch import claude_project_discovery_args

log = logging.getLogger(__name__)

AUTH_STATUS_TIMEOUT_SECONDS = 10


class ClaudeCatalogCheckFailed(RuntimeError):
    """The catalog failed after its sign-in observation was successfully recorded."""


def claude_control_command(binary: str) -> list[str]:
    """The no-turn Claude CLI command used for catalog and usage controls.

    The governed setting sources keep the operator's login and model state but
    load no CLAUDE.md, rules, hooks or skills (#2112).
    """
    return [
        binary, "-p", "--input-format", "stream-json", "--output-format",
        "stream-json", "--verbose", "--no-session-persistence", "--safe-mode",
        "--permission-mode", "dontAsk", "--tools", "",
        *claude_project_discovery_args(),
    ]


async def run_claude_control(
    binary: str, frames: list[dict[str, Any]], *, operation: str, context=None,
) -> bytes:
    """Run one bounded Claude control session and return its stream output."""
    env = clean_claude_env()
    if context is not None:
        from subagents.cc_env import apply_claude_config_dir

        apply_claude_config_dir(env, context.source_home)
    process = await asyncio.create_subprocess_exec(
        *claude_control_command(binary), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        async with killed_on_exit(process):
            stdout, stderr = await asyncio.wait_for(
                process.communicate(
                    "".join(json.dumps(frame) + "\n" for frame in frames).encode()
                ),
                timeout=30,
            )
    except asyncio.TimeoutError:
        raise RuntimeError(f"Claude Code {operation} timed out") from None
    if process.returncode:
        # A signed-out CLI reports on stdout; fall back to it so the reason shows.
        detail = (stderr or stdout).decode("utf-8", "replace").strip()
        raise RuntimeError(f"Claude Code {operation} failed: {detail}")
    return stdout


async def discover_claude_models(binary: str, *, context=None):
    """Read Claude's stream-JSON catalog without a turn or persisted session.

    The same process also reads the account's plan usage and records it, so
    every catalog refresh re-checks whether the signed-in account can send.
    """
    frames = [
        {"type": "control_request", "request_id": "corsia-model-list",
         "request": {"subtype": "list_models"}},
        {"type": "control_request", "request_id": "corsia-usage",
         "request": {"subtype": "get_usage", "skip_behaviors": True}},
        {"type": "control_request", "request_id": "corsia-model-list-end",
         "request": {"subtype": "end_session", "reason": "discovery_complete"}},
    ]
    # The Default's identity is fenced before the read its co-read belongs to.
    observation = None if context is not None else await run_in_db(_default_observation)
    try:
        stdout = await run_claude_control(binary, frames, operation="list_models",
                                         **({'context': context} if context is not None else {}))
    except (RuntimeError, OSError) as exc:
        if context is not None:
            raise RuntimeError('account_discovery_failed') from None
        await _record_failed_sign_in(binary, str(exc))
    responses = _control_responses(stdout)
    response = responses.get("corsia-model-list")
    if response is None:
        if context is not None:
            raise RuntimeError('account_discovery_failed')
        await _record_failed_sign_in(binary, "Claude Code returned no reply")
    if response.get("subtype") != "success":
        if context is not None:
            raise RuntimeError('account_discovery_failed')
        detail = str(response.get("error") or response)
        await _record_failed_sign_in(binary, detail)
    # Invalidate the old account before applying this co-read, in one transaction.
    if context is None:
        usage = responses.get("corsia-usage")
        # A signed-out CLI still lists its static models, so a listed catalog
        # proves nothing about the login: ask the CLI before calling it ready.
        if await auth_status_state(binary) == source_state.NEEDS_AUTH:
            await _record_failed_sign_in(binary, str((usage or {}).get("error") or "Claude Code is not signed in"))
        await _record_catalog_success(usage, observation)
    payload = response.get("response") or {}
    if context is None:
        known_contexts = await run_in_db(_known_claude_context_identities)
    else:
        from llm.context_window import known_discovery_context_identities

        prior = (context.prior_discovery or {}).get('discovery', {}).get('models', {})
        known_contexts = known_discovery_context_identities(prior)
    probes = []
    probe_rows = []
    for raw in payload.get("models") or []:
        selector = str(raw.get("value") or "").strip()
        if not selector or selector == "default":
            continue
        resolved = str(raw.get("resolvedModel") or "").strip()
        if known_contexts.get(selector) == resolved:
            continue
        probe_rows.append(raw)
        probes.append(_discover_claude_context(binary, selector,
                                              **({'context': context} if context is not None else {})))
    if probes:
        results = await asyncio.gather(*probes, return_exceptions=True)
        for raw, result in zip(probe_rows, results, strict=True):
            if isinstance(result, Exception):
                log.warning(
                    "Claude Code context discovery failed for %s: %s",
                    raw.get("value"), result,
                )
                continue
            raw["contextUsage"] = result
    if context is not None:
        from connections.account_discovery_types import StagedDiscovery

        usage = responses.get('corsia-usage') or {}
        report = usage.get('response') if usage.get('subtype') == 'success' else None
        return StagedDiscovery(payload, usage_report=report)
    return payload


async def auth_status_state(binary: str) -> str:
    """needs_auth only when ``claude auth status`` exits 1; anything else is unverified.

    A failed session says something went wrong, not that the user is signed
    out, so ask the CLI's own answer once, bounded. Only the exit code is read:
    the output carries the account's email and is never captured.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            binary, "auth", "status", stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            env=clean_claude_env(),
        )
        async with killed_on_exit(process):
            code = await asyncio.wait_for(process.wait(), timeout=AUTH_STATUS_TIMEOUT_SECONDS)
    except (OSError, asyncio.TimeoutError) as exc:
        log.warning("claude auth status did not answer: %s", type(exc).__name__)
        return source_state.UNVERIFIED
    return source_state.NEEDS_AUTH if code == 1 else source_state.UNVERIFIED


async def _record_failed_sign_in(binary: str, detail: str) -> NoReturn:
    """Fail discovery with an explicit receipt only after the source write succeeded."""
    state = await auth_status_state(binary)
    from connections.auth_failure import auth_failure_kind

    # A stale credential file can make ``claude auth status`` exit successfully
    # after the CLI has already reported that its refresh is impossible. This
    # exact provider error overrides only that stale success, never a generic
    # HTTP 401.
    if state != source_state.NEEDS_AUTH and auth_failure_kind("claude-code", detail) is not None:
        state = source_state.NEEDS_AUTH
    await _record_sign_in(state, detail)
    raise ClaudeCatalogCheckFailed(detail)


async def reconcile_turn_auth_failure(detail: str) -> None:
    """Recheck an exact expired session before marking the Claude source needs-auth.

    The catalog control session is the existing bounded authentication check. A
    matching terminal error forces ``needs_auth`` only if that check cannot
    already give the definitive answer, so a generic 401 never changes state by
    text alone.
    """
    from connections.auth_failure import auth_failure_kind
    from connections.models import get_connection

    if auth_failure_kind("claude-code", detail) is None:
        return
    try:
        await source_state.check_harness_source("claude-code", rediscover=True)
    except Exception as exc:  # noqa: BLE001 - terminal recovery must still name the expired login
        log.warning("Claude Code auth recheck failed: %s", type(exc).__name__)
    row = await run_in_db(get_connection, "claude-code")
    if source_state.row_state(row)["state"] == source_state.UNVERIFIED:
        await _record_sign_in(source_state.NEEDS_AUTH, detail)


async def _record_sign_in(state: str, detail: str | None = None) -> None:
    """Keep the session's outcome as Claude Code's source state.

    A write that fails or finds no row raises, so the recheck cannot present
    the row's old state as this check's result.
    """
    if await run_in_db(source_state.apply_check, "claude-code", state, detail) is None:
        raise RuntimeError("Claude Code source state was not recorded")


def _known_claude_context_identities() -> dict[str, str]:
    """Return selectors whose exact resolved model already has a window.

    Context-window evidence is identity-bound: a selector can keep its prior
    window only while ``resolvedModel`` has not changed. Re-probing an
    unchanged identity creates a fresh CLI control process without improving
    catalog truth, so leave its existing observation to ``store_success``.
    """
    from infra.database import get_db

    db = get_db()
    try:
        rows = db.execute(
            "SELECT provider_model_id,metadata_json FROM models "
            "WHERE harness = 'claude-code' AND context_window IS NOT NULL"
        ).fetchall()
    finally:
        db.close()
    known: dict[str, str] = {}
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict):
            continue
        resolved = str(metadata.get("resolved_identity") or "").strip()
        if resolved:
            known[str(row["provider_model_id"])] = resolved
    return known


def _control_responses(stdout: bytes) -> dict[str, dict[str, Any]]:
    """Every correlated control response in a stream, keyed by request id."""
    found: dict[str, dict[str, Any]] = {}
    for line in stdout.splitlines():
        try:
            response = json.loads(line).get("response") or {}
        except (json.JSONDecodeError, AttributeError):
            continue
        if isinstance(response, dict) and response.get("request_id"):
            found[str(response["request_id"])] = response
    return found


def _default_observation():
    from connections.account_identity import capture_default_observation

    return capture_default_observation("claude-code")


async def _record_catalog_success(response: dict[str, Any] | None, observation=None) -> None:
    """Commit sign-in and its co-read together; an unreadable meter stays unknown."""
    if response is not None and response.get("subtype") == "success":
        report = response.get("response")
    else:
        detail = (response or {}).get("error") or "no reply"
        log.warning("Claude Code get_usage failed: %s", detail)
        report = None
    from connections.account_identity import read_default_facts

    identity = await read_default_facts("claude-code") if observation is not None else {}
    await run_in_db(_store_default_catalog_success, report, identity, observation)


def _store_default_catalog_success(report: object, identity: dict | None = None,
                                   observation=None, *, db=None) -> None:
    """The ambient sign-in's co-read belongs to the Default account's current login.

    The read's identity commits first, so its usage is captured against the
    login it read; a read a newer observation superseded commits nothing.
    """
    from connections.account_identity import observe_discovered_identity
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import resolve_account_id
    from infra.database import write_transaction

    with write_transaction(db, scope="claude_default_catalog") as db:
        account_id = resolve_account_id("claude-code", db=db)
        if account_id is None:
            return
        if not observe_discovered_identity("claude-code", identity or {}, token=observation, db=db):
            return
        _store_catalog_success(report, account_id=account_id,
                               receipt=capture_receipt("claude-code", account_id=account_id, db=db), db=db)


def _store_catalog_success(report: object, *, account_id: str, receipt, db=None) -> None:
    """Expose only the new login's capacity after its sign-in commits.

    A read whose login was superseded changes nothing. A Default whose login is
    unknown still records its source check; its usage writes are refused (logged).
    """
    from connections import harness_usage
    from connections.account_receipts import receipt_is_current
    from connections.models import get_connection
    from connections.vendor_accounts import get_account
    from infra.database import write_transaction

    with write_transaction(db, scope="claude_catalog_sign_in") as db:
        if receipt is not None and not receipt_is_current(receipt, db=db):
            return
        if not get_account(account_id, db=db)['is_default']:
            if receipt is None:
                return
            harness_usage.mark_readings_stale(provider='claude-code', account_id=account_id, db=db)
            with write_transaction(db, scope='claude_account_catalog_usage') as usage_db:
                claude_usage.apply_usage_response(report, receipt=receipt, db=usage_db)
            return
        previous = get_connection("claude-code", db=db)
        row = source_state.apply_check("claude-code", source_state.READY, db=db,
                                       schedule_usage_check=False)
        if row is None:
            raise RuntimeError("Claude Code source state was not recorded")
        if previous is not None and previous["status"] != "available":
            # Sign-in invalidates the old limits, but proves no new capacity.
            harness_usage.record_usage(provider="claude-code", verdict={
                "state": "unknown", "source": "sign_in",
            }, check_error="New account usage has not been verified", receipt=receipt, db=db)
        try:
            # A failed usage write must not undo a successful source check or
            # expose the old account's sign-in allowance as a verified meter.
            with write_transaction(db, scope="claude_catalog_usage") as usage_db:
                claude_usage.apply_usage_response(report, receipt=receipt, db=usage_db)
        except Exception as exc:
            log.error("Claude Code usage observation failed: %s", exc)


async def _discover_claude_context(binary: str, selector: str, *, context=None) -> dict[str, Any]:
    """Probe one selector for its context usage, or raise naming the reason."""
    frames = [
        {"type": "control_request", "request_id": "corsia-set-model",
         "request": {"subtype": "set_model", "model": selector}},
        {"type": "control_request", "request_id": "corsia-context-usage",
         "request": {"subtype": "get_context_usage"}},
        {"type": "control_request", "request_id": "corsia-context-end",
         "request": {"subtype": "end_session", "reason": "discovery_complete"}},
    ]
    stdout = await run_claude_control(
        binary, frames, operation=f"context discovery for {selector}",
        **({'context': context} if context is not None else {}),
    )
    return read_context_usage(stdout, selector)
