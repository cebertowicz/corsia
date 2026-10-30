"""Claude Code catalog discovery through its stream-JSON control protocol.

Every read here runs ``claude -p`` in control-only mode: no model turn, no
persisted session, no tools. One process lists the models and reads the
account's plan usage (``get_usage``); one more per selector reads its context
window. ``connections.harness_models`` stores what these return.

The control session uses the selected account's home.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from connections import claude_usage, source_state
from connections.claude_context_probe import read_context_usage
from connections.claude_control_turns import converse
from connections.harness_process import killed_on_exit
from infra.db_async import run_in_db
from subagents.cc_env import clean_claude_env
from subagents.managed_launch import claude_project_discovery_args

log = logging.getLogger(__name__)

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
    in_turn: bool = False,
) -> bytes:
    """Run one bounded Claude control session and return its stream output.

    The frames go in together by default, which is right for reads that do not
    depend on each other. ``in_turn`` sends each request only after the one
    before it was answered, for a request that needs the earlier one to have
    taken effect: nothing promises the CLI answers in send order, and a usage
    read ahead of ``set_model`` would describe the previously selected model.
    """
    if context is None:
        raise RuntimeError('account_discovery_failed')
    from subagents.cc_env import apply_claude_config_dir

    env = clean_claude_env()
    apply_claude_config_dir(env, context.source_home)
    # Serialize before spawning so an invalid frame cannot leave a child process behind.
    payload = "".join(json.dumps(frame) + "\n" for frame in frames).encode()
    process = await asyncio.create_subprocess_exec(
        *claude_control_command(binary), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        async with killed_on_exit(process):
            if in_turn:
                stdout, stderr = await asyncio.wait_for(converse(process, frames), timeout=30)
            else:
                stdout, stderr = await asyncio.wait_for(process.communicate(payload), timeout=30)
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
    if context is None:
        raise RuntimeError('account_discovery_failed')
    frames = [
        {"type": "control_request", "request_id": "corsia-model-list",
         "request": {"subtype": "list_models"}},
        {"type": "control_request", "request_id": "corsia-usage",
         "request": {"subtype": "get_usage", "skip_behaviors": True}},
        {"type": "control_request", "request_id": "corsia-model-list-end",
         "request": {"subtype": "end_session", "reason": "discovery_complete"}},
    ]
    try:
        stdout = await run_claude_control(binary, frames, operation="list_models", context=context)
    except (RuntimeError, OSError):
        raise RuntimeError('account_discovery_failed') from None
    responses = _control_responses(stdout)
    response = responses.get("corsia-model-list")
    if response is None or response.get("subtype") != "success":
        raise RuntimeError('account_discovery_failed')
    payload = response.get("response") or {}
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
        probes.append(_discover_claude_context(binary, selector, context=context))
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
    from connections.account_discovery_types import StagedDiscovery

    usage = responses.get('corsia-usage') or {}
    report = usage.get('response') if usage.get('subtype') == 'success' else None
    return StagedDiscovery(payload, usage_report=report)


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


def _store_catalog_success(report: object, *, account_id: str, receipt, db=None) -> None:
    """Apply a usage co-read only to the login that produced it."""
    from connections import harness_usage
    from connections.account_receipts import receipt_is_current
    from infra.database import write_transaction

    with write_transaction(db, scope="claude_catalog_sign_in") as db:
        if receipt is None or not receipt_is_current(receipt, db=db):
            return
        harness_usage.mark_readings_stale(provider='claude-code', account_id=account_id, db=db)
        try:
            # A failed usage write must not undo a successful discovery or
            # expose the old login's allowance as a verified meter.
            with write_transaction(db, scope='claude_account_catalog_usage') as usage_db:
                claude_usage.apply_usage_response(report, receipt=receipt, db=usage_db)
        except Exception as exc:
            log.error("Claude Code usage observation failed: %s", type(exc).__name__)


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
        in_turn=True, **({'context': context} if context is not None else {}),
    )
    return read_context_usage(stdout, selector)
