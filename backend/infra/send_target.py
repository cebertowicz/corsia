"""Target resolution for thread sends."""

from __future__ import annotations

from typing import Literal

from loguru import logger

from infra import db_async
from infra.send_types import DeliveryFailureSide
from llm import resolution as model_resolution
from llm.resolution import ExecutionTarget, ModelUnavailableError


SELECTED_MODEL_UNAVAILABLE = "selected_model_unavailable"
CLAUDE_CODE_PROVIDER_DISABLED = "claude_code_provider_disabled"
CODEX_PROVIDER_DISABLED = "codex_provider_disabled"
HARNESS_PROVIDER_DISABLED = {CLAUDE_CODE_PROVIDER_DISABLED, CODEX_PROVIDER_DISABLED}
# The same FIFO-head hold, for a thread that has no model yet. Only the user
# choosing one lifts it (``thread.model_row.select_first_model``), so no catalog wake looks for it.
NO_MODEL_SELECTED = "no_model_selected"


class TargetUnavailable(RuntimeError):
    """The selected model or harness cannot take work right now.

    This is a thread-level condition, never a verdict on one message. The side
    distinguishes a provider outage that can clear from catalog/configuration
    truth we failed to write, which no retry ladder can repair.
    """

    def __init__(
        self, detail: str, *, side: DeliveryFailureSide | str | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.side = DeliveryFailureSide.declared(side)

class TargetInvalid(RuntimeError):
    """The selected target is malformed and can never be delivered to.

    Ambiguous transport identity, unsupported harness, invalid catalog metadata,
    or an empty selector is configuration, not an outage. Queueing it would
    accept a message that can never be delivered: a wedge labeled queued.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


async def resolve_target(model: str) -> ExecutionTarget:
    """Resolve catalog truth, distinguishing our defect from bad configuration.

    ``ModelUnavailableError`` here means no model is configured or the selected
    identifier is absent from our catalog. Neither is a provider refusal; they
    are rows we should have written, so the failure is OURS and no ladder fixes
    it. Admission classifies a resolved target as runnable, model-recoverable,
    or visibly failed below.

    ``ValueError`` means the catalog row cannot describe a deliverable target.
    That never clears by retrying and must not become queued work.
    """
    try:
        return await db_async.run_in_db(
            lambda: model_resolution.resolve_execution_target(model)
        )
    except ModelUnavailableError as exc:
        raise TargetUnavailable(str(exc), side=DeliveryFailureSide.OURS) from exc
    except ValueError as exc:
        raise TargetInvalid(str(exc)) from exc


TargetAdmission = Literal["run", "wait", "fail"]


def classify_target_admission(target: ExecutionTarget) -> TargetAdmission:
    """Classify a target at the one admission seam.

    Catalog/model unavailability can clear through ``notify_models_changed`` and
    waits at the FIFO head. Harness connection state has no live catalog wake,
    so it fails visibly rather than promising recovery that cannot occur.
    """
    if target.available:
        return "run"
    if target.availability_reason == "connection_unavailable":
        return "fail"
    return "wait"


def model_admission(model: str, *, db=None) -> TargetAdmission:
    """Classify the current catalog target from a claim transaction.

    Permanent resolution errors release an existing hold for the normal driver
    to fail visibly; only the classifier's ``wait`` outcome preserves it.
    """
    try:
        return classify_target_admission(
            model_resolution.resolve_execution_target(model, db=db)
        )
    except (ModelUnavailableError, ValueError):
        return "fail"


def lifted_hold_ids(rows, model: str, *, db) -> frozenset[str]:
    """Ids among ``rows`` that the idle claim should release and take.

    Hold and release use ``classify_target_admission`` through the same model
    resolver, so only an explicitly recoverable model state remains held.
    """
    held = frozenset(
        str(row["id"]) for row in rows
        if row["write_outcome"] == "proved_no_write"
        and row["delivery_error"] in ({SELECTED_MODEL_UNAVAILABLE} | HARNESS_PROVIDER_DISABLED)
    )
    from connections.account_holds import ACCOUNT_HOLD_REASONS

    # Idle callers have already admitted the current durable account choice.
    account_held = frozenset(str(row['id']) for row in rows
                            if row['write_outcome'] == 'proved_no_write'
                            and row['delivery_error'] in ACCOUNT_HOLD_REASONS)
    return account_held | (held if held and model_admission(model, db=db) != 'wait' else frozenset())


def boundary_account_lifted_ids(db, thread_id: str, turn, rows) -> frozenset[str]:
    """A boundary never admits current Y into frozen X or lifts model holds."""
    from connections.account_holds import ACCOUNT_HOLD_REASONS
    from connections.account_policy import AccountAdmissionError, chosen_account_id, require_claim
    from infra.vendor_turn_binding import model_vendor

    held = frozenset(str(row['id']) for row in rows
                     if row['write_outcome'] == 'proved_no_write'
                     and row['delivery_error'] in ACCOUNT_HOLD_REASONS)
    if not held:
        return frozenset()
    provider = model_vendor(str(turn['model']), db=db)
    if provider is None:
        return frozenset()
    try:
        current = chosen_account_id(thread_id, provider, db=db)
        if current != turn['vendor_account_id']:
            return frozenset()
        require_claim(provider, thread_id=thread_id, model=str(turn['model']), db=db)
    except AccountAdmissionError:
        return frozenset()
    return held


async def window_proved_target(target: ExecutionTarget) -> ExecutionTarget:
    """Run discovery once for a target whose catalog row lacks a window."""
    if target.context_window:
        return target
    from llm.context_window_discovery import ensure_context_window

    if await ensure_context_window(target.catalog_id):
        return await resolve_target(target.catalog_id)
    logger.error(
        "DISCOVERY DEFECT: no context-window evidence for {} after "
        "rediscovery; this send is rendered against the portability window "
        "and only the provider may reject it",
        target.catalog_id,
    )
    return target
