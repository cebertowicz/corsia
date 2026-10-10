"""Contract tier: account state and usage each have exactly one owner.

Pins: claim-helper enrollment never permits a neighboring independent state writer, and
subscription usage has exactly one runtime persistence owner. Kept under backend/tests/contract
so it stays opt-in locally and in the CI gate (CORSIA_CONTRACT_SUITE=1)."""
from __future__ import annotations

import ast
from pathlib import Path
import re

import pytest

from tests.contract import test_i3_one_owner as guard


BACKEND = Path(__file__).resolve().parents[2]


_VENDOR_TABLE = re.compile(r"\b(?:from|join|update|into|table)\s+vendor_accounts\b")


_WILDCARD = re.compile(r"\bselect\s+(?:\w+\s*\.\s*)?\*|\b\w+\s*\.\s*\*")


def _fragments(node, assignments, visiting=frozenset()):
    """Trace string fragments through composed SQL, including column comprehensions.

    This traces dependency expressions rather than evaluating Python. In the
    production projection, _COLUMNS's tuple supplies column names and _JOIN
    supplies the table; neither literal can be checked alone.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name) and node.id not in visiting:
        return [text for value in assignments.get(node.id, ())
                for text in _fragments(value, assignments, visiting | {node.id})]
    return [text for child in ast.iter_child_nodes(node)
            for text in _fragments(child, assignments, visiting)]


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    assignments: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            assignments.setdefault(node.target.id, []).append(node.value)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"execute", "executemany", "executescript"} or not node.args:
            continue
        sql = "".join(_fragments(node.args[0], assignments)).lower()
        if _VENDOR_TABLE.search(sql) and ("usage_json" in sql or _WILDCARD.search(sql)):
            violations.append(f"{path}:{node.lineno}")
    return violations


@pytest.mark.contract
@pytest.mark.parametrize('relative,source', [
    ('infra/input_turn_assignment_other.py', "UPDATE messages SET delivery_state='delivered'"),
    ('infra/nested/vendor_login.py', "UPDATE messages SET active_turn_id='other'"),
    ('infra/new_vendor_login.py', "UPDATE active_turns SET status='error'"),
])
def test_original_state_scan_rejects_unenrolled_claim_neighbors(monkeypatch, relative, source):
    inventory = list(guard.iter_backend_sources())
    path = guard.BACKEND_ROOT / relative
    monkeypatch.setattr(guard, 'iter_backend_sources', lambda: iter([*inventory, (path, source)]))
    with pytest.raises(AssertionError, match=relative):
        guard.test_owned_state_has_only_enrolled_transition_writers()


def test_only_canonical_harness_usage_can_access_account_usage():
    failures = []
    for path in BACKEND.rglob("*.py"):
        relative = path.relative_to(BACKEND)
        if relative.parts[0] == "tests" or path.name == "conftest.py":
            continue
        if relative.as_posix() in {"connections/harness_usage.py", "infra/vendor_account_schema.py"}:
            continue
        if path.name.startswith("migration_"):
            continue  # Frozen one-time transforms are not runtime readers.
        failures.extend(_violations(path))
    assert not failures, "Account usage ownership escaped harness_usage: " + ", ".join(failures)


@pytest.mark.parametrize("source", [
    'db.execute("SELECT usage_json FROM vendor_accounts")',
    'db.execute("SELECT * FROM vendor_accounts")',
    'db.execute("SELECT a.* FROM vendor_accounts a")',
    'query = "SELECT " + "usage_json " + "FROM vendor_accounts"\ndb.execute(query)',
    '_COLUMNS = ",".join("a." + name for name in ("id", "usage_json"))\n'
    '_JOIN = " FROM vendor_accounts a JOIN connections c ON c.id=a.connection_id"\n'
    'db.execute("SELECT " + _COLUMNS + _JOIN)',
    '_COLUMNS = "a.*"\n_JOIN = " FROM vendor_accounts a"\ndb.execute("SELECT " + _COLUMNS + _JOIN)',
    'db.execute("UPDATE vendor_accounts SET " + "usage_json=? WHERE id=?", params)',
])
def test_guard_detects_actual_composed_projection_and_wildcards(tmp_path, source):
    path = tmp_path / "bad.py"
    path.write_text(source)
    assert len(_violations(path)) == 1


def test_checkpoint_usage_column_and_account_metadata_can_share_a_module(tmp_path):
    path = tmp_path / "allowed.py"
    path.write_text('db.execute("SELECT usage_json FROM checkpoints WHERE id=?", params)\n'
                    '_COLUMNS="a.id,a.name"\n_JOIN=" FROM vendor_accounts a"\n'
                    'db.execute("SELECT " + _COLUMNS + _JOIN)\n')
    assert not _violations(path)
