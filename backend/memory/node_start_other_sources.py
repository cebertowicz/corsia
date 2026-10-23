"""Non-message prior-word source readers used by node-start recall."""

from __future__ import annotations

from core.correction_notes import local_clock
from memory.node_start_recall import _Hit, _matching, _owned_threads, _source_scope
from memory.task_match import first_term_at, match


def candidates(db, terms, user_id, caller_thread_id, caller, cloud_off, cutoff, source_candidates) -> list[_Hit]:
    """Read matching comments, decision answers and user-derived rules."""
    return [
        *_comment_candidates(db, terms, user_id, caller, cloud_off, cutoff, source_candidates),
        *_decision_candidates(db, terms, user_id, caller, cloud_off, cutoff, source_candidates),
        *_rule_candidates(db, terms, user_id, caller_thread_id, caller, cloud_off, cutoff, source_candidates),
    ]


def _comment_candidates(db, terms, user_id, caller, cloud_off, cutoff, source_candidates) -> list[_Hit]:
    from core.correction_targets import comment_target_sql

    source_scope, source_params = _source_scope("t", "c.created_at", caller, cloud_off, cutoff)
    matching, match_params = _matching("c.body", terms)
    rows = db.execute(
        f"""SELECT c.id,c.artifact_id,c.body,c.created_at,ai.title,ai.path
            FROM doc_comments c JOIN threads t ON t.id=({comment_target_sql('c')})
            LEFT JOIN artifact_identities ai ON ai.id=c.artifact_id
            WHERE c.author_kind='user' AND {_owned_threads('t')} {source_scope} {matching}
            ORDER BY c.created_at DESC,c.id DESC LIMIT ?""",
        [user_id, *source_params, *match_params, source_candidates],
    ).fetchall()
    hits = []
    for row in rows:
        found = match(terms, str(row["body"]))
        if found:
            about = row["title"] or row["path"] or row["artifact_id"]
            line = f'{local_clock(str(row["created_at"]))} · comment on “{about}” · “{_excerpt(found)}”'
            hits.append(_Hit(found, str(row["created_at"]), "comment", line))
    return hits


def _decision_candidates(db, terms, user_id, caller, cloud_off, cutoff, source_candidates) -> list[_Hit]:
    from core.corrections_recording import decision_answer_arrived_at_sql
    from plans.public import DECISION_TYPE

    answer = "json_extract(ai.metadata,'$.plan_decision.answer.text')"
    answered_at = decision_answer_arrived_at_sql("ai")
    source_scope, source_params = _source_scope("t", answered_at, caller, cloud_off, cutoff)
    matching, match_params = _matching(answer, terms)
    rows = db.execute(
        f"""SELECT ai.external_id,json_extract(ai.metadata,'$.plan_decision.title') AS title,
                   {answer} AS answer,{answered_at} AS answered_at
            FROM artifact_identities ai
            JOIN threads t ON t.id=json_extract(ai.metadata,'$.plan_decision.thread_id')
            WHERE ai.integration='local' AND ai.type=? AND ai.state='active'
              AND json_extract(ai.metadata,'$.plan_decision.state')='answered'
              AND json_type(ai.metadata,'$.plan_decision.answer.text')='text'
              AND {_owned_threads('t')} {source_scope} {matching}
            ORDER BY answered_at DESC LIMIT ?""",
        [DECISION_TYPE, user_id, *source_params, *match_params, source_candidates],
    ).fetchall()
    hits = []
    for row in rows:
        text, created_at = str(row["answer"]), str(row["answered_at"])
        found = match(terms, text)
        if found:
            line = f'{local_clock(created_at)} · answer to decision “{row["title"] or row["external_id"]}” · “{_excerpt(found)}”'
            hits.append(_Hit(found, created_at, "decision", line))
    return hits


def _rule_candidates(db, terms, user_id, caller_thread_id, caller, cloud_off, cutoff, source_candidates) -> list[_Hit]:
    statement = "json_extract(ce.body,'$.statement')"
    source_scope, source_params = _source_scope("t", "ce.updated_at", caller, cloud_off, cutoff)
    matching, match_params = _matching(statement, terms)
    rows = db.execute(
        f"""WITH top AS (
              SELECT ce.thread_id,ce.history_epoch,ce.entry_id,ce.revision,ce.updated_at,{statement} AS statement
              FROM continuity_entries ce JOIN threads t ON t.id=ce.thread_id
              WHERE {_owned_threads('t')} AND ce.state='active'
                AND ce.field IN (?,?)
                AND EXISTS (SELECT 1 FROM json_each(ce.body,'$.source_refs')
                            WHERE json_extract(value,'$.kind')='user')
                AND NOT (ce.thread_id=? AND ce.history_epoch=t.history_epoch)
                {source_scope} {matching}
              ORDER BY ce.updated_at DESC LIMIT ?)
            SELECT top.*,checkpoints.id AS checkpoint_id
            FROM top LEFT JOIN checkpoint_entry_search entry_index
              ON entry_index.thread_id=top.thread_id AND entry_index.history_epoch=top.history_epoch
             AND entry_index.entry_id=top.entry_id AND entry_index.revision=top.revision
            LEFT JOIN checkpoints ON checkpoints.generation_order=entry_index.last_generation_order
            ORDER BY top.updated_at DESC""",
        [user_id, "active_constraints", "approvals_and_preferences", caller_thread_id,
         *source_params, *match_params, source_candidates],
    ).fetchall()
    hits = []
    for row in rows:
        found = match(terms, str(row["statement"] or ""))
        if found:
            pointer = f' → get_checkpoints(checkpoint_id="{row["checkpoint_id"]}")' if row["checkpoint_id"] else ""
            line = f'{local_clock(str(row["updated_at"]))} · rule an agent recorded from their words (thread {str(row["thread_id"])[:8]}) · “{_excerpt(found)}”{pointer}'
            hits.append(_Hit(found, str(row["updated_at"]), "rule", line))
    return hits



def _excerpt(found) -> str:
    text = " ".join(found.span.split())
    if len(text) <= 280:
        return text
    first = first_term_at(found.matched_terms, text)
    start = max(0, min(first - 120, len(text) - 277))
    end = min(len(text), start + 277)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix
