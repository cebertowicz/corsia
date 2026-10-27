"""A Google call answered with No Content is an empty result, not a refusal.

Gmail answers a ``fields``-masked list that matches nothing with HTTP 204 and no body.
"""

from __future__ import annotations

import httpx

from connections.google import client as google_client
from connections.google import gmail_read
from tests.test_google_email import GMAIL, mail  # noqa: F401  (fixture)
from tests.test_google_sign_in import client_file, conductor, google  # noqa: F401  (fixtures)


async def test_a_search_with_no_matches_is_an_empty_list(mail):
    inner = mail.fake.handler
    mail.fake.handler = lambda request: (
        httpx.Response(204) if str(request.url).split("?")[0] == f"{GMAIL}/messages" else inner(request)
    )
    assert await gmail_read.search(mail.account, "from:nobody@example.com", 10) == []


async def test_no_content_is_an_empty_body(google):
    google.handler = lambda request: httpx.Response(204)
    assert await google_client.request_json("GET", f"{GMAIL}/messages", account_id=None, token="t") == {}
