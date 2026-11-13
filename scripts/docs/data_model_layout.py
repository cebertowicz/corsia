"""Subsystem ownership and connected diagram grouping for the data model."""
from collections import defaultdict
from pathlib import Path

# The shared schema.py contains several unrelated domains; its tables are
# assigned separately below. All other modules have one explicit subsystem.
MODULE_GROUPS = {
    "Threads and messages": "message_schema thread_updates_schema held_replies_schema presence_schema presence_rollup_schema chat_comment_schema continuation_schema delivery_foundation_schema",
    "Plans": "plan_schema plan_aggregates_schema plan_events_schema plan_intent_schema plan_liveness_schema plan_markdown_import_schema plan_pane_views_schema plan_status_schema unattributed_dispatch_schema",
    "Events and webhooks": "bulletin_schema event_intake_schema event_rule_schema event_watch_schema webhook_delivery_schema",
    "Documents and artifacts": "artifact_identity_schema artifact_relocation_schema doc_comments_schema doc_feedback_schema doc_review_schema",
    "Accounts and connections": "accounts_schema action_drafts_schema connected_accounts_schema connected_apps_schema connection_schema email_screens_schema key_store_schema vendor_account_schema",
    "Usage and measurement": "usage_schema usage_rollup_schema outcome_events_schema measure_keys_schema measurement_schema measurement_prepared",
    "Context and memory": "checkpoint_schema continuity_schema continuity_staging context_change_schema fact_extraction_schema lesson_placement_check_schema learning_schema",
    "Settings and policy": "settings_schema project_policy_schema vendor_policy_schema",
    "Host access": "host_access_schema",
    "Providers and runtime": "limit_history_schema managed_process_schema provider_availability_schema",
    "Store internals": "db_fts sqlite-internal",
}
BASE_TABLE_GROUPS = {
    "Threads and messages": "threads active_turns",
    "Context and memory": "context_bootloader_events",
    "Settings and policy": "projects",
    "Providers and runtime": "model_profiles models network_servers network_server_endpoints restart_wake_registrations self_restart_requests",
    "Events and webhooks": "dead_letter_events",
    "Documents and artifacts": "artifact_registrations durable_write_intents artifact_registration_events artifact_reconcile_receipts",
}
STORE_GROUPS = {"document": "Document store", "logs": "Log store", "analytics": "Analytics store"}


def module_group(module):
    """Return the explicit subsystem for a module, failing on missing entries."""
    stem = "sqlite-internal" if module == "SQLite internal" else Path(module).stem
    expected = {"sqlite-internal": "SQLite internal",
                "measurement_prepared": "backend/api/measurement_prepared.py"}.get(
                    stem, f"backend/infra/{stem}.py")
    matches = [group for group, names in MODULE_GROUPS.items()
               if module == expected and stem in names.split()]
    if len(matches) != 1:
        raise ValueError(f"Schema module must have exactly one subsystem: {module}")
    return matches[0]


def subsystem(store, module, table):
    if store in STORE_GROUPS:
        return STORE_GROUPS[store]
    if module == "backend/infra/schema.py":
        matches = [group for group, names in BASE_TABLE_GROUPS.items() if table in names.split()]
        if len(matches) != 1:
            raise ValueError(f"Base schema table must have exactly one subsystem: {table}")
        return matches[0]
    return module_group(module)


def regroup(groups):
    result = defaultdict(dict)
    modules = defaultdict(set)
    for (store, module), tables in sorted(groups.items()):
        for name, info in tables.items():
            group = subsystem(store, module, name)
            if name in result[group]:
                raise ValueError(f"Duplicate table in subsystem {group}: {name}")
            result[group][name] = {**info, "module": module}
            modules[group].add(module)
    return dict(result), dict(modules)


def connected_diagrams(tables, max_tables=8):
    """Pack foreign-key edges into connected diagrams without dropping edges."""
    remaining = {(name, target) for name, info in tables.items()
                 for _, target, _ in info["foreign"] if target in tables}
    diagrams = []
    while remaining:
        first = min(remaining)
        nodes = set(first)
        remaining.remove(first)
        while True:
            candidates = sorted(edge for edge in remaining
                                if nodes.intersection(edge) and len(nodes.union(edge)) <= max_tables)
            if not candidates:
                break
            edge = candidates[0]
            nodes.update(edge)
            remaining.remove(edge)
        diagrams.append({name: tables[name] for name in sorted(nodes)})
    return diagrams
