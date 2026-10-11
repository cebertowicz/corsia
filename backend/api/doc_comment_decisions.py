"""Shared HTTP refusal mapping for document suggestion decisions."""

from fastapi.responses import JSONResponse

from doceditor.comment_actions import CommentActionError


COMMENT_ACTION_HTTP_STATUS = {
    "not_found": 404,
    "not_suggestion": 400,
    "decision": 400,
    "not_author": 403,
    "not_owner": 403,
    "region_shape": 400,
    "region_bounds": 400,
    "region_target": 400,
}


def comment_action_status(exc: CommentActionError) -> int:
    """Return the status shared by every refused document-comment action."""
    return COMMENT_ACTION_HTTP_STATUS.get(exc.kind, 409)


def decision_refusal(exc: CommentActionError) -> JSONResponse:
    """Render a stable JSON refusal for a document-comment action."""
    return JSONResponse({"detail": exc.message, "kind": exc.kind}, status_code=comment_action_status(exc))
