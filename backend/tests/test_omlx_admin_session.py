"""oMLX admin routes take a session cookie, not the key: one shared sign-in per server.

The fake server mirrors oMLX 0.7.0 (checked against a real server): ``login`` is
refused until a key exists, every admin route answers 401 without the session
cookie, and ``setup-api-key`` works once.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from llm.providers import omlx_endpoints as ep
from llm.providers import omlx_lifecycle
from omlx_admin_fixtures import CFG, KEY, OTHER_KEY, omlx  # noqa: F401



def test_admin_calls_carry_the_session_cookie_and_never_the_key(omlx):
    response = asyncio.run(ep.admin_request(CFG, "GET", "/global-settings"))

    assert response.status_code == 200
    assert omlx.paths == ["/admin/api/login", "/admin/api/global-settings"]
    assert all(KEY not in (cookie or "") + auth for _m, _p, cookie, auth in omlx.requests)
    assert omlx.requests[1][2] == "omlx_admin_session=session-1"


def test_concurrent_calls_sign_in_once(omlx):
    async def burst():
        return await asyncio.gather(*(ep.admin_request(CFG, "GET", f"/stats?{n}") for n in range(8)))

    results = asyncio.run(burst())

    assert [r.status_code for r in results] == [200] * 8
    assert omlx.logins == 1


def test_a_rejected_session_signs_in_again_once_and_the_call_succeeds(omlx):
    asyncio.run(ep.admin_request(CFG, "GET", "/stats"))
    omlx.expire_next = 1

    response = asyncio.run(ep.admin_request(CFG, "GET", "/stats"))

    assert response.status_code == 200
    assert omlx.logins == 2


def test_a_second_401_is_returned_not_looped(omlx):
    asyncio.run(ep.admin_request(CFG, "GET", "/stats"))
    omlx.expire_next = 5

    response = asyncio.run(ep.admin_request(CFG, "GET", "/stats"))

    assert response.status_code == 401
    assert omlx.logins == 2  # one fresh sign-in, no more


def test_a_changed_registered_key_drops_the_old_session_before_any_401(omlx):
    asyncio.run(ep.admin_request(CFG, "GET", "/stats"))
    omlx.key = OTHER_KEY
    omlx.token = "session-new-owner"

    # The old cookie would 401 now; a new key must not even try it.
    response = asyncio.run(ep.admin_request({**CFG, "api_key": OTHER_KEY}, "GET", "/stats"))

    assert response.status_code == 200
    assert [p for p in omlx.paths].count("/admin/api/stats") == 2  # one call each, no 401 retry
    assert len(ep._sessions) == 1 and next(iter(ep._sessions))[1] == ep._key_digest(OTHER_KEY)


def test_a_server_with_no_key_makes_no_http_call_and_says_why(omlx):
    with pytest.raises(ep.OmlxAdminError) as caught:
        asyncio.run(ep.admin_request({**CFG, "api_key": None, "server_id": None}, "GET", "/stats"))

    assert str(caught.value) == ep.ADMIN_KEY_NEEDED
    assert omlx.requests == []


def test_a_refused_sign_in_reports_the_status_and_nothing_secret(omlx):
    omlx.key = OTHER_KEY

    with pytest.raises(ep.OmlxAdminError) as caught:
        asyncio.run(ep.admin_request(CFG, "GET", "/stats"))

    assert str(caught.value) == "oMLX admin sign-in failed (oMLX HTTP 401)"
    assert KEY not in str(caught.value)


def test_model_load_uses_the_same_cookie_path(omlx):
    result = asyncio.run(omlx_lifecycle.load_model(CFG, "some-model"))

    assert result["ok"] is True
    assert omlx.paths == ["/admin/api/login", "/admin/api/models/some-model/load"]
    assert omlx.requests[-1][2] == "omlx_admin_session=session-1" and omlx.requests[-1][3] == ""


def test_setup_admin_key_sets_the_first_key_once_and_a_retry_does_not_overwrite(omlx):
    omlx.key = None

    assert asyncio.run(ep.setup_admin_key(CFG, KEY)) is True
    assert omlx.key == KEY
    assert asyncio.run(ep.setup_admin_key(CFG, OTHER_KEY)) is False
    assert omlx.key == KEY  # the second attempt changed nothing


def test_setup_admin_key_reports_an_unexpected_status_without_the_key(omlx):
    omlx.key = None
    omlx.setup_status = 500

    with pytest.raises(ep.OmlxAdminError) as caught:
        asyncio.run(ep.setup_admin_key(CFG, KEY))

    assert str(caught.value) == "oMLX could not set its admin key (oMLX HTTP 500)"
    assert omlx.key is None


def test_no_key_cookie_or_setup_body_reaches_the_logs_or_the_session_cache_key(omlx, caplog):
    omlx.key = None
    caplog.set_level(logging.DEBUG)

    asyncio.run(ep.setup_admin_key(CFG, KEY))
    asyncio.run(ep.admin_request(CFG, "GET", "/stats"))
    omlx.expire_next = 5
    asyncio.run(ep.admin_request(CFG, "GET", "/stats"))  # the failing path logs too
    with pytest.raises(ep.OmlxAdminError):
        omlx.key = OTHER_KEY
        asyncio.run(ep.admin_request(CFG, "GET", "/stats"))

    text = caplog.text
    for secret in (KEY, "session-1", "session-2", "setup-cookie", "omlx_admin_session="):
        assert secret not in text
    assert KEY not in repr(list(ep._sessions))
