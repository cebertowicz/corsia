"""The setup decisions of a new install's first run, built by CORSIA.

The conductor raises a decision unchanged; the model never writes or edits one.
The GitHub cards are alternatives: the person's chat answer about having an
account (or, on the script path, the install's own question) picks which one
is raised, so the list holds every card a path may raise, not a sequence. Every payload is computed from facts read here: which apps
have a session folder (a stat, never a read), the email saved at install, and
whether web search works. Strings live in ``setup_strings``.

The find-projects decision's option A is the session-read consent;
``consent_wordings`` renders what that consent must match. The import decision's
option A is the consent to read the person's other tools, built only when one
is found and the model the CORSIA agent runs on is known; it goes first.
``agent_import_wordings`` renders every card that may authorize that read now.

Each payload carries ``task_title``, the seeded task it is raised from; the
conductor uses it to find ``source_node_id`` and never passes it on.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, replace
from typing import Any

from infra import onboarding_events as tasks
from onboarding import setup_strings as text


SEARCH_QUERY = "CORSIA"
MAX_NAME_CHARS = 40
GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Facts:
    """What the decisions are built from.

    ``web_search_works`` records the setup probe, ``self_check_passed`` records
    the caller's self-check result, ``session_apps`` are the apps with session
    folders, and ``email`` is the address the person confirmed at install.
    """

    web_search_works: bool = False
    self_check_passed: bool = True
    session_apps: tuple[str, ...] = ()
    email: str = ""
    agent_import: ImportCard | None = None


@dataclass(frozen=True)
class ImportCard:
    """The import card's inputs: found tools, the ready Claude Code or Codex among them,
    and who receives the read: a known provider's name, else the model's name (empty when local)."""

    tools: tuple[str, ...]
    connections: tuple[str, ...] = ()
    provider: str = ""


def _safe(value: object) -> str:
    return _CONTROL.sub(" ", str(value)).strip()[:MAX_NAME_CHARS]


def _option(option_id: str, label: str, detail: str) -> dict[str, str]:
    return {"id": option_id, "label": label, "detail": detail}


def _decision(task_title: str, strings: dict[str, str], *, options: list[dict[str, str]],
              recommendation: Any, **fields: str) -> dict[str, Any]:
    return {
        "task_title": task_title,
        "title": strings["title"].format(**fields),
        "context": strings["context"].format(**fields),
        "tradeoff": strings["tradeoff"].format(**fields),
        "recommendation": recommendation,
        "rationale": strings["rationale"],
        "options": options,
        "other_enabled": True,
    }


def _lettered(strings: dict[str, Any]) -> list[dict[str, str]]:
    return [_option(chr(ord("A") + index), label, detail)
            for index, (label, detail, *_answer) in enumerate(strings["options"])]



def _join(items: list[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    return ", ".join(items[:-1]) + f" and {items[-1]}" if len(items) > 1 else "".join(items)


def agents_email(email: str) -> str:
    """The agents' GitHub address: the person's Gmail with ``+agents``, else empty.

    Only Gmail ignores a ``+tag``, so no other provider's address is derived.
    """
    local, _, domain = _CONTROL.sub("", email).strip().partition("@")
    if domain.lower() not in GMAIL_DOMAINS or not local.split("+")[0]:
        return ""
    return f"{local.split('+')[0]}+agents@{domain.lower()}"


def find_projects(session_apps: tuple[str, ...]) -> dict[str, Any] | None:
    """The projects-only consent decision, or None when no app has session folders."""
    if not session_apps:
        return None
    return _decision(tasks.TASK_PROJECT, text.FIND_PROJECTS,
                     options=_lettered(text.FIND_PROJECTS), recommendation="A",
                     apps=_join(list(session_apps)))


def agent_import(card: ImportCard | None) -> dict[str, Any] | None:
    """The consent to read the person's other tools, or None when none was found."""
    if card is None or not card.tools:
        return None
    connections = text.AGENT_IMPORT_CONNECTIONS.format(
        tools=_join(list(card.connections)), verb="uses" if len(card.connections) == 1 else "use",
    ) if card.connections else ""
    settings = text.AGENT_IMPORT_OTHER_SETTINGS if card.connections else text.AGENT_IMPORT_SETTINGS
    if not card.provider:
        where = text.AGENT_IMPORT_LOCAL
    elif card.provider in text.AGENT_IMPORT_PROVIDERS.values():
        where = text.AGENT_IMPORT_CLOUD.format(provider=card.provider)
    else:
        where = text.AGENT_IMPORT_OTHER.format(model=_safe(card.provider))
    return _decision(tasks.TASK_START, text.AGENT_IMPORT, options=_lettered(text.AGENT_IMPORT),
                     recommendation="A", tools=_join(list(card.tools)),
                     verb="is" if len(card.tools) == 1 else "are", connections=connections,
                     settings=settings, where=where)


def agent_import_wordings(card: ImportCard | None) -> list[tuple[tuple[str, str, str], tuple[str, ...]]]:
    """Each card that may authorize the read now, with the tools it names.

    The card names every found tool, or a narrower set the person asked for;
    each is rendered from current facts exactly as it is built.
    """
    if card is None:
        return []
    wordings = []
    for size in range(len(card.tools), 0, -1):
        for tools in itertools.combinations(card.tools, size):
            rendered = agent_import(replace(
                card, tools=tools, connections=tuple(name for name in card.connections if name in tools),
            ))
            if rendered is not None:
                wordings.append(((rendered["title"], rendered["context"], rendered["options"][0]["label"]), tools))
    return wordings


def consent_wordings(session_apps: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """(title, context, option A label) of the decision that may authorize the read now.

    Rendered from current facts exactly as the decision is built, so a consent
    raised before the facts changed no longer matches and is raised again.
    """
    rendered = find_projects(session_apps)
    if rendered is None:
        return []
    return [(rendered["title"], rendered["context"], rendered["options"][0]["label"])]


def _community(strings: dict[str, Any]) -> dict[str, Any]:
    return _decision(tasks.TASK_GITHUB, strings, options=_lettered(strings), recommendation="A")


def community_card() -> dict[str, Any]:
    """The community decision as built; it needs no facts."""
    return _community(text.COMMUNITY)


def _github(email: str, strings: dict[str, Any] = text.GITHUB) -> dict[str, Any]:
    address = agents_email(email)
    return _decision(tasks.TASK_GITHUB, strings, options=_lettered(strings),
                     recommendation="A",
                     sign_up_with=text.AGENTS_ADDRESS.format(address=address) if address else text.ASK_FOR_ADDRESS)



def build_decisions(facts: Facts) -> list[dict[str, Any]]:
    """Every decision a path may raise, in setup-task order, each as-is. The GitHub cards are alternatives: raise the one that fits."""
    projects = find_projects(facts.session_apps)
    imported = agent_import(facts.agent_import)
    return [
        *([imported] if imported else []),
        _community(text.COMMUNITY),
        _github(facts.email),
        _github(facts.email, text.GITHUB_NO_ACCOUNT),
        *([projects] if projects else []),
    ]


def build_settlements(facts: Facts) -> list[dict[str, str]]:
    """Tasks CORSIA settles itself because no step applies; the conductor applies each as given.

    Only web search settles: every other seeded task holds a chat step.
    """
    if not facts.web_search_works:
        return []
    return [{"task_title": tasks.TASK_WEB_SEARCH, "outcome": "done", "text": text.WEB_SEARCH_WORKS}]


def build_plan(facts: Facts) -> dict[str, Any]:
    decisions = build_decisions(facts)
    return {
        "setup_decisions": decisions,
        "setup_task_settlements": build_settlements(facts),
    }


async def web_search_works() -> bool:
    """Whether one real check search works: Brave with the saved key, else keyless
    through DDGS, which counts only when it returns a result."""
    from infra.db_async import run_in_db
    from infra.key_store import WEB_SEARCH, resolve_builtin
    from tools.web_keyless_search import keyless_search
    from tools.web_search_outcome import CHECK_QUERY, WORKS, run_search

    resolved = await run_in_db(resolve_builtin, WEB_SEARCH)
    if resolved is None:
        keyless = await keyless_search(CHECK_QUERY)
        return keyless.kind == WORKS and bool(keyless.results)
    return (await run_search(resolved[0], CHECK_QUERY, 1)).kind == WORKS


def _saved_email() -> str:
    """The email the person confirmed at install, from the workspace's user profile."""
    from infra import config
    from infra.user_profile import profile_email

    return profile_email(config.WORKSPACE)


def model_provider(model: str) -> str | None:
    """Who receives what the CORSIA agent reads on ``model``: '' when it is local, else
    a known provider's name or the model's own; None when the model cannot be resolved.
    An External endpoint is not local: it may front a cloud API."""
    from llm.pricing import is_local_model
    from llm.resolution import ModelUnavailableError, resolve_execution_target

    try:
        target = resolve_execution_target(model)
    except (ModelUnavailableError, ValueError):
        return None
    if is_local_model(model):
        return ""
    return (text.AGENT_IMPORT_PROVIDERS.get(target.provider)
            or text.AGENT_IMPORT_PROVIDERS.get(target.harness) or target.display_name)


def import_card(model: str, ready_sources: set[str]) -> ImportCard | None:
    """Local import discovery, or None with no tool or no known model."""
    from onboarding import agent_import as reader
    from onboarding.project_discovery import Roots

    found = reader.found_tools(Roots.for_user())
    provider = model_provider(model) if found else None
    if provider is None:
        return None
    return ImportCard(
        tuple(reader.LABELS[tool] for tool in found),
        tuple(reader.LABELS[tool] for tool in found
              if tool in (reader.CLAUDE_CODE, reader.CODEX) and tool in ready_sources),
        provider,
    )


def ready_sources(rows: list[dict[str, Any]]) -> set[str]:
    return {str(row["source"]) for row in rows if row.get("state") == "ready" and row.get("source")}


async def read_facts(*, self_check_passed: bool = True, probe_web_search: bool = True,
                     agent_model: str | None = None) -> Facts:
    """Read the facts used to build setup decisions.

    Without ``probe_web_search`` no search runs and web search reads as not working,
    so its decision stays for the conductor to settle after testing it itself.
    The import card is built only for ``agent_model``, the model the CORSIA agent
    raising it runs on.
    """
    from infra.db_async import run_in_db, run_off_db

    from onboarding.project_discovery import Roots, session_apps

    sessions = tuple(await run_off_db(session_apps, Roots.for_user()))
    rows: list[dict[str, Any]] = []
    if agent_model:
        from api.provider_sources import list_sources

        rows = await run_in_db(list_sources)

    works = await web_search_works() if probe_web_search else False
    card = await run_in_db(import_card, agent_model, ready_sources(rows)) if agent_model else None
    return Facts(works, self_check_passed, sessions, await run_off_db(_saved_email), card)


async def setup_plan(*, self_check_passed: bool = True, probe_web_search: bool = True) -> dict[str, Any]:
    return build_plan(await read_facts(self_check_passed=self_check_passed, probe_web_search=probe_web_search))


SETUP_TITLES = frozenset({
    tasks.TASK_GITHUB, tasks.TASK_WEB_SEARCH, tasks.TASK_CONFIG, tasks.TASK_PHONE,
})


def has_open_setup_task(thread_id: str) -> bool:
    """True while the thread's plan holds a seeded setup task that is not settled: first run."""
    from infra.database import get_db
    from plans import public

    db = get_db()
    try:
        return public.has_open_titled_node(db, thread_id=thread_id, titles=SETUP_TITLES)
    finally:
        db.close()
