"""Direct file I/O within transaction bodies."""

import pytest

RULE = "file-io-in-write-transaction"


@pytest.mark.parametrize("call", [
    "open('x')", "path.read_text()", "path.write_text('x')", "path.read_bytes()",
    "path.write_bytes(b'x')", "path.open()", "path.unlink()", "path.rename('x')",
    "path.replace('x')", "path.mkdir()", "path.touch()", "os.replace('a', 'b')",
    "os.rename('a', 'b')", "os.remove('a')", "os.unlink('a')", "os.makedirs('a')",
    "os.mkdir('a')", "os.rmdir('a')", "os.fsync(1)", "shutil.copy('a', 'b')",
    "json.dump({}, fp)", "pickle.dump({}, fp)",
])
def test_direct_file_io_flags(run_engine, call):
    source = f"import os, shutil, json, pickle\nfrom pathlib import Path\nfrom infra.database import write_transaction\ndef action(path: Path):\n    with write_transaction(db):\n        {call}\n"
    assert len(run_engine("backend/example.py", source, rule_id=RULE)) == 1


def test_constructed_and_derived_path_methods(run_engine):
    source = '''\nfrom pathlib import Path as P
from infra.database import write_transaction
def action():
    base = P("root")
    destination = base / "file"
    with write_transaction(db):
        destination.replace("other")
        P("x").open()
        destination.rename("y")
'''
    assert len(run_engine("backend/example.py", source, rule_id=RULE)) == 3


def test_exception_is_limited_to_named_call(run_engine):
    source = '''\nimport os
from infra.database import write_transaction
def ensure_projection():
    with write_transaction(db):
        os.rename("a", "b")
        open("new")
def another():
    with write_transaction(db):
        os.rename("a", "b")
'''
    findings = run_engine("backend/connections/account_projection.py", source, rule_id=RULE)
    assert [finding.line for finding in findings] == [7, 10]


def test_outside_and_non_io_inside_are_not_flagged(run_engine):
    source = """\nfrom infra.database import write_transaction
open('outside')
with write_transaction(db):
    conn.execute('UPDATE t SET x = 1')
    store.replace(grant, db=conn)
    value.replace('.', '_')
    datetime.now().replace(tzinfo=None)
"""
    assert run_engine("backend/example.py", source, rule_id=RULE) == []


def test_nested_with_inside_writer(run_engine):
    source = """\nfrom infra.database import write_transaction
with write_transaction(db):
    with something():
        open('x')
"""
    assert len(run_engine("backend/example.py", source, rule_id=RULE)) == 1
