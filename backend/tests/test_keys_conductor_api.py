"""The conductor lists, adds and checks key entries through ``corsia_api``, and does nothing else.

Listing, adding and checking never carry a value. Setting, replacing,
clearing and removing stay the person's: they are off-limits to
``corsia_api`` whatever the method, and the schema step that adds the
store's tables is registered at 96.
"""

from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

import json
import re
import sqlite3
from contextlib import closing

import pytest

from infra import key_store
from infra.conductor import set_scope_coordinator
from infra.database import get_db
from infra.database_migrations import MIGRATIONS
from infra.migration_96_to_97_key_store import migrate_96_to_97
from infra.migration_106_to_107_key_store_credentials import _add_value_ref
from infra.schema import SCHEMA_VERSION
from tests.registry_helpers import make_session
from tools.conductor_api_policy import off_limits
from tools.corsia_api import CorsiaApiTool

VALUE = "BSAconductor-test-value-0123456789"


@pytest.fixture
def conductor(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    db = get_db()
    thread_id = make_session(db, prefix="conductor")
    assert set_corsia_agent(thread_id, db=db)
    return thread_id


@pytest.mark.asyncio
async def test_the_conductor_lists_and_adds_without_ever_seeing_a_value(conductor):
    key_store.set_value("web-search", VALUE)
    tool = CorsiaApiTool()
    listed = await tool.execute("GET", "/api/keys", _thread_id=conductor)
    assert json.loads(listed)["status"] == 200, listed
    assert VALUE not in listed
    added = await tool.execute(
        "POST", "/api/keys", body={"name": "maps", "label": "Maps", "description": "Map tiles"},
        _thread_id=conductor,
    )
    assert json.loads(added)["status"] == 200, added
    entry = key_store.entry_metadata("maps")
    assert entry["is_set"] is False
    assert entry["added_by_thread_id"] == conductor
    assert entry["used_by"] is None


@pytest.mark.asyncio
async def test_the_conductor_checks_the_saved_key_without_seeing_it(conductor, monkeypatch):
    from api import keys as keys_api
    from tools.web_search_outcome import WORKS, SearchOutcome

    async def works(api_key, query, count):
        assert api_key == VALUE
        return SearchOutcome(WORKS)

    monkeypatch.setattr(keys_api, "run_search", works)
    key_store.set_value("web-search", VALUE)
    checked = await CorsiaApiTool().execute("POST", "/api/keys/web-search/check", _thread_id=conductor)
    assert json.loads(checked)["status"] == 200, checked
    assert WORKS in checked
    assert VALUE not in checked


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", [
    ("PUT", "/api/keys/web-search/value", {"value": VALUE, "password": "x"}),
    ("POST", "/api/keys/web-search/clear", {"password": "x"}),
    ("POST", "/api/keys/maps/remove", {"password": "x"}),
])
async def test_every_value_route_is_off_limits(conductor, method, path, body):
    assert off_limits(path, method)
    result = await CorsiaApiTool().execute(method, path, body=body, _thread_id=conductor)
    assert result.startswith("Error:") and "off-limits" in result, result
    assert key_store.entry_metadata("web-search")["is_set"] is False


def test_the_key_store_step_is_registered_at_96_on_the_current_ladder():
    assert SCHEMA_VERSION == 162
    assert MIGRATIONS[96] is migrate_96_to_97


def test_the_steps_add_both_tables_idempotently_and_match_a_fresh_schema():
    from infra.database import initialize_schema

    def tables(db: sqlite3.Connection) -> dict[str, str]:
        return {
            # ALTER TABLE ADD COLUMN splices in its own spacing; only the tokens matter.
            name: re.sub(r"\s*([,()])\s*", r"\1", " ".join(sql.split()))
            for name, sql in db.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'"
                " AND name IN ('key_store_entries', 'key_write_attempts')"
            )
        }

    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE auth_tokens (token_hash TEXT PRIMARY KEY)")
        migrate_96_to_97(db)
        migrate_96_to_97(db)
        # The 105 -> 106 step adds value_ref; the fresh schema has it from the start.
        _add_value_ref(db)
        _add_value_ref(db)
        migrated = tables(db)
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        fresh = tables(db)
    assert set(migrated) == {"key_store_entries", "key_write_attempts"}
    assert migrated == fresh
