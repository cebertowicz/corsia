"""Fresh Codex app-server model discovery and cache promotion.

The app-server's ``model/list`` is the canonical picker source. It caches model
responses in ``CODEX_HOME/models_cache.json`` for five minutes and falls back to
bundled/in-memory models when refresh fails, so discovery cannot treat every
successful RPC response as authoritative.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from tempfile import mkdtemp
import shutil
from typing import Any

from connections.harness_process import killed_on_exit
from infra.db_async import run_in_db
from subagents.codex_app_server import CodexAppServerSession

log = logging.getLogger(__name__)
_MODEL_CACHE_FILENAME = "models_cache.json"


async def discover_codex_debug_models(binary: str, *, env=None) -> dict[str, Any]:
    """Read installed-CLI metadata to enrich, never define, picker rows."""
    process = await asyncio.create_subprocess_exec(
        binary, "debug", "models", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **({'env': env} if env is not None else {}),
    )
    try:
        async with killed_on_exit(process):
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
    except asyncio.TimeoutError:
        raise RuntimeError("codex debug models timed out") from None
    if process.returncode:
        detail = stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"codex debug models failed: {detail}")
    payload = json.loads(stdout)
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise RuntimeError("codex debug models returned an invalid payload")
    return payload


def _prepare_discovery_home(context):
    """Prepare the existing fresh home and its captured retirement closure."""
    if context is None or not context.source_home or not context.runtime_home:
        raise RuntimeError('account_discovery_failed')
    persistent_home = Path(context.runtime_home)
    from connections.account_projection import projection_gate

    projection_gate(context.receipt)  # Before mkdir or any fresh temporary allocation.
    persistent_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    persistent_cache = persistent_home / _MODEL_CACHE_FILENAME
    fresh_home = Path(mkdtemp(prefix='.corsia-codex-model-discovery-', dir=persistent_home.parent))

    def cleanup():
        from connections.account_projection import begin_retirement, complete_retirement

        if not begin_retirement(context.receipt, home=fresh_home, owner_turn_id=None):
            return  # A successor login owns cleanup of its retained stale receipt.
        shutil.rmtree(fresh_home)
        complete_retirement(context.receipt, home=fresh_home, owner_turn_id=None)

    return persistent_cache, fresh_home, cleanup


async def discover_codex_models(binary: str, *, context=None):
    """Return a freshly online app-server catalog or fail without reconciling.

    Discovery gets a clean cache in a temporary sibling of the persistent home,
    while existing managed sessions retain their last-known cache. An online
    response must write a new cache; only after the control session has stopped
    is that cache atomically promoted for subsequent managed execution.
    """
    persistent_cache, fresh_home, cleanup = await run_in_db(_prepare_discovery_home, context)
    transferred = False
    session = None

    try:
        fresh_cache = fresh_home / _MODEL_CACHE_FILENAME
        from connections.account_projection import ensure_projection

        await run_in_db(lambda: ensure_projection(context.receipt, home=fresh_home, kind='discovery'))
        session = CodexAppServerSession(
            binary=binary, task=None, codex_home=str(fresh_home), control_only=True,
            source_codex_home=context.source_home,
        )
        session.vendor_account_id = context.account_id
        session.vendor_account_bound = True
        session.vendor_account_receipt = context.receipt
        session.vendor_discovery_staged = True
        session.vendor_staged_rate_updates = []
        try:
            await session.start()
            payload = await session.list_models()
            if not fresh_cache.is_file():
                raise RuntimeError("Codex model/list returned without an online model refresh")
            try:
                usage_report = await session._request('account/rateLimits/read', None)
            except Exception:
                usage_report = None
            try:
                debug = await discover_codex_debug_models(binary, env=await run_in_db(session.build_env))
            except (OSError, RuntimeError, json.JSONDecodeError) as exc:
                log.warning("Codex context discovery failed: %s", exc)
            else:
                by_selector = {
                    str(row["slug"]): row for row in debug.get("models") or []
                    if isinstance(row, dict) and row.get("slug")
                }
                for raw in payload.get("data") or []:
                    if metadata := by_selector.get(str(raw.get("id") or "")):
                        raw["contextMetadata"] = metadata
        except BaseException:
            try:
                await session.close(timeout=10.0)
            except BaseException as close_error:
                log.warning("Codex discovery cleanup failed: %s", close_error)
            raise
        await session.close(timeout=10.0)
        from connections.account_discovery_types import StagedDiscovery
        from connections.model_cache_stage import ModelCacheStage

        stage = ModelCacheStage.prepare(fresh_cache, persistent_cache)
        transferred = True
        return StagedDiscovery(payload, usage_report=usage_report, cache_stage=stage, cleanup_owner=cleanup)
    finally:
        if not transferred:
            await run_in_db(cleanup)
