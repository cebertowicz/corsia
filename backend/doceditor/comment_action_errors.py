"""Stable document-comment action errors shared across adapters."""

from __future__ import annotations


class CommentActionError(ValueError):
    """A stable, adapter-mapped comment action failure."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class CommentSelectionChanged(ValueError):
    """A browser selection no longer matches the document; its draft is kept.

    ``kind`` is the machine-readable reason the browser acts on, as with ``CommentActionError``.
    """

    def __init__(self, message: str, kind: str = 'selection_changed'):
        super().__init__(message)
        self.kind = kind


def diagram_changed() -> CommentSelectionChanged:
    """The diagram a pin was drawn on is not the one the document holds now."""
    return CommentSelectionChanged('Place the pin on the current diagram. Your draft is kept.', 'diagram_changed')


def passage_gone() -> CommentSelectionChanged:
    """A comment sent again from a kept draft whose passage is no longer in the document with both its sides."""
    return CommentSelectionChanged(
        'The text this comment was on changed before it sent, so it isn’t placed. Your draft is kept.',
        'passage_gone',
    )


def identity_settling() -> CommentSelectionChanged:
    """The comment's id was adopted by the live document and is not stored yet: the same post goes through on a later try."""
    return CommentSelectionChanged('Comment identity is still settling; try again.', 'identity_settling')


# The 409 ``kind`` of a client_id that names another document's or author's comment: a later try goes under a new id.
CLIENT_ID_CLASH_KIND = 'client_id_clash'


def not_active_message(artifact_id: str) -> str:
    """The sentence every caller gives an artifact_id that no longer names an active document."""
    return f"artifact_id does not resolve to an active artifact: {artifact_id}"


def decision_only_error(*, decision: str | None, all_from_author: bool, reason: str | None) -> str | None:
    """Name a decision-only field sent without a decision, so no adapter drops it silently."""
    if decision is None and (all_from_author or reason is not None):
        return f"{'all_from_author' if all_from_author else 'reason'} requires decision"
    return None
