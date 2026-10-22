"""Match the person's new words to the running tasks they may bear on (alignment AL4, part 2).

``arrived`` is the one hook the three owner-words points call. It only
schedules ``hear`` on the DB pool and returns, so it can never raise into or
slow the send, comment or answer. ``hear`` finds the running canonical tasks the
words may bear on (explicit links first, then a text match above ``TEXT_BAR``)
and appends one ``INTENT_INPUT`` wake per task owner through ``plan_wake``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from loguru import logger

from plans import intent_words, node_states, service

#: The (shared, title_shared) counts a text match must reach: stricter than Context & Memory's
#: floor, which only says "plausibly related". The bar is live but has not been calibrated on
#: real traffic yet.
TEXT_BAR = (4, 2)
MAX_WAKES = 3
WORDS_MAX_BYTES = 1200
_CACHE_MAX = 2048
_terms: dict[tuple[str, int, int], object] = {}
_tasks: set[asyncio.Task] = set()
_RUNNING = ("running", "review")


@dataclass(frozen=True)
class Source:
    """One arrival of the person's words: what they said, when, to whom, and the nodes it names."""

    kind: str
    id: str
    words: str
    said_at: str
    thread_id: str
    project_id: str | None
    links: tuple[str, ...] = ()


def arrived(source_kind: str, source_id: str) -> None:
    """Schedule matching off the caller's path; never raises, never runs inline."""
    work = _off_path(source_kind, source_id)
    try:
        task = asyncio.get_running_loop().create_task(work)
    except Exception:
        work.close()
        logger.warning("plan input matching was not scheduled for {}", source_kind)
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _off_path(source_kind: str, source_id: str) -> None:
    from infra.db_async import run_in_db

    try:
        await run_in_db(hear, source_kind, source_id)
    except Exception:
        logger.exception("plan input matching failed")


def hear(source_kind: str, source_id: str) -> list[str]:
    """Wake the owners of the running tasks these words bear on; return the wake ids."""
    from infra.database import write_transaction

    with write_transaction(None, scope="person_words_arrived") as db:
        source = load(db, source_kind, source_id)
        if source is None or not source.words.strip():
            return []
        return _wake(db, source, _picked(db, source))


def load(db, kind: str, source_id: str) -> Source | None:
    if kind == "message":
        row = db.execute(
            """SELECT m.content,m.thread_id,m.created_at,t.project_id FROM messages m
               JOIN threads t ON t.id=m.thread_id WHERE m.id=?""", (source_id,),
        ).fetchone()
        return None if row is None else Source(
            kind, source_id, str(row["content"]), str(row["created_at"]), str(row["thread_id"]), row["project_id"],
        )
    if kind == "doc_comment":
        from jobs.corrections_scanning import source_candidate

        found = source_candidate(db, kind, source_id)
        if found is None:
            return None
        links = db.execute(
            """SELECT DISTINCT l.node_id FROM doc_comments c
               JOIN plan_node_artifact_links l ON l.artifact_id=c.artifact_id AND l.role='attention'
               WHERE c.id=? ORDER BY l.created_at,l.node_id""", (source_id,),
        ).fetchall()
        return Source(kind, source_id, found.quote or "", found.created_at, found.thread_id, found.project_id,
                      tuple(str(row["node_id"]) for row in links))
    if kind in ("decision_answer", "plan_decision_answer"):  # the correction hook's name for it, and the words table's
        return _decision(db, source_id)
    return None


def _decision(db, decision_id: str) -> Source | None:
    import json

    from core.corrections_recording import decision_answer_source_id
    from plans.decision_fields import answer_note
    from plans.decision_projection import identity_row

    row = identity_row(db, decision_id)
    body = json.loads(row["metadata"]).get("plan_decision") if row is not None else None
    if not isinstance(body, dict) or body.get("state") != "answered":
        return None
    thread = db.execute("SELECT project_id FROM threads WHERE id=?", (body.get("thread_id"),)).fetchone()
    if thread is None:
        return None
    node = body.get("source_node_id")
    return Source(
        "decision_answer", decision_answer_source_id(decision_id, body.get("revision")),
        answer_note(body.get("answer")) or "", str(body.get("answered_at") or ""), str(body["thread_id"]),
        thread["project_id"], (str(node),) if node else (),
    )


def _picked(db, source: Source) -> list[tuple]:
    """Running canonical (node, plan) pairs in wake order: explicit links, then text matches."""
    ordered: dict[str, tuple] = {}
    for node_id in source.links:
        try:
            node = intent_words.canonical(db, node_id)
        except service.PlanError:
            continue
        ordered.setdefault(str(node["id"]), node)
    explicit = list(ordered.values())
    candidates = [node for node in text_candidates(db, source.project_id) if str(node["id"]) not in ordered]
    cleared = [
        (hit, node) for node, hit in scored(db, candidates, source.words) if clears(hit, TEXT_BAR)
    ]
    cleared.sort(key=lambda pair: (-pair[0].shared, -pair[0].title_shared, str(pair[1]["id"])))
    return running(db, explicit + [node for _hit, node in cleared])


def text_candidates(db, project_id: str | None) -> list:
    """Every started canonical task in the project's plans (running is decided after).

    Who may hear the words is decided per owner in ``_wake``.
    """
    return db.execute(
        f"""SELECT n.* FROM plan_nodes n JOIN plans p ON p.id=n.plan_id
            LEFT JOIN threads t ON p.owner_kind='thread' AND t.id=p.owner_id
            WHERE n.kind='task' AND n.intent_node_id IS NULL
              AND n.state IN ('active','review_requested') AND {node_states.in_work_sql('n')}
              AND p.state='active'
              AND (CASE p.owner_kind WHEN 'thread' THEN t.project_id
                                     WHEN 'project_scope' THEN p.owner_id END) IS ?
            ORDER BY n.id""", (project_id,),
    ).fetchall()


def scored(db, nodes, text: str) -> list[tuple]:
    """(node, Match) for each node the words reach Context & Memory's floor on."""
    from memory import task_match

    words = _quotes(db, [str(node["id"]) for node in nodes])
    found = []
    for node in nodes:
        quotes = words.get(str(node["id"]), "")
        key = (str(node["id"]), int(node["requirements_revision"]), hash(quotes))
        terms = _terms.get(key)
        if terms is None:
            if len(_terms) >= _CACHE_MAX:
                _terms.clear()
            terms = _terms[key] = task_match.task_terms(task_match.NodeText(
                str(node["title"]), str(node["intent"] or ""), str(node["acceptance_criteria"]), quotes,
            ))
        hit = task_match.match(terms, text)
        if hit is not None:
            found.append((node, hit))
    return found


def clears(hit, bar: tuple[int, int]) -> bool:
    return hit.shared >= bar[0] and hit.title_shared >= bar[1]


def _quotes(db, node_ids: list[str]) -> dict[str, str]:
    import json

    rows = db.execute(
        """SELECT node_id,quote FROM plan_node_intent_words
           WHERE removed_at IS NULL AND node_id IN (SELECT value FROM json_each(?))
           ORDER BY node_id,added_at,rowid""", (json.dumps(node_ids),),
    ).fetchall()
    quotes: dict[str, list[str]] = {}
    for row in rows:
        quotes.setdefault(str(row["node_id"]), []).append(str(row["quote"]))
    return {node_id: "\n".join(parts) for node_id, parts in quotes.items()}


def running(db, nodes) -> list[tuple]:
    """Keep the tasks that are selected, delegated or awaiting review, with their plans."""
    from plans import admission, delegation

    plans: dict[str, object] = {}
    pairs = []
    for node in nodes:
        if node_states.retired(node) or node["state"] not in ("active", "review_requested"):
            continue
        plan = plans.get(str(node["plan_id"]))
        if plan is None:
            plan = plans[str(node["plan_id"])] = service._plan_row(db, node["plan_id"])
        if plan["state"] == "active":
            pairs.append((node, plan))
    projected = admission.project_many(pairs, db=db) if pairs else {}
    return [
        (node, plan) for node, plan in pairs
        if projected[str(node["id"])]["condition"] in _RUNNING
        or (node["state"] == "active" and delegation._valid_structured_epoch(node, db) is not None)
    ]


def _wake(db, source: Source, picked: list[tuple]) -> list[str]:
    from memory.node_start_recall import source_reaches_thread
    from plans import admission_projection, plan_wake

    woken: list[str] = []
    for node, plan in picked:
        recipient = admission_projection.executor_of(node, plan)
        if recipient is None or recipient == source.thread_id:
            continue
        # The person's words reach an owner only by the scope rule Context & Memory applies to the same words.
        if not source_reaches_thread(db, source.thread_id, recipient):
            continue
        wake = plan_wake.append(
            db, recipient=recipient, kind=plan_wake.INTENT_INPUT, subject_id=str(node["id"]),
            revision=source.kind, about=source.id, plan_id=str(node["plan_id"]),
            open_node_id=str(node["id"]), content=_content(db, node, source, recipient),
        )
        if wake is not None:
            woken.append(wake)
        if len(woken) == MAX_WAKES:
            break
    return woken


def _content(db, node, source: Source, recipient: str) -> str:
    from plans import plan_wake
    from plans.close_next_bounds import bounded
    from prompts import plan_wakes as wakes

    title = bounded(str(node["title"]), plan_wake.RETURNED_TITLE_MAX_BYTES)
    revision = int(node["requirements_revision"])
    return plan_wake.with_next(
        db, service.ensure_plan("thread", recipient, db=db).id, recipient,
        wakes.WAKE_INTENT_INPUT.format(title=title, requirements_revision=revision),
        intent_input={
            "node_id": str(node["id"]), "requirements_revision": revision, "title": title,
            "source_kind": source.kind, "said_at": source.said_at,
            "words": intent_words.bounded_text(source.words, WORDS_MAX_BYTES),
        },
    )
