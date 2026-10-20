"""One claimed-draft delivery lifecycle shared by account providers."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from infra.db_async import run_in_db
from tools.action_drafts import DraftError, claim_for_send, mark_sent, release_claim


Token = TypeVar("Token")
Result = TypeVar("Result")


async def _release(draft_id: str, *, label: str) -> None:
    try:
        await run_in_db(release_claim, draft_id)
    except Exception:
        logging.getLogger(__name__).exception("%s could not release the draft claim", label)


async def deliver_claimed(
    draft_id: str,
    *,
    acquire: Callable[[], Awaitable[Token]],
    send: Callable[[Token], Awaitable[Result]],
    result_id: Callable[[Result], str | None],
    success: Callable[[Result, str], str],
    sign_in_error: Callable[[Exception], str],
    request_error: Callable[[Exception], str],
    never_sent: Callable[[Exception], bool],
    still_sending: str,
    claim_error: str,
    unconfirmed: str,
    label: str,
) -> str:
    """Claim once and settle only outcomes that prove delivery did not happen.

    A claim remains ``sending`` after an unconfirmed request, missing provider id,
    or mark-sent failure. That makes a retry refuse instead of duplicating the
    person-approved action.
    """
    try:
        await run_in_db(claim_for_send, draft_id)
    except DraftError:
        return f"Error: {still_sending}"
    except Exception:
        logging.getLogger(__name__).exception("%s could not claim the draft", label)
        return f"Error: {claim_error}"

    try:
        token = await acquire()
    except Exception as exc:
        if exc.__class__.__name__ not in {"GoogleError", "MicrosoftError"}:
            logging.getLogger(__name__).exception("%s could not acquire delivery credentials", label)
        await _release(draft_id, label=label)
        return f"Error: Not sent: {sign_in_error(exc)}"

    try:
        result = await send(token)
    except Exception as exc:
        if never_sent(exc):
            await _release(draft_id, label=label)
            return f"Error: Not sent: {request_error(exc)}"
        logging.getLogger(__name__).exception("%s delivery outcome is unknown", label)
        return f"Error: {unconfirmed}"

    external_id = result_id(result)
    if not isinstance(external_id, str) or not external_id:
        return f"Error: {unconfirmed}"
    try:
        await run_in_db(mark_sent, draft_id, external_id)
    except Exception:
        logging.getLogger(__name__).exception(
            "%s accepted delivery but the draft could not be marked sent", label,
        )
    return success(result, external_id)
