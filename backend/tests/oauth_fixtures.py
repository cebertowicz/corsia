"""Shared fixtures for the hosted-app sign-in tests (import them into a test module)."""

from __future__ import annotations

import logging

import httpx
import pytest
from loguru import logger

from auth_fixtures import OWNER_USER, seed_account
from connections.app_lifecycle import create_app
from infra.accounts import token_digest
from infra.database import get_db
from oauth_fake_as import MCP_URL, FakeAS
import tools.mcp_oauth_auth as mcp_oauth_auth
import tools.mcp_oauth_flow as mcp_oauth_flow
import tools.mcp_oauth_protocol as mcp_oauth_protocol
import tools.mcp_oauth_secrets as mcp_oauth_secrets

SESSION = "signin-test-session-credential"
ALL_REACH = {"mode": "all", "project_ids": [], "outside_projects": False}


class Env:
    def __init__(self, fake: FakeAS, app_id: str, connects: list[str], lost: list[str]) -> None:
        self.fake, self.app_id, self.connects, self.lost = fake, app_id, connects, lost
        self.user_id = OWNER_USER
        self.session_hash = token_digest(SESSION)

    def new_app(self, name: str = "Other") -> str:
        return create_app(name, {"url": MCP_URL}, ALL_REACH, db=get_db())["id"]

    async def start(self, *, app_id: str | None = None, config: dict | None = None, metadata_url: str = "") -> dict:
        return await mcp_oauth_flow.start(
            app_id or self.app_id, "Linear", config or {"url": MCP_URL},
            user_id=self.user_id, session_hash=self.session_hash, metadata_url=metadata_url,
        )

    def audit(self, app_id: str | None = None) -> list[str]:
        rows = get_db().execute(
            "SELECT action FROM connected_app_audit WHERE app_id=? ORDER BY rowid", (app_id or self.app_id,),
        ).fetchall()
        return [row["action"] for row in rows]


async def browse(redirect: str, *, headers: dict | None = None) -> httpx.Response:
    """The person's browser following the provider's redirect to the loopback callback."""
    async with httpx.AsyncClient(follow_redirects=False) as client:
        return await client.get(redirect, headers=headers)


@pytest.fixture
async def env(isolated_db, monkeypatch):
    fake = FakeAS()
    connects: list[str] = []
    lost: list[str] = []
    monkeypatch.setattr(mcp_oauth_protocol, "new_client", fake.client)
    monkeypatch.setattr(mcp_oauth_flow, "_connect_in_background", connects.append)

    async def report(app_id: str) -> None:
        lost.append(app_id)

    monkeypatch.setattr(mcp_oauth_auth, "_report_lost", report)
    seed_account(OWNER_USER, SESSION, kind="session", db=get_db())
    app_id = create_app("Linear", {"url": MCP_URL}, ALL_REACH, db=get_db())["id"]
    mcp_oauth_flow._flows.clear()
    mcp_oauth_flow._results.clear()
    mcp_oauth_auth._locks.clear()
    yield Env(fake, app_id, connects, lost)
    await mcp_oauth_flow.stop_all()


class Sinks:
    """Every log sink a leak could reach: stdlib records and loguru messages."""

    def __init__(self, caplog: pytest.LogCaptureFixture) -> None:
        self.caplog = caplog
        self.loguru: list[str] = []

    def text(self) -> str:
        return "\n".join([record.getMessage() + str(record.exc_text or "") for record in self.caplog.records] + self.loguru)

    def assert_no(self, *secrets_: str) -> None:
        text = self.text()
        for value in secrets_:
            assert value not in text, "a secret reached a log sink"


@pytest.fixture
def sinks(caplog):
    caplog.set_level(logging.DEBUG)
    mcp_oauth_secrets.install()
    holder = Sinks(caplog)
    sink = logger.add(lambda message: holder.loguru.append(str(message)), level="DEBUG", format="{message}")
    yield holder
    logger.remove(sink)
