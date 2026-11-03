"""google_workspace: removals through updates are saved first, a failed preview shows gws's error,
output is capped, and the Mac gws pin is stored once. No Google call is made."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import tools.gws_check as gws_check
from infra.constants import READ_FILE_OUTPUT_CHAR_CAP
from tools import gws_messages
from tools.gws_run import Completed
from tools.managed_runtime import RUNTIME_PINS

from test_google_workspace import FakeGws, recorded, tool, use, workspace  # noqa: F401

REPO = Path(__file__).resolve().parents[2]


def _preview(name: str) -> dict:
    return json.loads(recorded(name))


@pytest.mark.parametrize("name,event", [("calendar_patch_cancel", "EV1"), ("calendar_patch_recurrence", "S1")])
def test_removing_events_through_an_update_is_saved_first(name, event):
    decision = gws_check.decide(_preview(name))
    assert decision == gws_check.Decision(gws_check.DELETE_EVENT, calendar_id="primary", event_id=event)


def test_creates_and_ordinary_edits_still_log_nothing():
    for name in ("calendar_patch", "calendar_insert"):
        assert gws_check.decide(_preview(name)) == gws_check.Decision()
    put = {**_preview("calendar_patch"), "method": "PUT", "body": {"summary": "x", "status": "confirmed"}}
    assert gws_check.decide(put) == gws_check.Decision()


def test_a_cancelling_put_is_saved_first():
    put = {**_preview("calendar_patch_cancel"), "method": "PUT"}
    assert gws_check.decide(put).kind == gws_check.DELETE_EVENT


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


def test_the_mac_build_script_ships_the_pinned_gws():
    script = (REPO / "desktop" / "scripts" / "build-macos-bundle.sh").read_text()
    pin = RUNTIME_PINS["gws"]["arm64"]

    def value(name: str) -> str:
        found = re.search(rf'^readonly {name}="([^"]+)"$', script, re.MULTILINE)
        assert found, name
        return found.group(1)

    version, archive = value("GWS_VERSION"), value("GWS_ARCHIVE")
    assert version == pin.version
    assert value("GWS_URL").replace("${GWS_VERSION}", version).replace("${GWS_ARCHIVE}", archive) == pin.url
    assert value("GWS_SHA256") == pin.sha256
