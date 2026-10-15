"""Read-only Gmail history pagination and expiry handling."""

from __future__ import annotations

import pytest

from connections.google import client
from events.connectors import gmail_feed as feed


async def test_history_pages_are_exhausted_before_the_cursor_advances(monkeypatch):
    calls: list[dict[str, str]] = []
    pages = [
        {"historyId": "h2", "nextPageToken": "next", "history": [
            {"messagesAdded": [{"message": {"id": "one"}}, {"message": {"id": "two"}}]},
        ]},
        {"historyId": "h3", "history": [
            {"messagesAdded": [{"message": {"id": "two"}}, {"message": {"id": "three"}}]},
        ]},
    ]

    async def get_json(_url: str, *, params: dict[str, str]):
        calls.append(dict(params))
        return pages.pop(0)

    monkeypatch.setattr(feed.client, "get_json", get_json)
    page = await feed.added_messages("h1")

    assert page == feed.HistoryPage(("one", "two", "three"), "h3")
    assert calls[0]["startHistoryId"] == "h1"
    assert calls[1]["pageToken"] == "next"


async def test_expired_history_is_distinct_from_an_ordinary_google_failure(monkeypatch):
    async def rejected(_url: str, *, params: dict[str, str]):
        raise client.GoogleRequestFailed(404)

    monkeypatch.setattr(feed.client, "get_json", rejected)
    with pytest.raises(feed.GmailHistoryExpired):
        await feed.added_messages("expired")
