"""Atomic publication side effects for installed checkpoints."""

from __future__ import annotations

import json

from loguru import logger as log


RESUME_PRESENCE_LINE = "resume_presence_line"


class _Missing:
    pass


_MISSING = _Missing()


def completion_receipt_id(checkpoint_id: str) -> str:
    """Return the transcript row owned by one installed checkpoint."""
    return f"checkpoint-receipt:{checkpoint_id}"


def _retire_source_continuation(record, selected_model: str, db) -> None:
    from core.checkpoint_types import SOURCE_RETIRING_TRIGGERS

    continuation = {
        "claude-code": ("claude-code", "session"),
        "codex": ("codex", "thread"),
    }.get(record.source_harness)
    if (
        continuation is None
        or record.trigger not in SOURCE_RETIRING_TRIGGERS
        or selected_model != record.source_model
    ):
        return
    from registry.models import find_for_thread, retire

    integration, artifact_type = continuation
    registration = find_for_thread(
        record.thread_id, integration, artifact_type, db=db,
    )
    if registration is not None:
        retire(registration["id"], db=db)


def publish_install(
    record, selected_model: str, installed_at: str, db, *,
    presence_line: str | None | _Missing = _MISSING,
) -> None:
    """Retire one source handle and record the install-time presence snapshot."""
    _retire_source_continuation(record, selected_model, db)
    if presence_line is _MISSING:
        record_install_receipt(record, installed_at, db)
    else:
        record_install_receipt(record, installed_at, db, presence_line=presence_line)


def record_install_receipt(
    record, installed_at: str, db, *,
    presence_line: str | None | _Missing = _MISSING,
) -> None:
    """Record one quiet activity receipt at the checkpoint boundary."""
    from core.agent_context_sources import current_responsibility_map_snapshot
    from core.installed_context import with_installed_snapshot
    from infra.queries import insert_message

    switch = None
    if record.trigger == "model_switch":
        switch = db.execute(
            "SELECT pending_model FROM threads WHERE id=?", (record.thread_id,),
        ).fetchone()
    map_model = (
        str(switch["pending_model"])
        if switch is not None and switch["pending_model"] else record.writer_model
    )
    # The map alone: the first dispatch after this install freezes the sources
    # it delivers, so no source read runs inside the install transaction.
    installed = current_responsibility_map_snapshot(
        record.thread_id, db, model=map_model,
    )
    if record.trigger == "model_switch":
        # The destination commits its runtime epoch later; its first dispatch binds it.
        installed["runtime_context_epoch"] = None
    insert_message(
        record.thread_id,
        "system",
        "Context saved",
        with_installed_snapshot({
            # Frozen beside the map: a later Turn replays these exact bytes.
            RESUME_PRESENCE_LINE: (
                _presence_line_at(db, record.thread_id, record.id, installed_at)
                if presence_line is _MISSING else presence_line
            ),
            "type": "activity_receipt",
            "event_type": "checkpoint.installed",
            "receipt_type": "checkpoint",
            "checkpoint_id": record.id,
            "checkpoint_mode": record.mode,
            "checkpoint_trigger": record.trigger,
            "through_seq": record.through_seq,
            "summary": "Context saved",
        }, installed),
        msg_id=completion_receipt_id(record.id),
        db=db,
        created_at=installed_at,
    )


def _presence_line_at(
    db, thread_id: str, checkpoint_id: str, installed_at: str,
) -> str | None:
    """The presence line as of the install; a malformed timestamp freezes none.

    An install must publish even when a stored time cannot be read, and an
    absent line renders nothing extra.
    """
    from infra import presence
    from infra.presence_view import presence_line

    try:
        as_of = presence.parse(installed_at)
    except (TypeError, ValueError):
        log.warning(
            "checkpoint receipt has unreadable presence timestamp thread_id={} checkpoint_id={}",
            thread_id, checkpoint_id,
        )
        return None
    try:
        return presence_line(db, thread_id, as_of=as_of)
    except (TypeError, ValueError) as error:
        # Stored presence or transcript timestamps are historic input too. An
        # unreadable one must not turn a completed checkpoint into a failed install.
        log.warning(
            "checkpoint receipt has unreadable presence data thread_id={} checkpoint_id={} error={}",
            thread_id, checkpoint_id, error,
        )
        return None


def frozen_presence_line(db, checkpoint_id: str) -> str | None:
    """The presence line frozen in one installed checkpoint's receipt."""
    row = db.execute(
        "SELECT thread_id, metadata FROM messages WHERE id=?",
        (completion_receipt_id(checkpoint_id),),
    ).fetchone()
    if row is None or not row["metadata"]:
        return None
    try:
        value = json.loads(row["metadata"]).get(RESUME_PRESENCE_LINE)
    except (TypeError, ValueError, AttributeError):
        log.warning(
            "checkpoint receipt has unreadable metadata thread_id={} checkpoint_id={}",
            row["thread_id"], checkpoint_id,
        )
        return None
    return value if isinstance(value, str) and value else None


def resume_anchor_text(resume_presence_line: str | None) -> str:
    """Render the exact presence line preflight selected and accounted for."""
    from history.adapters import mid_turn_resume_text

    return mid_turn_resume_text(resume_presence_line)
