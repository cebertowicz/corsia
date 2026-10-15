"""The code gate in front of every outbound send: the person asked, and saw this text.

``check_action`` passes only when the call runs inside a live turn that the
person started or wrote into, the draft is the thread's own open draft, the
document still holds the bytes the draft tools recorded and the call names,
and the person's newest message in this turn is later than that recording.
The model's judgement is not an input.
"""

from __future__ import annotations

from dataclasses import dataclass

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

# Messages claimed by one turn that a person wrote. Admission stamps
# ``reply_via_post_update`` only on a row with none of the other markers; the
# three IS NULL lines keep a row stamped before a marker existed from passing.
_HUMAN_INPUTS = """
    SELECT created_at FROM messages
    WHERE thread_id = :thread_id AND input_order IS NOT NULL
      AND role = 'user'
      AND active_turn_id = :turn_id
      AND json_valid(metadata)
      AND json_extract(metadata, '$.reply_via_post_update') = 1
      AND json_extract(metadata, '$.sender_kind') IS NULL
      AND json_extract(metadata, '$.completion') IS NULL
      AND json_extract(metadata, '$.routed_event') IS NULL
    ORDER BY input_order DESC
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


def _check_rows(
    kind: str, thread_id: str, turn_id: str, turn_order: int, draft_id: str,
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
        human = db.execute(
            _HUMAN_INPUTS, {"thread_id": thread_id, "turn_id": turn_id},
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
    if not isinstance(thread_id, str) or not isinstance(turn_id, str) or turn_order is None:
        return Refusal(NO_TURN)
    stage = await run_in_db(_check_rows, kind, thread_id, turn_id, turn_order, draft_id)
    if stage.refusal is not None or stage.draft is None:
        return stage.refusal or Refusal(NO_DRAFT)
    draft = stage.draft
    data = _read_bytes(draft["path"])
    if data is None or document_revision(data) != draft["recorded_revision"]:
        return Refusal(CHANGED_OUTSIDE)
    if revision != draft["recorded_revision"]:
        return Refusal(STALE_REVISION)
    if stage.human_at is None or stage.human_at <= draft["recorded_at"]:
        return Refusal(NOT_SEEN)
    return Approved(draft, data)
