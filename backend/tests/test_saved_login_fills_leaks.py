"""A fill approval's whole life never carries the saved password.

The sentinel login from the saved-login leak test is requested, listed,
approved, filled (as the fill executor will: ``may_fill`` then
``password_for_fill``), revoked, and every refusal is driven with the
password in its arguments. The password must not appear in any request,
grant, route response, event, log, exception or file but the values file.
"""
from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import infra.saved_login_fills as fills
import infra.saved_logins as saved_logins
from host_access.decision_capability import INTERACTIVE_UI_HEADER, USER_DECISION_HEADER
from infra.database import get_db
from test_saved_logins_leaks import _exception_text, _files_except_values, _forms, _sentinel, emitted, state  # noqa: F401

PERSON = "steve"
THREAD = "agent-thread"


def _client():
    from api.host_access import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_the_password_appears_in_no_fill_request_grant_response_event_log_or_error(
    state, tmp_path, caplog, emitted, monkeypatch,  # noqa: F811
):
    from infra.accounts import issue_token

    monkeypatch.setattr(fills, "_approvals", {})
    password, marker = _sentinel()
    db = get_db()
    db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
               (THREAD, PERSON, "Bookkeeper", "t", "t"))
    db.commit()
    db.close()
    session, _ = issue_token(PERSON, kind="session")
    texts: list[str] = []
    errors: list[BaseException] = []
    with caplog.at_level(logging.DEBUG), _client() as client:
        login_id = saved_logins.save_login(
            site="https://example.com", username="me@example.com", password=password, saved_by=PERSON,
        )
        request = fills.request_fill(THREAD, login_id)
        texts.append(repr(request))
        headers = {"Authorization": f"Bearer {session}", INTERACTIVE_UI_HEADER: "1"}
        listed = client.get(f"/api/host-access/leases?root_thread_id={THREAD}", headers=headers)
        texts.append(listed.text)
        pending = next(row for row in listed.json()["leases"] if row["kind"] == "fill")
        decision = {"Authorization": f"Bearer {session}", USER_DECISION_HEADER: pending["decision_capability"]}
        url = f"/api/host-access/leases/fills/{pending['id']}"
        body = {"revision": pending["revision"], "application_hash": pending["application_hash"]}
        texts.append(client.post(url + "/approve", json={**body, "application_hash": password,
                                                          "duration_seconds": 60}, headers=decision).text)
        approved = client.post(url + "/approve", json={**body, "duration_seconds": 60}, headers=decision)
        assert approved.status_code == 200
        texts.append(approved.text)
        assert fills.may_fill(THREAD, login_id) is True
        assert saved_logins.password_for_fill(login_id) == password  # the executor's read, after the check
        texts.append(repr(fills.listed(PERSON)))
        active = next(row for row in client.get(f"/api/host-access/leases?root_thread_id={THREAD}",
                                                 headers=headers).json()["leases"] if row["kind"] == "fill")
        revoked = client.post(url + "/revoke", headers={
            "Authorization": f"Bearer {session}", USER_DECISION_HEADER: active["decision_capability"],
        }, json={"revision": active["revision"], "application_hash": active["application_hash"]})
        assert revoked.status_code == 200
        texts.append(revoked.text)
        for attempt in (
            lambda: fills.request_fill(password, login_id),
            lambda: fills.request_fill(THREAD, password),
            lambda: fills.approve_fill(password, user_id=PERSON, revision=1, application_hash=password,
                                       duration_seconds=60),
            lambda: fills.approve_fill(request["id"], user_id=PERSON, revision=1,
                                       application_hash=request["application_hash"], duration_seconds=password),
            lambda: fills.revoke_fill(password, user_id=PERSON, revision=1, application_hash=password),
        ):
            with pytest.raises(fills.FillApprovalError) as refused:
                attempt()
            errors.append(refused.value)
        texts.append(repr(fills.may_fill(password, login_id)))
        snapshot = _files_except_values(tmp_path)

    forms = _forms(password, marker)
    for path, data in snapshot.items():
        assert not any(form in data for form in forms), path
    logged = caplog.text + "".join(repr(record.args) + repr(record.msg) for record in caplog.records)
    for text in (logged, "".join(emitted), *texts, *(_exception_text(error) for error in errors)):
        assert password not in text
        assert marker not in text
