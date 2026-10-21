"""Value objects shared by every provider fork kind."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeAlias

from history.types import PortableHistoryProjection
from llm.resolution import ExecutionTarget

# (call key, model the vendor reported for the call or None, measured usage)
ForkUsageCallback: TypeAlias = Callable[[str, str | None, dict[str, Any]], Awaitable[None]]


class ForkNotAdmitted(RuntimeError):
    """The caller could not admit one provider invocation right now."""


@dataclass(frozen=True, slots=True)
class ColdForkSource:
    """Frozen history and mandatory continuity, before the writer's one fit."""

    projection: PortableHistoryProjection
    protected_blocks: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class ProviderForkRequest:
    """Frozen input for one provider fork."""

    fork_id: str
    thread_id: str
    target: ExecutionTarget
    instruction: str
    reasoning_effort: str | None
    working_dir: str
    cold_source: ColdForkSource | None = None
    binary: str | None = None
    warm_source: object | None = None
    mcp_config: str | None = None
    max_output_tokens: int = 16_384
    tool_registry: object | None = None
    dynamic_tool_specs: tuple[dict[str, Any], ...] = ()
    developer_instructions: str | None = None
    on_usage: ForkUsageCallback | None = None
    vendor_account_id: str | None = None
    vendor_account_bound: bool = False
    vendor_account_receipt: object | None = None
    vendor_launch: object | None = None
    same_model_cold: Callable[[], object] | None = None
    instruction_without_review: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderForkResult:
    """Exact model body and provider usage captured from one invocation."""

    raw_body: str
    usage: dict[str, Any] | None
    diagnostic: str | None = None


class ProviderForkRejected(RuntimeError):
    """The provider invocation ended without producing fork content."""


class WarmForkUnusable(ProviderForkRejected):
    """The warm fork cannot hold its own input before generation."""

    def __init__(self, *args: object, reason: str = "warm_fork_over_window") -> None:
        super().__init__(*args)
        self.reason = reason
