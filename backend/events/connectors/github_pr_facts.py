"""PR facts from webhook payloads and rule-gated reads.

PR events supply ``base_sha``, ``draft`` and ``labels`` from their payload. Replies
(created PR comments and submitted reviews) read all three from the PR's live state,
even when the PR is closed. A settled head's read supplies ``HEAD_FACTS`` including PR
author origin.

An enabled rule naming a fact from ``read_facts`` requests enrichment. The PR is read
once through the repo-activity poller's existing budget/backoff gate. A failed PR read
leaves the facts as the event gave them; the reply still goes out. Author origin comes
from its canonical owner.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Mapping
from typing import Any

from events import rule_reads
from events.connectors.github_content_facts import author_origin
from events.connectors.github_reply_facts import REPLY_TYPES
from events.connectors.github_label_events import label_names

log = logging.getLogger(__name__)

#: PR payload facts; replies and settled heads fill missing values from a read.
PAYLOAD_FACTS = ("base_sha", "draft", "labels")
#: A settled head's read also supplies the PR's own facts: nothing else on that event has them.
HEAD_FACTS = ("author_origin", *PAYLOAD_FACTS)
#: Reply types completed by a read; replies include closed PRs.
READ_TYPES = REPLY_TYPES
HEAD_TYPE = "head.checks_completed"


def _object(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def pull_facts(pull: Mapping[str, Any]) -> dict[str, Any]:
    """``PAYLOAD_FACTS`` from a pull request object, a webhook's or a REST read's."""
    facts = {
        "base_sha": _text(_object(pull.get("base")).get("sha")),
        "draft": pull.get("draft") if isinstance(pull.get("draft"), bool) else None,
        "labels": label_names(pull["labels"]) if isinstance(pull.get("labels"), list) else None,
    }
    return {key: value for key, value in facts.items() if value is not None}


def counted_suites(suites: Any) -> list[dict[str, Any]]:
    """The suites a head waits on: those with runs, and those held for approval (no runs, ``action_required``)."""
    return [suite for suite in suites if isinstance(suite, dict) and (
        (suite.get("latest_check_runs_count") or 0) > 0 or suite.get("conclusion") == "action_required")
    ] if isinstance(suites, list) else []


def read_facts(event_type: str) -> tuple[str, ...]:
    """The facts a read supplies for one event type; none for a type that is never read."""
    return (PAYLOAD_FACTS if event_type in REPLY_TYPES else
            HEAD_FACTS if event_type == HEAD_TYPE else ())


async def _read(runner: Any, repo: str, path: str) -> Any:
    from events.connectors import github_repo_activity as poller

    try:
        response, _reason = await asyncio.shield(poller.gated_read(runner, repo, path))  # counted even if cancelled
    except Exception:
        log.warning("github-pr-facts: read failed for %s", path, exc_info=True)
        return None
    return response.body if response is not None and response.status == 200 else None


async def _pull(runner: Any, repo: str, number: int) -> Mapping[str, Any] | None:
    body = await _read(runner, repo, f"repos/{repo}/pulls/{number}")
    return body if isinstance(body, Mapping) else None


async def head_facts(
    runner: Any, repo: str, number: int | None, facts: Mapping[str, Any],
) -> dict[str, Any]:
    """Complete PR facts without replacing values already supplied by the event.

    Missing payload facts are filled from the PR read; existing PR payload values win.
    A failed PR read leaves the facts as given.
    """
    out = dict(facts)
    if number is not None:
        body = await _pull(runner, repo, number)
        if body is None:
            return out
        for name, value in pull_facts(body).items():
            out.setdefault(name, value)
        if "head_sha" in out:
            if login := _text(_object(body.get("user")).get("login")):
                out["author_origin"] = author_origin(login)
    return out


async def settled_head_facts(runner: Any, repo: str, number: int | None, facts: Mapping[str, Any]) -> dict[str, Any]:
    """A settled head's ``facts``, completed when an enabled rule names a fact the read supplies.

    A failed rule read reads anyway: no rule is ever evaluated without a fact it names.
    """
    from events.connectors.github import MANIFEST  # github.py imports this module

    try:
        named = (await asyncio.to_thread(rule_reads.type_rules, MANIFEST.connector, HEAD_TYPE, HEAD_FACTS))[2]
    except Exception:
        log.warning("github-pr-facts: rule read failed for %s; reading anyway", repo, exc_info=True)
        named = True
    return await head_facts(runner, repo, number, facts) if named else dict(facts)


async def enriched(envelope: Any, runner: Any = None) -> Any:
    """Complete a declared PR or reply envelope; leave other types unchanged."""
    if envelope is None or envelope.type not in READ_TYPES:
        return envelope
    if runner is None:
        from connections.github_broker import default_broker
        runner = default_broker()
    repo, number = envelope.facts.get("repo"), envelope.facts.get("pr_number")
    if not isinstance(repo, str):
        return envelope
    facts = await head_facts(runner, repo, number if isinstance(number, int) else None, envelope.facts)
    return dataclasses.replace(envelope, facts=facts)
