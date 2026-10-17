"""Schema v111 clears an agent-use restriction from endpoints off a Token Plan host.

The claims: an endpoint on an ordinary host marked ``interactive_only`` by the
earlier name-or-host rule reads ``allowed`` again, including one renamed since it
was marked; an endpoint on a Token Plan host keeps its restriction; an explicit
``allowed`` stays; non-object metadata is left alone; and the upgraded store
matches what a fresh registration stores. Every value here is synthetic.
"""

from __future__ import annotations

import json

from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_110_to_111_token_plan_by_host import migrate_110_to_111
from infra.schema import SCHEMA_VERSION

_ORDINARY = "https://api.example.com/compatible-mode/v1"
_TOKEN_PLAN = "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"


def _insert(db, sid: str, name: str, metadata) -> None:
    db.execute(
        "INSERT INTO network_servers (id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at,metadata_json) "
        "VALUES (?,'external','external',?,?,443,'online','t','t','t',?)",
        (sid, sid, name, metadata if isinstance(metadata, str) else json.dumps(metadata)),
    )


def test_the_step_is_registered_and_current():
    assert SCHEMA_VERSION == 124
    assert MIGRATIONS[110] is migrate_110_to_111


def test_v110_upgrade_unmarks_every_ordinary_host(isolated_db):
    from infra.database import get_db
    from llm.endpoint_policy import endpoint_agent_use

    db = get_db()
    _insert(db, "named-only", "Token Plan (Beijing)", {"base_url": _ORDINARY, "agent_use_restriction": "interactive_only"})
    _insert(db, "by-host", "Alibaba Token Plan", {"base_url": _TOKEN_PLAN, "agent_use_restriction": "interactive_only"})
    # Marked while named "Token Plan", then re-added under a new name: the mark survives the rename.
    _insert(db, "renamed", "Workspace", {"base_url": _ORDINARY, "agent_use_restriction": "interactive_only"})
    _insert(db, "named-allowed", "Token Plan", {"base_url": _ORDINARY, "agent_use_restriction": "allowed"})
    db.execute("PRAGMA user_version=110")
    db.commit()

    result = migrate_to_current(db, SCHEMA_VERSION)

    assert (result.migrated, result.from_version, result.to_version) == (True, 110, 124)
    rows = {sid: json.loads(raw) for sid, raw in db.execute("SELECT id, metadata_json FROM network_servers")}
    assert rows["named-only"] == rows["renamed"] == {"base_url": _ORDINARY}
    assert endpoint_agent_use("external@named-only/glm-5.3") == "allowed"
    assert endpoint_agent_use("external@renamed/glm-5.3") == "allowed"
    assert endpoint_agent_use("external@by-host/glm-5.3") == "interactive_only"
    assert endpoint_agent_use("external@named-allowed/glm-5.3") == "allowed"
    assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_v110_upgrade_skips_metadata_that_is_not_an_object(isolated_db):
    from infra.database import get_db

    db = get_db()
    for sid, raw in (("null-meta", "null"), ("list-meta", "[1]"), ("text-meta", '"x"'), ("bad-meta", "{broken")):
        _insert(db, sid, "Alibaba Token Plan", raw)

    migrate_110_to_111(db)

    rows = dict(db.execute("SELECT id, metadata_json FROM network_servers").fetchall())
    assert rows == {"null-meta": "null", "list-meta": "[1]", "text-meta": '"x"', "bad-meta": "{broken"}


def test_an_upgraded_store_matches_a_fresh_one(isolated_db):
    """The same endpoint ends with the same stored terms, whichever rule first ran."""
    from infra.database import get_db
    from infra.migration_109_to_110_agent_use_restriction import migrate_109_to_110

    db = get_db()
    _insert(db, "upgraded", "Token Plan (Beijing)", {"base_url": _ORDINARY, "agent_use_restriction": "interactive_only"})
    _insert(db, "fresh", "Token Plan (Beijing)", {"base_url": _ORDINARY})

    migrate_109_to_110(db)
    migrate_110_to_111(db)

    rows = {sid: json.loads(raw) for sid, raw in db.execute("SELECT id, metadata_json FROM network_servers")}
    assert rows["upgraded"] == rows["fresh"] == {"base_url": _ORDINARY}
