"""google_workspace runs: event updates that can remove occurrences are saved first, a failed
preview shows gws's error, and output is capped. No Google call is made."""

from __future__ import annotations

import json

import pytest

import tools.gws_check as gws_check
from infra.constants import READ_FILE_OUTPUT_CHAR_CAP
from tools import gws_messages
from tools.gws_run import Completed

from test_google_workspace import FakeGws, recorded, tool, use, workspace  # noqa: F401

SINGLE = {"id": "EV1", "summary": "Lunch"}
SERIES = {"id": "S1", "iCalUID": "S1@google.com", "recurrence": ["RRULE:FREQ=WEEKLY"]}


def _preview(name: str) -> dict:
    return json.loads(recorded(name))


@pytest.mark.parametrize("name", ["calendar_patch", "calendar_patch_cancel", "calendar_patch_recurrence"])
@pytest.mark.parametrize("method", ["PATCH", "PUT"])
def test_every_event_update_is_checked_against_the_event(name, method):
    decision = gws_check.decide({**_preview(name), "method": method})
    assert decision.kind == gws_check.UPDATE_EVENT and decision.calendar_id == "primary"


@pytest.mark.parametrize("method,current,body", [
    ("PATCH", SINGLE, {"status": "cancelled"}),
    ("PUT", SINGLE, {"summary": "x", "status": "cancelled"}),
    ("PATCH", SERIES, {"recurrence": ["RRULE:FREQ=WEEKLY;COUNT=2"]}),
    ("PATCH", SERIES, {"start": {"dateTime": "2026-11-10T09:00:00Z"}}),
    ("PATCH", SERIES, {"summary": "x", "anyFieldGoogleAddsLater": 1}),
    ("PUT", SERIES, {"summary": "x"}),
    ("PUT", SERIES, {**SERIES, "summary": "x"}),
    ("PATCH", SINGLE, None),
])
def test_an_update_that_may_remove_occurrences_is_saved_first(method, current, body):
    assert gws_check.update_removes_events(method, current, body)


@pytest.mark.parametrize("method,current,body", [
    ("PATCH", SINGLE, {"summary": "x", "start": {"dateTime": "2026-11-10T09:00:00Z"}}),
    ("PUT", SINGLE, {"summary": "x", "status": "confirmed"}),
    ("PATCH", SINGLE, {"recurrence": ["RRULE:FREQ=DAILY"]}),
    ("PATCH", SERIES, {"summary": "x", "attendees": [], "reminders": {"useDefault": True}}),
])
def test_an_update_that_keeps_every_occurrence_logs_nothing(method, current, body):
    assert not gws_check.update_removes_events(method, current, body)


async def test_an_ordinary_edit_reads_the_event_and_runs_with_nothing_logged(tool, workspace, monkeypatch):
    def real(words):
        return json.dumps(SINGLE) if words[:3] == ["calendar", "events", "get"] else '{"id": "EV1"}'

    fake = use(monkeypatch, FakeGws(recorded("calendar_patch"), real))
    result = await tool.execute(args=["calendar", "events", "patch"], body={"summary": "x"})
    assert result == '{"id": "EV1"}'
    assert [words[:3] for words in fake.real_runs()] == [["calendar", "events", "get"], ["calendar", "events", "patch"]]
    assert not (workspace / "google-deletions").exists()


async def test_an_event_that_cant_be_read_is_not_updated(tool, monkeypatch):
    fake = use(monkeypatch, FakeGws(recorded("calendar_patch_cancel"), lambda words: Completed(1, "", "notFound")))
    result = await tool.execute(args=["calendar", "events", "patch"], body=_preview("calendar_patch_cancel")["body"])
    assert result == "Error: notFound"
    assert [words[:3] for words in fake.real_runs()] == [["calendar", "events", "get"]]


async def test_a_cancelling_patch_and_a_truncating_patch_write_the_log_before_they_run(tool, workspace, monkeypatch):
    series = {"id": "S1", "iCalUID": "S1@google.com", "recurrence": ["RRULE:FREQ=WEEKLY"]}
    moved = {"id": "S1_20261110T170000Z", "iCalUID": "S1@google.com", "summary": "moved"}
    folder = workspace / "google-deletions" / "person@example.com"
    for name, event, expected in (
        ("calendar_patch_cancel", {"id": "EV1", "summary": "Lunch"}, [{"id": "EV1", "summary": "Lunch"}]),
        ("calendar_patch_recurrence", series, [series, moved]),
    ):
        seen = {}

        def real(words, event=event, seen=seen):
            if words[:3] == ["calendar", "events", "get"]:
                return json.dumps(event)
            if words[:3] == ["calendar", "events", "list"]:
                return json.dumps({"items": [event, moved]})
            seen["logged_before_patch"] = sorted(folder.glob("*")) if folder.exists() else []
            return "{}"

        use(monkeypatch, FakeGws(recorded(name), real))
        result = await tool.execute(args=["calendar", "events", "patch"], body=_preview(name)["body"])
        [logged] = seen["logged_before_patch"]
        assert json.loads(logged.read_text(encoding="utf-8")) == expected
        assert result.startswith("Done.")
        logged.unlink()


async def test_a_failed_preview_returns_gws_own_error(tool, monkeypatch):
    error = Completed(3, "", "error: unknown method 'lst' for drive.files")
    fake = use(monkeypatch, FakeGws(error))
    result = await tool.execute(args=["drive", "files", "lst"])
    assert result.startswith("Error: ") and "unknown method 'lst'" in result
    assert fake.real_runs() == []


async def test_a_preview_that_succeeds_without_a_request_is_still_refused(tool, monkeypatch):
    use(monkeypatch, FakeGws("{}"))
    assert await tool.execute(args=["calendar", "+agenda"]) == gws_messages.NO_PREVIEW


async def test_large_output_is_cut_with_how_to_narrow_it(tool, monkeypatch):
    big = "x" * (READ_FILE_OUTPUT_CHAR_CAP + 500)
    use(monkeypatch, FakeGws(recorded("drive_list"), {"drive files list": big}))
    result = await tool.execute(args=["drive", "files", "list"])
    assert result.startswith("x" * READ_FILE_OUTPUT_CHAR_CAP)
    assert result.endswith(gws_messages.CUT.format(cap=READ_FILE_OUTPUT_CHAR_CAP))
    assert "`fields`" in result and "`pageSize`" in result and "`output`" in result
    assert len(result) < len(big) + 400


async def test_output_under_the_cap_is_returned_whole(tool, monkeypatch):
    use(monkeypatch, FakeGws(recorded("drive_list"), {"drive files list": '{"files": []}'}))
    assert await tool.execute(args=["drive", "files", "list"]) == '{"files": []}'

