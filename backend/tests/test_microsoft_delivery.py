"""Microsoft gated delivery lifecycle tests with Graph mocked at the HTTP boundary."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from jsonschema import ValidationError, validate

from connections.connected_accounts import store_credentials
from connections.microsoft import calendar, client
from connections.microsoft.errors import MicrosoftError, PROVIDER_UNAVAILABLE, SERVICE_UNAVAILABLE
from infra.database import get_db
from delegation_fixtures import deleg_db  # noqa: F401
from tests.test_action_gate import THREAD, _admit, _bind, workspace  # noqa: F401
from tests.test_microsoft_refresh import Response, connect
from tools import action_delivery, microsoft_actions, microsoft_calendar_invites
from tools.action_gate import Approved
from tools.action_drafts import create_draft
from tools.microsoft_actions import (
    MAIL_STILL_SENDING,
    MAIL_UNCONFIRMED,
    MicrosoftCalendarListTool,
    MicrosoftMailDraftTool,
    MicrosoftMailSendTool,
)
from tools.microsoft_calendar_invites import (
    CALENDAR_STILL_SENDING,
    MicrosoftCalendarCreateTool,
    MicrosoftCalendarDraftTool,
    MicrosoftCalendarUpdateTool,
)


@dataclass
class GraphHttp:
    responses: list[object]
    calls: list[dict[str, Any]]

    async def __aenter__(self) -> GraphHttp:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs: Any) -> Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, Response)
        return response


def _row(draft_id: str) -> dict[str, Any]:
    return dict(get_db().execute("SELECT * FROM action_drafts WHERE id=?", (draft_id,)).fetchone())


async def _no_designated(_kwargs: dict[str, Any]) -> None:
    return None


async def _token(_account: str) -> str:
    return "access"


@pytest.fixture
def microsoft(deleg_db, workspace, monkeypatch):  # noqa: F811
    from tools import sandbox_config

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
    account = connect()
    monkeypatch.setattr(microsoft_actions, "_designated", _no_designated)
    monkeypatch.setattr(client, "access_token", _token)
    return account


def _graph(monkeypatch: pytest.MonkeyPatch, responses: list[object]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    http = GraphHttp(responses, calls)
    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_kwargs: http)
    return calls


async def _mail_draft(account: str) -> dict[str, Any]:
    result = await MicrosoftMailDraftTool().execute(
        _thread_id=THREAD, account_id=account, to=["to@example.test"], cc=["cc@example.test"],
        subject="Subject", body="Body",
    )
    assert not result.startswith("Error:"), result
    return json.loads(result)


async def _calendar_draft(
    account: str, *, operation: str, event_id: str | None = None, draft_id: str | None = None,
    title: str = "Planning", start: str = "2026-10-20T09:00:00-07:00",
    end: str = "2026-10-20T10:00:00-07:00", attendees: list[str] | None = None,
    description: str = "Agenda",
) -> dict[str, Any]:
    result = await MicrosoftCalendarDraftTool().execute(
        _thread_id=THREAD, account_id=account, operation=operation, event_id=event_id,
        draft_id=draft_id, title=title, start=start, end=end,
        attendees=attendees or ["reviewed@example.test"], description=description,
    )
    assert not result.startswith("Error:"), result
    return json.loads(result)


async def test_calendar_draft_revision_accepts_its_returned_draft_id(microsoft, workspace, monkeypatch) -> None:
    from infra import config

    monkeypatch.setattr(config, "WORKSPACE_SELECTION", config._resolve_workspace_selection(workspace))
    tool = MicrosoftCalendarDraftTool()
    first_args = {
        "account_id": microsoft,
        "operation": "create",
        "title": "Planning",
        "start": "2026-10-20T09:00:00-07:00",
        "end": "2026-10-20T10:00:00-07:00",
        "attendees": ["reviewed@example.test"],
        "description": "Agenda",
    }
    validate(first_args, tool.parameters)
    first = json.loads(await tool.execute(_thread_id=THREAD, **first_args))

    revised_args = {
        **first_args,
        "draft_id": first["draft_id"],
        "title": "Revised planning",
        "start": "2026-10-20T10:00:00-07:00",
        "end": "2026-10-20T11:00:00-07:00",
    }
    validate(revised_args, tool.parameters)
    revised = json.loads(await tool.execute(_thread_id=THREAD, **revised_args))

    assert revised["draft_id"] == first["draft_id"]
    assert revised["revision"] != first["revision"]


def test_calendar_draft_schema_refuses_raw_event_and_extra_attendee(microsoft) -> None:
    with pytest.raises(ValidationError):
        validate({
            "account_id": microsoft, "operation": "create", "event": {
                "subject": "Planning", "attendees": [{"emailAddress": {"address": "hidden@example.test"}}],
            },
        }, MicrosoftCalendarDraftTool().parameters)


async def test_calendar_draft_refuses_changed_readback(microsoft, monkeypatch) -> None:
    from tools import reviewed_drafts

    monkeypatch.setattr(reviewed_drafts, "_read_bytes", lambda _relative: b"changed")
    output = await MicrosoftCalendarDraftTool().execute(
        _thread_id=THREAD, account_id=microsoft, operation="create", title="Planning",
        start="2026-10-20T09:00:00-07:00", end="2026-10-20T10:00:00-07:00",
        attendees=["reviewed@example.test"], description="Agenda",
    )
    assert output == "Error: The draft changed while it was being saved, so it is not recorded. Save it again."


@pytest.mark.parametrize("attendee", ["Name <person@example.test>", "one@example.test, two@example.test"])
async def test_calendar_draft_refuses_non_plain_attendees(microsoft, attendee) -> None:
    output = await MicrosoftCalendarDraftTool().execute(
        _thread_id=THREAD, account_id=microsoft, operation="create", title="Planning",
        start="2026-10-20T09:00:00-07:00", end="2026-10-20T10:00:00-07:00",
        attendees=[attendee], description="Agenda",
    )
    assert output == "Error: Attendees must be email addresses."


async def test_calendar_send_builds_graph_bodies_only_from_recorded_fields(microsoft, monkeypatch) -> None:
    _approve(monkeypatch)
    calls = _graph(monkeypatch, [Response(201, {"id": "created"}), Response(200, {"id": "updated"})])
    create = await _calendar_draft(
        microsoft, operation="create", title="Planning hidden@example.test",
        description="Agenda hidden@example.test", attendees=["reviewed@example.test"],
    )
    assert json.loads(await MicrosoftCalendarCreateTool().execute(
        _thread_id=THREAD, account_id=microsoft, draft_id=create["draft_id"], revision=create["revision"],
    ))["id"] == "created"
    body = {
        "subject": "Planning hidden@example.test",
        "start": {"dateTime": "2026-10-20T16:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-10-20T17:00:00", "timeZone": "UTC"},
        "attendees": [{"emailAddress": {"address": "reviewed@example.test"}, "type": "required"}],
        "body": {"contentType": "text", "content": "Agenda hidden@example.test"},
    }
    assert calls[0]["method"] == "POST" and calls[0]["json"] == body

    update = await _calendar_draft(microsoft, operation="update", event_id="event/with slash")
    assert json.loads(await MicrosoftCalendarUpdateTool().execute(
        _thread_id=THREAD, account_id=microsoft, draft_id=update["draft_id"], revision=update["revision"],
    ))["id"] == "updated"
    assert calls[1]["method"] == "PATCH" and calls[1]["url"].endswith("/event%2Fwith%20slash")
    assert calls[1]["json"] == {
        **body,
        "subject": "Planning",
        "body": {"contentType": "text", "content": "Agenda"},
    }


async def test_calendar_send_refuses_a_changed_account(microsoft, monkeypatch) -> None:
    _approve(monkeypatch)
    saved = await _calendar_draft(microsoft, operation="create")
    other = connect("11111111-1111-4111-8111-111111111111")
    output = await MicrosoftCalendarCreateTool().execute(
        _thread_id=THREAD, account_id=other, draft_id=saved["draft_id"], revision=saved["revision"],
    )
    assert output.startswith("Error: account_id does not match this draft.")

    db = get_db()
    db.execute("UPDATE connected_accounts SET address='moved@example.test' WHERE id=?", (microsoft,))
    db.commit()
    assert await MicrosoftCalendarCreateTool().execute(
        _thread_id=THREAD, account_id=microsoft, draft_id=saved["draft_id"], revision=saved["revision"],
    ) == "Error: Not sent: the From line does not match the Microsoft account this draft was written from."


async def test_calendar_send_refuses_a_legacy_raw_event_draft(microsoft, workspace, monkeypatch) -> None:
    _approve(monkeypatch)
    calls = _graph(monkeypatch, [Response(201, {"id": "created"})])
    data = b'{"operation":"create","event_id":null,"event":{"subject":"Planning","attendees":[{"emailAddress":{"address":"hidden@example.test"}}]}}'
    (workspace / "legacy-event.json").write_bytes(data)
    legacy = create_draft(THREAD, "invite", "legacy-event.json", data, {"account_id": microsoft, "operation": "create"})
    get_db().commit()
    assert await MicrosoftCalendarCreateTool().execute(
        _thread_id=THREAD, account_id=microsoft, draft_id=legacy["draft_id"], revision=legacy["revision"],
    ) == ("Error: This saved calendar draft is invalid or incomplete; nothing changed. Save a new draft "
          "with microsoft_calendar_draft, show the person, and wait for their reply.")
    assert calls == []


def _approve(monkeypatch: pytest.MonkeyPatch) -> None:
    async def approved(_kind: str, draft_id: str, _revision: str) -> Approved:
        row = _row(draft_id)
        from infra import config
        return Approved(row, (config.WORKSPACE / row["path"]).read_bytes())

    monkeypatch.setattr(microsoft_actions, "check_action", approved)
    monkeypatch.setattr(microsoft_calendar_invites, "check_action", approved)


async def test_mail_send_carries_cc_and_refuses_mismatched_from_before_graph(microsoft, monkeypatch):
    _approve(monkeypatch)
    calls = _graph(monkeypatch, [Response(202, {})])
    saved = await _mail_draft(microsoft)

    sent = json.loads(await MicrosoftMailSendTool().execute(
        _thread_id=THREAD, account_id=microsoft, draft_id=saved["draft_id"], revision=saved["revision"],
    ))
    assert sent == {"sent": True, "account_id": microsoft}
    assert calls[0]["json"]["message"]["ccRecipients"] == [{"emailAddress": {"address": "cc@example.test"}}]

    saved = await _mail_draft(microsoft)
    from infra import config
    path = config.WORKSPACE / saved["path"]
    path.write_text(path.read_text().replace("From: mail@example.test", "From: other@example.test"))
    assert await MicrosoftMailSendTool().execute(
        _thread_id=THREAD, account_id=microsoft, draft_id=saved["draft_id"], revision=saved["revision"],
    ) == "Error: Not sent: the From line does not match the Microsoft account this draft was written from."
    assert len(calls) == 1


async def test_mail_timeout_stays_claimed_and_second_send_refuses(microsoft, monkeypatch):
    _approve(monkeypatch)
    calls = _graph(monkeypatch, [httpx.ReadTimeout("late response")])
    saved = await _mail_draft(microsoft)
    tool = MicrosoftMailSendTool()
    first = await tool.execute(_thread_id=THREAD, account_id=microsoft, draft_id=saved["draft_id"], revision=saved["revision"])
    assert first == f"Error: {MAIL_UNCONFIRMED}"
    assert _row(saved["draft_id"])["state"] == "sending"
    second = await tool.execute(_thread_id=THREAD, account_id=microsoft, draft_id=saved["draft_id"], revision=saved["revision"])
    assert second == f"Error: {MAIL_STILL_SENDING}"
    assert len(calls) == 1


async def test_mail_graph_400_releases_claim_with_named_refusal(microsoft, monkeypatch):
    _approve(monkeypatch)
    _graph(monkeypatch, [Response(400, {})])
    saved = await _mail_draft(microsoft)
    result = await MicrosoftMailSendTool().execute(
        _thread_id=THREAD, account_id=microsoft, draft_id=saved["draft_id"], revision=saved["revision"],
    )
    assert result == "Error: Not sent: Microsoft refused this request. Check the ids, names or fields you passed and try again."
    assert _row(saved["draft_id"])["state"] == "open"


async def test_mail_delivery_succeeds_when_mark_sent_fails(microsoft, monkeypatch):
    _approve(monkeypatch)
    _graph(monkeypatch, [Response(202, {})])
    saved = await _mail_draft(microsoft)

    def fail_mark_sent(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(action_delivery, "mark_sent", fail_mark_sent)
    assert json.loads(await MicrosoftMailSendTool().execute(
        _thread_id=THREAD, account_id=microsoft, draft_id=saved["draft_id"], revision=saved["revision"],
    )) == {"sent": True, "account_id": microsoft}
    assert _row(saved["draft_id"])["state"] == "sending"


async def test_calendar_create_claims_before_graph_and_update_refuses_blank_event_id(microsoft, workspace, monkeypatch):
    _approve(monkeypatch)
    calls = _graph(monkeypatch, [httpx.ReadTimeout("late response")])
    saved = await _calendar_draft(microsoft, operation="create")
    create = MicrosoftCalendarCreateTool()
    first = await create.execute(_thread_id=THREAD, account_id=microsoft, draft_id=saved["draft_id"], revision=saved["revision"])
    assert first.startswith("Error:") and _row(saved["draft_id"])["state"] == "sending"
    second = await create.execute(_thread_id=THREAD, account_id=microsoft, draft_id=saved["draft_id"], revision=saved["revision"])
    assert second == f"Error: {CALENDAR_STILL_SENDING}"
    assert len(calls) == 1

    data = b'{"operation":"update","event_id":" ","event":{"subject":"Planning"}}'
    (workspace / "blank-event.json").write_bytes(data)
    update = create_draft(THREAD, "invite", "blank-event.json", data, {"account_id": microsoft})
    get_db().commit()
    assert await MicrosoftCalendarUpdateTool().execute(
        _thread_id=THREAD, account_id=microsoft, draft_id=update["draft_id"], revision=update["revision"],
    ) == ("Error: This saved calendar draft is invalid or incomplete; nothing changed. Save a new draft "
          "with microsoft_calendar_draft, show the person, and wait for their reply.")
    assert len(calls) == 1


async def test_calendar_list_uses_bounded_calendar_view_and_refuses_injection(microsoft, monkeypatch):
    calls = _graph(monkeypatch, [Response(200, {"value": []})])
    result = json.loads(await MicrosoftCalendarListTool().execute(
        _thread_id=THREAD, account_id=microsoft, start="2026-10-20T00:00:00-07:00", end="2026-10-21T00:00:00-07:00",
    ))
    assert result == {"events": []}
    assert calls[0]["url"] == calendar.CALENDAR_VIEW
    assert calls[0]["params"]["startDateTime"] == "2026-10-20T00:00:00-07:00"
    assert calls[0]["params"]["endDateTime"] == "2026-10-21T00:00:00-07:00"
    assert "$filter" not in calls[0]["params"]

    refused = await MicrosoftCalendarListTool().execute(
        _thread_id=THREAD, account_id=microsoft, start="x' or 1 eq 1", end="2026-10-21T00:00:00-07:00",
    )
    assert refused == "Error: start and end must be ISO-8601 datetimes."
    assert len(calls) == 1


async def test_refresh_lost_race_uses_winner_token_without_disabling_account(deleg_db, monkeypatch):
    microsoft = connect()
    calls: list[tuple[str, dict[str, Any]]] = []

    class TokenHttp:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response(200, {"access_token": "candidate", "expires_in": 3600})

    monkeypatch.setattr(client.httpx, "AsyncClient", lambda **_kwargs: TokenHttp())

    def lose_race(account_id: str, _held: str, _updated: dict[str, Any], *, db) -> bool:
        current = client._usable_credentials(account_id, db=db)
        assert current is not None
        store_credentials(account_id, {**current, "access_token": "winner", "access_token_expires_at": str(time.time() + 3600)}, db=db)
        return False

    monkeypatch.setattr(client, "_store_refreshed", lose_race)
    import connections.account_state as account_state
    disabled: list[str] = []
    monkeypatch.setattr(account_state, "mark_needs_sign_in", lambda account_id, *args: disabled.append(account_id))
    assert await client.access_token(microsoft) == "winner"
    assert len(calls) == 1 and disabled == []


@pytest.mark.parametrize(
    ("raised", "logged"),
    [(RuntimeError("db fault"), True), (MicrosoftError(PROVIDER_UNAVAILABLE), False), (MicrosoftError(SERVICE_UNAVAILABLE), False)],
)
async def test_delivery_logs_only_unexpected_credential_failures(monkeypatch, caplog, raised, logged):
    async def db_call(*_args: object) -> None: return None
    async def acquire() -> str: raise raised
    async def send(_token: str) -> str: raise AssertionError("must not send without credentials")
    monkeypatch.setattr(action_delivery, "run_in_db", db_call)
    out = await action_delivery.deliver_claimed(
        "draft", acquire=acquire, provider_errors=(MicrosoftError,), send=send, result_id=lambda r: r,
        success=lambda _r, _i: "sent", sign_in_error=lambda _e: "sign in", request_error=str,
        never_sent=lambda _e: False, still_sending="busy", claim_error="claim", unconfirmed="unknown", label="test",
    )
    assert out == "Error: Not sent: sign in"
    assert ("could not acquire delivery credentials" in caplog.text) is logged
