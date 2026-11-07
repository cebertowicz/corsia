"""Which projects a caller is fenced from, and what that hides from a history read.

Design row R3 of the project-confinement design (corsia#713). The rule lives here,
in the layer every reader can reach, and ``tools.project_fence.fence_for_thread``
asks it for the same answer before turning it into file paths, so the file gate
and a history read can never disagree about who is outside a project.

A message or checkpoint row records no project: neither table has a project column
and neither project writer bumps the thread's epoch. So a history row is fenced by
the union of two rules over the thread it belongs to:

* its **current** project is one the caller is fenced from, which covers a thread
  moved into a fenced project with its pre-move history included; and
* it sits at or below the thread's **cut** — the last message number when that
  thread left a Local only project, written by ``record_local_only_cut`` inside
  both project-move writers. Messages at or below the cut are fenced, and *all* of
  that thread's checkpoints are, before and after the move, because a checkpoint
  carries earlier continuity entries and narrative forward whatever message number
  it was written at.

The cut fences everyone but the person, including the thread's own project: moving
out of a Local only project is something an agent can do to its own thread, so a
cut that hid only from outsiders would hide nothing. A cut only ever fences more.

A person's own session (no thread) is never fenced. A caller thread that cannot be
read fences every thread that has a project, never nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from infra.thread_confinement import Reader


class _All:
    """The fail-closed answer: fenced from every project that exists."""

    def __repr__(self) -> str:
        return "ALL"


#: Returned by :func:`fenced_project_ids` when the caller cannot be read.
ALL: Final = _All()


@dataclass(frozen=True, slots=True)
class FencedSubject:
    """A thread the caller may not read at all, with what the refusal copy needs."""

    project: str | None = None
    local_only: bool = False
    conductor: str | None = None


#: The approved refusal wording, held with the rule so a history read and a file
#: read name the same wall in the same words (row R5).
_ORDINARY_COPY = (
    "`{subject}` is in project `{name}`, outside your scope. "
    "Ask the CORSIA agent, or use `request_host_access` for reviewed temporary access."
)
_WORKSPACE_AGENT_COPY = (
    "`{subject}` is in a workspace agent's private folder, outside your scope. "
    "Ask the CORSIA agent, or use `request_host_access` for reviewed temporary access."
)
# A history read has no lease that opens it: chat history and checkpoints come from
# CORSIA's own database, not from project files a reviewed lease covers. So the thread
# wording says what is true instead of offering a route that stays refused (row R5).
_THREAD_ORDINARY_COPY = (
    "`{subject}` is in project `{name}`, outside your scope. "
    "Host access doesn't open it. Ask the CORSIA agent."
)
_THREAD_WORKSPACE_AGENT_COPY = (
    "`{subject}` belongs to a workspace agent, outside your scope. "
    "Host access doesn't open it. Ask the CORSIA agent."
)
_LOCAL_ONLY_COPY = (
    "`{subject}` is in Local only project `{name}`, outside your scope. "
    "Host access doesn't open it. Ask its conductor{conductor}."
)
#: Row R15: one fixed line on a search the wall narrowed, with no count to leak.
HISTORY_NOTICE = "History from projects this agent can't read was left out."
WITHHELD_LOCAL_ONLY = "Withheld: this project is Local only."


def refusal_copy(
    subject: str,
    project: str | None,
    local_only: bool,
    conductor: str | None,
    *,
    history: bool = False,
) -> str:
    """The approved refusal wording for one fenced subject, whether a path or a thread.

    ``history`` marks a read of a thread's history, where no host-access lease applies:
    the two wordings that offer one swap for the thread variants, and Local only, which
    already says host access does not open it, stays identical.
    """
    if project is None:
        return (_THREAD_WORKSPACE_AGENT_COPY if history else _WORKSPACE_AGENT_COPY).format(subject=subject)
    if not local_only:
        template = _THREAD_ORDINARY_COPY if history else _ORDINARY_COPY
        return template.format(subject=subject, name=project)
    who = f" (`{conductor}`)" if conductor else ""
    return _LOCAL_ONLY_COPY.format(subject=subject, name=project, conductor=who)


def thread_refusal(subject: FencedSubject, thread_id: str, *, caller: str | None = None) -> str:
    """The approved refusal for a thread an agent named directly, in row R5's words."""
    from infra.product_events import emit_product_event

    emit_product_event(
        "project_fence", "refused", thread_id=caller, properties={"local_only": subject.local_only}
    )
    return refusal_copy(thread_id, subject.project, subject.local_only, subject.conductor, history=True)


@dataclass(frozen=True, slots=True)
class Caller:
    """One caller's facts, read once.

    The file gate and the history readers both need these, and both derive every
    decision from this one value: reading the caller's Local only lock twice lets a
    lock change between the two reads split the decisions, which is the one thing the
    file gate and a history read must never do.
    """

    thread_id: str | None
    designated_agent: str | None = None
    known: bool = True
    project_id: str | None = None
    local_only: bool = False

    @property
    def is_corsia_agent(self) -> bool:
        """Whether this caller is the install's designated CORSIA agent."""
        return self.thread_id is not None and self.thread_id == self.designated_agent

    @property
    def inside_every_project(self) -> bool:
        """Whether even a folder no project claims is not walled from this caller.

        The one home of that rule. The CORSIA agent holding its own Local only lock is
        inside Local only by definition, so a wall around Local only folders would
        refuse it the work it is locked to do — which is also why
        :func:`fenced_project_ids` fences no project from such a caller.
        """
        return self.is_corsia_agent and self.local_only


def read_caller(thread_id: str | None, *, db) -> Caller:
    """The caller's facts: its thread row and the agent designation, one read each."""
    from infra.corsia_agent import get_corsia_agent

    if not thread_id:
        return Caller(thread_id=None)
    designated = get_corsia_agent(db=db)
    row = db.execute(
        "SELECT project_id, local_only FROM threads WHERE id = ?", (thread_id,)
    ).fetchone()
    if row is None:
        return Caller(thread_id=thread_id, designated_agent=designated, known=False)
    return Caller(
        thread_id=thread_id,
        designated_agent=designated,
        project_id=row["project_id"],
        local_only=bool(row["local_only"]),
    )


def fenced_project_ids(caller: Caller, *, db) -> frozenset[str] | _All:
    """Every project this caller is fenced from, or :data:`ALL` when it fails closed.

    - No thread is the person's own session: nothing is fenced from it.
    - A caller thread with no readable row is fenced from everything.
    - The CORSIA agent is fenced only from Local only projects, and only while it is
      on a cloud model; a thread locked Local only is inside every project.
    - Any other agent is fenced from every project but its own, and from all of them
      when it belongs to none.

    A thread with no project is fenced by neither a caller's project set nor its own
    project, because there is no project to be walled from; only :data:`ALL` hides
    such a thread, and it arises only from a caller that cannot be read.
    """
    from llm.locality import cloud_off_project_ids

    if caller.thread_id is None:
        return frozenset()
    if not caller.known:
        return ALL
    if caller.inside_every_project:
        return frozenset()
    if caller.is_corsia_agent:
        return frozenset(cloud_off_project_ids(db=db))
    every = frozenset(str(row["id"]) for row in db.execute("SELECT id FROM projects"))
    own = caller.project_id
    return every if own is None else every - {str(own)}


@dataclass(frozen=True, slots=True)
class HistoryFence:
    """One caller's fence over history rows, applied either as SQL or as a yes/no.

    Build it once per call with :meth:`for_reader` and reuse it for every query that
    call makes. Filter in SQL wherever the query allows, so a fenced row never
    counts toward a limit.
    """

    person: bool = False
    everything: bool = False
    project_ids: frozenset[str] = frozenset()

    @classmethod
    def for_reader(cls, reader: Reader, *, db) -> HistoryFence:
        """The caller's fence. A person's session has none, by construction."""
        if reader.is_person:
            return cls(person=True)
        fenced = fenced_project_ids(read_caller(reader.thread_id, db=db), db=db)
        if isinstance(fenced, _All):
            return cls(everything=True)
        return cls(project_ids=fenced)

    @property
    def any_walled(self) -> bool:
        """Whether this caller is walled from anything, which the fixed notice says."""
        return not self.person and (self.everything or bool(self.project_ids))

    def fences_project(self, project_id: object) -> bool:
        """Whether a thread whose current project is ``project_id`` is wholly fenced."""
        if self.person:
            return False
        return self.everything or (project_id is not None and str(project_id) in self.project_ids)

    def wholly_fenced(self, db, thread_id: str) -> FencedSubject | None:
        """What the refusal copy needs when ``thread_id`` is wholly fenced from here.

        Only the current project fences a whole thread: a cut hides part of a
        thread's rows, so an addressed read of a thread with a cut still runs and its
        query drops the fenced rows.
        """
        if not self.any_walled:
            return None
        row = db.execute(
            "SELECT t.project_id, p.name, p.conductor_thread_id, pi.cloud_models_off"
            " FROM threads t LEFT JOIN projects p ON p.id = t.project_id"
            " LEFT JOIN project_inference_policies pi ON pi.project_id = t.project_id"
            " WHERE t.id = ?",
            (thread_id,),
        ).fetchone()
        if row is None or not self.fences_project(row["project_id"]):
            return None
        return FencedSubject(
            project=row["name"],
            local_only=row["cloud_models_off"] == 1,
            conductor=row["conductor_thread_id"],
        )

    def hidden_below(self, cut: int | None) -> int | None:
        """The message number at and below which a thread's messages are hidden here.

        ``cut`` is that thread's ``local_only_cut_seq``; ``None`` means it never left
        a Local only project, so nothing is hidden.
        """
        return None if self.person or cut is None else int(cut)

    def visible_clause(self, thread_alias: str, seq_column: str | None = None) -> tuple[str, tuple]:
        """Return ``(sql, params)`` true for a row this caller may read, for ``WHERE``.

        ``thread_alias`` names the joined ``threads`` row the fenced thread belongs
        to. Pass ``seq_column`` (e.g. ``"m.seq"``) for message rows, where only the
        messages at or below the cut are hidden; leave it out for checkpoint rows,
        where a cut hides the thread's whole generation history. A caller with no
        :attr:`project_ids` and no :attr:`everything` adds no project filter, so the
        cut rule still applies. A thread with no project is never hidden by the
        project half, because there is no project to be walled from.
        """
        if self.person:
            return "1", ()
        if self.everything:
            return "0", ()
        cut = (
            f"({thread_alias}.local_only_cut_seq IS NULL OR "
            f"({seq_column} IS NOT NULL AND {seq_column} > {thread_alias}.local_only_cut_seq))"
            if seq_column
            else f"{thread_alias}.local_only_cut_seq IS NULL"
        )
        if not self.project_ids:
            return cut, ()
        marks = ",".join("?" for _ in self.project_ids)
        return (
            f"({thread_alias}.project_id IS NULL OR {thread_alias}.project_id NOT IN ({marks}))"
            f" AND {cut}",
            tuple(self.project_ids),
        )


def record_local_only_cut(db, *, thread_id: str, from_project_id: str | None) -> None:
    """Write the thread's cut when it leaves a Local only project, in the move's transaction.

    Both project-move writers call this with the project the thread is leaving, inside
    the transaction that already writes the move, so a move never commits without its
    cut. Leaving any other project writes nothing: the current-project rule already
    covers ordinary projects, and a cut exists only for content that must never leave
    the machine. A thread that leaves twice keeps the later number.
    """
    from llm.locality import cloud_off_project_ids

    if from_project_id is None or from_project_id not in cloud_off_project_ids(db=db):
        return
    last = db.execute(
        "SELECT MAX(seq) AS last_seq FROM messages WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    db.execute(
        "UPDATE threads SET local_only_cut_seq = MAX(COALESCE(local_only_cut_seq, 0), ?) WHERE id = ?",
        (int((last["last_seq"] if last else None) or 0), thread_id),
    )
