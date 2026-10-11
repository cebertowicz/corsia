"""Bounded serial recording of versioned owner-correction facts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Any, Awaitable, Callable

from loguru import logger

from core.corrections_recording import (
    CORRECTION_EVENT,
    FACT_KEYS,
    LABEL_REFUSED,
    REFUSAL_INPUT_TOO_LONG,
    REFUSAL_INVALID_OUTPUT,
    REFUSAL_OUTPUT_TRUNCATED,
    SYSTEM_PROMPT,
    CheckerVersion,
    ClassificationOutputError,
    CorrectionCandidate,
    checker_version,
    classifier_prompt,
    dedupe_key,
    fact_attributes,
    refusal_classification,
    subject_key,
    validate_classification,
)
from infra.db_async import run_in_db
from jobs.corrections_scanning import (
    PendingCandidateKey,
    hydrate_candidates,
    pending_candidate_keys,
)
from llm.isolated_contract import IsolatedStructuredTelemetry
from llm.isolated_structured import (
    IsolatedStructuredContextOverflowError,
    IsolatedStructuredIdentityError,
    IsolatedStructuredOutputTruncatedError,
    IsolatedStructuredProviderError,
    IsolatedStructuredRateLimitError,
    IsolatedStructuredTimeoutError,
    IsolatedStructuredUnavailableError,
    invoke_isolated_structured,
)

DEFAULT_BATCH_SIZE = 20
# Four hours bounds a deliberate drain while the scheduler continues to service
# maintenance triggers; at the 60-second call timeout it permits at least 240 calls.
RUN_TIME_BUDGET_SECONDS = 4 * 60 * 60
# A provider error or timeout may belong to one message (a rejected request) or
# to the provider (an outage or hang); the exception type cannot tell which.
# Consecutive failed calls across different messages can: three in a row stop
# the run, so one poison message never blocks the backlog and an outage costs
# at most three calls (three minutes at the call timeout).
PROVIDER_FAILURE_STREAK = 3
# Fortissimo reasons by default (as in the gated run), so the cap must leave room
# for thinking plus the verdict; the 60s call timeout bounds it (~80 tok/s).
CLASSIFIER_MAX_OUTPUT_TOKENS = 4096
MANUAL_REQUEST_SETTING = "corrections_recording_manual_request"
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": ["correction", "not_correction"]},
        "reason": {"type": "string"},
    },
    "required": ["label", "reason"],
    "additionalProperties": False,
}
Invoke = Callable[..., Awaitable[dict[str, str]]]


@dataclass(slots=True)
class _UsageTelemetry:
    """Record one detached checker call with the normal per-thread usage owner."""

    thread_id: str

    def record(self, telemetry: IsolatedStructuredTelemetry) -> None:
        if not telemetry.usage_available:
            return
        try:
            from infra.llm_usage import record_llm_usage

            record_llm_usage(
                self.thread_id,
                telemetry.catalog_id,
                {
                    "prompt_tokens": telemetry.prompt_tokens,
                    "completion_tokens": telemetry.completion_tokens,
                    "cache_creation_tokens": telemetry.cache_creation_tokens,
                    "cache_read_tokens": telemetry.cache_read_tokens,
                    "total_tokens": telemetry.total_tokens,
                    "cost_usd": telemetry.cost_usd,
                },
                source="corrections_recording",
                ttft_ms=None,
            )
        except Exception:
            logger.exception("corrections recording usage telemetry failed")


def _resolve_target(model_id: str):
    from core.checkpoint_render import checkpoint_binary
    from infra.database import get_db
    from llm.resolution import resolve_execution_target

    db = get_db()
    try:
        target = resolve_execution_target(model_id, db=db)
        return target, checkpoint_binary(target, db)
    finally:
        db.close()


def _owner_user_ids() -> list[str]:
    """Return each durable account owner whose messages can be scoped independently."""
    from infra.database import get_db

    db = get_db()
    try:
        return [
            str(row[0])
            for row in db.execute(
                "SELECT DISTINCT user_id FROM threads "
                "WHERE user_id IS NOT NULL AND trim(user_id)<>'' ORDER BY user_id"
            ).fetchall()
        ]
    finally:
        db.close()


def request_manual_corrections_recording() -> None:
    """Durably ask the existing nightly loop to drain for up to four hours."""
    from infra.settings import set_setting

    set_setting(MANUAL_REQUEST_SETTING, {"pending": True})


def claim_manual_corrections_recording() -> bool:
    """Atomically consume a manual request so concurrent loop ticks cannot duplicate it."""
    from infra.database import write_transaction
    from infra.settings import get_setting, set_setting

    with write_transaction(None, scope="claim_manual_corrections_recording") as db:
        request = get_setting(MANUAL_REQUEST_SETTING, db=db) or {}
        if request.get("pending") is not True:
            return False
        set_setting(MANUAL_REQUEST_SETTING, {"pending": False}, db=db)
        return True


def _write_status(status: str, reason: str | None = None, **counts: int) -> None:
    from infra.settings import set_setting
    from infra.time import utc_now_iso

    value: dict[str, str | int | None] = {"status": status, "reason": reason, "at": utc_now_iso()}
    value.update(counts)
    set_setting("corrections_recording_status", value)


def _refusal_code(exc: Exception) -> str:
    if isinstance(exc, IsolatedStructuredOutputTruncatedError):
        return REFUSAL_OUTPUT_TRUNCATED
    if isinstance(exc, IsolatedStructuredContextOverflowError):
        return REFUSAL_INPUT_TOO_LONG
    return REFUSAL_INVALID_OUTPUT


def _record_failure() -> None:
    from infra.database import write_transaction
    from infra.outcome_events import count_outcome_event

    with write_transaction(None, scope="corrections_recording_failure") as db:
        count_outcome_event(db, "mapping_failed", CORRECTION_EVENT)


def _record_fact(
    candidate: CorrectionCandidate,
    classification: dict[str, str],
    version: CheckerVersion,
) -> bool:
    from infra.database import write_transaction
    from infra.outcome_events import record_outcome_event

    attributes = fact_attributes(candidate, classification, version)
    if set(attributes) != set(FACT_KEYS):
        raise RuntimeError("correction fact attributes are incomplete")
    with write_transaction(None, scope="record_correction_fact") as db:
        result = record_outcome_event(
            db,
            dedupe_key=dedupe_key(candidate.message_id, version),
            event_name=CORRECTION_EVENT,
            source="corsia",
            provenance="agent_recorded",
            ts=candidate.created_at,
            subject_key=subject_key(candidate.message_id),
            project_id=candidate.project_id,
            agent_thread_id=candidate.thread_id,
            attributes=attributes,
        )
        if result.dropped_keys:
            raise RuntimeError("correction fact attributes were dropped")
        return result.inserted


async def _classify_and_record(
    candidate: CorrectionCandidate, target: Any, binary: str | None,
    version: CheckerVersion, invoke: Invoke,
) -> str:
    """Classify one candidate and write its fact.

    Returns "recorded", "refused", "failed" (our write failed),
    "call_failed" (the checker call failed; the message stays pending),
    "provider_unavailable", or "duplicate" when the fact already existed and
    nothing was inserted.
    """
    try:
        classification = await invoke(
            target,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=classifier_prompt(candidate.preceding_agent, candidate.content),
            output_schema=OUTPUT_SCHEMA,
            validator=validate_classification,
            max_output_tokens=CLASSIFIER_MAX_OUTPUT_TOKENS,
            timeout_seconds=60.0,
            binary=binary,
            telemetry_sink=_UsageTelemetry(candidate.thread_id).record,
            thread_id=candidate.thread_id,
        )
    except (
        IsolatedStructuredOutputTruncatedError,
        IsolatedStructuredContextOverflowError,
        ClassificationOutputError,
    ) as exc:
        # Only the checker's own output (or an item it can never fit) is a
        # refusal: this version will not re-ask it.
        classification = refusal_classification(_refusal_code(exc))
        logger.warning(
            "corrections recording refused {}: {}", candidate.message_id, classification["reason"],
        )
    except (
        IsolatedStructuredRateLimitError,
        IsolatedStructuredUnavailableError,
        IsolatedStructuredIdentityError,
    ) as exc:
        # Only these say the provider itself cannot serve whatever is asked:
        # leave this message pending and stop the run.
        logger.warning("corrections recording provider unavailable {}: {}", candidate.message_id, type(exc).__name__)
        return "provider_unavailable"
    except (IsolatedStructuredProviderError, IsolatedStructuredTimeoutError) as exc:
        # Ambiguous between this message and the provider: the run's failure
        # streak decides (PROVIDER_FAILURE_STREAK).
        logger.warning("corrections recording call failed {}: {}", candidate.message_id, exc)
        await run_in_db(_record_failure)
        return "call_failed"
    except Exception:
        logger.exception("corrections recording failed {}", candidate.message_id)
        await run_in_db(_record_failure)
        return "call_failed"
    try:
        inserted = await run_in_db(_record_fact, candidate, classification, version)
    except Exception:
        # A failed write is ours, not the checker's: never a refusal fact.
        logger.exception("corrections recording write failed {}", candidate.message_id)
        await run_in_db(_record_failure)
        return "failed"
    if not inserted:
        return "duplicate"
    return "refused" if classification["label"] == LABEL_REFUSED else "recorded"


@dataclass(slots=True)
class _PendingBatch:
    """One selected page and its hydrated owner-authored candidates."""

    selected_keys: list[PendingCandidateKey]
    candidates: list[CorrectionCandidate]


async def _next_pending_batch(
    version: CheckerVersion, batch_size: int, before: tuple[str, str] | None,
) -> _PendingBatch:
    """Globally select keys before reading the 20 classifier inputs."""
    keys: list[PendingCandidateKey] = []
    for owner_user_id in await run_in_db(_owner_user_ids):
        keys.extend(
            await run_in_db(pending_candidate_keys, version, batch_size, owner_user_id, before)
        )
    selected = sorted(
        keys, key=lambda key: (key.created_at, key.message_id), reverse=True,
    )[:batch_size]
    return _PendingBatch(selected, await run_in_db(hydrate_candidates, selected))


async def _finish_run(
    counts: dict[str, int], stop_reason: str, *, status: str | None = None,
) -> dict[str, int | str | None]:
    recorded, refused, failed = counts["recorded"], counts["refused"], counts["failed"]
    status = status or ("ok" if not failed else "partial")
    reason = None if status == "ok" else "One or more classifications failed and will retry"
    if status == "cancelled":
        reason = "Run stopped by shutdown; pending candidates will retry"
    elif status == "provider_unavailable":
        reason = "Provider unavailable; pending candidates will retry"
    result: dict[str, int | str | None] = {
        "status": status, "processed": sum(counts.values()), "recorded": recorded,
        "refused": refused, "failed": failed,
    }
    await run_in_db(
        _write_status, status, reason, stop_reason=stop_reason,
        processed=result["processed"], recorded=recorded, refused=refused, failed=failed,
    )
    return result


async def run_corrections_recording(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    invoke: Invoke = invoke_isolated_structured,
    time_budget_seconds: float = RUN_TIME_BUDGET_SECONDS,
    clock: Callable[[], float] = monotonic,
) -> dict[str, int | str | None]:
    """Drain newest pending batches serially until empty or the run budget expires."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if time_budget_seconds <= 0:
        raise ValueError("time_budget_seconds must be positive")
    from infra.settings import get_corrections_checker_model
    from llm.resolution import ModelUnavailableError

    model_id = await run_in_db(get_corrections_checker_model)
    if model_id is None:
        await run_in_db(_write_status, "unconfigured", "No checker model configured")
        return {"status": "unconfigured", "processed": 0, "recorded": 0, "refused": 0, "failed": 0}
    from llm.endpoint_policy import endpoint_agent_use
    if await run_in_db(endpoint_agent_use, model_id) == "interactive_only":
        reason = "Configured checker model is restricted to interactive use"
        logger.warning(reason)
        await run_in_db(_write_status, "unavailable", reason)
        return {"status": "unavailable", "processed": 0, "recorded": 0, "refused": 0, "failed": 0}
    try:
        target, binary = await run_in_db(_resolve_target, model_id)
    except (ModelUnavailableError, ValueError) as exc:
        await run_in_db(_write_status, "unavailable", str(exc)[:500])
        return {"status": "unavailable", "processed": 0, "recorded": 0, "refused": 0, "failed": 0}
    if not target.available:
        reason = target.availability_reason or "Configured checker model is unavailable"
        await run_in_db(_write_status, "unavailable", reason)
        return {"status": "unavailable", "processed": 0, "recorded": 0, "refused": 0, "failed": 0}

    counts = {"recorded": 0, "refused": 0, "failed": 0, "duplicate": 0}
    before: tuple[str, str] | None = None
    version, started_at = checker_version(model_id), clock()
    failure_streak = 0
    try:
        while clock() - started_at < time_budget_seconds:
            batch = await _next_pending_batch(version, batch_size, before)
            if not batch.selected_keys:
                return await _finish_run(counts, "empty")
            before = batch.selected_keys[-1].created_at, batch.selected_keys[-1].message_id
            for candidate in batch.candidates:
                if clock() - started_at >= time_budget_seconds:
                    return await _finish_run(counts, "budget")
                outcome = await _classify_and_record(candidate, target, binary, version, invoke)
                if outcome == "provider_unavailable":
                    return await _finish_run(
                        counts, "provider_unavailable", status="provider_unavailable",
                    )
                if outcome != "call_failed":
                    failure_streak = 0
                    counts[outcome] += 1
                    continue
                counts["failed"] += 1
                failure_streak += 1
                if failure_streak >= PROVIDER_FAILURE_STREAK:
                    return await _finish_run(
                        counts, "provider_unavailable", status="provider_unavailable",
                    )
    except asyncio.CancelledError:
        await _finish_run(counts, "shutdown", status="cancelled")
        raise
    return await _finish_run(counts, "budget")
