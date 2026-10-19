"""The Structured Plans public interface (#2217).

The only ``plans`` names code outside ``backend/plans`` may import, enforced
by ``tests/test_plans_public_boundary.py``. Every other ``plans`` module is
internal and may change shape without notice. A name is here because an
outside module needs it today; add one only with its first outside caller.

Import this module the way the caller needs: at the top of a module, or
inside a function where ``plans`` would otherwise form an import cycle. To
replace a function in a test for an outside caller, patch it here, since that
is where the caller looks it up.
"""

from __future__ import annotations

# Errors and node vocabulary.
from plans.decision_authz import DecisionClosed
from plans.decision_fields import DecisionInputError
from plans.operator_projection import OperatorNotFound
from plans.pane_views import PaneViewInputError
from plans.errors import PlanError, PlanInputError, RevisionConflict
from plans.service import (
    ASSIGNEES, MAX_NODE_TITLE, NODE_KINDS, _TERMINAL_STATES as TERMINAL_STATES,
)

# Plan tool and decision schemas (tools/plan_schemas, tools/plan_field_schemas).
from plans.boundary_fields import (
    HUMAN_OPERATIONS, MAX_ARCHIVE_NODES, OPERATION_FIELDS, OPERATION_REQUIRED_FIELDS,
)
from plans.scheduled_wait import PAUSE_OPERATIONS, pause_refusal

# Continuation sources (automation/continuation/sources/*).
from plans.scheduled_wait import wake_summary as scheduled_wake
from plans.event_wait import wake_summary as event_wake
from plans.decision_fields import (
    PICK_ANY, PICK_ONE, answer_note, chosen_ids, pick_of, recommended_ids,
    MAX_CONTEXT as DECISION_MAX_CONTEXT,
    MAX_OPTION_DETAIL as DECISION_MAX_OPTION_DETAIL,
    MAX_OPTION_LABEL as DECISION_MAX_OPTION_LABEL,
    MAX_OPTIONS as DECISION_MAX_OPTIONS,
    MAX_RATIONALE as DECISION_MAX_RATIONALE,
    MAX_TITLE as DECISION_MAX_TITLE,
    MAX_TRADEOFF as DECISION_MAX_TRADEOFF,
    MIN_OPTIONS as DECISION_MIN_OPTIONS,
    OPERATION_FIELDS as DECISION_OPERATION_FIELDS,
    OPERATION_REQUIRED_FIELDS as DECISION_OPERATION_REQUIRED_FIELDS,
    OPTION_IDS as DECISION_OPTION_IDS,
)

# Plan and node mutation and reads (tools/plans, api/plans).
from plans.boundary import PlanActor, ensure_current_plan, mutate, read_current_plan
from plans.current import addressed_to, read_checkpoint_status, read_current_status
from plans.decision_authz import resolve as resolve_decision
from plans.decisions import (
    answer as answer_decision,
    draft as draft_decision,
    invalidate as invalidate_decision,
    parent_review as parent_review_decision,
    raise_to_user as raise_decision,
    reorder as reorder_decision,
    update as update_decision,
)
from plans.user_settle import prepare as prepare_user_settlement
from plans.service import (
    _event as record_node_event,
    _node_row as node_row,
    ensure_plan, get_node, record_runner_event,
)
from plans.ask_endings import detail as ask_ending_detail
from plans.holds import clear_write as clear_hold_write
from plans.receipts import (
    load as load_mutation_receipt,
    record as record_mutation_receipt,
    request_fingerprint,
)
from plans.onboarding_reads import (
    decision_answer_actor,
    decision_owner_id,
    has_genuine_decision_raised_at_or_after,
    has_open_titled_node,
)

# Operator projections: the pane, chat list and attention (api/).
from plans.attention_detail import (
    annotate as annotate_attention,
    generic as generic_attention,
    queue as attention_queue,
)
from plans.activity_projection import (
    counted_plan_ids as activity_counted_plan_ids,
    read_project as read_activity_project,
    read_workspace as read_activity_workspace,
)
from plans.user_ask import (
    ASKABLE_TYPES,
    ASK_ENDINGS,
    AskRelease,
    ask_attention_state,
    ask_attention_state_sql,
    ask_linked_sql,
    ask_rounds,
    asked_artifact,
    cleared as ask_cleared,
    defer_asks,
    open_ask_artifacts,
    open_asks_sql,
    placed_ask_hold_sql,
    raised_asks,
    round_at,
    undefer_asks,
    user_ask_hold_sql,
)
from plans.user_ask_cards import ask_holds
from plans.decision_projection import DECISION_TYPE, identity_row as decision_identity_row
from plans.node_states import blocked_on_user_sql, failure_asks_user_sql, open_user_block_sql
from plans.folded_reads import read_folded, read_path as read_folded_path
from plans.operator_node_read import read_node as read_operator_node
from plans.operator_chat_state import THREAD_STATE_LIMIT, read_thread_states
from plans.operator_projection import (
    read_child as read_operator_child,
    read_project as read_operator_project,
    read_thread as read_operator_thread,
    read_workspace as read_operator_workspace,
    record_thread_view,
)
from plans.pane_views import delete_thread as delete_pane_view
from plans.review_evidence import latest as latest_review_evidence

# The thin status a checkpoint freezes (infra/checkpoint_snapshots).
from plans.close_next_bounds import bounded as bounded_label
from plans.status import cut_rows
from plans.status_context import render as render_status
# The status a plan write left owed: drained before a digest (infra/passive_changes)
# and swept at startup (app_lifecycle).
from plans.status_drain import drain_for_digest as drain_status_for_digest, sweep as sweep_owed_status
# Notifications raised by a plan write, driven after its commit.
from plans.notifications import (
    collect_touched, collect_wake_recipients, deliver as deliver_notifications, deliver_deferred, drive_after,
    drive_after_response, drive_deferred, drive_threads, notify_after_commit, prepare_after_commit,
)
from plans.nudge_scheduling import (
    schedule_after_commit, schedule_thread_after_commit, schedule_for_accepted_input, thread_plan_ids,
)

# Admission, selection, liveness and wakes (automation/, infra/).
from plans.wait_causes import stalled_why
from plans.admission import (
    project as project_admission,
    project_many as project_admission_many,
    reconcile_plan,
)
from plans.admission_attempts import latest_accepted_starts
from plans.admission_origin import registration_origin
from plans.models import PlanLivenessSignal
from plans.pause_authority import set_aside_while_paused, thread_paused_since
from plans.projections import liveness_signal, open_delegated_node_ids
from plans.plan_write import transaction as plan_transaction
from plans.selection_validity import selection_valid
from plans.turn_admission import snapshot_after_admission
from plans.wake_currency import KINDS as WAKE_KINDS, is_current as wake_is_current
from plans.watch_resolution import resolve_delivered_event

# Document feedback and shared-document ownership tasks (doceditor/, registry/).
from plans.artifact_links import link as link_node_artifact
from plans.feedback_nodes import (
    create_feedback_node, create_review_node, feedback_display, feedback_item_counts, load_feedback_items,
)
from plans.review_requests import (
    append_review_note, close_review_node, review_is_open, settle_review_node,
)
from plans.service import add_node, transition_node

# A step the system opens for the person and settles when it sees it done (host_access/browser).
from plans.system_user_steps import open_user_step, settle_user_step

# Lesson trial and captured-hypothesis reviews (core/lesson_*).
from plans.lesson_review_history import read_batch as lesson_review_history
from plans.lesson_reviews import (
    LessonOwnerError, ensure_capture_review, ensure_lesson_review,
    lesson_review_opened_at, lesson_review_refusal, open_capture_review_counts,
)

# Delegation and thread lifecycle (core/, tools/delegation, infra/thread_driver).
from plans.decision_lifecycle import withdraw_for_owner_thread_in_transaction
from plans.delegate_attach import (
    choose as choose_delegated_step,
    choose_for_send as choose_reused_worker_step,
    send_note as reused_worker_note,
    hand_over as hand_over_delegated_step,
    plan_task as delegated_plan_task,
)
from plans.delegation import harvest_ready_children, has_open_structured_attachment
from plans.delegation_child_lifecycle import (
    notify_silent_children, notify_stopped_open, notify_turn_error,
    on_child_thread_archived, on_child_thread_ended, silent_children,
)
from plans.delegation_turn_end import on_worker_turn_end
from plans.delegation_reply import plan_is_empty as plan_is_empty
from plans.delegation_needs import answer as answer_worker_needs
from plans.hierarchy import _set_parent as set_parent_plan, derive_parent_plan_id, rehome
from plans.upkeep_notice import is_plans_tool as is_plans_tool, notice_for as upkeep_notice_for

# Tool calls and model usage attributed to plan work (tools/, core/, subagents/, api/measurement_chain).
from plans.commitment_cost import project_many as project_commitment_costs
from plans.hierarchy import delegation_edge_sql, newest_attachment_sql
from plans.tool_dispatches import (
    record as record_tool_dispatch,
    settle as settle_tool_dispatch,
    settle_turn as settle_turn_dispatches,
)
from plans.usage_calls import (
    UNATTRIBUTED, binding_at_call_start, record_call_usage, record_observed_call_usage,
    record_turn_fallback, report_unrecordable_call,
    _measured as usage_is_measured,
    _report_defect as report_usage_defect,
)

# Frozen schema migrations (infra/migration_*).
from plans.archival import release_held, _require_no_open_children as require_no_open_children
from plans.blocked_wait import convert_hold as convert_blocked_hold
from plans.event_wait import give_deadline as give_event_wait_deadline
from plans.scheduled_wait import (
    MOVED_REASON as MOVED_PAUSE_REASON, is_agent_pause, lift_agent_plan_pause,
    schedule_pause as schedule_agent_pause, schedule_task as schedule_moved_task,
)
from plans.commitments import attached_parent_node
from plans.dependencies import move_dependents
from plans.lifecycle import archive_ended_owner
from plans.service import touch_node
from plans.admission import release as release_attempt
from plans.delegation_harvest import returned_steps as returned_worker_steps
from plans.delegation_owed_verdict import notify_owed_verdicts, owed_verdicts
from plans.hierarchy import shown_worker_plan
from plans.service import _transition as transition_node_in
from plans.worker_retirement import (
    CLOSED_WITH_DELEGATION, STOOD_DOWN,
    _suppress_open_steps as fail_open_worker_steps,
    close_open_steps as close_open_worker_steps,
)
from plans.aggregates_rebuild import rebuild as rebuild_plan_aggregates
from plans.aggregates_recount import stored_mismatches as plan_aggregate_mismatches
from plans.node_states import is_container_sql as container_sql
from plans.writer_context import sanctioned_write as sanctioned_plan_write
