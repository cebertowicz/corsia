"""Resolution of the CORSIA auth token eval clients send.

Kept lazy on purpose. The token is a real credential minted per install, so
there is no default to fall back to, and resolving it at call time rather than
at import time means ``--token`` and a harness that exports the environment
variable after import are both honoured.
"""

from __future__ import annotations

import os
import secrets


TOKEN_ENV = "CORSIA_EVAL_TOKEN"

# The eval endpoint takes the credential as a query parameter of this name.
AUTH_QUERY_KEY = "token"


def resolve_eval_token(explicit: str | None = None) -> str:
    """Return the auth token eval requests use, or explain what is missing.

    ``explicit`` wins when given (the ``--token`` flag); otherwise the value
    comes from ``CORSIA_EVAL_TOKEN``. There is no hardcoded default: tokens
    live in the account store and are minted per install.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    value = os.environ.get(TOKEN_ENV)
    if value and value.strip():
        return value.strip()
    raise RuntimeError(
        f"No CORSIA auth token configured. Set {TOKEN_ENV} or pass --token. "
        "Mint one with `corsia token create --label eval` (or "
        "`corsia token service` for a short-lived one). No hardcoded default "
        "token is used."
    )


def mint_disposable_token(username: str = "eval", *, label: str = "eval") -> str:
    """Provision ``username`` in the running instance and mint its API token.

    For harnesses that boot a backend against a throwaway workspace: there is
    no built-in credential any more, so they provision one exactly the way an
    operator would. The account password is random and discarded, because only
    the minted token is ever used. Requires the schema to exist, so call it
    after the app has started.
    """
    from infra import accounts  # noqa: PLC0415

    if accounts.get_user(username) is None:
        accounts.create_user(
            username, secrets.token_urlsafe(16), first_run_wake=False,
        )
    minted, _ = accounts.issue_token(username, kind="api", label=label)
    return minted
