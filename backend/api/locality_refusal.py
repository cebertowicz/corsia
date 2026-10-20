"""The person's 409 for a Local only refusal, rendered from its typed fields.

Governance & Scopes owns this wording; the agent text is in ``prompts.cloud_off``.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from infra.local_only import LocalityRefused


def locality_refused_response(exc: LocalityRefused, *, turning_on: bool = False) -> JSONResponse:
    """Map a ``LocalityRefused`` to the person-facing 409 every route returns."""
    if not exc.ready_local_selectors:
        message = "No local model is ready. Set up a local model in Settings, then try again."
    elif turning_on:
        message = "Switch this chat to a local model first, then turn on Local only."
    else:
        message = "This chat is Local only, so choose a local model and try again."
    return JSONResponse(status_code=409, content={"code": exc.code, "message": message})
