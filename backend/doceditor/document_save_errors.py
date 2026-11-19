"""Domain refusals shared by document save adapters."""

from typing import Any

MISSING_DOCUMENT_MESSAGE = "This document is deleted, so nothing changed."
MISSING_SAVE_MESSAGE = "Couldn’t save: this document is deleted. Your text stays here until you close it."

#: The editor PUT's own stale refusal (issue #3049). The writer's ``stale`` is worded for
#: suggestion batches and live writes and invites a retry, which is wrong for typed text
#: whose base is gone: the tab keeps the text and the person decides. ``PUT /api/files/content``
#: answers with this code and message; the tab keys its one-refusal-per-base rule on the code.
EDITOR_STALE_CODE = "stale_base"
EDITOR_STALE_MESSAGE = "Couldn’t save: this document changed after you started editing. Your text stays here."


class DocumentSaveError(ValueError):
    """A domain refusal mapped by each transport adapter."""

    def __init__(self, kind: str, message: str, detail: Any = None) -> None:
        self.kind = kind
        self.message = message
        self.detail = detail
        super().__init__(message)


class FileChangedOutside(DocumentSaveError):
    """The file no longer holds the bytes a live write was built on; nothing was replaced."""

    def __init__(self) -> None:
        super().__init__("stale", "This document changed just as it was being saved, so that save didn’t go through."
                                  " Nothing was lost; try again.")
