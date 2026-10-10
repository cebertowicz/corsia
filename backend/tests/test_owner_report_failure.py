"""A report to the owner can fail; the write that already landed never does."""
from __future__ import annotations

import hashlib
import logging

import pytest

from tests.test_owner_only_edit import (
    _managed,
    _save,
    _scope_root_doc,
    ownership_scope as ownership_scope,  # noqa: F401 - the fixture
)


def _failing_report(monkeypatch):
    from doceditor.feedback_edit_preparation import FeedbackPreparationChanged
    from registry import shared_document_events

    def fail(*_args, **_kwargs):
        raise FeedbackPreparationChanged("The document owner changed during publication preparation")

    monkeypatch.setattr(shared_document_events, "report_owner_write", fail)


def test_file_tool_write_succeeds_and_completes_when_the_owner_report_fails(
    ownership_scope, monkeypatch, caplog,
):
    from tools import file_write_completion

    scope = ownership_scope
    path, artifact_id = _scope_root_doc(scope)
    notices = []
    real_notice = file_write_completion.record_context_source_change
    monkeypatch.setattr(
        file_write_completion, "record_context_source_change",
        lambda *args, **kwargs: notices.append(path) or real_notice(*args, **kwargs),
    )
    _failing_report(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="tools.file_write_completion"):
        result = _managed(scope, path, "conductor", content="landed\n")
    assert result == "ok"
    assert path.read_text(encoding="utf-8") == "landed\n"
    assert notices == [path]
    warning = " ".join(record.getMessage() for record in caplog.records if record.levelno == logging.WARNING)
    assert artifact_id in warning
    assert "FeedbackPreparationChanged" in warning


def test_plain_save_keeps_its_hash_and_succeeds_when_the_owner_report_fails(
    ownership_scope, monkeypatch, caplog,
):
    from registry import service
    from registry.identity_metadata import last_written_sha256

    scope = ownership_scope
    path, artifact_id = _scope_root_doc(scope)
    _failing_report(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="doceditor.document_save"):
        _save(scope, path, "conductor")
    assert path.read_text(encoding="utf-8") == "saved text\n"
    artifact = service.find_file_identity(str(path), db=scope["db"])
    assert last_written_sha256(artifact["metadata"]) == hashlib.sha256(b"saved text\n").hexdigest()
    warning = " ".join(record.getMessage() for record in caplog.records if record.levelno == logging.WARNING)
    assert artifact_id in warning
    assert "FeedbackPreparationChanged" in warning
