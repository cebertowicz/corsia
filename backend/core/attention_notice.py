"""Tell an agent, measured, what it has just put in front of the person.

Rules about the person's attention are loaded long before the moment an agent
hands something over, and they do not hold there. This attaches one line to
the model-facing text of the call that does the handing over, stating what the
person now has to take in: the document's words, the least time that takes to
read, its images, and how many documents this agent already has waiting on
them. It states facts and leaves the judgment with the agent. It never
refuses, holds or changes the call, and nothing the person sees changes.

A call hands a document to the person when it:

- blocks a task on the person with a document to judge (``plan_update``
  ``block_node`` with ``ask_artifact_id``);
- gives the person a step with an instructions document (``plan_update`` with
  ``assignee: user`` and ``instructions_artifact_id``);
- shows the person a document (``document_open``);
- saves an update (``post_update``), including its linked document cards;
- drafts or updates a decision (``plan_decision``), before it is raised.

A refused plan call can leave matching state from an earlier call. Refusal is
therefore read from the dispatcher's result; the handover itself is read from
Plans' state through ``plans.public``. ``document_open`` keeps no state, so its
own reported outcome is read.
An update is read back from its saved message, named by the call's receipt,
so a refused update carries no notice either.
The call has taken effect by the time this runs, so a read that fails here is
logged and the result goes out without the line, never as an error.

Both live projection paths call this per result, beside the plan and
repository notices: native in ``core.turns_steps`` and the Claude Code/Codex
bridge in ``subagents.harness_tool_bridge``. The line itself is not persisted,
so a rebuilt history omits it; an update's row records only when its notice
was shown (``message_update_state.notice_at``), for Measurement. It reads the database and the document's
file, so it must run off the event loop.

Cost: a saved update reads its own message row; linked documents additionally
read the open asks and linked files. A decision reads its public projection.
No model is called.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from core.attention_measure import Measure, describe, measure_file, measure_text, total_of
from infra.database import get_db
from infra.product_events import emit_product_event

LABEL = "[CORSIA attention notice]"

#: ``document_open`` outcomes that showed the person the document. ``link`` is
#: not one: the tool returns it when it could not show the document, and the
#: update that then posts the link carries the notice.
_OPENED = frozenset({"requested", "already_open"})
_LEAD = {
    "ask": "The person now has '{name}' to read before they can answer: {length}.",
    "step": "The person now has '{name}' to read before they can do their step: {length}.",
    "open": "You put '{name}' in front of the person: {length}.",
}
_WAITING = " You now have {count} documents waiting on them{total}."
_CLOSE = (
    " If they could act on less, cut the file now{before}: put "
    "first what they must decide or do, and keep only what they need for it."
)
#: Said only where the document is waiting for the person, not already open in front of them.
_BEFORE_OPENING = ", before they open it"

#: The first line of ``tools.post_update_text.result_text`` and of a replayed
#: receipt: the update was saved, under this message id.
_SAVED_UPDATE = re.compile(r"Update saved as message (\S+)\.")
#: The card type chat gives a file made to be read (``registry.file_types``).
_READ_TYPE = "document"
_LINKED_ONE = "Your update links '{name}' for the person to read: {length}."
_LINKED_MANY = "Your update links {count} documents for the person to read{total}."
_LONGEST = " The longest is '{name}': {length}."
_CLOSE_MANY = (
    " If they could act on less, link fewer or cut them now, before they open "
    "them: put first what they must decide or do, and keep only what they need for it."
)

_CHECK = (
    "Before the person reads it: does each line carry its whole idea without leaving them a "
    "question, and does it say whether it is how things are today, a plan, or already done? "
    "If not, rewrite it now."
)

log = logging.getLogger(__name__)


def attention_notice(
    *, thread_id: str | None, tool_name: str, arguments: Any, result: Any = None,
    workspace: Path | None = None,
) -> str | None:
    """Add a notice without changing the outcome of the completed call."""
    from doceditor.document_scope import ProjectNotFound

    try:
        return _attention_notice(
            thread_id=thread_id, tool_name=tool_name, arguments=arguments,
            result=result, workspace=workspace,
        )
    except (sqlite3.Error, ValueError, OSError, ProjectNotFound):
        log.warning("attention notice: could not project %s", tool_name, exc_info=True)
        return None


def _attention_notice(
    *, thread_id: str | None, tool_name: str, arguments: Any, result: Any = None,
    workspace: Path | None = None,
) -> str | None:
    """Return a notice for a successful hand-over or a decision draft, else None."""
    if not thread_id or not isinstance(arguments, dict):
        return None
    if isinstance(result, str) and result.startswith("Error:"):
        return None
    if tool_name == "post_update":
        return _update_notice(thread_id, arguments, result)
    if tool_name == "plan_decision":
        return _decision_notice(thread_id, arguments, result)
    if tool_name == "plan_update":
        params = arguments.get("params")
        target = _plan_target(arguments.get("operation"), params) if isinstance(params, dict) else None
    elif tool_name == "document_open":
        target = _open_target(arguments, result)
    else:
        return None
    if target is None:
        return None
    kind, reference = target
    db = get_db()
    try:
        path = _handed_over(db, thread_id, kind, reference, workspace)
        if path is None:
            return None
        waiting = _waiting_paths(db, thread_id)
    finally:
        db.close()
    measure = measure_file(path)
    others = [measure_file(other) for other in waiting if other != path]
    # A step's own document waits on the person as an ask's does. The documents
    # of this agent's other open person steps are not counted: Plans' public
    # surface has no read for them.
    if kind in ("ask", "step") or path in waiting:
        others.append(measure)
    _record(thread_id, "open" if kind == "open_path" else kind, [measure], len(others))
    return _render(kind, path.name, measure, others) + "\n" + _CHECK


def _update_notice(thread_id: str, arguments: dict, result: Any) -> str | None:
    """Measure a saved update and any documents the person gets as cards."""
    from registry.path_cards import targets

    saved = _SAVED_UPDATE.match(result) if isinstance(result, str) else None
    if saved is None:
        return None
    db = get_db()
    try:
        from infra.chat_comment_answers import VISIBLE_CONTENT, VISIBLE_CONTENT_JOIN, words_for_the_person

        row = db.execute(
            f"SELECT {VISIBLE_CONTENT} AS content, m.content AS saved,"
            " t.user_id AS user_id, t.project_id AS project_id"
            f" FROM messages AS m JOIN threads AS t ON t.id = m.thread_id {VISIBLE_CONTENT_JOIN}"
            " WHERE m.id=? AND m.thread_id=?",
            (saved.group(1), thread_id),
        ).fetchone()
        if row is None or not isinstance(row["saved"], str):
            return None
        # Cards follow the chat text; the person reads every word, answers in comment threads included.
        text = row["content"] or ""
        words = words_for_the_person(row["saved"])
        linked = _linked_documents(db, text, row["user_id"], row["project_id"]) if targets(text) else []
        waiting = _waiting_paths(db, thread_id) if linked else []
    finally:
        db.close()
    measured = [(path, measure_file(path)) for path in linked]
    others = [measure_file(other) for other in waiting if other not in linked]
    update_measure = measure_text(words)
    _record(
        thread_id, "update", [measure for _path, measure in measured],
        len(others) + len(linked) if linked else None, text_measure=update_measure,
    )
    notice = f"{LABEL} Your update: {describe(update_measure)}."
    if linked:
        notice += " " + _render_update(measured, others).removeprefix(f"{LABEL} ")
    _mark_notice_shown(thread_id, saved.group(1))
    return notice + "\n" + _CHECK + " (replace it while it is unseen)"


def _mark_notice_shown(thread_id: str, message_id: str) -> None:
    """Record the first time this update's check was shown; a failed write only loses the reading."""
    from infra.database import TransactionOwnershipError, write_transaction
    from infra.db_connection import LaneWriteError
    from infra.time import utc_now_iso

    try:
        with write_transaction(None, scope="update_notice_shown") as conn:
            conn.execute(
                "UPDATE message_update_state SET notice_at=COALESCE(notice_at,?)"
                " WHERE message_id=? AND thread_id=?",
                (utc_now_iso(), message_id, thread_id),
            )
    # The notice still goes out: the call has taken effect and the line is the agent's check.
    except (sqlite3.Error, TransactionOwnershipError, LaneWriteError):
        log.warning("attention notice: could not record notice time for %s", message_id, exc_info=True)


def _decision_notice(thread_id: str, arguments: dict, result: Any) -> str | None:
    """Read and measure a successful decision through Plans' public boundary."""
    from core.attention_decision import measure_decision

    measure = measure_decision(thread_id, arguments, result, get_db)
    if measure is None:
        return None
    _record(thread_id, "decision", [], None, text_measure=measure)
    return f"{LABEL} Your decision: {describe(measure)}.\n{_CHECK}"


def _linked_documents(db, text: str, user_id: str, project_id: str | None) -> list[Path]:
    """The documents chat shows the person as cards under this agent's saved update."""
    from doceditor.document_scope import scope_roots
    from infra import config
    from registry.path_cards import reference_cards

    # The text, the roots and the second origin are the ones ``api.thread_cards.attach_cards``
    # gives the same reader; ``test_attention_notice_update_links`` holds the two together.
    origin, bases = scope_roots(project_id, db)
    cards = reference_cards(
        db, text, bases, str(user_id), origin=origin,
        fallback_origin=config.WORKSPACE.resolve() if project_id else None,
    )
    paths: list[Path] = []
    for card in cards:
        if card.get("type") != _READ_TYPE or not card.get("available"):
            continue
        artifact_id = card.get("artifact_id")
        path = _artifact_path(db, str(artifact_id)) if artifact_id else Path(str(card["path"]))
        if path is not None and path not in paths:
            paths.append(path)
    return paths


def _plan_target(operation: Any, params: dict) -> tuple[str, str] | None:
    """The document a plan call names for the person, as ``(kind, artifact id)``."""
    ask = _name(params.get("ask_artifact_id"))
    if operation == "block_node" and params.get("owner_kind") == "user" and ask:
        return "ask", ask
    instructions = _name(params.get("instructions_artifact_id"))
    if params.get("assignee") == "user" and instructions:
        return "step", instructions
    return None


def _open_target(arguments: dict, result: Any) -> tuple[str, str] | None:
    """The document a ``document_open`` call showed the person, as ``(kind, reference)``."""
    try:
        outcome = json.loads(result).get("outcome") if isinstance(result, str) else None
    except (ValueError, AttributeError):
        return None
    if outcome not in _OPENED:
        return None
    artifact_id, path = _name(arguments.get("artifact_id")), _name(arguments.get("path"))
    if artifact_id:
        return "open", artifact_id
    return ("open_path", path) if path else None


def _name(value: Any) -> str | None:
    """The argument as a non-empty name, or None when the call did not give one."""
    return value.strip() or None if isinstance(value, str) else None


def _handed_over(db, thread_id: str, kind: str, reference: str, workspace: Path | None) -> Path | None:
    """The file the call put in front of the person, or None when it did not."""
    from plans.public import person_step_instructions, raised_asks

    if kind == "open_path":
        from tools.artifact_tool_helpers import default_workspace, tool_path

        return Path(tool_path(reference, default_workspace(workspace)))
    if kind == "ask" and not any(
        str(row["asker_id"]) == thread_id for row in raised_asks(db, reference)
    ):
        return None
    if kind == "step":
        step = person_step_instructions(reference, db=db)
        if step is None or str(step["owner_thread_id"]) != thread_id:
            return None
    return _artifact_path(db, reference)


def _waiting_paths(db, thread_id: str) -> list[Path]:
    """The documents this agent has waiting on the person as open asks."""
    from plans.public import open_asks_sql

    asks = open_asks_sql(
        "l.artifact_id AS artifact_id, p.owner_kind AS owner_kind, p.owner_id AS owner_id",
        by="all",
    )
    rows = db.execute(
        f"SELECT DISTINCT artifact_id FROM ({asks}) WHERE owner_kind='thread' AND owner_id=?",
        (thread_id,),
    ).fetchall()
    paths = (_artifact_path(db, str(row["artifact_id"])) for row in rows)
    return [path for path in paths if path is not None]


def _artifact_path(db, artifact_id: str) -> Path | None:
    """Where a registered document's file is now, or None when it has none."""
    from infra import config
    from infra.workspace_paths import resolve_registered_path
    from registry import models as registry_models

    artifact = registry_models.get_artifact(artifact_id, db=db)
    if artifact is None:
        return None
    try:
        raw = resolve_registered_path(artifact.get("external_id"), artifact.get("path"))
    except ValueError:
        log.warning("attention notice: document %s is outside this workspace", artifact_id)
        return None
    raw = raw or artifact.get("external_id")
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else config.WORKSPACE / path


def _render(kind: str, name: str, measure: Measure | None, waiting: list[Measure | None]) -> str:
    """The notice text: this document, what this agent has waiting in all, then the choice."""
    lead = _LEAD["open" if kind == "open_path" else kind]
    text = f"{LABEL} " + lead.format(name=name, length=describe(measure))
    if len(waiting) > 1:
        text += _WAITING.format(count=len(waiting), total=total_of(waiting))
    # Nothing measured (an image, a missing file) gives no ground to suggest a cut.
    if measure is None:
        return text
    return text + _CLOSE.format(before="" if kind in ("open", "open_path") else _BEFORE_OPENING)


def _render_update(
    linked: list[tuple[Path, Measure | None]], others: list[Measure | None],
) -> str:
    """The notice for an update: what it links, what this agent has waiting in all, then the choice."""
    measures = [measure for _path, measure in linked]
    if len(linked) == 1:
        path, measure = linked[0]
        text = f"{LABEL} " + _LINKED_ONE.format(name=path.name, length=describe(measure))
    else:
        text = f"{LABEL} " + _LINKED_MANY.format(count=len(linked), total=total_of(measures))
        longest = max(linked, key=lambda item: _size(item[1]))
        if longest[1] is not None:
            text += _LONGEST.format(name=longest[0].name, length=describe(longest[1]))
    if others:
        everything = measures + others
        text += _WAITING.format(count=len(everything), total=total_of(everything))
    if all(measure is None for measure in measures):
        return text
    return text + (_CLOSE.format(before=_BEFORE_OPENING) if len(linked) == 1 else _CLOSE_MANY)


def _size(measure: Measure | None) -> int:
    """How much one document asks, for picking the longest; unmeasured sorts first."""
    return -1 if measure is None else max(measure.words, measure.unspaced)


def _record(
    thread_id: str, handover: str, measures: list[Measure | None], waiting: int | None,
    *, text_measure: Measure | None = None,
) -> None:
    """Record what was handed over, so its size can be read over time without a hand count.

    One event per call: an update that links several documents records their totals.
    """
    measured = [measure for measure in measures if measure is not None]
    properties: dict[str, Any] = {
        "handover": handover,
        "measured": bool(measured),
        "words": sum(measure.words for measure in measured) if measured else None,
        "characters": sum(measure.unspaced for measure in measured) if measured else None,
        "images": sum(measure.images for measure in measured) if measured else None,
        "waiting_documents": waiting,
    }
    if handover == "update":
        properties["linked_documents"] = len(measures)
    if text_measure is not None:
        properties[f"{handover}_words"] = text_measure.words
        properties[f"{handover}_characters"] = text_measure.unspaced
    emit_product_event(
        "attention_notice", "notice_emitted", thread_id=thread_id, properties=properties,
    )
