"""The fresh read: nothing an agent puts in front of the person reaches them until a reader
other than its writer's context has passed that exact text.

One gate for every door. A door builds an :class:`Item` from the text exactly as the person
will see it and awaits :func:`check` before it writes anything. On a pass the door takes
effect and records the returned ``fresh_read_id``; on a fail or a hold it refuses with the
returned message and delivers nothing. It never fails open: when no reader can run, the
item is held and the agent is told why.

Design: ``shared/substrate/DRAFT-fresh-reader-before-person.md`` (revision 4.4).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

from core.fresh_read_candidates import Candidate, candidates, install_cap, request_measure, seat_waiting
from core.fresh_read_prompt import (
    OUTPUT_SCHEMA, SYSTEM_PROMPT, Context, ReaderAnswer, message_line, task_text, user_prompt, validator,
)
from core.fresh_read_store import FAILED, HELD, PASSED, ReadRecord, record, sha256_bytes, sha256_text
from infra.db_async import run_in_db
from infra.fresh_read_schema import PERSON_ROW

#: The longest one check holds a door call, across every candidate; each candidate gets what is
#: left. Basis: measured. The slowest of three reads registered before the run, each at the
#: full input cap on a local Qwen3.8 Flash Next seat with nothing else running: 120.04 s on the
#: cold read (the two cache-warm reads took 47 s and 50 s), rounded up. Method and numbers are
#: in the PR that set it (#3114, "Bounds and their basis"). Cloud readers are unmeasured, and so
#: is a seat under load.
CHECK_BOUND_SECONDS = 121.0

_CHECK_NAMES = {
    1: "the person can't act on it from what it shows",
    2: "it asks or reopens something already settled",
    3: "it changes what the person asked for",
    4: "it doesn't say whether a claim is how things are, a plan, or done",
}


@dataclass(frozen=True)
class Document:
    """A document an item hands over, with the exact bytes the person will be shown; ``artifact_id``
    is None for a file with no registration, which is read but has no view to move."""

    artifact_id: str | None
    name: str
    data: bytes


@dataclass(frozen=True)
class Item:
    """One hand-over to the person. ``shown`` is the exact text they see; ``kind`` names the door."""

    kind: str
    item_ref: str
    shown: str
    writer_thread_id: str | None
    user_id: str | None = None
    node_id: str | None = None
    documents: tuple[Document, ...] = ()
    note_to_reader: str | None = None
    #: Where the item sits, read with it but not bound by its hash: a comment's passage and thread.
    where: str | None = None


@dataclass(frozen=True)
class Verdict:
    passed: bool
    fresh_read_id: str | None
    message: str | None = None
    #: No reader could run, as opposed to a reader failing the item.
    held: bool = False
    #: A failed read's findings, each with its quote, check number and why.
    findings: tuple[dict[str, Any], ...] = ()


#: The person's own messages in one chat, newest first, through ``idx_messages_person``: the
#: walk examines only rows it returns.
_PERSON_ROWS = (
    f"SELECT content, created_at FROM messages INDEXED BY idx_messages_person "
    f"WHERE thread_id=? AND {PERSON_ROW} ORDER BY created_at DESC"
)


def _person_messages(db, thread_id: str | None, budget: int | None, measure) -> tuple[tuple[str, str], ...]:
    """The person's own messages in the writer's chat and every chat up its delegating chain, newest first.

    Only what can fill ``budget`` (the reader's input cap) is read: each chat stops once its own
    newest messages pass it, since the merged list can hold no more. With no cap, none is read.
    """
    if budget is None:
        return ()
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    while thread_id and thread_id not in seen:  # to the chain's root; a cycle stops at the first repeat
        seen.add(thread_id)
        spent = 0
        for row in db.execute(_PERSON_ROWS, (thread_id,)):
            message = (str(row["created_at"]), str(row["content"]).strip())
            found.append(message)
            spent += measure(message_line(*message))
            if spent > budget:
                break
        parent = db.execute("SELECT parent_thread_id FROM threads WHERE id=?", (thread_id,)).fetchone()
        thread_id = parent["parent_thread_id"] if parent else None
    return tuple(sorted(found, key=lambda message: message[0], reverse=True))


def _task(db, node_id: str | None) -> str | None:
    """The task the item names, in full; an unknown node names none and the read still runs."""
    if not node_id:
        return None
    from plans import public

    try:
        return task_text(public.intent_of(node_id, db=db))
    except public.PlanError as exc:  # an unknown node, or an intent cycle: no task to read against
        logger.info("fresh read: node {} names no task ({})", node_id, exc)
        return None


def _prepare(item: Item, cap: int | None, found: list[Candidate]) -> tuple[str, str | None]:
    """Blocking. The reader's input and where the cap cut the person's messages.

    Any failure to read the task or the messages raises, and the check holds the item:
    a read against less than it should have is never passed off as a pass.
    """
    from infra.database import get_db

    measure = request_measure(found)
    db = get_db()
    try:
        context = Context(task=_task(db, item.node_id),
                          messages=_person_messages(db, item.writer_thread_id, cap, measure),
                          note_to_reader=item.note_to_reader, where=item.where)
    finally:
        db.close()
    # A document that is itself the item (an open) is read once, as the item.
    documents = tuple((doc.name, text) for doc in item.documents
                      if (text := doc.data.decode("utf-8", errors="replace")) != item.shown)
    return user_prompt(item.kind, item.shown, documents, context, cap, measure)


def _resolve(candidate: Candidate, writer_thread_id: str | None):
    """Blocking. The exact target, its account and binary, under the writer's locality policy."""
    from dataclasses import replace

    from connections.account_policy import thread_scope
    from core.fork_render import fork_binary
    from infra.database import get_db
    from llm.resolution import resolve_thread_target
    from reviewer.reviewer_account import resolve_scope_reviewer_account

    db = get_db()
    try:
        target = resolve_thread_target(candidate.model, thread_id=writer_thread_id, db=db)
        if not target.available:
            raise LookupError(target.availability_reason or "model_unavailable")
        if target.harness in {"claude-code", "codex"} and writer_thread_id:
            project = thread_scope(writer_thread_id, db=db)["project_id"]
            target = replace(target, account_id=resolve_scope_reviewer_account(
                target, project, db=db, thread_id=writer_thread_id))
        return target, fork_binary(target, db)
    finally:
        db.close()


async def _read_once(candidate: Candidate, item: Item, prompt: str, seconds: float) -> tuple[ReaderAnswer, Any]:
    """One candidate's read, given ``seconds``: what is left of the check's bound."""
    from llm.isolated_structured import invoke_isolated_structured

    async def read() -> tuple[ReaderAnswer, Any]:
        target, binary = await run_in_db(_resolve, candidate, item.writer_thread_id)
        seen: list[Any] = []
        answer = await invoke_isolated_structured(
            target, system_prompt=SYSTEM_PROMPT, user_prompt=prompt, output_schema=OUTPUT_SCHEMA,
            validator=validator(item.shown), max_output_tokens=candidate.output_allowance,
            timeout_seconds=seconds, binary=binary, telemetry_sink=seen.append,
            thread_id=item.writer_thread_id, account_id=target.account_id,
            account_bound=target.account_id is not None,
        )
        return answer, (seen[-1] if seen else None)

    return await asyncio.wait_for(read(), timeout=seconds)


def _reason(exc: BaseException) -> str:
    """A short, stable reason a candidate could not read."""
    name = type(exc).__name__
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "Timeout" in name:
        return "timed out"
    if "ContextOverflow" in name:
        return "input too long for this model"
    if "RateLimit" in name:
        return "at its usage limit"
    if isinstance(exc, LookupError):
        return f"not available ({exc})"
    if isinstance(exc, ValueError):
        return "gave no usable verdict"
    return f"failed ({name})"


def _emit(item: Item, verdict: str, read_id: str, extra: dict[str, Any]) -> None:
    from infra.product_events import emit_product_event

    emit_product_event("fresh_read", verdict, thread_id=item.writer_thread_id, user_id=item.user_id,
                       properties={"kind": item.kind, "fresh_read_id": read_id, "door_live": True, **extra})


def finding_lines(findings: tuple[dict[str, Any], ...]) -> str:
    return "\n".join(f'- "{finding["quote"]}" (check {finding["check"]}: {_CHECK_NAMES[finding["check"]]}): '
                     f'{finding["why"]}' for finding in findings)


def failed_message(findings: tuple[dict[str, Any], ...]) -> str:
    return ("Not delivered: a fresh reader found what the person would have to correct. Nothing was sent.\n"
            + finding_lines(findings)
            + "\nRevise and call again; new text gets a new read. To answer a finding, pass note_to_reader.")


def held_message(attempts: list[dict[str, Any]]) -> str:
    reasons = "; ".join(f"{attempt['model']}: {attempt['reason']}" for attempt in attempts) or "no reader model is set"
    return (f"Not delivered: the check before the person could not run ({reasons}). Nothing was sent. "
            "Try again later; the item stays unsent until a read passes.")


def _documents(item: Item) -> tuple[tuple[str, str, bytes], ...]:
    """The registered documents a pass moves; a file with no registration is read but has no view."""
    return tuple((doc.artifact_id, sha256_bytes(doc.data), doc.data) for doc in item.documents
                 if doc.artifact_id is not None)


async def check(item: Item) -> Verdict:
    """Read ``item`` fresh and record the outcome within ``CHECK_BOUND_SECONDS``; never raises for a reader's failure."""
    started = time.monotonic()
    deadline = started + CHECK_BOUND_SECONDS
    found = await run_in_db(candidates, item.writer_thread_id)
    cap = install_cap(found)
    attempts: list[dict[str, Any]] = []
    base = ReadRecord(kind=item.kind, item_ref=item.item_ref, sha256=sha256_text(item.shown), verdict=HELD,
                      writer_thread_id=item.writer_thread_id, node_id=item.node_id, input_cap=cap,
                      documents=_documents(item))
    try:
        prompt, base.messages_cut_from = await run_in_db(_prepare, item, cap, found)
    except Exception as exc:  # noqa: BLE001  # no input to read against: held, recorded, never passed
        logger.exception("fresh read: could not gather what {} is read against", item.kind)
        found, prompt = [], ""
        attempts.append({"model": "none",
                         "reason": f"could not read the task or the person's messages ({type(exc).__name__})"})
    for candidate in found:
        if not candidate.writer_own and await seat_waiting(candidate):
            attempts.append({"model": candidate.model, "reason": "local seat has waiting requests"})
            continue
        if (left := deadline - time.monotonic()) <= 0:
            attempts.append({"model": candidate.model, "reason": "the check's time ran out before its turn"})
            continue
        try:
            answer, telemetry = await _read_once(candidate, item, prompt, left)
        except Exception as exc:  # noqa: BLE001  # any failure makes this candidate unavailable; the gate moves on
            logger.info("fresh read: {} could not read {} ({})", candidate.model, item.kind, type(exc).__name__)
            attempts.append({"model": candidate.model, "reason": _reason(exc)})
            continue
        base.verdict = PASSED if answer.passed else FAILED
        base.findings, base.attempts, base.reader_model = list(answer.findings), attempts, candidate.model
        base.elapsed_ms = int((time.monotonic() - started) * 1000)
        if telemetry is not None:
            base.prompt_tokens, base.completion_tokens = telemetry.prompt_tokens, telemetry.completion_tokens
            base.cost_usd, base.cost_source = telemetry.cost_usd, telemetry.cost_source
        read_id = await run_in_db(record, base)
        _emit(item, base.verdict, read_id, {"reader_model": candidate.model, "elapsed_ms": base.elapsed_ms,
                                            "input_cap": cap, "messages_cut": base.messages_cut_from is not None,
                                            "skipped": len(attempts)})
        if answer.passed:
            return Verdict(True, read_id)
        return Verdict(False, None, failed_message(answer.findings), findings=tuple(answer.findings))
    base.attempts, base.elapsed_ms = attempts, int((time.monotonic() - started) * 1000)
    read_id = await run_in_db(record, base)
    _emit(item, HELD, read_id, {"elapsed_ms": base.elapsed_ms, "attempts": attempts})
    return Verdict(False, None, held_message(attempts), held=True)
