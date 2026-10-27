"""Validated transport models for document-comment requests."""
from pydantic import BaseModel, StrictBool, field_validator

from doceditor import comments as store
from doceditor.feedback_batch_core import PersonDocumentLane


class CommentCreate(BaseModel):
    body: str
    parent_id: str | None = None
    anchor_text: str | None = None
    from_off: int | None = None
    to_off: int | None = None
    review: bool = False
    replacement: str | None = None
    client_id: str | None = None
    live_capture: StrictBool = False
    # {x, y, w, h} in fractions of an image artifact; w = h = 0 (or omitted) is a point.
    region: dict | None = None
    # Required: the browser sends the person's stored lane with every comment; a missing one is a 422, never a silent lane.
    # Image-region comments always become image-held server-side; callers may only choose the document lane.
    delivery_policy: PersonDocumentLane

    @field_validator("client_id")
    @classmethod
    def _validate_client_id(cls, v: str | None) -> str | None:
        """Delegate to the shared validator so the user endpoint and the
        agent-principal twin can't drift on the idempotency-key contract."""
        return store.validate_client_id(v)
