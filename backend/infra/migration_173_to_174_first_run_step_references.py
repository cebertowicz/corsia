"""Schema 173 -> 174: open first-run setup steps name their step's reference.

The app first run now seeds each setup task (and the step 3 asking child) with a
description naming the reference file for that step, so the conductor re-reads it
when it starts the step, even after a checkpoint. Plans seeded before that change
have no description, and nothing reconciles them once the first turn has run, so
an install part-way through its first run would keep losing the step's text.

Each still-open ``first-run-app`` node with no description gets the same line the
seeder writes now (``onboarding.first_run_instructions.step_reference_line``),
through ``plans.public.touch_node``, the one writer of a node's revision. The line
is imported rather than copied: it names files this build serves. Settled,
archived and already-described nodes are left alone.

Data only; runs inside the runner's transaction and never commits.
"""

from __future__ import annotations

import sqlite3

_CANDIDATES_SQL = """
    SELECT id, workflow_step FROM plan_nodes
     WHERE workflow_ref='first-run-app' AND archived_at IS NULL
       AND COALESCE(description, '')=''
       AND state NOT IN ('accepted', 'accepted_with_followup', 'failed', 'closed')
"""


def migrate_173_to_174(conn: sqlite3.Connection) -> None:
    """Give each open first-run setup step its step reference line."""
    from onboarding.first_run_instructions import STEP_REFERENCES, step_reference_line
    from plans import public

    # The older rungs' migration tests run on a partial schema with no workflow columns.
    if "workflow_ref" not in {str(row[1]) for row in conn.execute("PRAGMA table_info(plan_nodes)")}:
        return
    for node_id, workflow_step in conn.execute(_CANDIDATES_SQL).fetchall():
        number = str(workflow_step or "").partition(":")[0]
        if number.isdigit() and int(number) in STEP_REFERENCES:
            public.touch_node(conn, node_id, description=step_reference_line(int(number)))
