"""A schema-78 store that holds every owner the 78 -> 79 migration converts.

History fixture: the rows below are what builds before the rename wrote, so
they carry the former product name on purpose. It is spelled once, as ``OLD``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from infra.database import initialize_schema
from tests.schema_rewind import rewind_to_v88_policy

OLD = "maestro"
NEW = "corsia"
FROM_VERSION = 78
THREAD_ID = "t-1"
SYSTEM_BODY = f"## 10-INTENT.md\n\nRules written while the product was called {OLD}."
DEAD_LETTER_SEED = "-webhook-dead-letter"
REGISTRATION_SEED = "-artifact-registration"
RESTART_SEED = "-restart"
RESTART_TURN = "turn-pending"
ARTIFACT_ID = "art-1"
SCOPE_ID = "reg-b"


def derived(name: str, seed: str, key: str) -> str:
    """The uuid5 a writer derives for ``key`` when its seed starts with ``name``."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{name}{seed}:{key}"))


def _entry(source_id: str, section: str, *, content: str | None, inclusion: str) -> dict:
    return {
        "source_id": source_id, "origin_section": section, "label": "row",
        "stable_reference": "ref", "freshness": "packaged", "inclusion": inclusion,
        "reason": "ships with CORSIA", "owner_surface": "system-tier",
        "prompt_content": content, "debug_reference": None,
        # A project that kept the former name: content, not a section key.
        "metadata": {"project_name": OLD}, "prompt_tokens_override": None,
        "raw_version": None,
    }


def installed_metadata(name: str) -> str:
    """The boundary record of a thread installed while the section key was ``name``."""
    return json.dumps({
        "responsibility_map_snapshot": {"project_name": OLD, "agents": []},
        "_corsia_installed_context_sources": [
            _entry(f"{name}:v1:context/10-INTENT.md", name,
                   content=SYSTEM_BODY, inclusion="included"),
            _entry(f"{name}:error:manifest-mismatch", name, content=None, inclusion="omitted"),
            # The last builds before the rename already wrote the new id form.
            _entry(f"{NEW}:v1:context/20-CONTEXT.md", name,
                   content="## 20-CONTEXT.md\n\nMore rules.", inclusion="included"),
            _entry("project:context/PROJECT.md", "project",
                   content="project text", inclusion="included"),
        ],
    }, sort_keys=True)


def usage_metadata(name: str) -> str:
    """A Native usage record that kept its full context snapshot."""
    return json.dumps({
        "turn_id": "turn-1",
        "context_usage": {"snapshot_id": "s-1", "context_snapshot": {"source_inventory": [
            {"source_id": f"native:system:{name}", "origin_section": name,
             "component_id": name, "layer": name, "label": "System (v1)"},
            {"source_id": "native:system:project", "origin_section": "project"},
        ]}},
    }, sort_keys=True)


def raw_metadata(name: str) -> str:
    """Hand-formatted text: spacing, escapes and constants ``json.dumps`` never writes."""
    return (
        '{ "note" : "caf\\u00e9 \\/ ' + OLD + ' stays" ,\n'
        '  "responsibility_map_snapshot":{"project_name":"' + OLD + '"},\n'
        '\t"_corsia_installed_context_sources" : [ { "source_id":"' + name
        + ':v1:context\\/A.md" , "origin_section" :  "' + name + '",'
        ' "metadata":{"project_name":"' + OLD + '","big":1e400,"gap":NaN} } ] }'
    )


# Rows that mention the former name only as content. The migration must leave
# each one exactly as stored.
HISTORY = {
    "m-tool": json.dumps({
        "llm_tool_calls": [{"function": {"name": "read_file", "arguments": json.dumps(
            {"path": f"/work/projects/{OLD}/a.md", "project": OLD})}}],
        "arguments": {"project": OLD, "source_id": f"{OLD}:v1:context/A.md"},
    }),
    "m-null-owner": json.dumps({
        "project_name": OLD, "_corsia_installed_context_sources": None,
        "context_usage": None,
    }),
    "m-compact-usage": json.dumps({"context_usage": {"project": OLD, "context_snapshot": None}}),
    "m-not-an-object": json.dumps([OLD, {"label": OLD}]),
}


def message(db: sqlite3.Connection, message_id: str, metadata, *, seq: int | None = None,
            delivery_options: str | None = None) -> None:
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,created_at,seq,metadata,"
        "delivery_options) VALUES (?,?,'user',?,'2026-09-19T03:00:00',?,?,?)",
        (message_id, THREAD_ID, f"said {OLD}", seq, metadata, delivery_options),
    )


def dead_letter(db: sqlite3.Connection, dead_id: str, external_id: str | None) -> None:
    db.execute(
        "INSERT INTO dead_letter_events (id,internal_event,external_id,reason,created_at) "
        "VALUES (?,'webhook.github.push',?,'r','t0')",
        (dead_id, external_id),
    )


def registration_event(db: sqlite3.Connection, event_id: str, event_type: str,
                       causal: str | None) -> None:
    db.execute(
        "INSERT INTO artifact_registration_events (id,artifact_id,event_type,"
        "causal_event_id,created_at) VALUES (?,?,?,?,'t0')",
        (event_id, ARTIFACT_ID, event_type, causal),
    )


def restart_registration(db: sqlite3.Connection, registration_id: str, turn_id: str,
                         status: str) -> None:
    db.execute(
        "INSERT INTO restart_wake_registrations (id,thread_id,reason,status,registered_at,"
        "operation_id,turn_id) VALUES (?,?,'restart_recovery',?,'t0',NULL,?)",
        (registration_id, THREAD_ID, status, turn_id),
    )


def _registry(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO artifact_identities (id,integration,type,external_id,created_at,"
        "updated_at) VALUES (?,'workspace','document','workspace://w/a.md','t0','t0')",
        (ARTIFACT_ID,),
    )
    for registration_id, parent in (("reg-a", "thread-a"), (SCOPE_ID, "thread-b")):
        db.execute(
            "INSERT INTO artifact_registrations (id,artifact_id,parent_thread_id,"
            "created_at,updated_at) VALUES (?,?,?,'t0','t0')",
            (registration_id, ARTIFACT_ID, parent),
        )
    registration_event(db, derived(OLD, REGISTRATION_SEED, f"{ARTIFACT_ID}:rehomed:cause-1"),
                       "rehomed", "cause-1")
    registration_event(
        db,
        derived(OLD, REGISTRATION_SEED, f"{ARTIFACT_ID}:origin_node_retired:cause-2:{SCOPE_ID}"),
        "origin_node_retired", "cause-2",
    )
    registration_event(db, derived(NEW, REGISTRATION_SEED, f"{ARTIFACT_ID}:pinned:cause-3"),
                       "pinned", "cause-3")
    registration_event(db, "0123456789ab", "registered", None)


def build_store(path: Path, plant=None) -> None:
    """Write the store at schema 78. ``plant`` adds rows before it is closed."""
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        rewind_to_v88_policy(db)
        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,next_message_seq,"
            "created_at,updated_at) VALUES (?,'u','T','m',9,'t0','t0')",
            (THREAD_ID,),
        )
        message(db, "m-installed", installed_metadata(OLD), seq=1)
        message(db, "m-usage", usage_metadata(OLD), seq=2)
        message(db, "m-raw", raw_metadata(OLD), seq=3)
        message(db, "m-bare", None, seq=4)
        for message_id, metadata in HISTORY.items():
            message(db, message_id, metadata)
        dead_letter(db, derived(OLD, DEAD_LETTER_SEED, "delivery-old"), "delivery-old")
        dead_letter(db, derived(NEW, DEAD_LETTER_SEED, "delivery-new"), "delivery-new")
        dead_letter(db, "abcdef123456", None)
        _registry(db)
        restart_registration(
            db, derived(OLD, RESTART_SEED, f"external:{THREAD_ID}:{RESTART_TURN}"),
            RESTART_TURN, "pending",
        )
        # Settled: the message it woke names this id, so it stays as written.
        restart_registration(
            db, derived(OLD, RESTART_SEED, f"external:{THREAD_ID}:turn-woken"),
            "turn-woken", "woken",
        )
        if plant is not None:
            plant(db)
        db.execute(f"PRAGMA user_version={FROM_VERSION}")
        db.commit()
