"""The PR reviewer runs on an account chosen by the account contract, never the ambient login.

Pins: a limited Default no longer fails the gate while a permitted account is usable; signed-out,
removed, project-denied and all-limited accounts still fail visibly with their account cause and
no review runs; the reviewer never reaches an account outside the permitted set, and no
environment is built from the ambient login or a Codex source home.
"""
from __future__ import annotations

import inspect
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from llm_reviewer_fixtures import _reviewer_catalog  # noqa: F401

MODEL = "claude-code/claude-opus-4-8"
PROJECT = "corsia-project"
_RESET = "2099-01-01T00:00:00+00:00"


@pytest.fixture
def scope(tmp_path, monkeypatch):
    """The reviewed project, a Default and a signed-in second Claude account, and a writable db."""
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import add_account, set_account_state
    from infra.database import get_db
    from vendor_account_fixtures import seed_default_account

    monkeypatch.setattr("infra.config.WORKSPACE", tmp_path)
    (tmp_path / "projects" / "corsia").mkdir(parents=True)
    db = get_db()
    db.execute("INSERT INTO projects(id,name,path,created_at,updated_at) VALUES (?,?,?,'t','t')",
               (PROJECT, "corsia", str(tmp_path / "projects" / "corsia")))
    default = seed_default_account('claude-code', db=db)
    default = set_account_state(default['id'], 'signed_in', expected_revision=0, db=db)
    receipt = capture_receipt('claude-code', account_id=default['id'], db=db)
    assert mark_discovery(default['id'], receipt=receipt, status='ready',
                          models={MODEL: {'available': True, 'resolved_identity': 'synthetic'}}, db=db)
    second = add_account("claude-code", "Second", db=db)
    second = set_account_state(second["id"], "signed_in", expected_revision=0, db=db)
    receipt = capture_receipt("claude-code", account_id=second["id"], db=db)
    assert mark_discovery(second["id"], receipt=receipt, status="ready",
                          models={MODEL: {"available": True, "resolved_identity": "synthetic"}}, db=db)
    db.commit()
    yield db, default, second
    db.close()


def _policy(db, *, allowlist, default_id, switch):
    from vendor_account_fixtures import set_legacy_policy

    set_legacy_policy(PROJECT, "claude-code", allowlist=allowlist, default_account_id=default_id,
                      switch_on_limit=switch, db=db)
    db.commit()


def _limit(db, account):
    from connections import harness_usage
    from connections.account_receipts import capture_receipt

    harness_usage.record_limited(
        provider="claude-code", receipt=capture_receipt("claude-code", account_id=account["id"], db=db),
        reason="usage_limit", resets_at=_RESET, source="rate_limit_update", db=db)
    db.commit()


def _allow(db, account):
    from connections import harness_usage
    from connections.account_receipts import capture_receipt

    harness_usage.record_allowed(
        provider="claude-code", receipt=capture_receipt("claude-code", account_id=account["id"], db=db),
        source="discovery", db=db)
    db.commit()


@pytest.fixture(autouse=True)
def _single_model(monkeypatch):
    monkeypatch.setenv("CORSIA_REVIEWER_MODEL", "claude-opus-4-8")
    monkeypatch.delenv("CORSIA_REVIEWER_FALLBACK_MODEL", raising=False)
    monkeypatch.setattr("reviewer.reviewer_runner.get_agent_defaults", lambda: {})


def _review():
    from reviewer.reviewer_runner import run_cc_review

    with patch("reviewer.reviewer_runner.exact_review_source") as source:
        source.return_value.__enter__.return_value = ("SRC", "/tmp/neutral-review", "/tmp/repo.git")
        return run_cc_review(diff="d", context_text="", pr_number=1, repo="o/r", head_sha="a" * 40,
                             working_dir="/tmp", base_ref="dev", base_sha="b" * 40)


def _run(invoke):
    with patch("reviewer.reviewer_runner.invoke_cc_subagent", invoke), \
            patch("reviewer.reviewer_runner.set_pending") as pending, \
            patch("reviewer.reviewer_runner.set_failure") as failure, \
            patch("reviewer.reviewer_runner.emit_product_event"):
        return _review(), pending, failure


def _config_dir(env) -> Path:
    return Path(env["CLAUDE_CONFIG_DIR"]).resolve()


class TestPermittedAccountSelection:
    @pytest.mark.parametrize("how", ["rollover", "project_default"])
    def test_limited_default_runs_on_the_permitted_secondary(self, scope, how):
        db, default, second = scope
        _limit(db, default)
        _allow(db, second)
        if how == "rollover":
            _policy(db, allowlist=[default["id"], second["id"]], default_id=default["id"], switch=True)
        else:
            _policy(db, allowlist=[second["id"], default["id"]], default_id=second["id"], switch=False)
        seen = []

        def invoke(*_args, **kwargs):
            seen.append(kwargs)
            return "[]"

        result, _pending, failure = _run(invoke)

        assert result == ("[]", "claude-opus-4-8")
        failure.assert_not_called()
        assert len(seen) == 1 and seen[0]["model"] == "claude-opus-4-8"
        assert _config_dir(seen[0]["env"]) == Path(second["source_home"]).resolve()

    def test_a_usable_default_uses_its_own_claude_root(self, scope):
        db, default, second = scope
        _policy(db, allowlist=[default["id"], second["id"]], default_id=default["id"], switch=True)
        seen = []

        result, _pending, failure = _run(lambda *_a, **kwargs: seen.append(kwargs) or "[]")

        assert result == ("[]", "claude-opus-4-8")
        failure.assert_not_called()
        assert _config_dir(seen[0]["env"]) == Path(default["source_home"]).resolve()
        assert "ANTHROPIC_API_KEY" not in seen[0]["env"]

    def test_the_codex_reviewer_runs_in_an_isolated_account_home(self, scope, monkeypatch, tmp_path):
        import json
        from connections.account_evidence import mark_discovery
        from connections.account_receipts import capture_receipt
        from connections.model_cache_stage import ModelCacheStage
        from connections.vendor_accounts import add_account, set_account_state
        from reviewer.reviewer_runner import run_cc_review

        db, _, _ = scope
        account = add_account('codex', 'Codex', db=db)
        account = set_account_state(account['id'], 'signed_in', expected_revision=0, db=db)
        (Path(account['source_home']) / 'auth.json').write_text(json.dumps({'auth_mode': 'chatgpt', 'tokens': {'access_token': 'synthetic'}}))
        cache_file = tmp_path / 'models.json'
        cache_file.write_text(json.dumps({'models': [{'slug': 'gpt-5-codex'}]}))
        cache = ModelCacheStage.prepare(cache_file, Path(account['runtime_home']) / 'models_cache.json').promote()
        receipt = capture_receipt('codex', account_id=account['id'], db=db)
        mark_discovery(account['id'], receipt=receipt, status='ready',
                       models={'codex/gpt-5-codex': {'available': True}}, cache_receipt=cache, db=db)
        db.commit()
        monkeypatch.setenv("CORSIA_REVIEWER_MODEL", "codex/gpt-5-codex")
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-must-not-leak")
        seen = []

        def invoke(*_args, **kwargs):
            home = Path(kwargs["env"]["CODEX_HOME"])
            seen.append((kwargs["env"], home.is_dir()))
            return "[]"

        with patch("reviewer.reviewer_runner.invoke_codex_reviewer", invoke), \
                patch("reviewer.reviewer_runner.exact_review_source") as source, \
                patch("reviewer.reviewer_runner.set_failure") as failure:
            source.return_value.__enter__.return_value = ("SRC", "/tmp/neutral-review", "/tmp/repo.git")
            result = run_cc_review(diff="d", context_text="", pr_number=1, repo="o/r", head_sha="a" * 40,
                                   working_dir="/tmp", base_ref="dev", base_sha="b" * 40)

        assert result == ("[]", "codex/gpt-5-codex")
        failure.assert_not_called()
        env, existed_during_review = seen[0]
        assert existed_during_review and not Path(env["CODEX_HOME"]).exists()
        assert "OPENAI_API_KEY" not in env


class TestUnusableAccountsStayVisible:
    @pytest.mark.parametrize("cause", ["usage_limited", "account_signed_out", "account_not_configured", "account_project_denied"])
    def test_no_review_runs_and_the_account_cause_is_reported(self, scope, cause):
        db, default, second = scope
        if cause == "usage_limited":
            _limit(db, default)
            _allow(db, second)
            _policy(db, allowlist=[default["id"], second["id"]], default_id=default["id"], switch=False)
        elif cause == "account_project_denied":
            _policy(db, allowlist=[], default_id=None, switch=False)
        elif cause == "account_signed_out":
            db.execute("UPDATE vendor_accounts SET state='signed_out' WHERE id=?", (default["id"],))
            db.commit()
        else:
            # An unordered policy selects the next enabled account, so every account is removed.
            db.execute("UPDATE vendor_accounts SET state='removed' WHERE id IN (?,?)", (default["id"], second["id"]))
            db.commit()

        def invoke(*_args, **_kwargs):  # pragma: no cover - the assertion is that it is never reached
            raise AssertionError("a review ran on an unusable account")

        result, _pending, failure = _run(invoke)

        assert result is None
        failure.assert_called_once()
        description = failure.call_args[0][2]
        assert description.startswith("qa/llm-reviewer: model-unavailable")
        assert description.endswith(f"Reviewer account unavailable: {cause}")

    def test_a_removed_default_runs_on_the_next_permitted_account(self, scope):
        db, default, second = scope
        _allow(db, second)
        db.execute("UPDATE vendor_accounts SET state='removed' WHERE id=?", (default["id"],))
        db.commit()
        seen = []

        result, _pending, failure = _run(lambda *_a, **kwargs: seen.append(kwargs) or "[]")

        assert result == ("[]", "claude-opus-4-8")
        failure.assert_not_called()
        assert _config_dir(seen[0]["env"]) == Path(second["source_home"]).resolve()

    def test_a_missing_default_account_is_not_replaced_by_the_ambient_login(self, scope):
        db, default, second = scope
        db.execute("DELETE FROM vendor_accounts WHERE id IN (?,?)", (default["id"], second["id"]))
        db.commit()

        result, _pending, failure = _run(lambda *_a, **_k: "[]")

        assert result is None
        assert failure.call_args[0][2].endswith("Reviewer account unavailable: account_not_configured")


class TestAccountContainment:
    def test_an_account_outside_the_permitted_set_is_refused_at_the_seam(self, scope):
        db, default, second = scope
        _policy(db, allowlist=[second["id"]], default_id=second["id"], switch=False)

        def invoke(*_args, **_kwargs):  # pragma: no cover
            raise AssertionError("a review ran on an unpermitted account")

        # A selector that wrongly named the Default is not trusted: the reviewer checks the result.
        with patch("connections.account_policy.scope_account", return_value=(default["id"], None)):
            result, _pending, failure = _run(invoke)

        assert result is None
        assert "account_project_denied" in failure.call_args[0][2]

    def test_a_failed_selection_never_builds_a_launch_environment(self, scope):
        db, default, second = scope
        _limit(db, default)
        _policy(db, allowlist=[default["id"]], default_id=default["id"], switch=True)

        with patch("connections.account_operation.admit_operation") as admit, \
                patch("subagents.cc_env.clean_claude_env") as ambient:
            result, _pending, failure = _run(lambda *_a, **_k: "[]")

        assert result is None
        failure.assert_called_once()
        admit.assert_not_called()
        ambient.assert_not_called()

    def test_the_backends_carry_no_ambient_environment_or_source_home(self):
        import reviewer.reviewer_account as account
        import reviewer.reviewer_backends as backends
        import reviewer.reviewer_runner as runner

        for backend in (backends.invoke_cc_subagent, backends.invoke_codex_reviewer):
            assert inspect.signature(backend).parameters["env"].default is inspect.Parameter.empty
        for name in ("clean_claude_env", "clean_codex_env"):
            assert not hasattr(backends, name)
        for module in (account, backends, runner):
            assert "codex_source" not in Path(module.__file__).read_text()
