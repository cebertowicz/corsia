"""Per-document SQLite durability; CRDT merging belongs to the live document.

One transaction commits exact browser batches together with a lane record.
Held evidence is immutable: a recovery choice only changes its own fields.
This store never opens the application's database.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import json
import sqlite3
from typing import TypeVar
from uuid import uuid4

from doceditor.live_publication_record import Publication, OwedPublication
from doceditor.live_failure_record import FailureRecord

_T = TypeVar("_T")


class StoreRefused(RuntimeError):
    """No acknowledgement may be sent for a refused transaction."""


@dataclass(frozen=True)
class BrowserMessage:
    message_id: str
    update: bytes


@dataclass(frozen=True)
class Mirror:
    state: bytes
    file_hash: str


@dataclass(frozen=True)
class HeldChoice:
    kind: str
    candidate: bytes
    mirror_state: bytes
    prior_hash: str
    result_hash: str
    publication: Publication | None = None


@dataclass(frozen=True)
class PendingWrite:
    op_id: str
    candidate: bytes
    mirror_state: bytes
    prior_hash: str
    result_hash: str
    state: str = "pending"
    outside_hash: str | None = None
    choice: HeldChoice | None = None
    publication: Publication | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), store_id TEXT NOT NULL,
    failures TEXT NOT NULL DEFAULT '{}', failure_generation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS live (seq INTEGER PRIMARY KEY, upd BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS mirror (
    seq INTEGER PRIMARY KEY, state BLOB NOT NULL, file_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), op_id TEXT UNIQUE NOT NULL,
    candidate BLOB NOT NULL, mirror_state BLOB NOT NULL,
    prior_hash TEXT NOT NULL, result_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'held')),
    outside_hash TEXT, choice TEXT CHECK(choice IN ('corsia', 'file')),
    choice_candidate BLOB, choice_mirror BLOB,
    choice_prior_hash TEXT, choice_result_hash TEXT,
    publication TEXT, choice_publication TEXT
);
CREATE TABLE IF NOT EXISTS publish (
    seq INTEGER PRIMARY KEY, publication TEXT NOT NULL,
    mirror_state BLOB NOT NULL, result_hash TEXT NOT NULL
);
"""


class LiveStore:
    """Synchronous store called off the pump, under the document committer lock."""

    def __init__(self, path: Path):
        self.path = path
        # Sees every SQLite refusal, from any thread, before it is raised.
        self.observe_refusal: Callable[[StoreRefused], None] = lambda error: None
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(self._connect()) as connection:
                connection.executescript(_SCHEMA)
            self._transaction(lambda db: db.execute(
                "INSERT OR IGNORE INTO meta(singleton,store_id) VALUES (1, ?)", (str(uuid4()),)))
            self._store_id = self._read("SELECT store_id FROM meta WHERE singleton=1")[0][0]
        except sqlite3.Error as exc:
            raise StoreRefused(str(exc)) from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=1.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def probe_owed(path: Path) -> bool:
        """Read a healthy existing store without creating or initializing it."""
        try:
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
                if db.execute("SELECT 1 FROM publish LIMIT 1").fetchone() is None:
                    return False
                check = db.execute("PRAGMA quick_check").fetchone()
                if check is None or check[0] != "ok":
                    raise sqlite3.DatabaseError("SQLite quick_check failed")
                return True
        except sqlite3.Error as exc:
            raise StoreRefused(str(exc)) from exc

    def _read(self, query: str, args: tuple = ()) -> list[sqlite3.Row]:
        try:
            with closing(self._connect()) as db:
                return db.execute(query, args).fetchall()
        except sqlite3.Error as exc:
            raise self._refused(exc) from exc

    def _transaction(self, action: Callable[[sqlite3.Connection], _T]) -> _T:
        from infra.database import write_transaction

        try:
            with closing(self._connect()) as db:
                # Reuse transaction ownership on this private connection; never
                # acquire a pooled application database connection.
                with write_transaction(db, scope="document_live_store"):
                    return action(db)
        except sqlite3.Error as exc:
            raise self._refused(exc) from exc

    def _refused(self, cause: sqlite3.Error) -> StoreRefused:
        error = StoreRefused(str(cause))
        error.__cause__ = cause
        self.observe_refusal(error)
        return error

    @property
    def store_id(self) -> str:
        """The lifetime identity loaded off-loop once, never queried in a frame."""
        return self._store_id

    def initialize(self, state: bytes, file_hash: str) -> None:
        def write(db):
            if db.execute("SELECT 1 FROM live LIMIT 1").fetchone() is None:
                db.execute("INSERT INTO live(upd) VALUES (?)", (state,))
                self._mirror(db, state, file_hash)
        self._transaction(write)

    def load_updates(self) -> tuple[bytes, ...]:
        return tuple(row[0] for row in self._read("SELECT upd FROM live ORDER BY seq"))

    def live_count(self) -> int:
        return self._read("SELECT COUNT(*) FROM live")[0][0]

    def compact(self, state: bytes) -> None:
        """Squash durable updates while no pending or held evidence exists (5.2)."""
        def write(db):
            if db.execute("SELECT 1 FROM pending").fetchone() is not None:
                raise StoreRefused("Cannot compact a document with a pending write")
            db.execute("DELETE FROM live")
            db.execute("INSERT INTO live(upd) VALUES (?)", (state,))
        self._transaction(write)

    def mirror(self) -> Mirror:
        rows = self._read("SELECT state, file_hash FROM mirror ORDER BY seq DESC LIMIT 1")
        if not rows:
            raise StoreRefused("Document store has no disk mirror")
        return Mirror(*rows[0])

    def pending(self) -> PendingWrite | None:
        rows = self._read("SELECT singleton,op_id,candidate,mirror_state,prior_hash,result_hash,state,"
                          "outside_hash,choice,choice_candidate,choice_mirror,choice_prior_hash,"
                          "choice_result_hash,publication,choice_publication FROM pending WHERE singleton=1")
        if not rows:
            return None
        row = rows[0]
        choice = None
        if row["choice"] is not None:
            choice = HeldChoice(row["choice"], row["choice_candidate"], row["choice_mirror"],
                                row["choice_prior_hash"], row["choice_result_hash"],
                                Publication.decode(row["choice_publication"]))
        return PendingWrite(row["op_id"], row["candidate"], row["mirror_state"],
                            row["prior_hash"], row["result_hash"], row["state"],
                            row["outside_hash"], choice, Publication.decode(row["publication"]))

    @staticmethod
    def _batch(db, messages: Sequence[BrowserMessage]) -> tuple[str, ...]:
        db.executemany("INSERT INTO live(upd) VALUES (?)", ((m.update,) for m in messages))
        return tuple(message.message_id for message in messages)

    @staticmethod
    def _mirror(db, state: bytes, file_hash: str) -> None:
        # Only the latest mirror is read; recovery uses the pending row's own state.
        latest = db.execute("INSERT INTO mirror(state, file_hash) VALUES (?, ?)", (state, file_hash)).lastrowid
        db.execute("DELETE FROM mirror WHERE seq < ?", (latest,))

    @staticmethod
    def _require(cursor) -> None:
        if cursor.rowcount != 1:
            raise StoreRefused("The document write is no longer in the expected state")

    def commit_batch(self, messages: Sequence[BrowserMessage]) -> tuple[str, ...]:
        return self._transaction(lambda db: self._batch(db, messages))

    def commit_import(self, update: bytes, state: bytes, file_hash: str) -> None:
        def write(db):
            if db.execute("SELECT 1 FROM pending").fetchone() is not None:
                raise StoreRefused("Waiting for the last document write to settle")
            db.execute("INSERT INTO live(upd) VALUES (?)", (update,))
            self._mirror(db, state, file_hash)
        self._transaction(write)

    def record_pending(
        self, record: PendingWrite, messages: Sequence[BrowserMessage] = (),
    ) -> tuple[str, ...]:
        def write(db):
            ack = self._batch(db, messages)
            db.execute(
                "INSERT INTO pending(singleton, op_id, candidate, mirror_state, prior_hash,"
                " result_hash, state, publication) VALUES (1, ?, ?, ?, ?, ?, 'pending', ?)",
                (record.op_id, record.candidate, record.mirror_state,
                 record.prior_hash, record.result_hash,
                 record.publication.encode() if record.publication else None),
            )
            return ack
        return self._transaction(write)

    def promote(self, op_id: str, clears_through: int | None = None) -> bool:
        """Land the pending write; True when it cleared the store failure it disproves.

        Only a ``store_refused`` failure recorded at or before ``clears_through``
        (the generation the write captured when it started) is cleared.
        """
        def write(db):
            row = db.execute(
                "SELECT candidate, mirror_state, result_hash, publication FROM pending"
                " WHERE op_id=? AND state='pending'", (op_id,),
            ).fetchone()
            if row is None:
                raise StoreRefused("No pending document write matches this operation")
            db.execute("INSERT INTO live(upd) VALUES (?)", (row[0],))
            self._mirror(db, row[1], row[2])
            self._owe(db, Publication.decode(row[3]), row[1], row[2])
            db.execute("DELETE FROM pending WHERE op_id=?", (op_id,))
            return clears_through is not None and self._clear_store_failure(db, clears_through)
        return self._transaction(write)

    @staticmethod
    def _clear_store_failure(db, clears_through: int) -> bool:
        failures = json.loads(db.execute("SELECT failures FROM meta WHERE singleton=1").fetchone()[0])
        failure = failures.get("store_refused")
        if failure is None or failure["generation"] > clears_through:
            return False
        del failures["store_refused"]
        db.execute("UPDATE meta SET failures=? WHERE singleton=1", (json.dumps(failures),))
        return True

    def discard(self, op_id: str) -> None:
        def write(db):
            self._require(db.execute("DELETE FROM pending WHERE op_id=? AND state='pending'", (op_id,)))
        self._transaction(write)

    def mark_held(self, op_id: str, outside_hash: str) -> None:
        self._transaction(lambda db: self._require(db.execute(
            "UPDATE pending SET state='held', outside_hash=?"
            " WHERE op_id=? AND state='pending'", (outside_hash, op_id),
        )))

    def record_choice(
        self, op_id: str, choice: HeldChoice, messages: Sequence[BrowserMessage] = (),
    ) -> tuple[str, ...]:
        def write(db):
            ack = self._batch(db, messages)
            self._require(db.execute(
                "UPDATE pending SET choice=?, choice_candidate=?, choice_mirror=?,"
                " choice_prior_hash=?, choice_result_hash=?, choice_publication=?"
                " WHERE op_id=? AND state='held' AND choice IS NULL",
                (choice.kind, choice.candidate, choice.mirror_state,
                 choice.prior_hash, choice.result_hash,
                 choice.publication.encode() if choice.publication else None, op_id),
            ))
            return ack
        return self._transaction(write)

    def clear_choice(self, op_id: str) -> None:
        self._transaction(lambda db: self._require(db.execute(
            "UPDATE pending SET choice=NULL, choice_candidate=NULL, choice_mirror=NULL,"
            " choice_prior_hash=NULL, choice_result_hash=NULL, choice_publication=NULL"
            " WHERE op_id=? AND state='held'",
            (op_id,),
        )))

    def apply_choice(
        self, op_id: str, choice: HeldChoice, messages: Sequence[BrowserMessage] = (),
    ) -> tuple[str, ...]:
        def write(db):
            row = db.execute(
                "SELECT choice, choice_candidate, choice_mirror, choice_prior_hash,"
                " choice_result_hash, choice_publication FROM pending WHERE op_id=? AND state='held'", (op_id,),
            ).fetchone()
            if row is None:
                raise StoreRefused("No held document write matches this operation")
            expected = (choice.kind, choice.candidate, choice.mirror_state,
                        choice.prior_hash, choice.result_hash,
                        choice.publication.encode() if choice.publication else None)
            if choice.kind == "corsia":
                if tuple(row) != expected:
                    raise StoreRefused("The recorded document choice has not settled")
            elif choice.kind != "file" or row[0] is not None:
                raise StoreRefused("The recorded document choice has not settled")
            ack = self._batch(db, messages)
            self._require(db.execute("DELETE FROM pending WHERE op_id=? AND state='held'", (op_id,)))
            db.execute("INSERT INTO live(upd) VALUES (?)", (choice.candidate,))
            self._mirror(db, choice.mirror_state, choice.result_hash)
            self._owe(db, choice.publication, choice.mirror_state, choice.result_hash)
            return ack
        return self._transaction(write)

    @staticmethod
    def _owe(db, publication: Publication | None, state: bytes, result_hash: str) -> None:
        """Owe one publication per landed write; rows are never merged or rewritten.

        The lane bounds how many are owed by admitting writes only below a bound.
        """
        if publication is None:
            return
        db.execute("INSERT INTO publish(seq, publication, mirror_state, result_hash)"
                   " VALUES ((SELECT MAX(seq) FROM mirror), ?, ?, ?)",
                   (publication.encode(), state, result_hash))

    def owed_count(self) -> int:
        return self._read("SELECT COUNT(*) FROM publish")[0][0]

    def next_publication(self) -> OwedPublication | None:
        """Read only the oldest owed row; publication never reads current file bytes."""
        rows = self._read("SELECT seq, publication, mirror_state, result_hash FROM publish ORDER BY seq LIMIT 1")
        if not rows:
            return None
        row = rows[0]
        return OwedPublication(row["seq"], Publication.decode(row["publication"]),
                               row["mirror_state"], row["result_hash"])

    def failure_snapshot(self) -> tuple[dict[str, FailureRecord], int]:
        """Read failure records and absence watermark in one metadata observation."""
        row = self._read("SELECT failures, failure_generation FROM meta WHERE singleton=1")[0]
        records = {kind: FailureRecord.decode(value) for kind, value in json.loads(row[0]).items()}
        return records, row[1]

    def failures(self) -> dict[str, FailureRecord]:
        return self.failure_snapshot()[0]

    def failure(self, kind: str) -> FailureRecord | None:
        return self.failures().get(kind)

    def failure_generation(self) -> int:
        return self._read("SELECT failure_generation FROM meta WHERE singleton=1")[0][0]

    def record_failure(self, kind: str, message: str) -> FailureRecord:
        """Keep the reason, retry count and a monotonic generation across restarts."""
        def write(db):
            row = db.execute("SELECT failures, failure_generation FROM meta WHERE singleton=1").fetchone()
            failures, generation = json.loads(row[0]), row[1] + 1
            attempts = failures.get(kind, {}).get("attempts", 0) + 1
            failures[kind] = {"attempts": attempts, "message": message, "generation": generation}
            db.execute("UPDATE meta SET failures=?, failure_generation=? WHERE singleton=1",
                       (json.dumps(failures), generation))
            return FailureRecord(attempts, message, generation)
        return self._transaction(write)

    def clear_failure(self, kind: str) -> int:
        def write(db):
            row = db.execute("SELECT failures, failure_generation FROM meta WHERE singleton=1").fetchone()
            failures = json.loads(row[0])
            if kind in failures:
                del failures[kind]
                db.execute("UPDATE meta SET failures=? WHERE singleton=1", (json.dumps(failures),))
            return row[1]
        return self._transaction(write)

    def delete_publication(self, seq: int) -> None:
        """Delete only after the application transaction has stamped this sequence."""
        self._transaction(lambda db: db.execute("DELETE FROM publish WHERE seq=?", (seq,)))
