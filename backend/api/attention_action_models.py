"""Request bodies for attention actions."""
from pydantic import BaseModel


class ClearBody(BaseModel):
    token: str
    outcome: str
    condition: dict | None = None
    reason: str | None = None
    verdict: str | None = None
    note: str | None = None
    hold_ids: list[str] | None = None


class DisposeBody(BaseModel):
    token: str
    outcome: str
    reason: str | None = None
    answer: str | None = None
    authority: str | None = None
    override_blocking: bool = False
    new_owner: str | None = None
    condition: dict | None = None
    replacement_id: str | None = None
    actor_thread: str | None = None
    hold_ids: list[str] | None = None
