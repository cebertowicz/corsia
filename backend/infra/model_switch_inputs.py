"""Unwritten FIFO transfer within the existing model-switch transaction."""

import sqlite3

from loguru import logger

from infra.thread_input_rows import starts_turn


def log_switch_choice(thread_id: str, choice: str) -> None:
    """Log a switch's checkpoint writer once, where it is chosen: source, target or forced."""
    logger.info("model switch checkpoint choice for {}: {}", thread_id, choice)


def _wholly_unstarted(item: sqlite3.Row) -> bool:
    return not (
        item["delivery_state"] != "queued"
        or item["seq"] is not None
        or item["active_turn_id"] is not None
        or item["write_started_at"] is not None
        or item["write_outcome"] not in {None, "proved_no_write"}
        or (item["delivery_attempt_id"] is not None
            and item["write_outcome"] != "proved_no_write")
    )


def rides_switch(item: sqlite3.Row) -> bool:
    """A wholly unstarted passive input is context, never source work to drain.

    It waits for activity rather than starting a Turn, so a switch must not wait
    on it; it rides the switch to the destination at its FIFO position instead.
    """
    return not starts_turn(item) and _wholly_unstarted(item)


def carry_passive_source_inputs(
    db: sqlite3.Connection, row: sqlite3.Row,
) -> tuple[str, ...]:
    """Move queued passive source inputs to the destination being activated."""
    if not db.in_transaction:
        raise RuntimeError("passive switch carry requires its writer transaction")
    carried = db.execute(
        "SELECT id,role,metadata,seq,active_turn_id,delivery_state,write_started_at,"
        "write_outcome,delivery_attempt_id FROM messages WHERE thread_id=? "
        "AND target_execution_epoch=? AND delivery_state='queued' "
        "AND active_turn_id IS NULL ORDER BY input_order",
        (str(row["id"]), int(row["execution_epoch"])),
    ).fetchall()
    moved = tuple(str(item["id"]) for item in carried if rides_switch(item))
    for message_id in moved:
        db.execute(
            "UPDATE messages SET target_model=?,target_execution_epoch=?,"
            "delivery_revision=delivery_revision+1,write_outcome=NULL,"
            "delivery_attempt_id=NULL,delivery_error=NULL,next_attempt_at=NULL "
            "WHERE id=?",
            (row["pending_model"], row["pending_execution_epoch"], message_id),
        )
    return moved


def adopt_first_model(
    db: sqlite3.Connection, thread_id: str, model: str, *, now: str,
) -> tuple[str, ...]:
    """Give a model-less thread its first model and the inputs it held.

    There is no source epoch to drain, so the thread takes ``model`` in its
    current epoch, and each wholly unstarted queued input that targeted no
    model takes it too, at its FIFO position.
    """
    if not db.in_transaction:
        raise RuntimeError("first model selection requires its writer transaction")
    from infra.local_only import require_local_only_model

    thread = db.execute(
        "SELECT thread.project_id,COALESCE(root.local_only,thread.local_only) AS local_only "
        "FROM threads AS thread LEFT JOIN threads AS root "
        "ON root.id=thread.local_only_root_thread_id WHERE thread.id=?", (thread_id,)
    ).fetchone()
    if thread is None:
        raise ValueError("first model selection thread does not exist")
    require_local_only_model(
        model,
        thread_local_only=bool(thread["local_only"]),
        project_id=thread["project_id"] or None,
        db=db,
    )
    db.execute(
        "UPDATE threads SET model=?,updated_at=? WHERE id=?", (model, now, thread_id),
    )
    held = db.execute(
        "SELECT id,seq,active_turn_id,delivery_state,write_started_at,write_outcome,"
        "delivery_attempt_id FROM messages WHERE thread_id=? "
        "AND delivery_state='queued' AND COALESCE(TRIM(target_model),'')='' "
        "ORDER BY input_order", (thread_id,),
    ).fetchall()
    adopted = tuple(str(item["id"]) for item in held if _wholly_unstarted(item))
    for message_id in adopted:
        db.execute(
            "UPDATE messages SET target_model=?,delivery_revision=delivery_revision+1,"
            "write_outcome=NULL,delivery_attempt_id=NULL,delivery_error=NULL,"
            "next_attempt_at=NULL WHERE id=?",
            (model, message_id),
        )
    return adopted


def retarget_unwritten_suffix(db: sqlite3.Connection, row: sqlite3.Row) -> tuple[sqlite3.Row, tuple[str, ...]]:
    """Move only a wholly unstarted held suffix; never replay uncertain writes."""
    if not db.in_transaction:
        raise RuntimeError("unwritten switch transfer requires its writer transaction")
    if row["switch_status"] != "preparing":
        return row, ()
    thread_id, epoch = str(row["id"]), int(row["execution_epoch"])
    if db.execute(
        "SELECT 1 FROM active_turns WHERE thread_id=? "
        "AND status='running' LIMIT 1", (thread_id,),
    ).fetchone() is not None:
        return row, ()
    if db.execute(
        "SELECT 1 FROM messages WHERE thread_id=? AND delivery_state='queued' "
        "AND (active_turn_id IS NOT NULL OR (write_started_at IS NOT NULL "
        "AND write_outcome IS NULL)) LIMIT 1", (thread_id,),
    ).fetchone() is not None:
        return row, ()
    head = db.execute(
        "SELECT input_order,write_outcome FROM messages WHERE thread_id=? "
        "AND target_execution_epoch=? AND delivery_state='queued' "
        "AND input_order IS NOT NULL ORDER BY input_order LIMIT 1", (thread_id, epoch),
    ).fetchone()
    if head is None:
        return row, ()
    if head["write_outcome"] != "proved_no_write":
        from infra.held_admission import limited_admission

        # The thread's own account decides whether its head is held by a limit.
        if head["write_outcome"] is not None or limited_admission(thread_id, str(row["model"]), db=db) is None:
            return row, ()
    suffix = db.execute(
        "SELECT id,seq,active_turn_id,delivery_state,write_started_at,write_outcome,"
        "delivery_attempt_id,target_model FROM messages WHERE thread_id=? "
        "AND target_execution_epoch=? AND input_order>=? AND delivery_state!='cancelled' "
        "ORDER BY input_order", (thread_id, epoch, head["input_order"]),
    ).fetchall()
    if any(
        item["delivery_state"] != "queued" or item["target_model"] != row["model"]
        or item["seq"] is not None or item["active_turn_id"] is not None
        or item["write_started_at"] is not None
        or item["write_outcome"] not in {None, "proved_no_write"}
        or (item["delivery_attempt_id"] is not None and item["write_outcome"] != "proved_no_write")
        for item in suffix
    ):
        return row, ()
    for item in suffix:
        db.execute(
            "UPDATE messages SET target_model=?,target_execution_epoch=?,"
            "delivery_revision=delivery_revision+1,write_outcome=NULL,"
            "delivery_attempt_id=NULL,delivery_error=NULL,next_attempt_at=NULL WHERE id=?",
            (row["pending_model"], row["pending_execution_epoch"], item["id"]),
        )
    db.execute(
        "UPDATE threads SET switch_after_input_order=?,switch_checkpoint_choice='target' "
        "WHERE id=?", (int(head["input_order"]) - 1, thread_id),
    )
    if row["switch_checkpoint_choice"] != "target":
        log_switch_choice(thread_id, "forced")
    return (db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone(),
            tuple(str(item["id"]) for item in suffix))


def return_unstarted_suffix_to_source(
    db: sqlite3.Connection, row: sqlite3.Row,
) -> tuple[str, ...]:
    """Undo what a failed switch never wrote, in the failing writer's transaction.

    A preparing transfer may have moved a suffix to the destination epoch, and
    inputs accepted after the request were routed straight there. Once the
    switch fails, that destination will never write any of it, so each row
    still exactly as unstarted as it arrived returns to the source epoch and
    model. ``input_order`` never changes: this is the same FIFO position, not a
    new one.

    The return stops at the first row that is not wholly unstarted. That row
    may already own a provider outcome, so it stays quarantined -- and nothing
    behind it may overtake it on the way back to the source.
    """
    if not db.in_transaction:
        raise RuntimeError("failed switch suffix return requires its writer transaction")
    pending_epoch = row["pending_execution_epoch"]
    if pending_epoch is None:
        return ()
    stranded = db.execute(
        "SELECT id,seq,active_turn_id,delivery_state,write_started_at,write_outcome,"
        "delivery_attempt_id FROM messages WHERE thread_id=? "
        "AND target_execution_epoch=? AND delivery_state!='cancelled' "
        "ORDER BY input_order", (str(row["id"]), int(pending_epoch)),
    ).fetchall()
    returned: list[str] = []
    for item in stranded:
        if not _wholly_unstarted(item):
            break
        db.execute(
            "UPDATE messages SET target_model=?,target_execution_epoch=?,"
            "delivery_revision=delivery_revision+1,write_outcome=NULL,"
            "delivery_attempt_id=NULL,delivery_error=NULL,next_attempt_at=NULL "
            "WHERE id=?",
            (str(row["model"]), int(row["execution_epoch"]), item["id"]),
        )
        returned.append(str(item["id"]))
    return tuple(returned)
