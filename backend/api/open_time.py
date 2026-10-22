"""Open time for the runs and plan steps a screen shows, in one request.

Each item names a lead thread and a range; the answer is the server's one
number for how long that work has been open (``infra.open_time``). A screen
asks for every visible run at once rather than once per run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, Field

from infra import open_time
from infra.auth import require_user
from infra.database import get_db
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user

router = APIRouter()

MAX_ITEMS = 200


class OpenTimeItem(BaseModel):
    id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    since: AwareDatetime
    until: AwareDatetime | None = None


class OpenTimeBody(BaseModel):
    items: list[OpenTimeItem] = Field(max_length=MAX_ITEMS)


def _read(user_id: str, items: list[OpenTimeItem]) -> dict[str, dict]:
    as_of = datetime.now(UTC)
    db = get_db()
    try:
        by_thread: dict[str, list[OpenTimeItem]] = {}
        for item in items:
            by_thread.setdefault(item.thread_id, []).append(item)
        found = {}
        for thread_id, asked in by_thread.items():
            thread = get_thread_for_user(thread_id, user_id, db=db)
            if thread is None or thread["purged_at"] is not None:
                continue
            answers = open_time.open_times(
                db, thread_id, [(item.since, item.until) for item in asked], as_of=as_of,
            )
            found.update({item.id: answer for item, answer in zip(asked, answers, strict=True)})
        return found
    finally:
        db.close()


@router.post("/open-time")
async def read_open_time(body: OpenTimeBody, user: dict = Depends(require_user)):
    """Open time per item id; an item for a thread that is not the user's is left out."""
    if len({item.id for item in body.items}) != len(body.items):
        raise HTTPException(422, "Item ids must be unique")
    return {"items": await run_in_db(partial(_read, user["id"], body.items))}
