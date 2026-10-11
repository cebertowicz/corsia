"""Schema v110 records an endpoint's agent-use terms as data.

The claims: an existing endpoint that matched the retired vendor rule becomes
``interactive_only`` once; an explicit value is never overwritten; every other
endpoint stays ``allowed``; and metadata that is not a JSON object is left alone
instead of aborting the upgrade. Every value here is synthetic.
"""

from __future__ import annotations

import json

from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_109_to_110_agent_use_restriction import migrate_109_to_110
from infra.schema import SCHEMA_VERSION


def test_the_step_is_registered_and_current():
    assert SCHEMA_VERSION >= 110
    assert MIGRATIONS[109] is migrate_109_to_110


def test_v109_upgrade_marks_a_legacy_token_plan_endpoint_interactive_only(isolated_db):
    from infra.database import get_db
    from llm.endpoint_policy import endpoint_agent_use

    db = get_db()
    db.execute(
        "INSERT INTO network_servers (id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at,metadata_json) "
        "VALUES ('legacy','external','external','legacy','Alibaba Token Plan',443,'online','t','t','t',?)",
        (json.dumps({"base_url": "https://token-plan.example.com/v1"}),),
    )
    db.execute("PRAGMA user_version=108")
    db.commit()

    result = migrate_to_current(db, SCHEMA_VERSION)

    assert (result.from_version, result.to_version) == (108, 110)
    assert endpoint_agent_use("external@legacy/glm-5.3") == "interactive_only"


def test_v110_upgrade_restricts_legacy_endpoints_without_overwriting_explicit_allowed(isolated_db):
    from infra.database import get_db
    from llm.endpoint_policy import endpoint_agent_use

    db = get_db()
    db.execute(
        "INSERT INTO network_servers (id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at,metadata_json) "
        "VALUES ('legacy-v109','external','external','legacy-v109','Alibaba Token Plan',443,'online','t','t','t',?)",
        (json.dumps({"base_url": "https://token-plan.example.com/v1"}),),
    )
    db.execute(
        "INSERT INTO network_servers (id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at,metadata_json) "
        "VALUES ('allowed-v109','external','external','allowed-v109','Alibaba Token Plan',443,'online','t','t','t',?)",
        (json.dumps({"agent_use_restriction": "allowed"}),),
    )
    db.execute("PRAGMA user_version=109")
    db.commit()

    result = migrate_to_current(db, SCHEMA_VERSION)

    assert SCHEMA_VERSION == 110
    assert (result.migrated, result.from_version, result.to_version) == (True, 109, 110)
    assert endpoint_agent_use("external@legacy-v109/glm-5.3") == "interactive_only"
    assert endpoint_agent_use("external@allowed-v109/glm-5.3") == "allowed"
    assert not migrate_to_current(db, SCHEMA_VERSION).migrated



def test_v109_upgrade_skips_metadata_that_is_not_an_object(isolated_db):
    from infra.database import get_db

    db = get_db()
    for sid, raw in (("null-meta", "null"), ("list-meta", "[1]"), ("text-meta", '"x"')):
        db.execute(
            "INSERT INTO network_servers (id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at,metadata_json) "
            "VALUES (?,'external','external',?,'Alibaba Token Plan',443,'online','t','t','t',?)",
            (sid, sid, raw),
        )
    db.execute("PRAGMA user_version=109")
    db.commit()

    result = migrate_to_current(db, SCHEMA_VERSION)

    assert (result.migrated, result.to_version) == (True, 110)
    rows = dict(db.execute("SELECT id, metadata_json FROM network_servers").fetchall())
    assert rows == {"null-meta": "null", "list-meta": "[1]", "text-meta": '"x"'}


def test_v109_upgrade_leaves_existing_endpoint_allowed(isolated_db):
    from infra.database import get_db
    from llm.endpoint_policy import endpoint_agent_use

    db = get_db()
    db.execute(
        "INSERT INTO network_servers (id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at,metadata_json) "
        "VALUES ('existing','external','external','existing','Existing endpoint',443,'online','t','t','t',?)",
        (json.dumps({"base_url": "https://existing.example/v1"}),),
    )
    db.execute("PRAGMA user_version=108")
    db.commit()

    result = migrate_to_current(db, SCHEMA_VERSION)

    assert (result.from_version, result.to_version) == (108, 110)
    assert endpoint_agent_use("external@existing/model") == "allowed"
