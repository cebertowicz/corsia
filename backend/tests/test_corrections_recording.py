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


def test_pending_candidates_exclude_another_users_root_and_subtree(isolated_db):
    from core.corrections_recording import checker_version
    from jobs import corrections_recording as job

    db = _db()
    try:
        _thread(db, "owner-root", "owner-project")
        _thread(db, "owner-child", "owner-project-child", parent_thread_id="owner-root")
        _thread(db, "other-root", "other-project", user_id="other")
        _thread(db, "other-child", "other-project-child", user_id="other", parent_thread_id="other-root")
        _message(db, "owner-root-message", "user", "That delivery is wrong.", thread_id="owner-root", at="2026-10-01T00:01:00Z")
        _message(db, "owner-child-message", "user", "Please redo this.", thread_id="owner-child", at="2026-10-01T00:02:00Z")
        _message(db, "other-root-message", "user", "Do not classify me.", thread_id="other-root", at="2026-10-01T00:03:00Z")
        _message(db, "other-child-message", "user", "Do not classify me either.", thread_id="other-child", at="2026-10-01T00:04:00Z")
        db.commit()
    finally:
        db.close()

    candidates = job._pending_candidates(checker_version("model-a"), 20, OWNER)

    assert [candidate.message_id for candidate in candidates] == [
        "owner-root-message", "owner-child-message",
    ]


@pytest.mark.asyncio
async def test_global_batch_selects_oldest_candidates_across_owners(isolated_db, monkeypatch):
    from infra.settings import set_setting
    from jobs import corrections_recording as job

    db = _db()
    try:
        _thread(db, "owner-root", "owner-project")
        _thread(db, "other-root", "other-project", user_id="other")
        _message(db, "z-owner-earliest", "user", "owner earliest", thread_id="owner-root", at="2026-10-01T00:01:00Z")
        _message(db, "a-other-earliest", "user", "other earliest", thread_id="other-root", at="2026-10-01T00:01:00Z")
        _message(db, "owner-later", "user", "owner later", thread_id="owner-root", at="2026-10-01T00:02:00Z")
        _message(db, "other-later", "user", "other later", thread_id="other-root", at="2026-10-01T00:03:00Z")
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
    result = await job.run_corrections_recording(batch_size=2, invoke=classify)

    assert result == {"status": "ok", "processed": 2, "recorded": 2, "failed": 0}
    assert ["other earliest" in prompt for prompt in seen] == [True, False]
    assert job.CLASSIFIER_MAX_OUTPUT_TOKENS == 2048
    assert output_budgets == [job.CLASSIFIER_MAX_OUTPUT_TOKENS] * 2
    assert ["owner earliest" in prompt for prompt in seen] == [False, True]


def test_token_plan_checker_setting_is_refused(monkeypatch):
    from api import system

    monkeypatch.setattr(system, "_ready_public_litellm_models", lambda: {"token-plan-model"})
    monkeypatch.setattr(system, "is_alibaba_token_plan_model", lambda model: model == "token-plan-model")
    with pytest.raises(HTTPException, match="Alibaba Token Plan"):
        system._save_exact_model_defaults({"corrections_checker_model": "token-plan-model"})


def test_token_plan_detection_uses_the_external_connection(isolated_db):
    from infra.settings import is_alibaba_token_plan_model

    db = _db()
    try:
        db.execute(
            "INSERT INTO network_servers (id,provider,kind,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at,metadata_json) "
            "VALUES ('token-plan','external','external','token-plan','Alibaba Token Plan',443,'online','t','t','t',?)",
            (json.dumps({"base_url": "https://token-plan.example.com/compatible-mode/v1"}),),
        )
        db.execute(
            "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,display_name,created_at,updated_at) "
            "VALUES ('token-model','native','external','glm','external@token-plan/glm','Token model','t','t')"
        )
        db.commit()
        assert is_alibaba_token_plan_model("external@token-plan/glm", db=db)
    finally:
        db.close()


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
        "status": "ok", "processed": 1, "recorded": 1, "failed": 0,
    }
    assert await job.run_corrections_recording(batch_size=1, invoke=correction) == {
        "status": "ok", "processed": 0, "recorded": 0, "failed": 0,
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


@pytest.mark.asyncio
async def test_malformed_output_records_nothing_and_is_counted(isolated_db, monkeypatch):
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
    set_setting("agents", {"defaults": {"corrections_checker_model": "model-a"}})

    async def malformed(*args, **kwargs):
        return kwargs["validator"]({"label": "wrong-shape", "reason": "No."})

    result = await job.run_corrections_recording(batch_size=1, invoke=malformed)
    assert result == {"status": "partial", "processed": 1, "recorded": 0, "failed": 1}
    db = _db()
    try:
        assert db.execute("SELECT COUNT(*) FROM outcome_events WHERE event_name='measurement.correction'").fetchone()[0] == 0
        assert db.execute(
            "SELECT n FROM outcome_event_counters WHERE counter='mapping_failed' AND event_name='measurement.correction'"
        ).fetchone()[0] == 1
    finally:
        db.close()
