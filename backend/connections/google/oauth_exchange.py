"""Exchange a code for the person's own Google Desktop client."""

import httpx

from connections.google.client import TIMEOUT_SECONDS, TOKEN_URL, GoogleRequestFailed, GoogleSignInError


async def exchange(code: object, *, verifier: str | None, client_id: str, client_secret: str | None,
                   redirect_uri: str, denied: str, browser_denied: str) -> dict:
    """Exchange one authorization code without exposing provider errors."""
    if not isinstance(code, str) or not code:
        raise GoogleSignInError(denied, browser_message=browser_denied)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            response = await http.post(TOKEN_URL, data={
                "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                "client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect_uri,
            })
    except httpx.HTTPError:
        raise GoogleRequestFailed() from None
    if response.status_code != 200:
        raise GoogleSignInError(denied, browser_message=browser_denied)
    try:
        return response.json()
    except ValueError:
        raise GoogleSignInError(denied, browser_message=browser_denied) from None
