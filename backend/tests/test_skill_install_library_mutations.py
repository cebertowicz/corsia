"""Atomic library-skill update and removal writer behavior."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml


def _git(workspace: Path, *args: str) -> None:
    done = subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


@pytest.fixture
def library_skill(tmp_path):
    from infra.skill_install import install_skill_bundle, origin_metadata

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--quiet")
    _git(workspace, "-c", "user.email=test@example.invalid", "-c", "user.name=Test", "commit", "--allow-empty", "-qm", "root")
    installed = install_skill_bundle(
        workspace,
        "example",
        b"---\nname: example\ndescription: first version\n---\n# First\n",
        {"notes.md": b"first notes\n", "dropped.md": b"remove me\n"},
        "first version",
        origin_metadata("library/example@a:skills/example", b"first", "2026-10-19T00:00:00+00:00", "1.0.0"),
        author_thread_id="owner",
        scope="thread",
        thread_id="owner",
        reason="library skill install",
    )
    assert installed["outcome"] == "installed"
    target = workspace / ".threads" / "owner" / "skills" / "example"
    return workspace, target


def _origin(target: Path) -> dict:
    return yaml.safe_load((target / "corsia.yaml").read_text())["origin"]


def test_update_replaces_the_bundle_deletes_dropped_files_and_preserves_first_adoption(library_skill):
    from core.skill_sidecar import installed_digest
    from infra.skill_install import origin_metadata, update_skill_bundle

    workspace, target = library_skill
    before = _origin(target)
    updated = update_skill_bundle(
        workspace,
        "example",
        b"---\nname: example\ndescription: second version\n---\n# Second\n",
        {"notes.md": b"second notes\n", "nested/added.md": b"new\n"},
        "second version",
        origin_metadata("library/example@b:skills/example", b"second", "ignored", "2.0.0"),
        author_thread_id="owner",
        thread_id="owner",
    )

    assert (updated["outcome"], updated["history_outcome"]) == ("updated", "written")
    assert not (target / "dropped.md").exists()
    assert (target / "nested" / "added.md").read_bytes() == b"new\n"
    after = _origin(target)
    assert after["adopted_at"] == before["adopted_at"]
    assert after["version"] == "2.0.0"
    assert after["installed_digest"] == installed_digest({
        "SKILL.md": (target / "SKILL.md").read_bytes(),
        "notes.md": (target / "notes.md").read_bytes(),
        "nested/added.md": (target / "nested" / "added.md").read_bytes(),
        "corsia.yaml": (target / "corsia.yaml").read_bytes(),
    })


def test_update_refuses_if_a_file_changed_after_the_writer_read_it(library_skill, monkeypatch):
    from infra import skill_install
    from infra.skill_install import origin_metadata, update_skill_bundle

    workspace, target = library_skill
    original_save = skill_install.save_guidance

    def save_after_local_edit(*args, **kwargs):
        (target / "notes.md").write_text("edited after read\n")
        return original_save(*args, **kwargs)

    monkeypatch.setattr(skill_install, "save_guidance", save_after_local_edit)
    result = update_skill_bundle(
        workspace,
        "example",
        b"---\nname: example\ndescription: second version\n---\n# Second\n",
        {"notes.md": b"second notes\n"},
        "second version",
        origin_metadata("library/example@b:skills/example", b"second", "ignored", "2.0.0"),
        author_thread_id="owner",
        thread_id="owner",
    )

    assert (result["outcome"], result["history_outcome"]) == ("failed", "stale")
    assert (target / "notes.md").read_text() == "edited after read\n"
    assert (target / "dropped.md").exists()


def test_remove_deletes_the_thread_owned_bundle_in_one_guidance_save(library_skill):
    from infra.skill_install import remove_skill_bundle

    workspace, target = library_skill
    result = remove_skill_bundle(workspace, "example", author_thread_id="owner", thread_id="owner")

    assert (result["outcome"], result["history_outcome"]) == ("removed", "written")
    assert not target.exists()


@pytest.mark.parametrize("writer", ["update", "remove"])
def test_mutations_report_history_off_without_writing(library_skill, monkeypatch, writer):
    from infra import skill_install
    from infra.guidance_git import SaveResult
    from infra.skill_install import origin_metadata, remove_skill_bundle, update_skill_bundle

    workspace, target = library_skill
    before = {path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()}
    monkeypatch.setattr(skill_install, "save_guidance", lambda *_args, **_kwargs: SaveResult("history-off"))
    if writer == "update":
        result = update_skill_bundle(
            workspace,
            "example",
            b"---\nname: example\ndescription: second version\n---\n# Second\n",
            {"notes.md": b"second notes\n"},
            "second version",
            origin_metadata("library/example@b:skills/example", b"second", "ignored", "2.0.0"),
            author_thread_id="owner",
            thread_id="owner",
        )
    else:
        result = remove_skill_bundle(workspace, "example", author_thread_id="owner", thread_id="owner")

    assert (result["outcome"], result["history_outcome"]) == ("failed", "history-off")
    assert {path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()} == before


def test_reviewed_binary_bundle_is_recorded_and_uses_raw_byte_cas(tmp_path, monkeypatch):
    from infra import skill_install
    from infra.skill_install import install_skill_bundle, origin_metadata, update_skill_bundle

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "--quiet")
    _git(workspace, "-c", "user.email=test@example.invalid", "-c", "user.name=Test", "commit", "--allow-empty", "-qm", "root")
    installed = install_skill_bundle(
        workspace, "binary", b"---\nname: binary\ndescription: binary\n---\nbody\n",
        {"assets/image.bin": b"\x89PNG\x00old"}, "binary",
        origin_metadata("example/library@a:skills/binary", b"skill", "2026-10-19T00:00:00+00:00", "1.0.0"),
        author_thread_id="owner", scope="thread", thread_id="owner", binary_safe_history=True,
    )
    target = workspace / ".threads" / "owner" / "skills" / "binary"
    assert (installed["outcome"], installed["history_outcome"]) == ("installed", "written")
    updated = update_skill_bundle(
        workspace, "binary", b"---\nname: binary\ndescription: binary two\n---\nbody\n",
        {"assets/image.bin": b"\x89PNG\x00new"}, "binary two",
        origin_metadata("example/library@b:skills/binary", b"skill-two", "ignored", "2.0.0"),
        author_thread_id="owner", thread_id="owner",
    )
    assert (updated["outcome"], updated["history_outcome"]) == ("updated", "written")
    assert (target / "assets" / "image.bin").read_bytes() == b"\x89PNG\x00new"

    original_save = skill_install.save_guidance

    def save_after_binary_edit(*args, **kwargs):
        (target / "assets" / "image.bin").write_bytes(b"\x89PNG\x00local")
        return original_save(*args, **kwargs)

    monkeypatch.setattr(skill_install, "save_guidance", save_after_binary_edit)
    result = update_skill_bundle(
        workspace, "binary", b"---\nname: binary\ndescription: binary three\n---\nbody\n",
        {"assets/image.bin": b"\x89PNG\x00newer"}, "binary three",
        origin_metadata("example/library@c:skills/binary", b"skill-three", "ignored", "3.0.0"),
        author_thread_id="owner", thread_id="owner",
    )

    assert (result["outcome"], result["history_outcome"]) == ("failed", "stale")
    assert (target / "assets" / "image.bin").read_bytes() == b"\x89PNG\x00local"


@pytest.mark.parametrize("operation", ["update", "remove"])
def test_token_confirmed_snapshot_refuses_later_text_and_binary_edits(library_skill, monkeypatch, operation):
    """The mutation CAS stays bound to the bytes approved by the discard token."""
    from infra import skill_install
    from infra.skill_install import origin_metadata, remove_skill_bundle, update_skill_bundle
    from tools import skill_library

    workspace, target = library_skill
    asset = target / "assets" / "image.bin"
    asset.parent.mkdir()
    asset.write_bytes(b"\x89PNG\x00original")
    token = skill_library._local_edits_token("owner", "example", target)
    confirmed = skill_library._confirmed_bundle("owner", "example", target, token)
    assert confirmed is not None
    original_save = skill_install.save_guidance

    def save_after_late_edit(*args, **kwargs):
        (target / "notes.md").write_text("late text edit\n")
        asset.write_bytes(b"\x89PNG\x00late binary edit")
        return original_save(*args, **kwargs)

    monkeypatch.setattr(skill_install, "save_guidance", save_after_late_edit)
    if operation == "update":
        result = update_skill_bundle(
            workspace, "example", b"---\nname: example\ndescription: second version\n---\n# Second\n",
            {"notes.md": b"second notes\n"}, "second version",
            origin_metadata("library/example@b:skills/example", b"second", "ignored", "2.0.0"),
            author_thread_id="owner", thread_id="owner", confirmed_files=confirmed,
        )
    else:
        result = remove_skill_bundle(
            workspace, "example", author_thread_id="owner", thread_id="owner", confirmed_files=confirmed,
        )

    assert (result["outcome"], result["history_outcome"]) == ("failed", "stale")
    assert (target / "notes.md").read_text() == "late text edit\n"
    assert asset.read_bytes() == b"\x89PNG\x00late binary edit"
