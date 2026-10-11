"""Shape of the synthetic fixture: volumes, skew and the seed.

Production volumes are the counts-only census in the project's volume
reference (schema version 96, 2026-10-08). Every table with 1,000 or more rows
is built at ``SCALE`` of production; smaller tables keep their production
count because they are cheap and several routes list them whole. Nothing here
is production data: every row is generated from ``SEED``.
"""

from __future__ import annotations

from bisect import bisect_left

SEED = 20261008
SCALE = 0.1

#: Rows per table in production (census 2026-10-08), tables with 1,000+ rows.
PRODUCTION_ROWS: dict[str, int] = {
    "messages": 2_158_258, "llm_usage": 719_794, "plan_tool_dispatches": 340_199,
    "artifact_registration_events": 166_672, "context_change_journal": 115_535,
    "plan_node_events": 113_283, "plan_mutation_receipts": 75_942,
    "artifact_registrations": 65_236, "active_turns": 62_682,
    "webhook_deliveries": 50_891, "artifact_identities": 48_370,
    "continuation_triggers": 24_720, "checkpoint_entry_search": 23_574,
    "checkpoint_staged_edits": 21_703, "guidance_loads": 17_934, "plan_nodes": 15_467,
    "plan_unattributed_dispatches": 15_434, "continuity_entries": 15_093,
    "plan_node_attempts": 13_793, "dead_letter_events": 11_783, "plan_events": 10_384,
    "host_access_action_reviews": 8_808, "outcome_events": 7_593,
    "expected_event_watches": 7_519, "checkpoints": 6_708, "threads": 5_533,
    "context_manifests": 4_028, "artifact_reconcile_receipts": 3_995,
    "thread_vendor_accounts": 3_741, "managed_process_handles": 3_310, "plans": 2_913,
    "plan_node_artifact_links": 2_518, "host_access_leases": 2_484,
    "plan_node_dependencies": 2_056, "thread_updates": 1_926, "presence_visits": 1_480,
    "reviewer_gate_state": 1_417, "restart_wake_registrations": 1_286,
    "doc_comments": 1_142, "plan_node_holds": 1_017,
}

#: Tables under 1,000 rows keep their production count.
SMALL_ROWS: dict[str, int] = {
    "plan_liveness_wakes": 966, "continuity_store": 832, "automation_schedules": 385,
    "self_restart_requests": 338, "thread_vendor_account_holds": 169,
    "doc_feedback_batch_items": 106, "fact_extraction_runs": 68,
    "doc_feedback_batches": 63, "context_bootloader_events": 39,
    "doc_comment_thread_views": 39, "thread_presence": 31, "plan_pane_views": 22,
    "projects": 20, "model_profiles": 18, "network_server_endpoints": 12,
    "system_bulletins": 11, "doc_review_requests": 9, "project_repo_policies": 7,
}

#: The one thread at the largest production parent, and its sibling relations.
MAX_THREAD_MESSAGES = 100_639
MAX_THREAD_USAGE = 36_834
MAX_PLAN_NODES = 671

#: (quantile, rows) knots of rows-per-parent in production; log-linear between.
MESSAGE_KNOTS = ((0.0, 1), (0.5, 107), (0.9, 558), (0.99, 3_524), (1.0, 4_500))
USAGE_KNOTS = ((0.0, 1), (0.5, 26), (0.9, 228), (0.99, 1_149), (1.0, 1_600))
NODE_KNOTS = ((0.0, 1), (0.5, 2), (0.9, 8), (0.99, 44), (1.0, 70))


def target_rows(table: str) -> int | None:
    """Rows this fixture builds for ``table``; None when the census has no figure."""
    if table in PRODUCTION_ROWS:
        return max(1, round(PRODUCTION_ROWS[table] * SCALE))
    return SMALL_ROWS.get(table)


def quantile_rows(knots: tuple[tuple[float, int], ...], count: int) -> list[int]:
    """Deterministic per-parent row counts matching ``knots`` for ``count`` parents.

    Parent ``k`` sits at quantile ``(k + 0.5) / count`` and gets the log-linear
    interpolation between the surrounding knots, so p50/p90/p99 land on the
    census values without any randomness. Returned smallest first.
    """
    from math import exp, log

    quantiles = [q for q, _ in knots]
    out = []
    for k in range(count):
        q = (k + 0.5) / count
        hi = min(max(bisect_left(quantiles, q), 1), len(knots) - 1)
        (q0, v0), (q1, v1) = knots[hi - 1], knots[hi]
        out.append(max(1, round(exp(log(v0) + (log(v1) - log(v0)) * (q - q0) / (q1 - q0)))))
    return out
