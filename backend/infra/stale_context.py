"""Reject a stale prepared context and name the check that found it."""

from __future__ import annotations

import os

from loguru import logger

from infra.send_types import DeliveryFailureSide, SendRejected


def _frame_site(exc: BaseException) -> str:
    tb = exc.__traceback__
    if tb is None:
        return "unknown"
    while tb.tb_next is not None:
        tb = tb.tb_next
    code = tb.tb_frame.f_code
    return f"{os.path.basename(code.co_filename)}:{code.co_name}:{tb.tb_lineno}"


def _chain(exc: BaseException) -> list[BaseException]:
    """``exc`` then its ``__cause__`` (else ``__context__``) links, outermost first."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    return chain


def stale_raise_site(exc: BaseException) -> str:
    """Return ``file:function:line`` of the check that found the context stale.

    A wrapper may re-raise the stale exception ``from`` the original, so walk
    the cause chain to the innermost stale-typed exception and take the last
    frame of its traceback. When wrapped, the outer sites follow as
    ``<-`` names. Read from tracebacks, never message text, so a reworded or
    dynamic message cannot hide which check fired.

    Temporary diagnosis for #1300: delete this, ``_chain``, ``_frame_site``
    and the ``site=`` log field when #1300 closes.
    """
    from subagents.cc_runtime_errors import CCPreparedDestinationStale
    from subagents.codex_runtime_errors import CodexPreparedContextStale

    stale_types = (CCPreparedDestinationStale, CodexPreparedContextStale)
    chain = _chain(exc)
    inner = max(
        (i for i, e in enumerate(chain) if isinstance(e, stale_types)), default=0,
    )
    sites = [_frame_site(chain[inner])]
    sites.extend(_frame_site(e) for e in reversed(chain[:inner]))
    return " <- ".join(sites)


def stale_rejection(
    exc: BaseException, *, harness: str, label: str, thread_id: str,
    turn_id: str | None,
) -> SendRejected:
    """Log which check found the prepared context stale; return the 409."""
    logger.warning(
        "prepared context stale harness={} site={} thread={} turn={}",
        harness,
        stale_raise_site(exc),
        thread_id[:8],
        (turn_id or "")[:8],
    )
    return SendRejected(
        409,
        f"{label} context changed before provider delivery; retry the send.",
        metadata={
            "event_type": "input.rejected",
            "harness": harness,
            "cause": "prepared_context_stale",
        },
        side=DeliveryFailureSide.PROVIDER,
    )
