"""Browser closure is a prerequisite for exposing a host-access decision."""

import asyncio

from fastapi import FastAPI
import httpx
import pytest

from infra.accounts import issue_token
from test_host_access_supervisor import _configure_review_fakes, _seed_session


@pytest.mark.asyncio
@pytest.mark.parametrize("closure_fails", [False, True])
async def test_interactive_approval_cannot_race_browser_isolation(
    isolated_db, monkeypatch, closure_fails,
):
    from api.host_access import router
    from host_access import processes, supervisor

    _seed_session(isolated_db, "root")
    owner_session, _ = issue_token("steve", kind="session")
    _configure_review_fakes(monkeypatch, supervisor)
    entered, release = asyncio.Event(), asyncio.Event()

    class Owner:
        async def close_browser(self, _thread_id):
            entered.set()
            await release.wait()
            if closure_fails:
                raise RuntimeError("browser unavailable")

    monkeypatch.setattr(processes, "PROCESS_OWNER", Owner())
    monkeypatch.setattr(supervisor, "get_host_access_auto_approve", lambda: False)
    app = FastAPI()
    app.include_router(router)
    pending = asyncio.create_task(supervisor.request_reviewed_lease(
        "root", purpose="Inspect host state", plan=["Read process state"],
        duration_seconds=60,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": f"Bearer {owner_session}",
                     "X-CORSIA-Interactive-UI": "1"},
        ) as client:
            listed = await client.get("/api/host-access/leases")
            assert listed.status_code == 200
            lease = listed.json()["leases"][0]
            assert lease["state"] == "reviewing"
            assert "decision_capability" not in lease
            fetched = await client.get(f"/api/host-access/leases/{lease['id']}")
            assert fetched.status_code == 200
            assert "decision_capability" not in fetched.json()
            approval = await client.post(
                f"/api/host-access/leases/{lease['id']}/approve",
                json={"revision": lease["revision"],
                      "application_hash": lease["application_hash"]},
            )
            assert approval.status_code == 403
            release.set()
            result = await asyncio.wait_for(pending, 2)
            assert result["state"] == ("blocked" if closure_fails else "awaiting_user")
            assert result["application_review_result"]["verdict"] == "allow"
            final = await client.get(f"/api/host-access/leases/{lease['id']}")
            assert ("decision_capability" in final.json()) is not closure_fails
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
