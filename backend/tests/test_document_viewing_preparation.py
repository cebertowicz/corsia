"""Viewing reads must release the DB worker and parse only the anchored prefix."""
from __future__ import annotations

import inspect
import json
from unittest.mock import AsyncMock

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from tests.test_document_viewing_context import KEY, SCOPE_LINE, _admit, _row, viewed_doc  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "parse"])
async def test_send_document_work_runs_off_db_worker(deleg_db, viewed_doc, monkeypatch, operation):
    from infra import document_viewing
    from infra.send_service import send_thread_input

    _, artifact_id = viewed_doc
    _admit(deleg_db, "busy")
    calls = []

    def assert_off_db():
        assert not any(frame.function == "_run_db_job" for frame in inspect.stack()), (
            "document work occupies DB worker"
        )
        calls.append(operation)

    if operation == "read":
        original = document_viewing.read_doc_text

        def checked_read(path):
            assert_off_db()
            return original(path)

        monkeypatch.setattr(document_viewing, "read_doc_text", checked_read)
    else:
        original = document_viewing.MarkdownIt.parse

        def checked_parse(parser, text, env=None):
            assert_off_db()
            return original(parser, text, env)

        monkeypatch.setattr(document_viewing.MarkdownIt, "parse", checked_parse)
    thread = dict(deleg_db.execute("SELECT * FROM threads WHERE id='sender-self'").fetchone())
    await send_thread_input(
        "sender-self", thread, "Review this section", native_mode="ws", ws=AsyncMock(),
        delivery_intent="queue_after_turn", input_id="off-worker",
        metadata={KEY: {"artifact_id": artifact_id, "heading_anchor": 3, "heading_line": SCOPE_LINE}},
    )
    assert calls == [operation]
    assert json.loads(_row(deleg_db, "off-worker")["metadata"])[KEY]["heading"] == "(Scope) 'now'"


def test_deep_anchor_parses_only_prefix_including_setext_underline(deleg_db, viewed_doc, monkeypatch):
    from infra import document_viewing

    path, artifact_id = viewed_doc
    prefix = "intro\n\n" * 200 + "Deep heading\n---\n"
    path.write_text(prefix + "\n# Later\n" + "unneeded tail\n" * 1000)
    parse = document_viewing.MarkdownIt.parse
    parsed = []

    def checked_parse(parser, text, env=None):
        parsed.append(text)
        assert text == prefix, "Markdown parser receives content after anchored heading"
        return parse(parser, text, env)

    monkeypatch.setattr(document_viewing.MarkdownIt, "parse", checked_parse)
    _admit(deleg_db, "prefix", {KEY: {
        "artifact_id": artifact_id, "heading_anchor": 401, "heading_line": "Deep heading",
    }})
    assert parsed == [prefix]
    assert json.loads(_row(deleg_db, "prefix")["metadata"])[KEY]["heading"] == "Deep heading"


def test_registered_repository_document_outside_workspace_is_named_by_its_full_path(
    deleg_db, tmp_path, monkeypatch,
):
    from registry import service

    path = tmp_path / "repository" / "outside.md"
    path.parent.mkdir()
    path.write_text("# Outside\n")
    monkeypatch.setattr(service, "get_allowed_dirs_rw", lambda **kwargs: [path.parent])
    artifact = service.register_write_strict("sender-self", str(path), db=deleg_db, notify=False)
    _admit(deleg_db, "outside-workspace", {KEY: {
        "artifact_id": artifact["id"], "heading_anchor": 1, "heading_line": "# Outside",
    }})
    assert json.loads(_row(deleg_db, "outside-workspace")["metadata"])[KEY] == {
        "path": str(path.resolve()), "heading": "Outside",
    }


def test_a_long_full_path_is_cut_in_the_middle_and_keeps_its_file_name(deleg_db, tmp_path, monkeypatch):
    """A path over the note's cap is marked as cut and keeps its file name; it cannot be opened as written."""
    from core.viewing_context import VIEWING_PATH_CAP
    from registry import service

    folder = tmp_path / "repository" / ("deep-" * 24) / ("deep-" * 24 + "folder")
    folder.mkdir(parents=True)
    path = folder / "the-file-name.md"
    path.write_text("# Outside\n")
    assert len(str(path.resolve())) > VIEWING_PATH_CAP
    monkeypatch.setattr(service, "get_allowed_dirs_rw", lambda **kwargs: [tmp_path / "repository"])
    artifact = service.register_write_strict("sender-self", str(path), db=deleg_db, notify=False)
    _admit(deleg_db, "long-path", {KEY: {"artifact_id": artifact["id"]}})
    named = json.loads(_row(deleg_db, "long-path")["metadata"])[KEY]["path"]
    assert len(named) == VIEWING_PATH_CAP == 240 and "…" in named and named.endswith("/the-file-name.md")


def test_unreadable_registered_roots_omit_the_note_and_admit_the_message(deleg_db, tmp_path, monkeypatch):
    from infra import input_preparation
    from infra.document_viewing import materialize_viewing_document
    from infra.project_repo_policy import RepoPolicyQueryError
    from registry import service

    path = tmp_path / "repository" / "outside.md"
    path.parent.mkdir()
    path.write_text("# Outside\n")
    monkeypatch.setattr(service, "get_allowed_dirs_rw", lambda **kwargs: [path.parent])
    artifact = service.register_write_strict("sender-self", str(path), db=deleg_db, notify=False)

    def unreadable(**kwargs):
        raise RepoPolicyQueryError("repository policy query failed: locked")

    monkeypatch.setattr(service, "get_allowed_dirs_rw", unreadable)
    request = {KEY: {"artifact_id": artifact["id"]}}
    prepared = materialize_viewing_document(input_preparation.prepare_viewing_document(
        "sender-self", "user", "roots-unreadable-probe", request, deleg_db,
    ))
    # A failed policy read is not the answer "outside every allowed root".
    assert (prepared.note, prepared.outcome) == (None, "roots_unreadable")
    _admit(deleg_db, "roots-unreadable", request)
    assert KEY not in json.loads(_row(deleg_db, "roots-unreadable")["metadata"])


def test_document_outside_every_allowed_root_has_no_note(deleg_db, tmp_path, monkeypatch):
    from infra import input_preparation
    from infra.document_viewing import materialize_viewing_document
    from registry import service

    path = tmp_path / "repository" / "outside.md"
    path.parent.mkdir()
    path.write_text("# Outside\n")
    monkeypatch.setattr(service, "get_allowed_dirs_rw", lambda **kwargs: [path.parent])
    artifact = service.register_write_strict("sender-self", str(path), db=deleg_db, notify=False)
    # The repository is no longer registered: the file still exists, the grant does not.
    monkeypatch.setattr(service, "get_allowed_dirs_rw", lambda **kwargs: [])
    request = {KEY: {"artifact_id": artifact["id"], "heading_anchor": 1}}

    prepared = materialize_viewing_document(input_preparation.prepare_viewing_document(
        "sender-self", "user", "no-grant-probe", request, deleg_db,
    ))
    assert (prepared.note, prepared.outcome) == (None, "outside_allowed")
    _admit(deleg_db, "no-grant", request)
    assert KEY not in json.loads(_row(deleg_db, "no-grant")["metadata"])


@pytest.mark.asyncio
async def test_send_without_document_skips_viewing_preparation(deleg_db, monkeypatch):
    from infra import input_preparation
    from infra.send_service import send_thread_input

    def refuse(*_args, **_kwargs):
        raise AssertionError("a send naming no document queued viewing preparation")

    _admit(deleg_db, "busy-plain")
    monkeypatch.setattr(input_preparation, "prepare_viewing_document", refuse)
    monkeypatch.setattr(input_preparation, "materialize_viewing_document", refuse)
    thread = dict(deleg_db.execute("SELECT * FROM threads WHERE id='sender-self'").fetchone())
    await send_thread_input(
        "sender-self", thread, "No document open", native_mode="ws", ws=AsyncMock(),
        delivery_intent="queue_after_turn", input_id="plain-send",
    )
    assert KEY not in json.loads(_row(deleg_db, "plain-send")["metadata"] or "{}")
