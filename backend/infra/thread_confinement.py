"""A thread's launch confinement, kept on its own row for every harness.

A confined spawn writes it once, in the transaction that creates the thread, so
no launch of that thread on any harness, by any later model switch, can find it
unconfined. The readers are the tool boundary (``tools.confined_reads``), the
Claude Code launch (``subagents.cc_confinement.launch_confinement``) and the
Codex launch (``subagents.codex_confinement``). A damaged record fails closed:
it could be the one that said ``untrusted``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from subagents.cc_invocation import CONFINEMENT_UNTRUSTED


ISOLATED_RESULT_LABEL = "from an untrusted agent; contains outside content"
UNTRUSTED_TURN_ERROR = "Couldn't finish this untrusted agent's turn."


@dataclass(frozen=True)
class Reader:
    """Authenticated read identity; an unresolved agent never becomes a person."""

    is_person: bool = False
    thread_id: str | None = None

    @classmethod
    def person(cls) -> Reader:
        return cls(is_person=True)

    @classmethod
    def agent(cls, thread_id: str | None = None) -> Reader:
        return cls(thread_id=thread_id if isinstance(thread_id, str) and thread_id else None)

    @classmethod
    def from_tool(cls, context: dict) -> Reader:
        return cls.agent(context.get("_thread_id"))

    @classmethod
    def from_user(cls, user: dict) -> Reader:
        return cls.agent(user.get("thread_id")) if user.get("token_kind") == "internal" else cls.person()


@dataclass(frozen=True)
class RestrictedRead:
    """The same denied transcript result at the tool and API boundaries."""

    reason: str = (
        "This is an untrusted agent's thread. Only its final result is shared, through delegate_status."
    )

    def payload(self) -> dict:
        return {"restricted": True, "restriction_reason": self.reason}


RESTRICTED_READ = RestrictedRead()


def may_read_transcript(reader: Reader, thread_row) -> bool:
    """A damaged confinement is restricted too; only the person and self bypass it."""
    return reader.is_person or thread_row["confinement"] is None or reader.thread_id == thread_row["id"]


def hidden_clause(reader: Reader, alias: str = "") -> tuple[str, tuple]:
    """The SQL equivalent of may_read_transcript, applied before ranking/limits."""
    if alias and not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", alias):
        raise ValueError("invalid thread alias")
    prefix = f"{alias}." if alias else ""
    return f"(? OR {prefix}confinement IS NULL OR {prefix}id = ?)", (reader.is_person, reader.thread_id)


def transcript_restriction(reader: Reader, thread_row) -> RestrictedRead | None:
    return None if may_read_transcript(reader, thread_row) else RESTRICTED_READ


def read_restriction(db: sqlite3.Connection, reader: Reader, thread_id: str) -> RestrictedRead | None:
    """Resolve the same rule for an id-addressed read with its own ownership check."""
    row = db.execute("SELECT id,confinement FROM threads WHERE id=?", (thread_id,)).fetchone()
    return transcript_restriction(reader, row) if row is not None else None


def project_thread(reader: Reader, thread_row) -> dict:
    """Keep status facts while withholding transcript-derived thread fields."""
    item = dict(thread_row)
    if not may_read_transcript(reader, item):
        item["title"] = None
        for key in ("switch_error", "last_message"):
            if key in item:
                item[key] = None
    return item


def checkpoint_for_reader(reader: Reader, thread_row, checkpoint) -> dict | None:
    """Keep checkpoint progress facts while withholding its private narrative/error."""
    if checkpoint is None:
        return None
    status = {"id": checkpoint.id, "status": checkpoint.status, "started_at": checkpoint.started_at}
    if may_read_transcript(reader, thread_row):
        status.update(trigger=checkpoint.trigger, mode=checkpoint.mode,
                      writer_model=checkpoint.writer_model, error=checkpoint.error)
    return status


def _shared_error(error: str | None) -> str | None:
    from subagents.provider_usage_limits import is_provider_usage_or_quota_limit

    if not error or is_provider_usage_or_quota_limit(error):
        return error
    return UNTRUSTED_TURN_ERROR


def worker_error_for_agent(db: sqlite3.Connection, thread_id: str, error: str) -> str:
    """Only unclassified worker text is private; provider refusals keep their meaning."""
    row = db.execute("SELECT id, confinement FROM threads WHERE id=?", (thread_id,)).fetchone()
    return _shared_error(error) if row and not may_read_transcript(Reader.agent(), row) else error


def project_runtime(reader: Reader, thread_row, truth: dict) -> dict:
    """Project liveness in the same place for all agent status readers."""
    if may_read_transcript(reader, thread_row):
        return truth
    result = dict(truth)
    if result.get("last_turn") is not None:
        result["last_turn"] = {**result["last_turn"], "error": _shared_error(result["last_turn"].get("error"))}
    return result


async def read_runtime_for(reader: Reader, thread_id: str, thread_row, **kwargs) -> dict:
    from infra.runtime_truth import read_runtime_truth

    return project_runtime(reader, thread_row, await read_runtime_truth(thread_id, thread_row, **kwargs))


def label_result(text: str, *, tail_chars: int | None = None) -> str:
    """Cap the answer before adding provenance, so every return retains the label."""
    text = text.strip()
    if tail_chars is not None and len(text) > tail_chars:
        text = "…" + text[-tail_chars:]
    return f"{ISOLATED_RESULT_LABEL}\n\n{text}"


def agent_result(db: sqlite3.Connection, thread_id: str, *, turn_id: str | None = None) -> dict | None:
    """The shared answer: a delivered assistant reply from the newest completed turn.

    Unconfined legacy transcripts may lack turn metadata; their existing start-time
    bound is applied here too, rather than independently by each delivery path.
    """
    thread = db.execute("SELECT id, confinement FROM threads WHERE id=?", (thread_id,)).fetchone()
    confined = thread is not None and not may_read_transcript(Reader.agent(), thread)
    turn = db.execute(
        "SELECT id, status, started_at FROM active_turns WHERE thread_id=? "
        + ("AND status='completed' " if confined else "")
        + "ORDER BY turn_order DESC LIMIT 1", (thread_id,),
    ).fetchone()
    if confined and (turn is None or (turn_id is not None and turn["id"] != turn_id)):
        return None
    where = "thread_id=? AND role='assistant' AND length(trim(content))>0 AND delivery_state='delivered'"
    params: list = [thread_id]
    if confined:
        turn_id = turn["id"]
        where += (
            " AND seq IS NOT NULL AND json_valid(metadata)"
            " AND COALESCE(json_extract(metadata, '$.llm_tool_calls'),'[]') IN ('[]','{}',0,'')"
            " AND COALESCE(json_extract(metadata, '$.tool_calls'),'[]') IN ('[]','{}',0,'')"
            " AND COALESCE(json_extract(metadata, '$.step_type'),'')<>'tool_calls'"
        )
    elif turn_id is None:
        where += " AND seq IS NOT NULL"
    if turn_id is not None:
        where += " AND json_valid(metadata) AND json_extract(metadata, '$.turn_id')=?"
        params.append(turn_id)
    row = db.execute(
        "SELECT id, role, content, created_at FROM messages WHERE " + where
        + " ORDER BY seq DESC, rowid DESC LIMIT 1", params,
    ).fetchone()
    if row is None:
        return None
    if not confined and turn_id is None and turn is not None:
        def instant(value):
            parsed = datetime.fromisoformat(str(value))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        if instant(row["created_at"]) < instant(turn["started_at"]):
            return None
    return dict(row)


def internal_route_restriction(reader: Reader, user_id: str, route, scope: dict) -> RestrictedRead | None:
    """Guard resolved internal API reads, including future buffer/context endpoints.

    Status is the one projected read; it returns the labelled completed result.
    Other explicit thread/message/checkpoint reads share the transcript decision.
    """
    if scope["method"] != "GET" or route.path == "/api/delegation/threads/{thread_id}/status":
        return None
    from urllib.parse import parse_qs
    from infra.database import get_db

    _, matched = route.matches(scope)
    targets = {**parse_qs(scope.get("query_string", b"").decode()), **matched.get("path_params", {})}
    db = get_db()
    try:
        for key, value in targets.items():
            values = value if isinstance(value, list) else [value]
            for target_id in values:
                if "thread" in key:
                    row = db.execute(
                        "SELECT id,confinement FROM threads WHERE id=? AND user_id=?", (target_id, user_id),
                    ).fetchone()
                elif "checkpoint" in key or "message" in key:
                    table = "checkpoints" if "checkpoint" in key else "messages"
                    row = db.execute(
                        f"SELECT t.id,t.confinement FROM {table} item JOIN threads t ON t.id=item.thread_id "
                        "WHERE item.id=? AND t.user_id=?", (target_id, user_id),
                    ).fetchone()
                else:
                    continue
                if row is not None and not may_read_transcript(reader, row):
                    return RESTRICTED_READ
        return None
    finally:
        db.close()


class ConfinementRecordDamaged(ValueError):
    """A thread's stored confinement cannot be read, so no launch may run it."""


def record(db: sqlite3.Connection, thread_id: str, mode: str, read_dirs: tuple[str, ...]) -> None:
    """Store a new thread's confinement once; a second write is refused."""
    if mode != CONFINEMENT_UNTRUSTED or not read_dirs:
        raise ValueError("only an untrusted confinement with read dirs is stored")
    if db.execute(
        "UPDATE threads SET confinement=?, confined_read_dirs=? WHERE id=? AND confinement IS NULL",
        (mode, json.dumps(list(read_dirs)), thread_id),
    ).rowcount != 1:
        raise ValueError(f"thread {thread_id} is missing or already has a confinement")


def parse(mode: object, raw_dirs: object) -> tuple[str, ...] | None:
    """The confined read dirs a row's two columns hold, ``None`` if unconfined."""
    if mode is None:
        return None
    try:
        dirs = json.loads(raw_dirs) if isinstance(raw_dirs, str) else None
    except ValueError:
        dirs = None
    if mode != CONFINEMENT_UNTRUSTED or not isinstance(dirs, list) or not dirs or any(
        not isinstance(path, str) or not path for path in dirs
    ):
        raise ConfinementRecordDamaged("this thread's confinement record is damaged")
    return tuple(dirs)


def read_dirs(thread_id: str, db: sqlite3.Connection) -> tuple[str, ...] | None:
    """The thread's confined read dirs, ``None`` if it is unconfined (or gone)."""
    row = db.execute(
        "SELECT confinement, confined_read_dirs FROM threads WHERE id=?", (thread_id,),
    ).fetchone()
    return None if row is None else parse(row[0], row[1])
