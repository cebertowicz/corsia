"""Tests for `hardcoded-path-or-id` rule."""

from __future__ import annotations


def test_users_path_literal_fires(run_engine):
    """`/Users/...` literal in production code fires."""
    src = (
        "def open_log():\n"
        "    return open('/Users/alice/.corsia/corsia.db')\n"
    )
    findings = run_engine(
        "backend/foo.py", src, rule_id="hardcoded-path-or-id"
    )
    assert len(findings) >= 1
    assert any("/Users/" in f.message for f in findings)


def test_uuid_literal_fires(run_engine):
    """A bare UUID literal in production code fires."""
    src = (
        "DEFAULT_PROJECT = '9f5e2c1b-0000-4000-8000-aaaabbbbcccc'\n"
    )
    findings = run_engine(
        "backend/projects.py", src, rule_id="hardcoded-path-or-id"
    )
    assert len(findings) >= 1


def test_named_uuid_namespace_constant_does_not_fire(run_engine):
    """A UUIDv5 namespace constant is code identity, not a data-row ID."""
    src = (
        "import uuid\n"
        "_EXTERNAL_MESSAGE_ID_NAMESPACE = uuid.UUID(\n"
        "    '6f57e8c7-206e-525d-bbf8-e09bcd1996be'\n"
        ")\n"
    )
    findings = run_engine(
        "backend/messages.py", src, rule_id="hardcoded-path-or-id"
    )
    assert findings == []


def test_uuid_constructor_for_ordinary_data_id_still_fires(run_engine):
    """Using ``uuid.UUID`` does not excuse an ordinary hardcoded data ID."""
    src = (
        "import uuid\n"
        "DEFAULT_PROJECT_ID = uuid.UUID(\n"
        "    '9f5e2c1b-0000-4000-8000-aaaabbbbcccc'\n"
        ")\n"
    )
    findings = run_engine(
        "backend/projects.py", src, rule_id="hardcoded-path-or-id"
    )
    assert len(findings) == 1


def test_path_in_docstring_does_not_fire(run_engine):
    """Paths inside docstrings are allowed."""
    src = (
        '"""Example::\n'
        "\n"
        "    /Users/alice/.corsia/corsia.db\n"
        '"""\n'
        "\n"
        "def f(): pass\n"
    )
    findings = run_engine(
        "backend/foo.py", src, rule_id="hardcoded-path-or-id"
    )
    assert findings == []


def test_path_in_config_file_does_not_fire(run_engine):
    """Paths in `config*.py` are allowed."""
    src = (
        "DEFAULT_DB = '/Users/alice/.corsia/corsia.db'\n"
    )
    findings = run_engine(
        "backend/infra/config.py", src, rule_id="hardcoded-path-or-id"
    )
    assert findings == []


def test_path_in_an_explicitly_allowed_file_does_not_fire(run_engine):
    """`allow_files` is an explicit, reviewed per-file exemption, not a general escape."""
    src = 'HOME_PATH_RE = re.compile(r"/Users/([^/]+)")\n'
    findings = run_engine(
        "scripts/check_docs_drift/home_path.py", src, rule_id="hardcoded-path-or-id"
    )
    assert findings == []


def test_same_path_pattern_in_a_non_allowed_file_still_fires(run_engine):
    """The exemption is per-file, not a hole in the pattern check itself."""
    src = 'HOME_PATH_RE = re.compile(r"/Users/([^/]+)")\n'
    findings = run_engine(
        "scripts/check_docs_drift/other_module.py", src, rule_id="hardcoded-path-or-id"
    )
    assert len(findings) >= 1
