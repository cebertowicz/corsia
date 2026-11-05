"""PR facts from webhook payloads and rule-gated reads.

PR events supply ``base_sha``, ``draft`` and ``labels`` from their payload. Replies
(created PR comments and submitted reviews) read all three from the PR's live state,
even when the PR is closed. Every enriched type also reads ``current_head`` and
``mergeable``. Its ``check_state`` describes ``head_sha``, else ``sha``, else that
live head. A settled head reuses its ranked ``conclusion`` without reading checks
again, and its read supplies ``HEAD_FACTS`` including PR author origin.

An enabled rule naming a fact from ``read_facts`` requests enrichment. The PR is
read once, its head's check suites once and check runs page by page. Every request
uses the repo-activity poller's existing budget/backoff gate, which bounds paging.
A failed PR read leaves ``check_state`` = ``unreadable`` with no ``current_head``;
the reply still goes out. Ranking, green conclusions, mergeability and author
origin come from their canonical owners.
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
from events.connectors.github_pr_quiet_reasons import GREEN, mergeable_label, rank
from reviewer.gh_helpers import newest_check_runs

log = logging.getLogger(__name__)

#: PR payload facts; replies and settled heads fill missing values from a read.
PAYLOAD_FACTS = ("base_sha", "draft", "labels")
#: Facts only a read supplies, on every event type that is read.
READ_FACTS = ("current_head", "check_state", "mergeable")
#: A settled head's read also supplies the PR's own facts: nothing else on that event has them.
HEAD_FACTS = ("author_origin", *PAYLOAD_FACTS, *READ_FACTS)
#: PR lifecycle and reply types completed by a read; replies include closed PRs.
READ_TYPES = frozenset({"pr.opened", "pr.synchronize", "pr.ready_for_review", "pr.labeled", "pr.unlabeled"}) | REPLY_TYPES
HEAD_TYPE = "head.checks_completed"
UNREADABLE = "unreadable"


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


def state_of(conclusion: str) -> str:
    """A completed head's ``check_state`` from its ``rank``: green only in ``GREEN``, the set the merge gate counts."""
    return conclusion if conclusion == "action_required" else "green" if conclusion in GREEN else "failing"


def check_state(suites: list[Any], runs: list[Any]) -> str:
    """The head's one state from its check suites and its newest run per check name.

    A held suite has no runs, so only the suites show it. Only check suites and runs
    are read, never commit statuses, so the reviewer's ``qa/llm-reviewer*`` statuses
    never count. A head with a non-green conclusion is decided by ``rank``; otherwise
    one still running, or with no runs, is pending.
    """
    newest = newest_check_runs([run for run in runs if isinstance(run, dict)])
    items = [*counted_suites(suites), *newest]
    state = state_of(rank(item.get("conclusion") for item in items if item.get("status") == "completed"))
    if state != "green":
        return state
    if not newest or any(item.get("status") != "completed" for item in items):
        return "pending"
    return "green"


def read_facts(event_type: str) -> tuple[str, ...]:
    """The facts a read supplies for one event type; none for a type that is never read."""
    return ((*PAYLOAD_FACTS, *READ_FACTS) if event_type in REPLY_TYPES else
            READ_FACTS if event_type in READ_TYPES else HEAD_FACTS if event_type == HEAD_TYPE else ())


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


async def _check_state(runner: Any, repo: str, sha: Any) -> str:
    """``sha``'s check state from its suites and every page of its runs.

    A failed suites read is ``unreadable``. Runs are read until ``total_count``: a failed or
    empty page before then, or no ``total_count``, is ``unreadable``; so is a page the read
    budget refuses, which is what bounds a head with very many runs.
    """
    if not isinstance(sha, str) or not sha:
        return UNREADABLE
    suites = _object(await _read(runner, repo, f"repos/{repo}/commits/{sha}/check-suites")).get("check_suites")
    if not isinstance(suites, list):
        return UNREADABLE
    runs: list[Any] = []
    page = 1
    while True:
        body = _object(await _read(runner, repo, f"repos/{repo}/commits/{sha}/check-runs?per_page=100&page={page}"))
        batch, total = body.get("check_runs"), body.get("total_count")
        if not isinstance(batch, list) or not isinstance(total, int):
            return UNREADABLE
        runs += batch
        if len(runs) >= total:
            return check_state(suites, runs)
        if not batch:
            return UNREADABLE
        page += 1


async def head_facts(
    runner: Any, repo: str, number: int | None, facts: Mapping[str, Any],
) -> dict[str, Any]:
    """Complete PR facts without replacing values already supplied by the event.

    The check head is ``head_sha``, else ``sha``, else ``current_head``. Replies
    supply neither own-head fact, so their checks describe the PR's live head.
    Settled heads reuse ``state_of(conclusion)`` without rereading checks. Missing
    payload facts are filled from the PR read; existing PR payload values win.
    A failed PR read leaves no ``current_head`` and marks checks ``unreadable``.
    """
    out = dict(facts)
    if number is not None:
        body = await _pull(runner, repo, number)
        if body is None:
            out["check_state"] = UNREADABLE
            return out
        if live_head := _text(_object(body.get("head")).get("sha")):
            out["current_head"] = live_head
        out["mergeable"] = mergeable_label(body.get("mergeable"))
        for name, value in pull_facts(body).items():
            out.setdefault(name, value)
        if "head_sha" in out:
            if login := _text(_object(body.get("user")).get("login")):
                out["author_origin"] = author_origin(login)
    if isinstance(conclusion := out.get("conclusion"), str):
        out["check_state"] = state_of(conclusion)
    else:
        out["check_state"] = await _check_state(runner, repo, out.get("head_sha") or out.get("sha") or out.get("current_head"))
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
