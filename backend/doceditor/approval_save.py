"""Prepare approval copies and retry a save when approval arrives during it."""

import hashlib

from doceditor.document_save_errors import DocumentSaveError
from infra.database import get_db


def write_with_approval(body, resolved, direct_user, expected_sha256, reports, suppress_feedback, accepted, write):
    from plans import public as plans_public
    from plans.approval_records import ApprovalChangedDuringSave

    plain_state: dict = {}
    for attempt in range(2):
        db = get_db()
        try:
            prepared = plans_public.prepare_save_approval(resolved, body.text.encode(), body.project_id, db=db) if direct_user else None
        finally:
            db.close()
        wrote = False

        def approval_hook(connection, artifact):
            nonlocal wrote
            wrote = plans_public.record_saved_version(connection, artifact["id"], prepared, accepted, retry=attempt > 0)

        try:
            result = write(body, resolved, direct_user, expected_sha256, reports,
                           suppress_feedback, approval_hook if direct_user else None, plain_state)
        except ApprovalChangedDuringSave as exc:
            if prepared is not None:
                prepared.discard()
            if attempt:
                raise DocumentSaveError("approval_changed", str(exc)) from exc
            if plain_state.get("published"):
                expected_sha256 = hashlib.sha256(body.text.encode()).hexdigest()
            continue
        except BaseException:
            if prepared is not None:
                prepared.discard()
            raise
        if prepared is not None and not wrote:
            prepared.discard()
        return result
    raise AssertionError("unreachable")
