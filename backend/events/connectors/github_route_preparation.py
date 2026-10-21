"""Ordered off-loop preparation for the owning-thread router."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from events.connectors.github import event_chip, summarize_event

if TYPE_CHECKING:
    from events.router import RouteResult
    from events.envelope import EventEnvelope
    from events.rules import Evaluation


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
    envelope: EventEnvelope | None = None
    evaluation: Evaluation | None = None


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
    check_suite_base_ref: str | None,
    check_suite_pr_selected: bool,
) -> None:
    """Perform each canonical write in the original noninterleaved stage order."""
    from events import router
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

    external_id = identity.external_id if identity else None
    tag_provenance = structured_tag(payload)
    selected = router._select_route(
        internal_event, payload, identity, external_id, tag_provenance,
        check_suite_base_ref, check_suite_pr_selected,
    )
    if isinstance(selected, router.RouteResult):
        prepared.result = selected
        return
    thread_id, wake_reason, routing_state = selected
    summary, details = summarize_event(internal_event, payload, external_id)
    if tag_provenance:
        details["tag_provenance"] = tag_provenance
    chip = event_chip(payload, external_id, summary)
    router._record_passive_change(
        routing_state=routing_state, wake_thread_id=thread_id,
        internal_event=internal_event, external_id=external_id,
        summary=summary, chip=chip, wake_reason=wake_reason,
        tag_provenance=tag_provenance,
    )
    prepared.envelope = routing_state.get('_envelope')
    prepared.evaluation = routing_state.get('_evaluation')
    prepared.thread_id, prepared.identity = thread_id, identity
    prepared.summary, prepared.details, prepared.chip = summary, details, chip
    prepared.notice = router.github_pr_notice.notice_for_routed_event(thread_id, identity)


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


async def prepare_route_ordered(
    internal_event: str,
    payload: dict[str, Any],
    identity: Any,
    check_suite_base_ref: str | None,
    check_suite_pr_selected: bool,
) -> PreparedRoute:
    """Join the exact DB unit and recover committed effects before leaving its gate."""
    from events import router
    from infra.db_async import run_in_db
    from subagents.runtime_guard import join_task_through_cancellation

    async with _preparation_gate():
        unit = _Preparation()
        preparation = asyncio.create_task(run_in_db(
            _prepare_route, unit, internal_event, payload, identity,
            check_suite_base_ref, check_suite_pr_selected,
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
