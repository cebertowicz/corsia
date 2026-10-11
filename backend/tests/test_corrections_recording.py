"""Focused behavior tests for the correction-recording backend path."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

OWNER = "owner"


def _db():
    from infra.database import get_db

    return get_db()


def _thread(
    db, thread_id: str = "thread", project_id: str = "project", *, user_id: str = OWNER,
    parent_thread_id: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,'t','t')",
        (project_id, project_id),
    )
    db.execute(
        "INSERT INTO threads (id,user_id,title,project_id,parent_thread_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (thread_id, user_id, thread_id, project_id, parent_thread_id,
         "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z"),
    )


def _message(
    db, message_id: str, role: str, content: str, *, thread_id: str = "thread",
    metadata: str = "{}", at: str,
) -> None:
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES (?,?,?,?,?,?)",
        (message_id, thread_id, role, content, metadata, at),
    )


def test_rubric_hash_is_pinned_to_the_approved_v1_text():
    from core.corrections_recording import RUBRIC_V1, checker_version, sha256_text

    assert sha256_text(RUBRIC_V1) == "32e7f5cdf83d1a0837b85e752e68ff2c233cc49987d5764d3bdbd4e3f845f954"
    version = checker_version("omlx@server/model")
    assert version.model_id == "omlx@server/model" and len(version.prompt_sha256) == 64


def test_classification_parses_one_raw_json_object():
    from core.corrections_recording import validate_classification

    assert validate_classification(
        '{"label":"correction","reason":"It identifies an incorrect delivery."}'
    ) == {
        "label": "correction",
        "reason": "It identifies an incorrect delivery.",
    }


@pytest.mark.parametrize(
    "raw",
    [
        '{"label":"correction",',
        '{"label":"correction","reason":"One sentence."}{"label":"not_correction","reason":"Second."}',
        '[]',
    ],
)
def test_classification_rejects_invalid_nonobject_and_trailing_json(raw):
    from core.corrections_recording import validate_classification

    with pytest.raises(ValueError):
        validate_classification(raw)


@pytest.mark.parametrize(
    ("role", "content", "metadata"),
    [
        ("assistant", "This is wrong", "{}"),
        ("user", "   ", "{}"),
        ("user", "This is wrong", json.dumps({"sender_kind": "agent"})),
        ("user", "[EVENT: wake]", "{}"),
        ("user", "[CORSIA status]", "{}"),
        ("user", "This is wrong", json.dumps({"routed_event": "event"})),
        ("user", "This is wrong", json.dumps({"send_idempotency_key": "key"})),
    ],
)
def test_owner_message_filter_excludes_non_owner_and_system_rows(role, content, metadata):
    from core.corrections_recording import is_owner_authored_message

    assert not is_owner_authored_message(role=role, content=content, metadata=metadata)


def test_owner_message_filter_accepts_a_real_user_message():
    from core.corrections_recording import is_owner_authored_message

    assert is_owner_authored_message(role="user", content="That claim is wrong.", metadata="{}")



@pytest.mark.asyncio
async def test_pending_candidate_prefilter_matches_wrapper_prefixes_case_sensitively(isolated_db):
    from core.corrections_recording import checker_version
    from jobs import corrections_recording as job

    db = _db()
    try:
        _thread(db)
        _message(db, "wrapper", "user", "[CORSIA plan] notice", at="2026-10-01T00:00:00Z")
        _message(db, "lowercase-owner", "user", "[corsia is wrong here]", at="2026-10-01T00:01:00Z")
        db.commit()
    finally:
        db.close()

    batch = await job._next_pending_batch(checker_version("model-a"), 20, None)

    assert [candidate.message_id for candidate in batch.candidates] == ["lowercase-owner"]



@pytest.mark.asyncio
async def test_pending_candidates_keyset_breaks_equal_timestamp_ties_by_message_id(isolated_db):
    from core.corrections_recording import checker_version
    from jobs import corrections_recording as job

    db = _db()
    try:
        _thread(db)
        for message_id in ("a", "b", "c"):
            _message(db, message_id, "user", f"correction {message_id}", at="2026-10-01T00:01:00Z")
        db.commit()
    finally:
        db.close()

    batch = await job._next_pending_batch(
        checker_version("model-a"), 20, ("2026-10-01T00:01:00Z", "b"),
    )

    assert [candidate.message_id for candidate in batch.candidates] == ["a"]


@pytest.mark.asyncio
async def test_global_batch_selects_newest_candidates_across_owners(isolated_db, monkeypatch):
    from infra.settings import set_setting
    from jobs import corrections_recording as job

    db = _db()
    try:
        _thread(db, "owner-root", "owner-project")
        _thread(db, "other-root", "other-project", user_id="other")
        _message(db, "owner-earliest", "user", "owner earliest", thread_id="owner-root", at="2026-10-01T00:01:00Z")
        _message(db, "other-earliest", "user", "other earliest", thread_id="other-root", at="2026-10-01T00:01:00Z")
        _message(db, "owner-later", "user", "owner later", thread_id="owner-root", at="2026-10-01T00:02:00Z")
        _message(db, "other-latest", "user", "other latest", thread_id="other-root", at="2026-10-01T00:03:00Z")
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(job, "_resolve_target", lambda model: (SimpleNamespace(available=True), None))
    seen: list[str] = []
    output_budgets: list[int] = []

    async def classify(*args, **kwargs):
        seen.append(kwargs["user_prompt"])
        output_budgets.append(kwargs["max_output_tokens"])
        return kwargs["validator"]({"label": "correction", "reason": "It corrects an earlier delivery."})

    set_setting("agents", {"defaults": {"corrections_checker_model": "model-a"}})
    ticks = iter([0.0, 0.0, 5.0])
    result = await job.run_corrections_recording(
        batch_size=2, invoke=classify, time_budget_seconds=1.0, clock=lambda: next(ticks),
    )

    assert result == {"status": "ok", "processed": 2, "recorded": 2, "refused": 0, "failed": 0}
    assert ["other latest" in prompt for prompt in seen] == [True, False]
    assert job.CLASSIFIER_MAX_OUTPUT_TOKENS == 4096
    assert output_budgets == [job.CLASSIFIER_MAX_OUTPUT_TOKENS] * 2
    assert ["owner later" in prompt for prompt in seen] == [False, True]


@pytest.mark.asyncio
async def test_global_batch_hydrates_only_selected_keys(isolated_db, monkeypatch):
    from core.corrections_recording import checker_version
    from jobs import corrections_recording as job
    from jobs import corrections_scanning

    db = _db()
    try:
        _thread(db, "owner-root", "owner-project")
        _thread(db, "other-root", "other-project", user_id="other")
        for index in range(3):
            _message(
                db, f"owner-{index}", "user", f"owner {index}", thread_id="owner-root",
                at=f"2026-10-01T00:0{index}:00Z",
            )
            _message(
                db, f"other-{index}", "user", f"other {index}", thread_id="other-root",
                at=f"2026-10-01T00:0{index}:30Z",
            )
        db.commit()
    finally:
        db.close()
    hydrated: list[list[str]] = []

    def record_hydration(keys):
        hydrated.append([key.message_id for key in keys])
        return corrections_scanning.hydrate_candidates(keys)

    monkeypatch.setattr(job, "hydrate_candidates", record_hydration)

    batch = await job._next_pending_batch(checker_version("model-a"), 2, None)

    assert [candidate.message_id for candidate in batch.candidates] == ["other-2", "owner-2"]
    assert [key.message_id for key in batch.selected_keys] == ["other-2", "owner-2"]
    assert hydrated == [["other-2", "owner-2"]]


@pytest.mark.asyncio
async def test_run_skips_sql_passing_non_owner_keys_and_reaches_older_messages(isolated_db, monkeypatch):
    from infra.settings import get_setting, set_setting
    from jobs import corrections_recording as job

    db = _db()
    try:
        _thread(db)
        # Vertical tab is whitespace to str.strip(), but outside the SQL prefilter's
        # four-character trim set, so these rows consume selected keys then hydrate out.
        for index in range(25):
            _message(
                db, f"ignored-{index}", "user", "\v", at=f"2026-10-01T00:{index:02d}:00Z",
            )
        for index in range(3):
            _message(
                db, f"owner-{index}", "user", f"owner correction {index}",
                at=f"2026-09-30T23:5{index}:00Z",
            )
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(job, "_resolve_target", lambda model: (SimpleNamespace(available=True), None))
    set_setting("agents", {"defaults": {"corrections_checker_model": "model-a"}})
    seen: list[str] = []

    async def classify(*args, **kwargs):
        seen.append(kwargs["user_prompt"])
        return kwargs["validator"]({"label": "correction", "reason": "It corrects a delivery."})

    result = await job.run_corrections_recording(
        batch_size=20, invoke=classify, time_budget_seconds=1.0, clock=lambda: 0.0,
    )

    assert result == {"status": "ok", "processed": 3, "recorded": 3, "refused": 0, "failed": 0}
    assert [f"owner correction {index}" in "\n".join(seen) for index in range(3)] == [True, True, True]
    assert get_setting("corrections_recording_status")["stop_reason"] == "empty"


@pytest.mark.asyncio
async def test_run_drains_multiple_newest_first_batches_and_records_empty_stop(isolated_db, monkeypatch):
    from infra.settings import get_setting, set_setting
    from jobs import corrections_recording as job

    db = _db()
    try:
        _thread(db)
        for index in range(5):
            _message(
                db, f"message-{index}", "user", f"correction {index}",
                at=f"2026-10-01T00:00:0{index}Z",
            )
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(job, "_resolve_target", lambda model: (SimpleNamespace(available=True), None))
    set_setting("agents", {"defaults": {"corrections_checker_model": "model-a"}})
    seen: list[str] = []

    async def classify(*args, **kwargs):
        seen.append(kwargs["user_prompt"])
        return kwargs["validator"]({"label": "correction", "reason": "It corrects the delivery."})

    result = await job.run_corrections_recording(batch_size=2, invoke=classify)

    assert result == {"status": "ok", "processed": 5, "recorded": 5, "refused": 0, "failed": 0}
    assert [f"correction {index}" in prompt for index, prompt in zip(range(4, -1, -1), seen)] == [True] * 5
    assert get_setting("corrections_recording_status")["stop_reason"] == "empty"


@pytest.mark.asyncio
async def test_run_continues_to_older_rows_after_a_per_message_failure(isolated_db, monkeypatch):
    from infra.settings import set_setting
    from jobs import corrections_recording as job

    db = _db()
    try:
        _thread(db)
        _message(db, "older", "user", "older correction", at="2026-10-01T00:00:01Z")
        _message(db, "newer", "user", "newer correction", at="2026-10-01T00:00:02Z")
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(job, "_resolve_target", lambda model: (SimpleNamespace(available=True), None))
    set_setting("agents", {"defaults": {"corrections_checker_model": "model-a"}})
    seen: list[str] = []

    async def classify(*args, **kwargs):
        prompt = kwargs["user_prompt"]
        seen.append(prompt)
        if "newer correction" in prompt:
            return kwargs["validator"]({"label": "wrong-shape", "reason": "Malformed."})
        return kwargs["validator"]({"label": "correction", "reason": "It corrects the delivery."})

    result = await job.run_corrections_recording(batch_size=1, invoke=classify)

    assert result == {"status": "ok", "processed": 2, "recorded": 1, "refused": 1, "failed": 0}
    assert ["newer correction" in prompt for prompt in seen] == [True, False]


@pytest.mark.asyncio
async def test_run_stops_at_the_budget_and_leaves_remaining_candidates_pending(isolated_db, monkeypatch):
    from infra.settings import get_setting, set_setting
    from jobs import corrections_recording as job

    db = _db()
    try:
        _thread(db)
        for index in range(3):
            _message(db, f"message-{index}", "user", f"correction {index}", at=f"2026-10-01T00:00:0{index}Z")
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(job, "_resolve_target", lambda model: (SimpleNamespace(available=True), None))
    set_setting("agents", {"defaults": {"corrections_checker_model": "model-a"}})
    ticks = iter([0.0, 0.0, 5.0])

    async def classify(*args, **kwargs):
        return kwargs["validator"]({"label": "correction", "reason": "It corrects the delivery."})

    result = await job.run_corrections_recording(
        batch_size=1, invoke=classify, time_budget_seconds=1.0, clock=lambda: next(ticks),
    )

    assert result == {"status": "ok", "processed": 1, "recorded": 1, "refused": 0, "failed": 0}
    assert get_setting("corrections_recording_status")["stop_reason"] == "budget"
    pending = await job._next_pending_batch(job.checker_version("model-a"), 20, None)
    assert len(pending.candidates) == 2


@pytest.mark.asyncio
async def test_cancellation_between_calls_writes_a_truthful_status_and_keeps_pending_rows(isolated_db, monkeypatch):
    from infra.settings import get_setting, set_setting
    from jobs import corrections_recording as job

    db = _db()
    try:
        _thread(db)
        _message(db, "message-1", "user", "first correction", at="2026-10-01T00:00:01Z")
        _message(db, "message-2", "user", "second correction", at="2026-10-01T00:00:02Z")
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(job, "_resolve_target", lambda model: (SimpleNamespace(available=True), None))
    set_setting("agents", {"defaults": {"corrections_checker_model": "model-a"}})
    calls = 0

    async def classify(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError()
        return kwargs["validator"]({"label": "correction", "reason": "It corrects the delivery."})

    with pytest.raises(asyncio.CancelledError):
        await job.run_corrections_recording(batch_size=1, invoke=classify)

    status = get_setting("corrections_recording_status")
    assert status["status"] == "cancelled" and status["processed"] == 1
    assert status["stop_reason"] == "shutdown"
    pending = await job._next_pending_batch(job.checker_version("model-a"), 20, None)
    assert len(pending.candidates) == 1


def test_interactive_only_checker_setting_is_refused(isolated_db, monkeypatch):
    from api import system

    db = _db()
    try:
        db.execute(
            "INSERT INTO network_servers (id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at,metadata_json) "
            "VALUES ('restricted','external','external','restricted','Restricted',443,'online','t','t','t',?)",
            (json.dumps({"agent_use_restriction": "interactive_only"}),),
        )
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(system, "_ready_public_litellm_models", lambda: {"external@restricted/glm"})

    with pytest.raises(HTTPException, match="interactive-only endpoint"):
        system._save_exact_model_defaults({"corrections_checker_model": "external@restricted/glm"})


def test_endpoint_agent_use_defaults_allowed_for_existing_endpoint(isolated_db):
    from llm.endpoint_policy import endpoint_agent_use

    db = _db()
    try:
        db.execute(
            "INSERT INTO network_servers (id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at,metadata_json) "
            "VALUES ('existing','external','external','existing','Existing',443,'online','t','t','t',?)",
            (json.dumps({"base_url": "https://existing.example/v1"}),),
        )
        db.commit()
    finally:
        db.close()

    assert endpoint_agent_use("external@existing/model") == "allowed"


@pytest.mark.parametrize("raw", ["{not json", json.dumps({"agent_use_restriction": "batch_ok"})])
def test_endpoint_agent_use_refuses_unattended_use_when_the_terms_cannot_be_read(isolated_db, raw):
    from llm.endpoint_policy import endpoint_agent_use

    db = _db()
    try:
        db.execute(
            "INSERT INTO network_servers (id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at,metadata_json) "
            "VALUES ('unreadable','external','external','unreadable','Unreadable',443,'online','t','t','t',?)",
            (raw,),
        )
        db.commit()
    finally:
        db.close()

    assert endpoint_agent_use("external@unreadable/model") == "interactive_only"


def test_owner_message_filter_drops_whitespace_only_messages_in_sql():
    import sqlite3

    from core.corrections_recording import owner_message_sql_predicate

    where, params = owner_message_sql_predicate()
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE messages (id TEXT, role TEXT, content TEXT, metadata TEXT)")
    db.executemany(
        "INSERT INTO messages VALUES (?, 'user', ?, '{}')",
        [("blank", " \t\r\n "), ("spoken", "  fix the header  ")],
    )
    kept = [row[0] for row in db.execute(f"SELECT id FROM messages m WHERE {where} ORDER BY id", params)]

    assert kept == ["spoken"]


@pytest.mark.asyncio
async def test_interactive_only_checker_is_skipped_at_runtime(isolated_db, monkeypatch):
    from infra.settings import set_setting
    from jobs import corrections_recording as job

    db = _db()
    try:
        db.execute(
            "INSERT INTO network_servers (id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at,metadata_json) "
            "VALUES ('restricted','external','external','restricted','Restricted',443,'online','t','t','t',?)",
            (json.dumps({"agent_use_restriction": "interactive_only"}),),
        )
        db.commit()
    finally:
        db.close()
    set_setting("agents", {"defaults": {"corrections_checker_model": "external@restricted/glm"}})
    monkeypatch.setattr(job, "_resolve_target", lambda _: pytest.fail("restricted model was resolved"))

    assert await job.run_corrections_recording() == {
        "status": "unavailable", "processed": 0, "recorded": 0, "refused": 0, "failed": 0,
    }


@pytest.mark.asyncio
async def test_reclassification_on_version_change_and_idempotent_rerun(isolated_db, monkeypatch):
    from infra.settings import set_setting
    from jobs import corrections_recording as job

    db = _db()
    try:
        _thread(db)
        _message(db, "agent", "assistant", "I shipped it.", at="2026-10-01T00:00:00Z")
        _message(db, "owner-message", "user", "That result is wrong.", at="2026-10-01T00:01:00Z")
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(job, "_resolve_target", lambda model: (SimpleNamespace(available=True), None))

    async def correction(*args, **kwargs):
        return kwargs["validator"]({"label": "correction", "reason": "It identifies a wrong delivered result."})

    set_setting("agents", {"defaults": {"corrections_checker_model": "model-a"}})
    assert await job.run_corrections_recording(batch_size=1, invoke=correction) == {
        "status": "ok", "processed": 1, "recorded": 1, "refused": 0, "failed": 0,
    }
    assert await job.run_corrections_recording(batch_size=1, invoke=correction) == {
        "status": "ok", "processed": 0, "recorded": 0, "refused": 0, "failed": 0,
    }
    set_setting("agents", {"defaults": {"corrections_checker_model": "model-b"}})
    assert (await job.run_corrections_recording(batch_size=1, invoke=correction))["recorded"] == 1

    db = _db()
    try:
        rows = db.execute(
            "SELECT attributes FROM outcome_events WHERE event_name='measurement.correction' ORDER BY observed_ts"
        ).fetchall()
        assert len(rows) == 2
        assert {json.loads(row[0])["checker_model_id"] for row in rows} == {"model-a", "model-b"}
    finally:
        db.close()


def _seed_one_message():
    db = _db()
    try:
        _thread(db)
        _message(db, "agent", "assistant", "I shipped it.", at="2026-10-01T00:00:00Z")
        _message(db, "owner-message", "user", "That result is wrong.", at="2026-10-01T00:01:00Z")
        db.commit()
    finally:
        db.close()


def _facts():
    db = _db()
    try:
        return [
            json.loads(row[0]) for row in db.execute(
                "SELECT attributes FROM outcome_events WHERE event_name='measurement.correction'"
            ).fetchall()
        ]
    finally:
        db.close()


def test_reason_formatting_never_discards_a_valid_verdict():
    from core.corrections_recording import MAX_REASON_CHARS, validate_classification

    folded = validate_classification('{"label":"correction","reason":"Line one.\\nLine two."}')
    assert folded == {"label": "correction", "reason": "Line one. Line two."}
    long = validate_classification(json.dumps({"label": "not_correction", "reason": "x" * 900}))
    assert len(long["reason"]) == MAX_REASON_CHARS
    fenced = validate_classification('```json\n{"label":"correction","reason":"Wrong."}\n```')
    assert fenced == {"label": "correction", "reason": "Wrong."}


@pytest.mark.parametrize(
    "raw",
    ['{"label":"maybe","reason":"No."}', '{"label":"correction","reason":"  "}', '{"label":"correction"}'],
)
def test_a_missing_or_unknown_verdict_is_still_rejected(raw):
    from core.corrections_recording import validate_classification

    with pytest.raises(ValueError):
        validate_classification(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        ("truncated", "output_truncated"),
        ("invalid", "invalid_output"),
    ],
)
async def test_unusable_output_records_a_typed_refusal_once(isolated_db, monkeypatch, error, code):
    from infra.settings import set_setting
    from jobs import corrections_recording as job
    from llm.isolated_structured import IsolatedStructuredOutputTruncatedError

    _seed_one_message()
    monkeypatch.setattr(job, "_resolve_target", lambda model: (SimpleNamespace(available=True), None))
    set_setting("agents", {"defaults": {"corrections_checker_model": "model-a"}})

    async def unusable(*args, **kwargs):
        if error == "truncated":
            raise IsolatedStructuredOutputTruncatedError("reviewer_output_truncated")
        return kwargs["validator"]({"label": "wrong-shape", "reason": "No."})

    result = await job.run_corrections_recording(batch_size=1, invoke=unusable)
    assert result == {"status": "ok", "processed": 1, "recorded": 0, "refused": 1, "failed": 0}
    assert [(fact["label"], fact["reason"]) for fact in _facts()] == [("refused", code)]
    # The refusal is the message's fact under this version: it is not re-asked.
    again = await job.run_corrections_recording(batch_size=1, invoke=unusable)
    assert again["processed"] == 0 and len(_facts()) == 1


@pytest.mark.asyncio
async def test_provider_outage_stops_after_one_call_and_leaves_the_backlog_pending(isolated_db, monkeypatch):
    from infra.settings import get_setting, set_setting
    from jobs import corrections_recording as job
    from llm.isolated_structured import IsolatedStructuredUnavailableError

    db = _db()
    try:
        _thread(db)
        _message(db, "older", "user", "older correction", at="2026-10-01T00:00:01Z")
        _message(db, "newer", "user", "newer correction", at="2026-10-01T00:00:02Z")
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(job, "_resolve_target", lambda model: (SimpleNamespace(available=True), None))
    set_setting("agents", {"defaults": {"corrections_checker_model": "model-a"}})
    calls = 0

    async def down(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise IsolatedStructuredUnavailableError("down")

    result = await job.run_corrections_recording(batch_size=2, invoke=down)

    assert result == {"status": "provider_unavailable", "processed": 0, "recorded": 0, "refused": 0, "failed": 0}
    assert calls == 1 and _facts() == []
    pending = await job._next_pending_batch(job.checker_version("model-a"), 20, None)
    assert [candidate.message_id for candidate in pending.candidates] == ["newer", "older"]
    assert get_setting("corrections_recording_status")["stop_reason"] == "provider_unavailable"


@pytest.mark.asyncio
async def test_timeout_skips_one_pending_message_and_continues_to_the_next(isolated_db, monkeypatch):
    from infra.settings import set_setting
    from jobs import corrections_recording as job
    from llm.isolated_structured import IsolatedStructuredTimeoutError

    db = _db()
    try:
        _thread(db)
        _message(db, "older", "user", "older correction", at="2026-10-01T00:00:01Z")
        _message(db, "newer", "user", "newer correction", at="2026-10-01T00:00:02Z")
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(job, "_resolve_target", lambda model: (SimpleNamespace(available=True), None))
    set_setting("agents", {"defaults": {"corrections_checker_model": "model-a"}})
    calls: list[str] = []

    async def timeout_then_record(*args, **kwargs):
        prompt = kwargs["user_prompt"]
        calls.append(prompt)
        if "newer correction" in prompt:
            raise IsolatedStructuredTimeoutError("slow")
        return kwargs["validator"]({"label": "correction", "reason": "It corrects the delivery."})

    result = await job.run_corrections_recording(batch_size=2, invoke=timeout_then_record)

    assert result == {"status": "ok", "processed": 1, "recorded": 1, "refused": 0, "failed": 0}
    assert ["newer correction" in prompt for prompt in calls] == [True, False]
    pending = await job._next_pending_batch(job.checker_version("model-a"), 20, None)
    assert [candidate.message_id for candidate in pending.candidates] == ["newer"]


@pytest.mark.asyncio
async def test_a_refused_write_is_a_counted_failure_not_a_checker_refusal(isolated_db, monkeypatch):
    from infra.outcome_event_input import OutcomeInputRefusal
    from infra.settings import set_setting
    from jobs import corrections_recording as job

    _seed_one_message()
    monkeypatch.setattr(job, "_resolve_target", lambda model: (SimpleNamespace(available=True), None))
    set_setting("agents", {"defaults": {"corrections_checker_model": "model-a"}})

    def refused_write(*args, **kwargs):
        raise OutcomeInputRefusal("write refused")

    monkeypatch.setattr(job, "_record_fact", refused_write)

    async def good(*args, **kwargs):
        return kwargs["validator"]({"label": "correction", "reason": "It is wrong."})

    result = await job.run_corrections_recording(batch_size=1, invoke=good)
    assert result == {"status": "partial", "processed": 1, "recorded": 0, "refused": 0, "failed": 1}
    assert _facts() == []


@pytest.mark.asyncio
async def test_a_fact_that_already_existed_is_not_counted_as_recorded(isolated_db, monkeypatch):
    from infra.settings import set_setting
    from jobs import corrections_recording as job

    _seed_one_message()
    monkeypatch.setattr(job, "_resolve_target", lambda model: (SimpleNamespace(available=True), None))
    set_setting("agents", {"defaults": {"corrections_checker_model": "model-a"}})
    monkeypatch.setattr(job, "_record_fact", lambda *args, **kwargs: False)

    async def good(*args, **kwargs):
        return kwargs["validator"]({"label": "correction", "reason": "It is wrong."})

    result = await job.run_corrections_recording(batch_size=1, invoke=good)
    assert result == {"status": "ok", "processed": 1, "recorded": 0, "refused": 0, "failed": 0}


@pytest.mark.asyncio
async def test_a_failed_write_does_not_abort_the_rest_of_the_batch(isolated_db, monkeypatch):
    from infra.outcome_event_input import OutcomeInputRefusal
    from infra.settings import set_setting
    from jobs import corrections_recording as job

    db = _db()
    try:
        _thread(db)
        _message(db, "agent", "assistant", "I shipped it.", at="2026-10-01T00:00:00Z")
        _message(db, "m1", "user", "That is wrong.", at="2026-10-01T00:01:00Z")
        _message(db, "m2", "user", "Also wrong.", at="2026-10-01T00:02:00Z")
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(job, "_resolve_target", lambda model: (SimpleNamespace(available=True), None))
    set_setting("agents", {"defaults": {"corrections_checker_model": "model-a"}})
    real = job._record_fact
    calls = []

    def flaky(candidate, classification, version):
        calls.append(candidate.message_id)
        if len(calls) == 1:
            raise OutcomeInputRefusal("write refused")
        return real(candidate, classification, version)

    monkeypatch.setattr(job, "_record_fact", flaky)

    async def good(*args, **kwargs):
        return kwargs["validator"]({"label": "correction", "reason": "It is wrong."})

    result = await job.run_corrections_recording(batch_size=2, invoke=good)
    assert result == {"status": "partial", "processed": 2, "recorded": 1, "refused": 0, "failed": 1}
    assert len(_facts()) == 1


@pytest.mark.asyncio
async def test_a_rate_limit_stops_the_run_with_no_fact(isolated_db, monkeypatch):
    from infra.settings import set_setting
    from jobs import corrections_recording as job
    from llm.isolated_structured import IsolatedStructuredRateLimitError

    _seed_one_message()
    monkeypatch.setattr(job, "_resolve_target", lambda model: (SimpleNamespace(available=True), None))
    set_setting("agents", {"defaults": {"corrections_checker_model": "model-a"}})

    async def limited(*args, **kwargs):
        raise IsolatedStructuredRateLimitError("limited")

    result = await job.run_corrections_recording(batch_size=1, invoke=limited)
    assert result == {"status": "provider_unavailable", "processed": 0, "recorded": 0, "refused": 0, "failed": 0}
    assert _facts() == []


@pytest.mark.asyncio
async def test_a_context_overflow_is_refused_once_as_input_too_long(isolated_db, monkeypatch):
    from infra.settings import set_setting
    from jobs import corrections_recording as job
    from llm.isolated_structured import IsolatedStructuredContextOverflowError

    _seed_one_message()
    monkeypatch.setattr(job, "_resolve_target", lambda model: (SimpleNamespace(available=True), None))
    set_setting("agents", {"defaults": {"corrections_checker_model": "model-a"}})

    async def overflow(*args, **kwargs):
        raise IsolatedStructuredContextOverflowError("too long")

    result = await job.run_corrections_recording(batch_size=1, invoke=overflow)
    assert result == {"status": "ok", "processed": 1, "recorded": 0, "refused": 1, "failed": 0}
    assert [(fact["label"], fact["reason"]) for fact in _facts()] == [("refused", "input_too_long")]
    assert (await job.run_corrections_recording(batch_size=1, invoke=overflow))["processed"] == 0
