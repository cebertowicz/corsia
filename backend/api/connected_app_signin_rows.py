"""What a connected-app row shows about its sign-in. Never a token, link or provider body."""
from __future__ import annotations


def sign_in_view(config: object, app_id: str, *, person: bool, db) -> dict[str, object] | None:
    """Sign-in facts for an app at a web address; None for a local app."""
    if not isinstance(config, dict) or not isinstance(config.get("url"), str):
        return None
    import tools.mcp_oauth_flow as flow
    import tools.mcp_oauth_store as store

    grant = store.load(app_id, db=db)
    if grant is not None and not grant.issued_for(config["url"]):
        grant = None  # issued for an address the app no longer has: it reads as needing a sign-in
    view: dict[str, object] = {**flow.status(app_id), "signed_in": grant is not None}
    if person and grant is not None and grant.account:
        view["account"] = grant.account
    return view
