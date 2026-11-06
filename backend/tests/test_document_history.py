"""History reads existing Git versions without modifying the workspace."""
from pathlib import Path
import subprocess

import pytest
from fastapi import HTTPException

from infra import document_history
from infra.document_history import list_versions, version_text
from infra.guidance_git import GitFault
from infra.guidance_targets import GuidanceTarget


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test author")
    git(root, "config", "user.email", "test@example.test")
    return root


def commit(repo: Path, path: Path, text: str, message: str) -> str:
    path.write_text(text)
    git(repo, "add", "--", str(path.relative_to(repo)))
    git(repo, "commit", "--author=Test author <test@example.test>", "-qm", message)
    return git(repo, "rev-parse", "HEAD").strip()


def test_list_and_text_are_read_only(repo):
    path = repo / "sample.md"
    first = commit(repo, path, "one\n", "first")
    second = commit(repo, path, "two\n", "second\n\nReason: Update text\nScope: workspace")
    before = git(repo, "status", "--porcelain")
    rows, truncated = list_versions(repo, path.relative_to(repo).as_posix())
    assert [row["id"] for row in rows] == [second, first]
    assert truncated is False
    assert rows[0]["author"] == "Test author"
    assert rows[0]["reason"] == "Update text"
    assert rows[1]["reason"] is None
    assert version_text(repo, path.relative_to(repo).as_posix(), first) == "one\n"
    assert git(repo, "status", "--porcelain") == before
    assert git(repo, "rev-parse", "HEAD").strip() == second


def test_no_history_and_unrecorded_reason(repo):
    path = repo / "sample.md"
    path.write_text("untracked")
    assert list_versions(repo, path.relative_to(repo).as_posix()) == ([], False)
    commit(repo, path, "recorded", "sweep\n\nReason: unrecorded")
    assert list_versions(repo, path.relative_to(repo).as_posix())[0][0]["reason"] is None


def test_workspace_without_repository_has_no_versions(tmp_path):
    workspace = tmp_path / "plain"
    workspace.mkdir()
    (workspace / "sample.md").write_text("text")
    assert list_versions(workspace, "sample.md") == ([], False)


def test_list_reports_truncation_past_the_cap(repo, monkeypatch):
    monkeypatch.setattr(document_history, "HISTORY_CAP", 2)
    path = repo / "sample.md"
    first = commit(repo, path, "one", "first")
    commit(repo, path, "two", "second")
    assert list_versions(repo, "sample.md")[1] is False
    commit(repo, path, "three", "third")
    rows, truncated = list_versions(repo, "sample.md")
    assert len(rows) == 2 and truncated is True
    assert first not in [row["id"] for row in rows]
    assert version_text(repo, "sample.md", first) == "one"
    commit(repo, repo / "other.md", "other", "other")
    assert list_versions(repo, "other.md")[1] is False


def test_deleted_then_recreated_file_lists_only_versions_with_text(repo):
    path = repo / "sample.md"
    first = commit(repo, path, "before delete", "first")
    git(repo, "rm", "-q", "--", "sample.md")
    git(repo, "commit", "-qm", "delete")
    deleted = git(repo, "rev-parse", "HEAD").strip()
    recreated = commit(repo, path, "after delete", "recreate")
    rows, _ = list_versions(repo, "sample.md")
    assert [row["id"] for row in rows] == [recreated, first]
    assert version_text(repo, "sample.md", first) == "before delete"
    with pytest.raises(ValueError):
        version_text(repo, "sample.md", deleted)


def test_rejects_outside_and_option_paths_at_route(repo, tmp_path, monkeypatch):
    from api import files_history

    class DB:
        def close(self):
            pass

    monkeypatch.setattr(files_history, 'get_db', DB)
    monkeypatch.setattr(files_history.config, 'WORKSPACE', repo)
    outside = tmp_path / 'outside.md'
    outside.write_text('outside')
    option = repo / '-option'
    option.write_text('file')
    for path in (outside, option):
        monkeypatch.setattr(files_history, 'checked_read_path', lambda *_: (path, []))
        with pytest.raises(HTTPException) as refused:
            files_history.history(str(path), 'person', None, None)
        assert refused.value.status_code == 400
    from registry.path_access import checked_read, ReadRefused
    with pytest.raises(ReadRefused, match='not_file'):
        checked_read(str(repo), [repo])


def test_refuses_unknown_version(repo):
    path = repo / "sample.md"
    commit(repo, path, "one", "first")
    with pytest.raises(ValueError):
        version_text(repo, path.relative_to(repo).as_posix(), "--help")
    with pytest.raises(ValueError):
        version_text(repo, path.relative_to(repo).as_posix(), "0" * 40)


def test_git_fault_is_unavailable_not_unknown(repo, monkeypatch):
    from api import files_history

    class DB:
        def close(self):
            pass

    path = repo / "SKILL.md"
    commit(repo, path, "one", "first")
    monkeypatch.setattr(files_history, "get_db", DB)
    monkeypatch.setattr(files_history.config, "WORKSPACE", repo)
    monkeypatch.setattr(files_history, "checked_read_path", lambda *_: (path, []))
    monkeypatch.setattr("infra.guidance_targets.guidance_target", lambda *_: GuidanceTarget("skill"))

    def fault(*_args, **_kwargs):
        raise GitFault("fatal: unable to read tree")

    monkeypatch.setattr(document_history, "_git", fault)
    for commit_id in (None, "a" * 40):
        with pytest.raises(HTTPException) as failure:
            files_history.history(str(path), "person", None, None, commit_id)
        assert failure.value.status_code == 503


def test_route_checks_scope_and_thread_before_git(monkeypatch, tmp_path):
    from api import files_history

    class DB:
        def close(self):
            pass

    monkeypatch.setattr(files_history, "get_db", DB)
    monkeypatch.setattr(files_history, "owns_thread", lambda *args, **kwargs: False)
    monkeypatch.setattr(files_history, "list_versions", lambda *_: pytest.fail("history was read"))
    with pytest.raises(HTTPException) as failure:
        files_history.history("outside", "person", None, "foreign")
    assert failure.value.status_code == 404
    monkeypatch.setattr(files_history, "checked_read_path", lambda *_: (_ for _ in ()).throw(HTTPException(400)))
    with pytest.raises(HTTPException) as failure:
        files_history.history("outside", "person", None, None)
    assert failure.value.status_code == 400
