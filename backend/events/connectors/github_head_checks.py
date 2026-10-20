"""Debounce completed GitHub check suites into one per-head event.

This module owns only the connector-side aggregation.  It deliberately has no
router dependency: the injected sink is the future generic event intake, while
the default sink makes the currently unwired integration visible in logs.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from events.connectors import github_notifications_feed as feed

log = logging.getLogger(__name__)
DEBOUNCE_SECONDS = 30
# No GitHub declarations file exists on dev; this is the local declaration until
# the package adopts the shipped declarations schema.
HEAD_CHECKS_COMPLETED_DECLARATION = {
    "type": "github.head.checks_completed",
    "use": "route",
    "facts": ("repo", "head_sha", "head_branch", "pr_number", "conclusion"),
}


class Refusal(Protocol):
    code: str
    text: str


class IntakeResult(Protocol):
    accepted: bool
    duplicate: bool
    refusal: Refusal | None
    rules_matched: list[str]


class EventSink(Protocol):
    async def admit_event(
        self, *, event_type: str, facts: dict[str, Any],
        origin: Literal["connector", "emitter", "agent", "platform"], producer: str,
        event_id: str, external_id: str | None = None, occurred_at: str | None = None,
    ) -> IntakeResult: ...


class LoggingSink:
    """Temporary default until the generic event intake is wired."""

    async def admit_event(self, **kwargs: Any) -> IntakeResult:
        log.info("github-head-checks: event intake unwired: %s", kwargs["event_id"])
        return _LoggedResult()


class _LoggedResult:
    accepted = False
    duplicate = False
    refusal = None
    rules_matched: list[str] = []


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pr_number(payload: dict[str, Any]) -> int | None:
    for candidate in _object(payload.get("check_suite")).get("pull_requests", []):
        number = _object(candidate).get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            return number
    return None


def _suite_key(suite: dict[str, Any]) -> tuple[str, str]:
    app = _object(suite.get("app"))
    run = _object(suite.get("workflow_run"))
    return (str(app.get("id") or app.get("slug") or "unknown"),
            str(run.get("workflow_id") or suite.get("name") or suite.get("id")))


def _latest_suites(suites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for suite in suites:
        key = _suite_key(suite)
        previous = latest.get(key)
        if previous is None or (str(suite.get("updated_at", "")), suite.get("id", 0)) >= (
            str(previous.get("updated_at", "")), previous.get("id", 0)
        ):
            latest[key] = suite
    return list(latest.values())


def _conclusion(suites: list[dict[str, Any]]) -> str:
    values = [str(suite.get("conclusion") or "failure") for suite in suites]
    failures = ("timed_out", "action_required", "startup_failure", "stale", "failure")
    for value in failures:
        if value in values:
            return value
    if "cancelled" in values:
        return "cancelled"
    return "success"


def _event_id(repo: str, head_sha: str, suites: list[dict[str, Any]]) -> str:
    pairs = sorted((str(suite.get("id")), suite.get("run_attempt", suite.get("attempt", 1)))
                   for suite in suites)
    encoded = json.dumps([repo, head_sha, pairs], separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


class HeadCheckAggregator:
    """One delayed read per quiet repository head; state is intentionally in memory."""

    def __init__(self, runner: feed.GhRunner, sink: EventSink, *,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.runner, self.sink, self.sleep = runner, sink, sleep
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._facts: dict[tuple[str, str], tuple[str | None, int | None]] = {}

    def observe(self, payload: dict[str, Any]) -> None:
        suite, repository = _object(payload.get("check_suite")), _object(payload.get("repository"))
        repo, head_sha = repository.get("full_name"), suite.get("head_sha")
        if not isinstance(repo, str) or not isinstance(head_sha, str):
            return
        key = (repo, head_sha)
        old = self._tasks.pop(key, None)
        if old is not None:
            old.cancel()
        self._facts[key] = (suite.get("head_branch") if isinstance(suite.get("head_branch"), str) else None,
                            _pr_number(payload))
        self._tasks[key] = asyncio.create_task(self._settle(key), name=f"github-head:{repo}:{head_sha}")

    async def _settle(self, key: tuple[str, str]) -> None:
        try:
            await self.sleep(DEBOUNCE_SECONDS)
            repo, head_sha = key
            try:
                response = await asyncio.to_thread(
                    feed.gh_get, self.runner, f"repos/{repo}/commits/{head_sha}/check-suites"
                )
            except Exception:
                log.exception("github-head-checks: aggregate read failed for %s@%s", *key)
                return
            suites = [item for item in _object(response.body).get("check_suites", []) if isinstance(item, dict)]
            latest = _latest_suites(suites)
            if not latest or any(suite.get("status") != "completed" for suite in latest):
                return
            branch, pr_number = self._facts.get(key, (None, None))
            facts = {"repo": repo, "head_sha": head_sha, "head_branch": branch,
                     "pr_number": pr_number, "conclusion": _conclusion(latest)}
            external_id = f"{repo}#{pr_number}" if pr_number is not None else None
            await self.sink.admit_event(event_type="github.head.checks_completed", facts=facts,
                origin="connector", producer="github", event_id=_event_id(repo, head_sha, latest),
                external_id=external_id)
        finally:
            self._tasks.pop(key, None)


_default: HeadCheckAggregator | None = None


def observe_admitted_check_suite(payload: dict[str, Any]) -> None:
    """Hook for admitted ``check_suite.completed`` deliveries."""
    global _default
    if _default is None:
        from reviewer.gh_api import run_gh
        _default = HeadCheckAggregator(run_gh, LoggingSink())
    _default.observe(payload)
