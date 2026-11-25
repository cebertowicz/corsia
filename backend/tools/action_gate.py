"""The code check in front of every outbound send: the draft still holds the text it was saved with.

``check_action`` passes only when the draft is the thread's own open draft and
the document still holds the bytes the draft tools recorded and the call names.
"""

from __future__ import annotations

from dataclasses import dataclass

from doceditor.document_scope import revision as document_revision
from infra import config, state
from infra.db_async import run_in_db

NO_DRAFT = "Not sent: there is no open draft with that id in this conversation."
ALREADY_DONE = "Not sent: that draft was already sent or discarded."
DRAFT_TOOL_BY_KIND = {"email": "email_draft", "invite": "calendar_draft_invite", "message": "messages_draft"}


def _changed_outside(kind: str, draft_tool: str | None = None) -> str:
    tool = draft_tool or DRAFT_TOOL_BY_KIND[kind]
    return (
        "Not sent: the draft was changed outside the draft tools, maybe by the person. Read it "
        f"again and keep their changes when you save it with {tool}, then "
        "show the person and wait for their reply."
    )


# Keep the public email constant byte-identical for existing callers and tests.
CHANGED_OUTSIDE = _changed_outside("email")
STALE_REVISION = (
    "Not sent: the draft has changed since you read it. Read it again, show the person, "
    "and wait for their reply."
)

@dataclass(frozen=True)
class Approved:
    """The draft that may be sent, with the exact bytes the gate checked."""

    draft: dict
    data: bytes


@dataclass(frozen=True)
class Refusal:
    """The tool's error text; nothing was sent."""

    text: str


def _open_draft(kind: str, thread_id: str, draft_id: str) -> dict | Refusal:
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute(
            "SELECT * FROM action_drafts WHERE id=? AND thread_id=? AND kind=?",
            (draft_id, thread_id, kind),
        ).fetchone()
        if row is None:
            return Refusal(NO_DRAFT)
        if row["state"] != "open":
            return Refusal(ALREADY_DONE)
        return dict(row)
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


async def check_action(
    kind: str, draft_id: str, revision: str, *, draft_tool: str | None = None,
) -> Approved | Refusal:
    """Decide whether this call may send ``draft_id``; run on the event-loop task.

    ``draft_tool`` names the tool that saves this draft when it is not the kind's default.
    """
    thread_id = state._current_thread_id.get(None)
    if not isinstance(thread_id, str):
        return Refusal(NO_DRAFT)
    draft = await run_in_db(_open_draft, kind, thread_id, draft_id)
    if isinstance(draft, Refusal):
        return draft
    data = _read_bytes(draft["path"])
    if data is None or document_revision(data) != draft["recorded_revision"]:
        return Refusal(_changed_outside(kind, draft_tool))
    if revision != draft["recorded_revision"]:
        return Refusal(STALE_REVISION)
    return Approved(draft, data)
