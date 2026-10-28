"""Staging takes a diff through the broker and keeps raw content off the event."""

import base64
import importlib.util
import json
from pathlib import Path
import sys

import pytest


PACKAGE = Path(__file__).resolve().parents[1] / "events/connectors/github_screen"
sys.path.insert(0, str(PACKAGE))
try:
    spec = importlib.util.spec_from_file_location("screen_stage", PACKAGE / "stage.py")
    stage_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stage_module)
finally:
    sys.path.remove(str(PACKAGE))


def test_stage_fetches_and_keeps_raw_pr_text_out_of_event(tmp_path):
    calls = []
    sha = "a" * 40
    def read(path, *, accept=None):
        calls.append((path, accept))
        if accept:
            return "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -0,0 +1 @@\n+hello\n"
        if "/git/commits/" in path:
            return '{"tree":{"sha":"' + 'c' * 40 + '"}}'
        if "/git/trees/" in path:
            return '{"tree":[]}'
        return '{"head":{"sha":"' + sha + '"},"base":{"sha":"' + 'b' * 40 + '"},"title":"TITLE_CANARY","body":"BODY_CANARY"}'
    event = {"facts": {"repo": "O/R", "pr_number": 1, "item_version": sha}}
    facts = stage_module.stage(event, "project", tmp_path, "o/r", read)
    assert facts["repo"] == "o/r"
    assert "CANARY" not in repr(facts)
    assert len(calls) == 6 and calls[1][1] == "application/vnd.github.v3.diff"
    assert "TITLE_CANARY" in (tmp_path / facts["staging_path"] / "pull-request.json").read_text()


def test_workflow_blobs_are_read_by_sha():
    root, github, workflows, blob = (c * 40 for c in "abcd")
    reads = []
    def read(path, **_kwargs):
        reads.append(path)
        key = path.rsplit("/", 1)[-1]
        if "/commits/" in path:
            return json.dumps({"tree": {"sha": root}})
        if key == root:
            return json.dumps({"tree": [{"path": ".github", "type": "tree", "sha": github}]})
        if key == github:
            return json.dumps({"tree": [{"path": "workflows", "type": "tree", "sha": workflows}]})
        if key == workflows:
            return json.dumps({"tree": [{"path": "build.yml", "type": "blob", "sha": blob}]})
        return json.dumps({"encoding": "base64", "content": base64.b64encode(b'run: ./scripts/test.sh').decode()})
    assert stage_module.workflow_texts("o/r", "e" * 40, read) == ["run: ./scripts/test.sh"]
    assert reads[-1].endswith("/blobs/" + blob)


def test_stage_checks_workflows_at_both_exact_commits(tmp_path, monkeypatch):
    head, base = "a" * 40, "b" * 40
    revisions = []
    def workflows(repo, revision, _read):
        revisions.append((repo, revision))
        return ["run: ./scripts/base.sh"] if revision == base else ["run: scripts/head.sh"]
    monkeypatch.setattr(stage_module, "workflow_texts", workflows)
    def read(_path, *, accept=None):
        if accept:
            return ("diff --git a/scripts/head.sh b/scripts/head.sh\n"
                    "--- a/scripts/head.sh\n+++ b/scripts/head.sh\n@@ -0,0 +1 @@\n+hi\n")
        return json.dumps({"head": {"sha": head}, "base": {"sha": base}})
    event = {"facts": {"repo": "o/r", "pr_number": 1, "item_version": head}}
    facts = stage_module.stage(event, "project", tmp_path, "o/r", read)
    floor = json.loads((tmp_path / facts["staging_path"] / "floor.json").read_text())
    assert "floor.ci_file" in floor["floor_hit"]
    assert revisions == [("o/r", base), ("o/r", head)]


def test_stale_head_does_not_stage(tmp_path):
    event = {"facts": {"repo": "o/r", "pr_number": 1, "item_version": "a" * 40}}
    with pytest.raises(RuntimeError, match="head moved"):
        stage_module.stage(event, "project", tmp_path, "o/r", lambda *_args, **_kw: '{"head":{"sha":"new"}}')
    assert not list(tmp_path.iterdir())


def test_stage_rejects_untrusted_identity_before_read(tmp_path):
    def read(*_args, **_kwargs):
        pytest.fail("a rejected identity must not reach the broker")
    for repo, number, version, reason in [("o/other", 1, "a" * 40, "tracked repository"),
                                          ("o/r", "1", "a" * 40, "integer"),
                                          ("o/r", 1, "main", "head SHA")]:
        event = {"facts": {"repo": repo, "pr_number": number, "item_version": version}}
        with pytest.raises(ValueError, match=reason):
            stage_module.stage(event, "project", tmp_path, "o/r", read)
    assert not list(tmp_path.iterdir())
