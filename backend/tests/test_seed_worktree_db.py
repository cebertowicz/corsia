from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


def _write_v2_bootstrap(
    home: Path,
    *,
    workspace: Path,
    state_root: Path,
) -> Path:
    bootstrap = home / ".corsia" / "config.json"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text(
        json.dumps(
            {
                "version": 2,
                "workspace": {
                    "id": "workspace-123",
                    "name": "Primary",
                    "root": str(workspace),
                },
                "state_root": str(state_root),
            }
        ),
        encoding="utf-8",
    )
    return bootstrap


_SEED_OWNER = "seed-owner"


def _init_schema_db(db_path: Path, *, with_account: bool = True) -> None:
    """Create the current schema, with the account a sanctioned caller provisions."""
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA_SQL)
    if with_account:
        db.execute(
            "INSERT INTO users (id, password_hash, created_at, password_changed_at)"
            " VALUES (?, 'unused', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (_SEED_OWNER,),
        )
    db.commit()
    db.close()


def _load_script(run_name: str = "seed_worktree_db_test"):
    """Load the script as a real module (not runpy.run_path, which returns a
    *copy* of its globals) so a test can mutate script["_HOME_BOOTSTRAP_PATH"]
    afterward and have the script's own functions actually see the change —
    they read it via their live __globals__, which is module.__dict__ here,
    not a disconnected snapshot."""
    repo = Path(__file__).resolve().parents[2]
    path = repo / "scripts" / "seed-worktree-db"
    loader = SourceFileLoader(run_name, str(path))
    spec = importlib.util.spec_from_file_location(run_name, str(path), loader=loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.__dict__


class _EPERMPath(type(Path())):
    """A Path whose ``exists()`` raises PermissionError, simulating a sandbox
    denying even a stat of the real home directory."""

    def exists(self, *args, **kwargs):
        raise PermissionError(1, "Operation not permitted", str(self))


# --- Zero production-state dependency ---------------------------------------


def test_seed_worktree_db_runs_with_no_home_bootstrap_at_all(tmp_path, monkeypatch):
    """No ~/.corsia/config.json anywhere: seeding must still succeed."""
    home = tmp_path / "home"
    target_state = tmp_path / "target-state"
    target_workspace = tmp_path / "target-workspace"
    target_state.mkdir()
    target_workspace.mkdir()
    _init_schema_db(target_state / "corsia.db")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CORSIA_E2E_MODEL", raising=False)
    monkeypatch.delenv("CORSIA_SEED_OMLX_ENDPOINT", raising=False)
    monkeypatch.delenv("CORSIA_SEED_OMLX_MODEL_ID", raising=False)
    monkeypatch.delenv("CORSIA_SEED_REVIEWER_MODEL_ID", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["seed-worktree-db", str(target_state), "--workspace", str(target_workspace)],
    )

    script = _load_script()
    script["main"]()

    conn = sqlite3.connect(target_state / "corsia.db")
    providers = conn.execute("SELECT value FROM settings WHERE key='providers'").fetchone()
    agents = conn.execute("SELECT value FROM settings WHERE key='agents'").fetchone()
    conn.close()
    assert json.loads(providers[0]) == {}
    assert json.loads(agents[0]) == {"defaults": {}}


def test_seed_worktree_db_refuses_sandboxed_denial_without_confirmation(
    tmp_path, monkeypatch
):
    """A sandbox-denied bootstrap stat (the exact #852 PermissionError) must
    not be silently treated as safe — it must fail loud without an explicit
    caller confirmation, since it cannot verify the target isn't prod."""
    target_state = tmp_path / "target-state"
    target_workspace = tmp_path / "target-workspace"
    target_state.mkdir()
    target_workspace.mkdir()
    _init_schema_db(target_state / "corsia.db")

    monkeypatch.delenv("CORSIA_E2E_MODEL", raising=False)
    monkeypatch.delenv("CORSIA_SEED_CONFIRM_NOT_PROD", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["seed-worktree-db", str(target_state), "--workspace", str(target_workspace)],
    )

    script = _load_script()
    # runpy returns a plain dict of module globals — mutate the entry
    # directly to simulate the sandbox denying even a stat of the real path.
    script["_HOME_BOOTSTRAP_PATH"] = _EPERMPath(
        str(tmp_path / "nonexistent-home" / ".corsia" / "config.json")
    )

    with pytest.raises(SystemExit, match="cannot verify"):
        script["main"]()


def test_seed_worktree_db_proceeds_on_sandboxed_denial_with_confirmation(
    tmp_path, monkeypatch
):
    """The trusted-caller escape hatch: with an explicit confirmation, a
    sandbox-denied bootstrap stat no longer blocks seeding."""
    target_state = tmp_path / "target-state"
    target_workspace = tmp_path / "target-workspace"
    target_state.mkdir()
    target_workspace.mkdir()
    _init_schema_db(target_state / "corsia.db")

    monkeypatch.delenv("CORSIA_E2E_MODEL", raising=False)
    monkeypatch.setenv("CORSIA_SEED_CONFIRM_NOT_PROD", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        ["seed-worktree-db", str(target_state), "--workspace", str(target_workspace)],
    )

    script = _load_script()
    script["_HOME_BOOTSTRAP_PATH"] = _EPERMPath(
        str(tmp_path / "nonexistent-home" / ".corsia" / "config.json")
    )

    script["main"]()

    conn = sqlite3.connect(target_state / "corsia.db")
    row = conn.execute("SELECT value FROM settings WHERE key='providers'").fetchone()
    conn.close()
    assert json.loads(row[0]) == {}


def test_seed_worktree_db_refuses_unparseable_bootstrap_without_confirmation(
    tmp_path, monkeypatch
):
    """A bootstrap that exists but fails to parse is just as unverifiable as
    a sandboxed denial — same fail-loud-without-confirmation behavior."""
    home = tmp_path / "home"
    target_state = tmp_path / "target-state"
    target_workspace = tmp_path / "target-workspace"
    target_state.mkdir()
    target_workspace.mkdir()
    _init_schema_db(target_state / "corsia.db")
    bootstrap = home / ".corsia" / "config.json"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text("{not-json", encoding="utf-8")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CORSIA_E2E_MODEL", raising=False)
    monkeypatch.delenv("CORSIA_SEED_CONFIRM_NOT_PROD", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["seed-worktree-db", str(target_state), "--workspace", str(target_workspace)],
    )

    script = _load_script()
    with pytest.raises(SystemExit, match="cannot verify"):
        script["main"]()


@pytest.mark.parametrize("use_symlink", [False, True], ids=["direct", "symlink"])
def test_seed_worktree_db_refuses_to_target_real_prod_state(
    tmp_path, monkeypatch, use_symlink
):
    home = tmp_path / "home"
    prod_workspace = tmp_path / "prod-workspace"
    prod_state = tmp_path / "prod-state"
    selected_workspace = tmp_path / "selected-workspace"
    prod_state.mkdir()
    selected_workspace.mkdir()
    (prod_state / "corsia.db").touch()
    _write_v2_bootstrap(home, workspace=prod_workspace, state_root=prod_state)

    target_state = prod_state
    if use_symlink:
        target_state = tmp_path / "target-state-alias"
        target_state.symlink_to(prod_state, target_is_directory=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CORSIA_E2E_MODEL", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["seed-worktree-db", str(target_state), "--workspace", str(selected_workspace)],
    )

    script = _load_script()
    sqlite_opens: list[object] = []

    def reject_sqlite_open(*args, **_kwargs):
        sqlite_opens.append(args)
        raise AssertionError("must fail before sqlite3.connect")

    monkeypatch.setattr(script["sqlite3"], "connect", reject_sqlite_open)

    with pytest.raises(SystemExit, match="production"):
        script["main"]()

    assert sqlite_opens == []


def test_seed_worktree_db_help_documents_synthetic_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["seed-worktree-db", "--help"])
    script = _load_script()
    script["main"]()  # must not raise; prints the module docstring


# --- Synthetic seeding building blocks ---------------------------------------


def test_seed_default_settings_only_fills_missing_keys(tmp_path):
    target_db = tmp_path / "corsia.db"
    _init_schema_db(target_db)
    conn = sqlite3.connect(target_db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('providers', ?, 't')",
        (json.dumps({"anthropic": {"apiKey": "already-here"}}),),
    )
    conn.commit()

    script = _load_script()
    script["_seed_default_settings"](conn)
    conn.commit()

    providers = json.loads(
        conn.execute("SELECT value FROM settings WHERE key='providers'").fetchone()[0]
    )
    agents = json.loads(
        conn.execute("SELECT value FROM settings WHERE key='agents'").fetchone()[0]
    )
    conn.close()
    assert providers == {"anthropic": {"apiKey": "already-here"}}
    assert agents == {"defaults": {}}


@pytest.fixture
def clean_openai_key():
    from infra import key_store_providers

    key_store_providers.remove_provider_key("openai")
    yield
    key_store_providers.remove_provider_key("openai")


def test_seed_provider_api_key_writes_key_store_not_settings(tmp_path, monkeypatch, clean_openai_key):
    from infra import key_store_providers

    monkeypatch.setenv("CORSIA_SEED_PROVIDER_API_KEY", "sk-test-key")
    script = _load_script()
    script["_seed_provider_api_key"](tmp_path / "state", tmp_path / "ws", "openai/gpt-4o")

    assert key_store_providers.provider_keys() == {"openai": "sk-test-key"}


def test_seed_provider_api_key_falls_back_to_provider_env_var(tmp_path, monkeypatch, clean_openai_key):
    from infra import key_store_providers

    monkeypatch.delenv("CORSIA_SEED_PROVIDER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    script = _load_script()
    script["_seed_provider_api_key"](tmp_path / "state", tmp_path / "ws", "openai/gpt-4o")

    assert key_store_providers.provider_keys() == {"openai": "sk-from-env"}


def test_seed_provider_api_key_without_input_writes_nothing(tmp_path, monkeypatch, clean_openai_key):
    from infra import key_store_providers

    monkeypatch.delenv("CORSIA_SEED_PROVIDER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    script = _load_script()
    script["_seed_provider_api_key"](tmp_path / "state", tmp_path / "ws", "openai/gpt-4o")

    assert key_store_providers.provider_keys() == {}


def test_seed_reviewer_model_merges_into_existing_agents_defaults(tmp_path, monkeypatch):
    target_db = tmp_path / "corsia.db"
    _init_schema_db(target_db)
    conn = sqlite3.connect(target_db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('agents', ?, 't')",
        (json.dumps({"defaults": {"model": "anthropic/claude-haiku-4-5"}}),),
    )
    conn.commit()

    script = _load_script()
    monkeypatch.setenv("CORSIA_SEED_REVIEWER_MODEL_ID", "omlx-seed-qwen")
    script["_seed_reviewer_model"](conn)
    conn.commit()

    agents = json.loads(
        conn.execute("SELECT value FROM settings WHERE key='agents'").fetchone()[0]
    )
    conn.close()
    assert agents == {
        "defaults": {
            "model": "anthropic/claude-haiku-4-5",
            "host_access_reviewer_model": "omlx-seed-qwen",
        }
    }


def test_seed_omlx_network_model_registers_synthetic_available_row(tmp_path, monkeypatch):
    target_db = tmp_path / "corsia.db"
    _init_schema_db(target_db)
    conn = sqlite3.connect(target_db)
    conn.row_factory = sqlite3.Row

    monkeypatch.setenv("CORSIA_SEED_OMLX_ENDPOINT", "http://127.0.0.1:8000")
    monkeypatch.setenv("CORSIA_SEED_OMLX_MODEL_ID", "Local-Model-oQ8-mtp")

    script = _load_script()
    catalog_id = script["_seed_omlx_network_model"](conn)
    conn.commit()

    assert catalog_id == "omlx-seed-local-model-oq8-mtp"
    server = conn.execute(
        "SELECT * FROM network_servers WHERE provider='omlx'"
    ).fetchone()
    endpoint = conn.execute(
        "SELECT * FROM network_server_endpoints WHERE server_id=?", (server["id"],)
    ).fetchone()
    model = conn.execute(
        "SELECT * FROM models WHERE id=?", (catalog_id,)
    ).fetchone()
    conn.close()

    assert server["status"] == "online"
    assert server["port"] == 8000
    assert endpoint["host"] == "127.0.0.1"
    assert model["backend"] == "network"
    assert model["available"] == 1
    assert model["provider"] == "omlx"
    assert model["provider_model_id"] == "Local-Model-oQ8-mtp"
    assert model["litellm_model"] == f"omlx@{server['id']}/Local-Model-oQ8-mtp"


def test_seed_omlx_network_model_returns_none_when_unset(tmp_path):
    target_db = tmp_path / "corsia.db"
    _init_schema_db(target_db)
    conn = sqlite3.connect(target_db)
    conn.row_factory = sqlite3.Row

    script = _load_script()
    assert script["_seed_omlx_network_model"](conn) is None
    conn.close()


def test_seed_omlx_network_model_requires_both_env_vars_together(tmp_path, monkeypatch):
    target_db = tmp_path / "corsia.db"
    _init_schema_db(target_db)
    conn = sqlite3.connect(target_db)
    conn.row_factory = sqlite3.Row

    monkeypatch.setenv("CORSIA_SEED_OMLX_ENDPOINT", "http://127.0.0.1:8000")
    monkeypatch.delenv("CORSIA_SEED_OMLX_MODEL_ID", raising=False)

    script = _load_script()
    with pytest.raises(SystemExit, match="must be set together"):
        script["_seed_omlx_network_model"](conn)
    conn.close()


def test_seed_omlx_network_model_requires_explicit_port(tmp_path, monkeypatch):
    target_db = tmp_path / "corsia.db"
    _init_schema_db(target_db)
    conn = sqlite3.connect(target_db)
    conn.row_factory = sqlite3.Row

    monkeypatch.setenv("CORSIA_SEED_OMLX_ENDPOINT", "http://localhost")
    monkeypatch.setenv("CORSIA_SEED_OMLX_MODEL_ID", "some-model")

    script = _load_script()
    with pytest.raises(SystemExit, match="explicit host and port"):
        script["_seed_omlx_network_model"](conn)
    conn.close()


# --- CORSIA_E2E_MODEL catalog sync (unchanged cloud-discovery path) ----


def test_seed_worktree_db_never_invents_missing_context_window(tmp_path, monkeypatch):
    from llm import catalog

    workspace = tmp_path / "worktree"
    state_root = tmp_path / "worktree-state"
    workspace.mkdir()
    state_root.mkdir()
    db_path = state_root / "corsia.db"
    _init_schema_db(db_path)
    db = sqlite3.connect(db_path)
    db.execute(
        "INSERT OR REPLACE INTO settings (key,value,updated_at) "
        "VALUES ('providers', ?, 't')",
        (json.dumps({}),),
    )
    db.commit()
    db.close()

    async def _discover(_provider, _key):
        return {
            "ok": True,
            "models": [{
                "id": "exact-unknown",
                "display_name": "Exact Unknown",
                "supports_tools": True,
            }],
        }

    monkeypatch.setattr(catalog, "discover_cloud_models", _discover)
    monkeypatch.setenv("CORSIA_E2E_MODEL", "anthropic/exact-unknown")

    script = _load_script()
    script["_sync_e2e_model_to_catalog"](
        workspace,
        state_root,
        db_path,
    )

    db = sqlite3.connect(db_path)
    row = db.execute(
        "SELECT context_window FROM models "
        "WHERE provider = 'anthropic' AND provider_model_id = 'exact-unknown'"
    ).fetchone()
    db.close()
    assert row == (None,)


def test_seed_worktree_db_end_to_end_synthetic_run(tmp_path, monkeypatch):
    """Full main() pass: settings, oMLX model, reviewer model, and the
    smoke project/session, with zero home-bootstrap dependency."""
    home = tmp_path / "home"
    target_state = tmp_path / "target-state"
    target_workspace = tmp_path / "target-workspace"
    target_state.mkdir()
    target_workspace.mkdir()
    _init_schema_db(target_state / "corsia.db")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CORSIA_E2E_MODEL", raising=False)
    monkeypatch.setenv("CORSIA_SEED_OMLX_ENDPOINT", "http://127.0.0.1:8000")
    monkeypatch.setenv("CORSIA_SEED_OMLX_MODEL_ID", "qwen-test")
    monkeypatch.setenv("CORSIA_SEED_REVIEWER_MODEL_ID", "omlx-seed-qwen-test")
    monkeypatch.setattr(
        sys,
        "argv",
        ["seed-worktree-db", str(target_state), "--workspace", str(target_workspace)],
    )

    script = _load_script()
    script["main"]()

    conn = sqlite3.connect(target_state / "corsia.db")
    conn.row_factory = sqlite3.Row
    agents = json.loads(
        conn.execute("SELECT value FROM settings WHERE key='agents'").fetchone()["value"]
    )
    model = conn.execute(
        "SELECT * FROM models WHERE id='omlx-seed-qwen-test'"
    ).fetchone()
    project = conn.execute(
        "SELECT * FROM projects WHERE name='Test Project'"
    ).fetchone()
    session = conn.execute(
        "SELECT * FROM threads WHERE title='Smoke Test'"
    ).fetchone()
    conn.close()

    assert agents["defaults"]["host_access_reviewer_model"] == "omlx-seed-qwen-test"
    assert model is not None
    assert model["available"] == 1
    assert project is not None
    assert session is not None
    assert session["user_id"] == _SEED_OWNER


def test_seed_worktree_db_refuses_without_an_account(tmp_path, monkeypatch):
    """No account to own the seeded thread: fail loud, never invent an owner."""
    home = tmp_path / "home"
    target_state = tmp_path / "target-state"
    target_workspace = tmp_path / "target-workspace"
    target_state.mkdir()
    target_workspace.mkdir()
    _init_schema_db(target_state / "corsia.db", with_account=False)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CORSIA_E2E_MODEL", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["seed-worktree-db", str(target_state), "--workspace", str(target_workspace)],
    )

    script = _load_script()
    with pytest.raises(SystemExit):
        script["main"]()

    conn = sqlite3.connect(target_state / "corsia.db")
    threads = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    conn.close()
    assert threads == 0


def _seed_with_measurement_fixture(tmp_path, monkeypatch, *, enabled: bool):
    """Run the seeder against a fresh schema DB and return its path."""
    target_state = tmp_path / "target-state"
    target_workspace = tmp_path / "target-workspace"
    target_state.mkdir()
    target_workspace.mkdir()
    _init_schema_db(target_state / "corsia.db")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CORSIA_E2E_MODEL", raising=False)
    if enabled:
        monkeypatch.setenv("CORSIA_SEED_MEASUREMENT_FIXTURE", "1")
    else:
        monkeypatch.delenv("CORSIA_SEED_MEASUREMENT_FIXTURE", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["seed-worktree-db", str(target_state), "--workspace", str(target_workspace)]
    )
    _load_script()["main"]()
    return target_state / "corsia.db"


def _seed_grain():
    """The twelve completed weeks before the week of Sep 28, which is in progress."""
    from datetime import datetime, timezone

    from core.measurement_scope import resolve_grain

    return resolve_grain("weeks", "UTC", now=datetime(2026, 9, 28, 12, tzinfo=timezone.utc))


def test_seed_worktree_db_measurement_fixture_feeds_the_series_queries(tmp_path, monkeypatch):
    """The opt-in fixture reproduces the weekly headline numbers through the
    real aggregate SQL, and a second run adds nothing."""
    from api.measurement_queries import prepare_outcome_periods as _prepare_outcome_periods, query_family as _query_family

    db_path = _seed_with_measurement_fixture(tmp_path, monkeypatch, enabled=True)
    _load_script()["main"]()  # idempotent second run

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    params = {"user_id": _SEED_OWNER, "scope_project": "all", "scope_agent": None,
              "grain": _seed_grain()}
    _prepare_outcome_periods(conn, params)
    outcomes = {r["period"]: r for r in _query_family(conn, "outcomes", params)}
    decisions = {r["period"]: r for r in _query_family(conn, "decisions", params)}
    tokens = {r["period"]: r for r in _query_family(conn, "tokens", params)}
    conn.close()

    assert {w: r["outcomes"] for w, r in outcomes.items()} == {
        "2026-09-07": 12, "2026-09-14": 100, "2026-09-21": 762}
    assert outcomes["2026-09-21"]["first_try"] == 594
    assert (decisions["2026-09-14"]["answered"], decisions["2026-09-14"]["recommendation"],
            decisions["2026-09-14"]["other"]) == (100, 31, 65)
    assert (decisions["2026-09-21"]["recommendation"], decisions["2026-09-21"]["other"]) == (43, 40)
    assert tokens["2026-09-21"]["tokens"] == 114_300_000 // 762 * 762


def test_seed_worktree_db_measurement_fixture_reaches_the_attention_estimate(tmp_path, monkeypatch):
    """The fixture gives settled weeks recorded presence, so the proxy Attention
    series serves values, and the thin week stays withheld."""
    from api.measurement import _row
    from api.measurement_presence import first_presence_period
    from api.measurement_queries import prepare_outcome_periods as _prepare_outcome_periods, query_family as _query_family

    db_path = _seed_with_measurement_fixture(tmp_path, monkeypatch, enabled=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    params = {"user_id": _SEED_OWNER, "scope_project": "all", "scope_agent": None,
              "grain": _seed_grain()}
    _prepare_outcome_periods(conn, params)
    active = {r["period"]: r for r in _query_family(conn, "active", params)}
    first_presence = first_presence_period(conn, _SEED_OWNER, params["grain"])
    conn.close()

    rows = {period: _row("active_hours_per_outcome", period, active.get(period), False,
                         grain=params["grain"], first_presence_period=first_presence)
            for period in ("2026-09-07", "2026-09-14", "2026-09-21")}
    assert rows["2026-09-07"]["value"] is None
    # The fixture records 60 and 120 minutes of presence, divided by that week's accepted outcomes.
    assert rows["2026-09-14"]["value"] == pytest.approx(60 / 60 / 100)
    assert rows["2026-09-21"]["value"] == pytest.approx(120 / 60 / 762)
    assert rows["2026-09-21"]["proxy"] is True


def test_seed_worktree_db_omits_measurement_fixture_by_default(tmp_path, monkeypatch):
    db_path = _seed_with_measurement_fixture(tmp_path, monkeypatch, enabled=False)
    conn = sqlite3.connect(db_path)
    plans = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    conn.close()
    assert plans == 0
