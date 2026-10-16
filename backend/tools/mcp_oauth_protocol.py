"""The OAuth wire protocol for hosted MCP servers: discovery, registration, tokens, revocation.

Plain ``httpx`` with redirects off and short timeouts. Everything a provider
sends is untrusted: only named fields are read, bodies are never logged or put
in an error, and every secret in a response is registered with the redactor from
the raw text, before it is parsed.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

import tools.mcp_oauth_secrets as mcp_oauth_secrets
from tools.mcp_oauth_errors import SignInError, from_provider_error

TIMEOUT = httpx.Timeout(10.0)
REVOKE_TIMEOUT = httpx.Timeout(5.0)
_BODY_MAX = 256 * 1024
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_SECRET_FIELD = re.compile(
    r'"(?:access_token|refresh_token|client_secret|registration_access_token|id_token)"\s*:\s*"([^"]{8,})"'
)
CLIENT_NAME = "CORSIA"


class RefreshRejected(Exception):
    """The provider ended the sign-in (invalid_grant): the person must sign in again."""


class RefreshUnavailable(Exception):
    """The provider could not be reached or was busy: keep the sign-in and try later."""


def new_client(**options) -> httpx.AsyncClient:
    """The one client shape for every provider request: no redirects, bounded time."""
    return httpx.AsyncClient(follow_redirects=False, timeout=options.pop("timeout", TIMEOUT), **options)


def check_url(url: object) -> str:
    """``url`` when it is an HTTPS address (or plain HTTP on this Mac) without credentials or fragment."""
    if not isinstance(url, str):
        raise SignInError("unknown")
    parts = urlsplit(url)
    secure = parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOOPBACK)
    if not secure or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise SignInError("unknown")
    return url


def origin_of(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or (443 if scheme == "https" else 80)


def note_secrets(raw: str) -> None:
    """Register every token-like field in a raw response body, valid JSON or not."""
    mcp_oauth_secrets.register(*_SECRET_FIELD.findall(raw))


async def _read_json(response: httpx.Response) -> dict | None:
    raw = response.text[:_BODY_MAX]
    note_secrets(raw)
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def _get_json(client: httpx.AsyncClient, url: str) -> dict | None:
    try:
        response = await client.get(check_url(url), headers={"Accept": "application/json"})
    except (httpx.HTTPError, SignInError):
        return None
    if response.status_code != 200:
        return None
    return await _read_json(response)


@dataclass
class Discovery:
    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    revocation_endpoint: str | None
    registration_endpoint: str | None
    userinfo_endpoint: str | None
    scope: str | None
    iss_supported: bool
    metadata_document_supported: bool


def _resource_metadata_urls(server_url: str) -> list[str]:
    parts = urlsplit(server_url)
    root = f"{parts.scheme}://{parts.netloc}/.well-known/oauth-protected-resource"
    path = parts.path.rstrip("/")
    return [root + path, root] if path else [root]


def _challenge_metadata_url(response: httpx.Response) -> str | None:
    for header in response.headers.get_list("www-authenticate"):
        match = re.search(r'resource_metadata="([^"]+)"', header)
        if match:
            return match.group(1)
    return None


async def _protected_resource(client: httpx.AsyncClient, server_url: str) -> dict | None:
    for url in _resource_metadata_urls(server_url):
        data = await _get_json(client, url)
        if data and isinstance(data.get("authorization_servers"), list) and data["authorization_servers"]:
            return data
    try:
        probe = await client.post(
            server_url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    except httpx.HTTPError:
        return None
    advertised = _challenge_metadata_url(probe) if probe.status_code == 401 else None
    if advertised and origin_of(advertised) == origin_of(server_url):
        data = await _get_json(client, advertised)
        if data and isinstance(data.get("authorization_servers"), list) and data["authorization_servers"]:
            return data
    return None


def _authorization_server_urls(issuer: str) -> list[str]:
    parts = urlsplit(issuer)
    root = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    if path:
        return [
            f"{root}/.well-known/oauth-authorization-server{path}",
            f"{root}/.well-known/openid-configuration{path}",
            f"{issuer.rstrip('/')}/.well-known/openid-configuration",
        ]
    return [f"{root}/.well-known/oauth-authorization-server", f"{root}/.well-known/openid-configuration"]


async def supports_oauth(client: httpx.AsyncClient, server_url: str) -> bool:
    """Whether the server advertises OAuth protected-resource metadata (C-R1)."""
    return await _protected_resource(client, server_url) is not None


async def discover(client: httpx.AsyncClient, server_url: str) -> Discovery:
    """RFC 9728 then RFC 8414 or OIDC discovery for one hosted server."""
    check_url(server_url)
    resource_meta = await _protected_resource(client, server_url)
    if resource_meta is None:
        raise SignInError("unknown")
    declared = resource_meta.get("resource")
    resource = declared if isinstance(declared, str) and origin_of(declared) == origin_of(server_url) else server_url
    issuer = resource_meta["authorization_servers"][0]
    if not isinstance(issuer, str):
        raise SignInError("unknown")
    check_url(issuer)
    metadata = None
    for url in _authorization_server_urls(issuer):
        metadata = await _get_json(client, url)
        if metadata:
            break
    if not metadata or metadata.get("issuer") != issuer:
        raise SignInError("unknown")
    methods = metadata.get("code_challenge_methods_supported")
    if methods is not None and "S256" not in methods:
        raise SignInError("not_allowed")
    scopes = resource_meta.get("scopes_supported")
    return Discovery(
        resource=resource,
        issuer=issuer,
        authorization_endpoint=check_url(metadata.get("authorization_endpoint")),
        token_endpoint=check_url(metadata.get("token_endpoint")),
        revocation_endpoint=_optional_url(metadata.get("revocation_endpoint")),
        registration_endpoint=_optional_url(metadata.get("registration_endpoint")),
        userinfo_endpoint=_optional_url(metadata.get("userinfo_endpoint")),
        scope=" ".join(scopes) if isinstance(scopes, list) and all(isinstance(item, str) for item in scopes) and scopes else None,
        iss_supported=metadata.get("authorization_response_iss_parameter_supported") is True,
        metadata_document_supported=metadata.get("client_id_metadata_document_supported") is True,
    )


def _optional_url(url: object) -> str | None:
    try:
        return check_url(url) if url else None
    except SignInError:
        return None


@dataclass
class ClientRegistration:
    client_id: str
    source: str
    client_secret: str | None = None
    registration_access_token: str | None = None


async def register_client(
    client: httpx.AsyncClient, disc: Discovery, redirect_uri: str, *, pasted_client_id: str | None,
    metadata_url: str,
) -> ClientRegistration:
    """A pasted client ID, else CORSIA's published metadata document, else dynamic registration."""
    if pasted_client_id:
        return ClientRegistration(pasted_client_id, "pasted")
    if metadata_url and disc.metadata_document_supported:
        return ClientRegistration(check_url(metadata_url), "metadata")
    if not disc.registration_endpoint:
        raise SignInError("address_mismatch")
    try:
        response = await client.post(disc.registration_endpoint, json={
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "application_type": "native",
        })
    except httpx.HTTPError:
        raise SignInError("app_busy") from None
    data = await _read_json(response)
    if response.status_code not in (200, 201) or not data or not isinstance(data.get("client_id"), str):
        raise SignInError("address_mismatch")
    secret, token = data.get("client_secret"), data.get("registration_access_token")
    return ClientRegistration(
        data["client_id"], "dcr",
        secret if isinstance(secret, str) else None, token if isinstance(token, str) else None,
    )


def new_pkce() -> tuple[str, str]:
    """A fresh PKCE verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(64)
    challenge = urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    mcp_oauth_secrets.register(verifier)
    return verifier, challenge


def new_state() -> str:
    state = secrets.token_urlsafe(32)
    mcp_oauth_secrets.register(state)
    return state


def authorization_url(
    disc: Discovery, *, client_id: str, redirect_uri: str, state: str, challenge: str,
) -> str:
    query = {
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": disc.resource,
    }
    if disc.scope:
        query["scope"] = disc.scope
    parts = urlsplit(disc.authorization_endpoint)
    joined = (parts.query + "&" if parts.query else "") + urlencode(query)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, joined, ""))


@dataclass
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: float
    scope: str | None


def _tokens_from(data: dict | None, status: int) -> Tokens:
    if data and isinstance(data.get("error"), str):
        raise from_provider_error(data["error"], data.get("error_description"))
    if status in (429, 503):
        raise SignInError("app_busy")
    if status >= 500:
        raise SignInError("app_error")
    data = data or {}
    access = data.get("access_token")
    token_type = data.get("token_type")
    if status != 200 or not isinstance(access, str) or not access or str(token_type).lower() != "bearer":
        raise SignInError("unknown")
    refresh, scope, expires = data.get("refresh_token"), data.get("scope"), data.get("expires_in")
    lifetime = expires if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > 0 else 3600
    return Tokens(
        access, refresh if isinstance(refresh, str) and refresh else None,
        time.time() + float(lifetime), scope if isinstance(scope, str) else None,
    )


async def exchange_code(
    client: httpx.AsyncClient, disc: Discovery, *, client_id: str, code: str, redirect_uri: str, verifier: str,
    client_secret: str | None = None,
) -> Tokens:
    """Trade a single-use authorization code (and the PKCE verifier) for tokens."""
    form = {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": client_id, "code_verifier": verifier, "resource": disc.resource,
    }
    if client_secret:
        form["client_secret"] = client_secret
    try:
        response = await client.post(disc.token_endpoint, data=form)
    except httpx.TimeoutException:
        raise SignInError("app_busy") from None
    except httpx.HTTPError:
        raise SignInError("app_error") from None
    return _tokens_from(await _read_json(response), response.status_code)


async def refresh_tokens(
    client: httpx.AsyncClient, *, token_endpoint: str, client_id: str, refresh_token: str, resource: str,
    client_secret: str | None = None,
) -> Tokens:
    """Trade a refresh token for new tokens, or say why the sign-in cannot continue."""
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id, "resource": resource}
    if client_secret:
        form["client_secret"] = client_secret
    try:
        response = await client.post(check_url(token_endpoint), data=form)
    except (httpx.HTTPError, SignInError):
        raise RefreshUnavailable from None
    data = await _read_json(response)
    if data and data.get("error") in ("invalid_grant", "invalid_client", "unauthorized_client"):
        raise RefreshRejected
    if response.status_code in (400, 401, 403) and not (data and data.get("error")):
        raise RefreshRejected
    try:
        return _tokens_from(data, response.status_code)
    except SignInError:
        raise RefreshUnavailable from None


async def revoke(client: httpx.AsyncClient, endpoint: str | None, *, token: str, client_id: str) -> bool:
    """Tell the provider to forget a token (RFC 7009). True only when it answered success."""
    if not endpoint:
        return False
    try:
        response = await client.post(
            check_url(endpoint), data={"token": token, "client_id": client_id, "token_type_hint": "refresh_token"},
            timeout=REVOKE_TIMEOUT,
        )
    except (httpx.HTTPError, SignInError):
        return False
    return response.status_code == 200


async def account_label(client: httpx.AsyncClient, disc: Discovery, access_token: str) -> str | None:
    """The signed-in account's email or name when the provider offers it; None otherwise."""
    if not disc.userinfo_endpoint:
        return None
    try:
        response = await client.get(disc.userinfo_endpoint, headers={"Authorization": f"Bearer {access_token}"}, timeout=REVOKE_TIMEOUT)
    except httpx.HTTPError:
        return None
    data = await _read_json(response) if response.status_code == 200 else None
    for key in ("email", "preferred_username", "name"):
        value = data.get(key) if data else None
        if isinstance(value, str) and 0 < len(value) <= 120:
            return value
    return None
