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
- shows the person a document (``document_open``).

Whether a plan call took effect is read from Plans' own state through
``plans.public``, never from the result text, so a refused call carries no
notice. ``document_open`` keeps no state, so its own reported outcome is read.
The call has taken effect by the time this runs, so a read that fails here is
logged and the result goes out without the line, never as an error.

Both live projection paths call this per result, beside the plan and
repository notices: native in ``core.turns_steps`` and the Claude Code/Codex
bridge in ``subagents.harness_tool_bridge``. Nothing is persisted, so a
rebuilt history omits the line. It reads the database and the document's
file, so it must run off the event loop.

Cost: the plan and document calls above are the only ones that read anything.
Each reads one indexed set of open asks and the files this agent has waiting
on the person; no model is called.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from infra.database import get_db
from infra.product_events import emit_product_event

LABEL = "[CORSIA attention notice]"

#: Average adult silent reading rate for English non-fiction (Brysbaert 2019,
#: "How many words do we read per minute?", Journal of Memory and Language).
#: Reading to judge is slower, so the time stated is a floor.
_WORDS_PER_MINUTE = 238

_WORD = re.compile(r"[^\W_]+")
#: Han and kana, which are written without spaces: a run of them is one
#: ``_WORD`` match however long, so they are counted as characters instead.
_UNSPACED = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")
_IMAGE = re.compile(r"!\[|<img\b", re.IGNORECASE)

_OPENED = frozenset({"requested", "link", "already_open"})
_LEAD = {
    "ask": "The person now has '{name}' to read before they can answer: {length}.",
    "step": "The person now has '{name}' to read before they can do their step: {length}.",
    "open": "You put '{name}' in front of the person: {length}.",
}
_WAITING = " You now have {count} documents waiting on them{total}."
_CLOSE = (
    " If they could act on less, cut the file now, before they open it: put "
    "first what they must decide or do, and keep only what they need for it."
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Measure:
    """What one document asks of its reader."""

    words: int
    unspaced: int
    images: int

    @property
    def in_words(self) -> bool:
        """Whether word count describes the text (false for unspaced scripts)."""
        return self.words >= self.unspaced


def attention_notice(
    *, thread_id: str | None, tool_name: str, arguments: Any, result: Any = None,
    workspace: Path | None = None,
) -> str | None:
    """Return the notice for a call that handed the person a document, else None."""
    if not thread_id or not isinstance(arguments, dict):
        return None
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
    except (sqlite3.Error, ValueError):
        # The call already took effect: a failed read here must not turn its
        # result into an error the agent would retry.
        log.warning("attention notice: could not read what %s handed over", tool_name, exc_info=True)
        return None
    finally:
        db.close()
    measure = _measure(path)
    others = [_measure(other) for other in waiting if other != path]
    if kind == "ask" or path in waiting:
        others.append(measure)
    _record(thread_id, kind, measure, len(others))
    return _render(kind, path.name, measure, others)


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
    """The document a ``document_open`` call showed or linked, as ``(kind, reference)``."""
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


def _measure(path: Path) -> _Measure | None:
    """Count what the file asks of a reader, or None when it cannot be read as text."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    unspaced = len(_UNSPACED.findall(text))
    words = len(_WORD.findall(_UNSPACED.sub(" ", text)))
    return _Measure(words=words, unspaced=unspaced, images=len(_IMAGE.findall(text)))


def _minutes(words: int) -> int:
    return max(1, math.ceil(words / _WORDS_PER_MINUTE))


def _duration(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if rest or not hours:
        parts.append(f"{rest} minute{'s' if rest != 1 else ''}")
    return " ".join(parts)


def _length(measure: _Measure | None) -> str:
    if measure is None:
        return "its length could not be measured, because the file could not be read as text"
    if measure.in_words:
        length = f"{measure.words:,} words, at least {_duration(_minutes(measure.words))} to read"
    else:
        length = f"{measure.unspaced:,} characters"
    if measure.images:
        length += f", {measure.images} image{'s' if measure.images != 1 else ''}"
    return length


def _render(kind: str, name: str, measure: _Measure | None, waiting: list[_Measure | None]) -> str:
    """The notice text: this document, what this agent has waiting in all, then the choice."""
    lead = _LEAD["open" if kind == "open_path" else kind]
    text = f"{LABEL} " + lead.format(name=name, length=_length(measure))
    if len(waiting) > 1:
        words = sum(item.words for item in waiting if item is not None and item.in_words)
        total = f": {words:,} words and at least {_duration(_minutes(words))} in all" if words else ""
        text += _WAITING.format(count=len(waiting), total=total)
    # Nothing measured (an image, a missing file) gives no ground to suggest a cut.
    return text + _CLOSE if measure is not None else text


def _record(thread_id: str, kind: str, measure: _Measure | None, waiting: int) -> None:
    """Record what was handed over, so its size can be read over time without a hand count."""
    emit_product_event(
        "attention_notice",
        "notice_emitted",
        thread_id=thread_id,
        properties={
            "handover": "open" if kind == "open_path" else kind,
            "measured": measure is not None,
            "words": measure.words if measure is not None else None,
            "characters": measure.unspaced if measure is not None else None,
            "images": measure.images if measure is not None else None,
            "waiting_documents": waiting,
        },
    )
