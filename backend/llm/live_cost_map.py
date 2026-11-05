"""Fetch LiteLLM's current model map once without blocking backend startup."""

from __future__ import annotations

import asyncio
import logging

import httpx
import litellm
from litellm.litellm_core_utils.get_model_cost_map import GetModelCostMap, _expand_model_aliases
from litellm.utils import _invalidate_model_cost_lowercase_map

log = logging.getLogger(__name__)
_REFRESH_TIMEOUT = 5.0  # Matches LiteLLM's fetch_remote_model_cost_map timeout.
_refresh_tasks: dict[asyncio.AbstractEventLoop, asyncio.Task] = {}


async def _refresh() -> None:
    try:
        async with asyncio.timeout(_REFRESH_TIMEOUT):
            async with httpx.AsyncClient(timeout=_REFRESH_TIMEOUT) as client:
                response = await client.get(litellm.model_cost_map_url)
                response.raise_for_status()
                model_map = response.json()
    except (httpx.HTTPError, TimeoutError, ValueError) as exc:
        log.warning("LiteLLM cost map refresh failed; using bundled map: %s", exc)
        return

    if not GetModelCostMap.validate_model_cost_map(
        model_map, GetModelCostMap._get_backup_model_count()
    ):
        return  # LiteLLM logs the validation failure.
    # Reads every entry, so a malformed map raises before anything global changes.
    model_map = _expand_model_aliases(model_map)
    # LiteLLM's proxy reload uses the same replacement and cache refresh.
    litellm.model_cost = model_map
    _invalidate_model_cost_lowercase_map()
    litellm.add_known_models(model_cost_map=model_map)


def start_refresh() -> asyncio.Task:
    """Start the one refresh for this backend lifespan."""
    loop = asyncio.get_running_loop()
    task = loop.create_task(_refresh())
    _refresh_tasks[loop] = task
    task.add_done_callback(_finish_refresh)
    return task


def _finish_refresh(task: asyncio.Task) -> None:
    loop = task.get_loop()
    if _refresh_tasks.get(loop) is task:
        del _refresh_tasks[loop]
    if not task.cancelled() and (error := task.exception()):
        log.error("LiteLLM cost map refresh broke; using bundled map", exc_info=error)


def refresh_task() -> asyncio.Task | None:
    """Return the lifecycle-owned refresh for shutdown."""
    return _refresh_tasks.get(asyncio.get_running_loop())


async def wait_for_refresh() -> None:
    """Wait for the startup refresh: a caller stopping leaves it running, and its failure stays its own."""
    task = refresh_task()
    if task is not None:
        await asyncio.wait([task])
