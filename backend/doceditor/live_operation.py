"""Lane operation values and the refusals the lane settles as author outcomes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from doceditor.live_publication_record import WriteActor


def is_ordinary_refusal(error: Exception) -> bool:
    """The named author outcomes: lane, matcher, comment, save-validation and policy refusals.

    Anything else, including a bare ValueError or a filesystem PermissionError,
    is a storage or code failure.
    """
    from doceditor.comment_action_errors import CommentActionError
    from doceditor.document_save import DocumentSaveError
    from registry.shared_write_errors import DurableWritePolicyError
    return isinstance(error, (LaneRefused, CommentActionError, DocumentSaveError, DurableWritePolicyError))


class LaneRefused(ValueError):
    pass


class EditRefused(LaneRefused):
    """A canonical matcher receipt to return unchanged to the tool caller."""


class FileUnreadable(OSError):
    """The document file's bytes on disk are not UTF-8 text: a storage failure."""


@dataclass(frozen=True)
class TextEdit:
    old: str
    new: str
    replace_all: bool = False


@dataclass(frozen=True)
class Operation:
    kind: Literal["write", "edit", "flush", "replace", "accept"]
    content: str | None = None
    base_hash: str | None = None
    edits: tuple[TextEdit, ...] = ()
    actor: WriteActor | None = None
    suggestions: tuple[str, ...] = ()
    single_suggestion: bool = False
    multi_edit: bool = False
    edit_path: str | None = None
    op_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True)
class WriteResult:
    text: str
    server_saved: bool
    merged: bool = False
    accepted: tuple[str, ...] = ()
    skipped: tuple[dict[str, str], ...] = ()
    receipt: str | None = None
    outcome: Literal["landed", "refused", "not_started", "pending"] = "landed"
    refusal: Exception | None = None

    def require_landed(self) -> WriteResult:
        """Transport boundaries retain their canonical refusal receipts."""
        if self.refusal is not None:
            raise self.refusal
        if self.outcome != "landed":
            raise LaneRefused(f"Document write {self.outcome}")
        return self
