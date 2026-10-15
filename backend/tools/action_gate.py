"""The code gate in front of every outbound send: the person asked, and saw this text.

``check_action`` passes only when the call runs inside a live turn that the
person started or wrote into, the draft is the thread's own open draft, the
document still holds the bytes the draft tools recorded and the call names,
and the person's newest message in this turn is later than that recording.
The model's judgement is not an input.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from doceditor.document_scope import revision as document_revision
from infra import config, state
from infra.db_async import run_in_db

NO_TURN = (
    "Not sent: this call has no live turn, so CORSIA can't confirm the person asked. "
    "Ask them in chat."
)
NOT_PERSON = (
    "Not sent: the person hasn't written in this turn. Show them the draft and end your "
    "turn; their reply starts your next one."
)
NO_DRAFT = "Not sent: there is no open draft with that id in this conversation."
ALREADY_DONE = "Not sent: that draft was already sent or discarded."
CHANGED_OUTSIDE = (
    "Not sent: the draft was changed outside the draft tools. Save it with email_draft, "
    "show the person, and wait for their reply."
)
STALE_REVISION = (
    "Not sent: the draft has changed since you read it. Read it again, show the person, "
    "and wait for their reply."
)
NOT_SEEN = (
    "Not sent: the person hasn't replied since the draft's last change. Show them the "
    "draft and end your turn; their reply starts your next one."
)

# The person's newest message this call can have seen: written by a person,
# claimed by this turn, delivered to the model, and ordered in the transcript
# before the Assistant Step that declared the call. A claim alone is not enough:
# a steered message can be claimed while a call the model emitted earlier is
# still pending. Admission stamps ``reply_via_post_update`` only on a row with
# none of the other markers; the three IS NULL lines keep a row stamped before a
# marker existed from passing.
_HUMAN_INPUTS = """
    SELECT created_at FROM messages
    WHERE thread_id = :thread_id AND input_order IS NOT NULL
      AND role = 'user'
      AND active_turn_id = :turn_id
      AND delivery_state = 'delivered'
      AND seq IS NOT NULL AND seq < :step_seq
      AND json_valid(metadata)
      AND json_extract(metadata, '$.reply_via_post_update') = 1
      AND json_extract(metadata, '$.sender_kind') IS NULL
      AND json_extract(metadata, '$.completion') IS NULL
      AND json_extract(metadata, '$.routed_event') IS NULL
    ORDER BY seq DESC
    LIMIT 1
"""


@dataclass(frozen=True)
class Approved:
    """The draft that may be sent, with the exact bytes the gate checked."""

    draft: dict
    data: bytes


@dataclass(frozen=True)
class Refusal:
    """The tool's error text; nothing was sent."""

    text: str


@dataclass(frozen=True)
class _Stage:
    """What the database stage learned, finished off once the bytes are read."""

    refusal: Refusal | None = None
    draft: dict | None = None
    human_at: str | None = None


def _declaring_step_seq(db, thread_id: str, turn_id: str, call_id: str) -> int | None:
    """Where the model response that declared ``call_id`` starts in this turn's transcript.

    Claude Code persists a parallel batch as one row per call, sharing the
    provider's message id; a steer can land between those rows. The model wrote
    every call in the response before it could see that steer, so the response's
    first row is the position that counts.
    """
    from core.message_row import assistant_calls, call_id as declared_id

    steps = db.execute(
        "SELECT seq, metadata, json_extract(metadata, '$.provider_message_id') AS response"
        " FROM messages WHERE thread_id=? AND role='assistant'"
        " AND json_valid(metadata) AND json_extract(metadata, '$.turn_id')=?",
        (thread_id, turn_id),
    ).fetchall()
    found = [
        step for step in steps
        if any(declared_id(call) == call_id for call in assistant_calls(dict(step)))
    ]
    if len(found) != 1 or not isinstance(found[0]["seq"], int):
        return None
    response = found[0]["response"]
    if not response:
        return found[0]["seq"]
    return min(
        step["seq"] for step in steps
        if step["response"] == response and isinstance(step["seq"], int)
    )


def _utc(value: str) -> datetime | None:
    """A stored timestamp as naive UTC, or None when it does not parse."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _check_rows(
    kind: str, thread_id: str, turn_id: str, turn_order: int, call_id: str, draft_id: str,
) -> _Stage:
    from infra.database import get_db

    db = get_db()
    try:
        running = db.execute(
            "SELECT 1 FROM active_turns WHERE id=? AND thread_id=? AND turn_order=?"
            " AND status='running'",
            (turn_id, thread_id, turn_order),
        ).fetchone()
        if running is None:
            return _Stage(Refusal(NO_TURN))
        step_seq = _declaring_step_seq(db, thread_id, turn_id, call_id)
        if step_seq is None:
            return _Stage(Refusal(NOT_PERSON))
        human = db.execute(
            _HUMAN_INPUTS,
            {"thread_id": thread_id, "turn_id": turn_id, "step_seq": step_seq},
        ).fetchone()
        if human is None:
            return _Stage(Refusal(NOT_PERSON))
        row = db.execute(
            "SELECT * FROM action_drafts WHERE id=? AND thread_id=? AND kind=?",
            (draft_id, thread_id, kind),
        ).fetchone()
        if row is None:
            return _Stage(Refusal(NO_DRAFT))
        if row["state"] != "open":
            return _Stage(Refusal(ALREADY_DONE))
        return _Stage(draft=dict(row), human_at=human["created_at"])
    finally:
        db.close()


def _read_bytes(relative: str) -> bytes | None:
    """The live text when the document is open in the editor, else the file."""
    from doceditor.live_tools import find_live

    root = config.WORKSPACE.resolve()
    path = (root / relative).resolve()
    if root not in path.parents:
        return None
    entry = find_live(path)
    if entry is not None:
        return entry.live.text.encode("utf-8")
    try:
        return path.read_bytes()
    except OSError:
        return None


async def check_action(kind: str, draft_id: str, revision: str) -> Approved | Refusal:
    """Decide whether this call may send ``draft_id``; run on the event-loop task."""
    thread_id = state._current_thread_id.get(None)
    # Contextvars are not visible in executor threads: read the identity here.
    identity = state._current_turn_identity.get(None)
    turn_id = getattr(identity, "turn_id", None)
    turn_order = getattr(identity, "turn_order", None)
    # The id the Assistant Step declared the call under. Natively and under Codex it is
    # also the invocation id; under Claude Code the invocation id is CORSIA's own.
    call_id = state._current_provider_call_id.get(None) or state._current_tool_invocation_id.get(None)
    if not isinstance(thread_id, str) or not isinstance(turn_id, str) or turn_order is None:
        return Refusal(NO_TURN)
    if not isinstance(call_id, str) or not call_id:
        return Refusal(NOT_PERSON)
    stage = await run_in_db(_check_rows, kind, thread_id, turn_id, turn_order, call_id, draft_id)
    if stage.refusal is not None or stage.draft is None:
        return stage.refusal or Refusal(NO_DRAFT)
    draft = stage.draft
    data = _read_bytes(draft["path"])
    if data is None or document_revision(data) != draft["recorded_revision"]:
        return Refusal(CHANGED_OUTSIDE)
    if revision != draft["recorded_revision"]:
        return Refusal(STALE_REVISION)
    person_at, recorded_at = _utc(stage.human_at or ""), _utc(draft["recorded_at"])
    if person_at is None or recorded_at is None or person_at <= recorded_at:
        return Refusal(NOT_SEEN)
    return Approved(draft, data)
