"""Project listing preserves the canonical address needed to resume a handoff."""

import asyncio


def test_listing_exposes_designated_model_less_conductor(isolated_db):
    from infra.accounts import create_user
    from infra.conductor import ensure_scope_conductor, get_conductor
    from infra.database import get_db
    from tools.query_projects import ListProjectsTool

    owner = create_user("listing-owner", "test-listing-password", first_run_wake=False)
    db = get_db()
    try:
        db.execute(
            "INSERT INTO projects (id,name,status,created_at,updated_at) "
            "VALUES ('garden','Garden Notes','active','t','t')"
        )
        db.commit()
        conductor = ensure_scope_conductor("garden", owner, model=None, db=db)
        assert get_conductor("garden", db=db) == conductor
        before = db.execute("SELECT model FROM threads WHERE id=?", (conductor,)).fetchone()[0]
    finally:
        db.close()

    result = asyncio.run(ListProjectsTool().execute())

    assert f"conductor_thread_id: {conductor}" in result
    db = get_db()
    try:
        assert get_conductor("garden", db=db) == conductor
        assert db.execute("SELECT model FROM threads WHERE id=?", (conductor,)).fetchone()[0] == before
    finally:
        db.close()


def test_listing_does_not_invent_an_absent_conductor(isolated_db):
    from infra.database import get_db
    from tools.query_projects import ListProjectsTool

    db = get_db()
    try:
        db.execute(
            "INSERT INTO projects (id,name,status,created_at,updated_at) "
            "VALUES ('missing','No conductor','active','t','t'),"
            "('archived','Archived','archived','t','t')"
        )
        db.commit()
    finally:
        db.close()

    result = asyncio.run(ListProjectsTool().execute())
    assert "conductor_thread_id: none (no designated conductor)" in result
    assert "Archived" not in result
    assert "Archived" in asyncio.run(ListProjectsTool().execute(status="all"))
