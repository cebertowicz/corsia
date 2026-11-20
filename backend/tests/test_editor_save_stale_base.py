"""The editor's PUT refuses a save whose base the file has since left."""

from __future__ import annotations

import hashlib
import logging
import uuid
from urllib.parse import urlencode

import pytest

from auth_fixtures import OWNER_TOKEN
from doceditor.document_save_errors import EDITOR_STALE_CODE, EDITOR_STALE_MESSAGE

pytestmark = pytest.mark.integration
_AUTH = "?" + urlencode({"token": OWNER_TOKEN})
BASE = "# Plan\n\nÜber café 漢字 🙂\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def document(integration_client, integration_workspace):
    """An ordinary shared document with no history repository behind it."""
    name = f"Stale Base {uuid.uuid4().hex[:8]}"
    created = integration_client.post("/api/projects" + _AUTH, json={"name": name})
    assert created.status_code == 200, created.text
    root = integration_workspace / "projects" / name.lower().replace(" ", "-")
    category = root / "shared" / "documents"
    category.mkdir(parents=True)
    (root / "shared" / "INDEX.md").write_text("# Shared\n", encoding="utf-8")
    (category / "INDEX.md").write_text("# Documents\n", encoding="utf-8")
    path = category / "plan.md"
    path.write_text(BASE, encoding="utf-8")
    return path, created.json()["project"]["id"]


def _put(client, path, text, project_id, **extra):
    return client.put("/api/files/content" + _AUTH, json={
        "path": str(path), "text": text, "project_id": project_id, **extra})


@pytest.fixture(autouse=True)
def _digest_log_unspent(monkeypatch):
    from api import files

    monkeypatch.setattr(files, "_digest_absence_logged", False)


def test_a_matching_base_saves_and_the_next_save_builds_on_what_it_wrote(integration_client, document):
    path, project_id = document

    first = _put(integration_client, path, "first typed\n", project_id, expected_sha256=_sha(BASE))
    second = _put(integration_client, path, "second typed\n", project_id, expected_sha256=_sha("first typed\n"))

    assert first.status_code == second.status_code == 200, (first.text, second.text)
    # The receipt's revision is the digest the tab computes for its next base.
    assert first.json()["revision"] == _sha("first typed\n")
    assert path.read_text(encoding="utf-8") == "second typed\n"


def test_a_changed_file_refuses_with_the_editors_own_code_and_message_and_keeps_both_texts(integration_client, document):
    path, project_id = document
    path.write_text("outside edit\n", encoding="utf-8")

    refused = _put(integration_client, path, "typed after the outside edit\n", project_id, expected_sha256=_sha(BASE))

    assert refused.status_code == 409
    assert refused.json()["detail"] == {"code": EDITOR_STALE_CODE, "message": EDITOR_STALE_MESSAGE}
    assert path.read_text(encoding="utf-8") == "outside edit\n"


def test_the_editor_refusal_is_not_the_suggestion_or_live_wording(integration_client, document):
    from doceditor.document_save import STALE_MESSAGE
    from doceditor.document_save_errors import FileChangedOutside

    assert EDITOR_STALE_MESSAGE not in {STALE_MESSAGE, FileChangedOutside().message}
    assert EDITOR_STALE_CODE != "stale"
    assert "suggestion" not in EDITOR_STALE_MESSAGE


def test_a_deleted_file_still_answers_that_it_is_gone_not_that_it_changed(integration_client, document):
    path, project_id = document
    path.unlink()

    gone = _put(integration_client, path, "typed\n", project_id, expected_sha256=_sha(BASE))

    assert gone.status_code == 404


def test_an_old_client_without_a_digest_still_overwrites_and_the_gap_is_logged_once(integration_client, document, caplog):
    path, project_id = document
    path.write_text("outside edit\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="api.files"):
        one = _put(integration_client, path, "old client one\n", project_id)
        two = _put(integration_client, path, "old client two\n", project_id)

    assert one.status_code == two.status_code == 200, (one.text, two.text)
    assert path.read_text(encoding="utf-8") == "old client two\n"
    lines = [r for r in caplog.records if "without expected_sha256" in r.getMessage()]
    assert len(lines) == 1


def test_a_save_with_no_digest_over_non_utf8_bytes_still_saves(integration_client, document):
    # A base holding U+FFFD sends no digest (the lossy read could never match), so this is the path it takes.
    path, project_id = document
    path.write_bytes(b"caf\xe9\n")

    saved = _put(integration_client, path, "cafe\n", project_id)

    assert saved.status_code == 200, saved.text
    assert path.read_bytes() == b"cafe\n"


def test_a_client_that_sends_a_digest_never_logs_the_gap(integration_client, document, caplog):
    path, project_id = document

    with caplog.at_level(logging.WARNING, logger="api.files"):
        saved = _put(integration_client, path, "guarded\n", project_id, expected_sha256=_sha(BASE))

    assert saved.status_code == 200, saved.text
    assert not [r for r in caplog.records if "without expected_sha256" in r.getMessage()]


def test_a_guidance_file_refuses_a_stale_base_and_records_no_version(integration_client, integration_workspace):
    from test_guidance_history import SKILL_BODY, _git

    _git(["init", "--quiet"], cwd=integration_workspace)
    thread = integration_client.post("/api/threads" + _AUTH, json={"title": "stale-guidance", "project_id": None})
    assert thread.status_code == 200, thread.text
    thread_id = thread.json()["thread_id"]
    skill = integration_workspace / ".threads" / thread_id / "skills" / "demo" / "SKILL.md"
    body = {"path": str(skill), "thread_id": thread_id}
    created = integration_client.put("/api/files/content" + _AUTH, json={**body, "text": SKILL_BODY, "create": True})
    assert created.status_code == 200, created.text
    outside = SKILL_BODY.replace("Body text.", "Edited outside CORSIA.")
    skill.write_text(outside, encoding="utf-8")
    relative = f".threads/{thread_id}/skills/demo/SKILL.md"
    commits = _git(["rev-list", "--count", "HEAD", "--", relative], cwd=integration_workspace).strip()

    typed = SKILL_BODY.replace("Body text.", "Typed here.")
    stale = integration_client.put("/api/files/content" + _AUTH, json={**body, "text": typed, "expected_sha256": _sha(SKILL_BODY)})

    assert stale.status_code == 409
    assert stale.json()["detail"] == {"code": EDITOR_STALE_CODE, "message": EDITOR_STALE_MESSAGE}
    assert skill.read_text(encoding="utf-8") == outside
    assert _git(["rev-list", "--count", "HEAD", "--", relative], cwd=integration_workspace).strip() == commits

    fresh = integration_client.put("/api/files/content" + _AUTH, json={**body, "text": typed, "expected_sha256": _sha(outside)})

    assert fresh.status_code == 200, fresh.text
    assert skill.read_text(encoding="utf-8") == typed
    # History keeps the outside edit as its own version beside the save that followed it.
    assert int(_git(["rev-list", "--count", "HEAD", "--", relative], cwd=integration_workspace).strip()) == int(commits) + 2
