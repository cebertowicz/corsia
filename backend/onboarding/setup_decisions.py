"""The setup decisions of a new install's first run, built by CORSIA.

The conductor raises each decision unchanged and in order; the model never
writes or edits one. Every payload is computed from facts read here: the
person's own oMLX settings, which apps have a session folder (a stat, never a
read), the email saved at install, and whether web search works. Strings live in
``setup_strings``.

The find-projects decision's option A is the session-read consent;
``consent_wordings`` renders what that consent must match.

Each payload carries ``task_title``, the seeded task it is raised from; the
conductor uses it to find ``source_node_id`` and never passes it on.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from infra import onboarding_events as tasks
from onboarding import setup_strings as text

OMLX_SETTING = "max_concurrent_requests"
SEARCH_QUERY = "CORSIA"
MAX_NAME_CHARS = 40
GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Facts:
    """What the decisions are built from.

    ``omlx_differences`` is None when the person has no oMLX of their own or its
    settings are unknown, and empty when they already match. ``email`` is the
    address the person confirmed at install, or empty.
    """

    omlx_differences: tuple[dict[str, Any], ...] | None = None
    web_search_works: bool = False
    self_check_passed: bool = True
    session_apps: tuple[str, ...] = ()
    email: str = ""


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
            for index, (label, detail) in enumerate(strings["options"])]


def _pick_one(task_title: str, strings: dict[str, str], recommendation: str,
              **fields: str) -> dict[str, Any]:
    """A yes/no decision: option A acts, option B declines."""
    options = [
        _option("A", strings["yes_label"].format(**fields), strings["yes_detail"].format(**fields)),
        _option("B", strings["no_label"].format(**fields), strings["no_detail"].format(**fields)),
    ]
    return _decision(task_title, strings, options=options, recommendation=recommendation, **fields)


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


def consent_wordings(session_apps: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """(title, context, option A label) of the decision that may authorize the read now.

    Rendered from current facts exactly as the decision is built, so a consent
    raised before the facts changed no longer matches and is raised again.
    """
    rendered = find_projects(session_apps)
    if rendered is None:
        return []
    return [(rendered["title"], rendered["context"], rendered["options"][0]["label"])]


def _github(email: str) -> dict[str, Any]:
    return _decision(tasks.TASK_GITHUB, text.GITHUB, options=_lettered(text.GITHUB),
                     recommendation="A",
                     sign_up_with=agents_email(email) or text.ASK_FOR_ADDRESS)


def _omlx(facts: Facts) -> dict[str, Any] | None:
    if not facts.self_check_passed:
        return None
    entry = next((row for row in facts.omlx_differences or () if row["setting"] == OMLX_SETTING), None)
    if entry is None:
        return None
    return _pick_one(tasks.TASK_CONFIG, text.OMLX, "A",
                     current=_safe(entry["current"]), recommended=_safe(entry["recommended"]))


def build_decisions(facts: Facts) -> list[dict[str, Any]]:
    """The complete decisions to raise, in setup-task order, each as-is."""
    omlx = _omlx(facts)
    projects = find_projects(facts.session_apps)
    return [
        *([omlx] if omlx else []),
        _github(facts.email),
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
    """One real search with the key web_search would use; no key means no."""
    from infra.db_async import run_in_db
    from infra.key_store import WEB_SEARCH, resolve_builtin
    from tools.web_search_outcome import CHECK_QUERY, WORKS, run_search

    resolved = await run_in_db(resolve_builtin, WEB_SEARCH)
    if resolved is None:
        return False
    return (await run_search(resolved[0], CHECK_QUERY, 1)).kind == WORKS


def _saved_email() -> str:
    """The email the person confirmed at install, from the workspace's user profile."""
    from infra import config
    from infra.user_profile import profile_email

    return profile_email(config.WORKSPACE)


async def read_facts(*, self_check_passed: bool = True, probe_web_search: bool = True) -> Facts:
    """Read the facts. With CORSIA's own API unreachable, oMLX is left out.

    Without ``probe_web_search`` no search runs and web search reads as not working,
    so its decision stays for the conductor to settle after testing it itself.
    """
    from infra.db_async import run_in_db, run_off_db

    from onboarding.project_discovery import Roots, session_apps

    omlx: tuple[dict[str, Any], ...] | None = None
    sessions = tuple(await run_off_db(session_apps, Roots.for_user()))
    if self_check_passed:
        from api.provider_sources import list_sources

        for row in await run_in_db(list_sources):
            if row.get("source") == "omlx" and row.get("omlx_settings") is not None:
                omlx = tuple((row["omlx_settings"] or {}).get("differences") or ())
    works = await web_search_works() if probe_web_search else False
    return Facts(omlx, works, self_check_passed, sessions, await run_off_db(_saved_email))


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
