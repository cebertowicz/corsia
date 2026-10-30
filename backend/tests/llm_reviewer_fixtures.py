from __future__ import annotations

import contextlib
import json
import sqlite3
import threading

import pytest


_REVIEWER_ROWS = (
    ("claude-opus-4-8", 200_000, False),
    ("claude-opus-4-8[1m]", 1_000_000, False),
    ("claude-sonnet-4-6", 200_000, False),
    ("claude-sonnet-4-6[1m]", 1_000_000, False),
)


def _seed_reviewer_catalog(conn: sqlite3.Connection) -> None:
    rows = []
    for selector, context_window, internal in _REVIEWER_ROWS:
        rows.append((
            f"claude-code/{selector}", "claude-code", "claude-code",
            selector, f"claude-code/{selector}", selector, "cc", "test",
            1, 1, context_window,
            json.dumps({"resolved_identity": selector, "internal_reviewer": internal}),
            "t", "t",
        ))
    for catalog_id, selector, context_window in (
        ("codex/misleading-cc", "misleading-cc", 200_000),
        ("reviewer/misleading-cc-large", "misleading-cc[1m]", 1_000_000),
    ):
        rows.append((
            catalog_id, "claude-code", "claude-code", selector,
            f"claude-code/{selector}", selector, "cc", "test", 1, 1,
            context_window,
            json.dumps({"resolved_identity": selector, "internal_reviewer": False}),
            "t", "t",
        ))
    for selector, internal in (("gpt-5-codex", False), ("codex-auto-review", True)):
        rows.append((
            f"codex/{selector}", "codex", "codex", selector,
            f"codex/{selector}", selector, "codex", "test",
            0 if internal else 1, 0 if internal else 1, 200_000,
            json.dumps({"resolved_identity": selector, "internal_reviewer": internal}),
            "t", "t",
        ))
    conn.executemany(
        "INSERT OR REPLACE INTO models "
        "(id,harness,provider,provider_model_id,litellm_model,display_name,"
        "backend,source,enabled,available,context_window,metadata_json,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO connections "
        "(id,type,provider,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        [
            ("reviewer-cc", "cli", "claude-code", "available", "t", "t"),
            ("reviewer-codex", "cli", "codex", "available", "t", "t"),
        ],
    )
    conn.commit()


@pytest.fixture(autouse=True)
def _reviewer_catalog(isolated_db):
    from infra.database import get_db

    conn = get_db()
    try:
        _seed_reviewer_catalog(conn)
    finally:
        conn.close()


@pytest.fixture
def reviewer_accounts(_reviewer_catalog):
    """A signed-in Default per harness whose discovery lists every seeded reviewer model.

    Opt-in: a test that builds its own accounts, or proves a missing one, does not take it.
    """
    from infra.database import get_db
    from vendor_account_fixtures import seed_signed_in_defaults

    db = get_db()
    try:
        seed_signed_in_defaults(db)
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    """Disable DuckDB analytics for reviewer tests."""
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


@pytest.fixture(autouse=True)
def _reviewer_model_env(monkeypatch):
    monkeypatch.setenv("CORSIA_REVIEWER_MODEL", "claude-opus-4-8")
    monkeypatch.setenv("CORSIA_REVIEWER_FALLBACK_MODEL", "claude-sonnet-4-6")


@pytest.fixture()
def _isolated_db(tmp_path, monkeypatch):
    """Create isolated DB and context authority for workflow tests."""
    import infra.database as db_mod

    db_path = tmp_path / "test.db"
    monkeypatch.setattr("infra.config.DB_PATH", db_path)
    monkeypatch.setattr("infra.database.DB_PATH", db_path)

    db_mod.close_thread_db()
    db_mod._thread_local = threading.local()
    db_mod._all_connections.clear()

    from infra.schema import SCHEMA_SQL
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    _seed_reviewer_catalog(conn)
    # The workflow's reviewer runs on a signed-in Default listing every seeded model.
    from vendor_account_fixtures import seed_signed_in_defaults

    seed_signed_in_defaults(conn)
    conn.commit()
    conn.close()

    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "corsia" / "shared" / "standards"
    project_root = project / "source" / "corsia"
    project_root.mkdir(parents=True)
    (project / "DEVELOPMENT-DOCTRINE.md").write_text(
        "# Development doctrine: maintainability is the test\n\n"
        "Root-cause fixes are required.\n"
    )
    (project / "PRD-ENGINEERING-ARCHITECTURE-STANDARDS.md").write_text(
        "# Engineering standards\n\nTest contract.\n"
    )
    (project / "PRODUCT-STANDARDS.md").write_text(
        "# Product standards\n\nTest contract.\n"
    )
    (project / "COPY-STANDARD.md").write_text(
        "# Copy standard\n\nTest contract.\n"
    )
    (project / "llm-anti-patterns.md").write_text("# LLM anti-pattern catalog\n")

    from reviewer import on_pr_review as reviewer
    monkeypatch.setattr(reviewer, "_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(reviewer, "_PROJECT_ROOT", str(project_root))

    @contextlib.contextmanager
    def _exact_source(*_args, **_kwargs):
        yield "EXACT REVIEW SOURCE", str(tmp_path / "review"), str(
            project_root / ".git"
        )

    monkeypatch.setattr(
        "reviewer.reviewer_runner.exact_review_source", _exact_source,
    )

    yield db_path

    db_mod.close_thread_db()
    db_mod._thread_local = threading.local()
    db_mod._all_connections.clear()


def dispatch_pr_review(payload: dict) -> None:
    from reviewer.on_pr_review import on_pr_review

    on_pr_review("webhook.github.pr_opened", payload)
