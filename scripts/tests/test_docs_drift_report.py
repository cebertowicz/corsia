"""Tests for the verdict-wiring report module (check_docs_drift.report)."""

from __future__ import annotations

from check_docs_drift.citation_check import CitationResult
from check_docs_drift.private_scan_check import PrivateScanResult
from check_docs_drift.report import overall_ok, to_json, to_text
from check_docs_drift.starter_kit_check import StarterKitResult


def _clean_results():
    return StarterKitResult(), CitationResult(), PrivateScanResult(
        owner_identity_skipped_reason=None
    )


def test_overall_ok_is_true_when_all_three_checks_pass():
    sk, ci, ps = _clean_results()

    assert overall_ok(sk, ci, ps) is True


def test_overall_ok_is_false_when_starter_kit_could_not_execute_and_was_required():
    """The exact bug this test set exists to catch: a skip must not read as a pass."""
    sk = StarterKitResult(skipped_reason="workspace not found", skip_is_failure=True)
    _, ci, ps = _clean_results()

    assert overall_ok(sk, ci, ps) is False


def test_overall_ok_is_true_when_starter_kit_skip_was_explicitly_allowed():
    sk = StarterKitResult(skipped_reason="workspace not found", skip_is_failure=False)
    _, ci, ps = _clean_results()

    assert overall_ok(sk, ci, ps) is True


def test_overall_ok_is_false_when_owner_identity_could_not_be_resolved():
    sk, ci, _ = _clean_results()
    ps = PrivateScanResult(owner_identity_skipped_reason="no owner identity resolved")

    assert overall_ok(sk, ci, ps) is False


def test_to_text_reports_fail_when_any_check_fails():
    sk = StarterKitResult(skipped_reason="workspace not found", skip_is_failure=True)
    _, ci, ps = _clean_results()

    assert to_text(sk, ci, ps).splitlines()[-1] == "FAIL"


def test_to_text_reports_pass_when_all_clean():
    sk, ci, ps = _clean_results()

    assert to_text(sk, ci, ps).splitlines()[-1] == "PASS"


def test_to_json_top_level_ok_matches_overall_ok():
    import json

    sk = StarterKitResult(skipped_reason="workspace not found", skip_is_failure=True)
    _, ci, ps = _clean_results()

    payload = json.loads(to_json(sk, ci, ps))

    assert payload["ok"] is False
    assert payload["starter_kit"]["skipped_reason"] == "workspace not found"


def test_to_text_names_a_skipped_owner_identity_sub_check():
    sk, ci, _ = _clean_results()
    ps = PrivateScanResult(
        owner_identity_skipped_reason="no owner identity resolved",
        owner_identity_required=False,
    )

    out = to_text(sk, ci, ps)

    assert "owner-identity sub-check SKIPPED: no owner identity resolved" in out
    assert out.splitlines()[-1] == (
        "PASS (owner-identity sub-check NOT run: no owner identity resolved)"
    )


def test_verdict_names_an_internal_name_skip_as_well():
    sk, ci, _ = _clean_results()
    ps = PrivateScanResult(internal_names_skipped_reason="no internal-names registry configured")

    assert to_text(sk, ci, ps).splitlines()[-1] == "PASS (internal-name sub-check NOT run)"


def test_to_json_verdict_is_not_a_plain_pass_over_a_skipped_sub_check():
    import json

    sk, ci, _ = _clean_results()
    ps = PrivateScanResult(
        owner_identity_skipped_reason="no owner identity resolved",
        owner_identity_required=False,
    )

    payload = json.loads(to_json(sk, ci, ps))

    assert payload["ok"] is True
    assert payload["verdict"] == (
        "PASS (owner-identity sub-check NOT run: no owner identity resolved)"
    )
