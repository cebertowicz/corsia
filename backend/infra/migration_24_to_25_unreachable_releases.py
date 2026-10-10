"""Schema step 24 -> 25: free FIFO rows whose promised release never came.

Two kinds of queued input could wait forever before this release:

* A failed switch left its suffix at the dead destination epoch, because
  ``fail_switch`` only marked the destination failed.
* A checkpoint refusal held the source's own inputs, waiting for an install
  or a window change. Delivery stops at a held input, so no Turn ever started,
  no fold was attempted, and that release was unreachable.

Going forward ``fail_switch`` returns the suffix itself and a deterministic
refusal fails its input visibly. This step frees the rows already stranded
before those fixes, by running the very same owners rather than restating
their predicates: a failed switch's suffix goes back to the source, and a hold
is released exactly as an install would release it. The startup recovery pass
that follows every migration then starts each freed thread's head on its own.
"""

from __future__ import annotations

import sqlite3

from infra.input_writes import release_checkpoint_inputs
from infra.model_switch_inputs import return_unstarted_suffix_to_source


def migrate_24_to_25(conn: sqlite3.Connection) -> None:
    """Return failed switches' suffixes, then release unreachable holds."""
    failed = conn.execute(
        "SELECT id,model,execution_epoch,pending_model,pending_execution_epoch "
        "FROM threads WHERE switch_status='failed' "
        "AND pending_model IS NOT NULL AND pending_execution_epoch IS NOT NULL"
    ).fetchall()
    for row in failed:
        return_unstarted_suffix_to_source(conn, row)
    # Candidate threads only; release_checkpoint_inputs applies the exact hold
    # predicate. A preparing switch still owns its held suffix: its terminal
    # settlement is that hold's reachable release, so it is left alone.
    held = conn.execute(
        "SELECT t.id,t.model FROM threads AS t "
        "WHERE t.switch_status IS NOT 'preparing' AND t.id IN ("
        "SELECT thread_id FROM messages WHERE delivery_state='queued' "
        "AND write_outcome='proved_no_write' AND active_turn_id IS NULL "
        "AND write_started_at IS NULL)"
    ).fetchall()
    for row in held:
        release_checkpoint_inputs(conn, str(row["model"]), thread_id=str(row["id"]))
