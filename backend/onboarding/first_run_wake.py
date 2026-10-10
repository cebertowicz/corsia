"""The first-run wake with its instructions and setup facts inline.

Turn 1 of a new install used to start by loading a skill, reading its first-run
reference and calling four tools for facts CORSIA already held. The wake now
carries all of it, rendered when the person picks the conductor's model (the
facts name that model): the served System first-run reference, embedded whole
so text edits flow through, and the facts those preparation calls would return.

Turn 1 has this one path. Every fact always renders; one whose read fails or
does not finish in time renders as unavailable with the read that gets it, so
nothing here raises for a fact. Rendering is deterministic for the same facts
(sorted JSON, no clock).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

FIRST_RUN_REFERENCE = "skills/conductor-bootstrap/references/first-run.md"
#: The whole set of fact reads shares this budget; the first model pick waits no longer.
FACTS_TIMEOUT_SECONDS = 5.0

#: Each fact's exact fallback read, shown with the fact when its read did not finish.
UNAVAILABLE_READS = {
    "your_model": "none; omit task 1's bracket",
    "model_sources": "tool model_sources, once, without recheck",
    "mcp_sources": "corsia_api GET /api/mcp/sources",
    "setup": "project_discovery_list with no id",
    "host_access_reviewer": "none; corsia_api hides host-access fields, so treat the reviewer as unknown",
    "web_search_key": "one web_search call",
}

INLINE_SUMMARY = (
    "First run of a new install. Your instructions are below: skill "
    "conductor-bootstrap's references/first-run.md. The Setup facts section "
    "holds what its Preparation reads; do not load the skill or call those "
    "tools for them. A fact marked unavailable names the one read that gets it."
)


def instructions() -> str:
    """The served first-run reference, whole."""
    from infra.system_package import system_resource_source

    return system_resource_source(FIRST_RUN_REFERENCE).read_text(encoding="utf-8").strip()


def _model_sources() -> dict[str, Any]:
    """What ``model_sources`` returns without a recheck."""
    from api.provider_sources import list_sources
    from tools.model_sources import github_agent_state, github_state

    return {"sources": list_sources(), "github": github_state(), "github_agent": github_agent_state()}


def _your_model(model: str) -> dict[str, Any]:
    from llm.resolution import resolve_execution_target

    target = resolve_execution_target(model)
    return {"display_name": target.display_name, "local": target.provider == "omlx"}


def _web_search_key() -> bool:
    """Whether a web search key is saved; whether it works is the conductor's to test."""
    from infra.key_store import WEB_SEARCH, resolve_builtin

    return resolve_builtin(WEB_SEARCH) is not None


def _unavailable(name: str) -> dict[str, str]:
    return {"unavailable": UNAVAILABLE_READS[name]}


async def setup_facts(model: str) -> dict[str, Any]:
    """The facts Preparation reads, for a conductor about to run on ``model``.

    Every fact is present. The reads run together under one total
    ``FACTS_TIMEOUT_SECONDS``; one that fails or is unfinished by then is the
    unavailable marker with its exact fallback read.
    """
    from api.system_reviewer import host_access_reviewer_status
    from infra.db_async import run_in_db, run_off_db
    from onboarding import setup_decisions
    from tools.mcp_import import detected_sources

    reads = {
        "your_model": lambda: run_in_db(_your_model, model),
        "model_sources": lambda: run_in_db(_model_sources),
        "mcp_sources": lambda: run_off_db(detected_sources),
        "setup": lambda: setup_decisions.setup_plan(self_check_passed=True, probe_web_search=False),
        "host_access_reviewer": lambda: run_in_db(host_access_reviewer_status),
        "web_search_key": lambda: run_in_db(_web_search_key),
    }
    tasks = {name: asyncio.ensure_future(read()) for name, read in reads.items()}
    await asyncio.wait(tasks.values(), timeout=FACTS_TIMEOUT_SECONDS)
    facts: dict[str, Any] = {}
    for name, task in tasks.items():
        if not task.done():
            task.cancel()
            log.error("first-run setup fact %s unavailable: unfinished after %ss", name, FACTS_TIMEOUT_SECONDS)
            facts[name] = _unavailable(name)
        elif task.exception() is not None:
            log.error("first-run setup fact %s unavailable: read failed", name, exc_info=task.exception())
            facts[name] = _unavailable(name)
        else:
            facts[name] = task.result()
    return facts


def render(facts: dict[str, Any], instruction_text: str) -> str:
    """The wake content: summary, instructions, then the facts as sorted JSON."""
    from events.delivery import format_event_input
    from infra.onboarding_events import FIRST_RUN_EVENT

    body = "\n\n".join((
        instruction_text,
        "## Setup facts\n\n" + json.dumps(facts, sort_keys=True, ensure_ascii=False, default=str),
    ))
    return format_event_input(FIRST_RUN_EVENT, INLINE_SUMMARY, None, notice=body)


async def render_for(model: str) -> str:
    """The full first-run wake for a conductor about to run on ``model``."""
    return render(await setup_facts(model), instructions())
