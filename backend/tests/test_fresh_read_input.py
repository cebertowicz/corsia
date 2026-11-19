"""What a fresh read is given, and how long its records live.

The cap bounds the whole request a reader's model is sent; the records end with the
thread that wrote them or the document they show.
"""
from __future__ import annotations

import pytest

from core.fresh_read_candidates import Candidate, install_cap, request_measure
from core.fresh_read_prompt import Context, request_text, user_prompt
from core.fresh_read_store import (
    PASSED, ReadRecord, record, record_person_edit, sha256_bytes, view,
)
from delegation_fixtures import deleg_db  # noqa: F401
from test_fresh_read_gate import _registered

pytestmark = pytest.mark.fresh_read

#: The local seat the cap-size read refused (window and output allowance from its catalog row).
_SEAT = Candidate(model="omlx@proof-seat/Qwen3.8-Flash-Next-oQ4e-mtp", window=262_144, output_allowance=32_768,
                  long_context_bound=None, server_id=None, is_local=True, writer_own=False)


def test_the_request_at_the_cap_fits_the_window_of_a_reader_with_no_bundled_tokenizer():
    # Dense lines, as every line of the person's messages starts with a timestamp.
    messages = tuple((f"2026-11-{i % 28 + 1:02d}T10:{i % 60:02d}:00Z", f"ok {i} — “dir” été ✓ {'x' * (i % 7)}")
                     for i in range(40_000, 0, -1))
    cap = install_cap([_SEAT])
    measure = request_measure([_SEAT])

    prompt, cut_from = user_prompt("update", "Done: the report is attached.", (("report.md", "# Report\n" * 200),),
                                   Context(task='{"title": "Report"}', messages=messages, note_to_reader=None),
                                   cap, measure)

    sent = len(request_text(prompt).encode("utf-8"))
    assert sent + _SEAT.output_allowance <= _SEAT.window
    assert cut_from is not None and cut_from[0].isdigit()  # the cap cut the messages, and says from when
    assert sent > cap - 200  # the room is used, not left empty by an over-wide reserve


def test_a_mix_with_any_reader_lacking_a_tokenizer_counts_bytes():
    claude = Candidate(model="claude-code/sonnet", window=200_000, output_allowance=2_000, long_context_bound=None,
                       server_id=None, is_local=False, writer_own=True)
    text = "été ✓"
    assert request_measure([_SEAT, claude])(text) == len(text.encode("utf-8"))
    assert request_measure([])(text) == len(text.encode("utf-8"))


def test_with_no_cap_no_message_goes_in_and_the_reader_is_told():
    prompt, cut_from = user_prompt("update", "Done.", (), Context(task=None, messages=(), note_to_reader=None),
                                   None, len)
    assert cut_from == "none: no input cap is known" and "(cut: none: no input cap is known)\n(none)" in prompt


def _pass_document(db, artifact_id: str, body: bytes, writer: str) -> str:
    return record(ReadRecord(kind="document_open", item_ref=artifact_id, sha256=sha256_bytes(body), verdict=PASSED,
                             writer_thread_id=writer, documents=((artifact_id, sha256_bytes(body), body),)), db=db)


def test_retiring_a_document_drops_its_view_bytes_and_the_persons_edits(deleg_db):
    from registry import models as registry_models

    retired, _path = _registered(deleg_db, "retired.md", "one")
    kept, _path = _registered(deleg_db, "kept.md", "same")
    _pass_document(deleg_db, retired["id"], b"one", "sender-self")
    record_person_edit(retired["id"], sha256_bytes(b"one, edited"), b"one, edited", db=deleg_db)
    _pass_document(deleg_db, kept["id"], b"same", "sender-self")

    registry_models.retire(retired["id"], db=deleg_db)

    assert view(retired["id"], db=deleg_db) is None
    assert view(kept["id"], db=deleg_db)["body"] == b"same"
    bodies = {bytes(row["body"]) for row in deleg_db.execute("SELECT body FROM fresh_read_bytes")}
    assert bodies == {b"same"}
    edits = deleg_db.execute("SELECT COUNT(*) FROM fresh_reads WHERE verdict='person_edit'").fetchone()[0]
    assert edits == 0


@pytest.mark.parametrize("operation", ["purge", "delete"])
def test_a_thread_purge_or_delete_takes_its_reads(isolated_db, operation):
    from datetime import datetime, timezone

    from core.thread_family import delete_thread_family
    from core.thread_lifecycle import _apply_purge
    from doc_feedback_helpers import seed_thread
    from infra.database import get_db

    db = get_db()
    try:
        seed_thread(db, "thread")
        seed_thread(db, "other")
        for writer in ("thread", "other"):
            record(ReadRecord(kind="update", item_ref=writer, sha256="a", verdict=PASSED, writer_thread_id=writer),
                   db=db)
        if operation == "purge":
            _apply_purge(db, "thread", datetime.now(timezone.utc))
        else:
            delete_thread_family(db, ["thread"])
        assert [row[0] for row in db.execute("SELECT writer_thread_id FROM fresh_reads")] == ["other"]
    finally:
        db.rollback()
        db.close()
