"""The current pull-request facts a GitHub review event can carry.

PR and review webhooks supply ``base_sha``, ``draft`` and ``labels`` from their
``pull_request`` object; PR comments read them because their payload shapes differ.
Three more are only ever read: ``current_head``, the PR's live head, so the recipient of
a rule's delivery or script finds a stale event by comparing it with the event's own head
(``sha`` on a PR event, ``head_sha`` on a checks event; a rule test compares a fact only
with a value, so a rule names a read fact to have it read); ``check_state``, the state
of that own head; and ``mergeable``, which GitHub computes after it sends ``opened`` and
``synchronize``, so those payloads almost always hold ``null``. A settled head has no
payload, so its read also supplies the PR's ``draft``, ``labels`` and author origin
(``HEAD_FACTS``). One ranking (``rank``) orders check conclusions everywhere: a settled
head's ``conclusion`` is it, and every ``check_state`` is ``state_of`` it.
A read happens when an enabled rule of the event's type names a fact the read supplies
(``read_facts``), whichever rule it is and whatever else it tests. The pull request is
read once; the head's check suites once and its check runs page by page. Every read
passes the repo-activity poller's budget and backoff gate; a spent budget is what
bounds the paging. A failed read is never an error: ``check_state`` reads
``unreadable`` and the event still goes out. Check selection, the green conclusions,
the mergeable mapping and author origin come from their owners.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Mapping
from typing import Any

from events import rule_reads
from events.connectors.github_content_facts import COMMENT_READ_TYPE, REPLY_TYPES, author_origin
from events.connectors.github_label_events import label_names
from events.connectors.github_pr_quiet_reasons import GREEN, mergeable_label, rank
from reviewer.gh_helpers import newest_check_runs

log = logging.getLogger(__name__)

#: PR facts from payloads except for comment events, which read them.
PAYLOAD_FACTS = ("base_sha", "draft", "labels")
#: Facts only a read supplies, on every event type that is read.
READ_FACTS = ("current_head", "check_state", "mergeable")
#: A settled head's read also supplies the PR's own facts: nothing else on that event has them.
HEAD_FACTS = ("author_origin", "draft", "labels", *READ_FACTS)
#: Open-PR event types whose facts are completed by a read; a terminal event keeps what its payload holds.
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
    return ((*PAYLOAD_FACTS, *READ_FACTS) if event_type == COMMENT_READ_TYPE else
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
    runner: Any, repo: str, number: int | None, facts: Mapping[str, Any], event_type: str = "",
) -> dict[str, Any]:
    """An event's ``facts`` completed with ``check_state`` of its own head and the PR's ``current_head``
    and ``mergeable``, and, on a settled head's, the PR's own facts (``HEAD_FACTS``); a PR event's
    payload facts stay.

    The own head is ``head_sha`` (a settled head) or ``sha`` (a PR event's payload head); no
    read changes it. A settled head's ``check_state`` is ``state_of`` its ``conclusion``, so its
    checks are not read again. On checks events, labels, draft, mergeable, author_origin
    and current_head describe the PR's live state at the read, not the settled head. A reply
    has no head of its own: its check_state describes current_head. When the PR read fails,
    ``current_head`` is absent and ``check_state`` is ``unreadable``.
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
        if event_type == COMMENT_READ_TYPE:
            out.update(pull_facts(body))
        if "head_sha" in out:
            out.update({name: value for name, value in pull_facts(body).items() if name in HEAD_FACTS})
            if login := _text(_object(body.get("user")).get("login")):
                out["author_origin"] = author_origin(login)
    if isinstance(conclusion := out.get("conclusion"), str):
        out["check_state"] = state_of(conclusion)
    else:
        out["check_state"] = await _check_state(runner, repo, out.get("current_head") if event_type in REPLY_TYPES else out.get("head_sha") or out.get("sha"))
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
    """An open-PR webhook envelope with its facts completed; any other envelope as it was."""
    if envelope is None or envelope.type not in READ_TYPES:
        return envelope
    if runner is None:
        from connections.github_broker import default_broker
        runner = default_broker()
    repo, number = envelope.facts.get("repo"), envelope.facts.get("pr_number")
    if not isinstance(repo, str):
        return envelope
    facts = await head_facts(runner, repo, number if isinstance(number, int) else None, envelope.facts, envelope.type)
    return dataclasses.replace(envelope, facts=facts)
