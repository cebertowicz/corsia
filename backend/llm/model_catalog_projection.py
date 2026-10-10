"""Shared read projection for the API and agent model catalog tool."""

from __future__ import annotations


def model_catalog_projection(active_threads: list[dict], *, thread_id: str | None = None,
                             project_id: str | None = None, account_id: str | None = None) -> dict:
    """Return the exact live model catalog payload exposed by ``GET /api/models``.

    Rows carry what each model is; a harness row's ``runs_on`` says what each
    permitted account can do with it, whole: status, reason, reset, usage and
    usage check. Its ``runnable`` says whether the model is available: a ready
    permitted sibling covers the selected account's ``usage_limited`` status.
    The selected ``runs_on`` entry still says which account the next turn uses;
    with rollover off, that turn stays held on its selected limited account.
    Without a thread, project or account the scope is the workspace.
    """
    from llm.capabilities import get_active_local_threads
    from llm.model_status import get_model_status_rows, scoped_status_rows

    scoped = thread_id is not None or project_id is not None or account_id is not None
    rows = (scoped_status_rows(thread_id=thread_id, project_id=project_id, account_id=account_id)
            if scoped else get_model_status_rows())
    return {
        "models": rows,
        "active_local_threads": len(get_active_local_threads(active_threads)),
    }
