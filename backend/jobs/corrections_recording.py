"""Bounded serial recording of versioned owner-correction facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from loguru import logger

from core.measurement_scope import scope_cte
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
    is_owner_authored_message,
    refusal_classification,
    subject_key,
    validate_classification,
)
from infra.db_async import run_in_db
from llm.isolated_contract import IsolatedStructuredTelemetry
from llm.isolated_structured import (
    IsolatedStructuredContextOverflowError,
    IsolatedStructuredError,
    IsolatedStructuredOutputTruncatedError,
    invoke_isolated_structured,
)

DEFAULT_BATCH_SIZE = 20
# Fortissimo reasons by default (as in the gated run), so the cap must leave room
# for thinking plus the verdict; the 60s call timeout bounds it (~80 tok/s).
CLASSIFIER_MAX_OUTPUT_TOKENS = 4096
MAX_PENDING_SCAN_MULTIPLIER = 8
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


def _pending_candidates(
    version: CheckerVersion, batch_size: int, owner_user_id: str,
) -> list[CorrectionCandidate]:
    """Read one owner's oldest pending rows through Measurement's canonical closure."""
    from infra.database import get_db

    db = get_db()
    try:
        rows = db.execute(
            scope_cte() + """
            SELECT m.rowid,m.id,m.thread_id,t.project_id,m.role,m.content,m.metadata,m.created_at
            FROM messages m
            JOIN scope_threads scoped ON scoped.id=m.thread_id
            JOIN threads t ON t.id=m.thread_id
            WHERE NOT EXISTS (
                SELECT 1 FROM outcome_events e
                WHERE e.event_name=:correction_event
                  AND e.subject_key=:message_prefix || m.id AND e.retracted_at IS NULL
                  AND json_extract(e.attributes,'$.checker_model_id')=:model_id
                  AND json_extract(e.attributes,'$.rubric_sha256')=:rubric_sha256
                  AND json_extract(e.attributes,'$.prompt_sha256')=:prompt_sha256
              )
            ORDER BY m.created_at ASC,m.id ASC LIMIT :scan_limit
            """,
            {
                "user_id": owner_user_id,
                "scope_project": "all",
                "scope_agent": None,
                "correction_event": CORRECTION_EVENT,
                "message_prefix": "message:",
                "model_id": version.model_id,
                "rubric_sha256": version.rubric_sha256,
                "prompt_sha256": version.prompt_sha256,
                "scan_limit": batch_size * MAX_PENDING_SCAN_MULTIPLIER,
            },
        ).fetchall()
        candidates: list[CorrectionCandidate] = []
        for row in rows:
            if not is_owner_authored_message(
                role=row[4], content=row[5], metadata=row[6],
            ):
                continue
            preceding = db.execute(
                """SELECT content FROM messages
                   WHERE thread_id=? AND role='assistant'
                     AND (created_at < ? OR (created_at=? AND rowid < ?))
                   ORDER BY created_at DESC,rowid DESC LIMIT 1""",
                (row[2], row[7], row[7], row[0]),
            ).fetchone()
            candidates.append(CorrectionCandidate(
                message_id=row[1], thread_id=row[2], project_id=row[3], content=row[5],
                created_at=row[7], preceding_agent=preceding[0] if preceding else "",
            ))
            if len(candidates) == batch_size:
                break
        return candidates
    finally:
        db.close()


def request_manual_corrections_recording() -> None:
    """Durably ask the existing nightly loop to run one bounded batch."""
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

    Returns "recorded", "refused", "failed", or "duplicate" when the fact
    already existed and nothing was inserted.
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
    except Exception as exc:
        # Rate limit, provider, timeout, unavailable or anything unexpected:
        # no fact, so the next run retries this message.
        if isinstance(exc, IsolatedStructuredError):
            logger.warning("corrections recording skipped {}: {}", candidate.message_id, type(exc).__name__)
        else:
            logger.exception("corrections recording failed {}", candidate.message_id)
        await run_in_db(_record_failure)
        return "failed"
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


async def run_corrections_recording(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    invoke: Invoke = invoke_isolated_structured,
) -> dict[str, int | str | None]:
    """Classify one bounded, serial batch; malformed output writes no fact.

    A missing or unavailable configured model records its reason and leaves every
    candidate eligible for the next nightly or manual run.  Every provider call
    is awaited before the next candidate begins.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    from infra.settings import get_corrections_checker_model
    from llm.resolution import ModelUnavailableError

    model_id = await run_in_db(get_corrections_checker_model)
    if model_id is None:
        await run_in_db(_write_status, "unconfigured", "No checker model configured")
        return {"status": "unconfigured", "processed": 0, "recorded": 0, "refused": 0, "failed": 0}
    try:
        target, binary = await run_in_db(_resolve_target, model_id)
    except (ModelUnavailableError, ValueError) as exc:
        await run_in_db(_write_status, "unavailable", str(exc)[:500])
        return {"status": "unavailable", "processed": 0, "recorded": 0, "refused": 0, "failed": 0}
    if not target.available:
        reason = target.availability_reason or "Configured checker model is unavailable"
        await run_in_db(_write_status, "unavailable", reason)
        return {"status": "unavailable", "processed": 0, "recorded": 0, "refused": 0, "failed": 0}

    version = checker_version(model_id)
    candidates: list[CorrectionCandidate] = []
    for owner_user_id in await run_in_db(_owner_user_ids):
        candidates.extend(
            await run_in_db(_pending_candidates, version, batch_size, owner_user_id)
        )
    candidates = sorted(
        candidates, key=lambda candidate: (candidate.created_at, candidate.message_id)
    )[:batch_size]
    counts = {"recorded": 0, "refused": 0, "failed": 0, "duplicate": 0}
    for candidate in candidates:
        outcome = await _classify_and_record(candidate, target, binary, version, invoke)
        counts[outcome] += 1
    recorded, refused, failed = counts["recorded"], counts["refused"], counts["failed"]
    status = "ok" if not failed else "partial"
    await run_in_db(
        _write_status,
        status,
        None if not failed else "One or more classifications failed and will retry",
        processed=len(candidates),
        recorded=recorded,
        refused=refused,
        failed=failed,
    )
    return {
        "status": status, "processed": len(candidates), "recorded": recorded,
        "refused": refused, "failed": failed,
    }
