"""Read a document or image review bar for Activity."""
from doceditor import review_bar
from infra.database import get_db
from plans import public
from registry import artifact_record as AR


def _review_bar(artifact_id: str, user_id: str) -> dict | None:
    """The review bar's read for a document or image, or None for any other type."""
    from doceditor.person_comment_projection import person_step_attention

    db = get_db()
    try:
        row = AR.identity_row_for(artifact_id, db=db)
        if row is None or row['type'] not in public.ASKABLE_TYPES:
            return None
        person_step = public.person_step_instructions(str(row['id']), db=db)
        attention = person_step and person_step_attention(db, person_step['node_id'], str(row['id']), user_id)
        if attention:
            person_step = {**person_step, 'comment_attention': attention}
        return {
            'artifact_id': artifact_id, 'artifact_type': row['type'],
            **review_bar.round_state(db, str(row['id'])),
            **({'person_step': person_step} if person_step is not None else {}),
        }
    finally:
        db.close()
