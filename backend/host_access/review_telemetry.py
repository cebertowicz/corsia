"""Usage and completion telemetry for a host-access review."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping
from uuid import uuid4

from loguru import logger

from connections.claude_unlisted import base_identity
from host_access.failure_reasons import ReviewTerminal, classify_failure
from infra.db_async import run_in_db
from infra.llm_usage import record_llm_usage
from infra.provider_usage import from_codex, unmeasured_ledger_row
from infra.product_events import emit_product_event
from llm.isolated_structured import IsolatedStructuredTelemetry


ReviewKind = Literal["application", "action"]


def usage_thread_id(
    application_payload: Mapping[str, Any],
    supplied: str | None,
) -> str | None:
    value = supplied if supplied is not None else application_payload.get(
        "root_thread_id"
    )
    normalized = str(value or "").strip()
    return normalized or None


_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "cache_creation_tokens",
                 "cache_read_tokens", "total_tokens", "cost_usd")


def _recorded_model(telemetry: IsolatedStructuredTelemetry) -> str:
    """Persist a concrete Claude Code identity whenever the reviewer reports one."""
    if telemetry.harness == "claude-code" and telemetry.reported_model:
        return f"claude-code/{base_identity(telemetry.reported_model)}"
    return telemetry.catalog_id


def _recorded_usage(telemetry: IsolatedStructuredTelemetry) -> dict[str, int | float | None]:
    """Normalize reviewer tokens into the disjoint ledger contract."""
    usage = {name: getattr(telemetry, name) for name in _USAGE_FIELDS}
    if telemetry.harness != "codex":
        return usage
    codex_usage = from_codex({
        "inputTokens": telemetry.prompt_tokens,
        "cachedInputTokens": telemetry.cache_read_tokens,
        "outputTokens": telemetry.completion_tokens,
    })
    return {
        **(codex_usage.ledger_row() if codex_usage is not None else unmeasured_ledger_row()),
        "cost_usd": telemetry.cost_usd,
    }


@dataclass(slots=True)
class ReviewTelemetry:
    review_kind: ReviewKind
    thread_id: str | None
    #: The requester's own model reviewed because the chosen reviewer could not run.
    reviewer_fallback: bool = False
    #: The account the review ran on; ``None`` for a reviewer with no account.
    account_id: str | None = None
    observed: IsolatedStructuredTelemetry | None = None
    unrecorded: list[IsolatedStructuredTelemetry] = field(default_factory=list)

    def record(self, telemetry: IsolatedStructuredTelemetry) -> None:
        self.observed = telemetry
        if telemetry.usage_available and self.thread_id is not None:
            self.unrecorded.append(telemetry)  # written by ``record_usage``, off the loop

    async def record_usage(self) -> None:
        while self.unrecorded:
            telemetry = self.unrecorded.pop(0)
            try:
                await run_in_db(
                    record_llm_usage, self.thread_id, _recorded_model(telemetry),
                    _recorded_usage(telemetry),
                    source=f"host_access_{self.review_kind}_review",
                    ttft_ms=None, harness=telemetry.harness,
                    vendor_account_id=self.account_id,
                    usage_id=uuid4().hex[:24],
                )
            except Exception as exc:
                logger.warning("host-access review usage telemetry failed for {}: {}",
                               telemetry.catalog_id, type(exc).__name__)

    async def emit_completion(self, terminal: ReviewTerminal) -> None:
        await self.record_usage()
        telemetry = self.observed
        if telemetry is None:
            return
        try:
            emit_product_event(
                "host_access_review",
                "completed",
                thread_id=self.thread_id,
                properties={
                    "catalog_id": telemetry.catalog_id,
                    "harness": telemetry.harness,
                    "transport_model": telemetry.transport_model,
                    "reported_model": telemetry.reported_model,
                    "review_kind": self.review_kind,
                    "usage_available": telemetry.usage_available,
                    "elapsed_ms": telemetry.elapsed_ms,
                    "cost_source": telemetry.cost_source or "unavailable",
                    "terminal": terminal,
                    "reviewer_fallback": self.reviewer_fallback,
                },
            )
        except Exception as exc:
            logger.warning(
                "host-access review completion telemetry failed for {}: {}",
                telemetry.catalog_id,
                type(exc).__name__,
            )


def failure_terminal(exc: BaseException) -> ReviewTerminal:
    # Telemetry keeps its original closed vocabulary: an unrecognised failure
    # is reported as transport loss here, while the durable holder-visible
    # reason withholds the cause instead of guessing.
    return classify_failure(exc) or "transport_failure"
