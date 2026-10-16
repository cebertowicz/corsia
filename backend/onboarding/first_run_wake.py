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

log = logging.getLogger(__name__)

FIRST_RUN_REFERENCE = "skills/conductor-bootstrap/references/first-run.md"
APP_REFERENCE = "skills/conductor-bootstrap/references/first-run-app.md"
#: The whole set of fact reads shares this budget; the first model pick waits no longer.
FACTS_TIMEOUT_SECONDS = 5.0
APP_FACT_KEYS = frozenset({
    "first_run_path", "harness_ready", "sources_missing", "local_download",
    "gmail_eligible", "web_search_ready", "github_ready", "own_omlx_source",
    "setup", "user_profile",
})
UNAVAILABLE_READS = {
    "your_model": "none; omit task 1's bracket",
    "model_sources": "tool model_sources, once, without recheck",
    "mcp_sources": "corsia_api GET /api/mcp/sources",
    "setup": "setup_decisions.read_facts once, without web search",
    "host_access_reviewer": "none; corsia_api hides host-access fields, so treat the reviewer as unknown",
    "web_search_key": "one web_search call",
    "harness_ready": "tool model_sources, once, without recheck",
    "sources_missing": "tool model_sources, once, without recheck",
    "local_download": "corsia_api GET /api/providers/local/setup",
    "gmail_eligible": "none; treat as no for gmail_eligible",
    "web_search_ready": "one web_search call",
    "github_ready": "tool model_sources, once, without recheck",
    "own_omlx_source": "local oMLX ownership and source read, once",
    "user_profile": "preferred-name read from the workspace profile",
}

INLINE_SUMMARY = (
    "First run of a new install. Your instructions are below: skill "
    "conductor-bootstrap's references/first-run.md. The Setup facts section "
    "holds what its Preparation reads; do not load the skill or call those "
    "tools for them. A fact marked unavailable names the one read that gets it."
)
APP_INLINE_SUMMARY = (
    "First run of a new install. Your instructions are below: skill "
    "conductor-bootstrap's references/first-run.md followed by first-run-app.md. "
    "The Setup facts section holds the facts the app-path instructions read."
)


def app_reference_served() -> bool:
    """Whether the app-path instruction reference is present in this System package."""
    from infra.system_package import system_resource_source

    return system_resource_source(APP_REFERENCE).is_file()


def instructions(path: str = "script") -> str:
    """The served first-run reference, plus the app reference for the app path."""
    from infra.system_package import system_resource_source

    first_run = system_resource_source(FIRST_RUN_REFERENCE).read_text(encoding="utf-8").strip()
    if path == "script":
        return first_run
    if path == "app":
        app = system_resource_source(APP_REFERENCE).read_text(encoding="utf-8").strip()
        return f"{first_run}\n\n{app}"
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


def _web_search_key() -> bool:
    """Whether a web search key is saved; whether it works is the conductor's to test."""
    from infra.key_store import WEB_SEARCH, resolve_builtin

    return resolve_builtin(WEB_SEARCH) is not None


def _app_harness_ready(snapshot: dict[str, Any]) -> list[str]:
    ready = {row.get("source") for row in snapshot["sources"] if row.get("state") == "ready"}
    return [name for name, source in (("claude_code", "claude-code"), ("codex", "codex")) if source in ready]


def _app_sources_missing(snapshot: dict[str, Any]) -> dict[str, str]:
    """The CLI sources F8 can offer, from the snapshot that owns their states."""
    states = {row.get("source"): row.get("state") for row in snapshot["sources"]}
    return {
        name: state for name, source in (("claude_code", "claude-code"), ("codex", "codex"))
        if (state := states.get(source)) in {"not_installed", "needs_auth"}
    }


def _app_local_download() -> dict[str, Any]:
    """The local setup flight in the compact form the app first-run guidance reads."""
    from llm.local_setup import setup_status

    status = setup_status()
    state = status["state"]
    if state in {"running", "downloading"}:
        return {"state": "downloading", "progress": status["stage"]}
    if state == "succeeded":
        return {"state": "done"}
    if state == "failed":
        return {"state": "failed", "message": status["result"].get("message")}
    return {"state": "none"}


def _app_github_ready(snapshot: dict[str, Any]) -> bool:
    return any(snapshot[key].get("state") == "ready" for key in ("github_agent", "github"))


def _own_omlx_source() -> bool:
    from llm.local_setup_ownership import existing_install_is_foreign
    from llm.network_discovery import get_servers

    if not existing_install_is_foreign():
        return False
    return any(
        server.get("kind") != "external" and server.get("status") == "online"
        for server in get_servers(provider="omlx")
    )


def _gmail_eligible() -> bool:
    """The Google email-and-calendar setup offer is available to every install."""
    return True


def _app_user_profile() -> dict[str, str | None]:
    from infra import config
    from infra.user_profile import profile_name

    return {
        "name": profile_name(config.WORKSPACE) or None,
        "work": None,
        "place": None,
        "check_first": None,
        "answer_style": None,
    }


async def _app_setup() -> dict[str, list[dict[str, Any]]]:
    from onboarding import setup_decisions

    facts = await setup_decisions.read_facts(self_check_passed=True, probe_web_search=False)
    # No address in the wake, and no projects consent: the app path never raises it.
    return {"setup_decisions": setup_decisions.build_decisions(replace(facts, email="", session_apps=()))}


def _unavailable(name: str) -> dict[str, str]:
    return {"unavailable": UNAVAILABLE_READS[name]}


async def _read_facts(reads: dict[str, Any]) -> dict[str, Any]:
    tasks = {name: asyncio.ensure_future(read()) for name, read in reads.items()}
    await asyncio.wait(tasks.values(), timeout=FACTS_TIMEOUT_SECONDS)
    results: dict[str, Any] = {}
    for name, task in tasks.items():
        if not task.done():
            task.cancel()
            log.error("first-run setup fact %s unavailable: unfinished after %ss", name, FACTS_TIMEOUT_SECONDS)
            results[name] = _unavailable(name)
        elif task.exception() is not None:
            log.error("first-run setup fact %s unavailable: read failed", name, exc_info=task.exception())
            results[name] = _unavailable(name)
        else:
            results[name] = task.result()
    return results


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
    })
    return {"first_run_path": path, **facts}


async def _app_setup_facts() -> dict[str, Any]:
    from infra.db_async import run_in_db, run_off_db
    from onboarding import setup_decisions

    reads = await _read_facts({
        "model_sources": lambda: run_in_db(_model_sources),
        "local_download": lambda: asyncio.to_thread(_app_local_download),
        "gmail_eligible": lambda: run_off_db(_gmail_eligible),
        "web_search_ready": setup_decisions.web_search_works,
        "own_omlx_source": lambda: run_off_db(_own_omlx_source),
        "setup": _app_setup,
        "user_profile": lambda: run_off_db(_app_user_profile),
    })
    snapshot = reads.pop("model_sources")
    harness_ready: list[str] | dict[str, str]
    github_ready: bool | dict[str, str]
    if isinstance(snapshot, dict) and "unavailable" not in snapshot:
        harness_ready = _app_harness_ready(snapshot)
        sources_missing: dict[str, str] = _app_sources_missing(snapshot)
        github_ready = _app_github_ready(snapshot)
    else:
        harness_ready = _unavailable("harness_ready")
        sources_missing = _unavailable("sources_missing")
        github_ready = _unavailable("github_ready")
    return {
        "first_run_path": "app",
        "harness_ready": harness_ready,
        "sources_missing": sources_missing,
        "local_download": reads["local_download"],
        "gmail_eligible": reads["gmail_eligible"],
        "web_search_ready": reads["web_search_ready"],
        "github_ready": github_ready,
        "own_omlx_source": reads["own_omlx_source"],
        "setup": reads["setup"],
        "user_profile": reads["user_profile"],
    }


async def setup_facts(model: str, path: str = "script") -> dict[str, Any]:
    """Facts for the selected path, bounded as one read budget and sorted on render."""
    if path == "app":
        return await _app_setup_facts()
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


async def render_for(model: str, path: str = "script") -> str:
    """The full first-run wake for a conductor about to run on ``model``."""
    if path == "app" and app_reference_served():
        return render(await setup_facts(model, "app"), instructions("app"), APP_INLINE_SUMMARY)
    if path == "app":
        log.warning("%s is not served; app first run is degrading to the script text", APP_REFERENCE)
    return render(await setup_facts(model, path), instructions())
