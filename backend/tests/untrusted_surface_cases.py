"""Explicit non-transcript/mutation dispositions for the registry boundary walk.

New matching tools must be driven or given a reviewed disposition here. The read
set itself comes from the real registry and routes, not from this inventory.
"""

NON_TRANSCRIPT_TOOLS = {
    "web_search": "External web search; cannot address CORSIA transcript storage.",
    "skill_library": "Skill package discovery; confined workers cannot publish packages.",
    "corsia_api": "Driven by the enumerated real-route walk in the same test module.",
    "coordination_instruction_file_create": "Mutation; confined workers cannot write instructions.",
    "coordination_instruction_file_replace": "Mutation; confined workers cannot write instructions.",
    "agent_roster_update": "Mutation; confined workers cannot edit roster state.",
    "process_discover": "Host-process query; no access to stored transcripts.",
    "search_knowledge": "Knowledge store; confined workers cannot write knowledge.",
    "artifact_register": "Mutation; confined workers cannot register artifacts.",
    "artifact_update": "Mutation; confined workers cannot update artifacts.",
    "artifact_rehome": "Mutation; confined workers cannot rehome artifacts.",
    "artifact_release": "Mutation; confined workers cannot release artifacts.",
    "delegate_create_thread": "Mutation; confined workers cannot create threads.",
    "delegate_send": "Mutation; confined workers cannot send intermediate content.",
    "delegate_set_model": "Mutation; confined workers cannot switch models.",
    "event_rules": "Rule/event state; confined workers cannot create rules or emit events.",
    "email_search": "External mailbox, not the CORSIA message store; no account used by this test.",
    "email_read": "External mailbox, not the CORSIA message store; no account used by this test.",
    "email_draft": "External mailbox mutation; no account used by this test.",
    "microsoft_mail_search": "External mailbox, not the CORSIA message store; no account used by this test.",
    "microsoft_mail_read": "External mailbox, not the CORSIA message store; no account used by this test.",
    "continuity_edit": "Own-thread mutation/review only; cannot read another thread by message id.",
}

# These readers have mutually exclusive selectors or an opaque content digest.
# They still run real tool implementations; only their argument construction differs.
TOOL_VARIANTS = {
    "get_checkpoints": (
        {"checkpoint_id": "surface-checkpoint"},
        {"thread_id": "isolated"},
    ),
    "search_history": (
        {"query": "outsidepayload1682", "include_checkpoints": True},
        {"query": "outsidepayload1682", "thread_id": "isolated", "include_checkpoints": True},
    ),
    "artifact_reconcile": ({"thread_id": "isolated", "mode": "preview"},),
    "coordination_instruction_file_read": ({"target_thread_id": "isolated"},),
}
