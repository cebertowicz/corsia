"""``POST /api/import`` — bring an earlier CORSIA install's content over (issue #2125).

The running backend does the writes, so the projects list and every index row
update the way they do for any other change. Importing writes guidance that
agents then load, so this stays a person's action: a signed-in session or the
``corsia import`` shell's own short-lived service token is accepted, and an
agent's ``api`` token is refused, the way ``api/key_session_guard`` checks it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.project_events import notify_projects_changed
from infra.auth import require_user_header_only
from infra.database import get_db
from infra.db_async import run_off_db

router = APIRouter(prefix="/api/import", tags=["import"])
log = logging.getLogger("corsia.earlier_install")

#: The shell's short-lived service token is the person at their own terminal; an agent's is not.
PERSON_KINDS = ("session", "service")
PERSON_ONLY = "Run `corsia import` yourself, or bring it over signed in to CORSIA."
OWN_WORKSPACE = "That is this CORSIA's own workspace, not an earlier one."


class EarlierInstall(BaseModel):
    """The folder to read, and whether this call may write."""

    path: str
    apply: bool = False


def require_person_for_import(user: dict) -> None:
    """Refuse an import unless the caller is the person or their own shell."""
    kind = str(user.get("token_kind") or "unknown")
    if kind not in PERSON_KINDS:
        log.warning("import refused: token_kind=%s", kind)
        raise HTTPException(403, PERSON_ONLY)


def _source(path: str, workspace: Path) -> Path:
    """The folder to read, refused when it is this install's own workspace."""
    candidate = Path(path.strip()).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(400, "Name the earlier workspace folder by its full path.")
    resolved = candidate.resolve(strict=False)
    if resolved == workspace or resolved.is_relative_to(workspace) or workspace.is_relative_to(resolved):
        raise HTTPException(400, OWN_WORKSPACE)
    return candidate


@router.post("")
async def import_earlier_install(
    body: EarlierInstall, user: dict = Depends(require_user_header_only),
) -> dict:
    """Report what an earlier install offers, and bring it over when ``apply`` is set."""
    from infra import config
    from onboarding.earlier_install import Unreadable, scan
    from onboarding.earlier_install_apply import run

    require_person_for_import(user)
    workspace = Path(config.WORKSPACE).resolve(strict=False)
    source = _source(body.path, workspace)

    def _job() -> dict:
        plan = scan(source)
        db = get_db()
        try:
            return run(plan, workspace=workspace, db=db, user_id=user["id"], apply=body.apply)
        finally:
            db.close()

    try:
        report = await run_off_db(_job)
    except Unreadable as error:
        raise HTTPException(400, str(error)) from error
    except OSError as error:
        raise HTTPException(400, f"Could not read that folder: {error.strerror or error}") from error
    if any(project["state"] == "added" for project in report["projects"]):
        notify_projects_changed(user["id"])
    return report
