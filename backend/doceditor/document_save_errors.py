"""Domain refusals shared by document save adapters."""

from typing import Any

MISSING_DOCUMENT_MESSAGE = "The document no longer exists. Reopen or choose another file."


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
        super().__init__("stale", "The document changed outside CORSIA while this write was saving."
                                  " Nothing was replaced; try again.")
