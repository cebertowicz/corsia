"""Duration declarations and first/latest operand validation."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from infra.measure_admission import admit
from infra.measure_registry import REGISTRY, sync
from infra.measure_validation import Lines, validate
from infra.outcome_events import record_outcome_event, retract_outcome_event
from infra.schema import SCHEMA_SQL


def declaration() -> dict:
    return {
        "schema": "corsia.measures/1", "namespace": "demo",
        "events": {
            "demo.start": {"attributes": {"pr": "id", "key": "id"}, "dedupe": ["key"]},
            "demo.end": {"attributes": {"introduced_by": "id", "key": "id", "minutes": "count"},
                         "dedupe": ["key"], "since": {"event": "demo.start", "on": {"introduced_by": "pr"}, "as": "minutes"}},
        },
    }


def check(data: dict) -> None:
    validate(data, Lines(yaml.safe_dump(data)))


def test_valid_since() -> None:
    check(declaration())


def test_valid_operands() -> None:
    data = declaration()
    del data["events"]["demo.end"]["since"]
    data["kpis"] = {"k": {"formula": "ratio", "num": {"event": ["demo.start", "demo.end"], "latest": ["key"]},
                          "den": {"event": "demo.start", "first": ["pr", "key"]}}}
    check(data)


@pytest.mark.parametrize("since,field", [
    (None, "since"), ({"event": "missing", "on": "key", "as": "minutes"}, "event"),
    ({"event": "demo.start", "on": "absent", "as": "minutes"}, "on"),
    ({"event": "demo.start", "on": {"introduced_by": "absent"}, "as": "minutes"}, "on"),
    ({"event": "demo.start", "on": {}, "as": "minutes"}, "on"),
    ({"event": "demo.start", "on": "key", "as": "key"}, "as"),
    ({"event": "demo.start", "on": "key"}, "as"),
    ({"event": "demo.start", "on": "key", "as": "minutes", "extra": 1}, "since"),
])
def test_invalid_since(since, field: str) -> None:
    data = declaration()
    data["events"]["demo.end"]["since"] = since
    with pytest.raises(ValueError, match=field):
        check(data)


@pytest.mark.parametrize("operand,field", [
    ({"event": "demo.start", "latest": ["key"], "first": ["key"]}, "latest|first"),
    ({"event": "demo.start", "latest": []}, "latest"),
    ({"event": "demo.start", "first": ["key", "pr", "key"]}, "first"),
    ({"event": "demo.start", "latest": "key"}, "latest"),
    ({"event": ["demo.start", "demo.end"], "first": ["pr"]}, "first"),
    ({"event": "demo.end", "latest": ["minutes"]}, "latest"),
    ({"event": "missing", "first": ["key"]}, "first"),
])
def test_invalid_operand(operand: dict, field: str) -> None:
    data = declaration()
    del data["events"]["demo.end"]["since"]
    data["kpis"] = {"k": {"num": operand}}
    with pytest.raises(ValueError, match=field):
        check(data)


def test_invalid_coverage_operand() -> None:
    data = declaration()
    del data["events"]["demo.end"]["since"]
    data["kpis"] = {"k": {"coverage": {"of": {"event": "demo.end", "first": ["minutes"]}}}}
    with pytest.raises(ValueError, match="first"):
        check(data)


def world(tmp_path: Path, data: dict | None = None) -> sqlite3.Connection:
    REGISTRY.reset()
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA_SQL)
    now = datetime.now(timezone.utc).isoformat()
    db.execute("INSERT INTO threads (id,user_id,created_at,updated_at) VALUES (?,?,?,?)", ("owner", "user", now, now))
    path = tmp_path / ".threads" / "owner" / "measures.yaml"
    path.parent.mkdir(parents=True)
    text = yaml.safe_dump(data if data is not None else declaration())
    sync(db, owner_thread_id="owner", path=str(path), text=text)
    return db


def emit(db: sqlite3.Connection, event: str, attrs: dict, when: datetime, project: str | None = "project") -> dict:
    result = admit(db, owner_thread_id="owner", event_name=event, attributes=attrs, project_id=project, ts=when.isoformat())
    assert result.refusal is None and result.result and result.result.inserted
    row = db.execute("SELECT attributes FROM outcome_events WHERE id=?", (result.result.event_id,)).fetchone()
    return json.loads(row[0])


def test_missing_start_stays_absent(tmp_path: Path) -> None:
    db = world(tmp_path)
    now = datetime.now(timezone.utc)
    end = emit(db, "demo.end", {"key": "end", "introduced_by": "a"}, now)
    assert "minutes" not in end
    emit(db, "demo.start", {"key": "late", "pr": "a"}, now - timedelta(minutes=10))
    assert "minutes" not in json.loads(db.execute("SELECT attributes FROM outcome_events WHERE event_name='demo.end'").fetchone()[0])


def test_latest_earlier_start_and_mapping(tmp_path: Path) -> None:
    db = world(tmp_path)
    now = datetime.now(timezone.utc)
    emit(db, "demo.start", {"key": "old", "pr": "a"}, now - timedelta(minutes=30))
    emit(db, "demo.start", {"key": "new", "pr": "a"}, now - timedelta(minutes=12))
    emit(db, "demo.start", {"key": "wrong", "pr": "b"}, now - timedelta(minutes=1))
    emit(db, "demo.start", {"key": "future", "pr": "a"}, now + timedelta(minutes=1))
    assert emit(db, "demo.end", {"key": "end", "introduced_by": "a"}, now)["minutes"] == 12


def test_string_on_sees_start_in_same_transaction(tmp_path: Path) -> None:
    data = declaration()
    data["events"]["demo.end"]["since"]["on"] = "key"
    db = world(tmp_path, data)
    now = datetime.now(timezone.utc)
    emit(db, "demo.start", {"key": "same", "pr": "a"}, now - timedelta(minutes=3))
    assert emit(db, "demo.end", {"key": "same"}, now)["minutes"] == 3


def test_future_start_not_used(tmp_path: Path) -> None:
    db = world(tmp_path)
    now = datetime.now(timezone.utc)
    emit(db, "demo.start", {"key": "future", "pr": "a"}, now + timedelta(minutes=1))
    assert "minutes" not in emit(db, "demo.end", {"key": "end", "introduced_by": "a"}, now)


@pytest.mark.parametrize("start_project,end_project,expected", [
    ("other", "project", False), ("project", "project", True),
    ("project", None, False), (None, None, True), (None, "project", False),
])
def test_platform_start_respects_project_scope(tmp_path: Path, start_project: str | None, end_project: str | None, expected: bool) -> None:
    data = declaration()
    data["events"]["demo.end"]["attributes"]["repo"] = "id"
    data["events"]["demo.end"]["since"] = {"event": "github.pr.opened", "on": {"repo": "repo", "introduced_by": "pr"}, "as": "minutes"}
    db = world(tmp_path, data)
    now = datetime.now(timezone.utc)
    record_outcome_event(db, dedupe_key="github-open", event_name="github.pr.opened", source="github", provenance="observed",
                         ts=(now - timedelta(minutes=7)).isoformat(), project_id=start_project,
                         attributes={"repo": "acme/repo", "pr": "42"})
    end = emit(db, "demo.end", {"key": "end", "repo": "acme/repo", "introduced_by": "42"}, now, end_project)
    if expected:
        assert end["minutes"] == 7
    else:
        assert "minutes" not in end


def test_start_from_another_project_does_not_match(tmp_path: Path) -> None:
    db = world(tmp_path)
    now = datetime.now(timezone.utc)
    emit(db, "demo.start", {"key": "start", "pr": "a"}, now - timedelta(minutes=5), "other")
    assert "minutes" not in emit(db, "demo.end", {"key": "end", "introduced_by": "a"}, now)


def test_offset_timestamp_matches_utc_start(tmp_path: Path) -> None:
    db = world(tmp_path)
    now = datetime.now(timezone.utc)
    emit(db, "demo.start", {"key": "start", "pr": "a"}, now - timedelta(minutes=5))
    local = now.astimezone(timezone(timedelta(hours=-7)))
    assert emit(db, "demo.end", {"key": "end", "introduced_by": "a"}, local)["minutes"] == 5


def test_retracted_starts_are_not_used(tmp_path: Path) -> None:
    db = world(tmp_path)
    now = datetime.now(timezone.utc)
    emit(db, "demo.start", {"key": "old", "pr": "a"}, now - timedelta(minutes=20))
    emit(db, "demo.start", {"key": "new", "pr": "a"}, now - timedelta(minutes=5))
    assert retract_outcome_event(db, "demo.start:new")
    assert emit(db, "demo.end", {"key": "first", "introduced_by": "a"}, now)["minutes"] == 20
    assert retract_outcome_event(db, "demo.start:old")
    assert "minutes" not in emit(db, "demo.end", {"key": "second", "introduced_by": "a"}, now)


def test_supplied_duration_preserved(tmp_path: Path) -> None:
    db = world(tmp_path)
    now = datetime.now(timezone.utc)
    emit(db, "demo.start", {"key": "start", "pr": "a"}, now - timedelta(minutes=5))
    assert emit(db, "demo.end", {"key": "end", "introduced_by": "a", "minutes": 42}, now)["minutes"] == 42
