"""Debounce completed GitHub check suites into one per-head event.

This module owns only the connector-side aggregation.  It deliberately has no
router dependency: the injected sink is the generic event intake (``admit_event``,
origin ``connector``), and ``event_id`` comes from the head's check suites, never
from a delivery.  Its reads pass the repo-activity poller's budget and backoff
gate, and the admission hook stays off unless explicitly enabled.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from events.connectors import github_notifications_feed as feed
from events.connectors import github_repo_activity as poller

log = logging.getLogger(__name__)
DEBOUNCE_SECONDS = 30
MAX_ATTEMPTS = 4
ENABLE_ENV = "CORSIA_GITHUB_HEAD_CHECKS_ENABLE"


class Refusal(Protocol):
    @property
    def code(self) -> str: ...
    @property
    def text(self) -> str: ...


class IntakeResult(Protocol):
    @property
    def accepted(self) -> bool: ...
    @property
    def duplicate(self) -> bool: ...
    @property
    def refusal(self) -> Refusal | None: ...
    @property
    def rules_matched(self) -> int: ...


class EventSink(Protocol):
    async def admit_event(
        self, *, event_type: str, facts: dict[str, Any],
        origin: Literal["connector", "emitter", "agent", "platform"], producer: str,
        event_id: str, external_id: str | None = None, occurred_at: str | None = None,
    ) -> IntakeResult: ...


class IntakeSink:
    """The generic event intake; ``github.head.checks_completed`` is declared in the connector manifest."""

    async def admit_event(
        self, *, event_type: str, facts: dict[str, Any],
        origin: Literal["connector", "emitter", "agent", "platform"], producer: str,
        event_id: str, external_id: str | None = None, occurred_at: str | None = None,
    ) -> IntakeResult:
        # The PR artifact is built from the facts, so intake takes neither external_id nor occurred_at.
        from events.intake import admit_event
        return await admit_event(event_type=event_type, facts=facts, origin=origin,
                                 producer=producer, event_id=event_id)


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pr_number(payload: dict[str, Any]) -> int | None:
    for candidate in _object(payload.get("check_suite")).get("pull_requests", []):
        number = _object(candidate).get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            return number
    return None


def _current_suites(body: Any) -> list[dict[str, Any]]:
    """REST suites have independent identities, even when they share an app.

    The REST shape names no workflow; a re-run reuses the suite and moves its
    ``updated_at``. Every returned suite must complete before the head settles.
    """
    return [suite for suite in _object(body).get("check_suites", []) if isinstance(suite, dict)]


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
    pairs = sorted((str(suite.get("id")), str(suite.get("updated_at") or "")) for suite in suites)
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
            repo, head_sha = key
            response = None
            for attempt in range(1, MAX_ATTEMPTS + 1):
                await self.sleep(DEBOUNCE_SECONDS * attempt)
                try:
                    response = await poller.gated_read(
                        self.runner, repo, f"repos/{repo}/commits/{head_sha}/check-suites")
                except Exception:
                    log.exception("github-head-checks: aggregate read failed for %s@%s", *key)
                    return
                if response is not None and response.status == 200:
                    break
            else:
                log.info("github-head-checks: aggregate read dropped after retries for %s@%s", *key)
                return
            assert response is not None
            latest = _current_suites(response.body)
            if not latest or any(suite.get("status") != "completed" for suite in latest):
                return
            branch, pr_number = self._facts.get(key, (None, None))
            facts: dict[str, Any] = {"repo": repo, "head_sha": head_sha, "conclusion": _conclusion(latest)}
            if branch is not None:
                facts["head_branch"] = branch
            if pr_number is not None:
                facts["pr_number"] = pr_number
            external_id = f"{repo}#{pr_number}" if pr_number is not None else None
            result = await self.sink.admit_event(event_type="github.head.checks_completed", facts=facts,
                origin="connector", producer="github", event_id=_event_id(repo, head_sha, latest),
                external_id=external_id)
            if result.refusal is not None:
                log.info("github-head-checks: intake refused %s@%s code=%s", *key, result.refusal.code)
        finally:
            if self._tasks.get(key) is asyncio.current_task():
                self._tasks.pop(key, None)
                self._facts.pop(key, None)


_default: HeadCheckAggregator | None = None


def hook_disabled_reason() -> str | None:
    """Off by default; the poller's switches (config off, test mode) also hold it."""
    reason = poller.disabled_reason()
    if reason:
        return reason
    return None if os.environ.get(ENABLE_ENV) == "1" else "not_enabled"


def observe_admitted_check_suite(payload: dict[str, Any]) -> None:
    """Hook for admitted ``check_suite.completed`` deliveries."""
    global _default
    if hook_disabled_reason():
        return
    if _default is None:
        from reviewer.gh_api import run_gh
        _default = HeadCheckAggregator(run_gh, IntakeSink())
    _default.observe(payload)
