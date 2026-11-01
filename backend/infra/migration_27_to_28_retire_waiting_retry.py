"""Convert any lingering waiting_retry Turn into an ordinary dead-boot orphan.

Issue #1655 crashed startup on a leftover ``waiting_retry`` row; the fix
(#1657) removed every application read of that status, including the branch
that used to interpret it during boot recovery. That is correct for any row
written from now on -- nothing writes the status any more -- but it left an
already-existing row worse off than before: the partial unique index
``idx_active_turns_one_active`` (schema.py) still counts ``waiting_retry`` as
a thread's one active Turn, while ``start_idle_turn``'s now-``running``-only
guard no longer sees it as occupying that slot. The guard would let a new
Turn through, and the index would then refuse the insert, so that one thread
could never start another Turn again -- a silent, permanent, single-thread
stall in place of the old loud, all-threads boot crash.

This is a one-time data conversion, not a second recovery path. A legacy row
is given a boot_epoch it already fails to match (its own), and its queued
messages' write markers are cleared exactly as the deleted
``_recover_legacy_retry`` cleared them before attempting to resume the
batch -- except nothing here resumes anything. From this point on,
``recover_threads``'s existing dead-boot branch is the row's only handler: it
settles the Turn as an abandoned one from a prior boot, which returns its
never-written queued input to the FIFO and frees the thread. No branch in ``recover_threads``
needs to know ``waiting_retry`` ever existed. The actual writes live in
``infra.thread_driver`` -- the sole enrolled owner of Turn/input state --
rather than here, so this one-time step does not become a second writer.

The schema's enum and ``CHECK (status != 'waiting_retry' OR retry_at IS NOT
NULL)`` still mention the retired value: dropping either needs a full table
rebuild, out of scope here. Once this step has run, no row can carry the
status again, so both are unreachable dead text rather than a live concern.
"""

from __future__ import annotations

import sqlite3

from infra.thread_driver import retire_legacy_waiting_retry_turns


def migrate_27_to_28(conn: sqlite3.Connection) -> None:
    """Retire every ``waiting_retry`` Turn into an ordinary dead-boot orphan."""
    retire_legacy_waiting_retry_turns(conn)
