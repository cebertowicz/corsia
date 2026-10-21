"""Request and telemetry contracts for isolated structured inference."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Literal

from llm.resolution import ExecutionTarget


DEFAULT_ISOLATED_TIMEOUT_SECONDS = 300.0
StructuredDispatchObserver = Callable[[], Awaitable[None]]
StructuredRawResultObserver = Callable[
    [dict[str, Any], dict[str, Any] | None],
    Awaitable[None],
]


@dataclass(slots=True)
class IsolatedStructuredUsageCollector:
    """Checkpoint usage records captured outside the request payload."""

    usage_records: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class IsolatedStructuredTelemetry:
    """Bounded provider-neutral observations from one detached invocation."""

    catalog_id: str
    harness: str
    transport_model: str
    reported_model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cache_creation_tokens: int | None
    cache_read_tokens: int | None
    total_tokens: int | None
    cost_usd: float | None
    cost_source: Literal["provider_reported", "corsia_pricing"] | None
    elapsed_ms: int
    usage_available: bool
    #: The model the catalog target expects to run as, the ledger's fallback identity.
    expected_reported_model: str | None = None


TelemetrySink = Callable[[IsolatedStructuredTelemetry], object]


def normalize_subscription_telemetry(
    telemetry: IsolatedStructuredTelemetry,
) -> IsolatedStructuredTelemetry:
    """Remove non-charge dollars from subscription-harness telemetry."""
    from llm.pricing import is_subscription_usage

    if is_subscription_usage(telemetry.harness):
        return replace(telemetry, cost_usd=None, cost_source=None)
    return telemetry


@dataclass(slots=True)
class _TelemetryAccumulator:
    """Invocation-local observations; never retains provider payloads."""

    reported_model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    cost_source: Literal["provider_reported", "corsia_pricing"] | None = None

    def snapshot(
        self,
        target: ExecutionTarget,
        *,
        elapsed_ms: int,
    ) -> IsolatedStructuredTelemetry:
        token_usage = (
            self.prompt_tokens,
            self.completion_tokens,
            self.cache_creation_tokens,
            self.cache_read_tokens,
            self.total_tokens,
        )
        telemetry = IsolatedStructuredTelemetry(
            catalog_id=target.catalog_id,
            harness=target.harness,
            transport_model=target.transport_model,
            reported_model=self.reported_model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cache_creation_tokens=self.cache_creation_tokens,
            cache_read_tokens=self.cache_read_tokens,
            total_tokens=self.total_tokens,
            cost_usd=self.cost_usd,
            cost_source=self.cost_source,
            elapsed_ms=elapsed_ms,
            usage_available=any(value is not None for value in token_usage),
            expected_reported_model=target.expected_reported_model,
        )
        return normalize_subscription_telemetry(telemetry)


@dataclass(frozen=True, slots=True)
class IsolatedStructuredRequest:
    """Caller-owned policy and result contract for one detached invocation."""

    system_prompt: str
    user_prompt: str
    output_schema: dict[str, Any]
    max_output_tokens: int
    # The same source conversation in provider-native message form. Transports
    # that accept a message array send this; single-prompt harnesses send the
    # equivalent flattened ``user_prompt``. Empty for ordinary single-turn use.
    source_messages: tuple[dict[str, str], ...] = ()
    thread_id: str | None = None
    actor_thread_id: str | None = None
    vendor_account_id: str | None = None
    vendor_account_bound: bool = False
    vendor_account_receipt: object | None = None
    vendor_launch: object | None = None
    reasoning_effort: str | None = None
    timeout_seconds: float | None = DEFAULT_ISOLATED_TIMEOUT_SECONDS  # caller-owned if None
    allow_provider_retries: bool = False
    require_reported_model: bool = True
    require_raw_terminal: bool = True
    require_structured_output: bool = True
    # Whether a reply stopped by ``max_output_tokens`` still counts as a
    # result. Off by default: a reviewer verdict or a host-access ruling cut
    # off mid-sentence is not an answer. Callers whose ceiling exists only to
    # stop a runaway — checkpoints, whose paid call is a whole pass over the
    # thread's history — turn it on and salvage what they bought.
    accept_truncated_output: bool = False
    dispatch_observer: StructuredDispatchObserver | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    raw_result_observer: StructuredRawResultObserver | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    telemetry: IsolatedStructuredUsageCollector | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    _telemetry: _TelemetryAccumulator = field(
        default_factory=_TelemetryAccumulator,
        repr=False,
        compare=False,
    )


def prepare_isolated_structured_request(
    *,
    system_prompt: str,
    user_prompt: str,
    output_schema: dict[str, Any],
    max_output_tokens: int,
    reasoning_effort: str | None,
    timeout_seconds: float,
    thread_id: str | None,
    actor_thread_id: str | None,
    account_id: str | None,
    account_bound: bool,
) -> tuple[IsolatedStructuredRequest, _TelemetryAccumulator]:
    """Validate caller input and create one request with its telemetry owner."""
    from llm.isolated_admission import validate_request

    telemetry = _TelemetryAccumulator()
    request = IsolatedStructuredRequest(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        output_schema=validate_request(
            system_prompt,
            user_prompt,
            output_schema,
            max_output_tokens,
            timeout_seconds,
        ),
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
        timeout_seconds=float(timeout_seconds),
        thread_id=thread_id,
        actor_thread_id=actor_thread_id,
        vendor_account_id=account_id,
        vendor_account_bound=account_bound,
        _telemetry=telemetry,
    )
    return request, telemetry
