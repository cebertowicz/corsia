"""Ordered off-loop preparation for the owning-thread router."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from events.connectors.github_rules import RuleScope
    from events.router import RouteResult


def _chip_line(payload: dict[str, Any], external_id: str | None, summary: str) -> str:
    """A readable `CI <conclusion> on <target>` line for check suites; otherwise the summary."""
    suite = payload.get("check_suite") or {}
    if suite and not (payload.get("comment") or payload.get("pull_request")):
        target = external_id or (payload.get("repository") or {}).get("full_name", "")
        return f"CI {suite.get('conclusion') or 'completed'} on {target}"
    return summary


def summary_facts(payload: dict[str, Any], facts: dict[str, Any] | None) -> dict[str, Any] | None:
    """Reuse normalized facts or classify authored text for the default wake."""
    from loguru import logger

    comment = payload.get("comment") or {}
    pr = payload.get("pull_request") or {}
    issue = payload.get("issue") or {}
    has_text = bool(comment.get("body") or pr.get("title") or issue.get("title"))
    if facts is None and has_text:
        from events.connectors.github_content_facts import content_facts

        try:
            facts = content_facts(payload)
        except Exception as exc:
            logger.warning("event-routing: author origin unavailable: {}", type(exc).__name__)
    if has_text and facts and not facts.get("author"):
        logger.warning("event-routing: author origin unavailable: missing author")
    return facts


@dataclass
class PreparedRoute:
    """Committed preparation effects recovered even after cancellation or error."""

    result: RouteResult | None = None
    thread_id: str | None = None
    identity: Any = None
    summary: str | None = None
    details: dict[str, Any] | None = None
    chip: str | None = None
    notice: str | None = None
    reap_payload: dict[str, Any] | None = None
    rule_plan: Any = None  # agent-written rules riding the routed delivery


@dataclass
class _Preparation:
    """Atomically distinguish an unstarted DB job from a started preparation."""

    prepared: PreparedRoute = field(default_factory=PreparedRoute)
    admission: threading.Lock = field(default_factory=threading.Lock)
    started: bool = False
    cancelled: bool = False

    def begin(self) -> bool:
        with self.admission:
            if self.cancelled:
                return False
            self.started = True
            return True

    def cancel_unstarted(self) -> None:
        with self.admission:
            if not self.started:
                self.cancelled = True


def _preparation_gate() -> asyncio.Lock:
    """The running loop owns its gate; no external registry retains closed loops."""
    loop = asyncio.get_running_loop()
    gate = getattr(loop, "_corsia_route_preparation_gate", None)
    if gate is None:
        gate = asyncio.Lock()
        setattr(loop, "_corsia_route_preparation_gate", gate)
    return gate


def _prepare_route(
    unit: _Preparation,
    internal_event: str,
    payload: dict[str, Any],
    identity: Any,
    selected_pr: dict[str, Any] | None,
    scope: RuleScope | None = None,
    delivery_id: str | None = None,
) -> None:
    """Perform each canonical write in the original noninterleaved stage order."""
    from events import router
    from events.connectors.github_rules import apply_routed_rules
    from events.identity import PrResolutionError
    from events.routing_inputs import structured_tag

    if not unit.begin():
        return
    prepared = unit.prepared
    if isinstance(identity, PrResolutionError):
        prepared.result = router._dead_letter_result(
            internal_event, payload, "check_suite_pr_resolution_failed", None,
        )
        return
    if identity is not None and router._record_source_facts_db(internal_event, identity, payload):
        prepared.reap_payload = payload
    suppressed = router._self_authored_result(internal_event, payload)
    if suppressed is not None:
        prepared.result = suppressed
        return
    external_id = identity.external_id if identity else None
    tag_provenance = structured_tag(payload)
    selected, routing_state = router._select_route(
        internal_event, payload, identity, external_id, tag_provenance, selected_pr,
    )
    if isinstance(selected, router.RouteResult):
        prepared.result = selected
        # Routing chose no thread; rules may still add recipients, never change this result.
        apply_routed_rules(prepared, internal_event, payload, selected_pr, routing_state,
                           scope=scope)
        return
    thread_id, wake_reason = selected
    self_caused_event = False
    if delivery_id and internal_event in ("webhook.github.pr_comment", "webhook.github.issue_comment"):
        from events import self_caused
        from infra.database import get_db

        db = get_db()
        try:
            row = db.execute("SELECT object_key FROM webhook_deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        finally:
            db.close()
        event_key = row[0] if row else None
        self_caused_event = bool(event_key and self_caused.is_self_caused("github", event_key, thread_id))
    facts = scope.envelope.facts if scope and scope.envelope else None
    summary, details = router._summarize(internal_event, payload, external_id, facts)
    if tag_provenance:
        details["tag_provenance"] = tag_provenance
    chip = router._chip_line(payload, external_id, summary)
    router._record_passive_change(
        routing_state=routing_state, wake_thread_id=thread_id,
        internal_event=internal_event, external_id=external_id,
        summary=summary, chip=chip, wake_reason=wake_reason,
        tag_provenance=tag_provenance,
    )
    prepared.thread_id, prepared.identity = thread_id, identity
    prepared.summary, prepared.details, prepared.chip = summary, details, chip
    prepared.notice = router.github_pr_notice.notice_for_routed_event(thread_id, identity)
    # Routing has decided; agent-written rules add recipients or drop or annotate this one.
    apply_routed_rules(prepared, internal_event, payload, selected_pr, routing_state, wake_reason,
                       scope)
    if self_caused_event and prepared.result is None:
        from events.connectors.github_rules import _drop

        _drop(prepared, internal_event, "self_caused")


async def release_notice_off_loop(thread_id: str, identity: Any) -> None:
    """Join canonical notice release before propagating repeated cancellation."""
    from events.connectors import github_pr_notice
    from infra.db_async import run_in_db
    from subagents.runtime_guard import join_task_through_cancellation

    release = asyncio.create_task(run_in_db(github_pr_notice.release_notice, thread_id, identity))
    cancellation = await join_task_through_cancellation(release)
    release.result()
    if cancellation is not None:
        raise cancellation


async def settle_routed_delivery(prepared: PreparedRoute, delivered: bool) -> None:
    """After the routed delivery: release an undelivered notice and record the rules that rode it."""
    from events.connectors.github_rules import record_routed_rides
    from infra.db_async import run_in_db

    if not delivered and prepared.notice and prepared.thread_id is not None:
        await release_notice_off_loop(prepared.thread_id, prepared.identity)
    if prepared.rule_plan is not None:
        await run_in_db(record_routed_rides, prepared, delivered)


async def prepare_route_ordered(
    internal_event: str,
    payload: dict[str, Any],
    identity: Any,
    selected_pr: dict[str, Any] | None,
    *, delivery_id: str | None = None,
) -> PreparedRoute:
    """Join the exact DB unit and recover committed effects before leaving its gate."""
    from events import router
    from events.connectors.github_rules import rule_scope
    from infra.db_async import run_in_db
    from subagents.runtime_guard import join_task_through_cancellation

    # Like the check suite's pull request, read before the gate: the projects
    # tracking the repository run git, which must never hold every webhook's turn.
    scope = await asyncio.to_thread(rule_scope, internal_event, payload, selected_pr)
    async with _preparation_gate():
        unit = _Preparation()
        preparation = asyncio.create_task(run_in_db(
            _prepare_route, unit, internal_event, payload, identity, selected_pr, scope, delivery_id,
        ))
        cancellation = None
        try:
            await asyncio.shield(preparation)
        except asyncio.CancelledError as error:
            cancellation = error
            unit.cancel_unstarted()
            await join_task_through_cancellation(preparation)
        finally:
            # Source facts may have committed even when later preparation fails.
            # Scheduling is loop-owned and occurs once before delivery or cancellation.
            if unit.prepared.reap_payload is not None:
                router.schedule_merged_pr_reap(unit.prepared.reap_payload)
        if cancellation is not None:
            # Consume the result/error only after the exact worker has finished.
            try:
                preparation.result()
            except BaseException as error:
                from loguru import logger

                logger.error("Route preparation failed during caller cancellation: {}", type(error).__name__)
            if unit.prepared.notice and unit.prepared.thread_id is not None:
                await release_notice_off_loop(unit.prepared.thread_id, unit.prepared.identity)
            raise cancellation
        preparation.result()
        return unit.prepared
