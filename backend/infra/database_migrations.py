"""Forward-only schema migrations, applied once at startup.

Before this module existed, a schema change made the next deploy refuse to
start until an operator ran a converter by hand against a stopped instance.
That is the failure this removes: an existing database at an older schema is
migrated forward in-process, so a deploy is a deploy.

Three properties are load-bearing, and each exists because of a specific way
this can destroy a production database.

**The transaction is explicit.** Python's ``sqlite3`` does not open one for
DDL, so ``with conn:`` around ``ALTER``/``CREATE``/``DROP`` wraps nothing and
each statement autocommits. A failure part-way through a ladder would leave a
half-converted database with no recovery path. The canonical
``write_transaction`` owns a single ``BEGIN IMMEDIATE`` spanning every step,
so a failure, including a failed commit, rolls the whole thing back.

**The version is re-read inside that transaction.** Deciding to migrate
outside it is a check-then-act race: two backends starting together would both
read the old version and both apply the ladder. ``BEGIN IMMEDIATE`` takes the
write lock, so the loser blocks, then re-reads and finds nothing to do.

**The backup is bound to the open connection**, not to a re-resolved path.
Backing up a different file than the one being changed is indistinguishable
from having no backup at all, and only becomes visible when it is needed.

Doctrine: category 5 — a new mechanism, which the first four cannot serve.

Deletion (1) does not apply: schema evolution is a real recurring need, not
an accident to remove. Making the bad state unrepresentable (2) does not
either — "database written by an older build" is a fact about time, not a
state that better modelling can forbid. Using the existing owner correctly
(3) is *necessary* and this does it, hooking into ``initialize_schema`` so
startup keeps one owner, but it is not *sufficient*: that owner had no
migration capability to use. Collapsing several mechanisms into one (4) would
be a nicer story than the true one — the four scripts in
``scripts/migrations/`` already ran, are dormant, and performed one-shot data
conversions for schema changes that are long since applied. This module does
not do their work and does not remove them.

What it does replace is the *practice* they represent: writing a bespoke
converter per schema change, documenting it, and having an operator run it by
hand against a stopped instance before the deploy will start. That is a
recurring cost paid per change, and a recurring opportunity to forget a step
during a deploy.

The ledger. Added, on the internal side: this module and its tests. Bought
down, on the operator's side: a manual step in every schema-touching deploy,
and the class of incident where that step is missed or half-completed. The
transaction discipline above is got right once here, rather than re-derived
per script — the existing scripts each re-implement it with the ``with conn:``
shape that does not roll back DDL, which is the subject of #1518.

The registered steps now make each supported deploy advance existing databases
automatically through the same guarded transaction.

Falsification: if pre-release schema changes stop happening, or if the ladder
grows long enough that squashing it before release becomes its own project,
this mechanism is not paying for itself and should be reconsidered rather than
extended.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import infra.startup_progress as startup_progress
from infra.checkpoint_schema import CHECKPOINT_ENTRY_SEARCH_SCHEMA_SQL
from infra.database import write_transaction
from infra.checkpoint_entry_index import backfill_checkpoint_entry_search
from infra.db_fts import _init_checkpoint_entry_search_fts
from infra.delivery_foundation_schema import CHECKPOINT_HOLD_INDEX_SQL
from infra.migration_8_to_9_artifacts import migrate_8_to_9
from infra.migration_10_to_11_artifact_origin import migrate_10_to_11
from infra.migration_14_to_15_codex_handles import migrate_14_to_15
from infra.migration_15_to_16_plan_waits import migrate_15_to_16
from infra.migration_16_to_17_wake_condition import migrate_16_to_17
from infra.migration_17_to_18_watch_identity import migrate_17_to_18
from infra.migration_21_to_22_restore_workspace_artifacts import migrate_21_to_22
from infra.migration_22_to_23_retire_ended_pull_requests import migrate_22_to_23
from infra.migration_23_to_24_rename_retired_at import migrate_23_to_24
from infra.migration_24_to_25_unreachable_releases import migrate_24_to_25
from infra.migration_25_to_26_accounts import migrate_25_to_26
from infra.migration_26_to_27_node_closed import migrate_26_to_27
from infra.migration_27_to_28_retire_waiting_retry import migrate_27_to_28
from infra.migration_28_to_29_unattributed_dispatches import migrate_28_to_29
from infra.migration_29_to_30_presence import migrate_29_to_30
from infra.migration_30_to_31_plan_node_cost_indexes import migrate_30_to_31
from infra.migration_33_to_34_checkpoint_listing import migrate_33_to_34
from infra.migration_34_to_35_webhook_object_key import migrate_34_to_35
from infra.migration_36_to_37_stranded_dependencies import migrate_36_to_37
from infra.migration_38_to_39_unbound_watches import migrate_38_to_39
from infra.migration_39_to_40_placement_marks import migrate_39_to_40
from infra.migration_40_to_41_scope_conductor_backfill import migrate_40_to_41
from infra.migration_41_to_42_plan_pane_views import migrate_41_to_42
from infra.migration_42_to_43_checkpoint_snapshots import migrate_42_to_43
from infra.migration_44_to_45_webhook_check_suite_id import migrate_44_to_45
from infra.migration_45_to_46_placement_answers import migrate_45_to_46
from infra.migration_46_to_47_guidance_manifests import migrate_46_to_47
from infra.migration_47_to_48_closed_stay_on_plan import migrate_47_to_48
from infra.migration_48_to_49_pane_view_one_mark import migrate_48_to_49
from infra.migration_49_to_50_withdrawn_decisions_close import migrate_49_to_50
from infra.migration_50_to_51_roster_revision import migrate_50_to_51
from infra.migration_51_to_52_workflow_task_fields import migrate_51_to_52
from infra.migration_52_to_53_unfiled_proposals import migrate_52_to_53
from infra.migration_53_to_54_unbound_human_turn_index import migrate_53_to_54
from infra.migration_54_to_55_checkpoint_fts_narrative import migrate_54_to_55
from infra.migration_55_to_56_workflow_version import migrate_55_to_56
from infra.migration_56_to_57_sent_back_close import migrate_56_to_57
from infra.migration_57_to_58_waits_and_gone_owners import migrate_57_to_58
from infra.migration_58_to_59_agent_pauses_scheduled import migrate_58_to_59
from infra.migration_59_to_60_event_waits_deadline import migrate_59_to_60
from infra.migration_60_to_61_settle_orphaned_worker_steps import migrate_60_to_61
from infra.migration_61_to_62_drop_unbound_human_unreadable_index import migrate_61_to_62
from infra.migration_62_to_63_thread_updates import migrate_62_to_63
from infra.migration_63_to_64_presence_seen import migrate_63_to_64
from infra.migration_64_to_65_remove_legacy_session_tables import migrate_64_to_65
from infra.migration_65_to_66_delete_orphaned_thread_state import migrate_65_to_66
from infra.migration_66_to_67_doc_comment_context import migrate_66_to_67
from infra.migration_67_to_68_managed_process_handles import migrate_67_to_68
from infra.migration_68_to_69_drop_unwritten_journal_columns import migrate_68_to_69
from infra.migration_69_to_70_drop_unbound_human_turn_index import migrate_69_to_70
from infra.migration_70_to_71_doc_suggestions import migrate_70_to_71
from infra.migration_71_to_72_doc_feedback import migrate_71_to_72
from infra.migration_72_to_73_doc_edit_feedback import migrate_72_to_73
from infra.migration_73_to_74_checkpoint_through_at import migrate_73_to_74
from infra.migration_74_to_75_checkpoint_through_at_backfill import migrate_74_to_75
from infra.migration_75_to_76_usage_verdicts import migrate_75_to_76
from infra.migration_76_to_77_outcome_events import migrate_76_to_77
from infra.migration_77_to_78_measurement_query_indexes import migrate_77_to_78
from infra.migration_78_to_79_stored_names import migrate_78_to_79
from infra.migration_79_to_80_bulletins import migrate_79_to_80
from infra.migration_80_to_81_vendor_accounts import migrate_80_to_81
from infra.migration_81_to_82_vendor_policy import migrate_81_to_82
from infra.migration_82_to_83_account_rollover import migrate_82_to_83
from infra.migration_83_to_84_write_hold_indexes import migrate_83_to_84
from infra.migration_84_to_85_doc_reviews import migrate_84_to_85
from infra.migration_85_to_86_doc_feedback_events import migrate_85_to_86
from infra.migration_86_to_87_doc_comment_thread_views import migrate_86_to_87
from infra.migration_87_to_88_doc_decision_events import migrate_87_to_88
from infra.migration_88_to_89_provider_policy import migrate_88_to_89
from infra.migration_89_to_90_pending_account import migrate_89_to_90
from infra.migration_90_to_91_plan_node_size import migrate_90_to_91
from infra.migration_91_to_92_deferred_move_receipt import migrate_91_to_92
from infra.migration_92_to_93_host_access_subscription_costs import migrate_92_to_93
from infra.migration_93_to_94_turn_usage_binding_backfill import migrate_93_to_94
from infra.migration_94_to_95_subscription_billing import migrate_94_to_95
from infra.migration_95_to_96_learning_boundary_index import migrate_95_to_96
from infra.migration_96_to_97_key_store import migrate_96_to_97
from infra.migration_97_to_98_effort_and_limit_history import migrate_97_to_98
from infra.migration_98_to_99_plan_status_dirty import migrate_98_to_99
from infra.migration_99_to_100_usage_rollup import migrate_99_to_100
from infra.migration_100_to_101_billing_charge_day import migrate_100_to_101
from infra.migration_101_to_102_reviewer_awareness import migrate_101_to_102
from infra.migration_102_to_103_recovered_errors import migrate_102_to_103
from infra.migration_103_to_104_retype_legacy_images import migrate_103_to_104
from infra.migration_104_to_105_review_time import migrate_104_to_105
from infra.migration_105_to_106_plan_aggregates import migrate_105_to_106
from infra.migration_106_to_107_key_store_credentials import migrate_106_to_107
from infra.migration_107_to_108_server_keys import migrate_107_to_108
from infra.migration_109_to_110_agent_use_restriction import migrate_109_to_110
from infra.migration_110_to_111_token_plan_by_host import migrate_110_to_111
from infra.migration_111_to_112_image_regions import migrate_111_to_112
from infra.migration_112_to_113_retype_upload_images import migrate_112_to_113
from infra.migration_113_to_114_update_operations import migrate_113_to_114
from infra.migration_114_to_115_scheduled_updates import migrate_114_to_115
from infra.migration_115_to_116_corrections_checker_cleanup import migrate_115_to_116
from infra.migration_116_to_117_script_schedules import migrate_116_to_117
from infra.migration_117_to_118_corsia_agent import migrate_117_to_118
from infra.migration_118_to_119_lesson_placement_checks import migrate_118_to_119
from infra.migration_119_to_120_withdraw_retired_user_asks import migrate_119_to_120
from infra.migration_120_to_121_action_drafts import migrate_120_to_121
from infra.migration_121_to_122_email_screens import migrate_121_to_122
from infra.migration_122_to_123_connected_apps import migrate_122_to_123
from infra.migration_123_to_124_drop_title_source import migrate_123_to_124
from infra.migration_124_to_125_reviewer_gate_approval import migrate_124_to_125
from infra.migration_125_to_126_presence_focus import migrate_125_to_126
from infra.migration_126_to_127_user_comments_index import migrate_126_to_127
from infra.migration_127_to_128_measure_registry import migrate_127_to_128
from infra.migration_128_to_129_connected_accounts import migrate_128_to_129
from infra.migration_129_to_130_project_inference_policy import migrate_129_to_130
from infra.migration_130_to_131_local_only_threads import migrate_130_to_131
from infra.migration_131_to_132_microsoft_accounts import migrate_131_to_132
from infra.migration_132_to_133_review_forks import migrate_132_to_133
from infra.migration_133_to_134_doc_stranded_suggestions import migrate_133_to_134
from infra.migration_134_to_135_doc_decision_bulk import migrate_134_to_135
from infra.migration_135_to_136_event_rules import migrate_135_to_136
from infra.migration_136_to_137_event_intake import migrate_136_to_137
from infra.migration_137_to_138_plan_intent import migrate_137_to_138
from infra.migration_138_to_139_rule_runs import migrate_138_to_139
from infra.migration_139_to_140_review_requesting_node import migrate_139_to_140
from infra.migration_140_to_141_worker_starts import migrate_140_to_141
from infra.migration_141_to_142_doc_comment_reply_roots import migrate_141_to_142
from infra.migration_142_to_143_decision_source_index import migrate_142_to_143
from infra.migration_143_to_144_sent_back_corrections import migrate_143_to_144
from infra.migration_144_to_145_mail_host_endpoints import migrate_144_to_145
from infra.migration_145_to_146_doc_feedback_delivery_policy import migrate_145_to_146
from infra.migration_146_to_147_measure_breaks import migrate_146_to_147
from infra.migration_147_to_148_correction_learning import migrate_147_to_148
from infra.migration_148_to_149_chat_comment_answers import migrate_148_to_149
from infra.migration_149_to_150_doc_insert_after import migrate_149_to_150
from infra.migration_150_to_151_self_caught_corrections import migrate_150_to_151
from infra.migration_151_to_152_person_step import migrate_151_to_152
from infra.migration_152_to_153_observe_rules import migrate_152_to_153
from infra.migration_153_to_154_retired_worker_artifacts import migrate_153_to_154
from infra.migration_154_to_155_thread_confinement import migrate_154_to_155
from infra.migration_155_to_156_chat_comments import migrate_155_to_156
from infra.migration_156_to_157_rehome_repair import migrate_156_to_157
from infra.migration_157_to_158_error_cause_kinds import migrate_157_to_158
from infra.migration_158_to_159_unconfine_main_agents import migrate_158_to_159
from infra.migration_159_to_160_untrusted_confinement import migrate_159_to_160
from infra.migration_160_to_161_self_caused import migrate_160_to_161
from infra.migration_mcp_secrets import FROM_VERSION as MCP_SECRETS_FROM_VERSION
from infra.migration_mcp_secrets import migrate_mcp_secrets

log = logging.getLogger(__name__)

#: How long the backup's reader connection waits for the database. It only ever
#: reads committed state alongside the migration's own write lock, so this is a
#: guard against an unrelated writer, not part of the migration's own timing.
_BACKUP_TIMEOUT_SECONDS = 30.0

#: Pages the pre-migration backup copies per step, 64 MiB at the default page
#: size: a fraction of a second each, so its progress stays current.
_BACKUP_STEP_PAGES = 16_384

#: How much free space the pre-migration backup requires, as a multiple of the
#: database size. The copy is byte-for-byte, so 1.0 is the floor; the margin
#: covers the WAL and the migration's own temporary tables.
_BACKUP_MIN_FREE_RATIO = 1.25

#: Free space the host must still have *after* the backup is written.
#:
#: This is what actually bounds a crash-restart loop. The write lock makes one
#: starter the backup winner within a single boot, and nothing bounds copies
#: across boots: under a supervisor that restarts on crash, every retry is a
#: fresh uncontested winner, and the timestamped filename means the
#: refuse-to-overwrite guard below never fires. Nine 4.27 GB copies in four
#: minutes once took a host from 41 GiB free to 3.4 GiB, which would have taken
#: down the host, the second instance and the CI runners with it.
#:
#: A reserve rather than a copy limit, deliberately. Counting existing backups
#: looks cheaper but is unsound: a failed migration rolls back and the previous
#: build resumes serving, so the database keeps changing and an older backup
#: for the same source version no longer represents the state a new attempt
#: would be protecting. Refusing on that count can therefore refuse a copy that
#: is genuinely needed, and leave startup blocked until someone deletes files.
#: Reserving space assumes nothing about what any existing backup contains: the
#: loop halts with the host still usable, and the operator can clear the
#: condition by freeing space anywhere rather than by picking a file to delete.
_BACKUP_FREE_FLOOR_BYTES = int(
    os.getenv("CORSIA_BACKUP_FREE_FLOOR_BYTES", str(10 * 1024**3))
)

#: How many pre-migration backups of a database survive a successful migration.
#:
#: Nothing else ever removes them, and each is a full copy: left alone they
#: accumulate until the room check above refuses the next copy and startup
#: fails on space our own stale backups hold. At least one is always kept (the
#: backup just taken), so a value below 1 is read as 1.
_PRE_MIGRATION_BACKUPS_KEEP = max(
    1, int(os.getenv("CORSIA_PRE_MIGRATION_BACKUPS_KEEP", "2"))
)

#: How long a losing starter waits for the migration's write lock.
#:
#: The ordinary connection busy timeout is tuned for ordinary queries — five
#: seconds in production — but the migration holds its lock for a full backup
#: plus every step, which the deploy runbook records as minutes on a database
#: this size. Left at the ordinary value, a second backend starting during a
#: real migration dies with `database is locked` instead of waiting and then
#: standing down. Waiting is the correct behaviour: the loser re-reads the
#: migrated version and returns without doing anything.
_MIGRATION_LOCK_WAIT_MS = 30 * 60 * 1000

#: One migration step. ``apply`` runs inside the caller's open transaction and
#: must never commit, roll back, or open one of its own.
MigrationStep = Callable[[sqlite3.Connection], None]


class MigrationError(RuntimeError):
    """A migration refused or failed; the database is unchanged."""


class BackupRoomError(MigrationError):
    """The backup reserve cannot fit, carrying the exact byte counts."""

    def __init__(self, message: str, needed_bytes: int, free_bytes: int):
        super().__init__(message)
        self.needed_bytes = needed_bytes
        self.free_bytes = free_bytes


@dataclass(frozen=True)
class MigrationResult:
    """What a migration attempt did, for the caller to log or surface."""

    migrated: bool
    from_version: int
    to_version: int
    backup_path: Path | None


#: The ladder, keyed by the version each step upgrades *from*. A step is
#: registered here only when its schema change has landed in ``infra.schema``;
#: the two are asserted equivalent by the migration tests, so a converted
#: database and a freshly created one cannot drift apart.
#:
#: These are squashed into one clean schema before release, so a released
#: build never inherits the ladder.
MIGRATIONS: dict[int, MigrationStep] = {}


# The checkpoints table as schema 7 defines it. Deliberately a copy of
# ``infra.checkpoint_schema.CHECKPOINT_SCHEMA_SQL`` rather than an import of it: a
# migration step must keep producing the shape it was written for. Importing
# the live constant would make this step silently start producing schema 8
# the day someone edits that file, and a database migrated last month would no
# longer match one migrated today. The copy is checked against the live
# constant by ``test_schema_converter_v7`` while 7 is current, and the whole
# ladder is squashed before release.
_CHECKPOINTS_7_DDL = """
CREATE TABLE checkpoints_new (
    generation_order INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    history_epoch INTEGER NOT NULL,
    previous_generation_id TEXT,
    through_seq INTEGER NOT NULL,
    through_message_id TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('manual','pressure','model_switch','idle')),
    mode TEXT NOT NULL CHECK (mode IN ('warm','cold')),
    status TEXT NOT NULL CHECK (
        status IN ('running','captured','installed','overtaken','failed','cancelled')
    ),
    source_harness TEXT NOT NULL,
    source_model TEXT NOT NULL,
    writer_harness TEXT NOT NULL,
    writer_model TEXT NOT NULL,
    originating_turn_id TEXT,
    first_included_seq INTEGER,
    continuity_base_revision INTEGER NOT NULL,
    continuity_base_snapshot_json TEXT NOT NULL,
    raw_result TEXT,
    continuity_snapshot_json TEXT,
    usage_json TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    captured_at TEXT,
    installed_at TEXT,
    ended_at TEXT
)
"""

_CHECKPOINTS_7_INDEXES = (
    "CREATE UNIQUE INDEX checkpoints_one_live"
    " ON checkpoints(thread_id) WHERE status IN ('running','captured')",
    "CREATE INDEX checkpoints_current"
    " ON checkpoints(thread_id,history_epoch,status,generation_order DESC)",
)


def _rebuild_checkpoints_with_idle_trigger(conn: sqlite3.Connection) -> None:
    """Widen the trigger vocabulary, which SQLite cannot do in place.

    A CHECK constraint can only be changed by rebuilding the table. Two
    details make that riskier than it looks:

    ``checkpoint_staged_edits`` holds a foreign key into this table, so the
    rebuild depends on the runner having foreign keys off for the duration and
    running ``foreign_key_check`` afterwards.

    The table is ``INTEGER PRIMARY KEY AUTOINCREMENT``, so after any row
    deletion ``sqlite_sequence`` sits above ``max(generation_order)``. It is
    carried across explicitly: letting it reset would let a future checkpoint
    reuse a generation number at or below an already-discarded usage bound,
    silently re-entering a band that was cleared on purpose.
    """
    sequence = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='checkpoints'"
    ).fetchone()
    columns = [row[1] for row in conn.execute("PRAGMA table_info(checkpoints)")]
    names = ",".join(f'"{column}"' for column in columns)

    conn.execute(_CHECKPOINTS_7_DDL)
    conn.execute(
        f"INSERT INTO checkpoints_new ({names})"
        f" SELECT {names} FROM checkpoints ORDER BY generation_order"
    )
    conn.execute("DROP TABLE checkpoints")
    conn.execute("ALTER TABLE checkpoints_new RENAME TO checkpoints")
    for statement in _CHECKPOINTS_7_INDEXES:
        conn.execute(statement)
    if sequence is not None:
        updated = conn.execute(
            "UPDATE sqlite_sequence SET seq=? WHERE name='checkpoints'",
            (sequence[0],),
        )
        if updated.rowcount == 0:
            conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES ('checkpoints', ?)",
                (sequence[0],),
            )


def _migrate_6_to_7(conn: sqlite3.Connection) -> None:
    """Idle trigger vocabulary, checkpoint search index, declaration drop.

    Three changes that each needed a stopped instance, which is why they ride
    one step. The runner owns the transaction, the backup, ``user_version``
    and both validations; this only moves schema and data.
    """
    from infra.db_fts import _init_checkpoints_fts

    _rebuild_checkpoints_with_idle_trigger(conn)
    # After the rebuild, so the triggers attach to the final table.
    _init_checkpoints_fts(conn)
    # External-content FTS starts empty; this indexes the rows that already
    # exist, so search covers history rather than only what follows.
    conn.execute("INSERT INTO checkpoints_fts(checkpoints_fts) VALUES('rebuild')")
    # Writer and readers removed by #1417; only pre-release rows remained.
    conn.execute("DROP TABLE IF EXISTS thread_context_declarations")


MIGRATIONS[6] = _migrate_6_to_7


def _migrate_7_to_8(conn: sqlite3.Connection) -> None:
    """Executor-bound plan admission: attempts, dispatches, holds, commitments.

    Lives in its own module because it rebuilds three tables and converts
    plan lineage data; see ``infra.migration_7_to_8_plans``.
    """
    from infra.migration_7_to_8_plans import migrate_7_to_8

    migrate_7_to_8(conn)


MIGRATIONS[7] = _migrate_7_to_8


MIGRATIONS[8] = migrate_8_to_9


def _migrate_9_to_10(conn: sqlite3.Connection) -> None:
    """Add producer-owned idle checkpoint due state to retained Turns."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(active_turns)")}
    if "idle_checkpoint_pending" not in columns:
        conn.execute(
            "ALTER TABLE active_turns ADD COLUMN idle_checkpoint_pending "
            "INTEGER NOT NULL DEFAULT 0 CHECK (typeof(idle_checkpoint_pending)='integer' "
            "AND idle_checkpoint_pending IN (0,1))"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_active_turns_idle_checkpoint_due "
        "ON active_turns(ended_at,turn_order) WHERE idle_checkpoint_pending=1"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS checkpoints_idle_boundary "
        "ON checkpoints(thread_id,history_epoch,through_message_id) "
        "WHERE trigger='idle'"
    )


MIGRATIONS[9] = _migrate_9_to_10
MIGRATIONS[10] = migrate_10_to_11


def _migrate_11_to_12(conn: sqlite3.Connection) -> None:
    """Seek one provider-write attempt in FIFO order during settlement."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_delivery_attempt "
        "ON messages(delivery_attempt_id, input_order) "
        "WHERE delivery_attempt_id IS NOT NULL"
    )


MIGRATIONS[11] = _migrate_11_to_12


def _migrate_12_to_13(conn: sqlite3.Connection) -> None:
    """Seek one active Turn's owned inputs in FIFO order at terminal."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_active_turn "
        "ON messages(active_turn_id, input_order) "
        "WHERE active_turn_id IS NOT NULL"
    )


MIGRATIONS[12] = _migrate_12_to_13


def _migrate_13_to_14(conn: sqlite3.Connection) -> None:
    """Add the retained Turn's durable pending-usage handoff."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(active_turns)")}
    if "usage_receipts_json" not in columns:
        conn.execute(
            "ALTER TABLE active_turns ADD COLUMN usage_receipts_json "
            "TEXT NOT NULL DEFAULT '{}' CHECK ("
            "json_valid(usage_receipts_json) "
            "AND json_type(usage_receipts_json)='object')"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_active_turns_pending_usage "
        "ON active_turns(turn_order) WHERE usage_receipts_json <> '{}'"
    )


MIGRATIONS[13] = _migrate_13_to_14
MIGRATIONS[14] = migrate_14_to_15
MIGRATIONS[15] = migrate_15_to_16
MIGRATIONS[16] = migrate_16_to_17
MIGRATIONS[17] = migrate_17_to_18


def _migrate_18_to_19(conn: sqlite3.Connection) -> None:
    """Retire only derived summary search; preserve all saved source rows."""
    for trigger in ("summaries_fts_insert", "summaries_fts_update", "summaries_fts_delete"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute("DROP TABLE IF EXISTS summaries_fts")


MIGRATIONS[18] = _migrate_18_to_19


def _migrate_19_to_20(conn: sqlite3.Connection) -> None:
    """Give stored plan inputs the one context-event shape plan_wake now writes.

    Until #1728 a plan's wakes and decision answers were appended as ``user``
    rows, so transcripts showed them as the user's own words. New rows are
    system context-event wakes like every routed event; this rewrites the
    stored ones to match, so one shape is read everywhere. Only those rows
    move: activity receipts and usage rows already carry a ``type``.
    """
    conn.execute(
        """
        UPDATE messages
           SET role = 'system',
               metadata = json_set(
                   COALESCE(metadata, '{}'),
                   '$.type', 'context_event',
                   '$.event_disposition', 'wake',
                   '$.summary', substr(content, 1, instr(content || char(10), char(10)) - 1)
               )
         WHERE role = 'user'
           AND json_extract(metadata, '$.origin') = 'plan'
           AND json_extract(metadata, '$.type') IS NULL
        """
    )


MIGRATIONS[19] = _migrate_19_to_20
MIGRATIONS[21] = migrate_21_to_22


def _migrate_20_to_21(conn: sqlite3.Connection) -> None:
    """Index only conserved inputs that a checkpoint or cap change may release."""
    conn.execute(CHECKPOINT_HOLD_INDEX_SQL)


MIGRATIONS[20] = _migrate_20_to_21
MIGRATIONS[22] = migrate_22_to_23
MIGRATIONS[23] = migrate_23_to_24
MIGRATIONS[24] = migrate_24_to_25
MIGRATIONS[25] = migrate_25_to_26
MIGRATIONS[26] = migrate_26_to_27
MIGRATIONS[27] = migrate_27_to_28
MIGRATIONS[28] = migrate_28_to_29
MIGRATIONS[29] = migrate_29_to_30
MIGRATIONS[30] = migrate_30_to_31


def _migrate_31_to_32(conn: sqlite3.Connection) -> None:
    """Re-derive every stored plan parent from its owner's identity (#2077).

    Spawners are rehomed before the threads they spawned, so each new edge
    hangs off an ancestor chain that is already final and never trips the
    cycle check. A thread that names itself as spawner is a root. A plan
    whose owner thread is gone keeps its stored edge.
    """
    import infra.config as config
    from plans import public

    conn.row_factory = sqlite3.Row
    workspace = public.ensure_plan(
        "workspace_scope", config.WORKSPACE_SELECTION.workspace_id, db=conn,
    )
    for row in conn.execute(
        "SELECT id FROM plans WHERE owner_kind='project_scope'"
    ).fetchall():
        public.set_parent_plan(row["id"], workspace.id, db=conn)
    rows = conn.execute(
        """WITH RECURSIVE spawn(id, depth) AS (
             SELECT id, 0 FROM threads
             WHERE spawned_by IS NULL OR spawned_by=id
               OR spawned_by NOT IN (SELECT id FROM threads)
             UNION ALL
             SELECT t.id, spawn.depth + 1 FROM threads t JOIN spawn
               ON t.spawned_by=spawn.id AND t.id<>spawn.id
           )
           SELECT p.id, p.owner_id FROM plans p JOIN spawn ON spawn.id=p.owner_id
           WHERE p.owner_kind='thread' ORDER BY spawn.depth, p.id"""
    ).fetchall()
    for row in rows:
        derived = public.derive_parent_plan_id("thread", row["owner_id"], db=conn)
        public.set_parent_plan(row["id"], derived, db=conn)


MIGRATIONS[31] = _migrate_31_to_32


def _migrate_32_to_33(conn: sqlite3.Connection) -> None:
    """Add the validated provider-reset identity to the existing liveness row."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(plan_liveness_wakes)")}
    if "provider_reset_at" not in columns:
        conn.execute("ALTER TABLE plan_liveness_wakes ADD COLUMN provider_reset_at TEXT")
    if "provider_reset_turn_id" not in columns:
        conn.execute(
            "ALTER TABLE plan_liveness_wakes ADD COLUMN provider_reset_turn_id TEXT"
        )


MIGRATIONS[32] = _migrate_32_to_33
MIGRATIONS[33] = migrate_33_to_34
MIGRATIONS[34] = migrate_34_to_35


def _migrate_35_to_36(conn: sqlite3.Connection) -> None:
    """Materialize searchable frozen entry revisions for entry-level retrieval."""
    for statement in CHECKPOINT_ENTRY_SEARCH_SCHEMA_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
    _init_checkpoint_entry_search_fts(conn)
    backfill_checkpoint_entry_search(conn)


MIGRATIONS[35] = _migrate_35_to_36
MIGRATIONS[36] = migrate_36_to_37


_CHECKPOINTS_38_DDL = """
CREATE TABLE checkpoints_new (
    generation_order INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    history_epoch INTEGER NOT NULL,
    previous_generation_id TEXT,
    through_seq INTEGER NOT NULL,
    through_message_id TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('manual','pressure','model_switch','idle','agent')),
    mode TEXT NOT NULL CHECK (mode IN ('warm','cold')),
    status TEXT NOT NULL CHECK (
        status IN ('running','captured','installed','overtaken','failed','cancelled')
    ),
    source_harness TEXT NOT NULL,
    source_model TEXT NOT NULL,
    writer_harness TEXT NOT NULL,
    writer_model TEXT NOT NULL,
    originating_turn_id TEXT,
    first_included_seq INTEGER,
    continuity_base_revision INTEGER NOT NULL,
    continuity_base_snapshot_json TEXT NOT NULL,
    raw_result TEXT,
    continuity_snapshot_json TEXT,
    usage_json TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    captured_at TEXT,
    installed_at TEXT,
    ended_at TEXT
)
"""


def _migrate_37_to_38(conn: sqlite3.Connection) -> None:
    """Add the persisted agent-requested checkpoint trigger."""
    sequence = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='checkpoints'"
    ).fetchone()
    columns = [row[1] for row in conn.execute("PRAGMA table_info(checkpoints)")]
    names = ",".join(f'"{column}"' for column in columns)
    conn.execute(_CHECKPOINTS_38_DDL)
    conn.execute(
        f"INSERT INTO checkpoints_new ({names}) "
        f"SELECT {names} FROM checkpoints ORDER BY generation_order"
    )
    for trigger in (
        "checkpoints_fts_insert",
        "checkpoints_fts_delete",
        "checkpoints_fts_update",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute("DROP TABLE IF EXISTS checkpoints_fts")
    conn.execute("DROP TABLE checkpoints")
    conn.execute("ALTER TABLE checkpoints_new RENAME TO checkpoints")
    for statement in _CHECKPOINTS_7_INDEXES:
        conn.execute(statement)
    conn.execute(
        "CREATE INDEX checkpoints_listing "
        "ON checkpoints(thread_id,generation_order DESC)"
    )
    conn.execute(
        "CREATE INDEX checkpoints_idle_boundary "
        "ON checkpoints(thread_id,history_epoch,through_message_id) "
        "WHERE trigger='idle'"
    )
    from infra.db_fts import _init_checkpoints_fts

    _init_checkpoints_fts(conn)
    conn.execute("INSERT INTO checkpoints_fts(checkpoints_fts) VALUES('rebuild')")
    if sequence is not None:
        updated = conn.execute(
            "UPDATE sqlite_sequence SET seq=? WHERE name='checkpoints'",
            (sequence[0],),
        )
        if updated.rowcount == 0:
            conn.execute(
                "INSERT INTO sqlite_sequence(name,seq) VALUES('checkpoints', ?)",
                (sequence[0],),
            )


MIGRATIONS[37] = _migrate_37_to_38
MIGRATIONS[38] = migrate_38_to_39
MIGRATIONS[39] = migrate_39_to_40
MIGRATIONS[40] = migrate_40_to_41
MIGRATIONS[41] = migrate_41_to_42
MIGRATIONS[42] = migrate_42_to_43


def _migrate_43_to_44(conn: sqlite3.Connection) -> None:
    """Copy the workspace bootloader setting under its renamed key.

    The old ``system_context`` row is kept only so reverted code still finds
    its list; it is never read. Drop it in the release after.
    """
    conn.execute(
        "INSERT OR IGNORE INTO settings(key, value, updated_at) "
        "SELECT 'workspace_context', value, updated_at "
        "FROM settings WHERE key = 'system_context'"
    )


MIGRATIONS[43] = _migrate_43_to_44
MIGRATIONS[44] = migrate_44_to_45
MIGRATIONS[45] = migrate_45_to_46
MIGRATIONS[46] = migrate_46_to_47
MIGRATIONS[47] = migrate_47_to_48
MIGRATIONS[48] = migrate_48_to_49
MIGRATIONS[49] = migrate_49_to_50
MIGRATIONS[50] = migrate_50_to_51
MIGRATIONS[51] = migrate_51_to_52
MIGRATIONS[52] = migrate_52_to_53
MIGRATIONS[53] = migrate_53_to_54
MIGRATIONS[54] = migrate_54_to_55
MIGRATIONS[55] = migrate_55_to_56
MIGRATIONS[56] = migrate_56_to_57
MIGRATIONS[57] = migrate_57_to_58
MIGRATIONS[58] = migrate_58_to_59
MIGRATIONS[59] = migrate_59_to_60
MIGRATIONS[60] = migrate_60_to_61
MIGRATIONS[61] = migrate_61_to_62
MIGRATIONS[62] = migrate_62_to_63
MIGRATIONS[63] = migrate_63_to_64
MIGRATIONS[64] = migrate_64_to_65
MIGRATIONS[65] = migrate_65_to_66
MIGRATIONS[66] = migrate_66_to_67
MIGRATIONS[67] = migrate_67_to_68
MIGRATIONS[68] = migrate_68_to_69
MIGRATIONS[69] = migrate_69_to_70
MIGRATIONS[70] = migrate_70_to_71
MIGRATIONS[71] = migrate_71_to_72
MIGRATIONS[72] = migrate_72_to_73
MIGRATIONS[73] = migrate_73_to_74
MIGRATIONS[74] = migrate_74_to_75
MIGRATIONS[75] = migrate_75_to_76
MIGRATIONS[76] = migrate_76_to_77
MIGRATIONS[77] = migrate_77_to_78
MIGRATIONS[78] = migrate_78_to_79
MIGRATIONS[79] = migrate_79_to_80
MIGRATIONS[80] = migrate_80_to_81
MIGRATIONS[81] = migrate_81_to_82
MIGRATIONS[82] = migrate_82_to_83
MIGRATIONS[83] = migrate_83_to_84
MIGRATIONS[84] = migrate_84_to_85
MIGRATIONS[85] = migrate_85_to_86
MIGRATIONS[86] = migrate_86_to_87
MIGRATIONS[87] = migrate_87_to_88
MIGRATIONS[88] = migrate_88_to_89
MIGRATIONS[89] = migrate_89_to_90
MIGRATIONS[90] = migrate_90_to_91
MIGRATIONS[91] = migrate_91_to_92
MIGRATIONS[92] = migrate_92_to_93
MIGRATIONS[93] = migrate_93_to_94
MIGRATIONS[94] = migrate_94_to_95
MIGRATIONS[95] = migrate_95_to_96
MIGRATIONS[96] = migrate_96_to_97
MIGRATIONS[97] = migrate_97_to_98
MIGRATIONS[98] = migrate_98_to_99
MIGRATIONS[99] = migrate_99_to_100
MIGRATIONS[100] = migrate_100_to_101
MIGRATIONS[101] = migrate_101_to_102
MIGRATIONS[102] = migrate_102_to_103
MIGRATIONS[103] = migrate_103_to_104
MIGRATIONS[104] = migrate_104_to_105
MIGRATIONS[105] = migrate_105_to_106
MIGRATIONS[106] = migrate_106_to_107
MIGRATIONS[107] = migrate_107_to_108
MIGRATIONS[MCP_SECRETS_FROM_VERSION] = migrate_mcp_secrets
MIGRATIONS[109] = migrate_109_to_110
MIGRATIONS[110] = migrate_110_to_111
MIGRATIONS[111] = migrate_111_to_112
MIGRATIONS[112] = migrate_112_to_113
MIGRATIONS[113] = migrate_113_to_114
MIGRATIONS[114] = migrate_114_to_115
MIGRATIONS[115] = migrate_115_to_116
MIGRATIONS[116] = migrate_116_to_117
MIGRATIONS[117] = migrate_117_to_118
MIGRATIONS[118] = migrate_118_to_119
MIGRATIONS[119] = migrate_119_to_120
MIGRATIONS[120] = migrate_120_to_121
MIGRATIONS[121] = migrate_121_to_122
MIGRATIONS[122] = migrate_122_to_123
MIGRATIONS[123] = migrate_123_to_124
MIGRATIONS[124] = migrate_124_to_125
MIGRATIONS[125] = migrate_125_to_126
MIGRATIONS[126] = migrate_126_to_127
MIGRATIONS[127] = migrate_127_to_128
MIGRATIONS[128] = migrate_128_to_129
MIGRATIONS[129] = migrate_129_to_130
MIGRATIONS[130] = migrate_130_to_131
MIGRATIONS[131] = migrate_131_to_132
MIGRATIONS[132] = migrate_132_to_133
MIGRATIONS[133] = migrate_133_to_134
MIGRATIONS[134] = migrate_134_to_135
MIGRATIONS[135] = migrate_135_to_136
MIGRATIONS[136] = migrate_136_to_137
MIGRATIONS[137] = migrate_137_to_138
MIGRATIONS[138] = migrate_138_to_139
MIGRATIONS[139] = migrate_139_to_140
MIGRATIONS[140] = migrate_140_to_141
MIGRATIONS[141] = migrate_141_to_142
MIGRATIONS[142] = migrate_142_to_143
MIGRATIONS[143] = migrate_143_to_144
MIGRATIONS[144] = migrate_144_to_145
MIGRATIONS[145] = migrate_145_to_146
MIGRATIONS[146] = migrate_146_to_147
MIGRATIONS[147] = migrate_147_to_148
MIGRATIONS[148] = migrate_148_to_149
MIGRATIONS[149] = migrate_149_to_150
MIGRATIONS[150] = migrate_150_to_151
MIGRATIONS[151] = migrate_151_to_152
MIGRATIONS[152] = migrate_152_to_153
MIGRATIONS[153] = migrate_153_to_154
MIGRATIONS[154] = migrate_154_to_155
MIGRATIONS[155] = migrate_155_to_156
MIGRATIONS[156] = migrate_156_to_157
MIGRATIONS[157] = migrate_157_to_158
MIGRATIONS[158] = migrate_158_to_159
MIGRATIONS[159] = migrate_159_to_160
MIGRATIONS[160] = migrate_160_to_161


def _connection_database_path(conn: sqlite3.Connection) -> Path | None:
    """Return the file this connection actually has open, or None if memory.

    Resolved from the connection rather than from configuration: the database
    that gets backed up must be the one being migrated, and a path resolved a
    second way can differ from the one already open.
    """
    for _, name, filename in conn.execute("PRAGMA database_list"):
        if name == "main":
            return Path(filename) if filename else None
    return None


def _database_bytes(source: Path) -> int:
    """Total bytes the backup must reproduce, including any WAL not yet merged."""
    total = source.stat().st_size
    wal = source.with_name(source.name + "-wal")
    if wal.exists():
        total += wal.stat().st_size
    return total


def _backup_room_state(source: Path) -> tuple[int, int, list[Path]]:
    """Share the preflight and migration room calculation so their decisions agree."""
    needed = _database_bytes(source)
    required = int(needed * _BACKUP_MIN_FREE_RATIO) + _BACKUP_FREE_FLOOR_BYTES
    return required, shutil.disk_usage(source.parent).free, pre_migration_backups(source)[:-1]


def backup_room(source: Path) -> tuple[int, int]:
    """Share preflight/migration available-room accounting without changing the filesystem."""
    required, free, stale = _backup_room_state(source)
    return required, free + sum(path.stat().st_size for path in stale)


def _backup_room_error(
    source: Path, from_version: int, required: int, available: int,
) -> BackupRoomError:
    """Describe a shared preflight or migration refusal with operator recovery steps."""
    needed = _database_bytes(source)
    return BackupRoomError(
        f"not enough free space for the pre-migration backup of schema "
        f"v{from_version}: {available / 1e9:.2f} GB free, {required / 1e9:.2f} GB "
        f"required to copy a {needed / 1e9:.2f} GB database and keep a "
        f"{_BACKUP_FREE_FLOOR_BYTES / 1e9:.2f} GB reserve. Free space, then "
        f"start again; the database is unchanged. Superseded backups in "
        f"{source.parent} are the usual place to reclaim it.",
        required,
        available,
    )


def require_backup_room(source: Path, from_version: int) -> tuple[int, int]:
    """Refuse before copying when the backup and reserve cannot fit.

    The reserve is required after the copy: a crash-restart attempt is a fresh,
    uncontested lock winner, so nothing else limits how many copies it can
    write. Refusing startup is the honest outcome because migrating without a
    backup is unsafe.
    """
    required, available = backup_room(source)
    if available < required:
        raise _backup_room_error(source, from_version, required, available)
    return required, available


def pre_migration_backups(source: Path) -> list[Path]:
    """This database's pre-migration backups, oldest first.

    Matches only ``<db name>.pre-migration-v<digits>-<UTC stamp>.bak`` beside
    the database, so the live file, its ``-wal``/``-shm``, other databases'
    backups, ``.archive-*`` files and the ``backups/`` directory never match.
    Ordered by the stamp in the name, not mtime, which a restore or copy
    rewrites.
    """
    pattern = re.compile(
        re.escape(source.name) + r"\.pre-migration-v(\d+)-(\d{8}T\d{6}Z)\.bak"
    )
    found: list[tuple[str, int, Path]] = []
    for path in source.parent.iterdir():
        match = pattern.fullmatch(path.name)
        if match and path.is_file():
            found.append((match.group(2), int(match.group(1)), path))
    return [path for _, _, path in sorted(found)]


def _delete_backups(victims: list[Path], reason: str) -> None:
    """Delete backups, logging one INFO line with the count and bytes reclaimed."""
    removed = 0
    reclaimed = 0
    for path in victims:
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError as error:
            log.warning("Could not remove pre-migration backup %s: %s", path, error)
            continue
        removed += 1
        reclaimed += size
    if removed:
        log.info(
            "Pruned %d pre-migration backup(s), reclaimed %d bytes (%s)",
            removed, reclaimed, reason,
        )


def _prune_backups(source: Path, keep_path: Path | None) -> None:
    """After a verified migration, keep the newest N backups and drop the rest."""
    backups = pre_migration_backups(source)
    survivors = backups[-_PRE_MIGRATION_BACKUPS_KEEP:]
    victims = [path for path in backups if path not in survivors and path != keep_path]
    _delete_backups(victims, "retention after migration")


def _ensure_backup_room(source: Path, from_version: int) -> None:
    """Prune oldest superseded backups, never the newest, before copying.

    Only backups credited by the shared preflight calculation are removed, and
    only until the backup plus reserve fits. If even that cannot make room,
    refusing is safer than migrating without a backup.
    """
    required, free, stale = _backup_room_state(source)
    available = free + sum(path.stat().st_size for path in stale)
    if available < required:
        raise _backup_room_error(source, from_version, required, available)
    if free >= required:
        return
    removed: list[Path] = []
    reclaimed = 0
    for path in stale:
        reclaimed += path.stat().st_size
        path.unlink()
        removed.append(path)
        if shutil.disk_usage(source.parent).free >= required:
            break
    log.info(
        "Pruned %d pre-migration backup(s), reclaimed %d bytes (making room for the backup)",
        len(removed), reclaimed,
    )
    require_backup_room(source, from_version)


def _write_backup(conn: sqlite3.Connection, from_version: int) -> Path | None:
    """Copy the database beside itself, called only by the migration's winner.

    Runs while the caller holds the write lock, which is what makes it safe:
    exactly one starter reaches here, so two of them cannot race on the backup
    filename or overwrite each other's copy with a *post*-migration snapshot —
    a backup that exists, looks valid, and is useless.

    The copy is read through a second connection rather than the caller's.
    SQLite's backup API deadlocks against an open write transaction on the same
    connection, and a separate reader cannot see the caller's uncommitted
    changes, so it captures exactly the pre-migration state.

    Returns the backup path, or None for an in-memory database with no file.
    """
    source = _connection_database_path(conn)
    if source is None:
        return None
    _ensure_backup_room(source, from_version)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = source.with_name(f"{source.name}.pre-migration-v{from_version}-{stamp}.bak")
    if target.exists():
        raise MigrationError(f"backup path already exists, refusing to overwrite: {target}")
    # The copy holds whatever the database holds, credentials included before
    # the key-store migrations move them, so only the owner may read it.
    os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    reader = sqlite3.connect(source, timeout=_BACKUP_TIMEOUT_SECONDS)
    try:
        backup = sqlite3.connect(target)
        try:
            # Stepwise so the copy publishes its progress. Each step takes and
            # drops its own read lock, which would restart the copy if another
            # connection committed in between; none can, because the caller
            # holds the write lock, so the result is the same single snapshot.
            reader.backup(
                backup, pages=_BACKUP_STEP_PAGES, progress=startup_progress.backup_step,
            )
        finally:
            backup.close()
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        reader.close()
    return target


def _plan(from_version: int, to_version: int) -> list[int]:
    """Return the ordered from-versions to apply, or raise if unreachable."""
    if from_version > to_version:
        raise MigrationError(
            f"database schema version {from_version} is newer than this build's"
            f" {to_version}; a newer CORSIA wrote it, so this build must not"
            " downgrade it. Deploy the newer build instead."
        )
    steps: list[int] = []
    version = from_version
    while version < to_version:
        if version not in MIGRATIONS:
            raise MigrationError(
                f"no migration registered from schema version {version};"
                f" cannot reach {to_version} from {from_version}."
                " The database was not modified."
            )
        steps.append(version)
        version += 1
    return steps


def _schema_snapshot(
    conn: sqlite3.Connection,
) -> set[tuple[str, str, str, str | None, int]]:
    """Return the schema entries whose changes require table validation.

    ``rootpage`` is part of the entry so a table or index rebuilt with
    byte-identical DDL still counts as changed: its b-tree is new even when
    its definition text is not.
    """
    return {
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql, rootpage FROM sqlite_schema "
            "WHERE type IN ('table', 'index')"
        )
    }


def _changed_tables(
    before: set[tuple[str, str, str, str | None, int]],
    after: set[tuple[str, str, str, str | None, int]],
) -> list[str]:
    """Find tables created or changed by migration schema entries.

    A table entry names itself while an index entry names its owning table in
    ``tbl_name``. Removed entries do not need validation because they no
    longer describe a live table.
    """
    return sorted(
        {
            tbl_name
            for _entry_type, _name, tbl_name, _sql, _rootpage in after - before
            if tbl_name
        }
    )


def _validate(conn: sqlite3.Connection, changed_tables: list[str]) -> None:
    """Check the migrated database inside the transaction, before commit.

    Foreign keys are checked across the whole database; page structure and
    row-to-index agreement only for tables the migration created or rebuilt.
    Data-only steps rely on SQLite maintaining their indexes.

    No whole-database ``quick_check``: its b-tree walk is one VM operation, so
    on a 25 GB database it ran 61 s without a progress callback, past the
    desktop's 60 s startup stall, while checking pages no step touched.
    """
    broken = conn.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise MigrationError(f"foreign_key_check failed after migration: {broken[:5]}")
    for table in changed_tables:
        quoted_table = table.replace('"', '""')
        integrity = conn.execute(
            f'PRAGMA integrity_check("{quoted_table}")'
        ).fetchone()[0]
        if integrity != "ok":
            raise MigrationError(
                f"integrity_check({table}) failed after migration: {integrity}"
            )


def migrate_to_current(conn: sqlite3.Connection, target_version: int) -> MigrationResult:
    """Migrate an existing database forward to ``target_version``.

    Returns without touching the database when it is already current. Raises
    ``MigrationError`` — leaving the database unchanged — when the ladder
    cannot reach the target, when the database is newer than this build, or
    when post-migration validation fails. A needed migration requires a clean
    startup connection: changing foreign-key enforcement inside an existing
    transaction is ineffective, and committing that owner's work is forbidden.
    """
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current == target_version:
        return MigrationResult(False, current, target_version, None)

    # Refuse before any effect when the ladder cannot reach the target, so an
    # unreachable version never gets as far as taking the write lock.
    _plan(current, target_version)
    if conn.in_transaction:
        raise MigrationError("migration requires a clean startup connection")

    backup_path: Path | None = None
    foreign_keys_were_on = bool(
        conn.execute("PRAGMA foreign_keys").fetchone()[0]
    )
    previous_busy_timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    # Wait out a concurrent migration rather than dying on the ordinary
    # query-sized busy timeout. Restored in the finally below.
    conn.execute(f"PRAGMA busy_timeout={_MIGRATION_LOCK_WAIT_MS}")
    # Table rebuilds re-point foreign keys, and this pragma is a no-op inside a
    # transaction, so it has to be set here. `foreign_key_check` inside the
    # transaction is what proves the result sound.
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        with write_transaction(conn, scope="migrate_schema"):
            try:
                # Re-read under the write lock: a concurrent starter may have
                # migrated while this one waited for the lock.
                locked_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if locked_version == target_version:
                    log.info("Schema already migrated to %d by another start", target_version)
                    return MigrationResult(False, locked_version, target_version, None)

                # The lock makes this the only backup within one boot. It cannot
                # bound copies across boots: a crash-restart loop is a fresh
                # uncontested winner every time, which is why `require_backup_room`
                # reserves space the copy may not consume.
                startup_progress.mark(
                    "backing_up", from_version=locked_version, to_version=target_version,
                )
                backup_path = _write_backup(conn, locked_version)
                log.info(
                    "Migrating schema %d -> %d (backup: %s)",
                    locked_version, target_version, backup_path,
                )
                from plans.public import sanctioned_plan_write  # lazy: plans imports infra

                steps = _plan(locked_version, target_version)
                startup_progress.mark("migrating", done=0, total=len(steps))
                # Long statements and loops of short ones both reach the
                # handler, so a step that is still working keeps publishing.
                # Pooled connections already carry it; this covers a connection
                # opened before startup began publishing.
                startup_progress.watch(conn)
                before_schema = _schema_snapshot(conn)
                for done, version in enumerate(steps, start=1):
                    with sanctioned_plan_write():
                        MIGRATIONS[version](conn)
                    startup_progress.mark("migrating", done=done, total=len(steps))
                conn.execute(f"PRAGMA user_version={target_version}")
                startup_progress.mark("validating")
                _validate(conn, _changed_tables(before_schema, _schema_snapshot(conn)))
            except BaseException as error:
                # Only claim a backup that exists. The failure can now come from
                # the room check *before* any copy is made, and an in-memory
                # database never has one -- naming a path of `None` hands an
                # operator false recovery state at exactly the wrong moment.
                recovery = (
                    f" A pre-migration backup is at {backup_path}."
                    if backup_path is not None
                    else " No pre-migration backup was written."
                )
                raise MigrationError(
                    f"migration {current} -> {target_version} rolled back, database"
                    f" unchanged: {error}.{recovery}"
                ) from error
    finally:
        conn.execute(f"PRAGMA busy_timeout={previous_busy_timeout}")
        if foreign_keys_were_on:
            conn.execute("PRAGMA foreign_keys=ON")

    log.info("Schema migrated %d -> %d", current, target_version)
    source = _connection_database_path(conn)
    if backup_path is not None and source is not None:
        try:
            _prune_backups(source, backup_path)
        except OSError as error:
            log.warning("Pre-migration backup pruning failed: %s", error)
    return MigrationResult(True, current, target_version, backup_path)
