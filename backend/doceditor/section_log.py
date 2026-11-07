"""What changed in each section of one open document, and when each agent last read it.

A section writer turn's wake counts the changes in every other section since the woken agent last
read the document (``section_wake``). A read is the agent's ``read_file`` of the document, or its
own landed write when the write met no change of others the agent had not seen: the moments
``LiveDocument.read_bases`` records, stamped here with their time. One change is one agent write or
one stretch of a person's editing in a section; comments and suggestions are counted from their rows.

Both live in memory with the live document, so an eviction or a restart forgets them and the wake
says the counts are unknown. A change older than the oldest read still kept can never be counted
and is dropped, and at most ``LIMIT`` are kept. Each change replaces the tuple whole, so a reader on
another thread sees one consistent log without a lock.
"""

from __future__ import annotations

from datetime import datetime, timezone

#: Most changes kept for one document; the oldest go first.
LIMIT = 1000


def now() -> str:
    """The same timestamp form as a comment's ``created_at``, so the two compare as text."""
    return datetime.now(timezone.utc).isoformat()


class SectionLog:
    def __init__(self) -> None:
        self.read_at: dict[str, str] = {}
        #: (when, heading, agent thread or None for a person), oldest first.
        self.changes: tuple[tuple[str, str, str | None], ...] = ()

    def read(self, thread_id: str) -> None:
        self.read_at[thread_id] = now()
        self._keep(self.changes)

    def wrote(self, thread_id: str, merged: bool) -> None:
        """This agent's write landed: a read only if it merged with nothing the agent had not read.

        ``merged`` is the write's receipt saying it merged with live edits. An agent with no read kept
        has none to extend, and one with changes of others since its read has not seen them.
        """
        if not merged and self.since(thread_id) == {}:
            self.read(thread_id)

    def changed(self, headings: list[str], thread_id: str | None = None) -> None:
        at = now()
        self._keep((*self.changes, *((at, heading, thread_id) for heading in headings)))

    def since(self, thread_id: str) -> dict[str, int] | None:
        """Others' changes per section since this agent's last read; None when it has no read kept."""
        read = self.read_at.get(thread_id)
        if read is None:
            return None
        counts: dict[str, int] = {}
        for at, heading, writer in self.changes:
            if at > read and writer != thread_id:
                counts[heading] = counts.get(heading, 0) + 1
        return counts

    def clear(self) -> None:
        self.read_at.clear()
        self.changes = ()

    def _keep(self, changes) -> None:
        oldest = min(self.read_at.values(), default=None)
        self.changes = () if oldest is None else tuple(change for change in changes if change[0] > oldest)[-LIMIT:]
