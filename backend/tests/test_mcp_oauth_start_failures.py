"""A sign-in that fails to start is remembered, so the app's row keeps saying why after a refresh."""

from __future__ import annotations

import pytest

from oauth_fixtures import env, sinks  # noqa: F401
from vendor_account_fixtures import isolated_shutdown_tasks  # noqa: F401
import tools.mcp_oauth_flow as flow
from tools.mcp_oauth_errors import SignInError


@pytest.mark.parametrize(
    ("status", "failure_class"),
    [(401, "not_approved"), (403, "not_approved"), (429, "app_busy"), (503, "app_busy"), (500, "app_error"), (502, "app_error")],
)
async def test_a_failed_registration_is_named_for_what_it_was(env, status, failure_class):
    env.fake.register_status = status
    env.fake.register_body = "Forbidden"  # Figma answers a refusal with plain text

    with pytest.raises(SignInError) as failure:
        await env.start()

    assert failure.value.failure_class == failure_class


@pytest.mark.parametrize(
    ("status", "body", "failure_class", "words"),
    [
        (400, {"error": "invalid_client_metadata", "error_description": "redirect_uris must use https"}, "unknown", "redirect_uris must use https"),
        (400, {"error": "invalid_redirect_uri"}, "address_mismatch", None),
        (422, "nope", "unknown", None),
        (404, {"error": "not_found"}, "unknown", "not_found"),
    ],
)
async def test_a_bad_request_is_never_called_a_refusal(env, status, body, failure_class, words):
    env.fake.register_status = status
    env.fake.register_body = body

    with pytest.raises(SignInError) as failure:
        await env.start()

    assert failure.value.failure_class == failure_class and failure.value.words == words


async def test_an_app_that_cannot_register_clients_still_needs_a_client_id(env):
    env.fake.dcr = False

    with pytest.raises(SignInError) as failure:
        await env.start()

    assert failure.value.failure_class == "address_mismatch"


async def test_a_sign_in_that_failed_to_start_survives_a_status_read_and_is_audited(env):
    env.fake.register_status = 403

    with pytest.raises(SignInError):
        await env.start()

    assert flow.status(env.app_id)["status"] == "failed"
    assert flow.status(env.app_id)["failure_class"] == "not_approved"
    assert env.audit() == ["sign_in_failed:not_approved"]


async def test_pressing_sign_in_again_clears_the_remembered_failure(env):
    env.fake.register_status = 403
    with pytest.raises(SignInError):
        await env.start()
    env.fake.register_status = 201

    started = await env.start()

    assert started["reopened"] is False and flow.status(env.app_id)["status"] == "pending"
