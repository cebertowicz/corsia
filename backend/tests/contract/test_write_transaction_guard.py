"""The shipping static gate rejects private commits, not mere missing mentions."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from tests.contract.conftest import iter_backend_sources

pytestmark = pytest.mark.contract
RULE_ID = "direct-commit-outside-write-transaction"
REPO_ROOT = Path(__file__).resolve().parents[3]
READ_SNAPSHOTS = [
    ("core/lesson_report_db.py", "report_snapshot"),
    ("core/checkpoint_agent_admission.py", "read_checkpoint_preparation"),
    ("core/turn_iteration_context.py", "_read_native_checkpoint_context"),
    ("thread/model_checkpoint.py", "read_switch_checkpoint_context"),
    ("thread/idle_checkpoint.py", "read_idle_context"),
    ("api/thread_cache_warm.py", "_warm_replay_candidates"),
    ("infra/runtime_truth.py", "provider_boundary_snapshot"),
    ("infra/runtime_truth.py", "_has_registered_provider"),
    ("infra/send_replay_reads.py", "read_candidates"),
    ("subagents/harness_checkpoint_context.py", "read_external_checkpoint_context"),
    ("thread/model_source.py", "_read_model_switch_start"),
    ("thread/model_source.py", "_read_model_switch_candidates"),
]


@pytest.fixture
def guard_engine(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "tools"))
    from antipatterns.config import load_config
    from antipatterns.engine import Engine

    return Engine.from_config(load_config(tmp_path), repo_root=tmp_path)


def _scan(engine, source, relative="backend/example.py"):
    path = engine.repo_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return [finding for finding in engine.run([path]) if finding.rule_id == RULE_ID]


def test_guard_is_registered_and_blocking_by_default(guard_engine):
    rules = [rule for rule in guard_engine.rules if rule.id == RULE_ID]
    assert len(rules) == 1
    assert rules[0].severity == "error"
    assert guard_engine.config["rules"][RULE_ID] == {"enabled": True, "severity": "error"}


@pytest.mark.parametrize("body", [
    "db.commit()",
    "db.execute('BEGIN')",
    "db.execute(' begin immediate ') ",
    "db.execute('COMMIT;')",
    "db.executescript('BEGIN; UPDATE records SET n=1; COMMIT;')",
    "db.executescript('COMMIT')",
    "with write_transaction(db):\n    pass\ndb.commit()",
    "db.commit()\nwith write_transaction(db):\n    pass",
    "with another_transaction(db):\n    db.commit()",
    "with write_transaction(db):\n    def later():\n        db.commit()",
    "with write_transaction(db):\n    later = lambda: db.commit()",
    "with write_transaction(db):\n    class Later:\n        db.commit()",
    "with write_transaction(db):\n    async def later():\n        db.commit()",
    "with write_transaction(db, scope=db.commit()):\n    pass",
    "with write_transaction(db):\n    pass\nwith other(db):\n    db.commit()",
    "try:\n    with write_transaction(db):\n        pass\nfinally:\n    db.commit()",
])
def test_private_boundary_is_an_error(guard_engine, body):
    findings = _scan(guard_engine, "def write(db):\n" + textwrap.indent(body, "    "))
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert findings[0].file == "backend/example.py"


def test_error_points_to_the_call_not_the_function(guard_engine):
    findings = _scan(guard_engine, "async def write(db):\n    db.commit()\n")
    assert [(item.line, item.col, item.snippet) for item in findings] == [
        (2, 5, "    db.commit()"),
    ]


@pytest.mark.parametrize("source", [
    "with write_transaction(db):\n    db.commit()",
    "with database.write_transaction(db):\n    db.execute('BEGIN IMMEDIATE')",
    "with lock, infra.database.write_transaction(db):\n    db.execute('COMMIT')",
    "async def write(db):\n    with write_transaction(db):\n        db.commit()",
    "with write_transaction(db):\n    if ready:\n        db.commit()",
    "def write(db):\n    with write_transaction(db):\n        db.commit()",
    "with write_transaction(db):\n    def later():\n        with write_transaction(db):\n            db.commit()",
    "# db.commit()\ntext = 'db.commit() BEGIN COMMIT'",
    "def read(db):\n    return db.execute('SELECT 1')",
    # The declared guard is lexical, not a dynamic-SQL or alias analysis.
    "db.execute(statement)\nfinish = db.commit\nfinish()",
])
def test_owned_writes_and_non_calls_are_not_errors(guard_engine, source):
    assert _scan(guard_engine, source) == []


@pytest.mark.parametrize("relative", [
    "backend/infra/database.py",
    "backend/tests/example.py",
    "backend/e2e_mock.py",
    "backend/e2e_app.py",
    "backend/e2e_pressure_fixture.py",
    "backend/eval/fixtures/blank_session.py",
    "backend/api/project_repos.py",
    "tools/example.py",
    "frontend/example.py",
    "backend/example.txt",
])
def test_only_existing_scope_exclusions_are_exempt(guard_engine, relative):
    assert _scan(guard_engine, "db.commit()", relative) == []


@pytest.mark.parametrize("relative", [
    "backend/api/project_scaffolding.py",
    "backend/infra/database_migrations.py",
    "backend/eval/endpoint.py",
    "backend/eval/fixtures/new_fixture.py",
    "backend/qa/production.py",
    "backend/test_production.py",
])
def test_sibling_or_test_named_production_sources_stay_in_scope(guard_engine, relative):
    assert len(_scan(guard_engine, "db.commit()", relative)) == 1


@pytest.mark.parametrize("relative,function", READ_SNAPSHOTS)
def test_read_snapshot_exception_is_only_its_plain_begin(guard_engine, relative, function):
    source = f"def {function}(db):\n    db.execute('BEGIN')\n"
    assert _scan(guard_engine, source, "backend/" + relative) == []
    for statement in ("db.commit()", "db.execute('BEGIN IMMEDIATE')", "db.execute('COMMIT')"):
        source = f"def {function}(db):\n    {statement}\n"
        assert len(_scan(guard_engine, source, "backend/" + relative)) == 1


@pytest.mark.parametrize("source", [
    "def different(db):\n    db.execute('BEGIN')",
    "def outer(db):\n    def provider_boundary_snapshot():\n        db.execute('BEGIN')",
    "class Other:\n    def provider_boundary_snapshot(db):\n        db.execute('BEGIN')",
    "def provider_boundary_snapshot(db):\n    db.execute('BEGIN; DELETE FROM rows;')",
])
def test_read_snapshot_exception_does_not_cover_other_scopes(guard_engine, source):
    assert len(_scan(guard_engine, source, "backend/infra/runtime_truth.py")) == 1


def test_unparseable_production_source_cannot_look_clean(guard_engine):
    findings = _scan(guard_engine, "def broken(:\n    db.commit()")
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert "parse" in findings[0].message.lower()


def test_existing_cli_blocks_rogue_commit_and_accepts_owned_commit(guard_engine, capsys):
    from antipatterns.cli import main

    path = guard_engine.repo_root / "backend/example.py"
    args = ["--paths", str(path), "--repo-root", str(guard_engine.repo_root),
            "--rule", RULE_ID, "--severity", "error"]
    _scan(guard_engine, "db.commit()")
    assert main(args) == 1
    assert RULE_ID in capsys.readouterr().out
    _scan(guard_engine, "with write_transaction(db):\n    db.commit()")
    assert main(args) == 0


def test_real_backend_has_no_private_boundary_and_known_snapshots(guard_engine):
    from antipatterns.engine import Engine, _matches_any

    rules = [rule for rule in guard_engine.rules if rule.id == RULE_ID]
    assert len(rules) == 1, "a missing rule must not yield a vacuous clean census"
    engine = Engine(rules=rules, config=guard_engine.config, repo_root=REPO_ROOT)
    sources = list(iter_backend_sources())
    assert sources, "backend inventory must not be empty"
    # Do not silently inherit a broad engine exemption absent from the G0 scope.
    excluded = engine.config["global"]["exclude_globs"]
    assert not [str(path) for path, _ in sources
                if _matches_any(path.relative_to(REPO_ROOT).as_posix(), excluded)]
    actual_snapshots = set()
    for path, source in sources:
        rel = path.relative_to(REPO_ROOT / "backend").as_posix()
        for node in ast.parse(source).body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if (rel, node.name) not in READ_SNAPSHOTS:
                continue
            for child in ast.walk(node):
                if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "execute" and child.args
                        and isinstance(child.args[0], ast.Constant)
                        and child.args[0].value == "BEGIN"):
                    actual_snapshots.add((rel, node.name))
    assert actual_snapshots == set(READ_SNAPSHOTS)
    assert engine.run([path for path, _ in sources]) == []
