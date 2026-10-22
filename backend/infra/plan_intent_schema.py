"""Schema fragment for task intent and its append-only supporting words."""

PLAN_INTENT_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS plan_node_intent_words (
        node_id TEXT NOT NULL REFERENCES plan_nodes(id) ON DELETE CASCADE,
        author TEXT NOT NULL CHECK (author IN ('person', 'agent')),
        source_kind TEXT NOT NULL CHECK (
            source_kind IN ('message', 'doc_comment', 'plan_decision_answer', 'plan_edit')
        ),
        source_id TEXT NOT NULL,
        quote TEXT NOT NULL CHECK (length(trim(quote)) > 0),
        role TEXT NOT NULL CHECK (role IN ('origin', 'bears_on', 'authority')),
        requirements_revision INTEGER NOT NULL CHECK (requirements_revision > 0),
        added_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_plan_node_intent_words_node
        ON plan_node_intent_words(node_id);
"""
