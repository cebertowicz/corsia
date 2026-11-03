"""The check before every google_workspace call, on dry-run output recorded from gws 0.22.5."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import tools.gws_check as gws_check
from tools import gws_messages
from tools.catalog import TOOL_CATALOG, pack_members
from tools.google_workspace import GoogleWorkspaceTool
from tools.managed_runtime import RUNTIME_PINS
from tools.registry import ToolRegistry
from tools.tool_surface import pack_index

FIXTURES = Path(__file__).parent / "fixtures" / "gws_previews"


def preview(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", [
    "drive_list", "drive_trash_update", "drive_share", "calendar_insert", "docs_create",
    "docs_insert", "docs_insert_target_revision", "docs_delete_guarded", "sheets_update", "sheets_append",
])
def test_allowed_calls_run_unchanged(name):
    assert gws_check.decide(preview(name)) == gws_check.Decision()


@pytest.mark.parametrize("name", ["gmail_send_helper", "gmail_messages_send", "gmail_messages_list"])
def test_anything_to_gmails_address_is_refused(name):
    assert gws_check.decide(preview(name)).refusal == gws_messages.MAIL


def test_gmail_under_the_shared_google_host_is_refused_too():
    request = {"method": "GET", "url": "https://www.googleapis.com/gmail/v1/users/me/messages", "dry_run": True}
    assert gws_check.decide(request).refusal == gws_messages.MAIL
    upload = {"method": "POST", "url": "https://www.googleapis.com/upload/gmail/v1/users/me/messages/send"}
    assert gws_check.decide(upload).refusal == gws_messages.MAIL


def test_emptying_the_drive_trash_is_refused():
    assert gws_check.decide(preview("drive_empty_trash")).refusal == gws_messages.TRASH


@pytest.mark.parametrize("name", [
    "docs_delete_unguarded", "docs_replace_unguarded", "docs_deletetab_unguarded", "docs_replace_named_unguarded",
])
def test_docs_delete_or_replace_needs_the_revision_the_agent_read(name):
    assert gws_check.decide(preview(name)).refusal == gws_messages.DOCS_GUARD


@pytest.mark.parametrize("request_type", [
    "deleteTableRow", "deleteTableColumn", "deletePositionedObject", "deleteHeader", "deleteFooter",
    "deleteFootnote", "replaceImage", "deleteSomethingGoogleAddsLater",
])
def test_every_docs_delete_or_replace_request_needs_the_revision(request_type):
    request = preview("docs_delete_unguarded")
    request["body"]["requests"] = [{"insertText": {"text": "x"}}, {request_type: {}}]
    assert gws_check.decide(request).refusal == gws_messages.DOCS_GUARD


def test_a_docs_write_with_an_empty_revision_is_still_unguarded():
    request = preview("docs_delete_guarded")
    request["body"]["writeControl"]["requiredRevisionId"] = ""
    assert gws_check.decide(request).refusal == gws_messages.DOCS_GUARD


def test_a_drive_delete_becomes_a_trash_of_that_file():
    decision = gws_check.decide(preview("drive_delete"))
    assert (decision.kind, decision.file_id, decision.refusal) == (gws_check.TRASH_FILE, "F1", None)


def test_event_delete_and_calendar_clear_or_delete_name_their_calendar():
    deleted = gws_check.decide(preview("calendar_event_delete"))
    assert (deleted.kind, deleted.calendar_id, deleted.event_id) == (gws_check.DELETE_EVENT, "primary", "EV1")
    cleared = gws_check.decide(preview("calendar_clear"))
    assert (cleared.kind, cleared.calendar_id) == (gws_check.SAVE_CALENDAR, "primary")
    removed = gws_check.decide(preview("calendar_delete"))
    assert (removed.kind, removed.calendar_id) == (gws_check.SAVE_CALENDAR, "abc@group.calendar.google.com")


@pytest.mark.parametrize("url", [
    "https://www.googleapis.com/drive/v3/files/F1/revisions/R1",
    "https://www.googleapis.com/drive/v3/files/F1/comments/C1",
    "https://www.googleapis.com/drive/v3/files/F1/comments/C1/replies/R1",
    "https://www.googleapis.com/drive/v3/drives/D1",
    "https://example.googleapis.com/v1/anything/new",
])
def test_a_delete_that_cannot_be_put_back_is_refused(url):
    assert gws_check.decide({"method": "DELETE", "url": url}).refusal == gws_messages.CANT_UNDO


@pytest.mark.parametrize("url", [
    "https://www.googleapis.com/drive/v3/files/F1/permissions/P1",
    "https://www.googleapis.com/calendar/v3/calendars/primary/acl/user%3Aa%40b.com",
    "https://www.googleapis.com/calendar/v3/users/me/calendarList/abc%40group.calendar.google.com",
])
def test_removing_a_share_or_a_listed_calendar_runs_unchanged(url):
    assert gws_check.decide({"method": "DELETE", "url": url}) == gws_check.Decision()


def test_the_check_reads_the_path_on_any_google_host():
    trashed = gws_check.decide({"method": "DELETE", "url": "https://drive.googleapis.com/drive/v3/files/F1"})
    assert (trashed.kind, trashed.file_id) == (gws_check.TRASH_FILE, "F1")
    cleared = gws_check.decide({"method": "POST", "url": "https://calendar.googleapis.com/calendar/v3/calendars/primary/clear"})
    assert (cleared.kind, cleared.calendar_id) == (gws_check.SAVE_CALENDAR, "primary")
    updated = gws_check.decide({**preview("calendar_patch"), "url": "https://calendar.googleapis.com/calendar/v3/calendars/primary/events/EV1"})
    assert (updated.kind, updated.event_id) == (gws_check.UPDATE_EVENT, "EV1")
    docs = {**preview("docs_delete_unguarded"), "url": preview("docs_delete_unguarded")["url"].replace("docs.", "www.")}
    assert gws_check.decide(docs).refusal == gws_messages.DOCS_GUARD


@pytest.mark.parametrize("url", [
    "https://example.com/drive/v3/files",
    "https://googleapis.com.example.com/drive/v3/files",
    "https://evilgoogleapis.com/drive/v3/files",
    "http://www.googleapis.com/drive/v3/files",
    "https://127.0.0.1/drive/v3/files",
    "drive/v3/files",
])
def test_a_request_to_any_address_but_googles_is_refused(url):
    assert gws_check.decide({"method": "GET", "url": url}).refusal == gws_messages.NO_PREVIEW


def test_the_address_is_unquoted_before_it_is_used():
    request = {"method": "DELETE", "url": "https://www.googleapis.com/calendar/v3/calendars/a%40b.com/events/e%5F1"}
    decision = gws_check.decide(request)
    assert (decision.calendar_id, decision.event_id) == ("a@b.com", "e_1")


@pytest.mark.parametrize("text", [
    "", "not json", "[]", '{"error": {"code": 401}}', '{"dry_run": false, "method": "GET", "url": "u"}',
    '{"dry_run": true, "method": "GET"}', '{"dry_run": true}{"dry_run": true}',
])
def test_output_that_is_not_exactly_one_request_has_no_preview(text):
    assert gws_check.parse_preview(text) is None


def test_a_recorded_preview_parses():
    text = (FIXTURES / "drive_list.json").read_text(encoding="utf-8")
    assert gws_check.parse_preview(text)["method"] == "GET"


def test_the_tool_text_is_the_approved_text_and_the_tool_is_counted_not_named(tmp_path):
    tool = GoogleWorkspaceTool(tmp_path)
    assert tool.name == "google_workspace" and tool.description == gws_messages.DESCRIPTION
    assert len(gws_messages.DESCRIPTION.split()) == 225
    assert set(tool.parameters["properties"]) == {"account", "args", "params", "body", "upload", "output"}
    assert TOOL_CATALOG["google_workspace"].account_bound
    assert pack_members()["google_workspace"] == ("google_workspace",)
    index = pack_index(ToolRegistry())
    assert "- google_workspace:" not in index
    assert index.count('- 1 more for a connected account: tools_load(pack="google_workspace")') == 1


def test_gws_is_pinned_with_a_checksum_for_each_supported_machine():
    pins = RUNTIME_PINS["gws"]
    assert set(pins) == {"arm64", "x86_64", "linux-arm64", "linux-x86_64"}
    for pin in pins.values():
        assert pin.version == "0.22.5" and "/v0.22.5/" in pin.url and pin.executable == "gws"
        assert re.fullmatch(r"[0-9a-f]{64}", pin.sha256)
