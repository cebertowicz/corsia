"""`GET /messages` Focus view and range read, through the real route and schema."""

from __future__ import annotations

import json
from urllib.parse import parse_qsl

import pytest
from auth_fixtures import OWNER_TOKEN

_AUTH = {"token": OWNER_TOKEN}

# (id, role, content, metadata): human, work, work, said-to-you, work, said-to-you.
_ROWS = [
    ("a1", "user", "go", {}),
    ("a2", "assistant", "", {"turn_id": "T1", "llm_tool_calls": [{"id": "c1", "function": {"name": "Read", "arguments": "{}"}}]}),
    ("a3", "assistant", "", {"turn_id": "T1", "reasoning": "thinking"}),
    ("a4", "assistant", "done", {"turn_id": "T1", "for_user": True}),
    ("a5", "assistant", "", {"turn_id": "T2", "reasoning": "again"}),
    ("a6", "assistant", "more", {"turn_id": "T2", "for_user": True}),
]


def _seed(client) -> str:
    from infra.database import get_db

    sid = client.post("/api/threads", params=_AUTH, json={"title": "Focus route"}).json()["thread_id"]
    db = get_db()
    try:
        for seq, (mid, role, content, meta) in enumerate(_ROWS, start=1):
            db.execute(
                "INSERT INTO messages (id, thread_id, role, content, metadata, created_at, seq) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"{sid}-{mid}", sid, role, content, json.dumps(meta), f"2026-06-12T00:00:{seq:02d}", seq),
            )
        db.commit()
    finally:
        db.close()
    return sid


@pytest.mark.integration
class TestFocusRoute:
    def _get(self, client, sid, query):
        return client.get(f"/api/threads/{sid}/messages", params={**_AUTH, **dict(parse_qsl(query))})

    def test_focus_view_returns_visible_rows_and_run_stubs(self, integration_client):
        sid = _seed(integration_client)
        data = self._get(integration_client, sid, "view=focus&limit=10").json()

        assert [m["id"].removeprefix(f"{sid}-") for m in data["messages"]] == ["a1", "a4", "a6"]
        assert [(r["first_id"].removeprefix(f"{sid}-"), r["count"]) for r in data["runs"]] == [
            ("a2", 2), ("a5", 1),
        ]
        assert all("fold_id" not in run for run in data["runs"])
        assert data["has_more"] is False
        assert data["oldest_id"] == f"{sid}-a1"

    def test_the_default_read_is_unchanged_by_the_focus_view(self, integration_client):
        sid = _seed(integration_client)
        data = self._get(integration_client, sid, "limit=10").json()
        assert len(data["messages"]) == len(_ROWS)
        assert "runs" not in data

    def test_the_range_read_is_inclusive_and_pages(self, integration_client):
        sid = _seed(integration_client)
        query = f"limit=1&from_id={sid}-a2&to_id={sid}-a3"
        first = self._get(integration_client, sid, query).json()
        assert [m["id"] for m in first["messages"]] == [f"{sid}-a3"]
        assert first["has_more"] is True
        rest = self._get(integration_client, sid, f"{query}&before={first['oldest_id']}").json()
        assert [m["id"] for m in rest["messages"]] == [f"{sid}-a2"]
        assert rest["has_more"] is False

    def test_an_unknown_bound_is_an_empty_page(self, integration_client):
        sid = _seed(integration_client)
        response = self._get(integration_client, sid, f"limit=5&from_id=nope&to_id={sid}-a3")
        assert response.status_code == 200
        assert response.json()["messages"] == []

    def test_an_inverted_range_is_400(self, integration_client):
        sid = _seed(integration_client)
        response = self._get(integration_client, sid, f"limit=5&from_id={sid}-a3&to_id={sid}-a2")
        assert response.status_code == 400

    @pytest.mark.parametrize("query", [
        "view=focus",
        "view=other&limit=5",
        "from_id=x&to_id=y",
        "limit=5&from_id=x",
        "limit=5&view=focus&from_id=x&to_id=y",
    ])
    def test_malformed_focus_and_range_reads_are_400(self, integration_client, query):
        sid = _seed(integration_client)
        assert self._get(integration_client, sid, query).status_code == 400

    def test_a_thread_that_is_not_the_users_is_404(self, integration_client):
        assert self._get(integration_client, "nonexistent", "view=focus&limit=5").status_code == 404
