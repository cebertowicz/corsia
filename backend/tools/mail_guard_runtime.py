"""Run reply checks over provider data with the existing email-read disclosure screen."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from tools.email_read import screened_message
from tools.mail_guard import Message, Verdict, check_reply, normalize_subject, same_sender


async def _visible(backend: Any, account_id: str, message: Message) -> Message:
    message = replace(message, body=await backend.fetch_body(account_id, message.message_id))
    source = SimpleNamespace(message_id=message.message_id, thread_id=message.thread_id,
                             headers={"from": message.sender, "subject": message.subject,
                                      "date": message.date.isoformat()}, body=message.body)
    rendered = await screened_message(account_id, source)
    try:
        payload = json.loads(rendered)
    except (TypeError, ValueError):
        return message
    if not isinstance(payload, dict) or not isinstance(payload.get("body"), str):
        return message
    return replace(message, visible_evidence=f"Subject: {payload.get('subject', '')}\n{payload['body']}")


async def guarded_reply(backend: Any, account: dict, target_id: str, body: str,
                        requested_to: list[str]) -> tuple[Verdict, Message]:
    """Read fresh provider state; screen only messages that the guard may show."""
    account_id = str(account["id"])
    target, thread, sender = await backend.guard_data(account_id, target_id)
    candidates = {m.message_id: m for m in thread if m.date > target.date}
    for message in sender:
        if (message.date > target.date and message.thread_id != target.thread_id
                and same_sender(message.sender, target.sender)
                and normalize_subject(message.subject) == normalize_subject(target.subject)):
            candidates[message.message_id] = message
    screened = {mid: await _visible(backend, account_id, message) for mid, message in candidates.items()}
    thread = [screened.get(m.message_id, m) for m in thread]
    sender = [screened.get(m.message_id, m) for m in sender]
    return check_reply(target, thread, sender, str(account["address"]), body, requested_to,
                       datetime.now(timezone.utc)), target
