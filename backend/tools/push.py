"""urgent_push (design prototype): the CORSIA agent's one urgent push to the person's devices.

The caller check runs first, server-side, on the thread the runner set (R1). The prototype's presence,
device list and delivery are injected through ``HOOKS`` so the guards can be run against fakes; the real
build replaces them with the app-wide here signal (R4), the subscription table and the Web Push sender.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from infra import here_signal
from infra.corsia_agent import is_corsia_agent
from infra.db_async import run_in_db
from infra.push_guard import LINE_MAX, REASON_MAX, TITLE_MAX, PushRefused, clean, tap_path, valid_tap_path, validate_endpoint
from tools.base import Tool

REFUSED = (
    "refused: only the CORSIA agent can send an urgent push. Send the CORSIA agent the case: what can't wait, "
    "why it can't wait until the person next opens CORSIA, and the Activity item where they answer. It decides "
    "whether to push. Keep the item in Activity either way."
)
HELD = (
    "held: the person is using CORSIA right now, so nothing was sent. Don't send again now. Arm one plan watch "
    "on the item; if it is still open when the watch wakes, judge it again."
)
DECLINED = (
    "declined: nothing was sent. Tell the asking agent why in one line; the item stays in Activity."
)
NO_DEVICE = (
    "no_device: no device has urgent notifications on, so nothing was sent. Use the person's email channel if one "
    "is set. The item stays in Activity either way."
)
FAILED = (
    "failed: the push service did not take it, so nothing reached the person's device. Use the person's email "
    "channel if one is set. The item stays in Activity either way."
)


def sent(count: int) -> str:
    return (
        f"sent: pushed to {count} device{'s' if count != 1 else ''}. The item stays in Activity, where the person "
        "answers it. Don't push it again."
    )


@dataclass
class PushHooks:
    #: Whether any live connection of the person reports here (R4): the app-wide here signal, in memory.
    is_here: Callable[[], bool] = lambda: here_signal.is_here()
    #: Whether the item is open in the person's Activity, read the way Activity's own list reads it (its "Your tasks" rows).
    item_open: Callable[[str, str], bool] = lambda thread_id, item_id: _open_in_activity(thread_id, item_id)
    #: The devices with urgent notifications on: dicts with `id` and `endpoint`.
    devices: Callable[[], list[dict]] = lambda: []
    #: Delivers to one device: "sent", "gone" (turn it Off, R22) or "failed".
    deliver: Callable[[dict, str, str, str], Awaitable[str]] = field(default=None)  # type: ignore[assignment]
    #: Turns a device Off after an R22 answer.
    turn_off: Callable[[dict], None] = lambda device: None
    #: Appends the log row (R3, R16): outcome, caller, item, title (None for a decline) and the one-line reason.
    log: Callable[[dict], None] = lambda entry: None


def _open_in_activity(thread_id: str, item_id: str) -> bool:
    """True when `item_id` is the external id of a row in the person's Activity "Your tasks" list: the id `/?activity=` opens."""
    from api.attention import activity_queue
    from infra.database import get_db
    from tools.delegation import _current_thread_context

    context = _current_thread_context(thread_id)
    if not context:
        return False
    db = get_db()
    try:
        rows, _, _ = activity_queue(db, context["user_id"], project_id=None, view="activity")
    finally:
        db.close()
    return any(row.get("external_id") == item_id for row in rows)


HOOKS = PushHooks()


class UrgentPushTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "urgent_push"

    @property
    def description(self) -> str:
        return (
            "Send the person one urgent push to the phones and browsers that have urgent notifications on, or "
            "decline to. Only the CORSIA agent can send; any other caller gets refused. Pass item_id (the id of the "
            "decision, task, review or reply open in the person's Activity), title (at most 40 characters), line (one line, at most 120) and "
            "reason (one line on why this can't wait). To decline, pass item_id, reason and decline: true; title and "
            "line are not needed. Title and line say only what the person must do and by when, never what it is "
            "about. Nothing sends while the person is using CORSIA. Returns one of sent, held, no_device, failed, "
            "refused or declined, and says what to do next."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "item_id": {"type": "string", "description": "Id of the person's open decision (from plan_decision), task (the plan step's id), review (the document's id) or reply."},
                "title": {"type": "string", "description": f"At most {TITLE_MAX} characters: what to do and by when."},
                "line": {"type": "string", "description": f"One line, at most {LINE_MAX} characters: what to do and by when."},
                "reason": {"type": "string", "description": "One line: why this can't wait, or why you decline."},
                "decline": {"type": "boolean", "description": "True to decline: sends nothing, logs the reason."},
            },
            "required": ["item_id", "reason"],
        }

    async def execute(self, **kwargs: Any) -> str:
        caller = kwargs.get("_thread_id")
        item_id = kwargs.get("item_id") if isinstance(kwargs.get("item_id"), str) else None
        reason_raw = kwargs.get("reason")
        if not isinstance(caller, str) or not is_corsia_agent(caller):
            HOOKS.log({"caller": caller, "outcome": "refused", "item_id": item_id, "title": None, "reason": None})
            return REFUSED
        declining = kwargs.get("decline") is True
        try:
            reason = clean(reason_raw, REASON_MAX, "reason")
            path = tap_path(item_id)
            if not valid_tap_path(path):
                raise PushRefused("The item_id must be an Activity item id.")
            title = line = None
            if not declining:
                # Before any send: the item must be open in the person's Activity (a decline needs no item).
                if not await run_in_db(HOOKS.item_open, caller, item_id):
                    raise PushRefused("That item is not open in the person's Activity: pass the id of a decision, task, review or reply that is.")
                title = clean(kwargs.get("title"), TITLE_MAX, "title")
                line = clean(kwargs.get("line"), LINE_MAX, "line")
        except PushRefused as error:
            return f"Error: Nothing sent. {error} Fix it and call again."

        def log(outcome: str) -> None:
            HOOKS.log({"caller": caller, "outcome": outcome, "item_id": item_id, "title": title, "reason": reason})

        if declining:
            log("declined")
            return DECLINED
        if HOOKS.is_here():
            log("held")
            return HELD
        devices = HOOKS.devices()
        if not devices:
            log("no_device")
            return NO_DEVICE
        delivered = 0
        for device in devices:
            # Every failure, of any kind, is "failed" for that device. The exception is never read, so no text, endpoint
            # or push-service answer can reach the result, the span or a log line.
            try:
                validate_endpoint(device["endpoint"])
                result = await HOOKS.deliver(device, title, line, path)
            except BaseException as error:  # noqa: BLE001
                if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                    raise
                result = "failed"
            if result == "gone":
                HOOKS.turn_off(device)
            delivered += result == "sent"
        outcome = "sent" if delivered else "failed"
        log(outcome)
        return sent(delivered) if delivered else FAILED


def payload(title: str, line: str, path: str) -> str:
    """The notification the worker shows: a title, one line, one tag per item, and the tap path."""
    return json.dumps({"title": title, "body": line, "tag": path, "path": path}, ensure_ascii=False)
