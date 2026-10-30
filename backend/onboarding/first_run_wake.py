"""The first-run wake with its instructions and setup facts inline.

The wake is rendered when the person picks the conductor's model. Script installs
keep the existing preparation facts. App installs receive only the facts their
post-setup instructions need, unless that new instruction reference is not served
with this build; that compatibility path deliberately keeps the script behavior.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from typing import Any

from infra.settings import get_host_access_auto_approve

log = logging.getLogger(__name__)

FIRST_RUN_REFERENCE = "skills/conductor-bootstrap/references/first-run.md"
APP_REFERENCE = "skills/conductor-bootstrap/references/first-run-app.md"
APP_START_REFERENCE = "skills/conductor-bootstrap/references/first-run-start.md"
#: Fact reads share this budget; cancelled reads finish cleanup before the pick returns.
FACTS_TIMEOUT_SECONDS = 5.0
APP_FACT_KEYS = frozenset({
    "first_run_path", "harness_ready", "sources_missing", "agents_found", "local_download",
    "local_eligible", "auto_approve", "tool_attempts", "pays_for", "bound_model", "web_search_ready", "web_search_key", "github_ready",
    "setup", "user_profile",
})
APP_PLAN_FACT_KEYS = APP_FACT_KEYS | {"connected_app_count", "email_calendar_ready"}
UNAVAILABLE_READS = {
    "your_model": "none; omit task 1's bracket",
    "bound_model": "none; treat the bound model as unknown",
    "model_sources": "tool model_sources, once, without recheck",
    "mcp_sources": "corsia_api GET /api/mcp/sources",
    "setup": "setup_decisions.read_facts once, without web search",
    "host_access_reviewer": "none; corsia_api hides host-access fields, so treat the reviewer as unknown",
    "web_search_key": "corsia_api GET /api/keys, the web-search entry's is_set",
    "harness_ready": "tool model_sources, once, without recheck",
    "sources_missing": "tool model_sources, once, without recheck",
    "agents_found": "none; skip the import step unless its consent decision is available",
    "local_download": "corsia_api GET /api/providers/local/setup",
    "auto_approve": "none; corsia_api hides host-access fields, so say nothing about its state",
    "local_eligible": "none; say nothing about local models",
    "setup_handoff": "setup progress handoff saved before model binding",
    "tool_attempts": "setup progress handoff saved before model binding",
    "setup_picks": "setup progress picks saved on the subscriptions screen",
    "pays_for": "setup progress picks saved on the subscriptions screen; offer no subscription",
    "web_search_ready": "one web_search call",
    "github_ready": "tool model_sources, once, without recheck",
    "user_profile": "preferred and full name read from the workspace profile",
}

INLINE_SUMMARY = (
    "First run of a new install. Your instructions are below: skill "
    "conductor-bootstrap's references/first-run.md. The Setup facts section "
    "holds what its Preparation reads; do not load the skill or call those "
    "tools for them. A fact marked unavailable names the one read that gets it."
)
APP_INLINE_SUMMARY = (
    "First run of a new install. Your instructions are below: skill "
    "conductor-bootstrap's references/first-run-start.md. The server has already "
    "posted your opening and seeded your plan. Start the first applicable step."
)


def app_reference_served() -> bool:
    """Whether the app-path instruction reference is present in this System package."""
    from infra.system_package import system_resource_source
    from onboarding.first_run_opening import OPENING_REFERENCE

    return all(system_resource_source(path).is_file() for path in (APP_REFERENCE, APP_START_REFERENCE, OPENING_REFERENCE))


def instructions(path: str = "script") -> str:
    """The script instructions, or the app's compact first-turn instructions."""
    from infra.system_package import system_resource_source

    if path == "script":
        return system_resource_source(FIRST_RUN_REFERENCE).read_text(encoding="utf-8").strip()
    if path == "app":
        return system_resource_source(APP_START_REFERENCE).read_text(encoding="utf-8").strip()
    raise ValueError("first_run_path must be 'app' or 'script'")


def _model_sources() -> dict[str, Any]:
    """What ``model_sources`` returns without a recheck."""
    from api.provider_sources import list_sources
    from tools.model_sources import github_agent_state, github_state

    return {"sources": list_sources(), "github": github_state(), "github_agent": github_agent_state()}


def _your_model(model: str) -> dict[str, Any]:
    from llm.resolution import resolve_execution_target

    target = resolve_execution_target(model)
    return {"display_name": target.display_name, "local": target.provider == "omlx"}


def _bound_model(model: str) -> dict[str, str] | None:
    """Project the model this selection is about to bind through its resolver."""
    from llm.resolution import ModelUnavailableError, resolve_execution_target

    try:
        target = resolve_execution_target(model)
    except ModelUnavailableError:
        return None
    return {"model": model, "source": target.provider}


def _web_search_key() -> bool:
    """Whether a web search key is saved; whether it works is the conductor's to test."""
    from infra.key_store import WEB_SEARCH, resolve_builtin

    return resolve_builtin(WEB_SEARCH) is not None


def _app_harness_ready(snapshot: dict[str, Any]) -> list[str]:
    ready = {row.get("source") for row in snapshot["sources"] if row.get("state") == "ready"}
    return [name for name, source in (("claude_code", "claude-code"), ("codex", "codex")) if source in ready]


def _app_sources_missing(snapshot: dict[str, Any]) -> dict[str, str]:
    """The subscription rows the app chat may offer, from the source snapshot that owns their state."""
    missing: dict[str, str] = {}
    for name, source in (("claude_code", "claude-code"), ("codex", "codex")):
        row = next((item for item in snapshot["sources"] if item.get("source") == source), {})
        state = row.get("state")
        if row.get("limited") is True:
            missing[name] = "limited"
        elif state in {"not_installed", "needs_auth", "unusable", "limited"}:
            missing[name] = str(state)
        elif state == "unverified":
            missing[name] = "unusable"
    return missing


def _app_agents_found() -> list[str]:
    """Tools with importable context, found locally independent of provider readiness."""
    from onboarding import agent_import
    from onboarding.project_discovery import Roots

    return agent_import.found_tools(Roots.for_user())


def _app_local_download() -> dict[str, Any]:
    """The current local-model state through the canonical, safe projection."""
    from onboarding.local_download_state import current

    return current()


def _app_local_eligible(snapshot: dict[str, Any]) -> bool:
    return any(item.get("source") == "omlx" and item.get("eligible") is True for item in snapshot["sources"])


def _app_github_ready(snapshot: dict[str, Any]) -> bool:
    return any(snapshot[key].get("state") == "ready" for key in ("github_agent", "github"))


def _app_setup_handoff() -> dict[str, Any] | None:
    """The validated setup-only facts that the first-run wake cannot reconstruct live."""
    from infra import setup_progress

    record = setup_progress.read() or {}
    handoff = record.get("handoff")
    return handoff if isinstance(handoff, dict) else None


def _app_setup_picks() -> dict[str, Any] | None:
    """The subscriptions screen's answer, which setup progress keeps as `picks`; None when it was not asked."""
    from infra import setup_progress

    picks = (setup_progress.read() or {}).get("picks")
    return picks if isinstance(picks, dict) else None


def _pays_for(picks: object) -> list[str]:
    """The tools the person said they pay for; nothing when they were not asked or said neither."""
    if not isinstance(picks, dict):
        return []
    return [name for name, pick in (("claude_code", "claude"), ("codex", "chatgpt")) if picks.get(pick) is True]


def _app_user_profile() -> dict[str, str | None]:
    from infra import config
    from infra.user_profile import profile_full_name, profile_name

    return {
        "name": profile_name(config.WORKSPACE) or None,
        "full_name": profile_full_name(config.WORKSPACE) or None,
    }


async def _app_setup(model: str | None = None) -> dict[str, list[dict[str, Any]]]:
    from onboarding import setup_decisions

    # The import card names who receives what is read: the model this conductor runs on.
    facts = await setup_decisions.read_facts(self_check_passed=True, probe_web_search=False, agent_model=model)
    # No address in the wake, and no projects consent: the app path never raises it.
    return {"setup_decisions": setup_decisions.build_decisions(replace(facts, email="", session_apps=()))}


def _unavailable(name: str) -> dict[str, str]:
    return {"unavailable": UNAVAILABLE_READS[name]}


async def _read_facts(reads: dict[str, Any]) -> dict[str, Any]:
    tasks = {name: asyncio.ensure_future(read()) for name, read in reads.items()}
    try:
        await asyncio.wait(tasks.values(), timeout=FACTS_TIMEOUT_SECONDS)
        results: dict[str, Any] = {}
        for name, task in tasks.items():
            if not task.done() or task.cancelled():
                log.error("first-run setup fact %s unavailable: unfinished after %ss", name, FACTS_TIMEOUT_SECONDS)
                results[name] = _unavailable(name)
            elif task.exception() is not None:
                log.error("first-run setup fact %s unavailable: read failed", name, exc_info=task.exception())
                results[name] = _unavailable(name)
            else:
                results[name] = task.result()
        return results
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


async def _script_setup_facts(model: str, path: str) -> dict[str, Any]:
    from api.system_reviewer import host_access_reviewer_status
    from infra.db_async import run_in_db, run_off_db
    from onboarding import setup_decisions
    from tools.mcp_import import detected_sources

    facts = await _read_facts({
        "your_model": lambda: run_in_db(_your_model, model),
        "model_sources": lambda: run_in_db(_model_sources),
        "mcp_sources": lambda: run_off_db(detected_sources),
        "setup": lambda: setup_decisions.setup_plan(self_check_passed=True, probe_web_search=False),
        "host_access_reviewer": lambda: run_in_db(host_access_reviewer_status),
        "web_search_key": lambda: run_in_db(_web_search_key),
        "auto_approve": lambda: run_in_db(get_host_access_auto_approve),
    })
    return {"first_run_path": path, **facts}


async def _app_setup_facts(model: str) -> dict[str, Any]:
    from infra.db_async import run_in_db, run_off_db
    from onboarding import setup_decisions

    reads = await _read_facts({
        **({"bound_model": lambda: run_in_db(_bound_model, model)} if model else {}),
        "model_sources": lambda: run_in_db(_model_sources),
        "agents_found": lambda: asyncio.to_thread(_app_agents_found),
        "local_download": lambda: asyncio.to_thread(_app_local_download),
        "web_search_ready": setup_decisions.web_search_works,
        "web_search_key": lambda: run_in_db(_web_search_key),
        "setup_handoff": lambda: run_in_db(_app_setup_handoff),
        "setup_picks": lambda: run_in_db(_app_setup_picks),
        "setup": lambda: _app_setup(model),
        "user_profile": lambda: run_off_db(_app_user_profile),
        "auto_approve": lambda: run_in_db(get_host_access_auto_approve),
    })
    snapshot = reads.pop("model_sources")
    harness_ready: list[str] | dict[str, str]
    github_ready: bool | dict[str, str]
    local_eligible: bool | dict[str, str] = _unavailable("local_eligible")
    if isinstance(snapshot, dict) and "unavailable" not in snapshot:
        harness_ready = _app_harness_ready(snapshot)
        local_eligible = _app_local_eligible(snapshot)
        sources_missing: dict[str, str] = _app_sources_missing(snapshot)
        github_ready = _app_github_ready(snapshot)
    else:
        harness_ready = _unavailable("harness_ready")
        sources_missing = _unavailable("sources_missing")
        github_ready = _unavailable("github_ready")
    handoff = reads["setup_handoff"]
    picks = reads["setup_picks"]
    pays_for: list[str] | dict[str, str] = (
        _unavailable("pays_for") if isinstance(picks, dict) and "unavailable" in picks else _pays_for(picks))
    if isinstance(handoff, dict) and "unavailable" not in handoff:
        tool_attempts = handoff.get("tools")
        if isinstance(tool_attempts, dict):
            tool_attempts = {
                "claude_code": tool_attempts.get("claude-code"),
                "codex": tool_attempts.get("codex"),
            }
    elif isinstance(handoff, dict):
        tool_attempts = _unavailable("tool_attempts")
    else:
        tool_attempts = None
    facts = {
        "first_run_path": "app",
        "harness_ready": harness_ready,
        "sources_missing": sources_missing,
        "agents_found": reads["agents_found"],
        "local_download": reads["local_download"],
        "local_eligible": local_eligible,
        "auto_approve": reads["auto_approve"],
        "tool_attempts": tool_attempts,
        "pays_for": pays_for,
        "web_search_ready": reads["web_search_ready"],
        "web_search_key": reads["web_search_key"],
        "github_ready": github_ready,
        "setup": reads["setup"],
        "user_profile": reads["user_profile"],
    }
    if model:
        facts["bound_model"] = reads["bound_model"]
    return facts


async def setup_facts(model: str, path: str = "script") -> dict[str, Any]:
    """Facts for the selected path, bounded as one read budget and sorted on render."""
    if path == "app":
        return await _app_setup_facts(model)
    if path == "script":
        return await _script_setup_facts(model, path)
    raise ValueError("first_run_path must be 'app' or 'script'")


def render(facts: dict[str, Any], instruction_text: str, summary: str = INLINE_SUMMARY) -> str:
    """The wake content: summary, instructions, then the facts as sorted JSON."""
    from events.delivery import format_event_input
    from infra.onboarding_events import FIRST_RUN_EVENT

    body = "\n\n".join((
        instruction_text,
        "## Setup facts\n\n" + json.dumps(facts, sort_keys=True, ensure_ascii=False, default=str),
    ))
    return format_event_input(FIRST_RUN_EVENT, summary, None, notice=body)


def first_app_step(facts: dict[str, Any]) -> str:
    """Inline only the first applicable numbered section; keep later steps on demand."""
    import re

    from infra.system_package import system_resource_source
    from onboarding.app_first_run import applicable_steps

    steps = applicable_steps(facts)
    number = steps[0]["step_number"] if steps else 9
    reference = system_resource_source(APP_REFERENCE).read_text(encoding="utf-8")
    if number == 9:
        return reference[reference.index("## Hand over to your agents"):].strip()
    match = re.search(rf"(?ms)^{number}\. \*\*.*?(?=^\d+\. \*\*|^Silently,|\Z)", reference)
    if match is None:
        raise ValueError(f"The first-run app reference has no step {number}")
    section = match.group().strip()
    if number == 3:
        # The first question needs no helper/connection instructions until answered.
        section = section.split("   - **Quick**", 1)[0].strip()
    elif number == 4:
        web = system_resource_source("skills/conductor-bootstrap/references/web-search.md").read_text(encoding="utf-8")
        section += "\n\n" + web.split("## 1. Explain, then ask", 1)[1].split("## 2.", 1)[0].strip()
    elif number == 5:
        lookup = system_resource_source("skills/conductor-bootstrap/references/look-up-the-person.md").read_text(encoding="utf-8")
        section += "\n\n" + lookup.split("## Before anything: ask when search was already set up", 1)[1].split("## A.", 1)[0].strip()
    return "## Your first step\n\n" + section


async def render_for(model: str, path: str = "script", *, thread_id: str | None = None) -> str:
    """Render the first run; a completed app setup reuses its committed facts."""
    if path == "app" and app_reference_served():
        from infra.db_async import run_in_db
        from onboarding.app_first_run import (
            IMPORT_DECISION_TITLE,
            read_app_facts,
            read_cached_app_facts,
            reconcile_rendered_app_facts,
        )

        facts = await run_in_db(read_cached_app_facts, thread_id) if thread_id else None
        if facts is None:
            facts = await read_app_facts(model)
        else:
            # The snapshot predates the person's choices: re-read the Auto-approve switch they can change.
            facts = {**facts, "bound_model": await run_in_db(_bound_model, model),
                     **await _read_facts({"auto_approve": lambda: run_in_db(get_host_access_auto_approve)})}
            from onboarding import setup_decisions  # consent names the provider picked after setup
            harness_ready = facts.get("harness_ready", [])
            ready = {"claude-code" if source == "claude_code" else source
                     for source in harness_ready} if isinstance(harness_ready, list) else set()
            setup = facts.get("setup")
            cached_decisions = setup.get("setup_decisions") if isinstance(setup, dict) else None
            # An unavailable marker stays as it is: a partial list beside it would pass for the complete one.
            if isinstance(cached_decisions, list):
                card = await run_in_db(setup_decisions.import_card, model, ready)
                decisions = [item for item in cached_decisions if item.get("title") != IMPORT_DECISION_TITLE]
                imported = setup_decisions.agent_import(card)
                facts["setup"] = {"setup_decisions": ([imported] if imported else []) + decisions}
        if thread_id:
            await run_in_db(reconcile_rendered_app_facts, thread_id, facts)
        return render(facts, instructions("app") + "\n\n" + first_app_step(facts), APP_INLINE_SUMMARY)
    if path == "app":
        log.warning("%s is not served; app first run is degrading to the script text", APP_REFERENCE)
    return render(await setup_facts(model, path), instructions())
