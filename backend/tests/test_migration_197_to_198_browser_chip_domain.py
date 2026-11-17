"""Schema 198: every stored browser chip carries the registrable domain a new chip would."""
import json
import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.migration_197_to_198_browser_chip_domain import migrate_197_to_198
from tools.tool_display import _browser_chip


def _step(db, message_id, metadata):
    db.execute(
        "INSERT INTO messages(id,thread_id,role,content,metadata,created_at) VALUES (?,'thread','assistant','',?,'t')",
        (message_id, json.dumps(metadata)),
    )


def _chip(db, message_id):
    return json.loads(db.execute("SELECT metadata FROM messages WHERE id=?", (message_id,)).fetchone()[0]).get(
        "browser_chip"
    )


def test_an_old_chip_gets_the_domain_a_new_chip_for_the_same_page_carries():
    url = "https://login.example-bank.com.verify-account.co.uk/start"
    new = _browser_chip("browser_open", {"url": url, "show_window": True}, "done")
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        _step(db, "old", {"turn_id": "t1", "browser_chip": {"host": new["host"]}})
        migrate_197_to_198(db)
        assert _chip(db, "old") == new


def test_a_chip_without_a_host_gets_a_null_domain_and_other_metadata_stays():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        _step(db, "nohost", {"turn_id": "t1", "browser_chip": {"host": None}})
        _step(db, "plain", {"turn_id": "t1"})
        migrate_197_to_198(db)
        assert _chip(db, "nohost") == {"host": None, "domain": None}
        assert _chip(db, "plain") is None
        assert json.loads(db.execute("SELECT metadata FROM messages WHERE id='nohost'").fetchone()[0])["turn_id"] == "t1"


def test_a_chip_that_already_has_a_domain_is_left_alone_and_a_rerun_changes_nothing():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        _step(db, "new", {"browser_chip": {"host": "docs.github.com", "domain": "github.com"}})
        _step(db, "old", {"browser_chip": {"host": "192.168.1.4"}})
        migrate_197_to_198(db)
        once = list(db.iterdump())
        migrate_197_to_198(db)
        assert list(db.iterdump()) == once
        assert _chip(db, "new") == {"host": "docs.github.com", "domain": "github.com"}
        assert _chip(db, "old") == {"host": "192.168.1.4", "domain": "192.168.1.4"}
