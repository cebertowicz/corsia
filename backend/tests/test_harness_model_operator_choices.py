"""An operator's hide and label on a Claude Code or Codex model outlive discovery.

Discovery re-runs at startup and at least hourly. It may change what a model is
and whether it is listed, but never whether the operator shows it or what they
named it. Hiding only takes a model out of the picker: a thread already pinned
to it keeps running.
"""

from __future__ import annotations

import pytest

from vendor_account_fixtures import seed_catalog, signed_in_default


def _row(selector: str, name: str, *, available: bool = True) -> dict:
    return {
        "selector_id": selector, "display_name": name, "description": "",
        "available": available, "resolved_identity": f"id-{selector}",
        "capabilities": {}, "context_window": 262_144, "is_default": False,
    }


def _discover(provider: str, *rows: dict, at: str = "2026-09-25T00:00:00+00:00") -> None:

    seed_catalog(
        provider, list(rows), harness_version="2.1.300",
        source="test", timestamp=at,
    )


def _catalog(provider: str) -> dict[str, dict]:
    """Each stored row as readers see it: the operator's choices over what discovery lists now."""
    from infra.database import get_db
    from llm.harness_catalog import project_stored_rows

    db = get_db()
    try:
        stored = db.execute("SELECT * FROM models WHERE harness = ?", (provider,)).fetchall()
        return {row["provider_model_id"]: row for row in project_stored_rows(stored, db=db)}
    finally:
        db.close()


@pytest.fixture
def claude_code(isolated_db):
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection

    upsert_connection(
        type=ConnectionType.CLI, provider="claude-code",
        status=ConnectionStatus.AVAILABLE, config={"binary": "/bin/claude"},
    )
    signed_in_default("claude-code")


def test_a_hidden_and_renamed_model_stays_that_way_across_discovery(claude_code):
    from llm import catalog

    _discover("claude-code", _row("opus[1m]", "Opus (1M context)"), _row("haiku", "Haiku"))
    catalog.update_model("claude-code/haiku", {"enabled": 0})
    catalog.update_model("claude-code/opus[1m]", {"alias": "Opus 5.5"})

    _discover(
        "claude-code", _row("opus[1m]", "Opus (1M context)"),
        _row("haiku", "Haiku 4.5", available=False),
        at="2026-09-25T01:00:00+00:00",
    )

    rows = _catalog("claude-code")
    assert rows["haiku"]["enabled"] == 0
    assert rows["haiku"]["available"] == 0  # discovery still owns availability
    assert rows["haiku"]["display_name"] == "Haiku 4.5"
    assert rows["opus[1m]"]["enabled"] == 1
    assert rows["opus[1m]"]["alias"] == "Opus 5.5"
    assert catalog.get_display_name("claude-code/opus[1m]") == "Opus 5.5"


def test_a_newly_released_model_is_shown_by_default(claude_code):
    _discover("claude-code", _row("opus[1m]", "Opus"))
    _discover(
        "claude-code", _row("opus[1m]", "Opus"), _row("sonnet", "Sonnet 5"),
        at="2026-09-25T01:00:00+00:00",
    )

    assert _catalog("claude-code")["sonnet"]["enabled"] == 1


def test_a_reference_only_row_is_shown_again_once_discovery_lists_it(claude_code):
    from infra.database import write_transaction

    _discover("claude-code", _row("opus", "Opus 5.5"))
    with write_transaction(None, scope="test_reference_row") as db:
        db.execute(
            "UPDATE models SET source = 'reference', enabled = 0, available = 0 "
            "WHERE id = 'claude-code/opus'"
        )

    _discover("claude-code", _row("opus", "Opus 5.5"), at="2026-09-25T01:00:00+00:00")

    row = _catalog("claude-code")["opus"]
    assert (row["enabled"], row["available"]) == (1, 1)


def test_a_hidden_model_keeps_running_for_threads_pinned_to_it(claude_code):
    from llm import catalog
    from llm.resolution import resolve_execution_target

    _discover("claude-code", _row("haiku", "Haiku"))
    catalog.update_model("claude-code/haiku", {"enabled": 0})

    from llm.model_status import get_model_status_rows

    target = resolve_execution_target("claude-code/haiku")
    assert target.available is True
    assert target.availability_reason is None
    # ...but it is out of the picker.
    assert "claude-code/haiku" not in {m["id"] for m in get_model_status_rows()}


def test_an_unlisted_hidden_model_still_reports_why_it_cannot_run(claude_code):
    from llm import catalog
    from llm.resolution import resolve_execution_target

    _discover("claude-code", _row("haiku", "Haiku"))
    catalog.update_model("claude-code/haiku", {"enabled": 0})
    _discover("claude-code", _row("sonnet", "Sonnet 5"), at="2026-09-25T01:00:00+00:00")

    target = resolve_execution_target("claude-code/haiku")
    assert target.available is False
    assert target.availability_reason == "not_returned_by_discovery"


def test_a_native_model_switched_off_is_withdrawn(isolated_db):
    from infra.database import get_db
    from llm import catalog
    from llm.resolution import resolve_execution_target

    db = get_db()
    try:
        model_id = db.execute(
            "SELECT id FROM models WHERE harness = 'native' AND available = 1 "
            "AND enabled = 1 LIMIT 1"
        ).fetchone()
    finally:
        db.close()
    if model_id is None:
        pytest.skip("the seeded catalog has no available native row")
    catalog.update_model(model_id["id"], {"enabled": 0})

    assert resolve_execution_target(model_id["id"]).available is False
