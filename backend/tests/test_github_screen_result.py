"""Screen payload validation and the fixed floor."""

import json

# Validation assertions below run through the production settlement entry point.

from automation.worker_settle import verdict
from events.connectors.github_screen.workflow import workflow

SCREEN_SCHEMA = workflow("project").corsia["workflow"]["result"]


def answer(payload, signs=None):
    return json.dumps({"payload": payload, "injection_signs": signs or []})


def test_email_screen_prompt_is_unchanged():
    import hashlib
    from connections.mail.email_screen import SYSTEM_PROMPT

    assert hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest() == "97cf502a45e6c3d237bce1fa4767248b040f5eb02e24b93e567fdcc91154a1d7"


def test_screen_workflow_is_available_only_to_its_package():
    from events.connectors import get_manifest
    from events.connector_workflows import resolve_shipped_workflow

    version = get_manifest("github").version
    resolved = resolve_shipped_workflow(f"rule_set:github@{version}", "github-pr-screen")
    assert resolved.error is None
    row = resolved.declaration.load("project")
    assert row.name == "github-pr-screen" and row.corsia["workflow"]["result"]
    contract = resolved.declaration.result
    assert "R1:" in contract.instructions() and len(contract.version()) == 64
    assert resolve_shipped_workflow("agent:someone", "github-pr-screen") is None
    assert resolve_shipped_workflow("rule_set:github@wrong", "github-pr-screen").error


def _context(facts=None):
    from events.connectors import get_manifest

    manifest = get_manifest("github")
    return manifest.shipped_workflows["github-pr-screen"].result.context(
        f"rule_set:github@{manifest.version}", "github-pr-screen", facts or {},
    )


def test_captured_floor_cannot_be_changed_by_worker(tmp_path):
    from automation.worker_settle import verdict
    from events.connectors.github_screen.result import capture_floor

    record = tmp_path / "floor.json"
    record.write_text(json.dumps({"paths": ["a"], "floor_hit": ["floor.github_dir"]}))
    captured = capture_floor(str(tmp_path))
    record.write_text(json.dumps({"paths": ["a"], "floor_hit": []}))
    context = _context()
    context["record"] = captured
    check = {"result_context": context}
    text = answer({"verdict": "safe", "categories": ["credential reach"], "findings": []})
    outcome, facts = verdict({"status": "completed"}, text, SCREEN_SCHEMA, check)
    assert outcome == "returned"
    result = json.loads(facts["result"])
    assert result["verdict"] == "flagged"
    assert set(result["categories"]) == {"credential reach", "floor.github_dir"}
    context["record"] = None
    assert verdict({"status": "completed"}, text, SCREEN_SCHEMA, check)[0] == "no_result"
    assert verdict({"status": "completed"}, "invalid", SCREEN_SCHEMA, check)[1]["reason"] == "invalid"


def test_validator_defect_does_not_recover_invalid(monkeypatch):
    def raises(_payload, _record):
        raise RuntimeError("unreadable")

    monkeypatch.setattr("events.connectors.github_screen.result.validate_captured_result", raises)
    context = _context()
    context["record"] = {}
    check = {"result_context": context}
    text = answer({"verdict": "flagged", "categories": ["credential reach"], "findings": []})
    outcome, facts = verdict({"status": "completed"}, text, SCREEN_SCHEMA, check)
    assert outcome == "no_result" and "Couldn’t" in facts["reason"]
    assert "Check the backend log" in facts["reason"]
    assert verdict({"status": "completed"}, "not a result", SCREEN_SCHEMA, check)[1]["reason"] == "invalid"


def test_screen_envelope_keeps_identity_and_unknown_signs():
    from automation.worker_settle import settlement
    from events.connectors import get_manifest

    row = {"id": 4, "thread_id": "owner", "rule_id": "rule", "event_id": "event",
           "request_identity": "request", "connector": "screen", "event_type": "staged",
           "artifact_external_id": "o/r#1", "artifact_integration": "github",
           "artifact_type": "pull-request", "workflow_id": "github-pr-screen",
           "origin": f"rule_set:github@{get_manifest('github').version}"}
    check = {"result_context": _context({"repo": "o/r", "pr_number": 1,
                                          "item_version": "a" * 40, "staging_path": "screen-a"})}
    result = settlement(row, check, "invalid", {"reason": "invalid"})
    assert result.facts["source"] == "outside" and result.facts["kind"] == "pull request"
    assert result.facts["repo"] == "o/r" and result.facts["pr_number"] == 1
    assert result.facts["item_version"] == "a" * 40 and result.facts["valid"] is False
    assert result.facts["injection_signs"] is None and "result" not in result.facts
    assert result.staging_path == "screen-a"


def test_remove_screen_staging_keeps_other_requests(tmp_path):
    from automation.worker_inputs import remove_staging_dir

    first = tmp_path / "staging" / "screen-first"
    second = tmp_path / "staging" / "screen-second"
    first.mkdir(parents=True)
    second.mkdir()
    remove_staging_dir(tmp_path, "screen-first")
    remove_staging_dir(tmp_path, ".")
    assert not first.exists() and second.is_dir()


def _screen_verdict(text, paths, floor_hits):
    context = _context()
    context["record"] = {"paths": list(paths), "floor_hit": list(floor_hits)}
    return verdict({"status": "completed"}, text, SCREEN_SCHEMA, {"result_context": context})


def _validated(text, paths, floor_hits):
    outcome, facts = _screen_verdict(text, paths, floor_hits)
    assert outcome == "returned", facts
    return {"payload": json.loads(facts["result"]), "injection_signs": facts["injection_signs"]}


def test_floor_cannot_be_cleared_by_model():
    text = answer({"verdict": "safe", "categories": [], "findings": []})
    result = _validated(text, {"src/a.py"}, {"floor.github_dir"})["payload"]
    assert result == {"verdict": "flagged", "categories": ["floor.github_dir"],
                      "findings": [], "floor_hit": ["floor.github_dir"]}


def test_floor_preserves_unsafe_verdict():
    text = answer({"verdict": "unsafe", "categories": [], "findings": []})
    result = _validated(text, {"a"}, {"floor.binary"})["payload"]
    assert result["verdict"] == "unsafe"
    assert result["floor_hit"] == ["floor.binary"]


def test_signs_raise_safe_to_flagged_and_must_be_unique():
    payload = {"verdict": "safe", "categories": [], "findings": []}
    result = _validated(answer(payload, ["R1"]), {"a"}, set())
    assert result["payload"]["verdict"] == "flagged"
    assert result["injection_signs"] == ["R1"]
    for signs in (["R1", "R1"], ["none"], ["R6"]):
        outcome, facts = _screen_verdict(answer(payload, signs), {"a"}, set())
        assert outcome == "invalid" and facts["reason"] == "invalid"


def test_finding_path_must_name_staged_file():
    text = answer({"verdict": "flagged", "categories": ["hidden payload"],
                   "findings": [{"path": "src/missing.py", "text": "payload"}]})
    outcome, facts = _screen_verdict(text, {"src/a.py"}, set())
    assert outcome == "invalid" and "outside the staged diff" in facts["detail"]


def test_unexpected_content_is_invalid():
    for result in [
        {"verdict": "safe", "categories": [], "findings": [], "extra": "text"},
        {"verdict": "safe", "categories": [], "findings": [{"path": "a", "text": "x", "extra": "text"}]},
        {"verdict": "safe", "categories": [], "findings": [{"path": "a", "text": "x" * 201}]},
    ]:
        assert _screen_verdict(answer(result), {"a"}, set())[0] == "invalid"
